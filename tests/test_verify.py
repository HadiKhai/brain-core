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


class WorkerReadyPackets(Scenario):
    def job(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app", {"calc.py": "value = 1\n",
                         "verify.py": "from calc import value\nassert value == 2\n"})
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        self.b(r, "set", "a", "verify=python3 -B verify.py")
        self.b(r, "wt", "add", "a", "job")
        wt = self.tmp / "app-work/worktrees/job"
        (wt / "calc.py").write_text("value = 2\n")
        self.git("commit", "-qam", "fix", cwd=wt)
        return r, home, wt

    def record(self, r, ok=True):
        return self.b(r, "ready", "a", "--branch", "job", "--record", "--reviews", "2", ok=ok)

    def packet(self):
        return next((self.tmp / "alice/nodes/a/job-handbacks").glob("*.json"))

    def test_packet_records_executed_verify_and_checker_does_not_rerun(self):
        r, home, wt = self.job()
        self.assertIn("packet missing", self.b(r, "ready", "a", "--branch", "job", ok=False))
        before = self.git("rev-parse", "HEAD", cwd=r)
        self.assertEqual(self.record(r).strip().splitlines()[-1], "ready")
        p = json.loads(self.packet().read_text())["ready"]
        self.assertEqual(p["head"], self.git("rev-parse", "job", cwd=home).strip())
        self.assertEqual(p["main"], self.git("rev-parse", "main", cwd=home).strip())
        self.assertEqual((p["verify_command"], p["verify_result"], p["review_rounds"]), ("python3 -B verify.py", 0, 2))
        self.assertNotEqual(self.git("rev-parse", "HEAD", cwd=r), before)
        # A checker must not execute the command: make the executable unavailable in its PATH.
        original = self.env["PATH"]
        tools = self.tmp / "tools"
        tools.mkdir()
        (tools / "git").symlink_to(shutil.which("git"))
        self.env["PATH"] = str(tools)
        try:
            self.assertEqual(self.b(r, "ready", "a", "--branch", "job").strip(), "ready")
        finally:
            self.env["PATH"] = original
        state = json.loads(self.b(r, "ui", "--json"))
        node = next(n for n in state["nodes"] if n["id"] == "a")
        self.assertTrue(node["worktrees"][0]["ready"]["ready"])

    def test_main_move_and_head_change_require_worker_recheck(self):
        r, home, wt = self.job()
        self.record(r)
        (home / "other.txt").write_text("new main\n")
        self.git("add", "other.txt", cwd=home)
        self.git("commit", "-qm", "main moves", cwd=home)
        self.assertIn("main moved", self.b(r, "ready", "a", "--branch", "job", ok=False))
        self.assertIn("rebase on current main", self.record(r, ok=False))
        self.git("rebase", "main", cwd=wt)
        self.assertEqual(self.record(r).strip().splitlines()[-1], "ready")
        self.git("commit", "--allow-empty", "-qm", "amend job", cwd=wt)
        self.assertIn("branch head changed", self.b(r, "ready", "a", "--branch", "job", ok=False))

    def test_failed_verify_dirty_tree_missing_config_and_reviews(self):
        r, home, wt = self.job()
        (wt / "calc.py").write_text("value = 3\n")
        self.assertIn("dirty", self.record(r, ok=False))
        self.git("commit", "-qam", "wrong", cwd=wt)
        self.assertIn("verify missing, changed or failed", self.record(r, ok=False))
        self.assertNotEqual(json.loads(self.packet().read_text())["ready"]["verify_result"], 0)
        self.b(r, "set", "a", "verify=")
        self.assertIn("no verify command", self.record(r, ok=False))
        self.b(r, "set", "a", "verify=true")
        self.assertIn("requires --reviews", self.b(r, "ready", "a", "--branch", "job", "--record", ok=False))
        self.assertIn("at least one", self.b(r, "ready", "a", "--branch", "job", "--record", "--reviews", "0", ok=False))

    def test_conflict_and_mutation_during_verify_cannot_be_ready(self):
        r, home, wt = self.job()
        (home / "calc.py").write_text("value = 9\n")
        self.git("commit", "-qam", "conflict", cwd=home)
        self.assertIn("merge-tree is not clean", self.record(r, ok=False))
        self.git("rebase", "--onto", "job", "main", "main", cwd=home)
        self.b(r, "set", "a", "verify=git commit --allow-empty -qm changed-during-verify")
        self.assertIn("verify changed the job's branch", self.record(r, ok=False))

    def test_malformed_or_incomplete_packets_fail_closed(self):
        r, home, wt = self.job()
        self.record(r)
        path = self.packet()
        record = json.loads(path.read_text())
        original = record["ready"]
        for data in ([1], {}, {**original, "review_rounds": 0}, {**original, "verify_result": "0"},
                     {**original, "checked_at": {}}, {**original, "node": "other"}):
            path.write_text(json.dumps({**record, "ready": data}))
            self.assertIn("not ready", self.b(r, "ready", "a", "--branch", "job", ok=False))
        path.write_text("{")
        self.assertIn("unreadable", self.b(r, "ready", "a", "--branch", "job", ok=False))



