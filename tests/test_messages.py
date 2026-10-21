"""Made-up worker sends: advisory classification, privacy and formatter controls."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
from unittest import mock

import core_loader
from scenario import Scenario, CORE


class WorkerMessages(Scenario):
    def core(self):
        return core_loader.SourceFileLoader("brain_messages", str(CORE / "bin/brain")).load_module()

    def test_formatter_and_briefs(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "a", "--title", "App", "--home", str(home))
        self.b(r, "wt", "add", "a", "job/sample")
        for kind in ("start", "done", "blocked", "question"):
            out = self.b(r, "msg", "a", kind, "One\nline", "--branch", "job/sample", "--report", "~/reports/example.md")
            self.assertEqual(out.strip(), kind + ": One line [branch: job/sample; report: ~/reports/example.md]")
        out = self.b(r, "msg", "a", "question", "What next?", "--branch", "job/sample")
        self.assertIn("job-handbacks/", out)
        self.b(r, "msg", "a", "progress", "Working", "--branch", "job/sample", ok=False)
        self.b(r, "msg", "a", "start", "Working", "--branch", "missing", ok=False)
        for args in (("--branch", "job/sample"), ("--small",), ()):
            brief = self.b(r, "brief", "a", *args, "--task", "Build a sample.")
            protocol = [line for line in brief.splitlines() if "Send the coordinator only" in line]
            self.assertEqual(len(protocol), 1)
            self.assertIn("peer thread", protocol[0])
            self.assertNotIn("done|failed|blocked", brief)
        self.assertIn("start/done/blocked/question", self.b(r, "handback", "--help"))

    def test_handback_formats_failure_and_question_with_report(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "a", "--title", "App", "--home", str(home))
        self.b(r, "wt", "add", "a", "job/sample")
        for state, kind in (("blocked", "blocked"), ("question", "question"), ("failed", "blocked")):
            out = self.b(r, "handback", "a", "--branch", "job/sample", "--state", state,
                         "--report", "~/reports/sample.md", "--lessons", "none: made-up fixture")
            self.assertIn(kind + ": coordinator can see it with brain due [branch: job/sample; report: ~/reports/sample.md]", out)

    def test_made_up_transcripts_warn_once_and_do_not_save_text(self):
        module = self.core()
        n = {"local": {"coordinator": "thread-parent", "worktrees": {"sample": {"path": str(self.tmp)}}}}
        transcript = self.tmp / "transcript.jsonl"
        data = {"transcript_path": str(transcript), "session_id": "made-up-session"}
        variants = []
        for runtime in ("claude", "codex", "code-mode", "code-mode-json"):
            rows = []
            for kind in ("start", "done", "blocked", "question", "progress"):
                args = {"threadId": "thread-parent", "message": kind + ": synthetic private detail; extra } text"}
                if runtime == "claude":
                    rec = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__t3_code__t3_thread_send", "input": args}]}}
                elif runtime == "codex":
                    rec = {"type": "response_item", "payload": {"type": "function_call", "name": "mcp__t3_code__t3_thread_send", "arguments": json.dumps(args)}}
                elif runtime == "code-mode-json":
                    rec = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "functions.exec", "input": "await tools.mcp__t3_code__t3_thread_send(" + json.dumps(args) + ");"}}
                else:
                    rec = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "functions.exec", "input": 'await tools.mcp__t3_code__t3_thread_send({threadId: "thread-parent", message: ' + json.dumps(args['message']) + '});'}}
                rows.append(rec)
            variants.append(rows)
        for index, rows in enumerate(variants):
            with self.subTest(runtime=index):
                data["session_id"] = "made-up-" + str(index)
                transcript.write_text("\n".join(map(json.dumps, rows)))
                out = io.StringIO()
                with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}), contextlib.redirect_stderr(out):
                    module.warn_worker_messages(None, n, data, self.tmp)
                    module.warn_worker_messages(None, n, data, self.tmp)
                self.assertEqual(out.getvalue().count("Worker message warning:"), 1)
                self.assertNotIn("synthetic private detail", out.getvalue())
        state = self.tmp / ".local/state/brain/message-warnings"
        self.assertEqual(len(list(state.iterdir())), 4)
        self.assertTrue(all(p.read_bytes() == b"" for p in state.iterdir()))
        # Negative controls: valid sends, peers and non-worker folders produce no warning/state.
        for args, cwd in (({"threadId": "thread-peer", "message": "progress: sample"}, self.tmp),
                          ({"threadId": "thread-parent", "message": "question: sample"}, self.tmp),
                          ({"threadId": "thread-parent", "message": "progress: sample"}, self.tmp.parent / (self.tmp.name + "-elsewhere"))):
            transcript.write_text(json.dumps({"type": "function_call", "name": "t3_thread_send", "arguments": args}))
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}), contextlib.redirect_stderr(out):
                module.warn_worker_messages(None, n, data, cwd)
            self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(list(state.iterdir())), 4)

    def test_stop_hook_warning_is_advisory_on_both_runtimes(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "a", "--title", "App", "--home", str(home))
        self.b(r, "set", "a", "coordinator=thread-parent")
        self.b(r, "wt", "add", "a", "job/sample")
        # Worktree folder encoding is supplied by the CLI.
        from glob import glob
        wt = Path(glob(str(self.tmp / "app-work/worktrees/*"))[0])
        transcript = self.tmp / "synthetic.jsonl"
        transcript.write_text(json.dumps({"type": "function_call", "name": "t3_thread_send", "arguments": {
            "threadId": "thread-parent", "message": "progress: sample"}}))
        import subprocess
        self.env["PATH"] = str(CORE / "bin") + os.pathsep + self.env["PATH"]
        for runtime in ("claude", "codex"):
            command = json.loads((CORE / "adapters/runtimes" / (runtime + "-hooks.json")).read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
            for repeat in range(2):
                result = subprocess.run(["sh", "-c", command], cwd=wt, env=self.env, capture_output=True, text=True,
                                        input=json.dumps({"cwd": str(wt), "session_id": runtime, "transcript_path": str(transcript)}), timeout=10)
                self.assertEqual((result.returncode, result.stdout), (0, ""))
                self.assertEqual(result.stderr.count("Worker message warning:"), int(repeat == 0))

    def test_warning_failure_never_suppresses_question_capture(self):
        module = self.core()
        r = self.init("alice")
        with mock.patch.object(module, "warn_worker_messages", side_effect=RuntimeError("synthetic failure")), \
             mock.patch.object(module, "capture_hook_questions") as capture, \
             mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps({"cwd": str(r), "stop_hook_active": True}))):
            module.hook_stop(argparse.Namespace(event="stop"))
        capture.assert_called_once()
