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


class Dashboard(Scenario):
    def test_blockers_paused_and_work_in_flight(self):
        r = self.init("alice")
        for nid in ("a", "p"):
            self.b(r, "new", nid, "--title", nid.upper())
        st = r / "nodes" / "a" / "status.md"
        text = st.read_text().replace("## Blocked / needs the operator\n",
                                      "## Blocked / needs the operator\n\n- Pick a hosting plan, cheap or\n"
                                      "  fast WRAPPED-END\n")
        text = text.replace("| --- | --- | --- | --- |\n", "| --- | --- | --- | --- |\n"
                            "| fix-auth | t-1 | #12 | review |\n| old | t-0 | #9 | merged |\n")
        st.write_text(text)
        self.b(r, "relabel", "p", "state:paused")
        out = self.b(r)
        self.assertIn("a: Pick a hosting plan, cheap or fast WRAPPED-END", out)
        self.assertIn("in flight 1", out)
        self.assertIn("[paused]", out)
        self.assertNotIn("p: no coordinator", out)
        self.assertIn("a: no coordinator", out)



class Housekeeping(Scenario):
    def dirty(self, r):
        """Someone else's unfinished work: an edit and a new file in another node."""
        st = r / "nodes" / "b" / "status.md"
        facts = re.search(r"<!-- brain:facts v1 -->.*?<!-- /brain:facts -->", st.read_text(), re.S).group()
        st.write_text("Updated: x\nHALF-WRITTEN\n" + facts + "\n")
        (r / "nodes" / "b" / "draft.md").write_text("draft\n")

    def assert_untouched(self, r):
        st = self.git("status", "--porcelain", cwd=r)
        self.assertIn("nodes/b/status.md", st)
        self.assertIn("nodes/b/draft.md", st)
        self.assertNotIn("HALF-WRITTEN", self.git("log", "-p", "--all", "--", "nodes", cwd=r))

    def test_commands_commit_only_what_they_wrote(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "bob")
        for nid in ("a", "b", "c"):
            self.b(r, "new", nid, "--title", nid.upper())
        self.dirty(r)
        self.b(r, "owners", "c", "alice,bob")            # seals c (no home yet)
        self.b(r, "seal", "c", "--owners", "bob", "--home", str(self.tmp / "ch"))
        self.b(r, "move", "a", "c")
        self.b(r, "split", "c/a", "e", "--title", "E")
        self.b(r, "new", "f", "--title", "F")
        self.b(r, "merge", "e", "f")
        (r / "nodes" / "f" / "CLAUDE.md").unlink()
        self.assertIn("fixed: linked nodes/f/CLAUDE.md", self.b(r, "check", "--fix", ok=None))  # c/a is sealed now
        self.b(r, "revoke", "bob")
        self.assert_untouched(r)
        log = self.git("log", "--format=%s", cwd=r)
        for msg in ("c: owners", "c: seal", "Move a", "Split e", "Merge e", "Revoke bob"):
            self.assertIn(msg, log)
        self.assertNotIn("nodes/b/", self.git("log", "--name-only", "--format=", "-8", cwd=r))

    def test_check_fix_repairs_each_kind(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "bob")
        self.b(r, "new", "a", "--title", "A")
        (self.tmp / "vh").mkdir()
        self.b(r, "new", "v", "--title", "V", "--owners", "bob", "--home", str(self.tmp / "vh"))
        self.b(r, "new", "b", "--title", "B")
        a, v = r / "nodes" / "a", r / "nodes" / "v"
        (a / "CLAUDE.md").unlink()                                       # links
        (a / "decisions.md").unlink()                                    # templates
        ag = v / "AGENTS.md"                                             # views
        ag.write_text(ag.read_text().split("## This is the operator's side")[0])
        gi = r / ".gitignore"                                            # gitignore
        gi.write_text(gi.read_text().replace(".brain/\n", ""))
        (r / "registry.md").write_text("stale\n")                        # registry
        self.git("add", "-A", cwd=r); self.git("commit", "-qm", "break things", cwd=r)
        self.dirty(r)
        out = self.b(r, "check", ok=False)
        for msg in ("CLAUDE.md must be a link", "missing decisions.md", "sealed-view section",
                    "doesn't ignore .brain/", "registry.md is out of date"):
            self.assertIn(msg, out)
        out = self.b(r, "check", "--fix")
        self.assertIn("all checks pass", out)
        self.assertTrue((a / "CLAUDE.md").is_symlink())
        self.assertTrue((a / "decisions.md").exists())
        self.assertIn("operator's side of a sealed node", ag.read_text())
        self.assertIn(".brain/", gi.read_text())
        self.assertIn("| v | V | bob | yes", (r / "registry.md").read_text())
        self.assert_untouched(r)



class FutureTimestamps(Scenario):
    def test_hygiene_extension_plans_and_examples_are_not_entry_timestamps(self):
        r = self.init('maple')
        (r / 'decisions.md').write_text(
            '## 2020-01-01 10:00: Recorded decision\n'
            'Deliver by 2999-01-01; discussed 2999-01-02 as an alternative.\n'
            '- Planned review: 2999-01-03\n'
            '## Schedule for 2999-01-04\n'
            '```md\n## 2999-01-05 10:00: Example only\n```\n'
            '<!--\n## 2999-01-06 10:00: Comment example\n-->\n'
            '## 2999-01-07 10:00: Future recorded decision\n'
            '- 2999-01-08 from worker: Future inbox-style entry\n')
        out = self.b(r, 'check')
        for number in range(2, 11):
            self.assertNotIn('decisions.md:' + str(number) + ': future', out)
        self.assertIn('decisions.md:11: future dated entry', out)
        self.assertIn('decisions.md:12: future dated entry', out)

    def test_five_minute_skew_uses_elapsed_time_across_dst(self):
        import datetime as dt
        from core_loader import SourceFileLoader
        from unittest import mock
        from zoneinfo import ZoneInfo
        m = SourceFileLoader("brain_future", str(CORE / "bin/brain")).load_module()
        r = self.init("alice")
        inst = m.Instance(r)
        node = inst.node("root")
        now = dt.datetime(2026, 11, 1, 1, 59, tzinfo=ZoneInfo("America/New_York"))
        with mock.patch.object(inst, "now_dt", return_value=now):
            (r / "status.md").write_text("Updated: 2026-11-01 01:03 -05:00\n")  # four actual minutes later
            self.assertEqual(m.future_entries(inst, node)[0], [])
            (r / "status.md").write_text("Updated: 2026-11-01 01:10 -05:00\n")  # eleven actual minutes later
            self.assertIn("future Updated:", m.future_entries(inst, node)[0][0])

    def test_offsets_skew_and_legacy_entries(self):
        import datetime as dt
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        cfg = r / "instance.md"
        cfg.write_text(cfg.read_text().replace("timezone: UTC", "timezone: Asia/Beirut"))
        st = r / "nodes/a/status.md"
        facts = re.search(r"<!-- brain:facts v1 -->.*?<!-- /brain:facts -->", st.read_text(), re.S).group()
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo("Asia/Beirut"))
        for when in (now + dt.timedelta(minutes=4), now - dt.timedelta(days=1)):
            st.write_text(f"Updated: {when:%Y-%m-%d %H:%M} by coordinator\n" + facts)
            self.assertNotIn("future", self.b(r, "check"))
        future = now + dt.timedelta(minutes=20)
        for text in (future.strftime("%Y-%m-%d %H:%M"), future.isoformat(timespec="seconds"),
                     future.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M +00:00")):
            st.write_text(f"Updated: {text} by coordinator\n" + facts)
            self.assertIn("future Updated: time", self.b(r, "check", ok=False))
        st.write_text("Updated: 15:58 by coordinator\n- 23:59: old log\n" + facts)
        self.assertNotIn("future", self.b(r, "check"))

    def test_other_dated_entries_warn_and_hook_explains_fix(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        node = r / "nodes/a"
        (node / "decisions.md").write_text("## 2999-01-01 00:00 +03:00: decision\n")
        st = node / "status.md"
        facts = re.search(r"<!-- brain:facts v1 -->.*?<!-- /brain:facts -->", st.read_text(), re.S).group()
        st.write_text("Updated: 2020-01-01 00:00\n- 2999-01-02: log entry\n- 23:59: old\n" + facts)
        out = self.b(r, "check")
        self.assertIn("! a: decisions.md:1: future dated entry", out)
        self.assertIn("! a: status.md:2: future dated entry", out)
        st.write_text("Updated: 2999-01-01 00:00 +03:00 by coordinator\n" + facts)
        p = subprocess.run([sys.executable, str(CORE / "bin/brain"), "hook", "stop"], cwd=node,
                           input=json.dumps({"cwd": str(node)}), capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0)
        self.assertIn("future Updated: time; fix it with `brain now`", json.loads(p.stdout)["reason"])



class FutureEntryRegressions(Scenario):
    def test_invalid_utf8_and_nonfiles_do_not_break_views_or_stop_hook(self):
        r = self.init("alice")
        (r / "notes.md").write_bytes(b"invalid \xff\n- 2999-01-01: future note\n")
        (r / "directory.md").mkdir()
        if hasattr(os, "mkfifo"):
            os.mkfifo(r / "pipe.md")
        self.assertIn("notes.md:2: future dated entry", self.b(r, "check"))
        self.assertIn("alice brain", self.b(r))
        self.assertIn('"nodes"', self.b(r, "ui", "--json"))
        st = r / "status.md"
        st.write_text("Updated: 2999-01-01 00:00 +00:00\n")
        res = subprocess.run([sys.executable, str(CORE / "bin/brain"), "hook", "stop"], cwd=r,
                             input=json.dumps({"cwd": str(r), "session_id": "invalid-utf8"}),
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(res.returncode, 0)
        self.assertIn("future Updated:", json.loads(res.stdout)["reason"])

    def test_unreadable_markdown_is_skipped(self):
        from core_loader import SourceFileLoader
        from unittest import mock
        m = SourceFileLoader("brain_unreadable_future", str(CORE / "bin/brain")).load_module()
        r = self.init("alice")
        bad = r / "unreadable.md"
        bad.write_text("not readable\n")
        (r / "readable.md").write_text("- 2999-01-01: future\n")
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == bad:
                raise PermissionError("unreadable")
            return original(path, *args, **kwargs)
        inst = m.Instance(r)
        with mock.patch.object(Path, "read_text", read):
            problems, warnings = m.future_entries(inst, inst.node("root"))
        self.assertEqual(problems, [])
        self.assertTrue(any("readable.md:1" in w for w in warnings))

    def test_human_time_ranges_are_not_negative_offsets(self):
        import datetime as dt
        from core_loader import SourceFileLoader
        from unittest import mock
        m = SourceFileLoader("brain_range_future", str(CORE / "bin/brain")).load_module()
        r = self.init("alice")
        inst = m.Instance(r)
        node = inst.node("root")
        now = dt.datetime(2026, 10, 19, 12, tzinfo=dt.timezone.utc)
        with mock.patch.object(inst, "now_dt", return_value=now):
            for text in ("2026-10-19 09:00-10:30", "2026-10-19 09:00-1030",
                         "2026-10-19 09:00:00-10:30"):
                (r / "status.md").write_text(f"Updated: {text}\n- {text}: meeting\n")
                problems, warnings = m.future_entries(inst, node)
                self.assertEqual(problems, [], text)
                self.assertFalse(any("status.md:" in w for w in warnings), text)
            # Explicit spaced offsets and ISO T timestamps still compare actual moments.
            for text in ("2026-10-19 09:00 -10:30", "2026-10-19T09:00-10:30",
                         "2026-10-19T09:00:00.123-10:30"):
                (r / "status.md").write_text(f"Updated: {text}\n")
                self.assertEqual(len(m.future_entries(inst, node)[0]), 1, text)

    def test_future_reminders_are_spaced_per_session_and_address_workers(self):
        r = self.init("alice")
        (r / "status.md").write_text("Updated: 2999-01-01 00:00 +00:00\n")
        self.git("commit", "-qam", "incorrect clock", cwd=r)
        def hook(session):
            res = subprocess.run([sys.executable, str(CORE / "bin/brain"), "hook", "stop"], cwd=r,
                                 input=json.dumps({"cwd": str(r), "session_id": session}),
                                 capture_output=True, text=True, env=self.env)
            self.assertEqual(res.returncode, 0)
            return json.loads(res.stdout) if res.stdout.strip() else None
        first = hook("worker")
        self.assertIn("future Updated:", first["reason"])
        self.assertIn("if you are a worker, tell your coordinator", first["reason"])
        self.assertIsNone(hook("worker"))
        self.assertIsNotNone(hook("coordinator"))
        state = r / ".brain/hooks.json"
        reminders = json.loads(state.read_text())
        reminders["worker"] = "2020-01-01T00:00:00+00:00"
        state.write_text(json.dumps(reminders))
        self.assertIsNotNone(hook("worker"))



class AutonomyDue(AutonomyProject):
    def test_finished_worker_is_due_until_locally_merged_and_closed(self):
        r, home, wt = self.project()
        self.assertNotIn('handback', [i['kind'] for i in self.due(r)])
        (wt / 'result.txt').write_text('ready\n')
        self.git('add', '-A', cwd=wt); self.git('commit', '-qm', 'result', cwd=wt)
        before = (r / 'nodes/app/status.md').read_bytes()
        ledger = (r / 'nodes/app/job-ledger.md').read_bytes()
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'done', '--report', 'reports/result.md')
        self.assertNotEqual((r / 'nodes/app/status.md').read_bytes(), before)
        self.assertIn('| job-a | - | - | done |', (r / 'nodes/app/status.md').read_text())
        self.assertEqual((r / 'nodes/app/job-ledger.md').read_bytes(), ledger)
        self.assertEqual([i['state'] for i in self.due(r) if i['kind'] == 'handback'], ['done'])
        self.assertIn('brain handback app --branch job-a', self.b(r, 'brief', 'app', '--branch', 'job-a'))
        self.git('merge', '-q', 'job-a', cwd=home)
        self.assertIn('merged-job', [i['kind'] for i in self.due(r)])
        self.assertNotIn('handback', [i['kind'] for i in self.due(r)])
        self.b(r, 'wt', 'prune', 'app', '--offline')
        self.assertNotIn('handback', [i['kind'] for i in self.due(r)])
        self.assertEqual(len(list((r / 'nodes/app/job-handbacks').glob('*.json'))), 1)
        self.b(r, 'jobs', 'app', '--branch', 'job-a', '--lessons', 'none: job-specific result')
        self.assertEqual(len(list((r / 'nodes/app/job-handbacks').glob('*.json'))), 0)

    def test_quiet_empty_and_due_exit_codes_and_no_file_changes(self):
        r = self.init('alice')
        def quiet():
            return subprocess.run([sys.executable, str(r / 'core/bin/brain'), 'due', 'root', '--quiet'],
                                  cwd=r, env=self.env, text=True, capture_output=True, timeout=10)
        p = quiet()
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, '', ''))
        (r / 'inbox.md').write_text('- 2026-10-19 10:00 +00:00 from worker: question: Which format?\n')
        p = quiet()
        self.assertEqual((p.returncode, p.stdout, p.stderr), (1, '', ''))
        self.assertIn('Which format?', self.b(r, 'due'))
        (r / 'inbox.md').write_text('')
        self.assertEqual(quiet().returncode, 0)

    def test_legacy_and_missing_worktrees_and_failed_handback(self):
        r, home, wt = self.project()
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'failed')
        self.assertEqual([i['state'] for i in self.due(r) if i['kind'] == 'handback'], ['failed'])
        self.git('worktree', 'remove', str(wt), cwd=home)
        self.assertIn('missing-worktree', [i['kind'] for i in self.due(r)])
        (r / 'nodes/app/job-ledger.md').write_text('# Old job notes\n')
        kinds = [i['kind'] for i in self.due(r)]
        self.assertIn('ledger', kinds)
        self.assertIn('unrecorded-worktree', kinds)
        self.assertEqual((r / 'nodes/app/job-ledger.md').read_text(), '# Old job notes\n')
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'missing', '--state', 'done', ok=False)

    def test_old_status_is_due_and_refresh_resolves_only_staleness(self):
        r, home, wt = self.project()
        st = r / 'nodes/app/status.md'
        st.write_text('Updated: 2020-01-01 00:00 +00:00\n\n## Now\n\n- Busy.\n')
        self.assertIn('stale-status', [i['kind'] for i in self.due(r)])
        self.b(r, 'status', 'app', '--refresh')
        self.assertNotIn('stale-status', [i['kind'] for i in self.due(r)])
        # The status-only commit must not create a self-sustaining due loop.
        self.git('add', 'nodes/app/status.md', cwd=r); self.git('commit', '-qm', 'app: refresh', cwd=r)
        self.assertEqual(self.due(r), [])
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'blocked', '--report', 'report.md')
        self.assertNotIn('stale-status', [i['kind'] for i in self.due(r)])
        self.b(r, 'status', 'app', '--refresh')
        self.assertEqual([i['kind'] for i in self.due(r)], ['handback'])
        # A worker commit changes no displayed fact until its signal or merge state changes.
        (wt / 'new.txt').write_text('new\n')
        self.git('add', '-A', cwd=wt); self.git('commit', '-qm', 'new', cwd=wt)
        self.assertNotIn('stale-status', [i['kind'] for i in self.due(r)])

    def test_refresh_before_a_combined_notes_commit_stays_current(self):
        r, home, wt = self.project()
        brief = r / 'nodes/app/brief.md'
        brief.write_text(brief.read_text() + '\nA fresh judgement.\n')
        self.b(r, 'status', 'app', '--refresh')
        self.git('add', 'nodes/app', cwd=r); self.git('commit', '-qm', 'app: notes', cwd=r)
        self.assertEqual(self.due(r), [])
        brief.write_text(brief.read_text() + '\nAnother event.\n')
        self.assertEqual(self.due(r), [], 'unprinted notes are not generated facts')

    def test_reused_branch_has_a_distinct_signal(self):
        r, home, wt = self.project()
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'done')
        self.b(r, 'wt', 'rm', 'app', 'job-a', '--cause', 'restart')
        self.b(r, 'wt', 'add', 'app', 'job-a')
        self.assertNotIn('handback', [i['kind'] for i in self.due(r)])
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'blocked')
        self.assertEqual(len(list((r / 'nodes/app/job-handbacks').glob('*.json'))), 1)

    def test_corrupt_worker_signal_is_due_rather_than_a_crash(self):
        r, home, wt = self.project()
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'done')
        signal = next((r / 'nodes/app/job-handbacks').glob('*.json'))
        signal.write_text('{broken')
        self.assertIn('invalid-handback', [i['kind'] for i in self.due(r)])

    def test_handback_record_cannot_be_moved_into_a_sealed_home(self):
        r, home, wt = self.project()
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'done')
        self.b(r, 'principal', 'add', 'bob')
        sealed_home = self.tmp / 'sealed-app'
        out = self.b(r, 'seal', 'app', '--owners', 'bob', '--home', str(sealed_home),
                     '--move', 'job-handbacks', ok=False)
        self.assertIn("stays on the operator's side", out)
        self.assertFalse(sealed_home.exists())
        self.assertTrue(list((r / 'nodes/app/job-handbacks').glob('*.json')))

    def test_due_redacts_and_never_calls_the_network(self):
        r, home, wt = self.project()
        (r / 'nodes/app/never-name.txt').write_text('OldCo\n')
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'done', '--report', 'OldCo/report.md')
        fakebin = self.tmp / 'fakebin'; fakebin.mkdir()
        gh = fakebin / 'gh'; gh.write_text('#!/bin/sh\ntouch "' + str(self.tmp / 'network-called') + '"\nexit 1\n'); gh.chmod(0o755)
        self.git('remote', 'add', 'origin', 'https://github.com/example/example', cwd=home)
        self.env['PATH'] = str(fakebin) + ':' + self.env['PATH']
        self.assertNotIn('OldCo', self.b(r, 'due', 'app', '--json'))
        self.assertFalse((self.tmp / 'network-called').exists())



