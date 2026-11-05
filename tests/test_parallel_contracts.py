"""Parallel forecasts must be explicit and arithmetically sound."""
import unittest

import core_loader
from scenario import CORE

jobs = core_loader.SourceFileLoader('parallel_contract_fixture', str(CORE / 'bin/brain')).load_module()


class ParallelContracts(unittest.TestCase):
    def test_time_target_requires_concurrency(self):
        text = '\n'.join('## ' + field + '\nSynthetic scope.' for field in jobs.CONTRACT_FIELDS)
        text += '\n## Budget\ntarget: 30 s\nsum: 40 x 6 s / 8 = 30 s\n'
        self.assertIn('concurrency:', ' '.join(jobs.contract_problems(text)))

    def test_declaration_matrix_and_rounding(self):
        valid = 'target: 30 s\nconcurrency: 8\nsum: 40 calls x 6 s / 8 = 30 s\n'
        cases = [
            (valid, None),
            (valid.replace('concurrency: 8\n', ''), 'concurrency:'),
            (valid.split('sum:')[0], 'sum:'),
            (valid.replace('8', '1').replace('30 s', '240 s'), 'serial_reason:'),
            (valid.replace('= 30 s', '= 31 s'), 'arithmetic'),
            (valid.replace('/ 8', '/ 4'), 'divisor'),
            (valid.replace('concurrency: 8', 'concurrency: 0'), 'integer'),
            (valid.replace('concurrency: 8', 'concurrency: 2.5'), 'integer'),
            (valid + 'concurrency: 8\n', 'duplicate'),
            ('Paid run with synthetic inputs.', 'concurrency:'),
            ('## Stop when\n- 2 hours elapsed', 'concurrency:'),
            ('Budget: 3 h of agent time', 'sum:'),
            ('cost cap 5 USD', 'concurrency:'),
            ('time box: 30 min', 'sum:'),
            ('ETA 20 min per call, 40 calls', 'concurrency:'),
            ('Stop after 2 hrs', 'concurrency:'),
            ('Budget: 3 hr', 'sum:'),
            ('a 2-hour run', 'concurrency:'),
            ('30-minute limit', 'sum:'),
            ('two-hour cap', 'concurrency:'),
            ('finish within 1h30m', 'sum:'),
            ('about twenty minutes', 'concurrency:'),
            ('spend cap 5€', 'sum:'),
            ('budget 40 $', 'concurrency:'),
            ('Stop after 2wk', 'concurrency:'),
            ('Limit: 5 ticks', 'sum:'),
            ('Budget 40 ₹', 'concurrency:'),
            ('Stop when spending reaches ten', 'sum:'),
            ('costs ten', 'concurrency:'),
            ('spent no more than ten', 'sum:'),
            ('Payments capped', 'concurrency:'),
            ('priced at ten', 'sum:'),
            ('budget of twenty', 'concurrency:'),
            ('the fee is ten', 'sum:'),
            ('ten cents', 'concurrency:'),
            ('ten quid', 'sum:'),
            ('budget twenty bucks', 'concurrency:'),
            ('by tomorrow', 'sum:'),
            ('by Friday', 'concurrency:'),
            ('by EOD', 'sum:'),
            ('tonight', 'concurrency:'),
            ('noon', 'sum:'),
            ('till 17:00', 'concurrency:'),
            ('## Size\n3 files, 300 lines\n', None),
            ('concurrency: 8\nsum: 40 x 6 s / 8 = 30 s plus two hours', 'sum:'),
            ('## Budget\n30M tokens, 2.5 h\n', 'concurrency:'),
            ('target: 1s\n', 'sum:'),
            ('No timed work.', None),
            ('concurrency: 1\nserial_reason: Ordered writes share state.\nsum: 3 x 60 s / 1 = 3 m', None),
            ('concurrency: 3\nsum: 1 x 1 s / 3 = 0.33 s', None),
            ('concurrency: 3\nsum: 1 x 1 s / 3 = 0.32 s', 'arithmetic'),
            ('concurrency: 6\nsum: 6 cheap lanes x 60 s / 6 = 60 s per lane round', None),
        ]
        from importlib import import_module
        validator = import_module(jobs.contract_problems.__module__.rsplit('.', 1)[0] + '.parallel_contracts')
        for text, expected in cases:
            with self.subTest(text=text):
                problems = validator.parallel_problems(text)
                if expected:
                    self.assertIn(expected, ' '.join(problems))
                    self.assertIn('example:', ' '.join(problems))
                else:
                    self.assertEqual(problems, [])


from scenario import Scenario
import test_verify
import test_task_dispatch
from unittest import mock
import os
import time


