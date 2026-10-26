"""Task authority is checked before the native lifecycle adapter can launch."""
import sys
from unittest import mock
from scenario import Scenario, CORE
sys.path.insert(0, str(CORE / 'lib'))
from brain import task_dispatch, task_flow, tasks, jobs, worker_lifecycle
from brain.instance import Instance


class TaskDispatch(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / 'fixture'
        self.root.mkdir()
        (self.root / 'instance.md').write_text('---\nprincipals: sample\ntimezone: UTC\n---\n')
        (self.root / 'node.md').write_text('---\nowners: sample\nseparation_policy: required\n---\n')
        self.inst = Instance(self.root)
        self.n = self.inst.node('root')
        self.contract = self.root / 'contract.md'
        self.contract.write_text('\n'.join('## ' + h + '\nScoped parser ownership\n' for h in
            ('Outcome', 'Authorized', 'Discretion', 'Stop when', 'Needs consent', 'Done means', 'Why separate')))
        with mock.patch.object(self.inst, 'commit'):
            tasks.mutate(self.inst, self.n, 'APP-1', {'title': 'Parser', 'status': 'ready', 'scope': ['parser.py'],
                'contract': 'contract.md', 'authority': 'repo:D1'}, create=True)

    def prepared(self):
        row = dict.fromkeys(jobs.JOB_COLS, '')
        row.update(Job='job/task-app-1', Started=self.inst.now(), Task='APP-1')
        jobs.write_jobs(self.inst, self.n, [row])
        with mock.patch.object(self.inst, 'commit'):
            tasks.mutate(self.inst, self.n, 'APP-1', {'status': 'in-progress', 'job': jobs.job_key(row), 'branch': row['Job']}, provider=True)
        path = jobs.contract_file(self.n, row)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(self.contract.read_bytes())
        return row

    def test_plan_requires_real_separation_reason_instead_of_generic_authority(self):
        plan = task_flow.launch_plan(self.inst, self.n)
        self.assertEqual(plan['commands'][2][-1], 'Scoped parser ownership')
        self.contract.write_text(self.contract.read_text().split('## Why separate')[0])
        with self.assertRaisesRegex(ValueError, 'separation'):
            task_flow.launch_plan(self.inst, self.n)

    def test_prepared_task_launch_uses_lifecycle_and_rechecks_changed_contract(self):
        row = self.prepared()
        self.n['fm']['tasks.capacity'] = '1'  # this prepared attempt owns the slot
        with mock.patch.object(worker_lifecycle, 'launch_worker', return_value={'state': 'done'}) as launch:
            self.assertEqual(task_dispatch.launch_task(self.inst, self.n, 'APP-1')['state'], 'done')
            self.assertEqual(launch.call_args.args[2], jobs.job_key(row))
            self.contract.write_text(self.contract.read_text() + '\nChanged authority\n')
            with self.assertRaisesRegex(ValueError, 'contract'):
                task_dispatch.launch_task(self.inst, self.n, 'APP-1')
            self.assertEqual(launch.call_count, 1)

    def test_hold_or_missing_prepared_job_never_calls_lifecycle(self):
        with mock.patch.object(worker_lifecycle, 'launch_worker') as launch:
            with self.assertRaises(ValueError):
                task_dispatch.launch_task(self.inst, self.n, 'APP-1')
            self.prepared()
            self.n['fm']['tasks.hold'] = 'Explicit hold'
            with self.assertRaises(ValueError):
                task_dispatch.launch_task(self.inst, self.n, 'APP-1')
            launch.assert_not_called()