class AutonomyFacts(AutonomyProject):
    def test_refresh_computes_unlocked_beside_a_real_background_commit(self):
        root, home, wt = self.project()
        (wt / 'feature.txt').write_text('worker change\n')
        self.git('add', 'feature.txt', cwd=wt)
        self.git('commit', '-qm', 'worker change', cwd=wt)
        (home / 'unrelated.bin').write_bytes(b'before')
        self.git('add', 'unrelated.bin', cwd=home)
        self.git('commit', '-qm', 'unrelated source', cwd=home)
        marker, release = self.tmp / 'refresh-compute', self.tmp / 'refresh-release'
        script = '''import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from brain import cli, status, instance
marker, release = map(Path, sys.argv[2:4])
original = status.status_facts
def slow(*args, **kwargs):
    assert instance._LOCK['fh'] is None, 'refresh projection holds the instance lock'
    marker.write_text('computing')
    deadline = time.monotonic() + 15
    while not release.exists():
        if time.monotonic() > deadline:
            raise RuntimeError('refresh was never released')
        time.sleep(.01)
    return original(*args, **kwargs)
status.status_facts = slow
sys.argv = ['brain', 'status', 'app', '--refresh']
cli.main()
'''
        env = {**self.env, 'BRAIN_ROOT': str(root)}
        reader = subprocess.Popen([sys.executable, '-B', '-c', script, str(CORE / 'lib'), str(marker), str(release)],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 8
            while not marker.exists() and reader.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            if not marker.exists():
                stdout, stderr = reader.communicate(timeout=3)
                self.fail('refresh did not reach unlocked projection: ' + stdout + stderr)
            writer = subprocess.run([sys.executable, '-B', '-c', '''import sys
sys.path.insert(0, sys.argv[1])
from brain import instance
with instance.brain_lock(instance.Instance(__import__('pathlib').Path(sys.argv[2]))):
    (__import__('pathlib').Path(sys.argv[3]) / 'unrelated.bin').write_bytes(b'after')
    __import__('subprocess').run(['git', 'commit', '-qam', 'background commit'], cwd=sys.argv[3], check=True)
''', str(CORE / 'lib'), str(root), str(home)], env=env, capture_output=True, text=True, timeout=3)
            self.assertEqual(writer.returncode, 0, writer.stdout + writer.stderr)
            self.assertIsNone(reader.poll())
        finally:
            release.write_text('continue')
            try:
                stdout, stderr = reader.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                reader.kill(); stdout, stderr = reader.communicate()
        self.assertEqual(reader.returncode, 0, stdout + stderr)

    def test_refresh_refuses_relevant_change_without_overwriting_it(self):
        root = self.init('alice')
        loader = core_loader.SourceFileLoader('refresh_relevant_fence', str(CORE / 'bin/brain'))
        m = loader.load_module()
        status_refresh = __import__('importlib').import_module(loader.package_name + '.status_refresh')
        inst, path = m.Instance(root), root / 'status.md'
        original = path.read_text()
        def changed(current, node):
            self.assertIsNone(status_refresh.instance._LOCK['fh'])
            prepared = m.refreshed_text(current, node)
            path.write_text(original + '\nConcurrent coordinator judgement.\n')
            return prepared
        with self.assertRaisesRegex(SystemExit, 'state changed during the unlocked step'):
            status_refresh.compute(inst, inst.node('root'), changed)
        self.assertEqual(path.read_text(), original + '\nConcurrent coordinator judgement.\n')

    def test_refresh_refuses_a_changed_job_ref(self):
        root, home, wt = self.project()
        loader = core_loader.SourceFileLoader('refresh_git_fence', str(CORE / 'bin/brain'))
        m = loader.load_module()
        refresh = __import__('importlib').import_module(loader.package_name + '.status_refresh')
        inst = m.Instance(root)
        node = inst.node('app')
        original = (node['dir'] / 'status.md').read_bytes()
        def changed(current, target):
            text = m.refreshed_text(current, target)
            (wt / 'concurrent.txt').write_text('relevant branch change\n')
            self.git('add', 'concurrent.txt', cwd=wt)
            self.git('commit', '-qm', 'relevant branch change', cwd=wt)
            return text
        with mock.patch.dict(os.environ, self.env), self.assertRaisesRegex(SystemExit, 'status refresh inputs changed'):
            refresh.compute(inst, node, changed)
        self.assertEqual((node['dir'] / 'status.md').read_bytes(), original)

    def test_refresh_fences_complete_ref_names(self):
        root, home, wt = self.project()
        loader = core_loader.SourceFileLoader('refresh_complete_refs', str(CORE / 'bin/brain'))
        m = loader.load_module()
        refresh = __import__('importlib').import_module(loader.package_name + '.status_refresh')
        common = refresh.common
        inst = m.Instance(root)
        base = self.git('rev-parse', 'HEAD', cwd=home).strip()
        self.git('commit', '--allow-empty', '-qm', 'concurrent target', cwd=home)
        head = self.git('rev-parse', 'HEAD', cwd=home).strip()
        for branch in ('job/fix+api', 'job/café', 'job/unqualified'):
            with self.subTest(branch=branch):
                ref = 'refs/heads/' + branch
                self.git('update-ref', ref, base, cwd=home)
                def changed(current, target):
                    answer = common.git('rev-parse', '--verify', branch if branch == 'job/unqualified' else ref, cwd=home).stdout
                    self.git('update-ref', ref, head, cwd=home)
                    return answer
                with self.assertRaisesRegex(SystemExit, 'status refresh inputs changed'):
                    refresh.compute(inst, inst.node('app'), changed)

    def test_refresh_fences_linked_worktree_head_history(self):
        root, home, wt = self.project()
        loader = core_loader.SourceFileLoader('refresh_linked_history', str(CORE / 'bin/brain'))
        m = loader.load_module()
        refresh = __import__('importlib').import_module(loader.package_name + '.status_refresh')
        inst = m.Instance(root)
        def changed(current, target):
            answer = refresh.common.git('log', '-1', '--format=%H', cwd=wt).stdout
            self.git('commit', '--allow-empty', '-qm', 'new worker history', cwd=wt)
            return answer
        with self.assertRaisesRegex(SystemExit, 'status refresh inputs changed'):
            refresh.compute(inst, inst.node('app'), changed)

    def test_refresh_accepts_unrelated_commit_inside_a_git_query(self):
        root, home, wt = self.project()
        loader = core_loader.SourceFileLoader('refresh_commit_during_query', str(CORE / 'bin/brain'))
        m = loader.load_module()
        refresh = __import__('importlib').import_module(loader.package_name + '.status_refresh')
        inst = m.Instance(root)
        base = self.git('rev-parse', 'HEAD', cwd=home).strip()
        run = subprocess.run
        committed = []
        def interleave(command, *args, **kwargs):
            if 'merge-base' in command and not committed:
                self.assertIsNone(refresh.instance._LOCK['fh'])
                committed.append(True)
                self.git('commit', '--allow-empty', '-qm', 'unrelated commit during query', cwd=home)
            return run(command, *args, **kwargs)
        def prepare(current, target):
            return refresh.common.git('merge-base', '--is-ancestor', base, 'refs/heads/main', cwd=home).returncode
        with mock.patch.object(subprocess, 'run', side_effect=interleave):
            self.assertEqual(refresh.compute(inst, inst.node('app'), prepare), 0)
        self.assertTrue(committed)

    def test_refresh_reports_maintenance_refusal_as_failure(self):
        root = self.init('alice')
        loader = core_loader.SourceFileLoader('refresh_maintenance_failure', str(CORE / 'bin/brain'))
        m = loader.load_module()
        events = __import__('importlib').import_module(loader.package_name + '.events')
        inst = m.Instance(root)
        with mock.patch.object(events, 'maintain', side_effect=ValueError('concurrent input changed')):
            with self.assertRaisesRegex(SystemExit, 'status refresh maintenance refused'):
                with m.brain_lock(inst), events.Command(root, command='status'):
                    pass
        commit = events.read(inst)[-1]
        self.assertEqual(commit['kind'], 'commit')
        self.assertFalse(commit['data']['maintenance_ok'])
        self.assertEqual(commit['data']['views'], {})

    def test_refresh_fences_symbolic_replacement_targets(self):
        root, home, wt = self.project()
        loader = core_loader.SourceFileLoader('refresh_replacement_refs', str(CORE / 'bin/brain'))
        m = loader.load_module()
        refresh = __import__('importlib').import_module(loader.package_name + '.status_refresh')
        inst = m.Instance(root)
        base = self.git('rev-parse', 'HEAD', cwd=home).strip()
        (home / 'delivered.txt').write_text('delivered change\n')
        self.git('add', 'delivered.txt', cwd=home)
        self.git('commit', '-qm', 'delivery', cwd=home)
        head = self.git('rev-parse', 'HEAD', cwd=home).strip()
        replacement = self.git('commit-tree', head + '^{tree}', '-p', base, '-m', 'replacement', cwd=home).strip()
        self.git('branch', 'replacement-target', replacement, cwd=home)
        self.git('symbolic-ref', 'refs/replace/' + head, 'refs/heads/replacement-target', cwd=home)
        def changed(current, target):
            answer = refresh.common.git('diff', '--name-only', base, head, cwd=home).stdout
            self.assertIn('delivered.txt', answer)
            self.git('update-ref', 'refs/heads/replacement-target', base, cwd=home)
            return answer
        with self.assertRaisesRegex(SystemExit, 'status refresh inputs changed'):
            refresh.compute(inst, inst.node('app'), changed)

    def test_refresh_reconciliation_fences_earlier_nodes_until_publication(self):
        root = self.init('alice')
        self.b(root, 'new', 'a', '--title', 'A')
        self.b(root, 'new', 'b', '--title', 'B')
        loader = core_loader.SourceFileLoader('refresh_batch_fence', str(CORE / 'bin/brain'))
        m = loader.load_module()
        events = __import__('importlib').import_module(loader.package_name + '.events')
        inst = m.Instance(root)
        path = root / 'nodes/a/status.md'
        path.write_text(path.read_text() + '\nDirty baseline.\n')
        original = events.snapshot_view
        changed = []
        def interleave(current, node, *args, **kwargs):
            if node['id'] == 'b' and not changed:
                path.write_text(path.read_text() + '\nConcurrent first-node judgement.\n')
                changed.append(True)
            return original(current, node, *args, **kwargs)
        journal = root / '.brain/events.jsonl'
        with m.brain_lock(inst), events.Command(root, command='status'):
            before = journal.read_bytes()
            with mock.patch.object(events, 'snapshot_view', side_effect=interleave):
                with self.assertRaisesRegex(SystemExit, 'state changed during the unlocked step'):
                    events.reconcile(inst, node=inst.node('root'), snapshot=True)
            self.assertEqual(journal.read_bytes(), before, 'stale reconciliation was published')
        self.assertTrue(changed)
        self.assertIn('Concurrent first-node judgement.', path.read_text())

    def test_refresh_write_lock_excludes_slow_projection(self):
        root = self.init('alice')
        loader = core_loader.SourceFileLoader('refresh_lock_measurement', str(CORE / 'bin/brain'))
        m = loader.load_module()
        refresh = __import__('importlib').import_module(loader.package_name + '.status_refresh')
        timings = __import__('importlib').import_module(loader.package_name + '.timings')
        inst = m.Instance(root)
        phases = {}
        token = timings._CURRENT.set(phases)
        def slow(current, node):
            self.assertIsNone(refresh.instance._LOCK['fh'])
            time.sleep(.15)
            return m.refreshed_text(current, node)
        try:
            with m.brain_lock(inst):
                text = refresh.compute(inst, inst.node('root'), slow)
                self.assertIsNotNone(refresh.instance._LOCK['fh'])
                m.atomic_write(root / 'status.md', text)
            self.assertGreaterEqual(phases['refresh_compute'], .15)
            self.assertLess(phases['lock_hold'], 1)
        finally:
            timings._CURRENT.reset(token)


    def test_refresh_preserves_judgement_bytes_and_separates_timestamps(self):
        r, home, wt = self.project()
        st = r / 'nodes/app/status.md'
        original = 'Updated: 2020-01-01 00:00 +00:00\n\n# App\n\n## Now\n\n- Judgement, exactly.\n'
        st.write_text(original)
        self.b(r, 'status', 'app', '--refresh')
        text = st.read_text()
        outside = text.split('<!-- brain:facts v1 -->')[0] + text.split('<!-- /brain:facts -->')[1][2:]
        outside = re.sub(r'(?ms)\n## Active work\n.*', '', outside)
        self.assertEqual(outside, original)
        self.assertIn('Open worktrees (this machine):\n- job-a: present', text)
        self.assertIn('runtime activity unknown', text)
        self.assertIn('Updated: 2020-01-01 00:00 +00:00', self.b(r, 'status', 'app'))
        self.assertIn('+00:00', text.split('Facts refreshed:', 1)[1].splitlines()[0])
        st.write_text('PREFIX\n' + text + '\nSUFFIX')
        prefix, rest = st.read_text().split('<!-- brain:facts v1 -->')
        _, suffix = rest.split('<!-- /brain:facts -->')
        self.b(r, 'status', 'app', '--refresh')
        self.assertTrue(st.read_text().startswith(prefix))
        self.assertTrue(st.read_text().endswith(suffix))
        self.b(r, 'status', '--refresh', ok=False)

    def test_fingerprint_tracks_only_displayed_facts(self):
        r, home, wt = self.project()
        self.b(r, 'status', 'app', '--refresh')
        st = r / 'nodes/app/status.md'
        before = st.read_text().split('<!-- brain:facts-state ', 1)[1].split(' -->', 1)[0]
        (wt / 'worker.txt').write_text('worker change\n')
        self.git('add', '-A', cwd=wt); self.git('commit', '-qm', 'worker commit', cwd=wt)
        (home / 'home.txt').write_text('unrelated home change\n')
        self.git('add', '-A', cwd=home); self.git('commit', '-qm', 'home commit', cwd=home)
        (r / 'nodes/app/brief.md').write_text('unprinted judgement\n')
        self.assertEqual(self.due(r), [])
        self.b(r, 'status', 'app', '--refresh')
        after = st.read_text().split('<!-- brain:facts-state ', 1)[1].split(' -->', 1)[0]
        self.assertEqual(json.loads(before)['fingerprint'], json.loads(after)['fingerprint'])
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'done')
        self.assertNotIn('stale-status', [i['kind'] for i in self.due(r)])
        self.b(r, 'status', 'app', '--refresh')
        self.assertNotIn('stale-status', [i['kind'] for i in self.due(r)])
        self.git('merge', '-qm', 'merge worker', 'job-a', cwd=home)
        self.assertIn('stale-status', [i['kind'] for i in self.due(r)])
        self.b(r, 'status', 'app', '--refresh')
        self.assertNotIn('stale-status', [i['kind'] for i in self.due(r)])

    def test_facts_refresh_does_not_freshen_judgement_or_hook(self):
        r = self.init('alice')
        st = r / 'status.md'
        st.write_text('Updated: 2020-01-01 00:00 +00:00\n\n## Now\n- Old judgement.\n')
        self.b(r, 'status', 'root', '--refresh')
        self.git('add', 'status.md', cwd=r); self.git('commit', '-qm', 'facts only', cwd=r)
        self.assertIn('Updated: 2020-01-01', self.b(r, 'status', 'root'))
        self.assertIn('behind by', self.b(r, 'stale', 'root', '2020-01-02T00:00:00Z'))
        self.assertIn('stale', [i['kind'] for i in json.loads(self.b(r, 'inbox', '--json'))])
        result = subprocess.run([sys.executable, str(r / 'core/bin/brain'), 'hook', 'stop'],
                                cwd=r, env=self.env, input=json.dumps({'cwd': str(r), 'session_id': 'old-judgement'}),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '', 'an unchanged read-only turn is not held for age alone')
        # Older generated Updated lines are never mistaken for coordinator judgement.
        st.write_text(st.read_text().replace('Facts refreshed:', 'Updated:').replace(
            'Updated: 2020-01-01 00:00 +00:00', 'No coordinator timestamp'))
        self.assertIn('(no Updated line)', self.b(r, 'status', 'root'))
        self.assertIn('has no Updated line', self.b(r, 'stale', 'root', '2020-01-02T00:00:00Z'))

    def test_merged_since_previous_refresh_is_a_snapshot_not_a_guess_from_the_clock(self):
        r, home, wt = self.project()
        self.b(r, 'status', 'app', '--refresh')
        (wt / 'a.txt').write_text('a\n')
        self.git('add', '-A', cwd=wt); self.git('commit', '-qm', 'A', cwd=wt)
        self.git('merge', '-q', 'job-a', cwd=home)
        st = r / 'nodes/app/status.md'
        before = st.read_bytes()
        self.b(r, 'wt', 'prune', 'app', '--offline')
        self.assertEqual(st.read_bytes(), before)
        self.b(r, 'status', 'app', '--refresh')
        self.assertIn('job-a (1 commits)', st.read_text())
        self.b(r, 'status', 'app', '--refresh')
        self.assertIn('Merged since previous refresh:\n- None recorded.', st.read_text())

    def test_facts_show_local_merge_before_ledger_closes_without_reporting_it_twice(self):
        r, home, wt = self.project()
        self.b(r, 'status', 'app', '--refresh')
        (wt / 'a.txt').write_text('a\n')
        self.git('add', '-A', cwd=wt); self.git('commit', '-qm', 'A', cwd=wt)
        self.git('merge', '-q', 'job-a', cwd=home)
        self.b(r, 'status', 'app', '--refresh')
        st = r / 'nodes/app/status.md'
        self.assertIn('- job-a: merged in local git; ledger still open', st.read_text())
        self.b(r, 'wt', 'prune', 'app', '--offline')
        self.b(r, 'status', 'app', '--refresh')
        self.assertIn('Merged since previous refresh:\n- None recorded.', st.read_text())

    def test_malformed_markers_are_not_overwritten(self):
        r = self.init('alice')
        st = r / 'status.md'
        for text in ['<!-- brain:facts v1 -->\nUnclosed\n',
                     '<!-- /brain:facts -->\n<!-- brain:facts v1 -->',
                     '<!-- brain:facts v1 --><!-- /brain:facts -->' * 2]:
            st.write_text(text)
            before = st.read_bytes()
            self.b(r, 'status', 'root', '--refresh', ok=False)
            self.assertEqual(st.read_bytes(), before)

    def test_refresh_preserves_crlf_outside_the_block(self):
        r = self.init('alice')
        st = r / 'status.md'
        original = b'Updated: old\r\n\r\n## Now\r\n- Original.\r\n'
        st.write_bytes(original)
        self.b(r, 'status', 'root', '--refresh')
        self.assertEqual(st.read_bytes().split(b'<!-- brain:facts v1 -->')[0] +
                         st.read_bytes().split(b'<!-- /brain:facts -->')[1][2:], original)
        st.write_bytes(b'PREFIX\r\n' + st.read_bytes() + b'SUFFIX\r\n')
        before = st.read_bytes()
        self.b(r, 'status', 'root', '--refresh')
        self.assertEqual(st.read_bytes().split(b'<!-- /brain:facts -->')[1],
                         before.split(b'<!-- /brain:facts -->')[1])
        self.assertTrue(st.read_bytes().startswith(b'PREFIX\r\n'))

    def test_refresh_and_due_do_not_wait_for_lock_from_hook(self):
        import fcntl
        r = self.init('alice')
        self.b(r, 'status', 'root', '--refresh')
        st = r / 'status.md'
        before = st.read_bytes()
        with open(r / '.brain/lock', 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for args, data in [(['due', 'root', '--quiet'], None),
                               (['hook', 'stop'], json.dumps({'cwd': str(r), 'session_id': 'locked'}))]:
                p = subprocess.run([sys.executable, str(r / 'core/bin/brain'), *args], cwd=r,
                                   env=self.env, input=data, capture_output=True, text=True, timeout=5)
                self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(st.read_bytes(), before)



class AutonomyInbox(Scenario):
    def items(self, r, *args):
        return json.loads(self.b(r, 'inbox', '--json', *args))

    def test_failed_stdout_does_not_consume_new_items(self):
        r = self.init('alice')
        self.items(r, '--new')
        saved = r / '.brain/inbox-seen.json'
        before = saved.read_bytes()
        (r / 'inbox.md').write_text('- 2026-10-19 10:00 +00:00 from worker: New question.\n')
        for flags in [[], ['--json']]:
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            with os.fdopen(write_fd, 'w') as broken:
                result = subprocess.run([sys.executable, str(r / 'core/bin/brain'), 'inbox', '--new', *flags],
                                        cwd=r, env=self.env, stdout=broken, stderr=subprocess.PIPE, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('BrokenPipeError', result.stderr)
            self.assertEqual(saved.read_bytes(), before)
        self.assertEqual([i['text'] for i in self.items(r, '--new')], ['inbox: 2026-10-19 10:00 +00:00 from worker: New question.'])
        self.assertEqual(self.items(r, '--new'), [])

    def test_stable_ids_once_per_machine_and_resolution(self):
        r = self.init('alice')
        self.b(r, 'new', 'a', '--title', 'A')
        st = r / 'nodes/a/status.md'
        st.write_text(st.read_text().replace('## Blocked / needs the operator\n',
                                             '## Blocked / needs the operator\n\n- Pick a plan.\n- Approve a budget.\n'))
        first = self.items(r, '--new')
        plan = next(i for i in first if i['text'] == 'Pick a plan.')
        self.assertEqual(self.items(r, '--new'), [])
        st.write_text(st.read_text().replace('Approve a budget.', 'Choose a new name.'))
        fresh = self.items(r, '--new')
        self.assertEqual([i['text'] for i in fresh], ['Choose a new name.'])
        again = next(i for i in self.items(r) if i['text'] == 'Pick a plan.')
        self.assertEqual((again['id'], again['first_seen']), (plan['id'], plan['first_seen']))
        self.assertTrue(again['reported'])
        st.write_text(st.read_text().replace('- Pick a plan.\n', ''))
        self.assertNotIn(plan['id'], [i['id'] for i in self.items(r, '--new')])
        self.assertNotIn(plan['id'], json.loads((r / '.brain/inbox-seen.json').read_text())['reporters']['root'])
        st.write_text(st.read_text().replace('## Blocked / needs the operator\n',
                                             '## Blocked / needs the operator\n\n- Pick a plan.\n'))
        self.assertEqual([i['text'] for i in self.items(r, '--new')], ['Pick a plan.'])
        (r / '.brain/inbox-seen.json').unlink()
        self.assertTrue(self.items(r, '--new'), 'another machine has independent seen state')

    def test_subtree_reporters_do_not_consume_each_others_requests(self):
        r = self.init('alice')
        for node in ['a', 'a/child', 'ab', 'sibling']:
            self.b(r, 'new', node, '--title', node)
        self.b(r, 'principal', 'add', 'bob')
        home = self.tmp / 'sealed-home'
        self.b(r, 'seal', 'a', '--owners', 'alice,bob', '--home', str(home))
        for node in ['root', 'a', 'a/child', 'ab', 'sibling']:
            folder = r if node == 'root' else r.joinpath('nodes', *node.replace('/', '/nodes/').split('/'))
            (folder / 'inbox.md').write_text('- 2026-10-19 10:00 +00:00 from worker: question for ' + node + '\n')
        def requests(*args):
            return [i for i in self.items(r, *args) if i['kind'] == 'inbox']
        child = requests('a', '--new')
        self.assertEqual({i['node'] for i in child}, {'a', 'a/child'})
        self.assertEqual(requests('a', '--new'), [])
        root = requests('root', '--new')
        self.assertEqual({i['node'] for i in root}, {'root', 'a', 'a/child', 'ab', 'sibling'})
        self.assertEqual({i['id'] for i in child}, {i['id'] for i in root if i['node'] in ('a', 'a/child')})
        self.assertEqual({i['node'] for i in requests('a/child', '--new')}, {'a/child'})
        # A sealed coordinator's local folder defaults to its own subtree, never parents or siblings.
        local = json.loads(self.run_(['inbox', '--json'], cwd=r / 'nodes/a', brain=r / 'core/bin/brain'))
        self.assertEqual({i['node'] for i in local}, {'a', 'a/child'})
        self.env['BRAIN_ROOT'] = str(r)
        external = json.loads(self.run_(['inbox', '--json'], cwd=home, brain=r / 'core/bin/brain'))
        self.assertEqual({i['node'] for i in external}, {'a', 'a/child'})
        self.assertFalse((home / '.brain/inbox-seen.json').exists())
        self.assertFalse(self.git('ls-files', '.brain', cwd=r).strip())

    def test_unscoped_legacy_state_repeats_instead_of_losing_requests(self):
        r = self.init('alice')
        first = self.items(r, '--new')
        saved = r / '.brain/inbox-seen.json'
        saved.write_text(json.dumps({i['id']: {'reported_at': '2026-01-01T00:00:00Z'} for i in first}))
        self.assertEqual({i['id'] for i in first}, {i['id'] for i in self.items(r, '--new')})
        self.assertEqual(self.items(r, '--new'), [])

    def test_ui_reuses_attention_sources_without_marking_reported(self):
        r = self.init('alice')
        (r / 'inbox.md').write_text('- 2026-10-19 10:00 +00:00 from worker: question: Which date?\n')
        state = json.loads(self.b(r, 'ui', '--json'))
        self.assertEqual({(i['node'], i['kind'], i['text']) for i in state['inbox']},
                         {(i['node'], i['kind'], i['text']) for i in state['attention']})
        self.assertTrue(all(i['id'] and i['first_seen'] for i in state['inbox']))
        self.assertFalse((r / '.brain/inbox-seen.json').exists())
        # The live view adds only the record's sender; everything else is the CLI's own inbox.
        self.assertTrue(all('from' in i for i in state['inbox']))
        self.assertEqual([{k: v for k, v in i.items() if k != 'from'} for i in state['inbox']], self.items(r))
        self.assertEqual(len(self.items(r, '--new')), len(state['inbox']))
        self.assertTrue(all(i['reported'] for i in json.loads(self.b(r, 'ui', '--json'))['inbox']))
        self.assertFalse(self.git('ls-files', '.brain', cwd=r).strip())

    def test_stale_day_count_does_not_create_another_notification(self):
        r = self.init('alice')
        st = r / 'status.md'
        st.write_text('Updated: 2020-01-01 00:00 +00:00\n')
        first = next(i for i in self.items(r, '--new') if i['kind'] == 'stale')
        st.write_text('Updated: 2020-01-02 00:00 +00:00\n')
        self.assertEqual(self.items(r, '--new'), [])
        self.assertEqual(next(i['id'] for i in self.items(r) if i['kind'] == 'stale'), first['id'])



    def test_command_timings_phases_profiles_filters_and_fail_soft(self):
        root = self.init('demo')
        m = core_loader.SourceFileLoader('command_timing_fixture', str(CORE / 'bin/brain')).load_module()
        tm = m._owners['cmd_timings']
        journal = root / '.brain/events.jsonl'
        before = journal.read_bytes() if journal.exists() else None
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(root)}), \
                mock.patch.object(sys, 'argv', ['brain', 'now']), mock.patch.object(tm, 'SLOW_SECONDS', 0):
            with tm.command():
                tm.select_command('now')
                tm.record('lock_wait', 0.125)
                tm.record('lock_hold', 0.25)
                with tm.phase('fixture'):
                    sum(range(100))
        self.assertEqual(journal.read_bytes() if journal.exists() else None, before)
        self.assertTrue((root / '.brain/timings/events.jsonl').is_file())
        rows = json.loads(self.b(root, 'timings', '--slow', '--command', 'now', '--json'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['lock_wait_s'], 0.125)
        self.assertEqual(rows[0]['lock_hold_s'], 0.25)
        self.assertGreaterEqual(rows[0]['phases']['fixture'], 0)
        profile = rows[0]['profile']
        self.assertTrue(profile['top'])
        self.assertLessEqual(len(profile['top']), 30)
        self.assertNotIn(str(self.tmp), json.dumps(profile))
        self.assertIn('No matching', self.b(root, 'timings', '--command', 'absent'))
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(root)}), \
                mock.patch.object(tm, 'publish', side_effect=PermissionError('fixture')):
            with tm.command():
                pass  # optional telemetry cannot turn a successful read into failure
        self.assertEqual(m.pending(m.read(m.Instance(root))), {})

    def test_slow_command_appends_one_bounded_record_and_reads_legacy_profiles(self):
        root = self.init('demo')
        m = core_loader.SourceFileLoader('bounded_timing_fixture', str(CORE / 'bin/brain')).load_module()
        tm = m._owners['cmd_timings']
        folder = root / '.brain/timings'
        before_names = {p.name for p in folder.glob('*')}
        before = (folder / 'events.jsonl').read_bytes() if (folder / 'events.jsonl').exists() else b''
        clock = mock.Mock()
        clock.perf_counter.side_effect = [0.0, 6.0]
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(root)}), \
                mock.patch.object(tm, 'time', clock), \
                mock.patch.object(tm.pstats, 'Stats') as stats:
            stats.return_value.stats = {('fixture.py', i, 'λ' * 5000 + str(i)): (1, 1, 0.01, 0.02, {})
                                        for i in range(40)}
            with tm.command():
                tm.select_command('status')
                tm.record('fixture', 6)
        after = (folder / 'events.jsonl').read_bytes()
        self.assertTrue(after.startswith(before))
        appended = after[len(before):]
        self.assertEqual(len(appended.splitlines()), 1)
        self.assertLessEqual(len(appended), tm.MAX_RECORD_BYTES)
        self.assertEqual(before_names | {'events.jsonl'}, {p.name for p in folder.iterdir()})
        self.assertTrue(json.loads(appended)['data']['slow'])
        self.assertIsInstance(json.loads(appended)['data']['profile'], dict)
        self.assertTrue(json.loads(appended)['data']['profile']['truncated'])
        legacy = {'command': 'legacy', 'elapsed_s': 6, 'top': [{'function': 'fixture:1:read'}]}
        filename = 'a' * 32 + '.json'
        m.atomic_write(folder / filename, json.dumps(legacy), private=True)
        row = json.loads(appended)
        row['data'].update(command='legacy', profile=filename)
        m.append_line(folder / 'events.jsonl', row)
        rows = json.loads(self.b(root, 'timings', '--command', 'legacy', '--json'))
        self.assertEqual(rows[0]['profile'], legacy)

    def test_lock_hold_counts_physical_ownership_once_across_release(self):
        root = self.init('demo')
        m = core_loader.SourceFileLoader('physical_lock_timing', str(CORE / 'bin/brain')).load_module()
        tm = m._owners['cmd_timings']
        phases, clock = {}, [0.0]
        token = tm._CURRENT.set(phases)
        try:
            with mock.patch.object(tm.time, 'perf_counter', side_effect=lambda: clock[0]):
                with m.brain_lock(m.Instance(root)):
                    clock[0] = 1
                    with m.brain_lock(m.Instance(root)):
                        clock[0] = 2
                    clock[0] = 4
                    m.unlock()
                    clock[0] = 100  # unlocked computation is excluded
                    with m.brain_lock(m.Instance(root)):
                        clock[0] = 103
                    m.unlock()
            self.assertEqual(phases['lock_hold'], 7)
        finally:
            tm._CURRENT.reset(token)

    def test_timed_writes_keep_operational_commit_and_abort_as_last_rows(self):
        root = self.init('demo')
        journal = root / '.brain/events.jsonl'
        self.b(root, 'new', 'app', '--title', 'App')
        self.assertEqual(json.loads(journal.read_text().splitlines()[-1])['kind'], 'commit')
        self.b(root, 'set', 'app', 'id=other', ok=False)
        self.assertEqual(json.loads(journal.read_text().splitlines()[-1])['kind'], 'abort')
        timings = json.loads(self.b(root, 'timings', '--command', 'set', '--json'))
        self.assertTrue(timings)
        self.assertFalse(timings[-1]['success'])
        self.assertGreaterEqual(timings[-1]['lock_wait_s'], 0)

    def test_rejected_command_never_persists_raw_command_text(self):
        root = self.init('demo')
        (root / 'never-name.txt').write_text('Orchid\n')
        m = core_loader.SourceFileLoader('rejected_timing_fixture', str(CORE / 'bin/brain')).load_module()
        tm = m._owners['cmd_timings']
        before = {p.name: p.read_bytes() for p in (root / '.brain/timings').glob('*') if p.is_file()}
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(root)}), \
                mock.patch.object(sys, 'argv', ['brain', 'orchid-private']), \
                mock.patch.object(tm, 'SLOW_SECONDS', 0), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as rejected:
            m.main()
        self.assertEqual(rejected.exception.code, 2)
        self.assertEqual(before, {p.name: p.read_bytes() for p in (root / '.brain/timings').glob('*') if p.is_file()})
        for path in (root / '.brain/timings').glob('*.json*'):
            self.assertNotIn('orchid', path.read_text().lower())

    def test_stale_inbox_acknowledgement_cannot_erase_new_ids_or_restore_resolved_markers(self):
        root = self.init('demo')
        self.b(root, 'new', 'app', '--title', 'App')
        status = root / 'nodes/app/status.md'
        initial = status.read_text()
        def blocked(*items):
            status.write_text(initial.replace('## Blocked / needs the operator\n',
                              '## Blocked / needs the operator\n\n' + ''.join('- ' + item + '\n' for item in items)))
        m = core_loader.SourceFileLoader('stale_inbox_fixture', str(CORE / 'bin/brain')).load_module()
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(root)}):
            inst = m.Instance(root)
            def scan():
                items, reporters = m.inbox_items(inst)
                now = inst.now_dt().isoformat(timespec='seconds')
                state = {i['id']: {'first_seen': i['first_seen'], 'reported_at': now} for i in items}
                return items, state, reporters.generation
            blocked('Choose a plan.')
            _, old, old_generation = scan()
            blocked('Choose a plan.', 'Choose a color.')
            _, new, new_generation = scan()
            m.publish_inbox_seen(inst, 'root', new, new_generation)
            path = root / '.brain/inbox-seen.json'
            latest = path.read_bytes()
            m.publish_inbox_seen(inst, 'root', old, old_generation)
            self.assertEqual(path.read_bytes(), latest, 'older scan must retain newly acknowledged IDs')
            items, old, old_generation = scan()
            plan = next(i['id'] for i in items if i['text'] == 'Choose a plan.')
            blocked('Choose a color.')
            _, new, new_generation = scan()
            m.publish_inbox_seen(inst, 'root', new, new_generation)
            latest = path.read_bytes()
            m.publish_inbox_seen(inst, 'root', old, old_generation)
            self.assertEqual(path.read_bytes(), latest, 'older scan must not undo a newer resolution')
            self.assertNotIn(plan, json.loads(latest)['reporters']['root'])
            blocked('Choose a plan.', 'Choose a color.')
            items, _, _ = scan()
            self.assertFalse(next(i for i in items if i['id'] == plan)['reported'])
        self.assertIn('Choose a plan.', [i['text'] for i in self.items(root, '--new')])

    def test_long_inbox_read_does_not_block_status_refresh(self):
        self.assert_read_does_not_block_status('inbox')

    def test_due_scan_does_not_hold_instance_lock_and_publication_does(self):
        self.assert_read_does_not_block_status('due')

    def assert_read_does_not_block_status(self, command):
        root = self.init('demo')
        marker, release = self.tmp / 'reading', self.tmp / 'release'
        script = '''import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from brain import cli, status, watch, instance
command = sys.argv[4]
original = status.inbox_items if command == 'inbox' else watch.watch_scan
def slow(*args, **kwargs):
    assert instance._LOCK['fh'] is None, 'read computation holds the instance lock'
    Path(sys.argv_marker).write_text('reading')
    deadline = time.monotonic() + 15
    while not Path(sys.argv_release).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('test reader was never released')
        time.sleep(.01)
    return original(*args, **kwargs)
if command == 'inbox':
    status.inbox_items = slow
else:
    watch.watch_scan = slow
    publish = watch.watch_run
    def checked_publish(*args, **kwargs):
        assert instance._LOCK['fh'] is not None, 'receipt publication lost its instance lock'
        assert kwargs.get('groups') is not None, 'publication would recompute groups under lock'
        return publish(*args, **kwargs)
    watch.watch_run = checked_publish
sys.argv_marker, sys.argv_release = sys.argv[2:4]
sys.argv = ['brain', command, 'root'] + (['--new'] if command == 'inbox' else [])
cli.main()
'''
        env = {**self.env, 'BRAIN_ROOT': str(root)}
        reader = subprocess.Popen([sys.executable, '-B', '-c', script, str(CORE / 'lib'), str(marker), str(release), command],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 8
            while not marker.exists() and reader.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists(), 'reader did not reach the controlled computation')
            start = time.monotonic()
            writer = subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'), 'status', 'root', '--refresh'],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(writer.returncode, 0, writer.stdout + writer.stderr)
            self.assertIsNone(reader.poll(), 'reader must still be computing when status refresh finishes')
            self.assertLess(time.monotonic() - start, 5)
        finally:
            release.write_text('release')
            try:
                stdout, stderr = reader.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                reader.kill(); stdout, stderr = reader.communicate()
        self.assertEqual(reader.returncode, 0, stdout + stderr)
        rows = json.loads(self.b(root, 'timings', '--command', command, '--json'))
        if command == 'inbox':
            self.assertIn('lock_wait', rows[-1]['phases'])
            self.assertIn('seen_write', rows[-1]['phases'])
        else:
            self.assertIn('watch_compute', rows[-1]['phases'])

    def test_runaway_scope_keeps_live_and_unsettled_but_excludes_closed_settled(self):
        root = self.init('demo')
        m = core_loader.SourceFileLoader('runaway_scope_fixture', str(CORE / 'bin/brain')).load_module()
        inst, node = m.Instance(root), m.Instance(root).node('root')
        row = {**dict.fromkeys(m.JOB_COLS, ''), 'Job': 'job/live', 'Started': inst.now_dt().isoformat()}
        closed = {**row, 'Job': 'job/closed', 'Ended': inst.now_dt().isoformat(), 'Outcome': 'merged', 'Settled': 'yes'}
        ended = {**row, 'Job': 'job/terminal'}
        rows = [closed, ended, row]
        summary = {'open': [{**ended, 'age_h': 1}, {**row, 'age_h': 1}], 'median_hours': None, 'median_tokens': None}
        with mock.patch.object(m, 'read_jobs', return_value=rows), \
                mock.patch.object(m, 'jobs_summary', return_value=summary), \
                mock.patch.object(m, 'job_signal', side_effect=lambda i, n, r: {'state': 'done'} if r['Job'] == ended['Job'] else None), \
                mock.patch.object(m, 'live_usage', return_value=None) as usage:
            self.assertEqual(m.runaway_jobs(inst, node), [])
            self.assertEqual([call.args[2]['Job'] for call in usage.call_args_list], ['job/terminal', 'job/live'])

    def test_runaway_malformed_historical_counters_remain_unknown(self):
        root = self.init('demo')
        m = core_loader.SourceFileLoader('runaway_unknown_fixture', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(root); node = inst.node('root')
        row = {**dict.fromkeys(m.JOB_COLS, ''), 'Job': 'job/live', 'Started': inst.now_dt().isoformat()}
        invalid = {**row, 'Job': 'job/closed', 'Ended': inst.now_dt().isoformat(),
                   'Outcome': 'merged', 'Settled': 'yes', 'Hours': 'unknown', 'Reviews': 'corrupt'}
        with mock.patch.object(m, 'read_jobs', return_value=[invalid, row]), \
                mock.patch.object(m, 'live_usage', return_value=None):
            summary = m.jobs_summary(inst, node)
            self.assertIsNone(summary['median_hours'])
            self.assertIsNone(summary['median_reviews'])
            self.assertEqual(m.runaway_jobs(inst, node), [])

    def test_read_snapshot_shares_facts_and_ledger_but_expires_after_call(self):
        root = self.init('demo')
        m = core_loader.SourceFileLoader('snapshot_scope_fixture', str(CORE / 'bin/brain')).load_module()
        reads = m._owners['memo']
        inst = m.Instance(root); node = inst.node('root')
        path = root / m.JOBS_FILE
        path.write_text(m.JOBS_MARK + '\n')
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(root)}):
            with reads.snapshot():
                first = m.read_jobs(node)
                path.write_text('legacy')
                self.assertIs(m.read_jobs(node), first)
            self.assertIsNone(m.read_jobs(node))
            path.write_text(m.JOBS_MARK + '\n')
            state = m.file_state(inst, node)
            with reads.snapshot():
                first = m.status_facts(inst, node, state, [], metadata_only=True)
                self.assertIs(m.status_facts(inst, node, state, [], metadata_only=True), first)
            self.assertIsNot(m.status_facts(inst, node, state, [], metadata_only=True), first)