class ReadyFailureRegressions(Scenario):
    job = WorkerReadyPackets.job
    record = WorkerReadyPackets.record
    packet = WorkerReadyPackets.packet

    def merge_tree_exit(self, code):
        tools = self.tmp / "tools"
        tools.mkdir(exist_ok=True)
        script = tools / "git"
        script.write_text(f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
                          "if 'merge-tree' in sys.argv and '--write-tree' in sys.argv:\n"
                          f"    Path({str(self.tmp / 'merge-called')!r}).touch()\n"
                          f"    sys.exit({code})\n"
                          f"os.execv({shutil.which('git')!r}, ['git', *sys.argv[1:]])\n")
        script.chmod(0o755)
        self.env["PATH"] = str(tools) + os.pathsep + os.environ.get("PATH", "")

    def test_fast_forward_ready_does_not_require_modern_merge_tree(self):
        r, home, wt = self.job()
        self.merge_tree_exit(129)
        self.assertEqual(self.record(r).strip().splitlines()[-1], "ready")
        self.assertEqual(self.b(r, "ready", "a", "--branch", "job").strip(), "ready")
        self.assertFalse((self.tmp / "merge-called").exists())

    def test_ready_uses_local_main_without_updating_remote_refs(self):
        r, home, wt = self.job()
        local_main = self.git("rev-parse", "main", cwd=home).strip()
        self.git("checkout", "-qb", "remote-tip", cwd=home)
        self.git("commit", "--allow-empty", "-qm", "remote ahead", cwd=home)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=home)
        remote_main = self.git("rev-parse", "HEAD", cwd=home).strip()
        self.git("checkout", "-q", "main", cwd=home)
        self.assertEqual(self.record(r).strip().splitlines()[-1], "ready")
        self.assertEqual(json.loads(self.packet().read_text())["ready"]["main"], local_main)
        self.assertNotEqual(local_main, remote_main)
        self.assertEqual(self.git("rev-parse", "origin/main", cwd=home).strip(), remote_main)
        skill = (CORE / "skills/home-job/SKILL.md").read_text()
        self.assertIn("cached `origin/HEAD` when origin exists, otherwise local `main`", skill)
        self.assertIn("Neither fetches", skill)

    def test_merge_tree_exit_codes_distinguish_conflict_old_git_and_error(self):
        r, home, wt = self.job()
        self.git("commit", "--allow-empty", "-qm", "main moves", cwd=home)
        for code, expected in ((129, "git is too old"), (128, "merge cleanliness is unknown"),
                               (1, "merge-tree is not clean")):
            self.merge_tree_exit(code)
            out = self.record(r, ok=False)
            self.assertIn("rebase on current main", out)
            self.assertIn(expected, out)
            if code != 1:
                self.assertNotIn("merge-tree is not clean", out)

    def test_failed_verify_prints_bounded_redacted_stdout_and_stderr(self):
        r, home, wt = self.job()
        (r / "nodes/a/never-name.txt").write_text("PrivateName\n")
        self.b(r, "set", "a", "verify=python3 -B fail.py")
        (wt / "fail.py").write_text("import sys\nprint('dropped from tail', flush=True)\n"
                                    "for i in range(20): print(f'context {i}', flush=True)\n"
                                    "print('PrivateName useful failure', file=sys.stderr)\nsys.exit(7)\n")
        self.git("add", "fail.py", cwd=wt)
        self.git("commit", "-qm", "failing verify", cwd=wt)
        out = self.record(r, ok=False)
        self.assertIn("verify failed (exit 7)", out)
        self.assertIn("context 19", out)
        self.assertIn("█" * len("PrivateName") + " useful failure", out)
        self.assertNotIn("PrivateName", out)
        self.assertNotIn("dropped from tail", out)
        self.assertNotIn("context 0\n", out)
        self.assertEqual(json.loads(self.packet().read_text())["ready"]["verify_result"], 7)



