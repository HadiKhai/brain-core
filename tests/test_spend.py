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


class Spend(Scenario):
    """Time and tokens per job from the runtimes' own transcripts: counted once, attributed to the right job."""
    fixture_init_in_process = True

    def setUp(self):
        super().setUp()
        self.cfg = self.tmp / "config"
        self.cfg.mkdir()
        self.env["BRAIN_CONFIG"] = str(self.cfg / "brains.json")
        self.claude, self.codex, self.other = (self.tmp / d for d in (".claude-work", ".codex", ".claude-other"))
        subs = [{"id": "work-claude", "runtime": "claude", "config_dir": str(self.claude), "brains": ["alice"]},
                {"id": "codex", "runtime": "codex", "config_dir": str(self.codex), "brains": ["*"]},
                {"id": "someone-else", "runtime": "claude", "config_dir": str(self.other), "brains": ["bob"]}]
        (self.cfg / "subscriptions.json").write_text(json.dumps({"subscriptions": subs}))
        self.r = self.init("alice")
        self.home = self.repo(self.tmp / "app")
        self.git("remote", "add", "origin", "https://github.com/Acme/App.git", cwd=self.home)
        # Fabricated remote evidence is local: cleanup never fetches a default.
        self.git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.home)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=self.home)
        self.b(self.r, "new", "app", "--title", "App", "--home", str(self.home))
        self.b(self.r, "wt", "add", "app", "job")
        self.wt = self.tmp / "app-work/worktrees/job"
        self.node = self.r / "nodes/app"
        # The job started three hours ago; its sessions ran in the hours since.
        ledger = self.node / "job-ledger.md"
        started = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)).isoformat(timespec="seconds")
        ledger.write_text(re.sub(r"(\| job \| )[^|]+", lambda m: m.group(1) + started + " ", ledger.read_text()))
        self.git("commit", "-qam", "app: job started earlier", cwd=self.r)
        self.t0 = int(time.time()) - 2 * 3600

    def at(self, minutes):
        return datetime.datetime.fromtimestamp(self.t0 + minutes * 60, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    @staticmethod
    def jsonl(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))

    def folder(self, cwd, config=None):
        return (config or self.claude) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))

    def cl(self, cwd, sid, first, rows, config=None, path=None, start=0):
        """A Claude Code session: its first message, then assistant records; the timestamp comes last, as there."""
        def rec(kind, minute, **kw):
            return {"parentUuid": None, "isSidechain": False, "type": kind, **kw, "cwd": str(cwd), "sessionId": sid,
                    "timestamp": self.at(minute)}
        out = [rec("user", start, message={"role": "user", "content": first})]
        for row in rows:
            kind, minute = row[0], row[1]
            if kind == "said":
                mid, usage, blocks = row[2], row[3], row[4] if len(row) > 4 else [{"type": "text", "text": "ok"}]
                for b in blocks:  # one response, split into one record per block, each carrying the same usage
                    out.append(rec("assistant", minute, message={"id": mid, "model": "claude-opus-5-5", "role": "assistant",
                                                                 "content": [b], "usage": dict(zip(
                                                                     ("input_tokens", "cache_read_input_tokens",
                                                                      "cache_creation_input_tokens", "output_tokens"), usage))}))
            elif kind == "compact":
                out.append(rec("system", minute, subtype="compact_boundary",
                               compactMetadata={"trigger": "auto", "preTokens": row[2]}))
            elif kind == "tool_result":
                out.append(rec("user", minute, message={"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": row[2]}]}))
        target = path or self.folder(cwd, config) / f"{sid}.jsonl"
        self.jsonl(target, out)
        return target

    def cx(self, cwd, sid, items, began=0, **meta):
        when = datetime.datetime.fromtimestamp(self.t0 + began * 86400, datetime.timezone.utc).astimezone()
        folder = self.codex / "sessions" / when.strftime("%Y/%m/%d")
        rows = [{"timestamp": self.at(0), "type": "session_meta", "payload": {
            "id": sid, "cwd": str(cwd), "originator": "T3 Code", **meta}},
                {"timestamp": self.at(0), "type": "turn_context", "payload": {"model": "gpt-6.1-sol"}}]
        rows += [{"timestamp": self.at(m), "type": t, "payload": p} for m, t, p in items]
        target = folder / f"rollout-{when:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl"
        self.jsonl(target, rows)
        return target

    @staticmethod
    def counter(total, last):
        keys = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
        return "event_msg", {"type": "token_count", "info": {"total_token_usage": dict(zip(keys, total)),
                                                              "last_token_usage": dict(zip(keys, last))}}

    @staticmethod
    def record(thread, rid, inp, cached, out, reasoning=0):
        return "token_usage_record", {"thread_id": thread, "response_id": rid, "usage": {
            "input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0, "output_tokens": out,
            "reasoning_output_tokens": reasoning, "total_tokens": inp + out}}

    def row(self, job="job"):
        rows = [l for l in (self.node / "job-ledger.md").read_text().splitlines() if l.startswith("| ")]
        head = [c.strip() for c in rows[0].strip("|").split("|")]
        for line in rows[2:]:
            cells = dict(zip(head, (c.strip() for c in line.strip("|").split("|"))))
            if cells["Job"] == job:
                last = cells
        return last

    def mark(self):
        from core_loader import SourceFileLoader
        m = SourceFileLoader("brain_spend", str(CORE / "bin/brain")).load_module()
        n = m.Instance(self.r).node("app")
        return "brain-job: " + m.job_key(m.open_job(m.read_jobs(n), "job"))

    def test_claude_counts_each_response_once_with_sub_agents_compactions_and_tools(self):
        tool = [{"type": "thinking", "thinking": "x"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}]
        s = self.cl(self.wt, "11111111-aaaa", "Fix the bug.", [
            ("said", 1, "m1", (10, 1000, 100, 50), tool),       # three records, one response
            ("tool_result", 2, "listing"),
            ("said", 3, "m2", (5, 2000, 0, 20)),
            ("compact", 4, 180000),
            ("said", 30, "m3", (7, 500, 50, 30))])               # 26 minutes later: idle in between
        self.cl(self.wt, "agent-1", "Look it up.", [("said", 2, "s1", (3, 300, 30, 10))],
                    path=s.parent / "11111111-aaaa" / "subagents" / "agent-1.jsonl")
        self.cl(self.wt, "99999999-zzzz", "Elsewhere.", [("said", 1, "o1", (1, 1, 1, 1))], config=self.other)
        # An earlier job on the same branch used this worktree path: before this job started, so not counted.
        self.cl(self.wt, "00000000-old", "Earlier job.", [("said", -200, "old1", (9000, 9000, 9000, 9000))], start=-201)
        self.cl(self.wt, "00000001-span", "Earlier, then this one.", [("said", -200, "sp1", (8000, 0, 0, 800)),
                                                                     ("said", 10, "sp2", (1, 0, 0, 1))], start=-201)
        out = self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        row = self.row()
        self.assertEqual((row["Fresh in"], row["Cache read"], row["Cache write"], row["Output"]),
                         ("26", "3800", "180", "111"))
        self.assertEqual((row["Tool calls"], row["Compactions"], row["Sub-agents"], row["Sub share"]),
                         ("2", "1", "1", "8%"))
        self.assertEqual(row["Usage"], "transcripts: 3 sessions")
        self.assertEqual(row["Active h"], "0.1")  # minutes 0-4, 10 and 30: 5 active minutes plus 1 plus 1
        self.assertIn("claude:claude-opus-5-5 100%", row["Models"])
        self.assertIn("✓", self.b(self.r, "check"))
        self.assertIn("removed the worktree", out)

    def test_codex_counters_forks_records_and_sub_agents(self):
        self.cx(self.wt, "c0000001-main", [
            (1, *self.counter((1000, 600, 100, 40), (1000, 600, 100, 40))),
            (2, *self.counter((1000, 600, 100, 40), (1000, 600, 100, 40))),    # repeated: no new usage
            (3, *self.counter((3000, 2600, 300, 90), (2000, 2000, 200, 50))),
            (4, "compacted", {"message": "x"}),
            (5, "response_item", {"type": "function_call", "name": "shell"}),
            (5, "response_item", {"type": "custom_tool_call", "name": "apply_patch"})])
        self.cx(self.wt, "c0000002-fork", [   # a fork starts from its parent's totals
            (1, *self.counter((500000 + 800, 400000 + 500, 70, 10), (800, 500, 70, 10))),
            (2, *self.counter((500000 + 1800, 400000 + 1300, 120, 10), (1000, 800, 50, 0)))],
            forked_from_id="c0000000-parent")
        self.cx(self.wt, "c0000003-recs", [   # per-response records win over its counter; others' are skipped
            (1, *self.record("c0000003-recs", "r1", 400, 100, 30, 5)),
            (1, *self.record("c0000003-recs", "r1", 400, 100, 30, 5)),
            (2, *self.record("someone-else", "r9", 99999, 0, 99999)),
            (2, *self.counter((777777, 0, 7, 0), (777777, 0, 7, 0)))])
        self.cx(self.home, "c0000004-sub", [(1, *self.counter((100, 0, 10, 0), (100, 0, 10, 0)))],
                   source={"subagent": {"thread_spawn": {"parent_thread_id": "c0000001-main", "depth": 1}}})
        self.cx(self.home, "c0000005-other", [(1, *self.counter((5555, 0, 5, 0), (5555, 0, 5, 0)))])
        self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        row = self.row()
        # main: 3000-2600 fresh, 2600 cached, 300 out; fork: 1800-1300, 1300, 120; records: 300, 100, 30; sub: 100, 0, 10
        self.assertEqual((row["Fresh in"], row["Cache read"], row["Output"], row["Reasoning"]),
                         ("1300", "4000", "460", "105"))
        self.assertEqual((row["Compactions"], row["Tool calls"], row["Sub-agents"]), ("1", "2", "1"))
        self.assertEqual(row["Usage"], "transcripts: 4 sessions")
        self.assertIn("codex:gpt-6.1-sol 100%", row["Models"])

    def test_reviewers_are_found_by_their_brief_and_coordinators_are_not(self):
        brief = self.b(self.r, "review", "app", "--branch", "job", "--built-by", "claude")
        mark = self.mark()
        self.assertIn(mark, brief)
        self.assertIn(mark, self.b(self.r, "brief", "app", "--branch", "job", "--task", "Fix it"))
        self.cl(self.node, "22222222-review", brief, [("said", 1, "r1", (40, 400, 0, 4))])
        self.cl(self.node, "33333333-coord", "You coordinate this node.", [  # printed the brief: not its job
            ("tool_result", 1, brief), ("said", 2, "c1", (9000, 9000, 0, 900))])
        self.cx(self.node, "c0000010-review", [
            (0, "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": brief}]}),
            (1, *self.counter((60, 0, 6, 0), (60, 0, 6, 0)))])
        self.cx(self.node, "c0000011-sub", [(1, *self.counter((7, 0, 1, 0), (7, 0, 1, 0)))],
                   source={"subagent": {"thread_spawn": {"parent_thread_id": "c0000010-review", "depth": 1}}})
        self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        row = self.row()
        self.assertEqual((row["Fresh in"], row["Output"], row["Sub-agents"]), ("107", "11", "1"))
        self.assertEqual(row["Usage"], "transcripts: 3 sessions")

    def test_a_worker_elsewhere_is_found_by_the_session_its_hand_back_recorded(self):
        elsewhere = self.tmp / "t3-worktree"
        elsewhere.mkdir()
        self.cl(elsewhere, "44444444-moved", "Continue the job.", [("said", 1, "w1", (11, 0, 0, 1))])
        self.cx(elsewhere, "c0000020-resumed", [(1, *self.counter((22, 0, 2, 0), (22, 0, 2, 0)))])
        self.cl(elsewhere, "55555555-other", "Unrelated.", [("said", 1, "u1", (999, 0, 0, 9))])
        for var, sid in (("CLAUDE_CODE_SESSION_ID", "44444444-moved"), ("CODEX_THREAD_ID", "c0000020-resumed")):
            env = {**self.env, var: sid}
            subprocess.run([sys.executable, str(self.r / "core/bin/brain"), "handback", "--summary", "Synthetic result", "app", "--branch", "job",
                            "--state", "blocked", "--report", "r.md", "--lessons", "none: none"], cwd=self.r, env=env,
                           capture_output=True, text=True, check=True)
        hb = json.loads(next((self.node / "job-handbacks").glob("*.json")).read_text())
        self.assertEqual(hb["sessions"], ["44444444-moved", "c0000020-resumed"])
        self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        row = self.row()
        self.assertEqual((row["Fresh in"], row["Output"], row["Usage"]), ("33", "3", "transcripts: 2 sessions"))

    def test_links_count_once_loops_end_and_other_accounts_stay_unread(self):
        self.cl(self.home, "66666666-home", "Hello.", [("said", 1, "h1", (100, 0, 0, 10))])
        (self.claude / "projects" / "projects").symlink_to(self.claude / "projects")  # a stray link onto itself
        twin = self.tmp / ".claude-twin"
        twin.mkdir()
        (twin / "projects").symlink_to(self.claude / "projects")  # a second account sharing the first one's sessions
        self.cl(self.home, "77777777-them", "Theirs.", [("said", 1, "x1", (5000, 0, 0, 500))], config=self.other)
        cfg = json.loads((self.cfg / "subscriptions.json").read_text())
        cfg["subscriptions"].append({"id": "twin", "runtime": "claude", "config_dir": str(twin), "brains": ["alice"]})
        (self.cfg / "subscriptions.json").write_text(json.dumps(cfg))
        (self.other / "projects" / "linked").symlink_to(self.claude / "projects")
        out = json.loads(self.b(self.r, "spend", "--machine", "--json"))
        self.assertEqual((out["total"]["fresh"], out["total"]["output"], out["total"]["sessions"]), (100, 10, 1))

    def test_ledger_v3_keeps_columns_a_newer_core_added(self):
        ledger = self.node / "job-ledger.md"
        text = ledger.read_text().replace("| Usage |", "| Usage | Waiting h |").replace("| --- |\n", "| --- | --- |\n", 1)
        text = re.sub(r"(\| job \|.*)\|\n", lambda m: m.group(1) + "| 1.5 |\n", text)
        ledger.write_text(text)
        self.b(self.r, "review", "app", "--branch", "job", "--built-by", "claude")
        self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        self.assertEqual(self.row()["Waiting h"], "1.5")
        self.assertEqual(self.row()["Reviews"], "1")
        self.assertIn("<!-- brain:job-ledger v3 -->", ledger.read_text())

    def test_medians_flags_budgets_attention_and_the_live_view(self):
        ledger = self.node / "job-ledger.md"
        cols = ("| Job | Started | Ended | Hours | Outcome | Reviews | Fresh in | Cache read | Output | Active h | "
                "Usage |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")
        past = "".join(f"| job/fix-{i} | 2026-01-0{i}T10:00+00:00 | 2026-01-0{i}T12:00+00:00 | 2.0 | merged | 1 | "
                       f"{1000 * i} | {10000 * i} | {100 * i} | 1.{i} | transcripts: 1 session |\n" for i in (1, 2, 3))
        text = ledger.read_text()
        open_row = next(l for l in text.splitlines() if l.startswith("| job |"))
        cells = [c.strip() for c in open_row.strip("|").split("|")]
        head = [c.strip() for c in next(l for l in text.splitlines() if l.startswith("| Job |")).strip("|").split("|")]
        row = dict(zip(head, cells))
        ledger.write_text("<!-- brain:job-ledger v2 -->\n\n" + cols + past +
                          f"| job | {row['Started']} |  |  |  | 0 |  |  |  |  |  |\n")
        contract = self.tmp / "contract.md"
        contract.write_text("## Outcome\nFix it.\n\n## Authorized\nAll of it.\n\n## Discretion\nAny.\n\n## Stop when\nDone.\n\n"
                            "## Needs consent\nNothing.\n\n## Done means\nGreen.\n\n## Budget\n30k tokens, 3 h\n")
        self.b(self.r, "jobs", "app", "--branch", "job", "--contract-file", str(contract))
        self.assertIn("Its budget is 30k tokens · 3 h. `brain jobs app --branch job` shows what this job has spent",
                      self.b(self.r, "brief", "app", "--branch", "job", "--task", "Fix it"))
        self.cl(self.wt, "88888888-heavy", "Fix it.", [
            ("said", 1, "h1", (20000, 40000, 0, 2000)), ("compact", 2, 150000), ("compact", 3, 150000),
            ("compact", 4, 150000)])
        self.cl(self.wt, "agent-2", "Search.", [("said", 1, "h2", (30000, 50000, 0, 3000))],
                    path=self.folder(self.wt) / "88888888-heavy" / "subagents" / "agent-2.jsonl")
        out = self.b(self.r, "jobs", "app")
        self.assertIn("median 22.2k tokens and 1.2 active h (3 measured jobs)", out)
        self.assertIn("(over twice the median in tokens)", out)
        self.assertIn("3 compactions: past the context window", out)
        self.assertIn("sub-agents spent 57% of its tokens", out)
        self.assertIn("budget 30k tokens · 3 h: over (145k of 30k tokens)", out)
        self.assertIn("22.2k tokens processed (spread 11.1k–33.3k), of them 2.2k fresh input and output", out)
        state = json.loads(self.b(self.r, "ui", "--json"))
        app = next(n for n in state["nodes"] if n["id"] == "app")
        self.assertEqual(app["jobs"]["median_tokens"], 22200)
        live = app["jobs"]["open"][0]
        self.assertEqual((live["usage"]["processed"], live["usage"]["compactions"], live["budget"]["tokens"]),
                         (145000, 3, 30000))
        self.assertEqual(state["spend"]["complete"], True)
        self.assertEqual(state["spend"]["since_week"]["processed"], 145000)
        self.assertIn("claude", state["spend"]["runtimes"])
        flags = [a["text"] for a in state["attention"] if a["kind"] == "job"]
        self.assertEqual(len(flags), 1)
        for part in ("job job:", "over twice the median of 22.2k", "over its 30k-token budget", "3 compactions",
                     "sub-agents spent 57%"):
            self.assertIn(part, flags[0])
        hand = self.b(self.r, "handback", "--summary", "Synthetic result", "app", "--branch", "job", "--state", "blocked", "--report", "r.md",
                      "--lessons", "none: none")
        self.assertIn("usage: 145k tokens processed", hand)
        self.assertIn("budget 30k tokens · 3 h: over (145k of 30k tokens", hand)

    def test_resumed_sessions_copied_history_and_what_the_cache_keeps(self):
        # Resumed today, begun a week ago: its transcript stays in that day's folder.
        self.cx(self.wt, "c0000050-resumed", [(1, *self.counter((70, 0, 7, 0), (70, 0, 7, 0)))], began=-7)
        # An old-style fork copies its parent's records, older than its own start: neither tokens nor time count.
        copied = [(m, "response_item", {"type": "function_call", "name": "shell"}) for m in range(-100, 0)]
        copied += [(-50, *self.counter((900000, 0, 9000, 0), (900000, 0, 9000, 0)))]
        rows = [{"timestamp": self.at(0), "type": "session_meta", "payload": {
            "id": "c0000051-fork", "cwd": str(self.wt), "forked_from_id": "c0000000-parent"}}]
        rows += [{"timestamp": self.at(m), "type": t, "payload": p} for m, t, p in copied]
        rows += [{"timestamp": self.at(1), "type": "event_msg", "payload": self.counter(
            (900000 + 30, 0, 9000 + 3, 0), (30, 0, 3, 0))[1]}]
        when = datetime.datetime.fromtimestamp(self.t0, datetime.timezone.utc).astimezone()
        self.jsonl(self.codex / "sessions" / when.strftime("%Y/%m/%d") / "rollout-x-c0000051-fork.jsonl", rows)
        (self.node / "never-name.txt").write_text("worktrees\n")
        self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        row = self.row()
        self.assertEqual((row["Fresh in"], row["Output"], row["Tool calls"]), ("100", "10", "0"))
        self.assertEqual(row["Active h"], "0.0")
        self.assertEqual(row["Usage"], "transcripts: 2 sessions")
        for f in (self.r / ".brain/spend/files").glob("*.json"):  # counts only: no folder a word could hide in
            text = f.read_text()
            self.assertNotIn(str(self.tmp), text)
            self.assertNotIn("worktrees", text)
            self.assertNotIn('"cwd"', text)

    def test_live_figures_use_what_the_host_reported(self):
        self.cl(self.wt, "99999998-wt", "Fix.", [("said", 1, "y1", (55, 55, 0, 5))])
        self.assertIn("115 tokens", self.b(self.r, "jobs", "app"))
        usage = self.tmp / "usage.json"
        usage.write_text(json.dumps({"fresh": 4000, "cache_read": 6000, "output": 500, "compactions": 4,
                                     "models": {"claude:opus": 10500}, "host": "T3"}))
        self.b(self.r, "jobs", "app", "--branch", "job", "--usage-file", str(usage))
        out = self.b(self.r, "jobs", "app")
        self.assertIn("usage: 10.5k tokens processed (fresh in 4k", out)
        self.assertIn("4 compactions: past the context window", out)
        state = json.loads(self.b(self.r, "ui", "--json"))
        live = next(n for n in state["nodes"] if n["id"] == "app")["jobs"]["open"][0]["usage"]
        self.assertEqual((live["processed"], live["models"]), (10500, {"claude:opus": 10500}))

    def test_model_names_are_ids_and_never_hold_a_never_name_word(self):
        (self.node / "never-name.txt").write_text("Zebracorp\n")
        s = self.cl(self.wt, "12121212-model", "Fix.", [("said", 1, "z1", (10, 0, 0, 1))])
        text = s.read_text().replace("claude-opus-5-5", "zebracorp-tuned")
        s.write_text(text)
        self.cl(self.wt, "13131313-odd", "Fix.", [("said", 2, "z2", (20, 0, 0, 2))])
        odd = self.folder(self.wt) / "13131313-odd.jsonl"
        odd.write_text(odd.read_text().replace("claude-opus-5-5", "a model with words"))
        state = json.loads(self.b(self.r, "ui", "--json"))
        live = next(n for n in state["nodes"] if n["id"] == "app")["jobs"]["open"][0]["usage"]
        self.assertEqual(sorted(live["models"]), ["claude:other", "claude:unknown"])
        for out in (json.dumps(state), self.b(self.r, "spend", "--machine", "--json"), self.b(self.r, "jobs", "app")):
            self.assertNotIn("zebracorp", out.lower())
        self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        self.assertNotIn("zebracorp", self.row()["Models"].lower())
        for f in (self.r / ".brain/spend").rglob("*.json"):
            self.assertNotIn("zebracorp", f.read_text().lower())
        usage = self.tmp / "usage.json"
        usage.write_text(json.dumps({"fresh": 1, "models": {"zebracorp-x": 1}}))
        self.assertIn("never-name", self.b(self.r, "jobs", "app", "--branch", "job", "--usage-file", str(usage), ok=False))

    def test_a_fork_copied_earlier_in_its_first_minute_counts_once(self):
        base = datetime.datetime.fromtimestamp(self.t0 + 600, datetime.timezone.utc).replace(second=0)
        stamp = lambda sec: (base + datetime.timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        rows = [{"timestamp": stamp(40), "type": "session_meta", "payload": {
            "id": "c0000060-fork", "cwd": str(self.wt), "forked_from_id": "c0000000-parent"}},
                {"timestamp": stamp(10), "type": "response_item", "payload": {"type": "function_call", "name": "x"}},
                {"timestamp": stamp(10), "type": "compacted", "payload": {"message": "x"}},
                {"timestamp": stamp(10), "type": "event_msg", "payload": self.counter((1000, 0, 100, 0), (1000, 0, 100, 0))[1]},
                {"timestamp": stamp(50), "type": "response_item", "payload": {"type": "function_call", "name": "y"}},
                {"timestamp": stamp(50), "type": "compacted", "payload": {"message": "y"}},
                {"timestamp": stamp(55), "type": "event_msg", "payload": self.counter((1030, 0, 103, 0), (30, 0, 3, 0))[1]}]
        when = datetime.datetime.fromtimestamp(self.t0, datetime.timezone.utc).astimezone()
        self.jsonl(self.codex / "sessions" / when.strftime("%Y/%m/%d") / "rollout-y-c0000060-fork.jsonl", rows)
        self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        row = self.row()
        self.assertEqual((row["Fresh in"], row["Output"], row["Tool calls"], row["Compactions"]), ("30", "3", "1", "1"))

    def test_a_cut_claude_folder_name_only_counts_sessions_that_ran_there(self):
        from core_loader import SourceFileLoader
        deep = self.tmp / ("x" * 120) / ("y" * 120)
        mine, other = deep / "job-wt", deep / "job-other"
        for d in (mine, other):
            d.mkdir(parents=True)
        name = re.sub(r"[^A-Za-z0-9]", "-", str(mine))
        self.assertGreater(len(name), 200)
        self.cl(mine, "14141414-mine", "Fix.", [("said", 1, "m1", (10, 0, 0, 1))],
                path=self.claude / "projects" / (name[:200] + "-1a2b") / "14141414-mine.jsonl")
        self.cl(other, "15151515-other", "Other.", [("said", 1, "o1", (9000, 0, 0, 900))],
                path=self.claude / "projects" / (name[:200] + "-3c4d") / "15151515-other.jsonl")
        m = SourceFileLoader("brain_spend_cut", str(CORE / "bin/brain")).load_module()
        with mock.patch.dict(os.environ, self.env, clear=True):
            inst = m.Instance(self.r)
            n = inst.node("app")
            row = m.open_job(m.read_jobs(n), "job")
            ids = sorted(a["id"] for a in m.job_sessions(inst, n, row, worktree=str(mine)))
        self.assertEqual(ids, ["14141414-mine"])

    def test_transcript_ids_are_never_kept_and_a_shared_folder_name_isnt_trusted(self):
        (self.node / "never-name.txt").write_text("Zebracorp\n")
        tool = [{"type": "tool_use", "id": "toolu_Zebracorp1", "name": "Bash", "input": {}}]
        self.cl(self.wt, "zebracorp-0001-session", "Fix.", [("said", 1, "msg_Zebracorp", (10, 0, 0, 1), tool)])
        self.cx(self.wt, "c0000070-zebracorp", [(1, *self.record("c0000070-zebracorp", "resp_Zebracorp", 20, 0, 2))])
        twin = self.tmp / "app_work/worktrees/job"  # Claude names its folder exactly like the job's worktree
        twin.mkdir(parents=True)
        self.cl(twin, "16161616-twin", "Elsewhere.", [("said", 1, "t1", (9000, 0, 0, 900))],
                path=self.folder(self.wt) / "16161616-twin.jsonl")
        self.b(self.r, "spend", "app")
        self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        row = self.row()
        self.assertEqual((row["Fresh in"], row["Output"], row["Usage"]), ("30", "3", "transcripts: 2 sessions"))
        for f in (self.r / ".brain/spend").rglob("*.json"):
            self.assertNotIn("zebracorp", f.read_text().lower(), f)

    def test_only_what_happened_while_the_job_was_open_counts_to_the_second(self):
        ledger = self.node / "job-ledger.md"
        begun = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).replace(second=40, microsecond=0)
        ledger.write_text(re.sub(r"(\| job \| )[^|]+", lambda m: m.group(1) + begun.isoformat() + " ", ledger.read_text()))
        at = lambda sec: (begun + datetime.timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        rec = lambda sec, mid, usage: {"type": "assistant", "cwd": str(self.wt), "sessionId": "17171717-span",
                                       "message": {"id": mid, "model": "claude-opus-5-5", "role": "assistant",
                                                   "content": [{"type": "text", "text": "ok"}],
                                                   "usage": dict(zip(("input_tokens", "cache_read_input_tokens",
                                                                      "cache_creation_input_tokens", "output_tokens"), usage))},
                                       "timestamp": at(sec)}
        self.jsonl(self.folder(self.wt) / "17171717-span.jsonl", [
            {"type": "user", "cwd": str(self.wt), "sessionId": "17171717-span", "message": {"role": "user", "content": "Go."},
             "timestamp": at(-35)},
            rec(-30, "before", (9000, 0, 0, 900)),   # the same minute, 30 seconds before the job began
            rec(10, "during", (30, 0, 0, 3))])
        self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        self.assertEqual((self.row()["Fresh in"], self.row()["Output"]), ("30", "3"))

    def test_a_never_name_word_added_later_and_sessions_without_a_folder(self):
        s = self.cl(self.home, "18181818-home", "Look.", [("said", 1, "q1", (10, 0, 0, 1))])
        s.write_text(s.read_text().replace("claude-opus-5-5", "zeta-model"))
        self.b(self.r, "spend", "app")
        (self.node / "never-name.txt").write_text("Zeta\n")  # added after the look-back and the cache were written
        state = json.loads(self.b(self.r, "ui", "--json"))
        self.assertNotIn("zeta", json.dumps(state).lower())
        self.assertNotIn("zeta", self.b(self.r, "spend", "--machine", "--json").lower())
        for f in (self.r / ".brain/spend/files").glob("*.json"):  # caches written before the word was added too
            self.assertNotIn("zeta", f.read_text().lower())
        self.assertNotIn(b'zeta', (self.r / '.brain/usage.sqlite').read_bytes().lower())
        # A transcript with no folder of its own is in no place, wherever the command runs.
        rows = [{"type": "user", "sessionId": "19191919-nocwd", "message": {"role": "user", "content": "Hi."},
                 "timestamp": self.at(0)},
                {"type": "assistant", "sessionId": "19191919-nocwd", "message": {
                    "id": "n1", "model": "claude-opus-5-5", "role": "assistant", "content": [{"type": "text", "text": "x"}],
                    "usage": {"input_tokens": 200, "output_tokens": 10}}, "timestamp": self.at(1)}]
        self.jsonl(self.folder(self.wt) / "19191919-nocwd.jsonl", rows)
        out = self.run_(["jobs", "app"], cwd=self.wt, brain=self.r / "core/bin/brain")
        self.assertNotIn("210 tokens", out)
        self.assertIn("usage: no transcripts found", out)

    def test_a_long_running_view_screens_names_added_while_it_runs(self):
        from core_loader import SourceFileLoader
        s = self.cl(self.home, "21212121-live", "Look.", [("said", 1, "lv1", (10, 0, 0, 1))])
        s.write_text(s.read_text().replace("claude-opus-5-5", "theta-model"))
        m = SourceFileLoader("brain_live_terms", str(CORE / "bin/brain")).load_module()
        with mock.patch.dict(os.environ, self.env, clear=True):
            inst = m.Instance(self.r)
            m.spend_machine(inst, budget=None)  # the view counts once...
            (self.node / "never-name.txt").write_text("Theta\n")  # ...the word is added while it runs...
            m._SPEND_TERMS.clear()  # ...and a minute passes
            m._MACHINE.clear()
            value = m.spend_machine(m.Instance(self.r), budget=None)
        self.assertNotIn("theta", json.dumps(value).lower())
        for f in (self.r / ".brain/spend/files").glob("*.json"):
            self.assertNotIn("theta", f.read_text().lower())
        self.assertNotIn(b'theta', (self.r / '.brain/usage.sqlite').read_bytes().lower())

    def test_a_response_that_grew_after_the_job_began_counts_its_growth(self):
        ledger = self.node / "job-ledger.md"
        begun = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).replace(second=40, microsecond=0)
        ledger.write_text(re.sub(r"(\| job \| )[^|]+", lambda m: m.group(1) + begun.isoformat() + " ", ledger.read_text()))
        at = lambda sec: (begun + datetime.timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        def said(sec, out, blocks):
            return {"type": "assistant", "cwd": str(self.wt), "sessionId": "20202020-grew", "timestamp": at(sec),
                    "message": {"id": "grow", "model": "claude-opus-5-5", "role": "assistant", "content": blocks,
                                "usage": {"input_tokens": 0, "output_tokens": out}}}
        self.jsonl(self.folder(self.wt) / "20202020-grew.jsonl", [
            {"type": "user", "cwd": str(self.wt), "sessionId": "20202020-grew", "message": {"role": "user", "content": "Go."},
             "timestamp": at(-100)},
            said(-90, 50, [{"type": "text", "text": "x"}]),  # the minute before the job began
            said(200, 150, [{"type": "tool_use", "id": "t9", "name": "Bash", "input": {}}])])
        self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        self.assertEqual((self.row()["Output"], self.row()["Tool calls"]), ("100", "1"))

    def test_host_numbers_win_over_transcripts(self):
        self.cl(self.wt, "99999999-wt", "Fix.", [("said", 1, "z1", (5, 5, 5, 5))])
        usage = self.tmp / "usage.json"
        usage.write_text(json.dumps({"fresh": 1, "cache_read": 2, "output": 3, "host": "T3 run report"}))
        self.b(self.r, "jobs", "app", "--branch", "job", "--usage-file", str(usage))
        self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        row = self.row()
        self.assertEqual((row["Fresh in"], row["Cache read"], row["Output"], row["Usage"]), ("1", "2", "3", "host: T3 run report"))
        usage.write_text(json.dumps({"fresh": -1}))
        self.b(self.r, "jobs", "app", "--branch", "job", "--usage-file", str(usage), ok=False)
        usage.write_text(json.dumps({"fresh": 1, "prompt": "text"}))
        self.b(self.r, "jobs", "app", "--branch", "job", "--usage-file", str(usage), ok=False)

    def test_look_back_over_a_homes_past_sessions_keeps_aggregates_only(self):
        self.cl(self.home, "aaaaaaa1-home", "SECRET-PROMPT-TEXT", [("said", 1, "l1", (100, 1000, 0, 10)),
                                                                        ("compact", 2, 160000)])
        self.cx(self.wt, "c0000030-wt", [(1, *self.counter((300, 100, 30, 0), (300, 100, 30, 0)))])
        clone = self.tmp / "clone"
        self.git("clone", "-q", str(self.home), str(clone), cwd=self.tmp)
        self.git("remote", "set-url", "origin", "git@github.com:acme/app.git", cwd=clone)
        self.cx(clone, "c0000031-clone", [(1, *self.counter((50, 0, 5, 0), (50, 0, 5, 0)))])
        elsewhere = self.repo(self.tmp / "elsewhere")
        self.cl(elsewhere, "aaaaaaa2-else", "Other project.", [("said", 1, "e1", (7777, 0, 0, 7))])
        out = self.b(self.r, "spend", "app")
        self.assertIn("app: 3 sessions (0 sub-agents)", out)
        self.assertIn("fresh in 350", out)
        self.assertIn("compactions: 1", out)
        kept = self.r / ".brain/spend/nodes/app.json"
        record = json.loads(kept.read_text())
        self.assertEqual(record["total"]["fresh"], 350)
        self.assertEqual(len(record["sessions"]), 3)
        self.assertNotIn("SECRET-PROMPT-TEXT", kept.read_text())
        self.assertNotIn(str(self.home), kept.read_text())
        for f in (self.r / ".brain/spend/files").glob("*.json"):
            self.assertNotIn("SECRET-PROMPT-TEXT", f.read_text())
        self.assertNotIn("spend", self.git("ls-files", ".brain", cwd=self.r))
        future = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
        self.assertIn("app since " + future + ": 0 sessions", self.b(self.r, "spend", "app", "--since", future))
        state = json.loads(self.b(self.r, "ui", "--json"))
        self.assertEqual(next(n for n in state["nodes"] if n["id"] == "app")["spend"]["total"]["fresh"], 0)

    def test_homeless_nodes_and_recorded_scratch_coordinators(self):
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        self.cl(scratch, "aaaa1001-coord", "private", [("said", 1, "c1", (100, 0, 0, 10))])
        self.cx(scratch, "cccc1002-coord", [(1, *self.counter((200, 0, 20, 0), (200, 0, 20, 0)))])
        self.cl(scratch, "aaaa1003-other", "private", [("said", 1, "c2", (900, 0, 0, 90))])
        self.cl(self.r, "aaaa1004-root", "private", [("said", 1, "r1", (30, 0, 0, 3))])
        self.cl(self.node, "aaaa1005-node", "private", [("said", 1, "n1", (40, 0, 0, 4))])
        ids = "aaaa1001-coord,cccc1002-coord,aaaa1001-coord"
        self.b(self.r, "set", "root", "coordinator_sessions=" + ids)
        local = json.loads((self.r / ".brain/local.json").read_text())
        self.assertEqual(local["nodes"]["root"]["coordinator_sessions"], ["cccc1002-coord", "aaaa1001-coord"])
        self.assertNotIn("coordinator_sessions", (self.r / "node.md").read_text())
        root = json.loads(self.b(self.r, "spend", "root", "--json"))
        self.assertEqual((root["total"]["fresh"], root["total"]["sessions"]), (330, 3))
        self.assertIn("root: 3 sessions", self.b(self.r, "spend", "root"))
        self.b(self.r, "set", "app", "home=none", "coordinator_sessions=" + ids)
        node = json.loads(self.b(self.r, "spend", "app", "--json"))
        self.assertEqual((node["total"]["fresh"], node["total"]["sessions"]), (340, 3))
        self.b(self.r, "set", "app", "coordinator_sessions=../bad", ok=False)
        self.b(self.r, "set", "root", "coordinator_sessions=")
        self.assertEqual(json.loads(self.b(self.r, "spend", "root", "--json"))["total"]["fresh"], 30)

    def test_external_home_adds_recorded_coordinators_without_double_counting(self):
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        self.cx(scratch, "cccc2001-coord", [(1, *self.counter((200, 0, 20, 0), (200, 0, 20, 0)))])
        self.cl(self.home, "aaaa2002-home", "private", [("said", 1, "h1", (100, 0, 0, 10))])
        self.cl(scratch, "aaaa2003-excluded", "private", [("said", 1, "x1", (900, 0, 0, 90))], config=self.other)
        self.b(self.r, "set", "app", "coordinator_sessions=cccc2001-coord,aaaa2002-home,aaaa2003-excluded")
        result = json.loads(self.b(self.r, "spend", "app", "--json"))
        self.assertEqual((result["total"]["fresh"], result["total"]["sessions"]), (300, 2))
        future = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
        self.assertEqual(json.loads(self.b(self.r, "spend", "app", "--since", future, "--json"))["total"]["sessions"], 0)

    def test_recorded_claude_coordinator_includes_scratch_subagents_only(self):
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        sid = "aaaa3001-coord"
        parent = self.cl(scratch, sid, "private", [("said", 1, "c1", (100, 0, 0, 10))])
        self.cl(scratch, "aaaa3002-child", "private", [("said", 2, "c2", (50, 0, 0, 5))],
                path=parent.parent / sid / "subagents" / "agent-1.jsonl")
        other = self.cl(self.home, "aaaa3003-home", "private", [("said", 1, "h1", (20, 0, 0, 2))])
        self.cl(scratch, "aaaa3004-foreign", "private", [("said", 2, "x1", (900, 0, 0, 90))],
                path=other.parent / "aaaa3003-home" / "subagents" / "agent-2.jsonl")
        self.b(self.r, "set", "app", "coordinator_sessions=" + sid)
        total = json.loads(self.b(self.r, "spend", "app", "--json"))["total"]
        self.assertEqual((total["fresh"], total["sessions"], total["subagents"]), (170, 3, 1))

    def test_recorded_codex_coordinator_includes_transitive_scratch_subagents(self):
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        items = [(1, *self.counter((100, 0, 10, 0), (100, 0, 10, 0)))]
        # Write descendants first so discovery must grow the lineage in more than one pass.
        for sid, parent, cwd in [
                ("cccc4003-grand", "cccc4002-child", scratch),
                ("cccc4002-child", "cccc4001-coord", self.home),
                ("cccc4001-coord", None, scratch),
                ("cccc4004-other", None, self.home),
                ("cccc4005-foreign", "cccc4004-other", scratch)]:
            source = {"source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}} if parent else {}
            self.cx(cwd, sid, items, **source)
        self.b(self.r, "set", "app", "coordinator_sessions=cccc4001-coord")
        total = json.loads(self.b(self.r, "spend", "app", "--json"))["total"]
        self.assertEqual((total["fresh"], total["sessions"], total["subagents"]), (400, 4, 2))

    def test_coordinator_selection_moves_repeated_session_last(self):
        self.b(self.r, "set", "root", "coordinator_sessions=aaaa5001-first,bbbb5002-next,aaaa5001-first")
        local = json.loads((self.r / ".brain/local.json").read_text())
        self.assertEqual(local["nodes"]["root"]["coordinator_sessions"], ["bbbb5002-next", "aaaa5001-first"])
        self.b(self.r, "set", "root", "coordinator_sessions=bbbb5002-next")
        local = json.loads((self.r / ".brain/local.json").read_text())
        self.assertEqual(local["nodes"]["root"]["coordinator_sessions"], ["aaaa5001-first", "bbbb5002-next"])

    def test_new_coordinator_appends_history_and_explicit_replace_is_available(self):
        self.b(self.r, "set", "root", "coordinator_sessions=aaaa6001-first,bbbb6002-next")
        self.b(self.r, "set", "root", "coordinator_sessions=cccc6003-new")
        local = json.loads((self.r / ".brain/local.json").read_text())
        self.assertEqual(local["nodes"]["root"]["coordinator_sessions"],
                         ["aaaa6001-first", "bbbb6002-next", "cccc6003-new"])
        self.b(self.r, "set", "root", "coordinator_sessions:=bbbb6002-next")
        local = json.loads((self.r / ".brain/local.json").read_text())
        self.assertEqual(local["nodes"]["root"]["coordinator_sessions"], ["bbbb6002-next"])
        self.assertNotIn("coordinator_sessions", (self.r / "node.md").read_text())
        self.b(self.r, "set", "root", "coordinator_sessions:=../bad", ok=False)
        self.b(self.r, "set", "root", "coordinator_sessions=")
        local = json.loads((self.r / ".brain/local.json").read_text())
        self.assertEqual(local["nodes"]["root"]["coordinator_sessions"], [])

    @unittest.skipUnless(shutil.which("zstd"), "zstd not installed")
    def test_compressed_codex_sessions_are_read_once(self):
        f = self.cx(self.wt, "c0000040-zst", [(1, *self.counter((400, 0, 40, 0), (400, 0, 40, 0)))])
        subprocess.run(["zstd", "-q", "--rm", str(f)], check=True)
        self.cx(self.wt, "c0000041-live", [(1, *self.counter((10, 0, 1, 0), (10, 0, 1, 0)))])
        self.b(self.r, "wt", "rm", "app", "job", "--force", "--cause", "measured")
        self.assertEqual((self.row()["Fresh in"], self.row()["Usage"]), ("410", "transcripts: 2 sessions"))

    def test_sqlite_usage_cache_incremental_invalidation_and_recount(self):
        """Whole-line offsets survive processes; every source generation wins."""
        m = core_loader.SourceFileLoader('speed_cache_fixture', str(CORE / 'bin/brain')).load_module()
        path = self.cl(self.wt, '11111111-cache', 'A fabricated task.', [
            ('said', 1, 'm1', (10, 20, 30, 40))])
        inst = m.Instance(self.r)
        allowed = {self.claude.resolve()}

        def read():
            m._SPEND_MEM.clear()  # simulate a new command/process
            return m.spend_file(inst, path, 'claude', allowed, set())

        def full():
            agg = m.spend_new('claude', path)
            with path.open('rb') as stream:
                m.spend_read(agg, stream)
            agg.update(path=str(path), id=path.stem)
            return m.spend_sum([agg])

        with mock.patch.dict(os.environ, self.env):
            first = read()
            self.assertEqual(m.spend_sum([first]), full())
            database = self.r / '.brain/usage.sqlite'
            self.assertEqual(database.read_bytes()[:16], b'SQLite format 3\x00')
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)
            with mock.patch.object(m, 'spend_read', wraps=m.spend_read) as parse:
                self.assertEqual(m.spend_sum([read()]), full())
                self.assertEqual(parse.call_count, 1)  # only the independent full recount
            original = path.read_bytes()
            self.cl(self.wt, '11111111-cache', 'A fabricated task.', [
                ('said', 2, 'm2', (5, 6, 7, 8))])
            offsets = []
            real_read = m.spend_read
            def capture(agg, stream, budget=None):
                offsets.append(agg['state']['offset'])
                return real_read(agg, stream, budget)
            with mock.patch.object(m, 'spend_read', side_effect=capture):
                appended = read()
            self.assertEqual(offsets, [len(original)])
            self.assertEqual(m.spend_sum([appended]), full())
            path.write_bytes(original)  # truncate
            self.assertEqual(m.spend_sum([read()]), full())
            replacement = path.with_suffix('.replacement')
            replacement.write_bytes(path.read_bytes().replace(b'"input_tokens":10', b'"input_tokens":90'))
            replacement.replace(path)  # rotate, even at the same length
            self.assertEqual(m.spend_sum([read()]), full())
            path.unlink()
            self.assertIsNone(read())
            # No session id, source path or transcript text enters SQLite.
            data = database.read_bytes()
            for private in (b'11111111-cache', str(path).encode(), b'A fabricated task.'):
                self.assertNotIn(private, data)

    def test_sqlite_cache_damage_and_refused_storage_fail_soft(self):
        m = core_loader.SourceFileLoader('speed_cache_damage', str(CORE / 'bin/brain')).load_module()
        path = self.cl(self.wt, '11111111-cache', 'A fabricated task.', [('said', 1, 'm1', (1, 2, 3, 4))])
        inst = m.Instance(self.r)
        cache = self.r / '.brain/usage.sqlite'
        cache.write_bytes(b'not a database')
        with mock.patch.dict(os.environ, self.env):
            used = m.spend_file(inst, path, 'claude', {self.claude}, set())
            self.assertEqual(m.spend_sum([used])['processed'], 10)
            m._SPEND_MEM.clear()
            cache.unlink()
            outside = self.tmp / 'outside-cache'; outside.write_text('sentinel')
            cache.symlink_to(outside)
            used = m.spend_file(inst, path, 'claude', {self.claude}, set())
            self.assertEqual(m.spend_sum([used])['processed'], 10)
            self.assertEqual(outside.read_text(), 'sentinel')

    def test_bounded_machine_view_does_not_bootstrap_all_session_headers(self):
        m = core_loader.SourceFileLoader('bounded_machine_fixture', str(CORE / 'bin/brain')).load_module()
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(m, 'codex_index', side_effect=AssertionError('unbounded discovery')), \
                mock.patch.object(m, 'spend_all', return_value=([], 0, False)) as scan:
            value = m.spend_machine(m.Instance(self.r), budget=.01)
        self.assertFalse(value['complete'])
        self.assertIsNone(value['since_week'])
        scan.assert_called_once()
        self.assertEqual(scan.call_args.kwargs['budget'], .01)

    def test_full_machine_hints_match_thread_records_recount_append_and_rebuild_old_cache(self):
        scratch = self.tmp / 'machine-scratch'
        scratch.mkdir()
        sid = '11111111-main'
        path = self.cx(scratch, sid, [(1, *self.record(sid, 'r1', 100, 40, 7, 3)),
                                     (2, *self.record('22222222-foreign', 'r9', 99999, 0, 99999))])
        m = core_loader.SourceFileLoader('machine_hint_records', str(CORE / 'bin/brain')).load_module()
        with mock.patch.dict(os.environ, self.env):
            inst = m.Instance(self.r)
            def recount():
                agg = m.spend_new('codex', path)
                with path.open('rb') as stream:
                    self.assertTrue(m.spend_read(agg, stream))
                return m.spend_sum([agg])
            def machine():
                m._MACHINE.clear()
                value = m.spend_machine(inst, budget=None)
                self.assertTrue(value['complete'])
                expected = recount()
                for key in ('fresh', 'cache_read', 'output', 'reasoning', 'processed', 'sessions'):
                    self.assertEqual(value['since_week'][key], expected[key], key)
                return value['since_week']
            self.assertEqual(machine()['processed'], 107)
            m._SPEND_MEM.clear()
            # A newly opened cache hit neither reparses source bytes nor recovers raw headers.
            m._MACHINE.clear()
            with mock.patch.object(m, 'spend_read', side_effect=AssertionError('unchanged recount')), \
                    mock.patch.object(m, 'codex_meta', side_effect=AssertionError('unchanged header')), \
                    mock.patch.object(m, 'codex_discovery_source', side_effect=AssertionError('unchanged discovery')):
                warm = m.spend_machine(inst, budget=None)
            self.assertEqual(warm['since_week']['processed'], 107)
            self.jsonl(path, [{'timestamp': self.at(3), 'type': 'token_usage_record',
                               'payload': self.record(sid, 'r2', 50, 5, 2, 1)[1]}])
            self.assertEqual(machine()['processed'], 159)
            m._SPEND_MEM.clear()
            self.assertEqual(machine()['processed'], 159)
            stat = path.stat()
            key = m.hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:32]
            bad = {**m.spend_new('codex', path), 'v': 6, 'key': key, 'ino': [stat.st_dev, stat.st_ino],
                   'stamp': [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_mode, stat.st_nlink],
                   'complete': True, 'published': True}
            bad['state']['offset'] = stat.st_size  # old defective aggregates consumed valid records as zero usage
            kept = {k: v for k, v in bad.items() if k not in m.SPEND_UNKEPT}
            cache = self.r / '.brain/spend/files' / (key + '.json')
            cache.write_text(json.dumps(kept))
            with m._owners['spend_file']._m_usage_cache.database(inst) as database:
                database.put('file:' + key, kept)
            m._SPEND_MEM.clear()
            self.assertEqual(machine()['processed'], 159)
            self.assertEqual(json.loads(cache.read_text())['v'], m.SPEND_VERSION)
            archive = self.codex / 'archived_sessions' / path.name
            archive.parent.mkdir(parents=True)
            archive.write_bytes(path.read_bytes())
            original_index = m.codex_index
            def append_after_discovery(*args, **kwargs):
                entries = original_index(*args, **kwargs)
                self.jsonl(path, [{'timestamp': self.at(4), 'type': 'token_usage_record',
                                   'payload': self.record(sid, 'r3', 10, 0, 1)[1]}])
                return entries
            m._SPEND_MEM.clear()
            m._MACHINE.clear()
            with mock.patch.object(m, 'codex_index', side_effect=append_after_discovery):
                changed = m.spend_machine(inst, budget=None)
            # The changed original uses a raw identity; its stable archive uses an opaque hint.
            self.assertEqual(changed['files'], 2)
            self.assertEqual(changed['since_week']['sessions'], 1)
            self.assertEqual(changed['since_week']['processed'], recount()['processed'])
            self.assertEqual(changed['since_week']['processed'], 170)
        state = json.loads(self.b(self.r, 'ui', '--json', '--wait'))
        self.assertEqual(state['spend']['since_week']['processed'], 170)
        self.assertNotIn(sid, cache.read_text())
        self.assertNotIn(sid.encode(), (self.r / '.brain/usage.sqlite').read_bytes())

    def test_rotation_metadata_uses_checked_descriptor_inside_command_snapshot(self):
        scratch = self.tmp / 'rotation-scratch'
        scratch.mkdir()
        first, second, third = '11111111-first', '22222222-second', '33333333-third'
        path = self.cx(scratch, first, [(1, *self.record(first, 'a1', 10, 0, 1))])
        replacement = self.cx(scratch, second, [(1, *self.record(second, 'b1', 90, 30, 7)),
                                               (2, *self.record(first, 'foreign', 50000, 0, 50000))])
        next_bytes = replacement.read_bytes()
        replacement.unlink()
        replacement = self.cx(scratch, third, [(1, *self.record(third, 'c1', 50, 0, 3))])
        last_bytes = replacement.read_bytes()
        replacement.unlink()
        staged = self.tmp / 'next-transcript'
        staged.write_bytes(next_bytes)
        m = core_loader.SourceFileLoader('rotation_metadata_fixture', str(CORE / 'bin/brain')).load_module()
        snapshot = m._owners['spend_file']._m_command_reads.snapshot
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(self.r)}):
            inst = m.Instance(self.r)
            original_index = m.codex_index
            def rotate_after_discovery(*args, **kwargs):
                entries = original_index(*args, **kwargs)
                staged.replace(path)
                return entries
            with snapshot():
                self.assertEqual(m.codex_meta(path, {self.codex}, set())[0], first)
                with mock.patch.object(m, 'codex_index', side_effect=rotate_after_discovery):
                    value = m.spend_machine(inst, budget=None)
            self.assertTrue(value['complete'])
            raw = m.spend_new('codex', path)
            with path.open('rb') as stream:
                m.spend_read(raw, stream)
            self.assertEqual(value['since_week']['processed'], m.spend_sum([raw])['processed'])
            self.assertEqual(value['since_week']['processed'], 97)
            m._SPEND_MEM.clear(); m._MACHINE.clear()
            with snapshot():
                self.assertEqual(m.spend_machine(inst, budget=None)['since_week']['processed'], 97)
            self.jsonl(path, [{'timestamp': self.at(4), 'type': 'token_usage_record',
                               'payload': self.record(second, 'b2', 20, 0, 1)[1]}])
            staged.write_bytes(last_bytes)
            original_open = m.history_open
            def rotate_after_open(source, *args, **kwargs):
                result = original_open(source, *args, **kwargs)
                if Path(source) == path and staged.exists():
                    staged.replace(path)
                return result
            m._SPEND_MEM.clear()
            with snapshot(), mock.patch.object(m, 'history_open', side_effect=rotate_after_open):
                held = m.spend_file(inst, path, 'codex', {self.codex}, set())
            self.assertEqual(held['id'], second, 'metadata must describe the held inode, not the new pathname')
            self.assertEqual(m.spend_sum([held])['processed'], 118)
            m._SPEND_MEM.clear(); m._MACHINE.clear()
            with snapshot():
                self.assertEqual(m.spend_machine(inst, budget=None)['since_week']['processed'], 53)
            stat = path.stat()
            key = m.hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:32]
            bad = {**m.spend_new('codex', path), 'v': 7, 'key': key, 'ino': [stat.st_dev, stat.st_ino],
                   'stamp': [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_mode, stat.st_nlink],
                   'complete': True, 'published': True}
            bad['state']['offset'] = stat.st_size
            kept = {k: v for k, v in bad.items() if k not in m.SPEND_UNKEPT}
            with m._owners['spend_file']._m_usage_cache.database(inst) as database:
                database.put('file:' + key, kept)
            m._SPEND_MEM.clear(); m._MACHINE.clear()
            with snapshot():
                self.assertEqual(m.spend_machine(inst, budget=None)['since_week']['processed'], 53)
            fourth = '44444444-fourth'
            replacement = self.cx(scratch, fourth, [(1, *self.record(fourth, 'd1', 70, 0, 4))])
            staged.write_bytes(replacement.read_bytes())
            replacement.unlink()
            m._SPEND_MEM.clear(); m._MACHINE.clear()
            with snapshot():
                self.assertEqual(m.codex_meta(path, {self.codex}, set())[0], third)
                staged.replace(path)  # the stale memo now predates discovery itself
                self.assertEqual(m.spend_machine(inst, budget=None)['since_week']['processed'], 74)
            for sid in (first, second, third, fourth):
                self.assertNotIn(sid.encode(), (self.r / '.brain/usage.sqlite').read_bytes())

    def test_append_during_discovery_retries_or_reports_unavailable_without_partial_job_cache(self):
        (self.wt / 'progress.txt').write_text('fabricated worker progress\n')
        self.git('add', 'progress.txt', cwd=self.wt)
        self.git('commit', '-qm', 'fixture work', cwd=self.wt)
        sid = '11111111-live'
        path = self.cx(self.wt, sid, [(1, *self.record(sid, 'r1', 40, 0, 10))])
        m = core_loader.SourceFileLoader('append_discovery_fixture', str(CORE / 'bin/brain')).load_module()
        snapshot = m._owners['spend_file']._m_command_reads.snapshot
        row = self.row()
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(self.r)}):
            inst, count = m.Instance(self.r), [0]
            node = inst.node('app')
            original = m.codex_mark_reader
            def append_after_marker(*args, **kwargs):
                result = original(*args, **kwargs)
                count[0] += 1
                self.jsonl(path, [{'timestamp': self.at(2), 'type': 'token_usage_record',
                                   'payload': self.record(sid, 'race-' + str(count[0]), 10, 0, 0)[1]}])
                return result
            def append_once(*args, **kwargs):
                return append_after_marker(*args, **kwargs) if not count[0] else original(*args, **kwargs)
            with snapshot(), mock.patch.object(m, 'codex_mark_reader', side_effect=append_once):
                used = m.job_usage(inst, node, row)
            self.assertEqual(used['processed'], 60)
            self.assertEqual(used['source'], 'transcripts: 1 session')
            # Worktree creation can cache the original job episode before setUp
            # moves its start time. Inspect the measured episode's exact key.
            key = m._owners['job_usage']._m_jobs.digest([node['id'], m.job_key(row), ''])
            cache = m.spend_cache_dir(inst) / 'jobs' / (key + '.json')
            self.assertEqual(json.loads(cache.read_text())['usage']['processed'], 60)
            with mock.patch.object(m, 'job_budget', return_value={'tokens': 40, 'hours': None}):
                self.assertTrue(any('40-token budget' in x for x in m.runaway_jobs(inst, node)))
            before = cache.read_bytes()
            self.jsonl(path, [{'timestamp': self.at(3), 'type': 'token_usage_record',
                               'payload': self.record(sid, 'next', 10, 0, 0)[1]}])
            m._SPEND_MEM.clear()
            with snapshot(), mock.patch.object(m, 'codex_mark_reader', side_effect=append_after_marker):
                self.assertIsNone(m.job_usage(inst, node, row), 'continued churn must be unavailable, not zero')
            self.assertEqual(cache.read_bytes(), before, 'incomplete discovery must not publish a partial job total')
            m._MACHINE.clear()
            with snapshot(), mock.patch.object(m, 'codex_mark_reader', side_effect=append_after_marker):
                machine = m.spend_machine(inst, budget=None)
            raw = m.spend_new('codex', path)
            with path.open('rb') as stream:
                m.spend_read(raw, stream)
            expected = m.spend_sum([raw])['processed']
            self.assertTrue(machine['complete'])
            self.assertEqual(machine['since_week']['processed'], expected)
            m._LIVE.clear()
            with snapshot():
                self.assertEqual(m.job_usage(inst, node, row)['processed'], expected)
            self.assertEqual(json.loads(cache.read_text())['usage']['processed'], expected)
            with mock.patch.object(m, 'job_budget', return_value={'tokens': 40, 'hours': None}):
                self.assertTrue(any('40-token budget' in x for x in m.runaway_jobs(inst, node)))

    def test_finished_usage_uses_the_recorded_final_ledger_counts(self):
        m = core_loader.SourceFileLoader('speed_finished_fixture', str(CORE / 'bin/brain')).load_module()
        row = {**self.row(), 'Ended': self.at(10), 'Outcome': 'merged',
               'Usage': 'transcripts: 1 session', 'Fresh in': '12', 'Output': '3'}
        inst = m.Instance(self.r)
        with mock.patch.object(m, 'job_usage', side_effect=AssertionError('recounted finished job')):
            self.assertEqual(m.live_usage(inst, inst.node('app'), row)['processed'], 15)


if __name__ == "__main__":
    unittest.main()
