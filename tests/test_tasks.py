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
        tasks.mutate(self.inst, self.n, ident, {'status': 'parked', 'park_reason': 'Two rounds', 'resume': 'Narrow parser'})
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