class NodeHygiene(Scenario):
    def setUp(self):
        super().setUp()
        loader = core_loader.SourceFileLoader('brain_hygiene', str(CORE / 'bin/brain'))
        self.m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.m)

    def node(self):
        r = self.init('maple')
        self.b(r, 'new', 'app', '--title', 'App')
        inst = self.m.Instance(r)
        return r, inst, inst.node('app')

    def block(self, n, since):
        text = ('Choose a color — waiting on owner; clears when chosen; since: ' + since +
                '; recommendation: blue; default: keep gray')
        with (n['dir'] / 'status.md').open('a') as f:
            f.write('\n## Blocked / needs the operator\n\n- ' + text + '\n')
        return text

    def test_facts_required_refresh_preserves_judgement(self):
        r, inst, n = self.node()
        st = n['dir'] / 'status.md'
        self.assertIn(self.m.FACTS_START, st.read_text())
        judgement = 'Updated: 2020-01-01 00:00 +00:00\n\n# App\n\n## Now\n\n- Exact judgement.\n'
        st.write_text(judgement)
        self.assertIn('missing or malformed generated facts block', self.b(r, 'check', ok=False))
        self.b(r, 'status', 'app', '--refresh')
        self.assertIn('✓', self.b(r, 'check'))
        self.assertEqual(self.m.FACTS_RE.sub('', st.read_text()).replace('\n\n\n\n', '\n\n'), judgement)
        st.write_text(st.read_text() + self.m.FACTS_START)
        self.assertIn('malformed', self.b(r, 'check', ok=False))

    def test_check_fix_migrates_missing_facts_without_changing_judgement(self):
        r, inst, n = self.node()
        originals = {}
        for st in (r / 'status.md', n['dir'] / 'status.md'):
            original = (b'Updated: 2020-01-01 00:00 +00:00\r\n\r\n# Status\r\n\r\n'
                        b'<!-- Keep this judgement verbatim. -->\r\n\r\n## Now\r\n\r\n'
                        b'- Exact judgement, with trailing spaces.  \r\n\r\n## Next\r\n- Keep planning.')
            st.write_bytes(original)
            originals[st] = original
        self.assertIn('generated facts block', self.b(r, 'check', ok=False))
        # This is the new-core migration command used by brain upgrade.
        out = self.b(r, 'check', '--fix')
        self.assertIn('root: inserted generated facts block', out)
        self.assertIn('app: inserted generated facts block', out)
        self.assertIn('all checks pass', out)
        pattern = re.compile(re.escape(self.m.FACTS_START.encode()) + b'.*?' +
                             re.escape(self.m.FACTS_END.encode()), re.S)
        for st, original in originals.items():
            after = st.read_bytes()
            block = pattern.search(after)
            self.assertIsNotNone(block)
            self.assertIn(b'## Facts (generated by brain status --refresh)', block.group())
            self.assertEqual(after[block.end():block.end() + 2], b'\n\n')
            self.assertEqual(after[:block.start()] + after[block.end() + 2:], original)
        before = {st: st.read_bytes() for st in originals}
        self.assertIn('all checks pass', self.b(r, 'check', '--fix'))
        self.assertEqual({st: st.read_bytes() for st in originals}, before)

    def test_check_fix_never_rewrites_malformed_facts_even_with_other_repairs(self):
        r, inst, n = self.node()
        st = n['dir'] / 'status.md'
        root_status = r / 'status.md'
        start, end = self.m.FACTS_START, self.m.FACTS_END
        for markers in (start, end, start + '\n' + start + '\n' + end,
                        start + '\n' + end + '\n' + start + '\n' + end,
                        end + '\n' + start):
            with self.subTest(markers=markers):
                damaged = ('Updated: 2020-01-01 00:00 +00:00\n\n# App\n\n' +
                           markers + '\n## Now\n- Preserve this human edit.\n').encode()
                st.write_bytes(damaged)
                # Ensure the facts fixer actually runs for a different node in this call.
                root_status.write_text('Updated: 2020-01-01 00:00 +00:00\n\n# Root\n\n## Now\n- Keep.\n')
                out = self.b(r, 'check', '--fix', ok=False)
                self.assertIn('root: inserted generated facts block', out)
                self.assertIn('app: status.md missing or malformed generated facts block', out)
                self.assertEqual(st.read_bytes(), damaged)
                self.assertIn(start, root_status.read_text())

    def test_now_limit_counts_wrapped_lines_but_not_history_or_comments(self):
        r, inst, n = self.node()
        st = n['dir'] / 'status.md'
        base = self.m.FACTS_RE.search(st.read_text()).group()
        st.write_text(base + '\n## Now\n' + '- current\n' * 30 + '\n<!-- comment -->\n## Recent changes\n' + '- old\n' * 100)
        self.assertNotIn('Now has', self.b(r, 'check'))
        st.write_text(st.read_text().replace('## Recent changes', '  wrapped\n## Recent changes'))
        self.assertIn('Now has 31 nonblank lines (limit 30); move history', self.b(r, 'check'))

    def test_blocked_exact_threshold_escalates_once_without_clock_renotification(self):
        r, inst, n = self.node()
        since = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        text = self.block(n, since.isoformat())
        with (n['dir'] / 'status.md').open('a') as f:
            f.write('<!-- A wrapped comment\n     is not part of the need. -->\n')
        self.assertEqual(self.m.status_section(n, 'blocked'), [text])
        with mock.patch.object(inst, 'now_dt', return_value=since + datetime.timedelta(hours=48)):
            b = self.m.blocked_entries(inst, n)[0]
            self.assertFalse(b['escalated'])
            self.assertEqual(b['age_hours'], 48)
            before = self.m.inbox_items(inst, [n], 'app')[0]
        with mock.patch.object(inst, 'now_dt', return_value=since + datetime.timedelta(hours=49)):
            due = [i for i in self.m.due_items(inst, n) if i['kind'] == 'blocked-overdue']
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0]['age_hours'], 49)
            self.assertEqual(due[0]['recommendation'], 'blue')
            self.assertEqual(due[0]['default'], 'keep gray')
            after = self.m.inbox_items(inst, [n], 'app')[0]
            self.assertNotEqual(before[0]['id'], after[0]['id'])
        saved = r / '.brain/inbox-seen.json'
        saved.write_text(json.dumps({'version': 2, 'reporters': {'app': {
            i['id']: {'reported_at': since.isoformat(), 'first_seen': since.isoformat()} for i in after}}}))
        with mock.patch.object(inst, 'now_dt', return_value=since + datetime.timedelta(hours=50)):
            later = self.m.inbox_items(inst, [n], 'app')[0]
            self.assertEqual(after[0]['id'], later[0]['id'])
            self.assertTrue(later[0]['reported'])
        attention = json.loads(self.b(r, 'ui', '--json'))['attention']
        item = next(i for i in attention if i['kind'] == 'blocked-overdue')
        self.assertTrue(item['escalated'])
        self.assertEqual(item['text'], text)
        self.assertIn('blocked-overdue', self.b(r, 'due', 'app'))

    def test_legacy_blocked_age_and_fixable_format_warning(self):
        r, inst, n = self.node()
        st = n['dir'] / 'status.md'
        with st.open('a') as f:
            f.write('\n## Blocked\n- Need a choice\n')
        self.git('add', '.', cwd=r)
        self.git('commit', '-qm', 'record need', cwd=r)
        intro = inst.now_dt()
        with mock.patch.object(inst, 'now_dt', return_value=intro + datetime.timedelta(hours=49)):
            self.assertTrue(self.m.blocked_entries(inst, n)[0]['escalated'])
        self.assertIn('missing valid since:', self.b(r, 'check'))
        self.assertIn("doesn't say what clears it", self.b(r, 'check'))
        # Empty placeholder bullets never become decisions.
        st.write_text(self.m.FACTS_RE.search(st.read_text()).group() + '\n## Blocked\n- None.\n')
        self.assertEqual(self.m.blocked_entries(inst, n), [])

    def test_code_setup_warns_without_stranding_jobs_and_accepts_bound_tick(self):
        r, inst, n = self.node()
        home = self.repo(self.tmp / 'source')
        self.b(r, 'set', 'app', 'home=' + str(home), 'coordinator=fixture-coordinator')
        out = self.b(r, 'wt', 'add', 'app', 'job-first')
        self.assertIn('needs verify=<command>', out)
        self.assertIn('needs a coordinator tick', out)
        self.assertIn('worktree:', out)
        self.assertIn('needs verify=<command>', self.b(r, 'check'))
        self.b(r, 'set', 'app', 'verify=python3 -c "pass"')
        self.b(r, 'schedule', 'add', 'app-tick', '--when', 'every 15 minutes', '--node', 'app', '--brief', 'Run due and pending')
        self.assertIn('needs a coordinator tick', self.b(r, 'check'))
        self.b(r, 'schedule', 'bind', 'app-tick', 'fixture-host-id')
        self.assertNotIn('needs a coordinator tick', self.b(r, 'check'))
        self.assertNotIn('needs verify=<command>', self.b(r, 'check'))

    def test_dead_note_paths_node_and_home_urls_globs_commands(self):
        r, inst, n = self.node()
        home = self.repo(self.tmp / 'source', {'present.py': '# file\n'})
        self.b(r, 'set', 'app', 'home=' + str(home))
        (n['dir'] / 'local').mkdir()
        (n['dir'] / 'brief.md').write_text('`present.py` [local](local/) `gone/file.py:12`\n'
            '[lost](missing.md#heading) [web](https://example.test/x)\n'
            '`*.py` `src/{a,b}.py` `brain check` `https://example.test`\n')
        (n['dir'] / 'knowledge.md').write_text('[gone folder](absent/)\n')
        out = self.b(r, 'check')
        self.assertIn('dead path gone/file.py:12', out)
        self.assertIn('dead path missing.md#heading', out)
        self.assertIn('dead path absent/', out)
        self.assertNotIn('dead path present.py', out)
        self.assertNotIn('dead path local/', out)
        self.assertNotIn('dead path https:', out)
        self.assertNotIn('dead path *.py', out)
        self.assertNotIn('dead path brain check', out)

    def test_study_skips_code_setup_even_with_stack_and_git_home(self):
        r, inst, n = self.node()
        home = self.repo(self.tmp / 'source')
        self.b(r, 'set', 'app', 'home=' + str(home), 'stack=python')
        self.b(r, 'relabel', 'app', 'kind:study', 'type:code')
        out = self.b(r, 'check')
        self.assertNotIn('needs verify=<command>', out)
        self.assertNotIn('needs a coordinator tick', out)
        # Changing only the kind restores both warnings, despite the same home and stack.
        self.b(r, 'relabel', 'app', 'kind:code')
        out = self.b(r, 'check')
        self.assertIn('needs verify=<command>', out)
        self.assertIn('needs a coordinator tick', out)

    def test_inline_home_paths_skip_templates_routes_but_keep_missing_brain_files(self):
        r, inst, n = self.node()
        home = self.repo(self.tmp / 'source')
        (home / 'src').mkdir()
        (home / 'src/exists.py').write_text('# present\n')
        (home / 'sources').mkdir()
        (home / 'sources/missing.md').write_text('# only in home\n')
        (home / 'lost.md').write_text('# only in home\n')
        self.b(r, 'set', 'app', 'home=' + str(home))
        text = ('`src/domain/<agg>/` `src/<module>.py` `/api/v1` `/api/config.json`\n'
                '`src/exists.py:12` `./src/exists.py` `sources/missing.md`\n'
                '[lost Brain note](lost.md) `core/missing.md` `./knowledge.md`\n')
        for name in ('brief.md', 'knowledge.md'):
            (n['dir'] / name).write_text(text)
        out = self.b(r, 'check')
        for name in ('brief.md', 'knowledge.md'):
            for line, value in ((2, 'sources/missing.md'), (3, 'lost.md'), (3, 'core/missing.md')):
                self.assertIn(name + ':' + str(line) + ': dead path ' + value, out)
        for value in ('src/domain/<agg>/', 'src/<module>.py', '/api/v1', '/api/config.json',
                      'src/exists.py:12', './src/exists.py', './knowledge.md'):
            self.assertNotIn('dead path ' + value, out)
        # Removing the home file must turn the same references into warnings.
        (home / 'src/exists.py').unlink()
        out = self.b(r, 'check')
        self.assertIn('dead path src/exists.py:12', out)
        self.assertIn('dead path ./src/exists.py', out)

    def test_unbound_home_skips_project_paths_and_still_checks_brain_targets(self):
        r, inst, n = self.node()
        self.b(r, 'set', 'app', 'home=' + str(self.tmp / 'unbound'))
        local_path = r / '.brain/local.json'
        local = json.loads(local_path.read_text())
        local['nodes']['app'].pop('home')
        local_path.write_text(json.dumps(local))
        (n['dir'] / 'knowledge.md').write_text(
            '`src/missing.py` `./src/missing.py` `README.md` `sources/lost.md`\n'
            '[lost](lost.md) `nodes/app/missing.md`\n')
        inst = self.m.Instance(r)
        self.assertIsNone(inst.node('app')['home_path'])
        problems = self.m.dead_note_paths(inst, inst.node('app'))
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(any('dead path sources/lost.md' in p for p in problems))
        self.assertTrue(any('dead path lost.md' in p for p in problems))
        self.assertTrue(any('dead path nodes/app/missing.md' in p for p in problems))

    def test_home_paths_use_their_own_root_without_unrelated_fallbacks(self):
        r, inst, n = self.node()
        (n['dir'] / 'src').mkdir()
        (n['dir'] / 'src/only-node.py').write_text('# node file\n')
        (r / 'src').mkdir()
        (r / 'src/only-root.py').write_text('# root file\n')
        (n['dir'] / 'knowledge.md').write_text('`src/only-node.py` `src/only-root.py`\n')
        for home_setting in ('here', 'none'):
            self.b(r, 'set', 'app', 'home=' + home_setting)
            inst = self.m.Instance(r)
            problems = self.m.dead_note_paths(inst, inst.node('app'))
            self.assertEqual(len(problems), 1, problems)
            self.assertIn('dead path src/only-root.py', problems[0])
        home = self.repo(self.tmp / 'source')
        self.b(r, 'set', 'app', 'home=' + str(home))
        inst = self.m.Instance(r)
        problems = self.m.dead_note_paths(inst, inst.node('app'))
        self.assertEqual(len(problems), 2, problems)

    def test_routes_preserve_missing_absolute_brain_files_with_bound_or_unbound_home(self):
        r, inst, n = self.node()
        home = self.repo(self.tmp / 'source')
        missing = n['dir'] / 'gone.md'
        (n['dir'] / 'knowledge.md').write_text('`/api/v1` `' + str(missing) + '`\n')
        for bound in (True, False):
            with self.subTest(bound=bound):
                n.update(external=True, home_path=home if bound else None)
                problems = self.m.dead_note_paths(inst, n)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn('dead path ' + str(missing), problems[0])

    def test_brain_note_paths_keep_child_parent_and_generated_file_checks(self):
        r, inst, n = self.node()
        home = self.repo(self.tmp / 'source')
        self.b(r, 'new', 'app/kid', '--title', 'Kid')
        self.b(r, 'new', 'sibling', '--title', 'Sibling')
        (n['dir'] / 'knowledge.md').write_text(
            '`nodes/kid/status.md` [child](nodes/kid/status.md)\n'
            '`../sibling/brief.md` `../sibling/missing.md`\n'
            '`job-trains/missing.json` `./role.md` `./lessons.md` `./handover.md`\n')
        # Home files must not mask missing node files, including new core-generated names.
        (home / 'job-trains').mkdir()
        (home / 'job-trains/missing.json').write_text('{}\n')
        for name in ('role.md', 'lessons.md', 'handover.md'):
            (home / name).write_text('# only at home\n')
        for bound in (True, False):
            with self.subTest(bound=bound):
                n.update(external=True, home_path=home if bound else None)
                problems = self.m.dead_note_paths(inst, n)
                self.assertEqual(len(problems), 5, problems)
                for value in ('../sibling/missing.md', 'job-trains/missing.json',
                              './role.md', './lessons.md', './handover.md'):
                    self.assertTrue(any('dead path ' + value in p for p in problems), value)

    def test_brain_root_metadata_is_not_masked_by_home_or_skipped_when_unbound(self):
        r, inst, n = self.node()
        home = self.repo(self.tmp / 'source', {'registry.md': '# home registry\n'})
        (n['dir'] / 'knowledge.md').write_text('`./registry.md` `./instance.md`\n')
        n.update(external=True, home_path=home)
        self.assertEqual(self.m.dead_note_paths(inst, n), [])
        (r / 'registry.md').unlink()
        for bound in (True, False):
            with self.subTest(bound=bound):
                n['home_path'] = home if bound else None
                problems = self.m.dead_note_paths(inst, n)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn('dead path ./registry.md', problems[0])

    def test_routing_rules_keep_sync_and_design_owned(self):
        role = (CORE / 'roles/coordinator.md').read_text()
        home = (CORE / 'skills/home-job/SKILL.md').read_text()
        tick = (CORE / 'adapters/hosts/t3.md').read_text()
        sync = (CORE / 'skills/node-sync/SKILL.md').read_text()
        self.assertIn('root never runs child sync', role)
        self.assertIn('ui-designer specialist (§0)', role)
        self.assertIn('before any UI code', home)
        self.assertIn('accepted design', home)
        self.assertIn('root never runs child sync', tick)
        self.assertIn('only for its own node', sync)
        check = (CORE / 'skills/check/SKILL.md').read_text()
        self.assertIn('a recorded coordinator tick', check)
        self.assertNotIn('locally bound coordinator tick', check)
        changes = (CORE / 'CHANGES.md').read_text()
        self.assertIn('Legacy age inference is approximate', changes)
        self.assertIn('original waiting time explicitly', changes)
        brief = (CORE / 'skills/node-brief/SKILL.md').read_text()
        self.assertIn('Send only a behind signal', brief)
        self.assertIn('Do not wait for sync during the daily brief', brief)
        self.assertNotIn('sync --all', brief)
        self.assertNotIn('`core/bin/brain sync <child>`', brief)
        # Reconciliation must retain the host-question job's descendant coverage too.
        for clause in ('every descendant node', "each child's coordinator thread id",
                       'without open jobs', 'provider it was created under'):
            self.assertIn(clause, tick)
        self.assertIn('Root also checks every descendant coordinator', role)
        self.assertIn('not during a daily brief: report the snapshot as behind', role)


    def test_wrapped_legacy_age_survives_refresh_and_history_is_read_once(self):
        r, inst, n = self.node()
        with (n['dir'] / 'status.md').open('a') as f:
            f.write('\n## Blocked\n- Choose the plan, waiting on owner;\n  clears when approved\n')
        dates = {k: self.env.get(k) for k in ('GIT_AUTHOR_DATE', 'GIT_COMMITTER_DATE')}
        self.env.update(GIT_AUTHOR_DATE='2020-01-01T00:00:00Z', GIT_COMMITTER_DATE='2020-01-01T00:00:00Z')
        self.git('add', '.', cwd=r)
        self.git('commit', '-qm', 'record wrapped need', cwd=r)
        for k, v in dates.items():
            if v is None:
                self.env.pop(k, None)
            else:
                self.env[k] = v
        self.b(r, 'status', 'app', '--refresh')
        inst = self.m.Instance(r)
        n = inst.node('app')
        with mock.patch.object(self.m, 'git', wraps=self.m.git) as git:
            first = self.m.blocked_entries(inst, n)
            second = self.m.blocked_entries(inst, n)
            meta = self.m.blocked_metadata(inst, n, 'blocked-overdue', first[0]['text'])
        logs = [c for c in git.call_args_list if c.args[:2] == ('log', '--reverse')]
        self.assertEqual(len(logs), 1)
        self.assertEqual(first[0]['since'], '2020-01-01T00:00:00+00:00')
        self.assertTrue(first[0]['escalated'])
        self.assertTrue(second[0]['escalated'])
        self.assertTrue(meta['escalated'])

    def test_dead_path_false_positives_and_brain_relative_paths(self):
        r, inst, n = self.node()
        (n['dir'] / 'knowledge.md').write_text(
            '`Next.js` `v1.2` `origin/main` `@acme/ui` `text/html` `and/or`\n'
            '`core/bin/brain` `nodes/app/brief.md` `./missing.py`\n'
            '`next.js` `vue.js` `chart.js` `index.ts` `utils.py` `package.json`\n'
            '[a title](lost.md "Local file")\n')
        home = self.repo(self.tmp / 'source', {'README.md': '# source\n'})
        (home / 'apps/web').mkdir(parents=True)
        (home / 'apps/web/package.json').write_text('{}\n')
        self.b(r, 'set', 'app', 'home=' + str(home))
        inst = self.m.Instance(r)
        problems = self.m.dead_note_paths(inst, inst.node('app'))
        self.assertEqual(len(problems), 2, problems)
        self.assertIn('dead path ./missing.py', problems[0])
        self.assertIn('dead path lost.md', problems[1])

    def test_bound_coordinator_wake_satisfies_tick_warning_without_a_schedule(self):
        r, inst, n = self.node()
        self.b(r, 'set', 'app', 'stack=python', 'verify=python3 -c "pass"', 'wake=exit 0')
        self.assertIn('needs a coordinator tick', self.b(r, 'check'))
        self.b(r, 'set', 'app', 'coordinator=fixture-coordinator')
        self.assertNotIn('needs a coordinator tick', self.b(r, 'check'))
        self.b(r, 'set', 'app', 'wake=')
        self.assertIn('needs a coordinator tick', self.b(r, 'check'))
        self.b(r, 'set', 'root', 'wake_default=exit 0')
        self.assertNotIn('needs a coordinator tick', self.b(r, 'check'))

    def test_second_machine_uses_recorded_tick_without_duplicate_binding(self):
        r, inst, n = self.node()
        self.b(r, 'set', 'app', 'stack=python', 'verify=python3 -c "pass"')
        self.b(r, 'schedule', 'add', 'app-tick', '--when', 'every 15 minutes', '--node', 'app', '--brief', 'Run due')
        # Shared schedule, no local coordinator: another machine owns its binding.
        self.assertNotIn('needs a coordinator tick', self.b(r, 'check'))
        self.b(r, 'set', 'app', 'coordinator=fixture-coordinator')
        self.assertIn('needs a coordinator tick', self.b(r, 'check'))
        self.b(r, 'schedule', 'bind', 'app-tick', 'fixture-host-id')
        self.assertNotIn('needs a coordinator tick', self.b(r, 'check'))

    def test_archived_judgement_is_not_subject_to_new_hygiene_checks(self):
        r, inst, n = self.node()
        self.b(r, 'archive', 'app')
        st = n['dir'] / 'status.md'
        st.write_text('## Now\n' + '- Historical item\n' * 31 + '\n## Blocked\n- Old choice\n')
        (n['dir'] / 'knowledge.md').write_text('`./old/missing.py`\n')
        before = st.read_bytes()
        out = self.b(r, 'check')
        self.assertNotIn('Now has', out)
        self.assertNotIn('missing valid since', out)
        self.assertNotIn('generated facts block', out)
        self.assertNotIn('dead path', out)
        self.assertNotIn("doesn't say what clears it", out)
        self.assertEqual(st.read_bytes(), before)

    def test_empty_blocked_forms_do_not_escalate_but_real_needs_do(self):
        r, inst, n = self.node()
        with (n['dir'] / 'status.md').open('a') as f:
            f.write('\n## Blocked\n- (none)\n- None right now\n- Nothing blocked.\n')
        self.assertEqual(self.m.blocked_entries(inst, n), [])
        self.assertNotIn('missing valid since', self.b(r, 'check'))
        self.assertNotIn("doesn't say what clears it", self.b(r, 'check'))
        self.assertFalse(self.m.empty_blocked('Nothing builds — waiting on owner'))

    def test_overdue_only_ticks_route_new_needs_without_status_commits(self):
        r, inst, n = self.node()
        self.block(n, '2020-01-01T00:00:00Z')
        before = (n['dir'] / 'status.md').read_bytes()
        commits = self.git('rev-list', '--count', 'HEAD', cwd=r)
        first = json.loads(self.b(r, 'inbox', 'app', '--new', '--json'))
        self.assertTrue(any(i['kind'] == 'blocked-overdue' for i in first))
        self.assertEqual(json.loads(self.b(r, 'inbox', 'app', '--new', '--json')), [])
        after = (n['dir'] / 'status.md').read_bytes()
        self.assertEqual(self.m.FACTS_RE.sub('', after.decode()), self.m.FACTS_RE.sub('', before.decode()))
        self.assertEqual(self.git('rev-list', '--count', 'HEAD', cwd=r), commits)
        for name in ('roles/coordinator.md', 'adapters/hosts/t3.md'):
            self.assertIn('committing unchanged status', (CORE / name).read_text())



