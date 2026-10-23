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


class Workbench(Scenario):
    def test_failed_setup_keeps_the_worktree_and_reports_failure(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, 'set', 'app', 'verify_export_port=true')
        self.env['PORT'] = '9200'
        self.env.pop('PORTS', None)
        self.env.pop('PORT_LAST', None)
        (r / "nodes" / "app" / "worktree-setup.sh").write_text(
            'set -eu\n[ "$PORT" = 9200 ]\n[ "$PORTS" = "$BRAIN_PORTS" ]\n'
            '[ "$PORT_LAST" = "$BRAIN_PORT_LAST" ]\necho partial > partial.txt\nexit 7\n')
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "wt", "add", "app", "job-fails"],
                           cwd=r, env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 7, p.stdout + p.stderr)
        self.assertIn("setup: FAILED", p.stdout)
        self.assertNotIn("launch a worker there", p.stdout)
        local = json.loads((r / ".brain" / "local.json").read_text())
        path = Path(local["nodes"]["app"]["worktrees"]["job-fails"]["path"])
        self.assertEqual((path / "partial.txt").read_text(), "partial\n")
        self.assertIn("job-fails", self.b(r, "wt", "list", "app"))

    def test_worktrees_with_setup_ports_and_prune(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        origin = self.tmp / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(origin), cwd=self.tmp)
        home = self.repo(self.tmp / "code" / "app", {"README.md": "app\n", ".gitignore": ".env\n"})
        self.git("remote", "add", "origin", str(origin), cwd=home)
        self.git("push", "-q", "-u", "origin", "main", cwd=home)
        self.git("remote", "set-head", "origin", "main", cwd=home)
        (home / ".env").write_text("SECRET=1\n")
        self.b(r, "new", "app", "--title", "App", "--owners", "team", "--home", str(home))
        for key in ('PORT', 'PORTS', 'PORT_LAST'):
            self.env.pop(key, None)
        (r / "nodes" / "app" / "worktree-setup.sh").write_text(
            'set -eu\n[ "${PORT+x}${PORTS+x}${PORT_LAST+x}" = "" ]\n'
            'cp "$MAIN/.env" .env\necho "PORT=$BRAIN_PORT" >> .env\n')

        out = self.b(r, "wt", "add", "app", "feat-a")
        wa = self.tmp / "code" / "app-work" / "worktrees" / "feat-a"
        self.assertIn("setup: ok", out)
        self.assertIn("SECRET=1", (wa / ".env").read_text())
        self.b(r, "wt", "add", "app", "feat-b")
        wb_ = wa.parent / "feat-b"
        ports = {(w / ".env").read_text().split("PORT=")[1].strip() for w in (wa, wb_)}
        self.assertEqual(len(ports), 2, "each worktree gets its own port")
        self.assertIn("✓", self.b(r, "check"))                  # nothing in the home, nothing in the instance

        # feat-a is done and merged; feat-b has unfinished work; feat-c is new and untouched
        (wa / "a.txt").write_text("a\n")
        self.git("add", "a.txt", cwd=wa); self.git("commit", "-qm", "A", cwd=wa)
        self.git("merge", "-q", "feat-a", cwd=home); self.git("push", "-q", "origin", "main", cwd=home)
        (wb_ / "wip.txt").write_text("wip\n")
        self.b(r, "wt", "add", "app", "feat-c")
        self.assertIn("would remove: feat-a", self.b(r, "wt", "prune", "app", "--dry-run"))
        out = self.b(r, "wt", "prune", "app")
        self.assertIn("removed: feat-a", out)
        self.assertIn("feat-b (uncommitted work)", out)
        self.assertIn("feat-c (not merged)", out)
        self.assertFalse(wa.exists())
        self.assertTrue((wb_ / "wip.txt").exists())
        self.b(r, "wt", "rm", "app", "feat-b", ok=False)        # uncommitted work is never discarded silently
        self.assertIn("feat-c", self.b(r, "wt", "list"))

    def test_add_reuses_cached_submodule_gitdir_before_setup_without_remote(self):
        r = self.init("alice")
        source = self.repo(self.tmp / "module-source")
        home = self.repo(self.tmp / "app")
        self.git("submodule", "add", "--name", "cached-module", str(source), "vendor/module", cwd=home)
        self.git("commit", "-qam", "Cache submodule", cwd=home)
        cached = home / ".git/modules/cached-module"
        before = (cached / "HEAD").read_bytes()
        before_config = (cached / "config").read_bytes()
        source.rename(self.tmp / "remote-unavailable")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes/app/worktree-setup.sh").write_text("git submodule update --init --recursive\n")
        out = self.b(r, "wt", "add", "app", "job-cached")
        wt = self.tmp / "app-work/worktrees/job-cached"
        self.assertIn("reused submodule: vendor/module", out)
        self.assertIn("setup: ok", out)
        self.assertEqual((wt / "vendor/module/README.md").read_text(), "hello\n")
        common = self.git("rev-parse", "--git-common-dir", cwd=wt / "vendor/module").strip()
        module_gitdir = (wt / "vendor/module" / common).resolve()
        self.assertNotEqual(module_gitdir, cached.resolve())
        self.assertEqual((module_gitdir / "objects/info/alternates").read_text().strip(), str(cached / "objects"))
        self.assertEqual((cached / "HEAD").read_bytes(), before)
        self.assertEqual(self.git("status", "--porcelain", cwd=wt), "")
        # A second job gets independent metadata and the requested detached commit.
        self.b(r, "wt", "add", "app", "job-second", "--no-setup")
        other = wt.parent / "job-second/vendor/module"
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=other),
                         self.git("rev-parse", "HEAD", cwd=home / "vendor/module"))
        self.assertNotEqual(self.git("rev-parse", "--git-dir", cwd=other),
                            self.git("rev-parse", "--git-dir", cwd=wt / "vendor/module"))
        self.assertEqual((cached / "HEAD").read_bytes(), before)
        self.assertEqual((cached / "config").read_bytes(), before_config)

    def test_prune_reports_submodules_and_retains_shared_module_data(self):
        r = self.init("alice")
        source = self.repo(self.tmp / "module-source")
        home = self.repo(self.tmp / "app")
        self.git("submodule", "add", "--name", "shared-name", str(source), "vendor/module", cwd=home)
        self.git("commit", "-qam", "Add module", cwd=home)
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "wt", "add", "app", "job-module")
        wt = self.tmp / "app-work/worktrees/job-module"
        self.git("submodule", "update", "--init", cwd=wt)
        self.git("commit", "--allow-empty", "-qm", "Finish job", cwd=wt)
        self.git("merge", "--ff-only", "job-module", cwd=home)
        shared = home / ".git/modules/shared-name"
        before = (shared / "HEAD").read_bytes()
        for flags in (("--dry-run",), ()):
            out = self.b(r, "wt", "prune", "app", "--offline", *flags)
            self.assertIn("job-module (submodules retained: vendor/module)", out)
            self.assertIn("none", out)
            self.assertTrue((wt / "vendor/module/README.md").exists())
            self.assertEqual((shared / "HEAD").read_bytes(), before)
        self.assertIn("job-module", self.b(r, "wt", "list", "app"))

    def test_reports_have_a_predictable_place(self):
        r = self.init("alice")
        self.b(r, "new", "site", "--title", "Site")
        d = Path(self.b(r, "report", "site", "UI audit: mobile").strip())
        self.assertEqual(d.parent, r / "nodes" / "site" / "reports")
        self.assertTrue(d.name.endswith("-ui-audit-mobile"))
        for h in ("## Answer", "## Findings", "## Controls", "## Not run", "## How to reproduce"):
            self.assertIn(h, (d / "index.md").read_text())
        self.assertIn("UI audit: mobile", self.b(r, "reports"))
        self.assertIn("scripts/", self.b(r, "workbench", "site"))

    def test_a_workbench_inside_the_instance_is_refused(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        home = self.repo(self.tmp / "h")
        self.b(r, "new", "x", "--title", "X", "--owners", "team", "--home", str(home))
        self.b(r, "set", "x", "workbench=" + str(r / "wb"))
        self.assertIn("workbench is inside the instance", self.b(r, "check", ok=False))

class PruneSafety(AutonomyProject):
    def close_job(self, r, branch='job-a'):
        ledger = r / 'nodes/app/job-ledger.md'
        lines = ledger.read_text().splitlines()
        for i, line in enumerate(lines):
            cells = line.split('|')
            if len(cells) > 4 and cells[1].strip() == branch:
                cells[3] = ' 2025-01-01T00:00:00+00:00 '
                cells[5] = ' merged '
                lines[i] = '|'.join(cells)
        ledger.write_text('\n'.join(lines) + '\n')

    def merged(self):
        r, home, wt = self.project()
        (wt / 'done.txt').write_text('done\n')
        self.git('add', '.', cwd=wt)
        self.git('commit', '-qm', 'finished', cwd=wt)
        self.git('merge', '-q', 'job-a', cwd=home)
        return r, home, wt

    def test_finished_prunes_and_closes_own_open_row(self):
        r, home, wt = self.merged()
        self.assertIn('removed: job-a', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertFalse(wt.exists())
        self.assertIn('merged', self.b(r, 'jobs', 'app', '--branch', 'job-a'))

    def test_different_open_job_claiming_path_survives(self):
        r, home, wt = self.merged()
        self.b(r, 'wt', 'add', 'app', 'other-job')
        local_path = r / '.brain/local.json'
        local = json.loads(local_path.read_text())
        local['nodes']['app']['worktrees']['other-job']['path'] = str(wt)
        local_path.write_text(json.dumps(local))
        self.assertIn('open ledger job other-job', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertIn('open ledger job other-job', self.b(r, 'wt', 'rm', 'app', 'job-a', '--force', ok=False))
        self.assertTrue(wt.exists())

    def test_reused_folder_followup_survives_and_other_finished_job_prunes(self):
        r, home, wt = self.merged()
        self.close_job(r)
        self.b(r, 'wt', 'add', 'app', 'followup')
        local_path = r / '.brain/local.json'
        local = json.loads(local_path.read_text())
        followup = local['nodes']['app']['worktrees']['followup']
        self.git('worktree', 'remove', followup['path'], cwd=home)
        self.git('switch', 'followup', cwd=wt)
        followup['path'] = str(wt)
        local_path.write_text(json.dumps(local))
        self.b(r, 'wt', 'add', 'app', 'finished')
        done = wt.parent / 'finished'
        (done / 'another').write_text('done')
        self.git('add', '.', cwd=done)
        self.git('commit', '-qm', 'another done', cwd=done)
        self.git('merge', '-q', 'finished', cwd=home)
        out = self.b(r, 'wt', 'prune', 'app', '--offline')
        self.assertIn('removed: finished', out)
        self.assertFalse(done.exists())
        self.assertIn('checked-out branch differs', out)
        self.assertTrue(wt.exists())
        self.assertIn('checked-out branch differs', self.b(r, 'wt', 'rm', 'app', 'job-a', '--force', ok=False))

    def test_detached_head_survives(self):
        r, home, wt = self.merged()
        self.close_job(r)
        self.git('checkout', '--detach', '-q', cwd=wt)
        self.assertIn('HEAD is detached', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertTrue(wt.exists())

    def test_live_binding_survives_but_terminal_binding_allows_cleanup(self):
        r, home, wt = self.merged()
        self.close_job(r)
        status = r / 'nodes/app/status.md'
        status.write_text('## Active work\n| Branch | Thread | PR | State |\n| job-a | fake-thread | - | working |\n')
        self.assertIn('live bound thread', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertIn('live bound thread', self.b(r, 'wt', 'rm', 'app', 'job-a', ok=False))
        status.write_text(status.read_text().replace('working', 'merged'))
        self.assertIn('removed: job-a', self.b(r, 'wt', 'prune', 'app', '--offline'))

    def test_add_refuses_owned_missing_folder_and_existing_folder(self):
        r, home, wt = self.project()
        self.git('worktree', 'remove', str(wt), cwd=home)
        self.assertIn('create a fresh worktree', self.b(r, 'wt', 'add', 'app', 'job/a', ok=False))
        self.assertFalse(wt.exists())
        wt.mkdir()
        self.assertIn('create a fresh worktree', self.b(r, 'wt', 'add', 'app', 'job/a', ok=False))

    def test_creation_sha_and_deleted_base_do_not_control_merge_status(self):
        for mode in ('sha', 'deleted'):
            with self.subTest(mode=mode):
                r = self.init(mode)
                home = self.repo(self.tmp / mode / 'repo')
                self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
                self.git('branch', 'job-base', cwd=home)
                base = self.git('rev-parse', 'HEAD', cwd=home).strip() if mode == 'sha' else 'job-base'
                self.b(r, 'wt', 'add', 'app', 'job-a', '--base', base)
                local = json.loads((r / '.brain/local.json').read_text())
                wt = Path(local['nodes']['app']['worktrees']['job-a']['path'])
                (wt / 'done').write_text('done')
                self.git('add', '.', cwd=wt)
                self.git('commit', '-qm', 'done', cwd=wt)
                self.git('merge', '-q', 'job-a', cwd=home)
                self.git('branch', '-D', 'job-base', cwd=home)
                due = json.loads(self.b(r, 'due', 'app', '--json'))
                self.assertTrue(any(item['kind'] == 'merged-job' and 'merged in local git' in item['text'] for item in due))
                self.b(r, 'status', 'app', '--refresh')
                self.assertIn('job-a: merged in local git', (r / 'nodes/app/status.md').read_text())
                state = json.loads(self.b(r, 'ui', '--json'))
                node = next(node for node in state['nodes'] if node['id'] == 'app')
                self.assertEqual(node['worktrees'][0]['ahead'], 0)
                self.assertIn('removed: job-a', self.b(r, 'wt', 'prune', 'app', '--offline'))
                self.assertFalse(wt.exists())

    def test_pr_mode_uses_remote_default_despite_stale_local_main(self):
        r, home, wt = self.project()
        origin = self.tmp / 'origin.git'
        self.git('init', '-q', '--bare', '-b', 'main', str(origin), cwd=self.tmp)
        self.git('remote', 'add', 'origin', str(origin), cwd=home)
        self.git('push', '-q', 'origin', 'main', cwd=home)
        self.git('remote', 'set-head', 'origin', 'main', cwd=home)
        self.b(r, 'set', 'app', 'merge=pr')
        (wt / 'done').write_text('done')
        self.git('add', '.', cwd=wt)
        self.git('commit', '-qm', 'done', cwd=wt)
        self.git('push', '-q', 'origin', 'job-a:main', cwd=wt)
        self.assertIn('removed: job-a', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertFalse(wt.exists())

    def test_different_open_row_for_same_branch_blocks_cleanup(self):
        r, home, wt = self.merged()
        ledger = r / 'nodes/app/job-ledger.md'
        text = ledger.read_text()
        row = next(line for line in text.splitlines() if line.startswith('| job-a |'))
        ledger.write_text(text + row + '\n')
        self.assertIn('open ledger job job-a', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertTrue(wt.exists())

    def test_local_mode_follows_origin_default_named_master(self):
        r, home, wt = self.project()
        self.git('branch', '-m', 'main', 'master', cwd=home)
        origin = self.tmp / 'origin.git'
        self.git('init', '-q', '--bare', '-b', 'master', str(origin), cwd=self.tmp)
        self.git('remote', 'add', 'origin', str(origin), cwd=home)
        self.git('push', '-q', 'origin', 'master', cwd=home)
        self.git('remote', 'set-head', 'origin', 'master', cwd=home)
        self.b(r, 'set', 'app', 'merge=local')
        (wt / 'done').write_text('done')
        self.git('add', '.', cwd=wt)
        self.git('commit', '-qm', 'done', cwd=wt)
        self.git('merge', '-q', 'job-a', cwd=home)
        self.assertIn('merged in local git', self.b(r, 'due', 'app'))
        self.assertIn('removed: job-a', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertFalse(wt.exists())

    def test_live_binding_accepts_backticks_and_branch_annotation(self):
        r, home, wt = self.merged()
        status = r / 'nodes/app/status.md'
        for cell in ('`job-a`', '`job-a` (then followup)'):
            with self.subTest(cell=cell):
                status.write_text('## Active work\n| Branch | Thread | PR | State |\n'
                                  + '| ' + cell + ' | fake-thread | - | working |\n')
                self.assertIn('live bound thread', self.b(r, 'wt', 'prune', 'app', '--offline'))
                refused = self.b(r, 'wt', 'rm', 'app', 'job-a', '--force', ok=False)
                self.assertIn('live bound thread', refused)
                self.assertNotIn('lessons:', refused)
                self.assertTrue(wt.exists())

    def test_refused_rm_prints_no_cleanup_handback(self):
        r, home, wt = self.merged()
        (r / 'nodes/app/status.md').write_text('## Active work\n| Branch | Thread | PR | State |\n'
                                             '| job-a | fake-thread | - | working |\n')
        out = self.b(r, 'wt', 'rm', 'app', 'job-a', '--force', ok=False)
        self.assertIn('live bound thread', out)
        self.assertNotIn('lessons:', out)
        self.assertTrue(wt.exists())

    def test_pr_mode_reports_missing_origin_default(self):
        r, home, wt = self.merged()
        origin = self.tmp / 'origin.git'
        self.git('init', '-q', '--bare', '-b', 'main', str(origin), cwd=self.tmp)
        self.git('remote', 'add', 'origin', str(origin), cwd=home)
        self.b(r, 'set', 'app', 'merge=pr')
        out = self.b(r, 'wt', 'prune', 'app', '--offline')
        self.assertIn('origin default unavailable; run git remote set-head origin -a', out)
        self.assertTrue(wt.exists())


class WorktreeGuardUnits(unittest.TestCase):
    def setUp(self):
        loader = core_loader.SourceFileLoader('prune_fixture', str(CORE / 'bin/brain'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.core = importlib.util.module_from_spec(spec)
        loader.exec_module(self.core)

    def test_checked_out_head_must_equal_recorded_branch_tip(self):
        def git(*args, **kwargs):
            output = 'refs/heads/job' if args[0] == 'symbolic-ref' else ('a' if args[-1] == 'HEAD' else 'b')
            return subprocess.CompletedProcess(args, 0, output, '')
        n = {'home_path': CORE, 'local': {}}
        with mock.patch.object(self.core, 'git', side_effect=git):
            self.assertIn('HEAD differs', self.core.removal_refusal(n, 'job', {'path': str(CORE)}))

    def test_squash_lookup_is_restricted_to_integration_branch(self):
        def git(*args, **kwargs):
            return subprocess.CompletedProcess(args, 1 if args[0] == 'merge-base' else 0,
                                               'tip' if args[0] == 'rev-parse' else '', '')
        with mock.patch.object(self.core, 'git', side_effect=git) as git_calls, \
             mock.patch.object(self.core, 'remote_url', return_value='https://github.com/example/repo'), \
             mock.patch.object(self.core, 'integration_ref', return_value='refs/remotes/origin/master'), \
             mock.patch.object(shutil, 'which', return_value='/fake/gh'), \
             mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '[{"number": 1}]', '')) as run:
            self.assertTrue(self.core.worktree_state(CORE, {'path': str(CORE), 'base': 'obsolete', 'start': 'old'}, 'job')[1])
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('--base') + 1], 'master')
        self.assertTrue(any(call.args == ('rev-list', '--count', 'refs/heads/master..refs/heads/job') for call in git_calls.call_args_list))

    def test_existing_worktree_documentation_uses_worktree_path(self):
        text = (CORE / 'adapters/hosts/t3.md').read_text()
        snippets = re.findall(r'\{[^{}]*["\']type["\']\s*:\s*["\']existing_worktree["\'][^{}]*\}', text)
        self.assertTrue(snippets)
        for snippet in snippets:
            with self.subTest(snippet=snippet):
                self.assertIn('"worktreePath"', snippet)
                self.assertNotRegex(snippet, r'"path"\s*:')

    def test_commit_counts_use_explicit_branch_refs(self):
        with mock.patch.object(self.core, 'git', return_value=subprocess.CompletedProcess([], 0, '1', '')) as git:
            self.assertEqual(self.core.job_commits(CORE, {'start': 'old'}, 'job'), '1')
            self.assertIn('old..refs/heads/job', git.call_args.args)


class WorktreeSetupDiagnostics(Scenario):
    def finish(self, r):
        return self.b(r, "handback", "a", "--branch", "job", "--state", "done", "--reviews", "1")

    def test_setup_completes_before_return_and_first_verify(self):
        r = self.init('alice')
        home = self.repo(self.tmp / 'app', {'.gitignore': '.env\nsetup-started\n',
                         'verify.py': "from pathlib import Path\nassert Path('.env').read_text() == 'GENERATED=prepared-fixture\\n'\n"})
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'set', 'a', 'verify=python3 -B verify.py')
        release = home / 'allow-setup'
        (r / 'nodes/a/worktree-setup.sh').write_text(
            'touch setup-started\nwhile ! test -f "$MAIN/allow-setup"; do sleep 0.02; done\n'
            "printf 'GENERATED=prepared-fixture\\n' > .env\n")
        process = subprocess.Popen([sys.executable, str(r / 'core/bin/brain'), 'wt', 'add', 'a', 'job'],
                                   cwd=r, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        wt = self.tmp / 'app-work/worktrees/job'
        try:
            deadline = time.monotonic() + 10
            while not (wt / 'setup-started').exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((wt / 'setup-started').exists())
            self.assertIsNone(process.poll())
            self.assertFalse((wt / '.env').exists())
        finally:
            release.touch()
            output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn('setup: running; waiting for completion', output)
        self.assertRegex(output, r'setup: ok in \d+\.\d+s')
        self.assertLess(output.index('setup: ok'), output.index('launch a worker'))
        self.assertEqual((wt / '.env').read_text(), 'GENERATED=prepared-fixture\n')
        self.assertTrue(self.finish(r).strip().endswith('ready'))

    def test_setup_failure_reports_duration_masks_words_and_returns_failure(self):
        r = self.init('alice')
        home = self.repo(self.tmp / 'app')
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        (r / 'nodes/a/never-name.txt').write_text('HiddenFixture\n')
        (r / 'nodes/a/worktree-setup.sh').write_text('echo HiddenFixture\nexit 7\n')
        process = subprocess.run([sys.executable, str(r / 'core/bin/brain'), 'wt', 'add', 'a', 'job'],
                                 cwd=r, env=self.env, capture_output=True, text=True)
        self.assertEqual(process.returncode, 7)
        self.assertRegex(process.stdout, r'setup: FAILED \(exit 7\) in \d+\.\d+s')
        self.assertNotIn('hiddenfixture', (process.stdout + process.stderr).casefold())
        self.assertNotIn('launch a worker', process.stdout)
        self.assertTrue((self.tmp / 'app-work/worktrees/job').exists())


if __name__ == "__main__":
    unittest.main()
