"""T3 worktree removal on fabricated repositories and a fabricated host store."""
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core_loader
from scenario import CORE

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@t', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}


class T3Worktrees(unittest.TestCase):
    def setUp(self):
        core = core_loader.SourceFileLoader('t3_worktrees_fixture', str(CORE / 'bin/brain')).load_module()
        self.m = core._owners['populated_submodules']
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        holder = tempfile.TemporaryDirectory(prefix='brain-t3wt-')
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name).resolve()
        self.repo = self.tmp / 'instance'
        self.home = self.tmp / 't3'
        (self.home / 'worktrees' / 'demo').mkdir(parents=True)
        (self.home / 'userdata').mkdir()
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main', cwd=self.repo)
        (self.repo / 'notes.md').write_text('demo\n')
        self.git('add', '.', cwd=self.repo)
        self.git('commit', '-qm', 'start', cwd=self.repo)
        self.threads = []
        self.live = set()
        live = mock.patch.object(self.m, 'live_directories', side_effect=lambda: set(self.live))
        live.start()
        self.addCleanup(live.stop)

    def git(self, *args, cwd):
        return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

    def worktree(self, name, settled=True, run='completed', repo=None):
        path = self.home / 'worktrees' / 'demo' / name
        self.git('worktree', 'add', '-q', str(path), '-b', 't3/' + name, cwd=repo or self.repo)
        self.threads.append(('thread-' + name, str(path), settled, run))
        return path

    def store(self):
        db = self.home / 'userdata' / 'statev2.sqlite'
        db.unlink(missing_ok=True)
        con = sqlite3.connect(db)
        con.execute('create table orchestration_v2_projection_threads (thread_id text primary key, payload_json text)')
        con.execute('create table orchestration_v2_projection_runs (thread_id text, status text, completed_at text)')
        for identity, path, settled, run in self.threads:
            payload = {'worktreePath': path, 'settledAt': '2026-01-01T00:00:00Z' if settled else None,
                       'settledOverride': 'settled' if settled else None}
            con.execute('insert into orchestration_v2_projection_threads values (?, ?)', (identity, json.dumps(payload)))
            con.execute('insert into orchestration_v2_projection_runs values (?, ?, ?)',
                        (identity, run, None if run == 'running' else '2026-01-01T00:00:00Z'))
        con.commit()
        con.close()

    def reap(self, **kw):
        self.store()
        return self.m.reap(self.repo, home=self.home, **kw)

    def verdicts(self, result):
        return {Path(r['path']).name: (r['verdict'], r['reason']) for r in result['rows']}

    def listed(self):
        return self.git('worktree', 'list', '--porcelain', cwd=self.repo)

    def test_settled_clean_worktree_is_removed_through_git(self):
        path = self.worktree('t3-0001')
        dry = self.reap()
        self.assertEqual(self.verdicts(dry), {'t3-0001': ('remove', 'settled and clean')})
        self.assertTrue(path.exists())
        self.assertGreater(dry['freed'], 0)
        result = self.reap(apply=True)
        self.assertEqual(result['removed'], [str(path)])
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), self.listed())
        self.assertFalse((self.repo / '.git' / 'worktrees' / 't3-0001').exists())
        # The branch, and with it every commit, stays.
        self.assertIn('t3/t3-0001', self.git('branch', '--list', 't3/t3-0001', cwd=self.repo))

    def test_dirty_untracked_and_unique_commits_are_kept_and_listed(self):
        modified = self.worktree('t3-0002')
        (modified / 'notes.md').write_text('changed\n')
        untracked = self.worktree('t3-0003')
        (untracked / 'draft.md').write_text('new\n')
        ahead = self.worktree('t3-0004')
        (ahead / 'work.md').write_text('only here\n')
        self.git('add', '.', cwd=ahead)
        self.git('commit', '-qm', 'only here', cwd=ahead)
        merged = self.worktree('t3-0005')
        (merged / 'done.md').write_text('merged\n')
        self.git('add', '.', cwd=merged)
        self.git('commit', '-qm', 'merged', cwd=merged)
        self.git('merge', '-q', 't3/t3-0005', cwd=self.repo)
        result = self.reap(apply=True)
        self.assertEqual(self.verdicts(result), {
            't3-0002': ('keep', 'uncommitted or untracked changes'),
            't3-0003': ('keep', 'uncommitted or untracked changes'),
            't3-0004': ('keep', 'commits only here'),
            't3-0005': ('removed', 'settled and clean')})
        for path in (modified, untracked, ahead):
            self.assertTrue(path.exists())
            self.assertIn(str(path), self.listed())
        lines = self.m.report(result, True)
        self.assertTrue(any('t3-0004' in line and 'commits only here' in line for line in lines))

    def test_unsettled_running_kept_live_and_unknown_threads_are_kept(self):
        self.worktree('t3-0006', settled=False)
        self.worktree('t3-0007', run='running')
        kept = self.worktree('t3-0008')
        busy = self.worktree('t3-0009')
        self.live = {str(busy / 'sub')}
        orphan = self.home / 'worktrees' / 'demo' / 't3-0010'
        self.git('worktree', 'add', '-q', str(orphan), '-b', 't3/orphan', cwd=self.repo)
        result = self.reap(apply=True, keep={kept.name})
        self.assertEqual(self.verdicts(result), {
            't3-0006': ('keep', 'thread unsettled'),
            't3-0007': ('keep', 'thread running'),
            't3-0008': ('keep', 'kept by setting'),
            't3-0009': ('keep', 'a process works inside it'),
            't3-0010': ('keep', 'no host thread records this worktree')})
        self.assertEqual(result['removed'], [])

    def test_unreadable_store_or_process_list_keeps_everything(self):
        path = self.worktree('t3-0011')
        self.store()
        (self.home / 'userdata' / 'statev2.sqlite').write_text('not a database')
        result = self.m.reap(self.repo, home=self.home, apply=True)
        self.assertEqual(self.verdicts(result), {'t3-0011': ('keep', 'host thread state unknown')})
        self.assertIn('host thread state unreadable', self.m.report(result, True)[0])
        with mock.patch.object(self.m, 'live_directories', return_value=None):
            result = self.reap(apply=True)
        self.assertEqual(self.verdicts(result), {'t3-0011': ('keep', 'live processes unknown')})
        self.assertTrue(path.exists())

    def test_other_repositories_are_reported_never_changed(self):
        other = self.tmp / 'home-checkout'
        other.mkdir()
        self.git('init', '-q', '-b', 'main', cwd=other)
        self.git('commit', '-q', '--allow-empty', '-m', 'start', cwd=other)
        path = self.worktree('t3-0012', repo=other)
        result = self.reap(apply=True)
        self.assertEqual(self.verdicts(result), {'t3-0012': ('other', 'another repository')})
        self.assertTrue(path.exists())
        self.assertIn('other repository (report only): demo: 1 worktree(s)', '\n'.join(self.m.report(result, True)))

    def test_thread_selection_touches_only_that_threads_worktree(self):
        first = self.worktree('t3-0013')
        second = self.worktree('t3-0014')
        result = self.reap(apply=True, threads_wanted=['thread-t3-0014'])
        self.assertEqual(result['removed'], [str(second)])
        self.assertTrue(first.exists())
        self.assertEqual(self.verdicts(result), {'t3-0014': ('removed', 'settled and clean')})

    def test_empty_submodule_admin_folder_needs_the_guarded_retry(self):
        path = self.worktree('t3-0015')
        modules = self.repo / '.git' / 'worktrees' / 't3-0015' / 'modules'
        modules.mkdir()
        refused = subprocess.run(['git', 'worktree', 'remove', str(path)], cwd=self.repo, capture_output=True, text=True)
        self.assertIn('containing submodules', refused.stderr)
        result = self.reap(apply=True)
        self.assertEqual(result['removed'], [str(path)])
        self.assertNotIn(str(path), self.listed())

    def test_submodule_data_keeps_the_worktree(self):
        path = self.worktree('t3-0016')
        modules = self.repo / '.git' / 'worktrees' / 't3-0016' / 'modules' / 'core'
        modules.mkdir(parents=True)
        result = self.reap(apply=True)
        self.assertEqual(self.verdicts(result), {'t3-0016': ('keep', 'a submodule is checked out')})
        self.assertTrue(path.exists())

    def test_git_refusal_keeps_and_reports_the_worktree(self):
        path = self.worktree('t3-0017')
        self.git('worktree', 'lock', str(path), cwd=self.repo)
        result = self.reap(apply=True)
        self.assertEqual(self.verdicts(result), {'t3-0017': ('keep', 'git worktree remove refused')})
        self.assertEqual(result['errors'], [str(path)])
        self.assertTrue(path.exists())

    def test_state_is_reread_right_before_each_removal(self):
        resumed = self.worktree('t3-0020')
        advanced = self.worktree('t3-0021')
        self.store()
        real = self.m.host_threads
        calls = []
        def changing(home):
            calls.append(home)
            if len(calls) == 2:
                # After classification: one thread resumes, another branch gains a unique commit.
                (advanced / 'late.md').write_text('late\n')
                self.git('add', '.', cwd=advanced)
                self.git('commit', '-qm', 'late', cwd=advanced)
                self.threads[0] = ('thread-t3-0020', str(resumed), False, 'running')
                self.store()
            return real(home)
        with mock.patch.object(self.m, 'host_threads', side_effect=changing):
            result = self.m.reap(self.repo, home=self.home, apply=True)
        self.assertEqual(self.verdicts(result), {
            't3-0020': ('keep', 'thread unsettled'), 't3-0021': ('keep', 'commits only here')})
        self.assertTrue(resumed.exists() and advanced.exists())

    def test_unreadable_keep_setting_removes_nothing(self):
        path = self.worktree('t3-0022')
        result = self.reap(apply=True, keep=None)
        self.assertEqual(self.verdicts(result), {'t3-0022': ('keep', 'keep setting unreadable')})
        self.assertTrue(path.exists())
        class Broken:
            def node(self, _):
                raise SystemExit('unreadable')
        self.assertIsNone(self.m.keep_setting(Broken())())
        node = self.tmp / 'root-node.md'
        node.write_text('---\nid: root\nt3_worktree_keep: t3-0000aaaa, /demo/path\n---\n')
        class Root:
            def node(self, _):
                return {'dir': node.parent, 'fm': {}}
        node.rename(self.tmp / 'node.md')
        self.assertEqual(self.m.keep_setting(Root())(), {'t3-0000aaaa', '/demo/path'})
        (self.tmp / 'node.md').unlink()
        self.assertIsNone(self.m.keep_setting(Root())())

    def test_spent_deadline_defers_removal(self):
        path = self.worktree('t3-0023')
        self.store()
        result = self.m.reap(self.repo, home=self.home, apply=True, deadline=0)
        self.assertEqual(self.verdicts(result)['t3-0023'], ('keep', 'time budget spent; next pass'))
        self.assertTrue(path.exists())

    def test_settle_path_judges_only_the_selected_worktree_without_sizes(self):
        self.worktree('t3-0024')
        chosen = self.worktree('t3-0025')
        self.store()
        with mock.patch.object(self.m, 'reachable_commits') as walk, \
                mock.patch.object(self.m, 'measure') as measure:
            result = self.m.reap(self.repo, home=self.home, apply=True, threads_wanted=['thread-t3-0025'],
                                 sizes=False, deadline=self.m.time.monotonic() + 2)
        walk.assert_not_called()
        measure.assert_not_called()
        self.assertEqual(result['removed'], [str(chosen)])
        self.assertEqual(len(result['rows']), 1)

    def test_removal_is_never_forced(self):
        path = self.worktree('t3-0026')
        self.store()
        real = self.m._git
        commands = []
        def recording(args, *a, **kw):
            commands.append(args)
            return real(args, *a, **kw)
        with mock.patch.object(self.m, '_git', side_effect=recording):
            (self.repo / '.git' / 'worktrees' / 't3-0026' / 'modules').mkdir()
            self.m.reap(self.repo, home=self.home, apply=True)
        self.assertFalse(path.exists())
        self.assertFalse(any('--force' in c or '-f' in c for c in commands))

    def test_ignored_files_and_hidden_index_edits_keep_the_worktree(self):
        (self.repo / '.gitignore').write_text('*.env\n')
        self.git('add', '.gitignore', cwd=self.repo)
        self.git('commit', '-qm', 'ignore', cwd=self.repo)
        ignored = self.worktree('t3-0027')
        (ignored / 'local.env').write_text('demo-value\n')
        assumed = self.worktree('t3-0028')
        self.git('update-index', '--assume-unchanged', 'notes.md', cwd=assumed)
        (assumed / 'notes.md').write_text('edited behind the flag\n')
        skipped = self.worktree('t3-0029')
        self.git('update-index', '--skip-worktree', 'notes.md', cwd=skipped)
        (skipped / 'notes.md').write_text('edited behind the flag\n')
        (self.repo / '.gitignore').write_text('*.env\n.brain/\n')
        self.git('commit', '-qam', 'ignore local state', cwd=self.repo)
        bookkeeping = self.worktree('t3-0030')
        (bookkeeping / '.brain').mkdir()
        (bookkeeping / '.brain' / 'hooks.json').write_text('{}\n')
        (bookkeeping / '.brain' / 'hooks.log').write_text('demo\n')
        state = self.worktree('t3-0031')
        (state / '.brain').mkdir()
        (state / '.brain' / 'hooks.json').write_text('{}\n')
        (state / '.brain' / 'local.json').write_text('{}\n')
        result = self.reap(apply=True)
        hidden = 'index hides changes (assume-unchanged or skip-worktree)'
        self.assertEqual(self.verdicts(result), {
            't3-0027': ('keep', 'ignored files'), 't3-0028': ('keep', hidden), 't3-0029': ('keep', hidden),
            't3-0030': ('removed', 'settled and clean'), 't3-0031': ('keep', 'ignored files')})
        for path in (ignored, assumed, skipped, state):
            self.assertTrue(path.exists())
        self.assertFalse(bookkeeping.exists())

    def test_expired_deadline_stops_checking_queued_worktrees(self):
        for index in range(6):
            self.worktree('t3-01%02d' % index)
        self.store()
        real, checked = self.m.judge, []
        def slow(entry, *args, **kw):
            checked.append(entry)
            self.m._deadline[0] = self.m.time.monotonic() - 1  # the first check spends the budget
            return real(entry, *args, **kw)
        with mock.patch.object(self.m, 'judge', side_effect=slow):
            result = self.m.reap(self.repo, home=self.home, apply=True, sizes=False,
                                 deadline=self.m.time.monotonic() + 60)
        self.assertLessEqual(len(checked), self.m.WORKERS)
        self.assertEqual(result['removed'], [])
        self.assertTrue(all(r['verdict'] == 'keep' for r in result['rows']))
        self.assertIn('time budget spent; next pass', {r['reason'] for r in result['rows']})

    def test_a_tag_sharing_the_branch_name_does_not_hide_unique_commits(self):
        path = self.worktree('t3-0032')
        (path / 'only.md').write_text('only here\n')
        self.git('add', '.', cwd=path)
        self.git('commit', '-qm', 'only here', cwd=path)
        self.git('tag', 't3/t3-0032', 'main', cwd=self.repo)
        self.assertTrue(self.m.unique_commits(path))
        result = self.reap(apply=True)
        self.assertEqual(self.verdicts(result), {'t3-0032': ('keep', 'commits only here')})
        self.assertTrue(path.exists())

    def test_a_commit_left_only_in_the_worktree_reflog_keeps_it(self):
        path = self.worktree('t3-0039')
        self.git('checkout', '-q', '--detach', cwd=path)
        (path / 'valuable.md').write_text('only in the reflog\n')
        self.git('add', '.', cwd=path)
        self.git('commit', '-qm', 'valuable', cwd=path)
        self.git('reset', '-q', '--hard', 'main', cwd=path)
        self.assertTrue(self.m.unique_commits(path))
        result = self.reap(apply=True)
        self.assertEqual(self.verdicts(result), {'t3-0039': ('keep', 'commits only here')})
        self.assertTrue(path.exists())

    def test_orig_head_private_refs_and_operations_in_progress_keep_it(self):
        orig = self.worktree('t3-0040')
        self.git('checkout', '-q', '--detach', cwd=orig)
        (orig / 'valuable.md').write_text('only behind ORIG_HEAD\n')
        self.git('add', '.', cwd=orig)
        self.git('commit', '-qm', 'valuable', cwd=orig)
        self.git('reset', '-q', '--hard', 'main', cwd=orig)
        admin = self.repo / '.git' / 'worktrees' / 't3-0040'
        (admin / 'logs' / 'HEAD').unlink()  # the reflog expired; ORIG_HEAD still names the commit
        self.assertTrue((admin / 'ORIG_HEAD').exists())
        private = self.worktree('t3-0041')
        (private / 'bisect.md').write_text('only behind a private ref\n')
        self.git('add', '.', cwd=private)
        self.git('commit', '-qm', 'private', cwd=private)
        self.git('update-ref', 'refs/worktree/saved', 'HEAD', cwd=private)
        self.git('reset', '-q', '--hard', 'main', cwd=private)
        self.git('update-ref', 'refs/heads/t3/t3-0041', 'main', cwd=private)
        (self.repo / '.git' / 'worktrees' / 't3-0041' / 'logs' / 'HEAD').unlink()
        busy = self.worktree('t3-0042')
        (self.repo / '.git' / 'worktrees' / 't3-0042' / 'rebase-merge').mkdir()
        # A merge interrupted after --autostash: clean status, no MERGE_HEAD, the stash only here.
        stashed = self.worktree('t3-0043')
        (stashed / 'notes.md').write_text('draft\n')
        stash = self.git('stash', 'create', cwd=stashed).strip()
        self.git('checkout', '-q', '--', 'notes.md', cwd=stashed)
        (self.repo / '.git' / 'worktrees' / 't3-0043' / 'MERGE_AUTOSTASH').write_text(stash + '\n')
        unknown = self.worktree('t3-0044')
        (self.repo / '.git' / 'worktrees' / 't3-0044' / 'SOME_FUTURE_STATE').write_text(stash + '\n')
        result = self.reap(apply=True)
        self.assertEqual(self.verdicts(result), {
            't3-0040': ('keep', 'commits only here'), 't3-0041': ('keep', 'commits only here'),
            't3-0042': ('keep', 'a git operation is in progress'),
            't3-0043': ('keep', 'a git operation is in progress'), 't3-0044': ('keep', 'commits only here')})
        for path in (orig, private, busy, stashed, unknown):
            self.assertTrue(path.exists())

    def test_keep_added_during_a_pass_protects_later_removals(self):
        first = self.worktree('t3-0033')
        second = self.worktree('t3-0034')
        reads = []
        def keep():
            reads.append(1)
            return set() if len(reads) <= 2 else {second.name}
        result = self.reap(apply=True, keep=keep)
        self.assertEqual(result['removed'], [str(first)])
        self.assertEqual(self.verdicts(result)['t3-0034'], ('keep', 'kept by setting'))
        self.assertTrue(second.exists())

    def test_late_files_or_open_handles_move_the_worktree_back(self):
        (self.repo / '.gitignore').write_text('*.env\n')
        self.git('add', '.gitignore', cwd=self.repo)
        self.git('commit', '-qm', 'ignore', cwd=self.repo)
        late = self.worktree('t3-0035')
        held = self.worktree('t3-0036')
        def inspect(hold):
            # Written after the final check, once the old path no longer reaches the tree.
            if hold.endswith('.t3-0035.removing'):
                (Path(hold) / 'late.env').write_text('demo-value\n')
                return False
            return True
        with mock.patch.object(self.m, 'open_inside', side_effect=inspect):
            result = self.reap(apply=True)
        self.assertEqual(result['removed'], [])
        self.assertEqual(sorted(result['errors']), [str(late), str(held)])
        self.assertTrue((late / 'late.env').exists())
        self.assertTrue(held.exists())
        for path in (late, held):
            self.assertIn('worktree ' + str(path), self.listed().splitlines())
        self.assertEqual(sorted(p.name for p in late.parent.iterdir()), ['t3-0035', 't3-0036'])

    def test_failed_move_back_reports_where_the_worktree_was_left(self):
        path = self.worktree('t3-0037')
        self.store()
        real = self.m._git
        def no_move_back(args, *a, **kw):
            if args[:2] == ['worktree', 'move'] and args[2].endswith('.removing'):
                return subprocess.CompletedProcess(args, 1, '', 'refused')
            return real(args, *a, **kw)
        with mock.patch.object(self.m, 'open_inside', return_value=True), \
                mock.patch.object(self.m, '_git', side_effect=no_move_back):
            result = self.m.reap(self.repo, home=self.home, apply=True)
        hold = self.m.hold_path(str(path))
        self.assertTrue(Path(hold).is_dir())
        self.assertEqual(result['errors'], [hold])
        self.assertIn('left at ' + hold, self.verdicts(result)['t3-0037'][1])

    def test_size_walk_stops_at_the_deadline(self):
        path = self.worktree('t3-0038')
        for index in range(50):
            (path / ('file%d.txt' % index)).write_text('x')
        self.m._deadline[0] = self.m.time.monotonic() - 1
        try:
            with self.assertRaises(OSError):
                self.m.measure(path)
        finally:
            self.m._deadline[0] = None

    def test_clone_families_count_only_when_every_sharer_goes(self):
        one = {'listed': 10, 'private': 2, 'shared': {7: [2, 1, 8]}}
        two = {'listed': 10, 'private': 2, 'shared': {7: [2, 1, 8]}}
        self.assertEqual(self.m.freed([one]), 2)
        self.assertEqual(self.m.freed([one, two]), 12)

    def test_due_hook_is_off_unless_the_root_node_opts_in(self):
        class Instance:
            root = self.repo
            def __init__(self, fm):
                self.fm = fm
            def node(self, _):
                return {'fm': self.fm}
        with mock.patch.object(self.m, 'reap_settled', return_value={'removed': []}) as reaped:
            self.assertIsNone(self.m.reap_due(Instance({})))
            reaped.assert_not_called()
            self.m.reap_due(Instance({'t3_worktree_reap': 'on'}))
            reaped.assert_called_once()

    def test_settle_hook_reaps_only_attested_threads_and_lists_what_it_kept(self):
        path = self.worktree('t3-0018')
        dirty = self.worktree('t3-0019')
        (dirty / 'draft.md').write_text('new\n')
        self.store()
        jobs = core_loader.SourceFileLoader('t3_settle_fixture', str(CORE / 'bin/brain')).load_module()._owners['settled_worktrees']
        root_node = self.tmp / 'root-node'
        root_node.mkdir()
        (root_node / 'node.md').write_text('---\nid: root\n---\n')
        class Instance:
            root = self.repo
            def node(self, _):
                return {'dir': root_node, 'fm': {}}
        with mock.patch.dict(os.environ, {'BRAIN_T3_HOME': str(self.home)}), \
                mock.patch.object(jobs, '_m_t3_worktrees', self.m), \
                mock.patch.object(jobs, '_m_guard') as guard, mock.patch('builtins.print') as printed:
            guard.redact.side_effect = lambda text, terms: text
            jobs.settled_worktrees(Instance(), {'id': 'demo'}, ['thread-t3-0018', 'thread-t3-0019'])
        self.assertFalse(path.exists())
        self.assertTrue(dirty.exists())
        self.assertEqual(printed.call_args[0][0].splitlines(), [
            'demo: removed T3 worktree t3-0018', 'demo: kept T3 worktree t3-0019: uncommitted or untracked changes'])

    def test_missing_t3_folder_is_a_quiet_no_op(self):
        with mock.patch.object(self.m, 'instance_repository') as repository:
            result = self.m.reap(self.repo, home=self.tmp / 'absent', apply=True)
        repository.assert_not_called()
        self.assertEqual((result['rows'], result['removed'], result['freed']), ([], [], 0))


if __name__ == '__main__':
    unittest.main()