if __name__ == "__main__":
    unittest.main()


class RuleActivationDue(unittest.TestCase):
    def test_b6_one_stable_snapshot_due_episode(self):
        patch = mock.patch.object(sys, 'path', [str(CORE / 'lib'), *sys.path])
        with patch:
            from brain import status, jobs, proposals, watch
            from brain.instance import Instance
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'instance.md').write_text('---\noperator: alice\nprincipals: alice\n---\n')
                (root / 'node.md').write_text('---\nid: root\nowners: alice\n---\n')
                (root / 'sources').mkdir()
                source = root / 'sources/import-proposals.md'
                source.write_text('Unreviewed rules.\n')
                inst = Instance(root)
                n = inst.node('root')
                state = {'jobs': [], 'worktrees': {}, 'handbacks': {}, 'inbox': []}
                evidence = (state, None, None)
                with mock.patch.object(status._m_rotation, 'rotation_due', return_value=None), \
                     mock.patch.object(status, 'blocked_entries', return_value=[]), \
                     mock.patch.object(watch, 'watch_job_stalls', return_value=[]):
                    def rule_items():
                        return [i for i in status.due_items(inst, n, evidence=evidence) if i['kind'] == 'rule-activation-review']
                    first = rule_items()
                    self.assertEqual(len(first), 1)
                    expected = jobs.digest(['root', 'rule-activation-review', proposals.pending_rule_snapshot(inst, n)['digest']])
                    self.assertEqual(first[0]['episode'], expected)
                    self.assertEqual(rule_items(), first)
                    source.write_text('A different unreviewed snapshot.\n')
                    self.assertNotEqual(rule_items()[0]['episode'], expected)
                    rejected = ('---\nstatus: rejected\nreviewed_by: alice\nreviewed_at: ' + inst.now() +
                                '\n---\nA rejected snapshot.\n')
                    source.write_text(rejected)
                    self.assertEqual(rule_items(), [])
                    source.write_text(rejected.replace('status: rejected', 'status: reviewed'))
                    self.assertEqual(len(rule_items()), 1)
                    crlf = rejected.replace('\n', '\r\n').encode('utf-8')
                    source.write_bytes(crlf)
                    self.assertEqual(rule_items(), [])
                    self.assertEqual(source.read_bytes(), crlf)


