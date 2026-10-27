"""Task invariants use only isolated fixture records and repositories."""
import argparse
import json
import sys
import unittest
from unittest import mock
from scenario import Scenario, CORE
sys.path.insert(0, str(CORE / 'lib'))
from brain import tasks
from brain.instance import Instance


class Tasks(Scenario):
    def assert_home_prepare_commit(self, *, symbolic=False, linked=False, move_main=False):
        home = self.tmp / 'home'
        self.repo(home, {'engine.py': 'value = 1\n'})
        base = self.git('rev-parse', 'HEAD', cwd=home).strip()
        (home / 'engine.py').write_text('value = 2\n')
        self.git('add', 'engine.py', cwd=home)
        self.git('commit', '-qm', 'Deliver fixture change', cwd=home)
        head = self.git('rev-parse', 'HEAD', cwd=home).strip()
        tasks._m_common.write_fm(self.root / 'node.md',
            {'id': 'root', 'owners': 'sample', 'home': str(home)}, '')
        self.n = self.inst.node('root')
        self.add('DONE-1', status='done', baseline_sha=base, merge_sha=head,
                 code_path='engine.py', scope=['engine.py'], evidence=['Tests passed'])
        if symbolic:
            self.git('branch', 'stable', cwd=home)
            self.git('symbolic-ref', 'refs/heads/main', 'refs/heads/stable', cwd=home)
        if linked:
            linked_home = self.tmp / 'linked-home'
            self.git('worktree', 'add', '-qb', 'job/linked', str(linked_home), cwd=home)
            tasks._m_common.write_fm(self.root / 'node.md',
                {'id': 'root', 'owners': 'sample', 'home': str(linked_home)}, '')
            self.n = self.inst.node('root')
        self.git('checkout', '-qb', 'job/builder', cwd=home)
        prepare = tasks.prepare_mutation
        def changed(*args, **kwargs):
            self.assertIsNone(tasks._m_instance._LOCK['fh'])
            result = prepare(*args, **kwargs)
            self.git('commit', '--allow-empty', '-qm', 'Unrelated builder commit', cwd=home)
            if move_main:
                self.git('update-ref', 'refs/heads/stable' if symbolic else 'refs/heads/main', base, cwd=home)
            return result
        with mock.patch.object(tasks, 'prepare_mutation', side_effect=changed):
            if move_main:
                with self.assertRaises(SystemExit):
                    self.add('NEW-1')
            else:
                self.add('NEW-1')
        self.assertEqual('NEW-1' in tasks.read_records(self.n), not move_main)

    def test_unrelated_home_branch_commit_during_prepare_allows_task_write(self):
        self.assert_home_prepare_commit()

    def test_linked_home_unrelated_commit_during_prepare_allows_task_write(self):
        self.assert_home_prepare_commit(linked=True)

    def test_linked_home_main_change_during_prepare_refuses(self):
        self.assert_home_prepare_commit(linked=True, move_main=True)

    def test_symbolic_main_target_change_during_prepare_refuses(self):
        self.assert_home_prepare_commit(symbolic=True, move_main=True)

    def test_symbolic_main_intermediate_repoint_and_restore_refuses(self):
        self.repo(self.root, {'engine.py': 'value = 1\n'})
        base = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        (self.root / 'engine.py').write_text('value = 2\n')
        self.git('add', 'engine.py', cwd=self.root)
        self.git('commit', '-qm', 'Deliver fixture change', cwd=self.root)
        head = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.add('DONE-1', status='done', baseline_sha=base, merge_sha=head,
                 code_path='engine.py', scope=['engine.py'], evidence=['Tests passed'])
        self.git('branch', 'stable', cwd=self.root)
        self.git('branch', 'alternate', base, cwd=self.root)
        self.git('symbolic-ref', 'refs/heads/middle', 'refs/heads/stable', cwd=self.root)
        self.git('symbolic-ref', 'refs/heads/main', 'refs/heads/middle', cwd=self.root)
        prepare = tasks.prepare_mutation
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            self.git('symbolic-ref', 'refs/heads/middle', 'refs/heads/alternate', cwd=self.root)
            self.git('symbolic-ref', 'refs/heads/middle', 'refs/heads/stable', cwd=self.root)
            return result
        with mock.patch.object(tasks, 'prepare_mutation', side_effect=changed), self.assertRaises(SystemExit):
            self.add('NEW-1')
        self.assertNotIn('NEW-1', tasks.read_records(self.n))

    def test_symbolic_replacement_target_change_during_prepare_refuses(self):
        self.repo(self.root, {'engine.py': 'value = 1\n'})
        base = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        (self.root / 'engine.py').write_text('value = 2\n')
        self.git('add', 'engine.py', cwd=self.root)
        self.git('commit', '-qm', 'Deliver fixture change', cwd=self.root)
        head = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.add('DONE-1', status='done', baseline_sha=base, merge_sha=head,
                 code_path='engine.py', scope=['engine.py'], evidence=['Tests passed'])
        replacement = self.git('commit-tree', head + '^{tree}', '-p', base,
                               '-m', 'Replacement fixture change', cwd=self.root).strip()
        self.git('branch', 'replacement-target', replacement, cwd=self.root)
        self.git('symbolic-ref', 'refs/replace/' + head, 'refs/heads/replacement-target', cwd=self.root)
        prepare = tasks.prepare_mutation
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            self.git('update-ref', 'refs/heads/replacement-target', base, cwd=self.root)
            return result
        with mock.patch.object(tasks, 'prepare_mutation', side_effect=changed), self.assertRaises(SystemExit):
            self.add('NEW-1')
        self.assertNotIn('NEW-1', tasks.read_records(self.n))

    def test_task_entry_points_work_while_home_builder_commits(self):
        import os
        import subprocess
        import time
        root = self.init('demo')
        home = self.repo(self.tmp / 'app', {'engine.py': 'value = 1\n'})
        self.b(root, 'new', 'app', '--title', 'App', '--home', str(home))
        base = self.git('rev-parse', 'HEAD', cwd=home).strip()
        (home / 'engine.py').write_text('value = 2\n')
        self.git('add', 'engine.py', cwd=home)
        self.git('commit', '-qm', 'Deliver fixture change', cwd=home)
        head = self.git('rev-parse', 'HEAD', cwd=home).strip()
        self.b(root, 'task', 'add', 'app', 'DONE-1', 'status=done', 'baseline_sha=' + base, 'merge_sha=' + head,
               'code_path=engine.py', 'scope=engine.py', 'evidence=Tests passed', '--title', 'Delivered')
        self.git('checkout', '-qb', 'job/builder', cwd=home)
        # The child signals each real commit; no network or shared instance.
        signal = self.tmp / 'builder-count'
        script = ("import pathlib, subprocess, sys, time\n"
                  "count = 0\n"
                  "while True:\n"
                  " subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'Builder tick'], check=True)\n"
                  " count += 1\n"
                  " pathlib.Path(sys.argv[1] + '.tmp').write_text(str(count))\n"
                  " pathlib.Path(sys.argv[1] + '.tmp').replace(sys.argv[1])\n"
                  " time.sleep(0.02)\n")
        builder = subprocess.Popen([sys.executable, '-B', '-c', script, str(signal)],
                                   cwd=home, env=self.env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not signal.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(signal.exists(), 'builder must commit before task commands')
            before = int(signal.read_text())
            self.b(root, 'task', 'add', 'app', 'JOB-1', 'status=ready', '--title', 'Start directly')
            self.b(root, 'task', 'add', 'app', 'WT-1', 'status=ready', '--title', 'Start worktree')
            from brain import jobs
            inst = Instance(root)
            with mock.patch.dict(os.environ, self.env, clear=True):
                jobs.job_started(inst, inst.node('app'), 'job/direct', 'codex', task_id='JOB-1')
            self.b(root, 'wt', 'add', 'app', 'job/task', '--task-id', 'WT-1', '--no-setup')
            rows = tasks.read_records(Instance(root).node('app'))
            self.assertEqual(rows['JOB-1']['status'], 'in-progress')
            self.assertEqual(rows['WT-1']['status'], 'in-progress')
            self.assertGreater(int(signal.read_text()), before)
            self.assertIsNone(builder.poll())
        finally:
            builder.terminate()
            builder.communicate(timeout=5)

    def test_unrelated_external_home_is_not_a_mutation_input(self):
        peer = self.peer_node('unrelated')
        home = self.repo(self.tmp / 'unrelated-home')
        tasks._m_common.write_fm(peer['dir'] / 'node.md',
            {'id': 'unrelated', 'owners': 'sample', 'home': str(home)}, '')
        prepare = tasks.prepare_mutation
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            self.git('commit', '--allow-empty', '-qm', 'Unrelated main commit', cwd=home)
            return result
        with mock.patch.object(tasks, 'prepare_mutation', side_effect=changed):
            self.add()
        self.assertIn('APP-1', tasks.read_records(self.n))

    def test_transitive_cross_node_completion_main_change_refuses(self):
        producer = self.peer_node('producer')
        home = self.repo(self.tmp / 'producer-home', {'engine.py': 'value = 1\n'})
        base = self.git('rev-parse', 'HEAD', cwd=home).strip()
        (home / 'engine.py').write_text('value = 2\n')
        self.git('add', 'engine.py', cwd=home)
        self.git('commit', '-qm', 'Deliver fixture change', cwd=home)
        head = self.git('rev-parse', 'HEAD', cwd=home).strip()
        tasks._m_common.write_fm(producer['dir'] / 'node.md',
            {'id': 'producer', 'owners': 'sample', 'home': str(home)}, '')
        producer = self.inst.node('producer')
        tasks.mutate(self.inst, producer, 'CODE-1', {'title': 'Deliver', 'status': 'done',
            'baseline_sha': base, 'merge_sha': head, 'code_path': 'engine.py',
            'scope': ['engine.py'], 'evidence': ['Tests passed']}, create=True)
        self.ops_done(self.n)
        tasks.mutate(self.inst, self.n, 'OPS-1', {'depends_on': ['producer:CODE-1']})
        prepare = tasks.prepare_mutation
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            self.git('update-ref', 'refs/heads/main', base, cwd=home)
            return result
        with mock.patch.object(tasks, 'prepare_mutation', side_effect=changed), self.assertRaises(SystemExit):
            self.add('NEW-1')
        self.assertNotIn('NEW-1', tasks.read_records(self.n))

    def test_validation_reads_requirement_snapshot_once_per_prepare(self):
        tasks.requirement_add(self.inst, self.n, 'REQ-1', 'Exact words', self.inst.now(), 'notes.md:1')
        self.add('BASE-1', requirement='REQ-1')
        read = tasks.read_records
        with mock.patch.object(tasks, 'read_records', wraps=read) as reads:
            tasks.prepare_mutation(self.inst, self.n, 'NEW-1',
                                   {'title': 'New task', 'requirement': 'REQ-1'}, create=True)
        self.assertEqual(sum(call.args[1:] == ('requirements',) for call in reads.call_args_list), 1)

    def test_requirement_change_during_prepare_refuses(self):
        tasks.requirement_add(self.inst, self.n, 'REQ-1', 'Exact words', self.inst.now(), 'notes.md:1')
        prepare = tasks.prepare_mutation
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            (self.n['dir'] / 'requirements/REQ-1.md').unlink()
            return result
        with mock.patch.object(tasks, 'prepare_mutation', side_effect=changed), self.assertRaises(SystemExit):
            self.add('NEW-1', requirement='REQ-1')
        self.assertNotIn('NEW-1', tasks.read_records(self.n))

    def test_target_record_change_during_prepare_refuses_without_lost_update(self):
        self.add('BASE-1')
        prepare = tasks.prepare_mutation
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            concurrent = tasks.read_records(self.n)['BASE-1']
            concurrent['title'] = 'Concurrent update'
            tasks.save_record(self.n, concurrent)
            return result
        with mock.patch.object(tasks, 'prepare_mutation', side_effect=changed), self.assertRaises(SystemExit):
            self.add('NEW-1')
        rows = tasks.read_records(self.n)
        self.assertNotIn('NEW-1', rows)
        self.assertEqual(rows['BASE-1']['title'], 'Concurrent update')

    def assert_conversion_rechecks_internal_main(self, child=False, normalized_status=False):
        self.repo(self.root, {'engine.py': 'value = 1\n'})
        base = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        (self.root / 'engine.py').write_text('value = 2\n')
        self.git('add', 'engine.py', cwd=self.root)
        self.git('commit', '-qm', 'Deliver fixture change', cwd=self.root)
        head = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        node = self.peer_node('producer') if child else self.n
        self.ops_done(node)
        changes = {'completion_kind': 'merge', 'merge_sha': head, 'baseline_sha': base,
                   'code_path': 'engine.py', 'scope': ['engine.py']}
        if normalized_status:
            changes['status'] = ' DONE '
        prepare = tasks.prepare_mutation
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            self.git('update-ref', 'refs/heads/main', base, cwd=self.root)
            return result
        with mock.patch.object(tasks, 'prepare_mutation', side_effect=changed), self.assertRaises(SystemExit):
            tasks.mutate(self.inst, node, 'OPS-1', changes)
        self.assertEqual(tasks.read_records(node)['OPS-1']['completion_kind'], 'ops')

    def test_completion_kind_conversion_without_status_rechecks_internal_main(self):
        self.assert_conversion_rechecks_internal_main()

    def test_child_node_completion_conversion_rechecks_ancestor_git_main(self):
        self.assert_conversion_rechecks_internal_main(child=True)

    def test_normalized_completion_status_rechecks_internal_main(self):
        self.assert_conversion_rechecks_internal_main(normalized_status=True)

    def test_report_artifact_change_after_validation_refuses_new_completion(self):
        import hashlib
        path = self.root / 'measurement.txt'
        path.write_text('original measurement')
        receipt = {'ref': 'fixture', 'scope': 'sample', 'result': 'passed', 'at': self.inst.now(),
                   'artifact': 'measurement.txt', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'version': 1}
        prepare = tasks.prepare_mutation
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            path.write_text('changed measurement')
            return result
        with mock.patch.object(tasks, 'prepare_mutation', side_effect=changed), self.assertRaises(SystemExit):
            self.add('REPORT-1', completion_kind='report', status='done', evidence=['Measured'], completion_receipt=receipt)
        self.assertNotIn('REPORT-1', tasks.read_records(self.n))

    def test_dependency_report_artifact_change_refuses_new_task(self):
        import hashlib
        path = self.root / 'measurement.txt'
        path.write_text('original measurement')
        receipt = {'ref': 'fixture', 'scope': 'sample', 'result': 'passed', 'at': self.inst.now(),
                   'artifact': 'measurement.txt', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'version': 1}
        self.add('REPORT-1', completion_kind='report', status='done', evidence=['Measured'], completion_receipt=receipt)
        prepare = tasks.prepare_mutation
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            path.write_text('changed dependency measurement')
            return result
        with mock.patch.object(tasks, 'prepare_mutation', side_effect=changed), self.assertRaises(SystemExit):
            self.add('CHILD-1', depends_on=['REPORT-1'])
        self.assertNotIn('CHILD-1', tasks.read_records(self.n))

    def setUp(self):
        super().setUp()
        self.root = self.tmp / 'fixture'
        self.root.mkdir()
        (self.root / 'instance.md').write_text('---\nprincipals: sample\ntimezone: UTC\n---\n')
        (self.root / 'node.md').write_text('---\nid: root\nowners: sample\nhome: here\n---\n')
        self.inst = Instance(self.root)
        self.n = self.inst.node('root')
        self.commit = mock.patch.object(self.inst, 'commit')
        self.commit.start()
        self.addCleanup(self.commit.stop)

    def add(self, ident='APP-1', **changes):
        if changes.get('status') == 'parked':
            changes = {'review_at': self.inst.now(), 'next_action': 'Repair the source', **changes}
        if changes.get('status') == 'dropped':
            changes = {'drop_decision': 'private:D-3', **changes}
        return tasks.mutate(self.inst, self.n, ident, {'title': 'Example', **changes}, create=True)

    def test_round_trip_multiline_and_links(self):
        tasks.requirement_add(self.inst, self.n, 'REQ-1', 'Keep the exact words\nand date', self.inst.now(), 'notes.md:3')
        self.add(title='Title: with punctuation', requirement='REQ-1', decisions=['repo:D1301', 'private:D-123'],
                 description='Details\nwith two lines')
        row = tasks.read_records(self.n)['APP-1']
        self.assertEqual(row['title'], 'Title: with punctuation')
        self.assertEqual(row['description'], 'Details\nwith two lines')
        self.assertEqual(row['decisions'], ['repo:D1301', 'private:D-123'])

    def test_cycles_missing_dependencies_and_self_parent_refuse_without_write(self):
        self.add()
        self.add('APP-2', depends_on=['APP-1'])
        for changes in ({'depends_on': ['APP-2']}, {'depends_on': ['MISSING']}, {'parent': 'APP-1'}):
            with self.assertRaises(ValueError):
                tasks.mutate(self.inst, self.n, 'APP-1', changes)
        self.assertEqual(tasks.read_records(self.n)['APP-1']['depends_on'], [])

    def test_parked_and_dropped_remain_dependency_blockers(self):
        self.add(status='parked', park_reason='Two failures', resume='Repair parser')
        self.add('APP-2', status='ready', depends_on=['APP-1'])
        self.assertEqual(tasks.projection(self.n)[1]['blocked_by'], ['APP-1'])
        tasks.mutate(self.inst, self.n, 'APP-1', {'status': 'dropped', 'drop_reason': 'Scope removed', 'drop_decision': 'private:D-3'})
        self.assertEqual(tasks.projection(self.n)[1]['blocked_by'], ['APP-1'])
        with self.assertRaises(ValueError):
            tasks.mutate(self.inst, self.n, 'APP-2', {'status': 'parked'})

    def test_active_requires_exact_live_job_and_completed_requires_main(self):
        for status in ('in-progress', 'in-review', 'testing', 'merged', 'done'):
            with self.assertRaises(ValueError):
                self.add(status=status, job='wrong', branch='job/sample', merge_sha='a' * 40,
                         code_path='README.md', evidence=['tests passed'])
        self.repo(self.root)
        base = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        (self.root / 'README.md').write_text('Changed implementation\n')
        self.git('add', 'README.md', cwd=self.root)
        self.git('commit', '-qm', 'Deliver report', cwd=self.root)
        sha = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.add(status='merged', baseline_sha=base, scope=['README.md'], merge_sha=sha, code_path='README.md', evidence=['focused tests passed'])
        tasks.mutate(self.inst, self.n, 'APP-1', {'status': 'done'})
        self.assertEqual(tasks.read_records(self.n)['APP-1']['status'], 'done')

    def test_bad_id_link_and_future_schema_refuse(self):
        for ident in ('../escape', '/tmp/escape', 'APP/1'):
            with self.assertRaises(ValueError):
                self.add(ident)
        with self.assertRaises(ValueError):
            self.add(decisions=['private diary contents'])
        base = self.root / 'tasks'
        base.mkdir()
        (base / 'APP-1.md').write_text('---\nversion: 99\nid: APP-1\n---\n')
        with self.assertRaises(ValueError):
            tasks.read_records(self.n)

    def test_symlink_store_never_reads_or_writes_target(self):
        foreign = self.tmp / 'foreign'
        foreign.mkdir()
        (self.root / 'tasks').symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.add()
        self.assertEqual(list(foreign.iterdir()), [])

    def test_external_fields_cannot_be_locally_overwritten(self):
        self.n['fm']['tasks.source'] = 'linear'
        tasks._m_common.write_fm(self.n['dir'] / 'node.md', self.n['fm'], '')
        with self.assertRaises(ValueError):
            self.add()
        self.add(source='local')
        self.assertEqual(tasks.read_records(self.n)['APP-1']['source'], 'local')

    def test_cli_next_orders_only_unblocked_ready_tasks(self):
        root = self.init('demo')
        self.b(root, 'task', 'add', 'root', 'APP-1', '--title', 'First')
        self.b(root, 'task', 'add', 'root', 'APP-2', 'status=ready', 'order=1', 'depends_on=APP-1', '--title', 'Blocked')
        self.b(root, 'task', 'add', 'root', 'APP-3', 'status=ready', 'order=2', '--title', 'Available')
        out = json.loads(self.b(root, 'task', 'next', 'root', '--json'))
        self.assertEqual(out['next']['id'], 'APP-3')
        self.assertIn('tasks: valid', self.b(root, 'task', 'check', 'root'))

    def test_job_start_requires_ready_task_and_records_exact_job(self):
        from brain import jobs
        self.add(status='ready')
        with self.assertRaises(SystemExit):
            jobs.job_started(self.inst, self.n, 'job/example', 'codex')
        jobs.job_started(self.inst, self.n, 'job/example', 'codex', task_id='APP-1')
        row = jobs.read_jobs(self.n)[0]
        task = tasks.read_records(self.n)['APP-1']
        self.assertEqual(row['Task'], 'APP-1')
        self.assertEqual(task['job'], jobs.job_key(row))
        self.assertEqual(task['status'], 'in-progress')
        jobs.job_started(self.inst, self.n, 'job/example', 'codex', task_id='APP-1')
        self.assertEqual(len(jobs.read_jobs(self.n)), 1)

    def test_failure_identity_deduplication_parks_after_two_rounds(self):
        self.add(status='ready')
        tasks.event(self.inst, self.n, 'APP-1', 'round-1', 'Parser failed')
        tasks.event(self.inst, self.n, 'APP-1', 'round-1', 'Parser failed')
        self.assertEqual(tasks.read_records(self.n)['APP-1']['strikes'], 1)
        tasks.event(self.inst, self.n, 'APP-1', 'round-2', 'Parser failed again')
        row = tasks.read_records(self.n)['APP-1']
        self.assertEqual(row['status'], 'parked')
        self.assertEqual(row['strikes'], 2)
        self.assertEqual(tasks.due_tasks(self.inst, self.n)[0]['kind'], 'task-parked')

    def test_job_close_without_main_verification_parks_instead_of_completing(self):
        from brain import jobs, sync
        self.add(status='ready')
        jobs.job_started(self.inst, self.n, 'job/example', 'codex', task_id='APP-1')
        row = jobs.read_jobs(self.n)[0]
        row.update(Outcome='merged', Ended=self.inst.now())
        with mock.patch.object(sync, 'integration_packet', return_value=None):
            tasks.job_closed(self.inst, self.n, row)
        task = tasks.read_records(self.n)['APP-1']
        self.assertEqual(task['status'], 'parked')
        self.assertTrue(task['resume'])

    def test_verified_landed_job_records_main_commit_and_changed_paths(self):
        from brain import jobs, sync
        self.repo(self.root)
        base = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.add(status='ready', scope=['engine.py'])
        jobs.job_started(self.inst, self.n, 'job/example', 'codex', task_id='APP-1')
        (self.root / 'engine.py').write_text('VERSION = 1\n')
        self.git('add', 'engine.py', cwd=self.root)
        self.git('commit', '-qm', 'Implement example', cwd=self.root)
        head = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        row = jobs.read_jobs(self.n)[0]
        row.update(Outcome='merged', **{'Head commit': head})
        with mock.patch.object(sync, 'integration_packet', return_value={'verify_result': 0, 'head': head, 'main': base}):
            tasks.job_closed(self.inst, self.n, row)
        row = tasks.read_records(self.n)['APP-1']
        self.assertEqual(row['status'], 'merged')
        self.assertEqual(row['merge_sha'], head)
        self.assertEqual(row['code_path'], 'engine.py')

    def test_backfill_is_additive_idempotent_and_preserves_exact_source(self):
        from brain import tasks_backfill
        path = self.root / 'backlog.md'
        text = '# Backlog\n- Add export support repo:D1301\n- [x] Already shipped\n- [ ] Keep source links\n'
        path.write_text(text)
        dry = tasks_backfill.backfill(self.inst, self.n)
        self.assertEqual(len(dry), 2)
        self.assertFalse((self.root / 'tasks').exists())
        created = tasks_backfill.backfill(self.inst, self.n, apply=True)
        self.assertEqual(len(created), 2)
        self.assertEqual(path.read_text(), text)
        requirement = tasks.read_records(self.n, 'requirements')[created[0]['requirement']]
        self.assertEqual(requirement['quote'], '- Add export support repo:D1301')
        self.assertEqual(requirement['date_basis'], 'file-observed')
        self.assertEqual(tasks_backfill.backfill(self.inst, self.n, apply=True), [])
        path.write_text('# Backlog\n\n' + text.split('\n', 1)[1])
        self.assertEqual(tasks_backfill.backfill(self.inst, self.n, apply=True), [])

    def train_fixture(self, count=1, unlinked=False):
        """A published journal and real Git endpoints, without running a gate."""
        from brain import jobs, train
        self.repo(self.root)
        base = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        entries = []
        for index in range(count):
            ident, branch, filename = 'APP-' + str(index + 1), 'job/example-' + str(index), 'engine' + str(index) + '.py'
            self.add(ident, status='ready', baseline_sha=base, scope=[filename])
            jobs.job_started(self.inst, self.n, branch, 'codex', task_id=ident)
            (self.root / filename).write_text('VERSION = 1\n')
            self.git('add', filename, cwd=self.root)
            self.git('commit', '-qm', 'Implement ' + ident, cwd=self.root)
            row = jobs.read_jobs(self.n)[-1]
            entries.append(dict(branch=branch, key=jobs.job_key(row), base=base,
                                head=self.git('rev-parse', 'HEAD', cwd=self.root).strip(), commits=1))
            tasks.handback_event(self.inst, self.n, row, dict(state='done', at=self.inst.now(), ready={'verify_result': 0}))
        if unlinked:
            # A legacy job may share the batch without a task binding.
            rows = jobs.read_jobs(self.n)
            row = {**rows[-1], 'Job': 'job/legacy', 'Task': ''}
            rows.append(row)
            jobs.write_jobs(self.inst, self.n, rows)
            entries.append({**entries[-1], 'branch': row['Job'], 'key': jobs.job_key(row)})
        tip = self.git('rev-parse', 'main', cwd=self.root).strip()
        data = dict(version=1, batch='train/fixture', state='verified', base=base, tip=tip,
                    accepted=entries, ended=self.inst.now(), publication_succeeded=True,
                    runs=[dict(jobs=[e['branch'] for e in entries], head=tip, result=0, log='fixture.log')])
        path = self.root / 'job-trains' / 'fixture.json'
        path.parent.mkdir()
        train.save(path, data)
        return train, jobs, path, data

    def test_train_finish_completes_one_linked_task_and_preserves_legacy_job(self):
        train, jobs, path, data = self.train_fixture(unlinked=True)
        train.finish(self.inst, self.n, path, data)
        row = tasks.read_records(self.n)['APP-1']
        self.assertEqual(row['status'], 'merged')
        self.assertEqual(row['merge_sha'], data['accepted'][0]['head'])
        self.assertEqual(row['code_path'], 'engine0.py')
        self.assertEqual(row['attempts'][0]['state'], 'closed')
        self.assertTrue(all(r['Ended'] for r in jobs.read_jobs(self.n)))
        self.assertEqual(data['state'], 'complete')
        tasks.validate(self.n, tasks.read_records(self.n))

    def test_train_finish_validates_multiple_tasks_against_final_dependency_graph(self):
        train, jobs, path, data = self.train_fixture(count=2)
        # Include both spellings of a dependency within this node.
        tasks.mutate(self.inst, self.n, 'APP-2', {'depends_on': ['APP-1', 'root:APP-1']})
        train.finish(self.inst, self.n, path, data)
        rows = tasks.read_records(self.n)
        self.assertEqual([r['status'] for r in rows.values()], ['merged', 'merged'])
        self.assertEqual([r['attempts'][0]['state'] for r in rows.values()], ['closed', 'closed'])
        tasks.validate(self.n, rows)
        tasks.mutate(self.inst, self.n, 'APP-2', {'priority': 'P1'})
        before = {p: p.read_bytes() for p in (self.root / 'tasks').glob('*.md')}
        train.finish(self.inst, self.n, path, data)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_train_finish_missing_exact_verification_parks_all_linked_tasks(self):
        train, jobs, path, data = self.train_fixture(count=2)
        data['runs'][0]['head'] = data['base']
        train.finish(self.inst, self.n, path, data)
        rows = tasks.read_records(self.n)
        self.assertEqual([r['status'] for r in rows.values()], ['parked', 'parked'])
        self.assertTrue(all(r['resume'] and r['review_at'] and r['next_action'] for r in rows.values()))
        tasks.validate(self.n, rows)

    def test_train_finish_incomplete_scope_parks_dependents_without_partial_validation(self):
        train, jobs, path, data = self.train_fixture(count=2)
        tasks.mutate(self.inst, self.n, 'APP-1', {'scope': ['missing.py']})
        tasks.mutate(self.inst, self.n, 'APP-2', {'depends_on': ['APP-1']})
        train.finish(self.inst, self.n, path, data)
        self.assertEqual([r['status'] for r in tasks.read_records(self.n).values()], ['parked', 'parked'])
        tasks.validate(self.n, tasks.read_records(self.n))

    def test_train_finish_recovery_replays_partial_task_publication(self):
        train, jobs, path, data = self.train_fixture(count=2)
        save = tasks.save_record
        calls = []
        def interrupted(node, row, folder='tasks'):
            calls.append(row['id'])
            if len(calls) == 2:
                raise OSError('Fixture interrupted task publication')
            save(node, row, folder)
        with mock.patch.object(tasks, 'save_record', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'Fixture interrupted'):
                train.finish(self.inst, self.n, path, data)
        self.assertEqual(json.loads(path.read_text())['state'], 'verified')
        self.assertTrue(all(r['Ended'] for r in jobs.read_jobs(self.n)))
        delivered = tasks.read_records(self.n)['APP-1']
        # Reconciliation runs with main unoccupied; instance records stay intact.
        self.git('checkout', '--detach', '-q', cwd=self.root)
        train.run_train(self.inst, self.n, self.root, 'false', argparse.Namespace(jobs=None))
        self.assertEqual([r['status'] for r in tasks.read_records(self.n).values()], ['merged', 'merged'])
        completed = tasks.read_records(self.n)['APP-1']
        metadata = {'event_pending', 'landing_event'}
        self.assertEqual({k: v for k, v in completed.items() if k not in metadata},
                         {k: v for k, v in delivered.items() if k not in metadata})
        self.assertNotIn('event_pending', completed)
        from brain import events
        landed = [e for e in events.facts(self.inst, self.n) if
                  e['data'].get('fact_type') == 'task_landed' and e['data'].get('evidence', {}).get('ref') == 'APP-1']
        self.assertEqual([e['ref'] for e in landed], [completed['landing_event']])
        stable = (self.root / 'tasks/APP-1.md').read_bytes()
        train.run_train(self.inst, self.n, self.root, 'false', argparse.Namespace(jobs=None))
        self.assertEqual((self.root / 'tasks/APP-1.md').read_bytes(), stable)
        self.assertEqual(json.loads(path.read_text())['state'], 'reconciled')
        tasks.validate(self.n, tasks.read_records(self.n))

    def test_train_finish_retains_verified_journal_when_task_binding_changed(self):
        train, jobs, path, data = self.train_fixture(count=2)
        tasks.mutate(self.inst, self.n, 'APP-2', {'job': 'different-attempt'})
        before = {p: p.read_bytes() for p in (self.root / 'tasks').glob('*.md')}
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            train.finish(self.inst, self.n, path, data)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(json.loads(path.read_text())['state'], 'verified')

    def test_train_finish_without_task_links_keeps_legacy_completion(self):
        train, jobs, path, data = self.train_fixture()
        rows = jobs.read_jobs(self.n)
        rows[0]['Task'] = ''
        jobs.write_jobs(self.inst, self.n, rows)
        before = (self.root / 'tasks/APP-1.md').read_bytes()
        train.finish(self.inst, self.n, path, data)
        self.assertEqual(data['state'], 'complete')
        self.assertEqual((self.root / 'tasks/APP-1.md').read_bytes(), before)

    def test_train_finish_defers_provider_write_and_emits_only_after_batch_is_durable(self):
        from brain import task_linear
        train, jobs, path, data = self.train_fixture(count=2)
        self.linear_config()
        row = tasks.read_records(self.n)['APP-1']
        row.update(source='linear', provider={'id': self.linear_issue()['id'],
                                             'team': self.n['fm']['tasks.linear.team_id'], 'state_type': 'started'})
        tasks.save_record(self.n, row)
        observed = []
        def emit(inst, node, row):
            observed.append([r['status'] for r in tasks.read_records(node).values()])
        with mock.patch.object(task_linear, 'request') as request, mock.patch.object(tasks, 'emit_task_landed', side_effect=emit):
            train.finish(self.inst, self.n, path, data)
        request.assert_not_called()
        self.assertEqual(observed, [['merged', 'merged'], ['merged', 'merged']])
        self.assertEqual(tasks.read_records(self.n)['APP-1']['provider_pending'], 'completed')

    def test_train_finish_validates_entire_batch_before_any_task_write(self):
        train, jobs, path, data = self.train_fixture(count=2)
        rows = tasks.read_records(self.n)
        rows['APP-2']['title'] = ''
        tasks.save_record(self.n, rows['APP-2'])
        before = {p: p.read_bytes() for p in (self.root / 'tasks').glob('*.md')}
        with self.assertRaisesRegex(ValueError, 'title required'):
            train.finish(self.inst, self.n, path, data)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(json.loads(path.read_text())['state'], 'verified')

    def test_backfill_selects_build_order_and_attaches_open_job(self):
        from brain import tasks_backfill, jobs
        (self.root / 'status.md').write_text('# Status\n## Done\n- Finished\n## Build order\n1. Add preview\n2. Add export\n')
        jobs.job_started(self.inst, self.n, 'job/example', 'codex')
        created = tasks_backfill.backfill(self.inst, self.n, apply=True)
        self.assertEqual(len(created), 3)
        self.assertNotIn('Finished', [r['title'] for r in created])
        job = jobs.read_jobs(self.n)[0]
        task = tasks.read_records(self.n)[job['Task']]
        self.assertEqual(task['job'], jobs.job_key(job))
        self.assertEqual(task['status'], 'in-progress')
        tasks.validate(self.n, tasks.read_records(self.n))

    def test_backfill_rejects_linked_source_and_does_not_invent_linear_issues(self):
        from brain import tasks_backfill
        foreign = self.tmp / 'foreign.md'
        foreign.write_text('- Private external promise\n')
        (self.root / 'backlog.md').symlink_to(foreign)
        with self.assertRaises(ValueError):
            tasks_backfill.backfill(self.inst, self.n, apply=True)
        (self.root / 'backlog.md').unlink()
        (self.root / 'backlog.md').write_text('- DEMO-42 Add export\n- Repair local scripts\n')
        self.n['fm']['tasks.source'] = 'linear'
        created = tasks_backfill.backfill(self.inst, self.n, apply=True)
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0]['id'].startswith('TASK-'))

    def test_rules_index_preserves_superseded_record_and_source_diary(self):
        from brain import task_requirements
        diary = self.root / 'decisions.md'
        diary.write_text('Historical source stays intact\n')
        task_requirements.add_rule(self.inst, self.n, 'D-1', 'Use exports', self.inst.now(), 'decisions.md:1')
        task_requirements.add_rule(self.inst, self.n, 'D-2', 'Use explicit exports', self.inst.now(), 'decisions.md:2', 'D-1')
        rules = tasks.read_records(self.n, 'rules')
        self.assertEqual(rules['D-1']['status'], 'superseded')
        self.assertEqual(rules['D-2']['status'], 'active')
        index = (self.root / 'rules.md').read_text()
        self.assertIn('## D-2', index)
        self.assertNotIn('## D-1', index)
        self.assertIn('- D-1: D-2', index)
        self.assertEqual(diary.read_text(), 'Historical source stays intact\n')

    def test_rules_index_refuses_to_replace_manual_content(self):
        from brain import task_requirements
        path = self.root / 'rules.md'
        path.write_text('Existing manual index\n')
        with self.assertRaises(ValueError):
            task_requirements.add_rule(self.inst, self.n, 'D-1', 'Use exports', self.inst.now(), 'notes.md:1')
        self.assertFalse((self.root / 'rules').exists())
        self.assertEqual(path.read_text(), 'Existing manual index\n')

    def test_migration_preflights_schema_and_is_repeatable(self):
        from brain import tasks_backfill
        (self.root / 'backlog.md').write_text('- Export reports\n')
        self.assertEqual(tasks_backfill.migrate(self.inst, [self.n])[0]['created'], 1)
        self.assertEqual(tasks_backfill.migrate(self.inst, [self.n])[0]['created'], 0)
        path = self.root / 'requirements' / 'REQ-future.md'
        path.write_text('---\nversion: 99\nid: REQ-future\n---\n')
        (self.root / 'backlog.md').write_text('- Export reports\n- Add layout\n')
        with self.assertRaises(ValueError):
            tasks_backfill.migrate(self.inst, [self.n])
        self.assertEqual(len(tasks.read_records(self.n)), 1)

    def linear_config(self):
        self.n['fm'].update({'tasks.source': 'linear', 'tasks.linear.team_id': '11111111-1111-4111-8111-111111111111',
                             'tasks.linear.started_state_id': '22222222-2222-4222-8222-222222222222',
                             'tasks.linear.done_state_id': '33333333-3333-4333-8333-333333333333'})
        tasks._m_common.write_fm(self.n['dir'] / 'node.md', self.n['fm'], '')

    def linear_issue(self, ident='DEMO-1', state='unstarted'):
        return {'id': '44444444-4444-4444-8444-444444444444', 'identifier': ident, 'title': 'Export report',
                'description': 'Product description', 'priority': 2, 'url': 'https://linear.app/example/issue/' + ident,
                'team': {'id': self.n['fm']['tasks.linear.team_id']}, 'state': {'id': '55555555-5555-4555-8555-555555555555', 'type': state},
                'assignee': {'name': 'Developer'}, 'inverseRelations': {'nodes': [], 'pageInfo': {'hasNextPage': False}}}

    def test_linear_pull_preserves_internal_metadata_and_provider_state_owns_fields(self):
        from brain import task_linear
        self.linear_config()
        with mock.patch.object(task_linear, 'collect', return_value=[self.linear_issue()]):
            task_linear.pull(self.inst, self.n)
        tasks.mutate(self.inst, self.n, 'DEMO-1', {'decisions': ['repo:D1305'], 'estimate': '2h'})
        issue = self.linear_issue(state='completed')
        issue['title'] = 'Changed in provider'
        with mock.patch.object(task_linear, 'collect', return_value=[issue]):
            task_linear.pull(self.inst, self.n)
        row = tasks.read_records(self.n)['DEMO-1']
        self.assertEqual(row['status'], 'done')
        self.assertEqual(row['title'], 'Changed in provider')
        self.assertEqual(row['decisions'], ['repo:D1305'])
        with self.assertRaises(ValueError):
            tasks.mutate(self.inst, self.n, 'DEMO-1', {'title': 'Local overwrite'})

    def test_linear_failure_retains_mirror_and_state_intent_without_private_variables(self):
        from brain import task_linear
        self.linear_config()
        with mock.patch.object(task_linear, 'collect', return_value=[self.linear_issue()]):
            task_linear.pull(self.inst, self.n)
        tasks.mutate(self.inst, self.n, 'DEMO-1', {'decisions': ['private:D-123'], 'evidence': ['Internal-only evidence']})
        before = (self.root / 'tasks' / 'DEMO-1.md').read_text()
        with mock.patch.object(task_linear, 'collect', side_effect=ValueError('offline')):
            with self.assertRaises(ValueError):
                task_linear.pull(self.inst, self.n)
        self.assertEqual((self.root / 'tasks' / 'DEMO-1.md').read_text(), before)
        with mock.patch.object(task_linear, 'request', side_effect=ValueError('offline')) as request:
            with self.assertRaises(ValueError):
                task_linear.write_state(self.inst, self.n, 'DEMO-1', 'started')
        self.assertEqual(tasks.read_records(self.n)['DEMO-1']['provider_pending'], 'started')
        self.assertEqual(set(request.call_args.args[1]), {'id', 'state'})
        self.assertNotIn('private', json.dumps(request.call_args.args[1]))
        with mock.patch.object(task_linear, 'request', return_value={'issueUpdate': {'success': True, 'issue': {'id': self.linear_issue()['id']}}}):
            task_linear.write_state(self.inst, self.n, 'DEMO-1', 'started')
        self.assertNotIn('provider_pending', tasks.read_records(self.n)['DEMO-1'])

    def test_linear_pagination_and_duplicate_create_do_not_drop_or_duplicate_issues(self):
        from brain import task_linear
        self.linear_config()
        pages = [{'issues': {'nodes': [self.linear_issue()], 'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}}},
                 {'issues': {'nodes': [self.linear_issue('DEMO-2')], 'pageInfo': {'hasNextPage': False}}}]
        with mock.patch.object(task_linear, 'request', side_effect=pages) as request:
            self.assertEqual(len(task_linear.collect(self.n)), 2)
            self.assertEqual(request.call_args.args[1]['after'], 'next')
        with mock.patch.object(task_linear, 'collect', return_value=[self.linear_issue()]), mock.patch.object(task_linear, 'request') as request:
            row = task_linear.create(self.inst, self.n, ' export REPORT ')
        self.assertEqual(row['id'], 'DEMO-1')
        request.assert_not_called()

    def test_linear_incomplete_dependency_page_refuses_entire_pull(self):
        from brain import task_linear
        self.linear_config()
        issue = self.linear_issue()
        issue['inverseRelations']['pageInfo']['hasNextPage'] = True
        with mock.patch.object(task_linear, 'collect', return_value=[issue]):
            with self.assertRaises(ValueError):
                task_linear.pull(self.inst, self.n)
        self.assertFalse((self.root / 'tasks').exists())

    def test_linear_transport_scrubs_provider_errors_and_source_is_immutable(self):
        import os
        from brain import task_linear
        self.linear_config()
        with mock.patch.object(task_linear, 'collect', return_value=[self.linear_issue()]):
            task_linear.pull(self.inst, self.n)
        with self.assertRaises(ValueError):
            tasks.mutate(self.inst, self.n, 'DEMO-1', {'source': 'local'})
        with mock.patch.dict(os.environ, {'BRAIN_LINEAR_TOKEN': 'lin_api_fixture-secret'}), \
                mock.patch.object(task_linear.urllib.request, 'urlopen', side_effect=OSError('fixture-secret in provider response')):
            with self.assertRaises(ValueError) as error:
                task_linear.request('query { viewer { id } }', {})
        self.assertNotIn('fixture-secret', str(error.exception))

    def test_linear_unknown_blocker_is_preserved_and_blocks_job_start(self):
        from brain import task_linear
        self.linear_config()
        issue = self.linear_issue()
        issue['inverseRelations']['nodes'] = [{'type': 'blocks', 'issue': {'identifier': 'OTHER-42'}}]
        with mock.patch.object(task_linear, 'collect', return_value=[issue]):
            task_linear.pull(self.inst, self.n)
        self.assertEqual(tasks.projection(self.n)[0]['blocked_by'], ['OTHER-42'])
        with self.assertRaises(ValueError):
            tasks.prepare_start(self.n, 'DEMO-1', 'job/example')

    def test_source_task_upsert_is_idempotent_and_preserves_parked_progress(self):
        row = tasks.upsert_source_task(self.inst, self.n, 'enforcement:sample', 'Repair recurring failure', kind='fix')
        ident = row['id']
        tasks.mutate(self.inst, self.n, ident, {'status': 'parked', 'park_reason': 'Two rounds', 'resume': 'Narrow parser', 'review_at': self.inst.now(), 'next_action': 'Repair source'})
        same = tasks.upsert_source_task(self.inst, self.n, 'enforcement:sample', 'New observation')
        self.assertEqual(same['id'], ident)
        self.assertEqual(same['title'], 'Repair recurring failure')
        self.assertEqual(same['status'], 'parked')
        self.assertEqual(len(tasks.read_records(self.n)), 1)

    def test_ui_snapshot_screens_nested_fields_and_keeps_repair_explicit(self):
        from brain import guard
        self.add(description='Private word appears here', evidence=['Private word evidence'])
        with mock.patch.object(guard, 'never_terms', return_value=[('Private word', 'private term')]):
            state = tasks.ui_tasks(self.inst, self.n)
        self.assertNotIn('Private word', json.dumps(state))
        self.assertEqual(len(state['rows']), 1)
        (self.root / 'tasks' / 'APP-1.md').write_text('---\nversion: 99\nid: APP-1\n---\n')
        state = tasks.ui_tasks(self.inst, self.n)
        self.assertEqual(state['rows'], [])
        self.assertTrue(state['error'])

    def peer_node(self, ident, owners='sample'):
        path = self.root / 'nodes' / ident
        path.mkdir(parents=True)
        (path / 'node.md').write_text('---\nid: ' + ident + '\nowners: ' + owners + '\nhome: here\n---\n')
        return self.inst.node(ident)

    def ops_done(self, n, ident='OPS-1'):
        return tasks.mutate(self.inst, n, ident, {'title': 'Restore service', 'source': 'local', 'completion_kind': 'ops',
            'status': 'done', 'evidence': ['Exact operation passed'],
            'completion_receipt': {'result': 'passed', 'ref': 'run:fixture-1', 'scope': 'Restore service', 'at': self.inst.now()}}, create=True)

    def test_cross_node_landing_unblocks_once_and_preserves_boundary(self):
        peer = self.peer_node('producer')
        tasks.mutate(self.inst, peer, 'OPS-1', {'title': 'Restore service', 'completion_kind': 'ops'}, create=True)
        self.add(status='blocked', depends_on=['producer:OPS-1'])
        self.assertEqual(tasks.projection(self.n)[0]['blocked_by'], ['producer:OPS-1'])
        tasks.mutate(self.inst, peer, 'OPS-1', {'status': 'done', 'evidence': ['Operation passed'],
            'completion_receipt': {'result': 'passed', 'ref': 'run:fixture', 'scope': 'Restore service', 'at': self.inst.now()}})
        # The integrated event producer already reconciles the real landing.
        current = tasks.read_records(self.n)['APP-1']
        self.assertEqual(current['status'], 'ready')
        self.assertRegex(current['dependency_event'], r'^[0-9a-f]{32}$')
        before = (self.n['dir'] / 'tasks' / 'APP-1.md').read_bytes()
        self.assertEqual(tasks.reconcile_dependencies(self.inst, 'producer', 'event:one'), [])
        self.assertEqual(tasks.reconcile_dependencies(self.inst, 'producer', 'event:one'), [])
        self.assertEqual((self.n['dir'] / 'tasks' / 'APP-1.md').read_bytes(), before)
        sealed = self.peer_node('sealed', owners='other')
        with self.assertRaises(ValueError):
            tasks.mutate(self.inst, self.n, 'APP-1', {'depends_on': ['sealed:OPS-1']})

    def test_cross_node_cycles_are_rejected_before_mutation(self):
        peer = self.peer_node('producer')
        self.add()
        tasks.mutate(self.inst, peer, 'APP-2', {'title': 'Peer work', 'depends_on': ['root:APP-1']}, create=True)
        with self.assertRaises(ValueError):
            tasks.mutate(self.inst, self.n, 'APP-1', {'depends_on': ['producer:APP-2']})
        self.assertEqual(tasks.read_records(self.n)['APP-1']['depends_on'], [])

    def test_pause_hold_and_reconsideration_date_never_dispatch_or_unpause(self):
        from brain import task_flow
        peer = self.peer_node('producer')
        self.ops_done(peer)
        self.add(status='blocked', depends_on=['producer:OPS-1'], hold='private:D-1 until explicit resume')
        self.assertEqual(tasks.reconcile_dependencies(self.inst, 'producer', 'event:one'), [])
        tasks.mutate(self.inst, self.n, 'APP-1', {'hold': ''})
        self.n['fm']['labels'] = 'state:paused'
        self.assertTrue(task_flow.holds(self.inst, self.n))
        with self.assertRaises(ValueError):
            tasks.prepare_start(self.n, 'APP-1', 'job/example')
        self.n['fm']['labels'] = ''
        self.assertEqual(tasks.reconcile_dependencies(self.inst, 'producer', 'event:two'), [{'node': 'root', 'task': 'APP-1'}])

    def test_park_handover_and_aging_preserve_exact_open_set(self):
        from brain import task_flow
        self.add(status='parked', park_reason='Two failures', resume='Narrow parser')
        old = (self.inst.now_dt() - __import__('datetime').timedelta(days=2)).isoformat()
        tasks.mutate(self.inst, self.n, 'APP-1', {'last_progress_at': old})
        first = [r for r in tasks.due_tasks(self.inst, self.n) if r['kind'] == 'task-aging'][0]
        before = task_flow.brief_index(self.inst, self.n)
        tasks.mutate(self.inst, self.n, 'APP-1', {'estimate': '1h'})
        after = task_flow.brief_index(self.inst, self.n)
        second = [r for r in tasks.due_tasks(self.inst, self.n) if r['kind'] == 'task-aging'][0]
        self.assertEqual(first['episode'], second['episode'])
        self.assertEqual(before, after)
        self.assertIn('APP-1 [parked]', '\n'.join(after))

    def test_operations_and_exact_report_receipts_do_not_borrow_code_proof(self):
        import hashlib
        row = self.ops_done(self.n)
        self.assertEqual(row['status'], 'done')
        artifact = self.root / 'report.md'
        artifact.write_text('Measured result\n')
        receipt = {'result': 'passed', 'ref': 'report:fixture', 'scope': 'Measured result', 'at': self.inst.now(),
                   'artifact': 'report.md', 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'version': '1'}
        self.add('REPORT-1', completion_kind='report', status='done', evidence=['Artifact measured'], completion_receipt=receipt)
        artifact.write_text('Changed later\n')
        with self.assertRaises(ValueError):
            tasks.validate(self.n, tasks.read_records(self.n))

    def test_plain_git_attempt_and_rename_keep_one_obligation(self):
        from brain import task_flow
        self.repo(self.root)
        head = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.git('branch', 'job/first', cwd=self.root)
        self.add(status='ready', scope=['engine.py'])
        task_flow.attempt(self.inst, self.n, 'APP-1', 'attempt:one', branch='job/first', head=head, source_ref='run:first')
        task_flow.alias(self.inst, self.n, 'APP-1', 'job/first', 'job/renamed')
        row = tasks.read_records(self.n)['APP-1']
        self.assertEqual(len(tasks.read_records(self.n)), 1)
        self.assertEqual(row['aliases'], ['job/first', 'job/renamed'])
        self.assertEqual(row['baseline_sha'], head)
        self.assertEqual(len(row['attempts']), 1)
        with self.assertRaises(ValueError):
            tasks.mutate(self.inst, self.n, 'APP-1', {'status': 'merged', 'merge_sha': head,
                'code_path': 'README.md', 'evidence': ['Base is unchanged']})

    def test_open_successor_and_uncovered_scope_prevent_completion(self):
        self.repo(self.root)
        base = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        (self.root / 'engine.py').write_text('VERSION = 1\n')
        self.git('add', 'engine.py', cwd=self.root)
        self.git('commit', '-qm', 'Deliver narrow code', cwd=self.root)
        head = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.add('APP-2')
        self.add(scope=['other.py'], baseline_sha=base)
        for changes in ({'scope': ['other.py']}, {'scope': ['engine.py'], 'superseded_by': ['APP-2']}):
            with self.assertRaises(ValueError):
                tasks.mutate(self.inst, self.n, 'APP-1', {'status': 'done', 'merge_sha': head,
                    'code_path': 'engine.py', 'evidence': ['Narrow code passed'], **changes})

    def test_stopped_register_keeps_raw_receipts_open_and_replays_without_duplicates(self):
        from brain import tasks_backfill
        receipt = {'ref': 'source-report.md:9', 'scope': 'Old teardown behavior was not proven', 'head': 'b' * 40,
                   'outcome': 'retired implementation', 'raw': 'Original scope and failure receipt'}
        register = {'version': 1, 'obligations': [{'node': 'root', 'source_id': 'stopped:one-obligation',
            'title': 'Reconcile teardown guarantees', 'source_ref': 'source-report.md:8',
            'aliases': ['job/old', 'job/new'], 'receipts': [receipt]}]}
        path = self.tmp / 'register.json'
        path.write_text(json.dumps(register))
        first = tasks_backfill.import_register(self.inst, self.n, path, apply=True)
        self.assertEqual(len(first), 1)
        row = tasks.read_records(self.n)[first[0]['task']]
        self.assertEqual(row['status'], 'backlog')
        self.assertTrue(row['reconciliation'])
        self.assertEqual(row['raw_receipts'], [receipt])
        self.assertEqual(row['aliases'], ['job/old', 'job/new'])
        self.assertEqual(tasks_backfill.import_register(self.inst, self.n, path, apply=True), [])
        self.assertEqual(len(tasks.read_records(self.n)), 1)

    def test_code_completion_cannot_use_unchanged_or_unrelated_scope(self):
        self.repo(self.root)
        base = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.add(scope=['engine.py'], baseline_sha=base)
        for head, paths in ((base, 'README.md'), ('b' * 40, 'engine.py')):
            with self.assertRaises(ValueError):
                tasks.mutate(self.inst, self.n, 'APP-1', {'status': 'done', 'merge_sha': head,
                    'code_path': paths, 'evidence': ['Unproven claim']})

    def test_snapshot_marks_stale_completion_as_unproven_instead_of_verified(self):
        row = self.ops_done(self.n)
        row['completion_receipt']['result'] = 'unknown'
        tasks.save_record(self.n, row)
        snapshot = tasks.ui_tasks(self.inst, self.n)
        self.assertEqual(snapshot['rows'][0]['delivery_state'], 'unproven')

    def test_shared_task_sync_keeps_public_default_and_existing_mode(self):
        from brain import task_linear
        self.linear_config()
        path = self.root / 'task-sync.json'
        with mock.patch.object(task_linear, 'sync'):
            self.assertTrue(tasks.provider_pull_on_wake(self.inst, self.n))
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            path.chmod(0o640)
            self.assertTrue(tasks.provider_pull_on_wake(self.inst, self.n))
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(json.loads(path.read_text())['state'], 'synced')

    def test_failed_provider_sync_retains_unknown_hold_without_leaking_error(self):
        from brain import task_linear, task_flow
        self.linear_config()
        with mock.patch.object(task_linear, 'collect', return_value=[self.linear_issue()]):
            task_linear.pull(self.inst, self.n)
        with mock.patch.object(task_linear, 'sync', side_effect=ValueError('sensitive provider error')):
            self.assertFalse(tasks.provider_pull_on_wake(self.inst, self.n))
        state = json.loads((self.root / 'task-sync.json').read_text())
        self.assertEqual(state['state'], 'unknown')
        self.assertNotIn('sensitive', json.dumps(state))
        self.assertTrue(task_flow.holds(self.inst, self.n))
        with self.assertRaises(ValueError):
            tasks.prepare_start(self.n, 'DEMO-1', 'job/example')

    def test_backfill_retains_closed_job_claims_as_reconciliation_and_explicit_dependencies(self):
        from brain import tasks_backfill, jobs
        jobs.job_started(self.inst, self.n, 'job/old-attempt', 'codex')
        rows = jobs.read_jobs(self.n)
        rows[0].update(Ended=self.inst.now(), Outcome='closed', Cause='Retired implementation, scope unresolved')
        jobs.write_jobs(self.inst, self.n, rows)
        (self.root / 'backlog.md').write_text('- APP-1 Import the source\n- APP-2 Share depends_on=APP-1,MISSING-1\n')
        created = tasks_backfill.backfill(self.inst, self.n, apply=True)
        self.assertEqual(len(created), 3)
        current = tasks.read_records(self.n)
        self.assertEqual(current['APP-2']['depends_on'], ['APP-1'])
        self.assertEqual(current['APP-2']['unresolved_dependencies'], ['MISSING-1'])
        task = next(r for r in current.values() if r.get('branch') == 'job/old-attempt')
        self.assertEqual(task['status'], 'backlog')
        self.assertTrue(task['reconciliation'])
        self.assertEqual(task['raw_receipts'][0]['raw']['Cause'], 'Retired implementation, scope unresolved')
        self.assertEqual(tasks_backfill.backfill(self.inst, self.n, apply=True), [])

    def test_backfill_patch_equivalence_maps_rebased_sha_without_closing_scope(self):
        from brain import tasks_backfill
        self.repo(self.root)
        base = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.git('checkout', '-qb', 'job/source', cwd=self.root)
        (self.root / 'engine.py').write_text('VERSION = 1\n')
        self.git('add', 'engine.py', cwd=self.root)
        self.git('commit', '-qm', 'Implement source behavior', cwd=self.root)
        head = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.git('checkout', '-q', 'main', cwd=self.root)
        (self.root / 'other.txt').write_text('Independent change\n')
        self.git('add', 'other.txt', cwd=self.root)
        self.git('commit', '-qm', 'Independent main change', cwd=self.root)
        self.git('cherry-pick', head, cwd=self.root)
        main = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.assertNotEqual(main, head)
        mapping = tasks_backfill.delivery_mapping(self.n, base, head)
        self.assertEqual(mapping['kind'], 'patch-equivalent')
        self.assertEqual(mapping['main_sha'], main)
        self.assertEqual(mapping['code_paths'], ['engine.py'])
        self.assertEqual(tasks_backfill.delivery_mapping(self.n, base, base)['kind'], 'unchanged')

    def test_park_requires_all_resume_fields_and_drop_requires_decision(self):
        self.add(status='ready')
        fields = {'status': 'parked', 'park_reason': 'Two failed rounds', 'resume': 'Repair parser',
                  'review_at': self.inst.now(), 'next_action': 'Narrow parser'}
        for key in ('park_reason', 'resume', 'review_at', 'next_action'):
            with self.assertRaises(ValueError):
                tasks.mutate(self.inst, self.n, 'APP-1', {k: v for k, v in fields.items() if k != key})
        with self.assertRaises(ValueError):
            tasks.mutate(self.inst, self.n, 'APP-1', {'status': 'dropped', 'drop_reason': 'Scope removed'})

    def test_paused_ready_task_is_never_selected_or_started_by_elapsed_time(self):
        from brain import task_flow
        self.add(status='ready')
        self.n['fm']['labels'] = 'state:paused'
        with self.assertRaisesRegex(ValueError, 'held'):
            tasks.prepare_start(self.n, 'APP-1', 'job/example')
        self.assertTrue(task_flow.holds(self.inst, self.n))
        self.n['fm']['labels'] = ''
        self.n['fm']['tasks.hold'] = 'private:D-7 explicit resumption required'
        with self.assertRaisesRegex(ValueError, 'held'):
            tasks.prepare_start(self.n, 'APP-1', 'job/example')

    def test_capacity_counts_active_attempts_and_frees_closed_attempts(self):
        from brain import task_flow
        self.n['fm']['tasks.capacity'] = '1'
        self.add('OPS-1', status='ready', completion_kind='ops')
        task_flow.attempt(self.inst, self.n, 'OPS-1', 'run:one', source_ref='receipt:one')
        self.add('OPS-2', status='ready', completion_kind='ops')
        self.assertTrue(task_flow.holds(self.inst, self.n, tasks.read_records(self.n)['OPS-2']))
        task_flow.attempt(self.inst, self.n, 'OPS-1', 'run:one', source_ref='receipt:one', close=True)
        self.assertFalse(task_flow.holds(self.inst, self.n, tasks.read_records(self.n)['OPS-2']))

    def test_task_landing_event_retries_use_metadata_and_do_not_duplicate(self):
        import types
        package = sys.modules[tasks.__package__]
        stub = types.ModuleType(tasks.__package__ + '.events')
        observed = []
        stub.facts = lambda *args: observed
        def record(inst, n, fact_type, job_key=None, **fields):
            self.assertEqual(fact_type, 'task_landed')
            self.assertEqual(set(fields), {'phase', 'state', 'inputs', 'evidence'})
            self.assertEqual(fields['evidence'], {'ref': 'OPS-1'})
            receipt = {'ref': 'fixture-event', 'data': {'fact_type': fact_type, **fields}}
            observed.append(receipt)
            return receipt
        stub.record_fact = record
        with mock.patch.dict(sys.modules, {stub.__name__: stub}), mock.patch.object(package, 'events', stub, create=True):
            self.ops_done(self.n)
            row = tasks.read_records(self.n)['OPS-1']
            self.assertNotIn('event_pending', row)
            self.assertTrue(tasks.emit_task_landed(self.inst, self.n, row))
            self.assertEqual(len(observed), 1)

    def test_launch_plan_requires_per_task_contract_authority_and_never_creates_worktree(self):
        self.add(status='ready', scope=['engine.py'])
        with self.assertRaises(ValueError):
            tasks.launch_plan(self.inst, self.n)
        contract = self.root / 'task-contract.md'
        contract.write_text('\n'.join('## ' + heading + '\nExplicit scoped behavior\n' for heading in
            ('Outcome', 'Authorized', 'Discretion', 'Stop when', 'Needs consent', 'Done means')))
        tasks.mutate(self.inst, self.n, 'APP-1', {'contract': 'task-contract.md', 'authority': 'private:D-1'})
        plan = tasks.launch_plan(self.inst, self.n)
        self.assertEqual(plan['task'], 'APP-1')
        self.assertEqual(plan['commands'][0], ['brain', 'wt', 'add', 'root', 'job/task-app-1', '--task-id', 'APP-1'])
        self.assertFalse((self.root / 'worktrees').exists())
        self.assertFalse((self.root / 'job-ledger.md').exists())
        self.n['fm']['labels'] = 'state:paused'
        self.assertIsNone(tasks.launch_plan(self.inst, self.n))
