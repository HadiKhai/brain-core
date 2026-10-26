"""Scenario tests for core/bin/brain. Run with `brain selftest` (or python3 -m unittest in tests/).

Every test builds instances in a throwaway folder that is also $HOME, so nothing real is touched.
"""
import json
import re
import datetime
import importlib.util
import core_loader
import threading
from unittest import mock
import argparse
import contextlib
import io
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CORE = Path(os.environ.get("BRAIN_CORE") or Path(__file__).resolve().parent.parent)


from scenario import Scenario, AutonomyProject, shlex_quote


class Connections(Scenario):
    """Learning across nodes: items and specialists from several sources, review in bulk, recall, connect."""
    fixture_templates = True

    fixture_init_in_process = True

    H = "a" * 40

    def setUp(self):
        super().setUp()
        self.r, self.homes = self.cached_setup('connections', self.connection_fixture)

    def connection_fixture(self):
        self.r = self.init("alice")
        for p in ("ben", "cleo"):
            self.fixture_b(self.r, "principal", "add", p)
        self.homes = {}
        for nid, owners in (("app", ""), ("lab", ""), ("client", "alice,ben"), ("cleos", "cleo")):
            h = self.repo(self.tmp / f"{nid}-home", {"main.ts": f"export const {nid} = 1\n", "README.md": "hi\n"})
            self.homes[nid] = h
            self.fixture_b(self.r, "new", nid, "--title", nid.title(), "--home", str(h), *(["--owners", owners] if owners else []))
        self.fixture_b(self.r, "new", "studio", "--title", "Studio")
        for nid in ("studio/a", "studio/b"):
            self.fixture_b(self.r, "new", nid, "--title", nid)
        with open(self.r / "nodes/app/decisions.md", "a") as fh:
            fh.write("\n## 2026-01-01 10:00 +00:00: Queue host KV writes\nWhy: the host drops writes above 10/s.\n")
        (self.r / "nodes/lab/knowledge.md").write_text("# Knowledge\n\n## Failures and fixes\n\n- Writes dropped; a queue fixed it.\n")
        (self.r / "nodes/lab/never-name.txt").write_text("Quokkasoft\n")
        self.git("add", "-A", cwd=self.r)
        self.git("commit", "-qm", "fixture", cwd=self.r)
        return self.r, self.homes


    def add(self, iid, *args, ok=True):
        return self.b(self.r, "item", "add", iid, "--title", iid.replace("-", " "), "--kind", "method", "--ref", "x",
                      *args, ok=ok)

    def propose(self, iid, src, body="Queue the writes.", *args, ok=True):
        return self.add(iid, "--from", src, "--repo", "brain/alice", "--commit", self.H, "--proposed", "--body", body,
                        *args, ok=ok)

    def connect_at(self, root, *args, now):
        # Exercise the real CLI and operation lifecycle through the existing
        # isolated loader/Instance clock seam, without changing production time.
        m = core_loader.SourceFileLoader('connect_clock', str(CORE / 'bin/brain')).load_module()
        output = io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(root)}), \
                mock.patch.object(sys, 'argv', ['brain', *args]), \
                mock.patch.object(m.Instance, 'now_dt', return_value=now), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            m.main()
        return output.getvalue()

    def test_private_connect_modes_keep_shared_bytes_across_clock_boundary(self):
        r = self.r
        before = (r / 'status.md').read_bytes()
        refreshed = re.search(rb'^Facts refreshed: (.+)$', before, re.M).group(1).decode()
        clock = datetime.datetime.strptime(refreshed, '%Y-%m-%d %H:%M %z')
        modes = [((), True), (('plan',), True), (('plan', '--fresh'), True),
                 (('--fresh', '--json'), True), (('brief', 'app'), False),
                 (('brief', 'app', '--save'), True)]
        for i, (args, writes) in enumerate(modes, 1):
            with self.subTest(args=args):
                count = len((r / '.brain/events.jsonl').read_text().splitlines())
                self.connect_at(r, 'connect', 'root', *args, now=clock + datetime.timedelta(minutes=i))
                self.assertEqual((r / 'status.md').read_bytes(), before)
                self.assertEqual(self.git('status', '--porcelain', cwd=r), '')
                records = [json.loads(line) for line in (r / '.brain/events.jsonl').read_text().splitlines()][count:]
                if writes:
                    self.assertEqual([row['kind'] for row in records], ['begin', 'commit'])
                    self.assertEqual(records[0]['ref'], records[1]['ref'])
                    self.assertEqual(records[1]['data']['command'], 'connect')
                    self.assertTrue(records[1]['data']['maintenance_ok'])
                else:
                    self.assertEqual(records, [])
        self.assertIn('app', (r / '.brain/connect/root/briefs/app.md').read_text())

    def test_a_plan_deletes_nothing_in_a_connect_folder_moved_away(self):
        from core_loader import SourceFileLoader
        self.b(self.r, "connect", "root")
        conn = self.r / ".brain/connect/root"
        (conn / "keep.txt").write_text("keep\n")
        m = SourceFileLoader("brain_plan_move", str(CORE / "bin/brain")).load_module()
        check, calls, away = m.connect_inside, [], self.homes["app"] / "moved"

        def check_then_move(*args):  # moved right after the folder was first checked
            check(*args)
            calls.append(1)
            if len(calls) == 1:
                os.rename(conn, away)
        m.connect_inside, m.find_root = check_then_move, (lambda: self.r)
        a = argparse.Namespace(id="root", action="plan", nodes=[], final=False, save=False, fresh=True,
                               only=None, runtime=None, model=None, to=None, json=False)
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            m.cmd_connect(a)
        self.assertEqual((away / "keep.txt").read_text(), "keep\n")

    def test_a_saved_brief_never_lands_where_its_folder_moved(self):
        from core_loader import SourceFileLoader
        self.b(self.r, "connect", "root")
        self.b(self.r, "connect", "root", "brief", "app", "--save")
        m = SourceFileLoader("brain_connect_move", str(CORE / "bin/brain")).load_module()
        briefs = self.r / ".brain/connect/root/briefs"
        put = m.history_put

        def put_then_move(fd, name, text):
            put(fd, name, text)
            if name.startswith(".brief-"):
                os.rename(briefs, self.homes["app"] / "moved")
        m.history_put = put_then_move
        a = argparse.Namespace(id="root", action="brief", nodes=["lab"], final=False, save=True, fresh=False,
                               only=None, runtime=None, model=None, to=None, json=False)
        m.find_root = lambda: self.r
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            m.cmd_connect(a)
        self.assertEqual(sorted(p.name for p in (self.homes["app"] / "moved").iterdir()), ["app.md"])

    def test_a_connect_folder_swapped_for_a_link_is_not_trusted(self):
        from core_loader import SourceFileLoader
        self.b(self.r, "connect", "root")
        m = SourceFileLoader("brain_connect_swap", str(CORE / "bin/brain")).load_module()
        conn, away = self.r / ".brain/connect", self.homes["app"] / "connect"
        put = m.history_put

        def put_then_swap(fd, name, text):  # moved into a home, and a link left where it was
            put(fd, name, text)
            if name.startswith(".brief-"):
                os.rename(conn, away)
                conn.symlink_to(away)
        m.history_put = put_then_swap
        m.find_root = lambda: self.r
        a = argparse.Namespace(id="root", action="brief", nodes=["lab"], final=False, save=True, fresh=False,
                               only=None, runtime=None, model=None, to=None, json=False)
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            m.cmd_connect(a)
        self.assertEqual([p.name for p in (away / "root/briefs").iterdir()], [])

    def test_items_and_specialists_from_several_nodes(self):
        r = self.r
        self.add("kv-queue", "--from", "app,lab", "--keywords", "rate limit, dropped writes", "--body", "Queue them.")
        text = (r / "library/kv-queue.md").read_text()
        for line in ("source_node: app", "source_nodes: app, lab", "scope: alice", "keywords: rate limit, dropped writes"):
            self.assertIn(line, text)
        self.b(r, "usable", "kv-queue", "lab")
        self.b(r, "usable", "kv-queue", "client", ok=False)   # ben was never in scope
        self.add("pair", "--from", "studio/a,studio/b", "--body", "x")
        self.assertTrue((r / "nodes/studio/library/pair.md").exists())  # their lowest common ancestor (axiom 13)
        self.assertIn("can't see every source", self.add("pair2", "--from", "studio/a,studio/b", "--at", "app",
                                                          "--body", "x", ok=False))
        self.add("pair3", "--from", "studio/a,studio/b", "--at", "root", "--body", "x")
        self.assertIn("share no owner", self.add("none", "--from", "app,cleos", "--body", "x", ok=False))
        self.add("mixed", "--from", "app,client", "--body", "x")
        self.assertIn("scope: alice\n", (r / "library/mixed.md").read_text())
        self.assertIn("--consented-by alice,ben", self.add("wide", "--from", "app,client", "--scope", "alice,ben",
                                                           "--body", "x", ok=False))
        self.add("wide", "--from", "app,client", "--scope", "alice,ben", "--consented-by", "alice,ben", "--body", "x")
        self.b(r, "usable", "wide", "client")
        self.propose("leak", "app,lab", "Ask Quokkasoft.", ok=False)  # lab's never-name word, though app is first
        self.propose("leak", "app,lab", "x", "--keywords", "Quokkasoft", ok=False)
        self.assertFalse((r / "library/leak.md").exists())
        self.propose("p-shared", "app,lab")
        self.propose("p-studio", "studio/a,studio/b")
        self.propose("p-app", "app")
        self.assertIn("✓", self.b(r, "check"))
        inbox = self.b(r, "inbox", "root")
        self.assertIn("studio: [", inbox)
        self.assertIn("1 item(s) proposed", inbox)
        # specialists drawn from several nodes
        self.b(r, "specialist", "new", "host-engineer", "--from", "app,lab", "--title", "Host engineer", "--proposed")
        role = (r / "specialists/host-engineer/role.md").read_text()
        self.assertIn("source_nodes: app, lab", role)
        self.assertIn("status: proposed", role)
        self.assertIn("wider than what", self.b(r, "specialist", "new", "x", "--from", "app,client", "--title", "X",
                                                "--scope", "alice,ben", ok=False))
        self.b(r, "specialist", "new", "studio-reviewer", "--from", "studio/a,studio/b", "--title", "R", "--proposed")
        self.assertTrue((r / "nodes/studio/specialists/studio-reviewer/role.md").exists())
        self.b(r, "hire", "host-engineer", "app", "--task", "t", ok=False)
        # one review sitting
        sheet = self.b(r, "proposals")
        self.assertIn("proposal(s) waiting for review", sheet)
        for wanted in ("## root", "## studio", "## app", "`p-shared` (method)", "from app, lab", "Queue the writes.",
                       "specialist `host-engineer`", "brain item accept p-shared --reviewed-by <you>",
                       "brain specialist accept studio-reviewer --at studio"):
            self.assertIn(wanted, sheet)
        self.assertNotIn("p-app", self.b(r, "proposals", "studio"))
        rows = json.loads(self.b(r, "proposals", "--json"))
        self.assertEqual({x["node"] for x in rows}, {"root", "studio", "app"})
        head = self.git("rev-parse", "HEAD", cwd=r)
        self.b(r, "item", "accept", "p-shared", "kv-queue", "--reviewed-by", "alice", ok=False)  # kv-queue is active
        self.assertEqual(head, self.git("rev-parse", "HEAD", cwd=r))
        self.assertIn("status: proposed", (r / "library/p-shared.md").read_text())
        out = self.b(r, "item", "accept", "p-shared", "p-studio", "--reviewed-by", "alice")
        self.assertIn("p-studio: accepted", out)
        self.assertEqual(sorted(self.git("show", "--name-only", "--format=", "HEAD", cwd=r).split()),
                         ["library/p-shared.md", "nodes/studio/library/p-studio.md"])
        self.b(r, "item", "reject", "p-app", "missing", ok=False)
        self.assertIn("status: proposed", (r / "nodes/app/library/p-app.md").read_text() if
                      (r / "nodes/app/library/p-app.md").exists() else (r / "library/p-app.md").read_text())
        self.b(r, "item", "reject", "p-app")
        self.b(r, "specialist", "accept", "host-engineer", "--reviewed-by", "alice")
        self.b(r, "hire", "host-engineer", "app", "--task", "t")
        self.b(r, "hire", "host-engineer", "client", "--task", "t", ok=False)  # scoped to alice alone
        self.assertNotIn("## root", self.b(r, "proposals"))
        self.assertIn("✓", self.b(r, "check"))
        (r / "library/kv-queue.md").write_text(text.replace("source_nodes: app, lab", "source_nodes: app, gone"))
        self.assertIn("source_nodes names 'gone'", self.b(r, "check", ok=False))

    def test_recall_and_briefs_find_what_other_nodes_learned(self):
        r = self.r
        self.add("kv-queue", "--from", "app", "--keywords", "rate limit", "--body", "Throttle host writes; at most 10/s.")
        self.propose("evictions", "app", "Statements evict silently; resubmit.", "--keywords", "evicted")
        out = self.b(r, "recall", "lab", "our", "writes", "are", "throttled", "by", "the", "host")
        self.assertIn("`kv-queue` (method, from app)", out)
        self.assertIn("problem: host, throttled, writes", out)
        self.assertIn("`kv-queue`", self.b(r, "recall", "lab", "throttling"))  # the same word in another form
        self.assertIn("nothing learned yet", self.b(r, "recall", "client", "writes dropped by the host"))
        out = self.b(r, "recall", "lab", "statement evicted")
        self.assertIn("1 unreviewed proposal(s) also match: evictions", out)
        self.assertNotIn("`evictions`", out)  # counted, never offered as usable
        rows = json.loads(self.b(r, "recall", "lab", "host writes", "--json"))
        self.assertEqual([x["id"] for x in rows["items"]], ["kv-queue"])
        self.assertIn("nothing learned yet", self.b(r, "recall", "lab", "unrelated words entirely"))
        self.b(r, "recall", "lab", "a", ok=False)
        brief = self.b(r, "brief", "lab", "--task", "Writing to the host keeps getting throttled")
        self.assertIn("`kv-queue`", brief)  # stems: "writing" meets "writes", "throttled" meets nothing it needs
        self.assertIn("brain recall lab", brief)
        self.assertIn("nodes/lab/knowledge.md", brief)
        self.assertNotIn("knowledge.md", self.b(r, "brief", "app", "--task", "t"))
        (r / "nodes/client/knowledge.md").write_text("# K\n\n- built with care\n")
        self.assertNotIn("knowledge.md", self.b(r, "brief", "client", "--task", "t"))  # sealed: the task carries it
        self.b(r, "specialist", "new", "tester", "--title", "Tester")
        self.assertIn("brain recall app", self.b(r, "hire", "tester", "app", "--task", "host writes"))

    def test_connect_plans_digests_checks_and_gates_the_connector(self):
        r = self.r
        (r / "nodes/studio/nodes/a/decisions.md").write_text("# Decisions\n\n" + "\n".join(
            f"## 2026-01-01 10:{i % 60:02d} +00:00: Decision {i}\nWhy: " + "because " * 40 for i in range(400)) + "\n")
        self.propose("p-app", "app")
        self.git("add", "-A", cwd=r)
        self.git("commit", "-qm", "more", cwd=r)
        before = self.git("status", "--porcelain", cwd=r)
        status_before = (r / 'status.md').read_text()
        refreshed = re.search(r'^Facts refreshed: (.+)$', status_before, re.M).group(1)
        boundary = datetime.datetime.strptime(refreshed, '%Y-%m-%d %H:%M %z') + datetime.timedelta(minutes=1)
        started = time.monotonic()
        summary = self.connect_at(r, "connect", "root", now=boundary)
        print('connect-clock-evidence: ' + json.dumps(dict(
            clock_before=refreshed, clock_after=boundary.isoformat(), seconds=time.monotonic() - started,
            status_before=status_before, status_after=(r / 'status.md').read_text(),
            porcelain_before=before, porcelain_after=self.git('status', '--porcelain', cwd=r),
            diff=self.git('diff', '--', 'status.md', cwd=r))))
        self.assertIn("owners differ", summary)
        self.assertIn("client: ", summary)
        d = r / ".brain/connect/root"
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")
        self.assertEqual(before, self.git("status", "--porcelain", cwd=r))  # nothing in shared state
        plan = json.loads((d / "plan.json").read_text())
        rows = {x["id"]: x for x in plan["nodes"]}
        self.assertNotIn("root", rows)
        self.assertEqual(plan["snapshot"], self.git("rev-parse", "HEAD", cwd=r).strip())
        app = [f["ref"] for f in rows["app"]["files"]]
        self.assertIn("app:decisions.md", app)
        self.assertIn("item:p-app", app)
        self.assertNotIn("app:brief.md", app)  # an untouched template isn't worth a read
        big = next(f for f in rows["studio/a"]["files"] if f["ref"] == "studio/a:decisions.md")["parts"]
        self.assertGreater(len(big), 1)
        self.assertEqual([p["first_line"] for p in big[1:]], [p["last_line"] + 1 for p in big[:-1]])
        self.assertTrue(all(p["tokens"] <= 20000 for p in big))
        self.assertFalse(rows["studio/a"]["code"])
        self.assertTrue(rows["app"]["code"])
        brief = self.b(r, "connect", "root", "brief", "app", "--runtime", "codex")
        for wanted in ("app:decisions.md#1", "app:code", "skills/connect/SKILL.md", "`read: <id>`", "done: <node>",
                       "[<node>@<path in its home>]", "brain scrub app"):
            self.assertIn(wanted, brief)
        self.b(r, "connect", "root", "brief", "nobody", ok=False)
        self.b(r, "connect", "root", "brief", ok=False)
        self.assertIn("aren't finished", self.b(r, "connect", "root", "brief", "--final", ok=False))
        self.assertIn("not started", self.b(r, "connect", "root", "check", ok=False))
        digests = d / "digests"

        def digest(nid, extra=""):
            reads = [p["id"] for f in rows[nid]["files"] for p in f["parts"]] + ([f"{nid}:code"] if rows[nid]["code"] else [])
            cite = {"app": "[app:decisions.md] [app@main.ts] [item:p-app]"}.get(nid, f"[{nid}:status.md]")
            (digests / f"{nid.replace('/', '+')}.md").write_text(
                f"## Failures and fixes\n- writes dropped, queued {cite}\n" + "".join(f"read: {x}\n" for x in reads)
                + extra + f"done: {nid}\n")
        for nid in rows:
            digest(nid)
        self.b(r, "connect", "root", "check")
        for i in range(31):
            (self.homes["app"] / f"part{i}.ts").write_text(f"export const p{i} = {i}\n")
        digest("app", "".join(f"- part {i} [app@part{i}.ts] [app@part{i}.ts#L1]\n" for i in range(30)))  # + main.ts
        self.assertIn("cites 31 code files, over the 30", self.b(r, "connect", "root", "check", ok=False))
        self.assertIn("aren't finished and clean: app", self.b(r, "connect", "root", "brief", "--final", ok=False))
        digest("app", "".join(f"- part {i} [app@part{i}.ts]\n" for i in range(29)))
        self.b(r, "connect", "root", "check")
        digest("app", "".join(f"- spelled [app@{'./' * i}main.ts]\n" for i in range(31)))  # one file, 31 spellings
        self.b(r, "connect", "root", "check")
        digest("app", "- [app@../lab-home/main.ts] [app:nope.md] [item:ghost] [studio/a@main.ts] [lab@main.ts]\n")
        self.assertIn("4 to nothing that exists", self.b(r, "connect", "root", "check", ok=False))
        digest("app")
        digest("lab", "- Quokkasoft said so\n")
        self.assertIn("1 unsafe line(s)", self.b(r, "connect", "root", "check", ok=False))
        digest("lab")
        (digests / "client.md").write_text((digests / "client.md").read_text().replace("done: client\n", ""))
        self.assertIn("client: 3 of 3 read", self.b(r, "connect", "root", "check", ok=False))
        self.b(r, "connect", "root", "brief", "--final", ok=False)
        digest("client")
        (digests / "client.md").write_text((digests / "client.md").read_text().replace("read: client:code\n", ""))
        self.assertIn("client: 2 of 3 read", self.b(r, "connect", "root", "check", ok=False))
        digest("client")
        final = self.b(r, "connect", "root", "brief", "--final")
        for wanted in ("themes.md", "digest:app#1:", "`read: <id>`", "--from <node>,<node>", f"--commit {plan['snapshot']}",
                       "--repo \"brain/alice\"", "connections.md", "brain specialist new <id> --from", "drafts",
                       "client owned by alice, ben", "Never accept"):
            self.assertIn(wanted, final)
        self.assertNotIn("Quokkasoft", final)
        (d / "themes.md").write_text("## Writes dropped\n- app and lab [app:decisions.md]\nread: digest:app#1\n")
        self.assertIn(f"1 of {len(rows)} digest reads done", self.b(r, "connect", "root", "check", ok=False))
        (d / "themes.md").write_text("## Writes dropped\n" + "".join(f"read: digest:{n}#1\n" for n in rows))
        self.b(r, "connect", "root", "check")
        self.assertIn("digests exist", self.b(r, "connect", "root", ok=False))
        self.assertIn(f"{len(rows)} nodes to compare", self.b(r, "connect", "root", "plan"))
        self.b(r, "connect", "root", "--fresh", "--only", "app,lab")
        self.assertFalse((digests / "app.md").exists())
        self.assertEqual(len(json.loads((d / "plan.json").read_text())["nodes"]), 2)
        self.assertIn("fewer than two", self.b(r, "connect", "root", "--fresh", "--only", "app", ok=False))
        self.assertIn("not below", self.b(r, "connect", "studio", "--only", "app", ok=False))
        self.assertEqual(before, self.git("status", "--porcelain", cwd=r))

    def test_connect_review_round_one(self):
        r = self.r
        # A link planted at .brain/connect never sends a deletion or a write elsewhere.
        victim = self.homes["client"] / "root"
        victim.mkdir()
        (victim / "user-data.txt").write_text("keep me\n")
        (r / ".brain").mkdir(exist_ok=True)
        (r / ".brain/connect").symlink_to(self.homes["client"])
        self.assertIn("lead out of the brain", self.b(r, "connect", "root", ok=False))
        self.assertEqual((victim / "user-data.txt").read_text(), "keep me\n")
        self.assertFalse((victim / "plan.json").exists())
        (r / ".brain/connect").unlink()
        (r / ".brain/connect").mkdir()
        (r / ".brain/connect/root").symlink_to(victim)
        self.assertIn("lead out of the brain", self.b(r, "connect", "root", ok=False))
        self.assertTrue((victim / "user-data.txt").exists())
        (r / ".brain/connect/root").unlink()
        # Distinct node ids never share a digest.
        self.b(r, "new", "studio/x", "--title", "X")
        self.b(r, "new", "studio/x/y", "--title", "XY")
        self.b(r, "new", "studio/x--y", "--title", "X--Y")
        for nid in ("studio/x/y", "studio/x--y"):
            (r / ("nodes/studio/nodes/x/nodes/y" if nid == "studio/x/y" else "nodes/studio/nodes/x--y")
             / "knowledge.md").write_text("# K\n\n- " + nid + "\n")
        # A line too long for one read is cut into character ranges.
        (r / "nodes/lab/knowledge.md").write_text("# K\n\n- " + "word " * 30000 + "\n- short\n")
        (r / "nodes/lab/sources").mkdir(exist_ok=True)
        (r / "nodes/lab/sources/import-proposals.md").write_text("# Imported + unreviewed\n\n- Run the linter first.\n")
        self.git("add", "-A", cwd=r)
        self.git("commit", "-qm", "more", cwd=r)
        self.b(r, "connect", "studio")
        brief = self.b(r, "connect", "studio", "brief", "studio/x/y", "studio/x--y")
        self.assertIn("digests/studio+x+y.md", brief)
        self.assertIn("digests/studio+x--y.md", brief)
        self.b(r, "connect", "root", "--only", "app,lab")
        plan = json.loads((r / ".brain/connect/root/plan.json").read_text())
        parts = next(f for f in next(x for x in plan["nodes"] if x["id"] == "lab")["files"]
                     if f["ref"] == "lab:knowledge.md")["parts"]
        self.assertTrue(all(p["tokens"] <= 20000 for p in parts))
        self.assertTrue(any(p.get("chars") for p in parts))
        self.assertIn("characters 1-59997", self.b(r, "connect", "root", "brief", "lab"))
        # Malformed citations and uncited notes don't pass.
        rows = {x["id"]: x for x in plan["nodes"]}
        for nid in rows:
            reads = [p["id"] for f in rows[nid]["files"] for p in f["parts"]] + [f"{nid}:code"]
            (r / ".brain/connect/root/digests" / f"{nid}.md").write_text(
                f"- fine [{nid}:status.md]\n" + "".join(f"read: {x}\n" for x in reads) + f"done: {nid}\n")
        self.b(r, "connect", "root", "check")
        digest = r / ".brain/connect/root/digests/app.md"
        digest.write_text("- claim [app:missing file.md]\n" + digest.read_text())
        self.assertIn("1 to nothing that exists", self.b(r, "connect", "root", "check", ok=False))
        digest.write_text(digest.read_text().replace("[app:missing file.md]", "with no source"))
        self.assertIn("1 note line(s) cite nothing", self.b(r, "connect", "root", "check", ok=False))
        self.b(r, "connect", "root", "brief", "--final", ok=False)
        # The review sheet carries unreviewed rules in full.
        sheet = self.b(r, "proposals", "lab")
        self.assertIn("### rules: nodes/lab/sources/import-proposals.md", sheet)
        self.assertIn("Run the linter first.", sheet)

    def test_connect_review_round_two(self):
        r = self.r
        # Widening learning from several nodes needs every source owner, not only those still in scope.
        self.add("mixed", "--from", "app,client", "--body", "x")
        self.assertIn("every source owner", self.b(r, "item", "scope", "mixed", "alice,ben", "--consented-by", "alice",
                                                   ok=False))
        self.b(r, "item", "scope", "mixed", "alice,ben", "--consented-by", "alice,ben")
        self.b(r, "usable", "mixed", "client")
        # A source's never-name words travel with what was learned from it, wherever it goes.
        self.add("lab-note", "--from", "lab", "--keywords", "host writes", "--body", "Quokkasoft queues host writes.")
        self.assertIn("nothing learned yet", self.b(r, "recall", "app", "host writes"))
        self.assertNotIn("Quokkasoft", self.b(r, "brief", "app", "--task", "host writes"))
        self.assertNotIn("Quokkasoft", self.b(r, "item", "show", "lab-note", "--node", "app", ok=False))
        self.b(r, "specialist", "new", "host-engineer", "--from", "app,lab", "--title", "Host engineer", "--proposed")
        role = r / "specialists/host-engineer/role.md"
        role.write_text(role.read_text() + "\n## Standards\n\n- Ask Quokkasoft first.\n")
        out = self.b(r, "specialist", "accept", "host-engineer", "--reviewed-by", "alice", ok=False)
        self.assertIn("edit it before accepting", out)
        self.assertNotIn("Quokkasoft", self.b(r, "proposals"))
        self.assertNotIn("Quokkasoft", self.b(r, "proposals", "--json"))
        role.write_text(role.read_text().replace("status: proposed", "status: active"))  # activated by hand
        out = self.b(r, "hire", "host-engineer", "app", "--task", "t", ok=False)
        self.assertNotIn("Quokkasoft", out)
        role.write_text(role.read_text().replace("Ask Quokkasoft first.", "Queue writes."))
        self.b(r, "hire", "host-engineer", "app", "--task", "t")
        self.propose("shared", "app,lab", "Queue writes for Wallabyco.")
        (r / "nodes/lab/never-name.txt").write_text("Quokkasoft\nWallabyco\n")  # forbidden after it was proposed
        for args in (("proposals",), ("proposals", "--json")):
            self.assertNotIn("Wallabyco", self.b(r, *args))
        self.assertIn("edit it before accepting", self.b(r, "item", "accept", "shared", "--reviewed-by", "alice", ok=False))
        # The connect plan's JSON is screened like its summary.
        node_md = r / "nodes/app/node.md"
        node_md.write_text(node_md.read_text().replace("title: App", "title: App for Quokkasoft"))
        self.git("add", "-A", cwd=r)
        self.git("commit", "-qm", "more", cwd=r)
        self.assertNotIn("Quokkasoft", self.b(r, "connect", "root", "--only", "app,lab", "--json"))
        self.assertNotIn("Quokkasoft", self.b(r, "connect", "root", "plan"))
        # Reads stay within budget for any script: the budget counts encoded bytes.
        (r / "nodes/app/knowledge.md").write_text("# K\n\n- " + "\u6f22\u5b57" * 40000 + "\n")
        self.git("add", "-A", cwd=r)
        self.git("commit", "-qm", "cjk", cwd=r)
        self.b(r, "connect", "root", "--only", "app,lab", "--fresh")
        plan = json.loads((r / ".brain/connect/root/plan.json").read_text())
        rows = {x["id"]: x for x in plan["nodes"]}
        parts = [p for f in rows["app"]["files"] for p in f["parts"]]
        self.assertTrue(all(p["tokens"] <= 20000 for p in parts), [p["tokens"] for p in parts])
        cjk = [p for p in parts if p.get("chars")]
        self.assertGreater(len(cjk), 1)
        self.assertEqual([p["chars"][0] for p in cjk[1:]], [p["chars"][1] + 1 for p in cjk[:-1]])
        # Citation-shaped typos count, in any case; prose brackets and links don't.
        for nid in rows:
            reads = [p["id"] for f in rows[nid]["files"] for p in f["parts"]] + [f"{nid}:code"]
            (r / ".brain/connect/root/digests" / f"{nid}.md").write_text(
                f"- fine [{nid}:status.md] [TODO: later] [https://example.test/a]\n"
                + "".join(f"read: {x}\n" for x in reads) + f"done: {nid}\n")
        self.b(r, "connect", "root", "check")
        digest = r / ".brain/connect/root/digests/app.md"
        digest.write_text("- typo [APP:missing.md] [app:status.md]\n" + digest.read_text())
        self.assertIn("1 to nothing that exists", self.b(r, "connect", "root", "check", ok=False))
        digest.write_text(digest.read_text().replace("- typo [APP:missing.md] [app:status.md]\n", ""))
        # A dirty themes file stops the connector from resuming.
        self.b(r, "connect", "root", "brief", "--final")
        (r / ".brain/connect/root/themes.md").write_text("## T\n- uncited claim\nread: digest:app#1\n")
        self.assertIn("themes.md has", self.b(r, "connect", "root", "brief", "--final", ok=False))

    def test_connect_review_round_three(self):
        r = self.r
        # A node's never-name words stay out of its own digest, its reader's brief and the overview; another
        # node's knowledge may name them (the words guard that node's home, not every file). Learning holding a
        # word of where it came from is shown nowhere: connect skips it and says so.
        self.propose("shared", "app,lab", "Queue writes.")
        self.add("quokkasoft-tip", "--from", "app", "--body", "x")  # lab's word, in app's own knowledge
        self.add("lab-secret", "--from", "lab", "--body", "Ask Quokkasoft.")
        node_md = r / "nodes/app/node.md"
        node_md.write_text(node_md.read_text().replace("title: App", "title: App for Quokkasoft"))
        self.git("add", "-A", cwd=r)
        self.git("commit", "-qm", "title", cwd=r)
        self.b(r, "connect", "root", "--only", "app,lab")
        for args in (("plan",), ("plan", "--json"), ("brief", "lab")):
            self.assertNotIn("uokkasoft", self.b(r, "connect", "root", *args))
        self.assertIn("item:quokkasoft-tip#1", self.b(r, "connect", "root", "brief", "app"))
        self.assertIn("1 item(s) or specialist(s) skipped", self.b(r, "connect", "root", "plan"))
        plan = json.loads((r / ".brain/connect/root/plan.json").read_text())
        rows = {x["id"]: x for x in plan["nodes"]}
        self.assertNotIn("item:lab-secret", [f["ref"] for f in rows["lab"]["files"]])
        self.assertIn("item:shared", [f["ref"] for f in rows["app"]["files"]])
        digests = r / ".brain/connect/root/digests"

        def digest(nid, extra=""):
            reads = [p["id"] for f in rows[nid]["files"] for p in f["parts"]] + [f"{nid}:code"]
            (digests / f"{nid}.md").write_text(f"## Notes\n- fine [{nid}:status.md]\n{extra}"
                                               + "".join(f"read: {x}\n" for x in reads) + f"done: {nid}\n")
        digest("app", "- app names it: Quokkasoft [item:quokkasoft-tip]\n")
        digest("lab", "- lab names it: Quokkasoft [lab:status.md]\n")
        out = self.b(r, "connect", "root", "check", ok=False)
        self.assertIn("lab: done; 2 citations; 1 unsafe line(s)", out)
        self.assertNotIn("app: done; 2 citations; 1 unsafe", out)
        self.b(r, "connect", "root", "brief", "--final", ok=False)
        digest("lab")
        # Every substantive line cites, prose and numbered notes included.
        digest("app", "A plain claim\n1. A numbered claim\n")
        self.assertIn("2 note line(s) cite nothing", self.b(r, "connect", "root", "check", ok=False))
        # A large digest is read in bounded, checkpointed reads.
        digest("app", "".join(f"- note {i} " + "detail " * 40 + "[app:status.md]\n" for i in range(400)))
        self.b(r, "connect", "root", "check")
        final = self.b(r, "connect", "root", "brief", "--final")
        self.assertIn("digest:app#2:", final)
        self.assertIn("digest:lab#1:", final)
        themes = r / ".brain/connect/root/themes.md"
        themes.write_text("## T\n- shared [app:status.md]\nread: digest:app#1\nread: digest:lab#1\n")
        self.assertIn("digest reads done", self.b(r, "connect", "root", "check", ok=False))
        # Proposal ids, paths and lessons are screened too; lessons show with their role.
        self.propose("wallabyco-note", "app", "Queue writes.")
        self.b(r, "specialist", "new", "host-engineer", "--from", "app,lab", "--title", "Host engineer", "--proposed")
        (r / "specialists/host-engineer/lessons.md").write_text(
            "# Lessons\n\n- 2026-01-01 10:00 +00:00 · from app · scope alice: Queue writes, never drop them.\n")
        sheet = self.b(r, "proposals")
        self.assertIn("Queue writes, never drop them.", sheet)
        (r / "nodes/app/never-name.txt").write_text("Wallabyco\n")  # forbidden by its source after proposing
        for args in (("proposals",), ("proposals", "--json")):
            self.assertNotIn("allabyco", self.b(r, *args))
        self.assertIn("\n\n## app", self.b(r, "proposals"))
        # Hire and review briefs carry the same pointers as worker briefs.
        (r / "nodes/app/knowledge.md").write_text("# K\n\n- built with care\n")
        self.b(r, "specialist", "new", "tester", "--title", "Tester")
        self.assertIn("nodes/app/knowledge.md", self.b(r, "hire", "tester", "app", "--task", "t"))

    def test_connect_review_round_four(self):
        r = self.r
        self.b(r, "connect", "root", "--only", "app,lab")
        # Briefs are saved only inside connect's own folder, never where a path could lead.
        out = self.b(r, "connect", "root", "brief", "app", "--save")
        saved = r / ".brain/connect/root/briefs/app.md"
        self.assertIn(".brain/connect/root/briefs/app.md", out)
        self.assertIn("app:decisions.md#1", saved.read_text())
        self.assertEqual(oct(saved.stat().st_mode & 0o777), "0o600")
        self.b(r, "connect", "root", "brief", "--final", "--save", ok=False)  # digests unfinished
        shutil.rmtree(r / ".brain/connect/root/briefs")
        victim = self.homes["client"] / "briefs"
        victim.mkdir()
        (r / ".brain/connect/root/briefs").symlink_to(victim)
        self.assertIn("lead out of the brain", self.b(r, "connect", "root", "brief", "app", "--save", ok=False))
        self.assertEqual(list(victim.iterdir()), [])
        self.b(r, "connect", "root", "brief", "app", "--out", "x.md", ok=False)  # no arbitrary destinations
        (r / ".brain/connect/root/briefs").unlink()
        # A lesson carries the words of where it was learned.
        self.b(r, "specialist", "new", "tester", "--title", "Tester")
        self.assertIn("lesson or its tags contain a never-name word", self.b(
            r, "lesson", "tester", "--from", "lab", "Quokkasoft wants green builds", ok=False))
        # Legacy records can predate write screening; their source and scope still govern reads.
        with (r / "specialists/tester/lessons.md").open("a") as f:
            f.write("- 2026-01-01 10:00 +00:00 · from lab · scope alice: Quokkasoft wants green builds\n")
        self.b(r, "lesson", "tester", "--from", "app", "Keep builds green")
        brief = self.b(r, "hire", "tester", "app", "--task", "t")
        self.assertIn("Keep builds green", brief)
        self.assertNotIn("uokkasoft", brief)
        # So does every lesson of a proposed specialist, wherever it is shown, accepted or read.
        self.b(r, "specialist", "new", "reviewer", "--at", "app", "--title", "Reviewer", "--proposed")
        self.assertIn("lesson or its tags contain a never-name word", self.b(
            r, "lesson", "reviewer", "--from", "lab", "Quokkasoft reviews twice", ok=False))
        with (r / "nodes/app/specialists/reviewer/lessons.md").open("a") as f:
            f.write("- 2026-01-01 10:00 +00:00 · from lab · scope alice: Quokkasoft reviews twice\n")
        for args in (("proposals",), ("proposals", "--json")):
            self.assertNotIn("uokkasoft", self.b(r, *args))
        self.assertIn("edit it before accepting", self.b(r, "specialist", "accept", "reviewer", "--at", "app",
                                                         "--reviewed-by", "alice", ok=False))
        self.b(r, "connect", "root", "--only", "app,lab", "--fresh")
        plan = json.loads((r / ".brain/connect/root/plan.json").read_text())
        app = next(x for x in plan["nodes"] if x["id"] == "app")
        self.assertFalse(any("reviewer" in f["ref"] for f in app["files"]))
        # Recall's JSON hides the node's own words, as its text does.
        (r / "nodes/lab/never-name.txt").write_text("Quokkasoft\nthrottling\n")
        self.add("throttle", "--from", "app", "--body", "Throttle writes.")
        out = self.b(r, "recall", "lab", "throttling", "writes", "--json")
        self.assertIn('"throttle"', out)
        self.assertNotIn("throttling", out)



if __name__ == "__main__":
    unittest.main()