class VerifyLogs(Scenario):
    job = WorkerReadyPackets.job

    def runner(self):
        r, home, wt = self.job()
        m = core_loader.SourceFileLoader("brain_verify_logs", str(CORE / "bin/brain")).load_module()
        inst = m.Instance(r)
        n = inst.node("a")
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        return m, inst, n, home, wt

    def command(self, code):
        return shlex.quote(sys.executable) + " -B -c " + shlex.quote(code)

    def run_verify(self, setup, code):
        m, inst, n, home, wt = setup
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result, path = m.run_verify(inst, n, "job", self.command(code), {"path": str(wt)}, self.env)
        return result, Path(path), output.getvalue()

    def test_full_private_failure_log_and_each_lane_tail(self):
        setup = self.runner()
        result, path, output = self.run_verify(setup,
            "import sys\nprint('[lane alpha] unique-early-detail')\n"
            "print('[lane beta] separate-detail')\nprint('[lane alpha] FAIL first')\n"
            "print('[lane beta] exit 7')\n"
            "for i in range(1000): print('later-success', i)\n"
            "print('stderr-detail', file=sys.stderr)\nsys.exit(7)")
        text = path.read_text()
        self.assertEqual(result, 7)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent, setup[2]["dir"] / "job-handbacks")
        self.assertIn(str(path), output)
        self.assertEqual(len(text.splitlines()), 1005)
        self.assertIn('unique-early-detail', text)
        self.assertIn('later-success 999', text)
        self.assertIn('stderr-detail', text)
        self.assertIn('verify failing lane: alpha', output)
        self.assertIn('verify failing lane: beta', output)
        self.assertIn('unique-early-detail', output)
        self.assertIn('separate-detail', output)
        self.assertNotIn('later-success 1\n', output)

    def test_redaction_at_every_split_and_casefold_boundary(self):
        m, inst, n, home, wt = setup = self.runner()
        terms = [(x, 'fixture') for x in ('secretword', 'Straße', 'across\nlines', 'αβ')]
        raw = 'start SECRETWORD Straße STRASSE across\nlines αβ end\n'
        expected = None
        for split in range(len(raw) + 1):
            scrubber = m.VerifyScrubber(terms)
            text = scrubber.feed(raw[:split]) + scrubber.feed(raw[split:], final=True)
            for word, _ in terms:
                self.assertNotIn(word.casefold(), text.casefold())
            self.assertTrue(text.startswith('start '))
            self.assertTrue(text.endswith(' end\n'))
            expected = expected or text
            self.assertEqual(text, expected)
        (n['dir'] / 'never-name.txt').write_text('secretword\nStraße\n')
        result, path, output = self.run_verify(setup,
            "import os,sys,time\nos.write(1, b'x'*8190 + b'SECRET')\n"
            "time.sleep(.03)\nos.write(2, 'WORD STRASSE Straße\\n'.encode())\nsys.exit(1)")
        for text in (path.read_text(), output):
            self.assertNotIn('secretword', text.casefold())
            self.assertNotIn('strasse', text.casefold())
        self.assertTrue(path.read_text().startswith('x'*8190))

    def test_streaming_and_progress_before_exit_even_without_newline(self):
        setup = self.runner()
        m, inst, n, home, wt = setup
        m.VERIFY_PROGRESS_SECONDS = .1
        release = self.tmp / 'release'
        output = io.StringIO()
        result, errors = [], []
        code = ("import os,time\nfrom pathlib import Path\nos.write(1,b'partial-output')\n"
                f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n")

        def run():
            try:
                result.append(m.run_verify(inst, n, 'job', self.command(code), {'path': str(wt)}, self.env))
            except BaseException as error:
                errors.append(error)
        with contextlib.redirect_stdout(output):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                deadline = time.monotonic() + 5
                while 'characters logged' not in output.getvalue() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(worker.is_alive())
                self.assertIn('verify: running', output.getvalue())
                logs = list((n['dir'] / 'job-handbacks').glob('*.log'))
                self.assertEqual(len(logs), 1)
                self.assertEqual(logs[0].read_text(), 'partial-output')
            finally:
                release.touch()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0][0], 0)

    def test_log_retention_is_scoped_and_does_not_follow_links(self):
        setup = self.runner()
        paths = [self.run_verify(setup, 'print("fixture")')[1] for _ in range(7)]
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertTrue(all(p.exists() for p in paths[2:]))
        folder = paths[-1].parent
        untouched = folder / 'other-job-verify-1-0123456789ab.log'
        untouched.write_text('other job')
        self.run_verify(setup, 'pass')
        self.assertEqual(untouched.read_text(), 'other job')
        key = paths[-1].name.split('-verify-')[0]
        for i in range(5):
            (folder / (key + '-verify-' + '9'*25 + f'-{i:012x}.log')).write_text('future clock fixture')
        latest = self.run_verify(setup, 'print("current clock fixture")')[1]
        self.assertEqual(latest.read_text(), 'current clock fixture\n')
        self.assertEqual(len(list(folder.glob(key + '-verify-*.log'))), 5)
        for p in folder.iterdir():
            p.unlink()
        folder.rmdir()
        folder.symlink_to(setup[3], target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, "link or not a folder"):
            self.run_verify(setup, 'pass')
        self.assertFalse(list(setup[3].glob('*.log')))

    def test_stdin_environment_exit_and_overall_fallback_through_handback(self):
        r, home, wt = self.job()
        code = ("import os,sys\nassert input() == 'fixture-input'\n"
                "assert os.environ['PORT'] == '4100'\nassert os.environ['BRANCH'] == 'job'\n"
                "assert os.environ['MAIN'] == " + repr(str(home)) + "\n"
                "assert os.environ['WORKTREE'] == " + repr(str(wt)) + "\n"
                "assert os.environ['NODE'] == 'a'\nprint('plain failure detail')\nsys.exit(9)")
        (wt / 'stdin-check.py').write_text(code)
        self.git('add', 'stdin-check.py', cwd=wt)
        self.git('commit', '-qm', 'stdin fixture', cwd=wt)
        self.b(r, 'set', 'a', 'verify=python3 -B stdin-check.py')
        result = subprocess.run([sys.executable, str(r / 'core/bin/brain'), 'handback', 'a', '--branch', 'job',
                                 '--state', 'done', '--reviews', '1'], cwd=r, env=self.env,
                                input='fixture-input\n', capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('verify failed (exit 9)', result.stdout)
        self.assertIn('plain failure detail', result.stdout)
        packet = json.loads(next((r / 'nodes/a/job-handbacks').glob('*.json')).read_text())['ready']
        self.assertEqual(packet['verify_result'], 9)
        self.assertEqual(Path(packet['verify_log']).read_text(), 'plain failure detail\n')
        self.assertEqual(self.git('status', '--porcelain', cwd=r), '')
        self.assertNotIn('.log', self.git('ls-files', 'nodes/a/job-handbacks', cwd=r))

    def test_split_utf8_and_progress_after_command_closes_output(self):
        m, inst, n, home, wt = self.runner()
        m.VERIFY_PROGRESS_SECONDS = .1
        release = self.tmp / 'release-closed'
        closed = self.tmp / 'output-closed'
        output, results, errors = io.StringIO(), [], []
        code = ("import os,time\nfrom pathlib import Path\nos.write(1,b'caf\\xc3')\n"
                "time.sleep(.3)\nos.write(2,b'\\xa9')\nos.close(1)\nos.close(2)\n"
                f"Path({str(closed)!r}).touch()\n"
                f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n")

        def run():
            try:
                results.append(m.run_verify(inst, n, 'job', self.command(code), {'path': str(wt)}, self.env))
            except BaseException as error:
                errors.append(error)
        with contextlib.redirect_stdout(output):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                deadline = time.monotonic() + 5
                while not closed.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(closed.exists())
                # A startup heartbeat can occur between the split UTF-8 writes. Require
                # a later heartbeat, after the child's explicit output-closed barrier.
                previous = output.getvalue().count('characters logged')
                while output.getvalue().count('characters logged') <= previous and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertGreater(output.getvalue().count('characters logged'), previous)
                self.assertTrue(worker.is_alive())
                self.assertIn('verify: running', output.getvalue())
                log = next((n['dir'] / 'job-handbacks').glob('*.log'))
                self.assertEqual(log.read_bytes(), 'café'.encode())
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            finally:
                release.touch()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0][0], 0)

    def test_expanding_casefold_overlaps_never_split_a_protected_span(self):
        m = core_loader.SourceFileLoader("brain_verify_overlap", str(CORE / "bin/brain")).load_module()
        terms = [(t, 'fixture') for t in ('Σ', 'Σs', 'ß', 'sς')]
        for raw in ('ςßss', 'ςßss-ßΣ', 'sςßΣßss'):
            for split in range(len(raw) + 1):
                scrub = m.VerifyScrubber(terms)
                text = scrub.feed(raw[:split]) + scrub.feed(raw[split:]) + scrub.feed('', final=True)
                self.assertTrue(all(t.casefold() not in text.casefold() for t, _ in terms))
                self.assertEqual(len(text), len(raw))

    def test_exception_cleanup_kills_a_command_that_ignores_sigterm(self):
        m, inst, n, home, wt = self.runner()
        original = m.VerifyScrubber.feed
        def interrupt(scrub, text, final=False):
            if text == 'interrupt-fixture':
                raise KeyboardInterrupt()
            return original(scrub, text, final)
        # A shell builtin loop avoids leaving a grandchild behind if cleanup regresses.
        command = "trap '' TERM; printf interrupt-fixture; while :; do :; done"
        real_popen, children, timers, rescued = m.subprocess.Popen, [], [], []
        def popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            # Test safety: kill a regressed implementation after one second as well.
            def rescue():
                if child.poll() is None:
                    rescued.append(True)
                    child.kill()
            timer = threading.Timer(1, rescue)
            timers.append(timer)
            timer.start()
            return child
        with mock.patch.object(m.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(m.VerifyScrubber, 'feed', interrupt), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                m.run_verify(inst, n, 'job', command, {'path': str(wt)}, self.env)
        for timer in timers:
            timer.cancel()
        self.assertEqual(rescued, [])
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)

    def test_case_alias_and_unknown_descriptor_location(self):
        setup = self.runner()
        m, inst, n, home, wt = setup
        alias = n['dir'].parent.with_name(n['dir'].parent.name.upper()) / n['dir'].name
        if alias.exists():  # macOS case-insensitive volume; Linux keeps its native spelling
            n['dir'] = alias
        result, path, output = self.run_verify(setup, 'print("case fixture")')
        self.assertEqual(result, 0)
        self.assertEqual(path.read_text(), 'case fixture\n')
        with mock.patch.object(m, 'history_fd_path', return_value=None), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.run_verify(setup, 'pass')  # fail closed: unknown location is no containment evidence

    def test_handback_keeps_operator_gitignore_edits_uncommitted(self):
        r, home, wt = self.job()
        gi = r / '.gitignore'
        committed = self.git('show', 'HEAD:.gitignore', cwd=r)
        # Simulate upgrading an existing instance, with an unrelated operator edit.
        gi.write_text(committed.replace('**/job-handbacks/*-verify-*.log\n', '') + 'operator-fixture/\n')
        self.b(r, 'ready', 'a', '--branch', 'job', '--record', '--reviews', '1')
        self.assertEqual(self.git('show', 'HEAD:.gitignore', cwd=r), committed)
        self.assertIn('operator-fixture/', gi.read_text())
        self.assertIn('**/job-handbacks/*-verify-*.log', gi.read_text())
        self.assertIn(' M .gitignore', self.git('status', '--porcelain', cwd=r))

    def test_each_lane_tail_keeps_only_its_last_fifteen_lines(self):
        setup = self.runner()
        code = ("import sys\nfor lane in ('alpha', 'beta'):\n"
                " for i in range(30): print(f'[lane {lane}] detail {i}')\n"
                " print(f'[lane {lane}] FAILED')\nsys.exit(1)")
        result, path, output = self.run_verify(setup, code)
        self.assertEqual(result, 1)
        for lane in ('alpha', 'beta'):
            section = output.split('verify failing lane: ' + lane + '\n')[1].split('verify ')[0]
            self.assertEqual(len(section.splitlines()), 15)
            self.assertNotIn(f'[lane {lane}] detail 15\n', section)
            self.assertIn(f'[lane {lane}] detail 16\n', section)
            self.assertIn(f'[lane {lane}] FAILED', section)
        self.assertEqual(len(path.read_text().splitlines()), 62)

    def test_home_and_checkout_case_aliases_refuse_logs_inside_them(self):
        m, inst, n, home, wt = setup = self.runner()
        for root in (home, wt):
            alias = root.with_name(root.name.upper())
            if not alias.exists():
                alias = root  # case-sensitive systems still exercise containment
            node = root / 'inside-node'
            node.mkdir()
            n['dir'] = node
            with mock.patch.object(m, 'home_places', return_value=[alias]):
                with self.assertRaises(SystemExit):
                    self.run_verify(setup, 'pass')
            self.assertFalse((node / 'job-handbacks').exists())

    def test_eof_flush_and_generated_path_are_scrubbed(self):
        m, inst, n, home, wt = setup = self.runner()
        node = n['dir'].with_name('hiddenfixture')
        n['dir'].rename(node)
        n['dir'], n['id'] = node, 'hiddenfixture'
        (node / 'never-name.txt').write_text('SecretWord\nhiddenfixture\n')
        result, path, output = self.run_verify(setup,
            "import os,sys\nos.write(1,b'[lane alpha] FAILED end SecretWord\\xc3')\nsys.exit(1)")
        expected = '[lane alpha] FAILED end ' + '█'*10 + '�'
        self.assertEqual(path.read_bytes(), expected.encode())
        self.assertIn(expected, output)
        self.assertNotIn('secretword', output.casefold())
        self.assertNotIn('hiddenfixture', output.casefold())
        self.assertIn('verify log: ', output)
        self.assertEqual(result, 1)

    def test_universal_newlines_in_tails_and_counts_preserve_raw_log(self):
        m, inst, n, home, wt = setup = self.runner()
        m.VERIFY_PROGRESS_SECONDS = .1
        raw = '[lane alpha] old\r[lane beta] ERROR detail\r\n[lane alpha] FAIL final\r'
        code = ("import os,time,sys\nos.write(1," + repr(raw[:-1].encode()) + ")\n"
                "os.write(2,b'\\r')\nos.close(1)\nos.close(2)\ntime.sleep(.25)\nsys.exit(1)")
        result, path, output = self.run_verify(setup, code)
        self.assertEqual(result, 1)
        self.assertEqual(path.read_bytes(), raw.encode())
        self.assertIn('3 lines,', output)
        self.assertIn('verify failing lane: alpha', output)
        self.assertIn('verify failing lane: beta', output)
        self.assertIn('[lane beta] ERROR detail', output)
        self.assertNotIn('\r', output)

    def test_check_repairs_missing_private_log_exclusion(self):
        r = self.init('alice')
        gi = r / '.gitignore'
        gi.write_text(gi.read_text().replace('**/job-handbacks/*-verify-*.log\n', ''))
        self.assertIn('private verify logs', self.b(r, 'check', ok=False))
        self.assertIn('private verify logs', self.b(r, 'check', '--fix'))
        self.assertIn('**/job-handbacks/*-verify-*.log', gi.read_text())
        self.assertIn('all checks pass', self.b(r, 'check'))