class ParallelLaunchCommands(Scenario):
    fixture_templates = True
    fixture_init_in_process = True
    job = test_verify.WorkerReadyPackets.job

    def test_registration_brief_hire_and_job_forecast_use_same_refusals(self):
        root, home, wt = self.job()
        self.b(root, 'specialist', 'new', 'tester', '--title', 'Tester')
        module = core_loader.SourceFileLoader('parallel_launch_fixture', str(CORE / 'bin/brain')).load_module()
        inst = module.Instance(root)
        node = inst.node('a')
        row = module.open_job(module.read_jobs(node), 'job')
        self.assertEqual(row['Parallel policy'], '1')
        path = module.contract_file(node, row)
        path.parent.mkdir(exist_ok=True)
        base = '\n'.join('## ' + field + '\nSynthetic scope.' for field in jobs.CONTRACT_FIELDS)
        valid = base + '\n## Budget\ntarget: 30 s\nconcurrency: 8\nsum: 40 x 6 s / 8 = 30 s\n'
        source = self.tmp / 'contract.md'
        variants = [(valid.replace('concurrency: 8\n', ''), 'concurrency:'),
                    (valid.split('sum:')[0], 'sum:'),
                    (valid.replace('concurrency: 8', 'concurrency: 1').replace('/ 8 = 30', '/ 1 = 240'), 'serial_reason:'),
                    (valid.replace('= 30 s', '= 31 s'), 'arithmetic')]
        timings = []
        for text, field in variants:
            source.write_text(text)
            path.write_text(text)
            commands = [('jobs', 'a', '--branch', 'job', '--contract-file', str(source)),
                        ('brief', 'a', '--branch', 'job', '--task', 'Synthetic work.'),
                        ('hire', 'tester', 'a', '--branch', 'job', '--task', 'Synthetic work.'),
                        ('brief', 'a', '--import', '--task', text),
                        ('jobs', 'a', '--branch', 'job')]
            for command in commands:
                with self.subTest(command=command, field=field):
                    started = time.monotonic()
                    self.assertIn(field, self.b(root, *command, ok=False))
                    elapsed = time.monotonic() - started
                    timings.append((command[0], elapsed))
                    self.assertLess(elapsed, 1, (command, elapsed))
        print('contract_refusal_seconds=' + str(timings), flush=True)
        source.write_text(valid)
        self.b(root, 'jobs', 'a', '--branch', 'job', '--contract-file', str(source))
        self.assertIn('Contract for this job', self.b(root, 'brief', 'a', '--branch', 'job', '--task', 'Synthetic work.'))
        self.assertIn('Contract for this job', self.b(root, 'brief', 'a', '--branch', 'job', '--task', valid))
        self.assertIn('Contract for this job', self.b(root, 'hire', 'tester', 'a', '--branch', 'job', '--task', 'Synthetic work.'))
        self.b(root, 'jobs', 'a', '--branch', 'job')
        self.assertIn('Role: import worker', self.b(root, 'brief', 'a', '--import', '--task', valid))
        # Existing open jobs have no policy marker and keep their old contract.
        path.write_text(base + '\n## Budget\n30M tokens, 3 h\n')
        rows = module.read_jobs(node)
        rows[0].pop('Parallel policy')
        with mock.patch.dict(os.environ, self.env, clear=True):
            module.write_jobs(inst, node, rows)
        self.assertIn('Contract for this job', self.b(root, 'brief', 'a', '--branch', 'job', '--task', 'Synthetic work.'))


class ParallelTaskCommands(Scenario):
    def setUp(self):
        super().setUp()
        from brain import tasks
        from brain.instance import Instance
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

    prepared = test_task_dispatch.TaskDispatch.prepared

    def test_task_plan_and_launch_refuse_all_bad_declarations_before_host_calls(self):
        from brain import task_dispatch, task_flow, worker_lifecycle, jobs as native_jobs
        base = self.contract.read_text()
        variants = [('target: 30 s\nsum: 40 x 6 s / 8 = 30 s', 'concurrency:'),
                    ('target: 30 s\nconcurrency: 8', 'sum:'),
                    ('concurrency: 1\nsum: 40 x 6 s / 1 = 240 s', 'serial_reason:'),
                    ('concurrency: 8\nsum: 40 x 6 s / 8 = 31 s', 'arithmetic')]
        for declaration, field in variants:
            self.contract.write_text(base + '\n' + declaration)
            with self.assertRaisesRegex(ValueError, field):
                task_flow.launch_plan(self.inst, self.n)
        valid = base + '\nconcurrency: 8\nsum: 40 x 6 s / 8 = 30 s'
        self.contract.write_text(valid)
        self.assertEqual(task_flow.launch_plan(self.inst, self.n)['task'], 'APP-1')
        row = self.prepared()
        row['Parallel policy'] = '1'
        native_jobs.write_jobs(self.inst, self.n, [row])
        with mock.patch.object(worker_lifecycle, 'launch_worker', return_value={'state': 'done'}) as launch:
            for declaration, field in variants:
                text = base + '\n' + declaration
                self.contract.write_text(text)
                native_jobs.contract_file(self.n, row).write_text(text)
                # Native lifecycle revalidation must refuse even if the host
                # launch entry point itself is stubbed for this fixture.
                with self.assertRaises((ValueError, SystemExit)):
                    task_dispatch.launch_task(self.inst, self.n, 'APP-1')
            launch.assert_not_called()
            self.contract.write_text(valid)
            native_jobs.contract_file(self.n, row).write_text(valid)
            self.assertEqual(task_dispatch.launch_task(self.inst, self.n, 'APP-1')['state'], 'done')
