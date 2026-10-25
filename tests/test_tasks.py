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
        tasks.mutate(self.inst, self.n, 'APP-1', {'status': 'dropped', 'drop_reason': 'Scope removed'})
        self.assertEqual(tasks.projection(self.n)[1]['blocked_by'], ['APP-1'])
        with self.assertRaises(ValueError):
            tasks.mutate(self.inst, self.n, 'APP-2', {'status': 'parked'})

    def test_active_requires_exact_live_job_and_completed_requires_main(self):
        for status in ('in-progress', 'in-review', 'testing', 'merged', 'done'):
            with self.assertRaises(ValueError):
                self.add(status=status, job='wrong', branch='job/sample', merge_sha='a' * 40,
                         code_path='README.md', evidence=['tests passed'])
        self.repo(self.root)
        sha = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.add(status='merged', merge_sha=sha, code_path='README.md', evidence=['focused tests passed'])
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
        self.assertEqual([r['id'] for r in out], ['APP-3'])
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
        (self.root / 'engine.py').write_text('VERSION = 1\n')
        self.git('add', 'engine.py', cwd=self.root)
        self.git('commit', '-qm', 'Implement example', cwd=self.root)
        head = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.add(status='ready')
        jobs.job_started(self.inst, self.n, 'job/example', 'codex', task_id='APP-1')
        row = jobs.read_jobs(self.n)[0]
        row.update(Outcome='merged', **{'Head commit': head})
        with mock.patch.object(sync, 'integration_packet', return_value={'verify_result': 0, 'head': head, 'main': base}):
            tasks.job_closed(self.inst, self.n, row)
        row = tasks.read_records(self.n)['APP-1']
        self.assertEqual(row['status'], 'merged')
        self.assertEqual(row['merge_sha'], head)
        self.assertEqual(row['code_path'], 'engine.py')