class VerifyInputDiagnostics(Scenario):
    job = WorkerReadyPackets.job
    packet = WorkerReadyPackets.packet

    def inputs_job(self):
        r, home, wt = self.job()
        (wt / '.gitignore').write_text('.env*\n')
        (wt / 'apps/web').mkdir(parents=True)
        self.git('add', '.gitignore', cwd=wt)
        self.git('commit', '-qm', 'ignore fixture inputs', cwd=wt)
        self.b(r, 'set', 'a', 'verify_inputs=apps/web/.env.*')
        return r, home, wt

    def finish(self, r, ok=True):
        return self.b(r, 'handback', 'a', '--branch', 'job', '--state', 'done', '--reviews', '1', ok=ok)

    def private_map(self, packet):
        path = self.packet().parent / packet['input_files_ref']
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        return json.loads(path.read_text())['files']

    def assert_private(self, output, values):
        for value in values:
            self.assertNotIn(value, output)
        self.assertNotRegex(output, r'\b[0-9a-f]{64}\b')

    def test_verify_edits_own_env_names_path_and_fix_without_contents(self):
        r, home, wt = self.inputs_job()
        env = wt / 'apps/web/.env.local'
        env.write_text('FAKE_VALUE=initial-fixture\n')
        self.b(r, 'set', 'a', "verify=printf 'GENERATED=second-fixture\\n' >> apps/web/.env.local")
        output = self.finish(r, ok=False)
        self.assertIn('verify modified its own inputs: apps/web/.env.local (modified)', output)
        self.assertIn('before the first verify', output)
        self.assertIn('only if they cannot affect the result', output)
        self.assert_private(output, ('initial-fixture', 'second-fixture', 'FAKE_VALUE=', 'GENERATED='))
        data = json.loads(self.packet().read_text())
        self.assertIsNone(data['ready'])
        self.assertIn('verify modified its own inputs:', data['ready_reasons'][0])
        self.assertIn('verify modified its own inputs:', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))

    def test_between_runs_names_added_removed_modified_and_cannot_reuse(self):
        r, home, wt = self.inputs_job()
        folder = wt / 'apps/web'
        (folder / '.env.local').write_text('FAKE_VALUE=first-fixture\n')
        (folder / '.env.removed').write_text('FAKE_VALUE=removed-fixture\n')
        self.finish(r)
        initial = json.loads(self.packet().read_text())['ready']
        self.assertEqual(set(self.private_map(initial)), {'apps/web/.env.local', 'apps/web/.env.removed'})
        self.assertEqual(self.packet().stat().st_mode & 0o777, 0o600)
        (folder / '.env.local').write_text('FAKE_VALUE=changed-fixture\n')
        (folder / '.env.removed').unlink()
        (folder / '.env.added').write_text('FAKE_VALUE=added-fixture\n')
        output = self.b(r, 'ready', 'a', '--branch', 'job', ok=False)
        self.assertIn('changed since the last verify:', output)
        for path, action in (('.env.local', 'modified'), ('.env.removed', 'removed'), ('.env.added', 'added')):
            self.assertIn('apps/web/' + path + ' (' + action + ')', output)
        self.assert_private(output, ('first-fixture', 'changed-fixture', 'added-fixture', 'removed-fixture'))
        self.assertNotIn(str(wt), output)
        # A fresh successful run may establish new evidence, but a changed input cannot reuse the old one.
        output = self.finish(r)
        self.assertNotIn('reused the green run', output)
        self.assertIn('reused the green run', self.finish(r))
        self.assertEqual(self.b(r, 'ready', 'a', '--branch', 'job').strip(), 'ready')

    def test_during_verify_added_and_removed_inputs_are_named_even_with_dirty_tracked_files(self):
        r, home, wt = self.inputs_job()
        (wt / 'apps/web/.env.removed').write_text('FAKE_VALUE=removed-fixture\n')
        self.b(r, 'set', 'a', "verify=rm apps/web/.env.removed && printf added-fixture > apps/web/.env.added && echo '# fixture change' >> calc.py")
        output = self.finish(r, ok=False)
        self.assertIn('verify modified its own inputs:', output)
        self.assertIn('apps/web/.env.removed (removed)', output)
        self.assertIn('apps/web/.env.added (added)', output)
        self.assertIn("verify changed the job's branch", output)
        self.assert_private(output, ('removed-fixture', 'added-fixture'))
        self.assertIsNone(json.loads(self.packet().read_text())['ready'])

    def test_protected_path_and_new_privacy_policy_are_masked(self):
        r, home, wt = self.inputs_job()
        env = wt / 'apps/web/.env.HiddenFixture'
        env.write_text('FAKE_VALUE=initial-fixture\n')
        self.finish(r)
        env.write_text('FAKE_VALUE=changed-fixture\n')
        # The private map retains the real relative key, but every outward reason is screened.
        (r / 'nodes/a/never-name.txt').write_text('HiddenFixture\n')
        output = self.b(r, 'ready', 'a', '--branch', 'job', ok=False)
        self.assertNotIn('hiddenfixture', output.casefold())
        self.assertIn('apps/web/.env.', output)
        self.assertIn('(modified)', output)
        self.assert_private(output, ('initial-fixture', 'changed-fixture'))
        ui = self.b(r, 'ui', '--json')
        self.assertNotIn('hiddenfixture', ui.casefold())
        self.assertNotIn('"input_files"', ui)
        for fingerprint in self.private_map(json.loads(self.packet().read_text())['ready']).values():
            self.assertNotIn(fingerprint, ui)
        self.b(r, 'set', 'a', 'verify=echo another-fixture >> apps/web/.env.HiddenFixture')
        output = self.finish(r, ok=False)
        self.assertNotIn('hiddenfixture', output.casefold())
        # Refreshing policy must also screen the already-recorded refusal, not just a new difference.
        (r / 'nodes/a/never-name.txt').write_text('HiddenFixture\nmodified\n')
        self.assertNotIn('modified', self.b(r, 'ready', 'a', '--branch', 'job', ok=False).casefold())

    def test_old_packet_without_path_map_still_refuses_changed_inputs(self):
        r, home, wt = self.inputs_job()
        env = wt / 'apps/web/.env.local'
        env.write_text('FAKE_VALUE=first-fixture\n')
        self.finish(r)
        path = self.packet()
        data = json.loads(path.read_text())
        del data['ready']['input_files_ref']
        path.write_text(json.dumps(data))
        env.write_text('FAKE_VALUE=changed-fixture\n')
        output = self.b(r, 'ready', 'a', '--branch', 'job', ok=False)
        self.assertIn('per-path evidence unavailable', output)
        self.assert_private(output, ('first-fixture', 'changed-fixture'))

    def test_path_only_reason_escapes_controls_and_rejects_invalid_old_map(self):
        m = core_loader.SourceFileLoader('brain_input_reason', str(CORE / 'bin/brain')).load_module()
        node = self.tmp / 'node'
        node.mkdir()
        n = {'id': 'fixture', 'dir': node, 'parent': None}
        output = m.verify_input_reason(n, {'apps/.env\nlocal': 'a'*64}, {'apps/.env\nlocal': 'b'*64})
        self.assertIn(r'apps/.env\nlocal', output)
        self.assertNotIn('\n', output)
        self.assert_private(output, ('a'*64, 'b'*64))
        for old in (None, [], {'../outside': 'a'*64}, {'/outside': 'a'*64}, {'env': []}):
            output = m.verify_input_reason(n, old, {})
            self.assertIn('per-path evidence unavailable', output)
            self.assertNotIn('outside', output)
            output = m.verify_input_reason(n, {}, old)
            self.assertIn('per-path evidence unavailable', output)
            self.assertNotIn('outside', output)
        (node / 'never-name.txt').write_text('Σ\n')
        output = m.verify_input_reason(n, {'apps/.env.Σ\nlocal': 'a'*64}, {})
        self.assertNotIn('Σ', output)
        self.assertNotIn(r'\u03a3', output)
        (node / 'never-name.txt').write_text('modified\n')
        output = m.verify_input_reason(n, {'apps/.env': 'a'*64}, {'apps/.env': 'b'*64})
        self.assertNotIn('modified', output)

    def test_due_json_rescreens_recorded_paths_after_policy_changes(self):
        r, home, wt = self.inputs_job()
        env = wt / 'apps/web/.env.HiddenFixture'
        env.write_text('FAKE_VALUE=initial-fixture\n')
        self.b(r, 'set', 'a', 'verify=echo changed-fixture >> apps/web/.env.HiddenFixture')
        self.finish(r, ok=False)
        self.assertIn('HiddenFixture', self.packet().read_text())
        (r / 'nodes/a/never-name.txt').write_text('HiddenFixture\n')
        for args in (('due', 'a', '--json'), ('due', 'a'), ('ui', '--json'), ('ready', 'a', '--branch', 'job')):
            output = self.b(r, *args, ok=False if args[0] == 'ready' else True)
            self.assertNotIn('hiddenfixture', output.casefold())

    def test_per_file_hashes_stay_in_ignored_sidecar_and_missing_evidence_still_refuses(self):
        r, home, wt = self.inputs_job()
        env = wt / 'apps/web/.env.local'
        env.write_text('FAKE_VALUE=first-fixture\n')
        self.finish(r)
        packet = json.loads(self.packet().read_text())['ready']
        files = self.private_map(packet)
        history = self.git('log', '-p', '--', 'nodes/a/job-handbacks', cwd=r)
        for fingerprint in files.values():
            self.assertNotIn(fingerprint, history)
            self.assertNotIn(fingerprint, self.packet().read_text())
        sidecar = self.packet().parent / packet['input_files_ref']
        self.assertEqual(self.git('status', '--porcelain', cwd=r), '')
        self.assertEqual(self.git('ls-files', str(sidecar.relative_to(r)), cwd=r), '')
        # Reuse preserves the existing local map rather than multiplying sidecars.
        self.assertIn('reused the green run', self.finish(r))
        self.assertEqual(json.loads(self.packet().read_text())['ready']['input_files_ref'], packet['input_files_ref'])
        sidecar.unlink()
        env.write_text('FAKE_VALUE=changed-fixture\n')
        output = self.b(r, 'ready', 'a', '--branch', 'job', ok=False)
        self.assertIn('per-path evidence unavailable', output)
        self.assertNotIn('reused the green run', self.finish(r))
        self.assertTrue(self.private_map(json.loads(self.packet().read_text())['ready']))


    def test_untrusted_sidecar_is_optional_and_never_weakens_changed_input_refusal(self):
        r, home, wt = self.inputs_job()
        env = wt / 'apps/web/.env.local'
        env.write_text('FAKE_VALUE=initial-fixture\n')
        self.finish(r)
        packet = json.loads(self.packet().read_text())['ready']
        sidecar = self.packet().parent / packet['input_files_ref']
        original = sidecar.read_text()
        env.write_text('FAKE_VALUE=changed-fixture\n')
        wrong = json.loads(original)
        wrong['inputs'] = 'wrong-digest-fixture'
        sidecar.write_text(json.dumps(wrong))
        self.assertIn('per-path evidence unavailable', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        sidecar.write_text(original)
        sidecar.chmod(0o644)
        self.assertIn('per-path evidence unavailable', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        sidecar.chmod(0o600)
        outside = self.tmp / 'outside.map'
        outside.write_text(original)
        sidecar.unlink()
        sidecar.symlink_to(outside)
        self.assertIn('per-path evidence unavailable', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        self.assertEqual(outside.read_text(), original)


    def test_legacy_inline_map_never_enters_ui_output(self):
        r, home, wt = self.inputs_job()
        (wt / 'apps/web/.env.local').write_text('FAKE_VALUE=initial-fixture\n')
        self.finish(r)
        data = json.loads(self.packet().read_text())
        files = self.private_map(data['ready'])
        # Older experimental packets may still carry the map inline. Keep it private too.
        data['ready']['input_files'] = files
        self.packet().write_text(json.dumps(data))
        output = self.b(r, 'ui', '--json')
        self.assertNotIn('"input_files"', output)
        self.assertNotIn('"input_files_ref"', output)
        for fingerprint in files.values():
            self.assertNotIn(fingerprint, output)

        self.b(r, 'handback', 'a', '--branch', 'job', '--state', 'done')
        history = self.git('log', '-p', '--', 'nodes/a/job-handbacks', cwd=r)
        self.assertNotIn('input_files', self.packet().read_text().replace('input_files_ref', 'sidecar_ref'))
        for fingerprint in files.values():
            self.assertNotIn(fingerprint, history)


if __name__ == "__main__":
    unittest.main()
