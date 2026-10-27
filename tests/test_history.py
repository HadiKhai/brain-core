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


class History(Scenario):
    cli_in_process = True
    """brain history: past sessions about a home, extracted without a model into batches readers finish."""
    fixture_init_in_process = True
    fixture_templates = True

    def setUp(self):
        super().setUp()
        self.cfg = self.tmp / "config"
        self.env["BRAIN_CONFIG"] = str(self.cfg / "brains.json")
        (self.r, self.home, self.wt, self.clone, self.claude,
         self.codex, self.other) = self.cached_setup('history', self.history_fixture)

    def history_fixture(self):
        self.cfg.mkdir()
        self.r = self.init("alice")
        self.home = self.repo(self.tmp / "garden")
        self.git("remote", "add", "origin", "https://github.com/Acme/Garden.git", cwd=self.home)
        self.b(self.r, "new", "garden", "--title", "Garden", "--home", str(self.home))
        (self.r / "nodes" / "garden" / "never-name.txt").write_text("Zebracorp\n")
        self.claude, self.codex, self.other = (self.tmp / d for d in (".claude-work", ".codex", ".codex-other"))
        subs = [{"id": "work-claude", "runtime": "claude", "label": "Work", "config_dir": str(self.claude),
                 "brains": ["alice"]},
                {"id": "codex", "runtime": "codex", "config_dir": str(self.codex), "brains": ["*"]},
                {"id": "someone-else", "runtime": "codex", "config_dir": str(self.other), "brains": ["bob"]}]
        (self.cfg / "subscriptions.json").write_text(json.dumps({"subscriptions": subs}))
        self.wt = self.tmp / "garden-wt"
        self.git("worktree", "add", "-q", "-b", "feature", str(self.wt), cwd=self.home)
        self.clone = self.tmp / "scratch-clone"
        self.git("clone", "-q", str(self.home), str(self.clone), cwd=self.tmp)
        self.git("remote", "set-url", "origin", "git@github.com:acme/garden.git", cwd=self.clone)
        self.sessions()
        return self.r, self.home, self.wt, self.clone, self.claude, self.codex, self.other

    @staticmethod
    def jsonl(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))

    def claude_rows(self, cwd, sid, start=10):
        def at(m):
            return f"2026-10-01T{start:02d}:{m:02d}:00Z"
        base = {"cwd": str(cwd), "sessionId": sid, "gitBranch": "main", "isSidechain": False}

        def user(uuid, content, m, **kw):
            return {**base, "type": "user", "uuid": f"{sid}-{uuid}", "timestamp": at(m),
                    "message": {"role": "user", "content": content}, **kw}

        def said(mid, text, m):
            return {**base, "type": "assistant", "uuid": f"{sid}-a{m}", "timestamp": at(m),
                    "message": {"id": mid, "role": "assistant", "content": [{"type": "text", "text": text}]}}
        return [
            {"type": "ai-title", "aiTitle": "Storage choice", "sessionId": sid},
            user("u1", "Use SQLite, not Postgres: one file is easier to back up.", 1, promptSource="sdk"),
            said("m1", "Switching to SQLite.", 2), said("m1", "Migration written.", 3),
            {**base, "type": "assistant", "uuid": f"{sid}-tool", "timestamp": at(4), "message": {
                "id": "m2", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
            user("tr", [{"type": "tool_result", "content": "TOOL-OUTPUT-NEVER"}], 5),
            user("meta", "META-NEVER", 6, isMeta=True),
            user("note", "<task-notification>NOTICE-NEVER</task-notification>", 7, promptSource="system"),
            user("side", "SIDECHAIN-NEVER", 8, isSidechain=True),
            user("u2", "<system-reminder>REMINDER-NEVER</system-reminder>Keep the API flat. Zebracorp asked.", 9),
            said("m3", "Flattened.", 10),
            user("u3", "<command-name>/review</command-name>\n<command-message>review</command-message>\n"
                       "<command-args>the auth flow</command-args>", 11),
            user("u4", "<local-command-stdout>STDOUT-NEVER</local-command-stdout>", 12),
            {**base, "type": "system", "subtype": "compact_boundary", "uuid": f"{sid}-cb", "timestamp": at(13),
             "compactMetadata": {"trigger": "auto", "preTokens": 400000}},
            user("sum", "This session is being continued. SUMMARY-NEVER", 14, isCompactSummary=True),
            user("u5", "Rotate it: api_key = sk-" + "a" * 30 + "\nThen tell me.", 15),
            said("m4", "Done.", 16),
            user("u6", "```log\n" + "\n".join(f"line {i} of a long pasted log" for i in range(400)) + "\n```\n"
                       "That is the crash.", 17),
            user("u7", "\n".join(f"Paragraph {i}: the plan keeps the garden small and the beds raised." for i in
                                 range(140)), 18),
        ]

    def codex_rows(self, sid, cwd, items, repo=None, at0="2026-10-02T09:00:00Z", **meta):
        rows = [{"timestamp": at0, "type": "session_meta", "payload": {
            "id": sid, "session_id": sid, "cwd": str(cwd), "originator": "T3 Code",
            "git": {"repository_url": repo or "", "branch": "main"}, **meta}}]
        return rows + [{"timestamp": at0, "type": t, "payload": p} for t, p in items]

    @staticmethod
    def item(kind, text, id=None, phase=None, turn="t1"):
        item = {"type": kind, "id": id or text[:12], "content": [{"type": "text", "text": text}]}
        if phase:
            item["phase"] = phase
        return "event_msg", {"type": "item_completed", "turn_id": turn, "item": item}

    def sessions(self):
        p = self.claude / "projects"
        self.jsonl(p / "-garden" / "11111111-aaaa.jsonl", self.claude_rows(self.home, "11111111-aaaa"))
        self.jsonl(p / "-garden" / "11111111-aaaa" / "subagents" / "agent-1.jsonl",
                   self.claude_rows(self.home, "subagent-0001", start=11))
        self.jsonl(p / "-garden-wt" / "22222222-bbbb.jsonl", self.claude_rows(self.wt, "22222222-bbbb", start=12)[:3])
        self.jsonl(p / "-clone" / "33333333-cccc.jsonl", self.claude_rows(self.clone, "33333333-cccc", start=13)[:3])
        elsewhere = self.repo(self.tmp / "elsewhere")
        self.jsonl(p / "-elsewhere" / "44444444-dddd.jsonl",
                   [{**r, "message": {"role": "user", "content": "ELSEWHERE-NEVER"}} if r.get("type") == "user" else r
                    for r in self.claude_rows(elsewhere, "44444444-dddd")[:3]])
        s = self.codex / "sessions" / "2026" / "10" / "02"
        self.jsonl(s / "rollout-2026-10-02T09-00-00-aaaa0001.jsonl", self.codex_rows(
            "aaaa0001-0000", "/gone/folder", [
                self.item("UserMessage", "<environment_context>ENV-NEVER</environment_context>", id="env"),
                self.item("UserMessage", "Ship the CLI first.", id="u-ship"),
                self.item("AgentMessage", "Looking around.", phase="commentary"),
                ("event_msg", {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 150000}}}),
                self.item("AgentMessage", "CLI shipped.", phase="final_answer"),
                self.item("AgentMessage", "AFTER-FINAL-NEVER", phase="commentary"),
                ("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "CLI shipped."}),
                ("compacted", {"message": "COMPACTED-NEVER"}),
                self.item("UserMessage", "Drop the web UI for now.\n<in-app-browser-context source=\"x\">"
                                         "BROWSER-NEVER</in-app-browser-context>", id="u-drop", turn="t2"),
                self.item("AgentMessage", "Dropping it.", phase="commentary", turn="t2"),
            ], repo="git@github.com:acme/garden.git"))
        self.jsonl(s / "rollout-2026-10-02T10-00-00-aaaa0002.jsonl", self.codex_rows(
            "aaaa0002-0000", self.home, [
                ("response_item", {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "Old style question?"}]}),
                ("event_msg", {"type": "user_message", "message": "Old style question?"}),
                ("event_msg", {"type": "agent_message", "message": "Old style answer."}),
            ], at0="2026-10-02T10:00:00Z"))
        self.jsonl(s / "rollout-2026-10-02T11-00-00-aaaa0003.jsonl", self.codex_rows(
            "aaaa0003-0000", self.home, [
                ("compacted", {"message": "Earlier we chose Rust for the core."}),
                self.item("UserMessage", "Continue from there.", id="u-cont"),
                self.item("AgentMessage", "Continuing.", phase="final_answer"),
            ], at0="2026-10-02T11:00:00Z", history_base={"thread_id": "gone-parent", "end_byte_offset": 9}))
        self.jsonl(s / "rollout-2026-10-02T12-00-00-aaaa0004.jsonl", self.codex_rows(
            "aaaa0004-0000", self.home, [
                self.item("UserMessage", "Brief: make the tests pass.", id="u-brief"),
                self.item("AgentMessage", "Halfway.", phase="final_answer"),
                self.item("UserMessage", "Keep going.", id="u-more", turn="t2"),
                self.item("AgentMessage", "Fixed 3 tests.", phase="final_answer", turn="t2"),
            ], at0="2026-10-02T12:00:00Z", originator="codex_exec"))
        self.jsonl(s / "rollout-2026-10-02T13-00-00-aaaa0005.jsonl", self.codex_rows(
            "aaaa0005-0000", self.home, [self.item("UserMessage", "Ship the CLI first.", id="u-ship"),
                                         self.item("AgentMessage", "CLI shipped.", phase="final_answer")],
            at0="2026-10-02T13:00:00Z", forked_from_id="aaaa0001-0000"))
        self.jsonl(self.other / "sessions" / "2026" / "10" / "02" / "rollout-x-bbbb0001.jsonl", self.codex_rows(
            "bbbb0001-0000", self.home, [self.item("UserMessage", "OTHER-ACCOUNT-NEVER", id="o1")]))

    def batches(self):
        out = self.tmp / "garden-work" / "scratch" / "history"
        return out, json.loads((out / "manifest.json").read_text()), \
            "\n".join(f.read_text() for f in sorted((out / "batches").glob("b*.md")))

    def test_extract_keeps_operator_turns_and_last_replies_and_drops_the_rest(self):
        before = self.git("status", "--porcelain", cwd=self.r)
        home_head = self.git("rev-parse", "HEAD", cwd=self.home)
        summary = self.b(self.r, "history", "garden")
        out, m, text = self.batches()
        self.assertIn("garden: 7 sessions", summary)
        self.assertIn("1 subscription(s) not allowed for this brain: not read", summary)
        self.assertIn("1 copies of others", summary)
        self.assertIn("1 session(s) begin with Codex's compaction summary", summary)
        self.assertIn("claude compacted as early as 400k here (1 compaction(s))", summary)
        self.assertIn("codex compacted as early as 150k here", summary)
        for kept in ("Use SQLite, not Postgres", "> Switching to SQLite.\n>\n> Migration written.", "Keep the API flat.",
                     "/review the auth flow", "> Then tell me.", "That is the crash.", "Paragraph 139",
                     "Ship the CLI first.", "> CLI shipped.", "Drop the web UI for now.", "> Dropping it.",
                     "Old style question?", "> Old style answer.", "Earlier we chose Rust for the core.",
                     "Brief: make the tests pass.", "> Fixed 3 tests.", '"Storage choice"', "pasted block: "):
            self.assertIn(kept, text)
        for dropped in ("TOOL-OUTPUT-NEVER", "META-NEVER", "NOTICE-NEVER", "SIDECHAIN-NEVER", "REMINDER-NEVER",
                        "STDOUT-NEVER", "SUMMARY-NEVER", "ENV-NEVER", "COMPACTED-NEVER", "BROWSER-NEVER",
                        "ELSEWHERE-NEVER", "OTHER-ACCOUNT-NEVER", "AFTER-FINAL-NEVER", "Zebracorp", "sk-aaaa", "Halfway.", "Looking around."):
            self.assertNotIn(dropped, text)
        self.assertEqual(text.count("Old style question?"), 1)
        self.assertEqual(text.count("Ship the CLI first."), 1)
        self.assertIn("[redacted secret]", text)
        self.assertIn("line 0 of a long pasted log", text)
        self.assertNotIn("line 200 of a long pasted log", text)
        matched = {s["id"]: s["match"] for s in m["sessions"]}
        self.assertEqual(matched, {"11111111-aaaa": "folder", "22222222-bbbb": "folder", "33333333-cccc": "remote",
                                   "aaaa0001-0000": "remote", "aaaa0002-0000": "folder", "aaaa0003-0000": "folder",
                                   "aaaa0004-0000": "folder"})
        self.assertTrue(next(s for s in m["sessions"] if s["id"] == "aaaa0004-0000")["delegated"])
        self.assertEqual(m["counts"]["files"], 9)  # subagent transcripts are never scanned
        self.assertEqual(m["repo"], "github.com/acme/garden")
        for f in [out / "manifest.json", *(out / "batches").glob("*.md")]:
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        self.assertEqual((out / "batches").stat().st_mode & 0o777, 0o700)
        self.assertIn("scratch/", (self.tmp / "garden-work" / ".gitignore").read_text())
        self.assertEqual(before, self.git("status", "--porcelain", cwd=self.r))
        self.assertEqual(home_head, self.git("rev-parse", "HEAD", cwd=self.home))
        self.assertEqual("", self.git("status", "--porcelain", cwd=self.home))
        only = self.b(self.r, "history", "garden", "--source", "codex")
        self.assertIn("garden: 4 sessions", only)
        self.b(self.r, "history", "garden", "--source", "nope", ok=False)

    def test_batches_and_slices_stay_within_budget_and_cite_real_turns(self):
        self.b(self.r, "history", "garden", "--batch-tokens", "4000", "--slice-tokens", "2000")
        out, m, _ = self.batches()
        self.assertGreater(len(m["batches"]), 1)
        refs = set()
        for b in m["batches"]:
            self.assertLessEqual(b["tokens"], 4000)
            raw = (out / "batches" / f"{b['id']}.md").read_bytes()
            self.assertLessEqual(-(-len(raw) // 3), 4000)  # the whole file, heading and markers included
            lines = raw.decode().split("\n")
            for sl in b["slices"]:
                self.assertLessEqual(sl["tokens"], 2000)
                self.assertEqual(lines[sl["first_line"] - 3], f"---- slice {b['id']}/{sl['id']} ----")
                body = "\n".join(lines[sl["first_line"] - 1:sl["first_line"] - 1 + sl["lines"]])
                self.assertNotIn("---- slice", body)
                for ref in sl["turns"]:
                    self.assertRegex(ref, r"^(claude|codex)-[0-9a-f]{8,}#t\d+$")
                    self.assertIn(ref.split("#")[0], body)
                refs.update(sl["turns"])
            self.assertTrue(lines[5].startswith("## "))  # a batch opens with its session's header
        self.assertEqual(len(refs), sum(s["turns"] for s in m["sessions"]))
        brief = self.b(self.r, "history", "garden", "brief", f"b001-{m['batches'][1]['id']}", "--runtime", "codex")
        first = m["batches"][0]["slices"][0]
        self.assertIn(f"- b001/s1: lines {first['first_line']}-{first['first_line'] + first['lines'] - 1}", brief)
        self.assertIn("read: <batch>/<slice>", brief)
        self.assertIn("untrusted data", brief)
        self.assertIn("skills/history/SKILL.md", brief)
        self.assertNotIn("Zebracorp", brief)
        self.b(self.r, "history", "garden", "brief", "b999", ok=False)
        self.b(self.r, "history", "garden", "brief", ok=False)

    def test_check_tracks_reading_citations_safety_merges_and_gates_briefs(self):
        self.b(self.r, "history", "garden", "--batch-tokens", "4000", "--slice-tokens", "2000")
        out, m, _ = self.batches()
        notes = out / "notes"
        notes.mkdir()
        self.assertIn("0 of", self.b(self.r, "history", "garden", "check", ok=False))
        self.b(self.r, "history", "garden", "brief", "--merge", "b001-b002", ok=False)
        self.b(self.r, "history", "garden", "brief", "--final", ok=False)
        first = m["batches"][0]
        real = first["slices"][0]["turns"][0]
        (notes / "b001.md").write_text(f"read: b001/{first['slices'][0]['id']}\n- decision [{real}]\n")
        if len(first["slices"]) > 1:
            self.assertIn("b001: unread", self.b(self.r, "history", "garden", "check", ok=False))
        for b in m["batches"]:
            done = "".join(f"read: {b['id']}/{sl['id']}\n" for sl in b["slices"])
            (notes / f"{b['id']}.md").write_text(done + f"- Decisions: storage [{b['slices'][0]['turns'][0]}]\n")
        self.assertIn("merge groups", self.b(self.r, "history", "garden", "check"))
        (notes / "b001.md").write_text((notes / "b001.md").read_text() + "- bogus [codex-deadbeef#t9]\n")
        self.assertIn("1 to no such turn", self.b(self.r, "history", "garden", "check", ok=False))
        elsewhere = m["batches"][1]["slices"][0]["turns"][-1]
        (notes / "b001.md").write_text((notes / "b001.md").read_text().replace("codex-deadbeef#t9", elsewhere))
        self.assertIn("1 outside this batch", self.b(self.r, "history", "garden", "check", ok=False))
        (notes / "b001.md").write_text((notes / "b001.md").read_text().replace(elsewhere, real)
                                       + "- Zebracorp said so\n")
        self.assertIn("1 unsafe line(s)", self.b(self.r, "history", "garden", "check", ok=False))
        (notes / "b001.md").write_text((notes / "b001.md").read_text().replace("- Zebracorp said so\n", ""))
        self.b(self.r, "history", "garden", "check")
        last = m["batches"][-1]["id"]
        merge = self.b(self.r, "history", "garden", "brief", "--merge", f"b001-{last}")
        self.assertIn(f"merge-b001-{last}.md", merge)
        self.assertIn("`merged: <batch>`", merge)
        (notes / f"merge-b001-{last}.md").write_text("merged: b001\n")
        self.assertIn("not yet merged", self.b(self.r, "history", "garden", "check"))
        self.assertIn("not yet merged", self.b(self.r, "history", "garden", "brief", "--final", ok=False))
        (notes / f"merge-b001-{last}.md").write_text("".join(f"merged: {b['id']}\n" for b in m["batches"]))
        final = self.b(self.r, "history", "garden", "brief", "--final")
        self.assertIn(f"merge-b001-{last}.md", final)
        self.assertIn("--proposed", final)
        self.assertIn("--repo \"github.com/acme/garden\"", final)
        self.assertIn("--commit " + self.git("rev-parse", "HEAD", cwd=self.home).strip(), final)
        self.assertIn("readers' notes exist", self.b(self.r, "history", "garden", ok=False))
        self.b(self.r, "history", "garden", "--fresh")
        self.assertFalse(notes.exists())

    def test_codex_forms_mixed_in_one_file_malformed_records_and_late_commentary(self):
        s = self.codex / "sessions" / "2026" / "10" / "03"
        odd = [("event_msg", {"type": "token_count", "info": ["not", "a", "dict"]}),
               ("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "id": {"x": 1},
                                                                  "content": "not a list"}}),
               ("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": 7}]})]
        rows = self.codex_rows("cccc0001-0000", self.home, [
            ("response_item", {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "First, in the oldest form."}]}),
            ("event_msg", {"type": "user_message", "message": "First, in the oldest form."}),
            ("response_item", {"type": "message", "role": "assistant", "phase": "final_answer",
                               "content": [{"type": "output_text", "text": "Answer one."}]}),
            ("response_item", {"type": "message", "role": "assistant", "phase": "commentary",
                               "content": [{"type": "output_text", "text": "LATE-COMMENTARY-NEVER"}]}),
            *odd,
            self.item("UserMessage", "Second, after a newer CLI resumed it.", id="u-second"),
            self.item("AgentMessage", "Answer two.", phase="final_answer"),
        ], at0="2026-10-03T09:00:00Z")
        rows.insert(3, {"timestamp": {"bad": 1}, "type": "event_msg", "payload": [1, 2]})
        self.jsonl(s / "rollout-2026-10-03T09-00-00-cccc0001.jsonl", rows)
        with open(s / "rollout-2026-10-03T09-00-00-cccc0001.jsonl", "a") as fh:
            fh.write("{not json\n[1,2]\n")
        self.jsonl(self.claude / "projects" / "-garden" / "55555555-eeee.jsonl", [
            {"type": "user", "uuid": 5, "sessionId": 9, "cwd": str(self.home), "timestamp": "2026-10-03T10:00:00Z",
             "message": {"content": "Odd ids are fine."}},
            {"type": "assistant", "cwd": str(self.home), "message": {"id": ["x"], "content": [{"type": "text", "text": "Yes."}]}}])
        self.b(self.r, "history", "garden")
        _, m, text = self.batches()
        self.assertEqual(text.count("First, in the oldest form."), 1)
        self.assertIn("Second, after a newer CLI resumed it.", text)
        self.assertIn("> Answer one.", text)
        self.assertIn("> Answer two.", text)
        self.assertNotIn("LATE-COMMENTARY-NEVER", text)
        self.assertIn("Odd ids are fine.", text)
        self.assertEqual(next(s for s in m["sessions"] if s["id"] == "cccc0001-0000")["turns"], 2)

    def test_redaction_precedes_trimming(self):
        long = "\n".join(["filler line"] * 2500) + "\ntoken = " + "z" * 5000 + "TAILSECRET\n" + \
            "Zebra" + "corp " * 2 + "\n" + "x" * 3000
        self.jsonl(self.claude / "projects" / "-garden" / "66666666-ffff.jsonl", [
            {"type": "user", "uuid": "u-long", "sessionId": "66666666-ffff", "cwd": str(self.home),
             "timestamp": "2026-10-04T10:00:00Z", "message": {"content": long}}])
        self.b(self.r, "history", "garden")
        _, _, text = self.batches()
        self.assertIn("long message:", text)
        self.assertNotIn("TAILSECRET", text)
        self.assertNotIn("zzzz", text)
        self.assertNotIn("Zebracorp", text)

    def test_links_never_reach_a_forbidden_subscription_and_output_stays_outside_the_home(self):
        personal = self.tmp / ".claude-personal"
        subs = json.loads((self.cfg / "subscriptions.json").read_text())
        subs["subscriptions"].append({"id": "personal-claude", "runtime": "claude", "config_dir": str(personal),
                                      "brains": ["bob"]})
        (self.cfg / "subscriptions.json").write_text(json.dumps(subs))
        self.jsonl(personal / "projects" / "-garden" / "77777777-aaaa.jsonl",
                   [{"type": "user", "uuid": "p1", "sessionId": "77777777-aaaa", "cwd": str(self.home),
                     "timestamp": "2026-10-05T10:00:00Z", "message": {"content": "PERSONAL-NEVER"}}])
        (self.claude / "projects" / "-linked").symlink_to(personal / "projects" / "-garden")
        (self.claude / "projects" / "-garden" / "linked.jsonl").symlink_to(
            personal / "projects" / "-garden" / "77777777-aaaa.jsonl")
        summary = self.b(self.r, "history", "garden")
        _, m, text = self.batches()
        self.assertNotIn("PERSONAL-NEVER", text)
        self.assertIn("1 linked into a subscription not allowed for this brain", summary)
        self.assertIn("2 subscription(s) not allowed", summary)
        shutil.rmtree(self.tmp / "garden-work" / "scratch")
        (self.home / "inside").mkdir()
        (self.tmp / "garden-work" / "scratch").symlink_to(self.home / "inside")
        self.assertIn("resolves inside the home", self.b(self.r, "history", "garden", ok=False))
        self.assertEqual([], list((self.home / "inside").iterdir()))

    def test_a_turn_spanning_two_batches_and_unsafe_notes_gate_the_briefs(self):
        self.b(self.r, "history", "garden", "--batch-tokens", "2000", "--slice-tokens", "2000")
        out, m, _ = self.batches()
        spans = {}
        for b in m["batches"]:
            for sl in b["slices"]:
                for ref in sl["turns"]:
                    spans.setdefault(ref, []).append(b["id"])
        ref, where = next((r, w) for r, w in spans.items() if len(set(w)) > 1)
        notes = out / "notes"
        notes.mkdir()
        for b in m["batches"]:
            done = "".join(f"read: {b['id']}/{sl['id']}\n" for sl in b["slices"])
            (notes / f"{b['id']}.md").write_text(done + f"- Decisions: x [{b['slices'][0]['turns'][0]}]\n")
        (notes / f"{where[-1]}.md").write_text((notes / f"{where[-1]}.md").read_text() + f"- long turn [{ref}]\n")
        self.b(self.r, "history", "garden", "check")
        (notes / "b001.md").write_text((notes / "b001.md").read_text() + "- api_key = sk-" + "b" * 30 + "\n")
        self.assertIn("fix these notes first: b001",
                      self.b(self.r, "history", "garden", "brief", "--merge", "b001-b002", ok=False))
        self.assertIn("fix these notes first", self.b(self.r, "history", "garden", "brief", "--final", ok=False))

    def test_output_links_old_modes_and_unregistered_accounts(self):
        self.b(self.r, "history", "garden")
        out = self.tmp / "garden-work" / "scratch" / "history"
        (self.home / "inside").mkdir()
        (self.home / "inside" / "b999.md").write_text("keep me\n")
        shutil.rmtree(out / "batches")
        (out / "batches").symlink_to(self.home / "inside")
        os.chmod(out, 0o755)
        os.chmod(out / "manifest.json", 0o644)
        stranger = self.tmp / ".claude-stranger" / "projects" / "-garden"
        self.jsonl(stranger / "88888888-aaaa.jsonl",
                   [{"type": "user", "uuid": "s1", "sessionId": "88888888-aaaa", "cwd": str(self.home),
                     "timestamp": "2026-10-06T10:00:00Z", "message": {"content": "STRANGER-NEVER"}}])
        (self.claude / "projects" / "-garden" / "stranger.jsonl").symlink_to(stranger / "88888888-aaaa.jsonl")
        summary = self.b(self.r, "history", "garden")
        self.assertEqual(["b999.md"], [p.name for p in (self.home / "inside").iterdir()])
        self.assertFalse((out / "batches").is_symlink())
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out / "manifest.json").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("STRANGER-NEVER", self.batches()[2])
        self.assertIn("1 linked into a subscription not allowed", summary)
        self.assertEqual("", self.git("status", "--porcelain", "--ignored", "--", "inside", cwd=self.home)
                         .replace("?? inside/\n", ""))
        brief = self.tmp / "briefs" / "reader.md"
        brief.parent.mkdir()
        brief.write_text("old\n")
        os.chmod(brief, 0o644)
        self.b(self.r, "history", "garden", "brief", "b001", "--out", str(brief))
        self.assertEqual(brief.stat().st_mode & 0o777, 0o600)
        self.assertIn("inside the home", self.b(self.r, "history", "garden", "brief", "b001", "--out",
                                                str(self.home / "brief.md"), ok=False))
        self.assertFalse((self.home / "brief.md").exists())

    def test_metadata_terms_odd_values_and_reconciled_copies(self):
        self.git("remote", "set-url", "origin", "https://github.com/Zebracorp/garden.git", cwd=self.home)
        s = self.codex / "sessions" / "2026" / "10" / "07"
        self.jsonl(s / "rollout-a-dddd0001.jsonl", [
            {"timestamp": "2026-10-07T09:00:00Z", "type": "session_meta", "payload": {
                "id": "dddd0001-0000", "cwd": str(self.home), "originator": "codex_exec",
                "history_base": {"thread_id": "gone-parent"}}},
            {"timestamp": "2026-10-07T09:00:00Z", "type": "compacted", "payload": {"message": "Summary of the gone part."}},
            {"timestamp": "2026-10-07T09:01:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 10 ** 400}}}},
            {"timestamp": "2026-10-07T09:01:00Z", **dict(zip(("type", "payload"),
                                                           self.item("UserMessage", "Delegated brief.", id="d1")))},
            {"timestamp": "2026-10-07T09:02:00Z", **dict(zip(("type", "payload"),
                                                           self.item("AgentMessage", "Delegated result.",
                                                                     phase="final_answer")))}])
        self.jsonl(s / "rollout-b-dddd0002.jsonl", [
            {"timestamp": "2026-10-07T10:00:00Z", "type": "session_meta", "payload": {
                "id": "dddd0002-0000", "cwd": str(self.home)}}])
        self.jsonl(s / "rollout-c-dddd0003.jsonl", [
            {"timestamp": "2026-10-07T11:00:00Z", "type": "session_meta", "payload": {
                "id": "dddd0003-0000", "cwd": str(self.home), "forked_from_id": "dddd0002-0000"}},
            {"timestamp": "2026-10-07T11:00:00Z", "type": "compacted", "payload": {"message": "Only record left."}},
            {"timestamp": "2026-10-07T11:01:00Z", **dict(zip(("type", "payload"),
                                                           self.item("UserMessage", "After it.", id="e1")))}])
        self.jsonl(s / "rollout-d-dddd0004.jsonl", [
            {"timestamp": "2026-10-07T12:00:00Z", "type": "session_meta", "payload": {
                "id": "dddd\ud800", "cwd": str(self.home) + "\u0000x"}}])
        with open(s / "rollout-d-dddd0004.jsonl", "a") as fh:
            fh.write('{"timestamp": "x", "type": "session_meta", "payload": {"id": "\\ud800", "cwd": "/a\\u0000"}}\n')
        p = self.claude / "projects" / "-garden"
        base = {"cwd": str(self.home), "timestamp": "2026-10-08T09:00:00Z"}
        self.jsonl(p / "99999999-aaaa.jsonl", [
            {**base, "type": "user", "uuid": "same-turn", "sessionId": "99999999-aaaa",
             "message": {"content": "Asked once, answered later."}}])
        self.jsonl(p / "99999999-bbbb.jsonl", [
            {**base, "type": "user", "uuid": "same-turn", "sessionId": "99999999-bbbb",
             "message": {"content": "Asked once, answered later."}},
            {**base, "type": "assistant", "timestamp": "2026-10-08T09:05:00Z", "sessionId": "99999999-bbbb",
             "message": {"id": "m", "content": [{"type": "text", "text": "The finished answer."}]}}])
        with open(p / "99999999-bbbb.jsonl", "a") as fh:
            fh.write('{"type":"user","cut off mid-wri\n')
        summary = self.b(self.r, "history", "garden")
        out, m, text = self.batches()
        for kept in ("Summary of the gone part.", "Delegated brief.", "Delegated result.", "Only record left.",
                     "After it.", "The finished answer."):
            self.assertIn(kept, text)
        self.assertEqual(text.count("Asked once, answered later."), 1)
        self.assertIn("malformed records skipped", summary)
        for shown in ((out / "manifest.json").read_text(), summary, self.b(self.r, "history", "garden", "plan", "--json")):
            self.assertNotIn("zebracorp", shown.lower())

    def test_check_matches_scrub_and_rejects_malformed_citations(self):
        self.b(self.r, "history", "garden")
        out, m, _ = self.batches()
        notes = out / "notes"
        notes.mkdir()
        good = m["batches"][0]["slices"][0]["turns"][0]

        def write(extra):
            for b in m["batches"]:
                done = "".join(f"read: {b['id']}/{sl['id']}\n" for sl in b["slices"])
                (notes / f"{b['id']}.md").write_text(done + f"- Decisions: x [{good}]\n" + extra)
        write("")
        self.b(self.r, "history", "garden", "check")
        for bad, shown in (("- Zebra\u200bcorp agreed\n", "unsafe"), ("- api_\u200bkey = DEMO\n", "unsafe"),
                           ("- see [codex-deadbee#t99]\n", "no such turn")):
            write(bad)
            self.assertIn(shown, self.b(self.r, "history", "garden", "check", ok=False))
            self.b(self.r, "history", "garden", "brief", "--final", ok=False)

    def test_round_three_streams_forms_completions_odd_values_and_whitespace(self):
        s = self.codex / "sessions" / "2026" / "10" / "09"
        rows = self.codex_rows("eeee0001-0000", self.home, [
            self.item("UserMessage", "Question one.", id="q1"),
            self.item("AgentMessage", "Answer one.", phase="final_answer"),
            ("event_msg", {"type": "user_message", "message": "Question two, older CLI."}),
            ("event_msg", {"type": "agent_message", "message": "Answer two."}),
            ("event_msg", {"type": "user_message", "message": "Question three."}),
            ("event_msg", {"type": "agent_message", "message": "Still investigating."}),
            ("event_msg", {"type": "task_complete", "last_agent_message": "The final answer."}),
            ("event_msg", {"type": "agent_message", "message": "AFTER-DONE-NEVER"}),
        ], at0="2026-10-09T09:00:00Z")
        self.jsonl(s / "rollout-e-eeee0001.jsonl", rows)
        p = s / "rollout-e-eeee0001.jsonl"
        p.write_text("".join("  " + line + "\n" for line in p.read_text().splitlines()))
        self.jsonl(self.claude / "projects" / "-garden" / "aaaaaaaa-1111.jsonl", [
            {"type": "user", "uuid": "big-time", "sessionId": "aaaaaaaa-1111", "cwd": str(self.home),
             "timestamp": 10 ** 400, "message": {"content": "A huge timestamp."}},
            {"type": "user", "uuid": "key-time", "sessionId": "aaaaaaaa-1111", "cwd": str(self.home),
             "timestamp": {"Zebracorp": "safe"}, "message": {"content": "A dict timestamp."}}])
        self.jsonl(s / "rollout-f-ffff0001.jsonl", self.codex_rows("sk-" + "a" * 30, self.home, [
            self.item("UserMessage", "Secret-shaped id.", id="sid")], at0="2026-10-09T10:00:00Z"))
        summary = self.b(self.r, "history", "garden")
        out, m, text = self.batches()
        turns = text.split("## codex-eeee0001", 1)[1].split("\n## ", 1)[0]
        for kept in ("Question one.", "> Answer one.", "Question two, older CLI.", "> Answer two.",
                     "Question three.", "> The final answer.", "A huge timestamp.", "A dict timestamp."):
            self.assertIn(kept, text)
        self.assertNotIn("Still investigating.", turns)
        self.assertNotIn("AFTER-DONE-NEVER", turns)
        self.assertEqual(next(x for x in m["sessions"] if x["id"] == "eeee0001-0000")["turns"], 3)
        for shown in ((out / "manifest.json").read_text(), summary, self.b(self.r, "history", "garden", "plan", "--json")):
            self.assertNotIn("zebracorp", shown.lower())
            self.assertNotIn("sk-aaaa", shown)
        self.assertEqual([], [d.name for d in (out.parent).iterdir() if d.name != "history"])
        self.b(self.r, "history", "garden")
        self.assertEqual([], [d.name for d in (out.parent).iterdir() if d.name != "history"])

    def test_round_three_open_files_are_checked_where_they_really_are(self):
        loader = core_loader.SourceFileLoader("brain_history_open", str(CORE / "bin/brain"))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(mod)
        allowed, forbidden = self.tmp / "allowed", self.tmp / "forbidden"
        (allowed / "projects").mkdir(parents=True)
        (forbidden / "projects" / "-x").mkdir(parents=True)
        (forbidden / "projects" / "-x" / "f.jsonl").write_text("{}\n")
        (allowed / "projects" / "-x").symlink_to(forbidden / "projects" / "-x")
        (allowed / "projects" / "own.jsonl").write_text("{}\n")
        roots = [allowed.resolve()]
        fh, why = mod.history_open(str(allowed / "projects" / "-x" / "f.jsonl"), roots, set())
        self.assertIsNone(fh)
        self.assertEqual(why, "not_allowed")
        fh, inode = mod.history_open(str(allowed / "projects" / "own.jsonl"), roots, set())
        self.assertIsNotNone(fh)
        fh.close()
        self.assertEqual((None, "unreadable"), mod.history_open(str(allowed / "projects" / "own.jsonl"), roots, set(),
                                                                same=(0, 0)))
        self.assertEqual(mod.history_remote("ssh://git@github.com:2222/Acme/Garden.git"),
                         mod.history_remote("ssh://git@github.com:2222/acme/garden"))

    def test_round_three_brief_out_replaces_hard_links_and_check_matches_scrub_whole(self):
        self.b(self.r, "history", "garden")
        out, m, _ = self.batches()
        (self.home / "precious.txt").write_text("precious\n")
        linked = self.tmp / "briefs" / "reader.md"
        linked.parent.mkdir()
        os.link(self.home / "precious.txt", linked)
        self.b(self.r, "history", "garden", "brief", "b001", "--out", str(linked))
        self.assertEqual("precious\n", (self.home / "precious.txt").read_text())
        self.assertIn("history reader", linked.read_text())
        self.assertEqual(linked.stat().st_mode & 0o777, 0o600)
        notes = out / "notes"
        notes.mkdir()
        good = m["batches"][0]["slices"][0]["turns"][0]
        for b in m["batches"]:
            done = "".join(f"read: {b['id']}/{sl['id']}\n" for sl in b["slices"])
            (notes / f"{b['id']}.md").write_text(done + f"- Decisions: x [{good}]\n- Zebra\x0bcorp agreed\n")
        self.assertIn("unsafe", self.b(self.r, "history", "garden", "check", ok=False))
        self.b(self.r, "history", "garden", "brief", "--final", ok=False)

    def test_round_three_never_name_node_id_is_screened_from_the_summary(self):
        (self.r / "nodes" / "garden" / "never-name.txt").write_text("Zebracorp\ngarden\n")
        summary = self.b(self.r, "history", "garden")
        self.assertNotIn("garden", summary.lower())

    def test_round_four_identity_beats_place_and_turn_ids_beat_text(self):
        stray = self.repo(self.tmp / "garden-work" / "worktrees" / "stray")
        nested = self.repo(self.home / "vendor-repo")
        for name, cwd in (("bbbbbbbb-0001", stray), ("bbbbbbbb-0002", nested)):
            self.jsonl(self.claude / "projects" / ("-" + name) / f"{name}.jsonl", [
                {"type": "user", "uuid": name, "sessionId": name, "cwd": str(cwd),
                 "timestamp": "2026-10-10T09:00:00Z", "message": {"content": f"UNRELATED-{name}-NEVER"}}])
        self.jsonl(self.claude / "projects" / "-gone" / "bbbbbbbb-0003.jsonl", [
            {"type": "user", "uuid": "gone", "sessionId": "bbbbbbbb-0003",
             "cwd": str(self.tmp / "garden-work" / "worktrees" / "deleted-job"),
             "timestamp": "2026-10-10T10:00:00Z", "message": {"content": "From a deleted job worktree."}}])
        s = self.codex / "sessions" / "2026" / "10" / "10"
        self.jsonl(s / "rollout-g-gggg0001.jsonl", self.codex_rows("gggg0001-0000", self.home, [
            self.item("UserMessage", "Same question.", id="sq1", turn="t1"),
            self.item("AgentMessage", "First answer.", phase="final_answer", turn="t1"),
            ("event_msg", {"type": "user_message", "message": "Same question.", "turn_id": "t2"}),
            ("event_msg", {"type": "agent_message", "message": "Second answer.", "turn_id": "t2"}),
        ], at0="2026-10-10T11:00:00Z"))
        self.b(self.r, "history", "garden")
        _, m, text = self.batches()
        self.assertNotIn("UNRELATED-", text)
        self.assertIn("From a deleted job worktree.", text)
        self.assertEqual(next(x for x in m["sessions"] if x["id"] == "gggg0001-0000")["turns"], 2)
        self.assertIn("> First answer.", text)
        self.assertIn("> Second answer.", text)

    def test_round_four_fail_closed_and_screened_briefs(self):
        self.b(self.r, "new", "secretive", "--title", "Notes token: sk-" + "c" * 30, "--home", str(self.home))
        self.b(self.r, "history", "secretive")
        brief = self.b(self.r, "history", "secretive", "brief", "b001", "--runtime", "codex")
        self.assertNotIn("sk-cccc", brief)
        self.assertIn("read: <batch>/<slice>", brief)
        env = mock.patch.dict(os.environ, self.env, clear=True)
        env.start()
        self.addCleanup(env.stop)
        loader = core_loader.SourceFileLoader("brain_history_r4", str(CORE / "bin/brain"))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(mod)
        own = self.claude / "projects" / "-garden" / "11111111-aaaa.jsonl"
        roots = [self.claude.resolve()]
        with mock.patch.object(mod, "history_fd_path", lambda fd: None):
            self.assertEqual((None, "unreadable"), mod.history_open(str(own), roots, set()))
        inst = mod.Instance(self.r)
        n = inst.node("garden")
        out = self.tmp / "outside"
        out.mkdir()
        with mock.patch.object(mod, "history_fd_path", lambda fd: str(self.home)):
            with self.assertRaises(SystemExit):
                mod.history_open_dir(inst, n, out, "a folder")
        with mock.patch.object(mod, "history_fd_path", lambda fd: None):
            with self.assertRaises(SystemExit):
                mod.history_open_dir(inst, n, out, "a folder")
        os.close(mod.history_open_dir(inst, n, out, "a folder"))

    def test_history_needs_a_git_home_and_matches_remote_forms(self):
        self.b(self.r, "new", "plain", "--title", "Plain")
        self.assertIn("needs an external git home", self.b(self.r, "history", "plain", ok=False))
        self.b(self.r, "history", "garden", "plan", ok=False)
        loader = core_loader.SourceFileLoader("brain_history_test", str(CORE / "bin/brain"))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(mod)
        same = {mod.history_remote(u) for u in ("https://github.com/Acme/Garden.git", "git@github.com:acme/garden",
                                                "ssh://git@github.com:22/acme/garden.git",
                                                "https://user:pw@github.com/acme/garden/")}
        self.assertEqual(same, {"github.com/acme/garden"})
        self.assertEqual(mod.history_remote("/local/path/garden"), "")
        self.assertNotEqual(mod.history_remote("ssh://git@git.example:2222/team/secret.git"),
                            mod.history_remote("ssh://git@git.example:3333/team/secret.git"))
        self.assertEqual(mod.history_remote("ssh://git@git.example:22/Team/Secret.git"), "git.example/Team/Secret")
        self.assertNotEqual(mod.history_remote("https://git.example/Team/Secret"),
                            mod.history_remote("https://git.example/team/secret"))
        self.assertEqual(mod.history_split("x" * 9000, 1000)[0], "x" * 750)
        self.assertTrue(all(mod.history_tokens(p) <= 1000 for p in mod.history_split("é" * 5000 + "\nb\n", 1000)))

    def test_technical_headings_and_a_technical_reread(self):
        def finish_reading():
            out, m, _ = self.batches()
            (out / "notes").mkdir(exist_ok=True)
            for b in m["batches"]:
                done = "".join(f"read: {b['id']}/{sl['id']}\n" for sl in b["slices"])
                (out / "notes" / f"{b['id']}.md").write_text(
                    done + f"- Failures and fixes: the backup broke on locks [{b['slices'][0]['turns'][0]}]\n")
            return self.b(self.r, "history", "garden", "brief", "--final")
        self.b(self.r, "history", "garden")
        reader = self.b(self.r, "history", "garden", "brief", "b001")
        for heading in ("Decisions (and why)", "People and terms", "How it's built", "Failures and fixes",
                        "Code conventions and style", "Kinds of work"):
            self.assertIn(heading, reader)
        final = finish_reading()
        for wanted in ("- decisions.md:", "- knowledge.md:", "--kind <kind>", "`method`", "`style`", "`fact`",
                       "--keywords", f"brain specialist new <id> --at garden", "--proposed"):
            self.assertIn(wanted, final)
        self.assertIn("readers' notes exist", self.b(self.r, "history", "garden", "--technical", ok=False))
        self.assertIn("technical re-read", self.b(self.r, "history", "garden", "--technical", "--fresh"))
        reader = self.b(self.r, "history", "garden", "brief", "b001")
        self.assertIn("Failures and fixes", reader)
        self.assertNotIn("Decisions (and why)", reader)
        final = finish_reading()
        self.assertIn("technical re-read", final)
        self.assertIn("flagged conflict", final)
        self.assertNotIn("- decisions.md:", final)
        self.assertIn("- knowledge.md:", final)
        self.assertEqual(self.b(self.r, "history", "garden", "plan", "--json").count('"focus": "technical"'), 1)



class HistorySizes(History):
    cli_in_process = True
    fixture_templates = True
    # Both History.setUp and the extra instance below only construct fixtures;
    # history, containment and Git assertions still use their original paths.
    fixture_init_in_process = True

    def test_extraction_taken_back_when_its_folder_moves_into_the_home(self):
        from core_loader import SourceFileLoader
        self.b(self.r, "history", "garden")
        m = SourceFileLoader("brain_history_move", str(CORE / "bin/brain")).load_module()
        inst = m.Instance(self.r)
        n = inst.node("garden")
        wb = self.tmp / "garden-work"
        old = (wb / "scratch/history/manifest.json").read_text()
        check, calls = m.fd_outside, []

        def check_then_move(*args):  # the scratch folder moves into the home just after it was checked
            check(*args)
            calls.append(args[3])
            if calls.count("the workbench's scratch") == 1 and args[3] == "the workbench's scratch":
                os.rename(wb / "scratch", self.home / "moved")
        m.fd_outside = check_then_move
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.history_write(inst, n, wb, [("manifest.json", "{}"), ("batches/b001.md", "new\n")])
        moved = self.home / "moved"
        self.assertEqual((moved / "history/manifest.json").read_text(), old)  # the old extraction, back in place
        self.assertEqual([p.name for p in moved.iterdir() if p.name.startswith(".history")], [])
        self.assertIn("? moved/", self.git("status", "--porcelain", "--untracked-files=normal", cwd=self.home))
        self.assertNotIn("new", (moved / "history/manifest.json").read_text())
        os.rename(moved, wb / "scratch")
        calls.clear()

        def move_before_cleanup(*args):  # moved after the swap was checked: the old one is never deleted there
            check(*args)
            calls.append(args[3])
            if calls.count("the workbench's scratch") == 2 and args[3] == "the workbench's scratch":
                os.rename(wb / "scratch", self.home / "moved")
        m.fd_outside = move_before_cleanup
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.history_write(inst, n, wb, [("manifest.json", "{}"), ("batches/b001.md", "new\n")])
        self.assertTrue([p for p in (self.home / "moved").iterdir() if p.name.startswith(".history-old-")])

    def test_batches_moved_mid_extraction_get_no_more_and_nothing_is_published(self):
        from core_loader import SourceFileLoader
        self.b(self.r, "history", "garden")
        m = SourceFileLoader("brain_batches_move", str(CORE / "bin/brain")).load_module()
        inst = m.Instance(self.r)
        n = inst.node("garden")
        wb = self.tmp / "garden-work"
        old = (wb / "scratch/history/manifest.json").read_text()
        put, moved = m.history_put, self.home / "moved-batches"

        def put_then_move(fd, name, text):
            put(fd, name, text)
            if name == "b001.md":
                staged = next(p for p in (wb / "scratch").iterdir() if p.name.startswith(".history-"))
                os.rename(staged / "batches", moved)
        m.history_put = put_then_move
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.history_write(inst, n, wb, [("batches/b001.md", "one\n"), ("batches/b002.md", "two\n"),
                                          ("manifest.json", "{}")])
        self.assertEqual(sorted(p.name for p in moved.iterdir()), ["b001.md"])  # nothing more written there
        self.assertEqual((wb / "scratch/history/manifest.json").read_text(), old)  # the old one still published
        self.assertEqual([p.name for p in (wb / "scratch").iterdir() if p.name.startswith(".history-")], [])
        shutil.rmtree(moved)

        def put_last_then_move(fd, name, text):  # after the last file: only the staged tree's own check sees it
            put(fd, name, text)
            if name == "manifest.json":
                staged = next(p for p in (wb / "scratch").iterdir() if p.name.startswith(".history-"))
                os.rename(staged / "batches", moved)
        m.history_put = put_last_then_move
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.history_write(inst, n, wb, [("batches/b001.md", "one\n"), ("manifest.json", "{}")])
        self.assertEqual((wb / "scratch/history/manifest.json").read_text(), old)  # a tree without batches: not published

    def test_named_folders_add_sessions_that_worked_on_the_node_from_elsewhere(self):
        scratch = self.tmp / "scratch-thread"
        scratch.mkdir()
        self.jsonl(self.claude / "projects" / "-scratch-thread" / "55555555-eeee.jsonl",
                   [{**r, "message": {"role": "user", "content": "SCRATCH-WORK on the garden."}}
                    if r.get("type") == "user" else r for r in self.claude_rows(scratch, "55555555-eeee", start=14)[:3]])
        self.b(self.r, "history", "garden")
        _, m, text = self.batches()
        self.assertNotIn("55555555-eeee", [x["id"] for x in m["sessions"]])
        out = self.b(self.r, "history", "garden", "--fresh", "--also", str(scratch))
        self.assertIn("also sessions run in 1 folder named as this node's (--also): 1 of them", out)
        _, m, text = self.batches()
        self.assertEqual([x["match"] for x in m["sessions"] if x["id"] == "55555555-eeee"], ["named folder"])
        self.assertIn("SCRATCH-WORK", text)
        count = lambda out: int(re.search(r"garden: (\d+) sessions", out)[1])  # noqa: E731
        self.assertEqual(count(self.b(self.r, "spend", "garden", "--also", str(scratch))),
                         count(self.b(self.r, "spend", "garden")) + 1)  # spend looks back over the same sessions
        self.b(self.r, "new", "other", "--title", "Other", "--home", str(self.repo(self.tmp / "OtherHome")))
        second = self.init("bob")  # another brain on this machine, with a project of its own
        self.b(second, "new", "theirs", "--title", "Theirs", "--home", str(self.repo(self.tmp / "theirs")))
        loose = self.tmp / "loose-brain"  # a brain nobody registered
        (loose / "nodes/x").mkdir(parents=True)
        for f in ("instance.md", "node.md"):
            (loose / f).write_text("x\n")
        (self.tmp / "holder/deep/inner").mkdir(parents=True)
        for f in ("instance.md", "node.md"):
            (self.tmp / "holder/deep/inner" / f).write_text("x\n")
        home_folder, brain = "your home folder or a brain", "your home folder or a brain"
        for bad, why in ((str(self.tmp.parent), home_folder), (str(self.tmp).upper(), home_folder),
                         (str(self.r / "nodes"), brain), (str(self.r).upper(), brain),
                         (str(loose), brain), (str(loose / "nodes/x"), brain), (str(self.tmp / "holder"), brain),
                         ("relative/path", "absolute"),
                         (str(self.tmp / "OtherHome" / "src"), "another project's home"),
                         (str(self.tmp / "otherhome"), "another project's home"),
                         (str(self.tmp / "theirs"), "another project's home")):
            self.assertIn(why, self.b(self.r, "history", "garden", "--fresh", "--also", bad, ok=False), bad)
        out = self.b(self.r, "history", "garden", "--fresh", "--also", "relative/Zebracorp", ok=False)
        self.assertNotIn("Zebracorp", out)  # a refusal never prints a never-name word
        loop = self.tmp / "Zebracorp-loop"
        loop.symlink_to(loop)
        out = self.b(self.r, "history", "garden", "--fresh", "--also", str(loop), ok=False)
        self.assertNotIn("Zebracorp", out)
        self.assertNotIn("Traceback", out)
        # Inside a named folder, only sessions outside every brain and every other repository count.
        clone = scratch / "their-clone"
        self.git("clone", "-q", str(self.tmp / "theirs"), str(clone), cwd=self.tmp)  # another project's clone
        deep = scratch / "a/b/c/d/e"  # a brain nobody registered, deeper than a folder check looks
        (deep / "nodes/n").mkdir(parents=True)
        for f in ("instance.md", "node.md"):
            (deep / f).write_text("x\n")
        for cwd, sid in ((clone, "66666666-ffff"), (deep / "nodes/n", "77777777-aaaa")):
            self.jsonl(self.claude / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / f"{sid}.jsonl",
                       [{**r, "message": {"role": "user", "content": f"FOREIGN-{sid}"}} if r.get("type") == "user" else r
                        for r in self.claude_rows(cwd, sid, start=15)[:3]])
        # A refused session isn't let back in by the remote it recorded; broken git metadata is never "no repo".
        s = self.codex / "sessions" / "2026" / "10" / "04"
        self.jsonl(s / "rollout-2026-10-04T09-00-00-bbbb0001.jsonl", self.codex_rows(
            "bbbb0001-0000", deep / "nodes/n", [self.item("UserMessage", "FOREIGN-remote", id="u-r")],
            repo="https://github.com/Acme/Garden.git", at0="2026-10-04T09:00:00Z"))
        dangling = scratch / "dangling"
        dangling.mkdir()
        (dangling / ".git").write_text("gitdir: /nowhere/at/all\n")
        self.jsonl(self.claude / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(dangling)) / "88888888-bbbb.jsonl",
                   [{**r, "message": {"role": "user", "content": "FOREIGN-dangling"}} if r.get("type") == "user" else r
                    for r in self.claude_rows(dangling, "88888888-bbbb", start=16)[:3]])
        bare = scratch / "their-bare.git"  # a bare clone has no .git entry
        self.git("clone", "-q", "--bare", str(self.tmp / "theirs"), str(bare), cwd=self.tmp)
        broken_bare = scratch / "broken-bare"
        (broken_bare / "objects").mkdir(parents=True)
        (broken_bare / "refs").mkdir()
        (broken_bare / "HEAD").write_text("garbage\n")  # git can't read it: still someone's repository
        for cwd, sid in ((bare, "99999999-cccc"), (broken_bare, "99999999-dddd")):
            self.jsonl(self.claude / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / f"{sid}.jsonl",
                       [{**r, "message": {"role": "user", "content": f"FOREIGN-{sid}"}} if r.get("type") == "user" else r
                        for r in self.claude_rows(cwd, sid, start=17)[:3]])
        out = self.b(self.r, "history", "garden", "--fresh", "--also", "~Zebracorp_nobody/x", ok=False)
        self.assertNotIn("Zebracorp", out)
        self.assertNotIn("Traceback", out)
        # A parent in a named folder never vouches for a sub-agent that ran elsewhere.
        base = count(self.b(self.r, "spend", "garden", "--also", str(scratch)))
        parent_dir = self.claude / "projects" / "-scratch-thread" / "55555555-eeee" / "subagents"
        self.jsonl(parent_dir / "agent-foreign.jsonl", [{**r, "isSidechain": True}
                                                       for r in self.claude_rows(clone, "55555555-eeee", start=18)[:3]])
        self.jsonl(parent_dir / "agent-brain.jsonl", [{**r, "isSidechain": True}
                                                     for r in self.claude_rows(self.r / "nodes", "55555555-eeee", start=18)[:3]])
        self.jsonl(parent_dir / "agent-ok.jsonl", [{**r, "isSidechain": True}
                                                  for r in self.claude_rows(scratch, "55555555-eeee", start=18)[:3]])
        s2 = self.codex / "sessions" / "2026" / "10" / "05"
        self.jsonl(s2 / "rollout-2026-10-05T09-00-00-cccc0001.jsonl", self.codex_rows(
            "cccc0001-0000", scratch, [self.item("UserMessage", "parent", id="u-p")], at0="2026-10-05T09:00:00Z"))
        self.jsonl(s2 / "rollout-2026-10-05T09-10-00-cccc0002.jsonl", self.codex_rows(
            "cccc0002-0000", clone, [self.item("UserMessage", "child", id="u-c")], at0="2026-10-05T09:10:00Z",
            source={"subagent": {"thread_spawn": {"parent_thread_id": "cccc0001-0000"}}}))
        after = count(self.b(self.r, "spend", "garden", "--also", str(scratch)))
        self.assertEqual(after, base + 2)  # the scratch sub-agent and the Codex parent; not the foreign children
        damaged = scratch / "damaged-bare"  # below the named folder: never counted, whatever it is
        self.git("clone", "-q", "--bare", str(self.tmp / "theirs"), str(damaged), cwd=self.tmp)
        os.rename(damaged / "HEAD", damaged / "HEAD-gone")
        plain = scratch / "plain-subfolder"
        plain.mkdir()
        for cwd, sid in ((damaged, "aaaaaaaa-1111"), (plain, "aaaaaaaa-2222")):
            self.jsonl(self.claude / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / f"{sid}.jsonl",
                       [{**r, "message": {"role": "user", "content": f"FOREIGN-{sid}"}} if r.get("type") == "user" else r
                        for r in self.claude_rows(cwd, sid, start=19)[:3]])
        self.assertEqual(count(self.b(self.r, "spend", "garden", "--also", str(scratch))), after)
        # Naming a damaged foreign bare repository itself: still a repository.
        self.jsonl(self.claude / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(damaged)) / "aaaaaaaa-3333.jsonl",
                   [{**r, "message": {"role": "user", "content": "FOREIGN-named-bare"}} if r.get("type") == "user" else r
                    for r in self.claude_rows(damaged, "aaaaaaaa-3333", start=20)[:3]])
        self.assertEqual(count(self.b(self.r, "spend", "garden", "--also", str(scratch), "--also", str(damaged))), after)
        # A folder below a damaged bare repository, named directly: still inside a repository.
        (damaged / "maintenance").mkdir()
        self.jsonl(self.claude / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(damaged / "maintenance")) /
                   "aaaaaaaa-4444.jsonl", self.claude_rows(damaged / "maintenance", "aaaaaaaa-4444", start=21)[:3])
        self.assertEqual(count(self.b(self.r, "spend", "garden", "--also", str(scratch), "--also",
                                      str(damaged / "maintenance"))), after)
        # A readable foreign repository isn't let in by the remote a session recorded there.
        s4 = self.codex / "sessions" / "2026" / "10" / "07"
        self.jsonl(s4 / "rollout-2026-10-07T09-00-00-eeee0001.jsonl", self.codex_rows(
            "eeee0001-0000", clone, [self.item("UserMessage", "FOREIGN-recorded", id="u-f")],
            repo="https://github.com/Acme/Garden.git", at0="2026-10-07T09:00:00Z"))
        self.assertEqual(count(self.b(self.r, "spend", "garden", "--also", str(scratch))), after)
        # Nor a damaged one (git can't read it), nor a folder below one, whatever remote their sessions recorded.
        for cwd, sid in ((damaged, "ffff0001"), (damaged / "maintenance", "ffff0002")):
            self.jsonl(s4 / f"rollout-2026-10-07T10-00-00-{sid}-0000.jsonl", self.codex_rows(
                f"{sid}-0000", cwd, [self.item("UserMessage", f"FOREIGN-{sid}", id=f"u-{sid}")],
                repo="https://github.com/Acme/Garden.git", at0="2026-10-07T10:00:00Z"))
        self.assertEqual(count(self.b(self.r, "spend", "garden", "--also", str(scratch))), after)
        # A session whose folder is gone, inside a brain that's still there, matches nothing by its remote.
        s3 = self.codex / "sessions" / "2026" / "10" / "06"
        self.jsonl(s3 / "rollout-2026-10-06T09-00-00-dddd0001.jsonl", self.codex_rows(
            "dddd0001-0000", self.r / "nodes" / "gone-folder", [self.item("UserMessage", "FOREIGN-gone", id="u-g")],
            repo="https://github.com/Acme/Garden.git", at0="2026-10-06T09:00:00Z"))
        self.assertEqual(count(self.b(self.r, "spend", "garden", "--also", str(scratch))), after)
        self.b(self.r, "history", "garden", "--fresh", "--also", str(scratch))
        _, m, text = self.batches()
        self.assertNotIn("FOREIGN-", text)
        self.assertEqual(sorted(x["id"] for x in m["sessions"] if x["match"] == "named folder"),
                         ["55555555-eeee", "cccc0001-0000"])

    def test_sessions_of_a_deleted_checkout_with_a_newline_still_match(self):
        from core_loader import SourceFileLoader
        odd = self.tmp / "line\nbreak"
        self.git("worktree", "add", "-q", "-b", "odd", str(odd), cwd=self.home)
        shutil.rmtree(odd)
        m = SourceFileLoader("brain_history_odd", str(CORE / "bin/brain")).load_module()
        inst = m.Instance(self.r)
        self.assertTrue(m.history_matcher(inst, inst.node("garden"))[0](str(odd / "src"), ""))

    def test_batches_fit_the_runtime_that_compacts_soonest_unless_given(self):
        s = self.codex / "sessions" / "2026" / "10" / "03"
        self.jsonl(s / "rollout-2026-10-03T09-00-00-aaaa0009.jsonl", self.codex_rows("aaaa0009-0000", self.home, [
            self.item("UserMessage", "Small window here.", id="u-small"),
            ("event_msg", {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 130000}}}),
            ("compacted", {"message": "x"}),
            self.item("AgentMessage", "Compacted.", phase="final_answer")], at0="2026-10-03T09:00:00Z"))
        summary = self.b(self.r, "history", "garden")
        m = json.loads((self.tmp / "garden-work/scratch/history/manifest.json").read_text())
        self.assertEqual(m["compactions"]["codex"][0], 130000)
        self.assertEqual((m["batch_tokens"], m["slice_tokens"], m["sized_by"]), (35000, 20000, "codex"))
        self.assertIn("sized so a codex reader holds one before it compacts", summary)
        self.assertIn("codex compacted as early as 130k here (", summary)
        self.assertNotIn("holds 0 batch", summary)
        self.assertTrue(all(b["tokens"] <= 35000 for b in m["batches"]))
        self.b(self.r, "history", "garden", "--fresh", "--batch-tokens", "60000")
        m = json.loads((self.tmp / "garden-work/scratch/history/manifest.json").read_text())
        self.assertEqual((m["batch_tokens"], m["sized_by"]), (60000, None))  # given: kept, and the summary warns
        self.assertIn("holds 0 batch", self.b(self.r, "history", "garden", "plan"))
        self.jsonl(s / "rollout-2026-10-03T10-00-00-aaaa0010.jsonl", self.codex_rows("aaaa0010-0000", self.home, [
            self.item("UserMessage", "Tiny window.", id="u-tiny"),
            ("event_msg", {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 62000}}}),
            ("compacted", {"message": "x"})], at0="2026-10-03T10:00:00Z"))
        self.assertIn("pass --batch-tokens", self.b(self.r, "history", "garden", "--fresh", ok=False))
        self.b(self.r, "history", "garden", "--fresh", "--batch-tokens", "8000", "--slice-tokens", "4000")



if __name__ == "__main__":
    unittest.main()