class RenderedFingerprint(Scenario):
    fixture_templates = True
    fixture_init_in_process = True
    """Native fabricated readiness, plus focused render-boundary telemetry probes."""

    def setUp(self):
        super().setUp()
        self.env['BRAIN_NO_UPDATE_CHECK'] = '1'
        env = mock.patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.m = core_loader.SourceFileLoader('rendered_facts', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('oak')
        self.home = self.repo(self.tmp / 'home')
        self.b(self.root, 'new', 'sample', '--title', 'Sample', '--home', str(self.home))
        self.inst = self.m.Instance(self.root)
        self.n = self.inst.node('sample')
        self.branch = 'job/example'
        self.wt = self.tmp / 'worker'
        self.git('worktree', 'add', '-qb', self.branch, str(self.wt), cwd=self.home)
        self.n['fm']['verify'] = 'true'
        self.n['local']['workbench'] = str(self.tmp / 'workbench')
        self.w = {'path': str(self.wt), 'port': 4100}
        self.n['local']['worktrees'] = {self.branch: self.w}
        head, main = self.m.ready_refs(self.n, self.branch)
        self.w['start'] = head
        self.row = dict.fromkeys(self.m.JOB_COLS, '')
        self.row.update(Job=self.branch, Started=(self.inst.now_dt() - datetime.timedelta(hours=1)).isoformat(),
                        Runtime='codex', Reviews='1')
        self.m.write_jobs(self.inst, self.n, [self.row])
        self.packet = dict(version=1, node=self.n['id'], branch=self.branch, head=head,
                           main=main, integration_base=main, verify_command='true', verify_result=0,
                           inputs=self.m.verify_inputs(self.n, self.w), review_rounds=1,
                           checked_at=self.inst.now_dt().isoformat(),
                           verify_port_context=self.m.verify_port_context(
                               self.n, self.m.verify_env(self.n, self.branch, self.w)))
        self.signal = dict(version=3, job=self.branch, started=self.row['Started'], state='done',
                           summary='Synthetic completed result', lessons='none: fixture',
                           ready=self.packet, wake_id='a' * 32)
        self.hb = self.m.handback_file(self.n, self.row)
        self.hb.parent.mkdir(exist_ok=True)
        self.save_signal()
        self.assertTrue(self.m.ready_state(self.inst, self.n, self.branch)['ready'])

    def save_signal(self):
        self.hb.write_text(json.dumps(self.signal))

    def facts(self):
        state, _, _ = self.m.due_evidence(self.inst, self.n)
        return self.m.status_facts(self.inst, self.n, state,
                                   self.m.facts_meta(self.n).get('merged_before', []))[0]

    def assert_change(self, change, rejected=False):
        self.m.refresh_status(self.inst, self.n)
        before = self.m.due_evidence(self.inst, self.n)[1]
        old_facts = self.facts()
        self.assertIsNone(self.m.status_lag(self.inst, self.n))
        change()
        if rejected:
            self.assertFalse(self.m.ready_state(self.inst, self.n, self.branch, display=True)['ready'])
        self.assertNotEqual(old_facts, self.facts(), 'semantic mutation must be visible')
        self.assertNotEqual(before, self.m.due_evidence(self.inst, self.n)[1])
        self.assertIsNotNone(self.m.status_lag(self.inst, self.n))
        self.assertIn('stale-status', [i['kind'] for i in self.m.due_items(self.inst, self.n)])
        self.m.refresh_status(self.inst, self.n)
        self.assertIsNone(self.m.status_lag(self.inst, self.n))
        self.assertNotIn('stale-status', [i['kind'] for i in self.m.due_items(self.inst, self.n)])

    # (a) The twelve independently reproduced misses from the previous design.
    # The first also provides (b), the current-node verify regression proof.
    def test_a01_b_current_verify(self):
        self.assert_change(lambda: self.n['fm'].__setitem__('verify', 'false'), rejected=True)

    def test_a02_current_verify_export_port(self):
        self.assert_change(lambda: self.n['fm'].__setitem__('verify_export_port', 'true'), rejected=True)

    def test_a03_retired_rehearse_option_does_not_restore_a_gate(self):
        # The retained test predates removal of rehearsal as a readiness gate.
        self.m.refresh_status(self.inst, self.n)
        before = self.m.due_evidence(self.inst, self.n)[1]
        self.n['fm']['rehearse'] = 'required'
        self.assertTrue(self.m.ready_state(self.inst, self.n, self.branch, display=True)['ready'])
        self.assertEqual(before, self.m.due_evidence(self.inst, self.n)[1])
        self.assertIsNone(self.m.status_lag(self.inst, self.n))

    def test_a04_current_verify_lanes(self):
        self.assert_change(lambda: self.n['fm'].__setitem__('verify_lanes', json.dumps([
            dict(name='unit', command='true', inputs=['README.md'])])), rejected=True)

    def test_a05_worktree_checkout_branch(self):
        self.assert_change(lambda: self.git('checkout', '--detach', '-q', cwd=self.wt), rejected=True)

    def test_a06_worktree_clean(self):
        self.assert_change(lambda: (self.wt / 'README.md').write_text('synthetic dirty change'), rejected=True)

    def test_a07_current_verify_inputs(self):
        (self.home / '.git/info/exclude').write_text('.env\n')
        self.assert_change(lambda: (self.wt / '.env').write_text('SYNTHETIC=1\n'), rejected=True)

    def test_a08_train_ejected(self):
        path = self.n['dir'] / 'job-trains' / 'sample.json'
        path.parent.mkdir()
        self.assert_change(lambda: path.write_text(json.dumps(dict(version=1, ejected=[dict(
            key=self.m.job_key(self.row), packet=self.m.ready_fingerprint(self.n, self.row, self.packet))]))),
            rejected=True)

    def test_a09_exact_restart_acknowledgement(self):
        self.signal.update(state='blocked', restart='compactions', restart_id='b' * 32)
        self.save_signal()
        self.assert_change(lambda: self.w.update(restart_ack=self.m.digest([
            self.m.job_key(self.row), self.signal])))
        self.assertTrue(self.m.restart_acknowledged(self.n, self.row, self.signal))

    def test_a10_versionless_handback_legacy_head(self):
        self.signal = dict(job=self.branch, started=self.row['Started'], state='done',
                           summary='Synthetic legacy completion')
        self.save_signal()
        path = self.m.ready_path(self.inst, self.n, self.branch)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.packet))
        self.assertTrue(self.m.ready_state(self.inst, self.n, self.branch)['ready'])
        def change():
            self.packet['head'] = 'f' * 40
            path.write_text(json.dumps(self.packet))
        self.assert_change(change, rejected=True)

    def test_a11_switch_semantic_payload(self):
        self.row['Switches'] = '2026-01-15T09:05:00+00:00 codex -> claude: provider limit'
        self.m.write_jobs(self.inst, self.n, [self.row])
        def change():
            self.row['Switches'] = '2026-01-15T09:05:00+00:00 codex -> local-model: provider failure'
            self.m.write_jobs(self.inst, self.n, [self.row])
        self.assert_change(change)

    def test_a12_rehearsal_delivery_status(self):
        self.packet['rehearsal'] = dict(version=1, passed=True, regressions=[],
                                        delivery=[dict(coordinator=0, status=200)])
        self.save_signal()
        def change():
            self.packet['rehearsal']['delivery'][0]['status'] = 503
            self.save_signal()
        self.assert_change(change)

    def test_c_native_restart_counters(self):
        # Restart counters describe an active worker, before its terminal handback.
        self.hb.unlink()
        usage = {'compactions': 4, 'processed': 1000, 'subagents': 0, 'sub_share': 0}
        with mock.patch.object(self.m, 'live_usage', return_value=usage):
            self.m.refresh_status(self.inst, self.n)
            before = self.m.due_evidence(self.inst, self.n)[1]
            old_facts = self.facts()
            usage.update(compactions=8, processed=2000)
            self.assertNotEqual(old_facts, self.facts())
            self.assertEqual(before, self.m.due_evidence(self.inst, self.n)[1])
            self.assertIsNone(self.m.status_lag(self.inst, self.n))
            self.m.refresh_status(self.inst, self.n)
            self.assertEqual(before, self.m.facts_meta(self.n)['fingerprint'])
            self.assertIsNone(self.m.status_lag(self.inst, self.n))

    def test_c_rendered_telemetry_patterns(self):
        # Probe the shared render boundary with exact production-shaped telemetry.
        # Every pair independently exercises comparison AND refresh, with no new API
        # dependency, so the same tests run unchanged against the requested old core.
        pairs = [
            ('4 tokens', '5 tokens'), ('999 tokens', '1.01k tokens'),
            ('1M tokens', '2B tokens'), ('1,000 processed tokens', '2,000 processed tokens'),
            ('1M tokens, over twice the median of 2M', '2M tokens, over twice the median of 3M'),
            ('token budget 1000', 'token budget 2000'), ('over its 2M-token budget', 'over its 3M-token budget'),
            ('sub-agents spent 51% of its tokens', 'sub-agents spent 72% of its tokens'),
            ('token meter: 51%', 'token meter: 62%'),
            ('1M tokens processed (spread 1M–2M), of them 2M fresh input and output',
             '2M tokens processed (spread 2M–3M), of them 3M fresh input and output'),
            ('open 12.3 h, over twice the median of 6.1 h', 'open 13.4 h, over twice the median of 6.2 h'),
            ('age 1.2d', 'age 1.3d'), ('duration 12 seconds', 'duration 13 seconds'),
            ('elapsed 59 s', 'elapsed 1 min'), ('duration 2 minutes', 'duration 3 minutes'),
            ('median 6.1 h, spread 1–9 h; of which 2 h active',
             'median 7.2 h, spread 2–10 h; of which 3 h active'),
            ('Facts refreshed: 2026-01-15 09:00 +00:00', 'Facts refreshed: 2026-01-16 10:01 +00:00'),
            ('Facts refreshed: 2026-01-15', 'Facts refreshed: 2026-01-16'),
            ('4 compactions; 1 restarts', '5 compactions; 2 restarts'),
            ('- machine: load/CPU 1/5/15m 0.12/0.13/0.14; free memory 32 GB; thermal normal',
             '- machine: load/CPU 1/5/15m 0.32/0.33/0.34; free memory 30 GB; thermal warm'),
        ]
        original = self.m.status_facts
        for old, new in pairs:
            with self.subTest(old=old):
                value = [old]
                def render(*args, **kwargs):
                    lines, merged = original(*args, **kwargs)
                    return lines + ['', value[0]], merged
                with mock.patch.object(self.m, 'status_facts', side_effect=render):
                    self.m.refresh_status(self.inst, self.n)
                    before = self.m.due_evidence(self.inst, self.n)[1]
                    value[0] = new
                    self.assertEqual(before, self.m.due_evidence(self.inst, self.n)[1])
                    self.assertIsNone(self.m.status_lag(self.inst, self.n))
                    self.m.refresh_status(self.inst, self.n)
                    self.assertEqual(before, self.m.facts_meta(self.n)['fingerprint'])
                    self.assertIsNone(self.m.status_lag(self.inst, self.n))

    def test_semantic_numbers_and_recorded_dates_stay_verbatim(self):
        samples = [
            '- job/123: merged (4 commits); PR #812; head 123456789abcdef',
            'Closed jobs with unsettled threads: 12', 'Due: 4 · Needs the human: 2',
            '- 2026-01-15 09:05 +00:00: job/123 (2 commits)',
            'started 2026-01-15 09:05 +00:00; 3 reviews',
            'verify command: sleep 12; timeout 34s; port 4100',
            '| job/123 | queued | 1234567 | 2; owner 123 | wait |',
            'delivery: [{"coordinator": 0, "status": 503}]',
        ]
        self.assertEqual(self.m.normalise_status_facts(samples), samples)
        before = self.m.status_fingerprint(samples)
        for index, text in enumerate(samples):
            with self.subTest(text=text):
                changed = list(samples)
                changed[index] = re.sub(r'\d', lambda match: str((int(match[0]) + 1) % 10), text)
                self.assertNotEqual(before, self.m.status_fingerprint(changed))
