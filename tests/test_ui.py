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


class LiveView(Scenario):
    def test_shared_needs_sources_have_the_same_item_id(self):
        root = self.init("alice")
        self.b(root, "new", "app", "--title", "App")
        status = root / "nodes/app/status.md"
        with status.open("a") as f:
            f.write("\n## Blocked\n\n- Choose a color; waiting on operator; clears when chosen; "
                    "since: 2020-01-01 00:00 +00:00; recommendation: blue; default: gray\n")
        with (root / "nodes/app/inbox.md").open("a") as f:
            f.write("\n- 2026-01-01 00:00 +00:00 from worker: question: Which shape?\n")
        state = json.loads(self.b(root, "ui", "--json"))
        for item in (i for i in state["inbox"] if i["node"] == "app" and i["kind"] in ("blocked-overdue", "inbox")):
            due = next(d for d in state["due"] if d["node"] == "app" and d["kind"] == item["kind"])
            self.assertEqual(due.get("id"), item["id"])

    def test_unavailable_live_runtime_stops_new_calls_and_keeps_a_failed_gate(self):
        import contextlib
        import importlib.util
        import io
        from unittest import mock
        spec = importlib.util.spec_from_file_location("brain_live_under_test", CORE / "tests" / "live.py")
        live = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(live)
        calls = []
        def unavailable(run, *args, **kwargs):
            calls.append(run.scenario.__name__)
            raise live.RuntimeUnavailable("provider session limit")
        def first(run):
            run.agent(self.tmp, "test")
        def second(run):
            run.agent(self.tmp, "test")
        mkdtemp = tempfile.mkdtemp
        def temporary(*args, **kwargs):
            kwargs.setdefault("dir", self.tmp)
            return mkdtemp(*args, **kwargs)
        output = io.StringIO()
        with mock.patch.object(live.Run, "agent", unavailable), \
             mock.patch.object(live, "SCENARIOS", [first, second]), \
             mock.patch.object(live.tempfile, "mkdtemp", temporary), \
             mock.patch.object(sys, "argv", ["live.py", "--runtimes", "codex", "--jobs", "1"]), \
             mock.patch.dict(os.environ, {"LIVE_CODEX_BIN": sys.executable}), \
             contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as ended:
            live.main()
        self.assertEqual(ended.exception.code, 1)
        self.assertEqual(calls, ["first"])
        self.assertIn("BLOCKED", output.getvalue())
        self.assertIn("not started: codex unavailable", output.getvalue())

    def test_ui_serves_a_read_only_live_state_to_this_machine_only(self):
        import socket
        import urllib.error
        import urllib.request
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        st = r / "nodes" / "app" / "status.md"
        st.write_text(st.read_text().replace("| --- | --- | --- | --- |\n",
                      "| --- | --- | --- | --- |\n| job-a | [job a](t3-thread://v1/t1) (Codex) | - | building |\n")
                      .replace("## Blocked / needs the operator\n", "## Blocked / needs the operator\n\n- The API key\n"))
        self.git("commit", "-qam", "app: job a started", cwd=r)
        self.b(r, "wt", "add", "app", "job-a")
        port = json.loads(self.b(r, "ui", "--json"))   # the same state, once
        app = next(n for n in port["nodes"] if n["id"] == "app")
        self.assertEqual(app["work"][0]["link"], "t3-thread://v1/t1")
        self.assertEqual(app["work"][0]["runtime"], "Codex")
        self.assertEqual(app["worktrees"][0]["branch"], "job-a")
        need = next(i for i in port["attention"] if i["node"] == "app" and i["kind"] == "blocked")
        self.assertEqual(need["text"], "The API key")
        self.assertFalse(need["escalated"])
        self.assertIn("age_hours", need)
        self.assertGreaterEqual(sum(port["activity"]["counts"].get("app", [])), 1)   # "app: …" commits count for app
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            free = sk.getsockname()[1]
        p = subprocess.Popen([sys.executable, str(r / "core" / "bin" / "brain"), "ui", "--port", str(free),
                              "--every", "0.5"], cwd=r, env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            core = p.stdout.readline()   # the first line names the core that serves the page, and its commit
            self.assertTrue(core.startswith("brain ui core: "), core)
            self.assertIn(" @ ", core)
            line = p.stdout.readline()
            full = line.split(": ", 1)[1].split()[0]
            self.assertTrue(full.startswith("http://127.0.0.1:"))
            url, key = full.split("?k=")
            page = urllib.request.urlopen(url, timeout=5).read()
            self.assertIn(b"<title>", page)
            self.assertIn(b'<div id="root">', page)
            self.assertNotIn(b"The API key", page)  # public shell contains no brain snapshot
            assets = re.findall(rb'(?:src|href)="(\./assets/[^"?]+)"', page)
            self.assertGreaterEqual(len(assets), 2)
            for asset in assets:
                response = urllib.request.urlopen(url + asset.decode()[2:], timeout=5)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertGreater(len(response.read()), 0)
            # Runtime notices and font license ship alongside the static assets.
            notices = urllib.request.urlopen(url + "assets/THIRD_PARTY_NOTICES.txt", timeout=5).read()
            self.assertIn(b"react ", notices)
            self.assertIn(b"SIL OPEN FONT LICENSE", urllib.request.urlopen(url + "assets/Geist-OFL.txt", timeout=5).read())
            # A rebuild while serving: the shell follows the files on disk, never stale asset names.
            shell = r / "core/ui/dist/index.html"
            rebuilt = page.replace(b"<title>", b"<title>Rebuilt ")
            shell.write_bytes(rebuilt)
            self.assertEqual(urllib.request.urlopen(url, timeout=5).read(), rebuilt)
            shell.write_bytes(page)
            script = next(a for a in assets if a.endswith(b".js"))
            self.assertIn(b"__STATE__", urllib.request.urlopen(url + script.decode()[2:], timeout=5).read())
            for path in ("assets/../index.html", "assets/%2e%2e%2findex.html", "assets/%2e%2e%5cindex.html", "assets/.hidden", "assets/nope.js", "assets/%00.js", "assets/a%00b", "assets/%0A.js", "assets/%FF.js"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(url + path, timeout=5)
                self.assertEqual(e.exception.code, 404)
            outside = self.tmp / "private.txt"
            outside.write_text("must not be served")
            (r / "core/ui/dist/assets/outside.txt").symlink_to(outside)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(url + "assets/outside.txt", timeout=5)
            self.assertEqual(e.exception.code, 404)
            for endpoint in ("api/events", "api/events?k=wrong", "api/state?k=wrong"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(url + endpoint, timeout=5)
                self.assertEqual(e.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as e:   # another account on this machine has no key
                urllib.request.urlopen(url + "api/state", timeout=5)
            self.assertEqual(e.exception.code, 403)
            state = json.loads(urllib.request.urlopen(url + "api/state?k=" + key, timeout=5).read())
            self.assertEqual(state["brain"]["name"], "alice")
            found = json.loads(urllib.request.urlopen(url + "api/search?kind=node&q=ap&k=" + key, timeout=5).read())
            self.assertEqual([i["id"] for i in found["items"]], ["app"])          # the picker's server search
            for endpoint in ("api/search?q=ap", "api/search?q=ap&k=wrong"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(url + endpoint, timeout=5)
                self.assertEqual(e.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(url + "api/search?offset=x&k=" + key, timeout=5)
            self.assertEqual(e.exception.code, 400)
            self.assertEqual(set(state), set(port))  # --json contract and live snapshot agree
            first_snapshot = state

            req = urllib.request.Request(url + "api/state?k=" + key, headers={"Host": "evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(e.exception.code, 403)              # another site can't read it through a browser
            ev = urllib.request.urlopen(url + "api/events?k=" + key, timeout=10)
            first = ev.readline()
            self.assertTrue(first.startswith(b"data: {"))
            streamed = json.loads(first[len(b"data: "):])
            self.assertEqual(set(streamed), set(first_snapshot))
            self.assertEqual(streamed["brain"]["name"], first_snapshot["brain"]["name"])
            ev.readline()
            # a change in the files reaches an open page without a reload
            (r / "nodes" / "app" / "inbox.md").write_text("- 2026-01-01 00:00 from worker: question: which key?\n")
            deadline = time.time() + 10
            got = b""
            while time.time() < deadline and b"which key?" not in got:
                got = ev.readline()
            self.assertIn(b"which key?", got)
            ev.close()
        finally:
            p.terminate()
            p.wait(timeout=5)
            p.stdout.close()


class ExpandedUI(Scenario):
    def setUp(self):
        super().setUp()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env["BRAIN_CONFIG"] = str(self.tmp / "config" / "brains.json")
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        loader = core_loader.SourceFileLoader("brain_ui_test", str(CORE / "bin/brain"))
        self.m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.m)

    def test_ui_scan_matches_cli_and_does_not_write(self):
        root = self.init("alice")
        first = self.repo(self.tmp / "code" / "first")
        second = self.repo(self.tmp / "elsewhere" / "second")
        self.b(root, "new", "first", "--title", "First", "--home", str(first))
        expected = json.loads(self.b(root, "scan", str(first.parent), str(second.parent), "--json"))
        def files():
            return {str(p.relative_to(self.tmp)): p.read_bytes() for p in self.tmp.rglob("*")
                    if p.is_file() and not p.is_symlink()}
        before = files()
        state = json.loads(self.b(root, "ui", "--json", "--scan", str(first.parent),
                                  "--scan", str(second.parent)))
        self.assertEqual(state["scan"], expected)
        self.assertEqual(files(), before)
        self.assertEqual([c["adoptable"] for c in state["scan"]], [False, True])
        self.assertEqual(json.loads(self.b(root, "ui", "--json"))["scan"], [])
        self.assertIn("every scan path must be a directory", self.b(root, "ui", "--json", "--scan",
                                                                   str(self.tmp / "missing"), ok=False))

    def test_ui_exposes_due_and_complete_ledger_without_polling(self):
        root = self.init("alice")
        self.b(root, "new", "project", "--title", "Project")
        inst = self.m.Instance(root)
        node = inst.node("project")
        rows = [{"Job":f"job-{i}", "Started":"2026-01-01T00:00:00+00:00", "Ended":"2026-01-01T01:00:00+00:00",
                 "Hours":"1", "Reviews":"2", "Outcome":"merged", "Lesson check":"required"} for i in range(65)]
        self.m.write_jobs(inst, node, rows)
        with mock.patch.object(self.m, "usage_refresh", side_effect=AssertionError("UI must never poll")):
            state = self.m.ui_state(inst)
        ui_node = next(n for n in state["nodes"] if n["id"] == "project")
        self.assertEqual(len(ui_node["jobs"]["rows"]), 65)
        self.assertEqual(ui_node["jobs"]["median_reviews"], 2)
        self.assertEqual(len([d for d in state["due"] if d["kind"] == "lesson-outcome"]), 65)
        # The live view adds only each job's branch name; everything else is `brain due --json`.
        self.assertEqual([d.get("branch") for d in state["due"] if d["kind"] == "lesson-outcome"][:2], ["job-0", "job-1"])
        self.assertEqual([{k: v for k, v in d.items() if k != "branch"} for d in state["due"]], json.loads(self.b(root, "due", "--json")))

    def test_ui_search_ranks_folds_and_pages(self):
        names = ["Mariana", "Bruno Ana", "Ánalyst", "Ana Lima", "ana", "Zed"]
        state = {"nodes": [{"id": f"n{i}", "title": t, "updated": f"2026-01-0{i + 1}"} for i, t in enumerate(names)]}
        found = self.m.ui_search(state, "node", "ana")
        # exact, then name starts (accent-insensitive, ties alphabetical), then a later word, then contains
        self.assertEqual([i["title"] for i in found["items"]], ["ana", "Ana Lima", "Ánalyst", "Bruno Ana", "Mariana"])
        self.assertEqual(found["total"], 5)
        self.assertEqual(self.m.ui_search(state, "node", "LIMA")["items"][0]["title"], "Ana Lima")
        self.assertEqual(self.m.ui_search(state, "node", "nothing like it")["items"], [])
        self.assertEqual([i["title"] for i in self.m.ui_search(state, "node", "")["items"]][:2], ["Zed", "ana"])  # recent first
        arabic = {"nodes": [{"id": "a", "title": "مدرسة أحمد"}]}
        self.assertEqual(len(self.m.ui_search(arabic, "node", "مدرسه احمد")["items"]), 1)  # ta marbuta and alef forms
        many = {"nodes": [{"id": f"p{i:02}", "title": f"Project {i:02}"} for i in range(45)],
                "specialists": [{"id": "rev", "node": "p00", "title": "Project reviewer", "jobs": []}]}
        pages = [self.m.ui_search(many, "node", "project", offset, 20) for offset in (0, 20, 40)]
        self.assertEqual([p["next"] for p in pages], [20, 40, None])
        self.assertEqual(sum(len(p["items"]) for p in pages), 45)
        self.assertEqual({i["kind"] for i in self.m.ui_search(many, "all", "project", 0, 100)["items"]}, {"node", "specialist"})
        self.assertEqual(self.m.ui_search(many, "specialist", "rev")["items"][0]["secondary"], "Project 00 · 0 hires")

    def test_ui_sender_comes_from_the_record_and_due_names_its_job(self):
        sender = self.m.inbox_sender
        self.assertEqual(sender({"kind": "inbox", "text": "2026-01-01 09:30 +01:00 from worker: question: which key?"}), "worker")
        self.assertEqual(sender({"kind": "blocked", "text": "Waiting from Morgan: a review"}), "")
        self.assertEqual(sender({"kind": "inbox", "text": "a question with no record of who from x: y"}), "")
        root = self.init("alice")
        self.b(root, "new", "project", "--title", "Project")
        inst = self.m.Instance(root)
        row = {"Job": "job-a", "Started": "2026-01-01T00:00:00+00:00", "Ended": "", "Hours": "", "Reviews": "0", "Outcome": ""}
        self.m.write_jobs(inst, inst.node("project"), [row])
        due = [{"node": "project", "kind": "handback", "text": "job-a: ready to merge", "job": self.m.job_key(row)}]
        with mock.patch.object(self.m, "due_items", side_effect=lambda inst, n, *a, **k: due if n["id"] == "project" else []):
            state = self.m.ui_state(self.m.Instance(root))
        self.assertEqual([d.get("branch") for d in state["due"] if d.get("job")], ["job-a"])
        (root / "nodes/project/inbox.md").write_text("- 2026-01-01 00:00 from worker: question: which key?\n")
        state = self.m.ui_state(self.m.Instance(root))
        self.assertEqual([i["from"] for i in state["inbox"] if i["kind"] == "inbox"], ["worker"])

    def test_ui_from_a_core_worktree_serves_that_worktree(self):
        other = self.tmp / "instance"
        (other / "core" / "bin").mkdir(parents=True)
        (other / "core" / "bin" / "brain").write_text("# the instance's own core\n")
        worktree = self.tmp / "core-worktree"
        worktree.mkdir()
        calls = []
        with mock.patch.object(self.m, "try_find_root", return_value=other), \
                mock.patch.object(self.m.os, "execv", side_effect=lambda *a: calls.append(a)), \
                mock.patch.dict(os.environ, {"BRAIN_NO_REEXEC": ""}):
            for git_entry, command, expected in (("file", "ui", 0), ("dir", "ui", 1), ("file", "status", 2)):
                os.environ["BRAIN_NO_REEXEC"] = ""   # a re-exec sets it for the process it starts
                shutil.rmtree(worktree / ".git", ignore_errors=True)
                (worktree / ".git").unlink() if (worktree / ".git").is_file() else None
                (worktree / ".git").write_text("gitdir: elsewhere\n") if git_entry == "file" else (worktree / ".git").mkdir()
                with mock.patch.object(self.m, "CORE", worktree), mock.patch.object(self.m.sys, "argv", ["brain", command]):
                    self.m.reexec_into_instance_core()
                self.assertEqual(len(calls), expected, (git_entry, command))

    def test_ui_policy_and_new_data_redact_home_terms(self):
        root = self.init("alice")
        self.b(root, "new", "project", "--title", "Project")
        (root / "nodes/project/never-name.txt").write_text("HiddenName\n")
        cfg = Path(self.env["BRAIN_CONFIG"]).parent
        (cfg / "subscriptions.json").write_text(json.dumps({"stop_percent":73, "subscriptions":[]}))
        (cfg / "usage.json").write_text(json.dumps({"poll_interval_seconds":300, "subscriptions":[], "accounts":{}}))
        with mock.patch.object(self.m, "due_items", return_value=[{"node":"project", "text":"HiddenName action", "reasons":["HiddenName"]}]):
            state = self.m.ui_state(self.m.Instance(root), scan=[{"name":"HiddenName", "path":"/tmp/HiddenName"}])
        self.assertNotIn("HiddenName", json.dumps({k:state[k] for k in ("due", "scan")}))
        self.assertEqual(state["usage_policy"], {"stop_percent":73, "max_age_seconds":600})
        (cfg / "subscriptions.json").write_text("not json")
        state = self.m.ui_state(self.m.Instance(root))
        self.assertIsNone(state["usage_policy"]["stop_percent"])



if __name__ == "__main__":
    unittest.main()
