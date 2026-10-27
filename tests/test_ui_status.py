"""Dashboard status evidence from fabricated local ledgers and host snapshots."""
import datetime
import json
import core_loader
from scenario import CORE, Scenario


class UIStatus(Scenario):
    def setUp(self):
        super().setUp()
        self.m = core_loader.SourceFileLoader('ui_status_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.tmp / 'sample'
        (self.root / '.brain').mkdir(parents=True)
        (self.root / 'instance.md').write_text('---\noperator: maple\ntimezone: UTC\n---\n')
        (self.root / 'node.md').write_text('---\ntitle: Sample\n---\n')
        self.inst = self.m.Instance(self.root)
        self.now = datetime.datetime(2026, 1, 2, 12, tzinfo=datetime.timezone.utc)
        self.inst.now_dt = lambda: self.now
        self.n = self.inst.node('root')

    def test_request_projection_retains_sources_answers_and_explicit_routing(self):
        line = '- 2026-01-02 10:00 +00:00 from worker: question: Which color?; options: Blue | Gray; recommendation: Blue'
        order = '- 2026-01-02 10:01 +00:00 from coordinator: DECISION verify-speed: Land the fix'
        (self.root / 'inbox.md').write_text(line + '\n' + order + '\n')
        item = {'id': self.m.request_episode_id(self.inst, self.n, line), 'node': 'root', 'kind': 'inbox', 'text': 'inbox: ' + line[2:]}
        before = (self.root / 'inbox.md').read_bytes()
        projected = self.m.ui_inbox_projection(self.inst, [self.n], [item])
        self.assertEqual(projected[0]['recipient'], 'maple')
        self.assertEqual(projected[0]['options'], ['Blue', 'Gray'])
        self.assertEqual(projected[0]['recommendation'], 'Blue')
        self.assertTrue(projected[0]['question'])
        self.assertFalse(projected[1]['open'])
        self.assertEqual(projected[1]['source'], 'root/inbox.md')
        self.assertEqual((self.root / 'inbox.md').read_bytes(), before)
        self.assertFalse((self.root / '.brain/wake').exists())
        self.m.record_answer(self.inst, self.n, line, item['id'], 'Blue')
        archived = self.m.ui_inbox_projection(self.inst, [self.n], [])
        self.assertTrue(archived[0]['answered'])
        self.assertFalse(archived[0]['open'])
        self.assertEqual(archived[0]['text'], line[2:])
        self.assertEqual((self.root / 'inbox.md').read_bytes(), before)

    def test_structured_question_keeps_its_options_deadline_and_answer(self):
        line = '- 2026-01-02 10:00 +00:00 from notification: question: Which color? <!-- communication:abcdef -->'
        questions = {'abcdef': {'node': 'root', 'options': [{'id': 'a', 'label': 'Blue'}, {'id': 'b', 'label': 'Gray'}],
                                'recommendation': 'a', 'deadline': '2026-01-03T00:00:00Z', 'answer': 'a'}}
        result = self.m.ui_request_metadata(self.inst, self.n, line, questions, self.m.lifecycle_empty())
        self.assertEqual(result['answer'], 'a')
        self.assertEqual(result['deadline'], '2026-01-03T00:00:00Z')
        self.assertEqual(result['options'], questions['abcdef']['options'])
        child = {**self.n, 'id': 'child'}
        self.assertEqual(self.m.ui_request_metadata(self.inst, child, line, questions, self.m.lifecycle_empty())['recipient'], '')

    def test_archive_projection_preserves_distinct_retained_episodes_of_the_same_line(self):
        import hashlib
        line = '- 2026-01-02 10:00 +00:00 from worker: question: Which color?; options: Blue | Gray; recommendation: Blue'
        raw = line + '\n'
        (self.root / 'inbox.md').write_text(raw)
        current = self.m.request_episode_id(self.inst, self.n, line)
        def entry(identity):
            receipt = {'id': identity, 'answered': True}
            return ('<!-- inbox-archive:' + identity + ':' + hashlib.sha256(raw.encode()).hexdigest() + ' -->\n'
                    '<!-- episode ' + json.dumps(receipt) + ' -->\n' + raw)
        archive = self.root / 'inbox-archive.md'
        archive.write_text(entry('a' * 24) + entry('b' * 24) + entry('a' * 24))
        before = archive.read_bytes()
        items = [{'id': current, 'node': 'root', 'kind': 'inbox', 'text': line[2:]}]
        rows = self.m.ui_inbox_projection(self.inst, [self.n], items)
        self.assertEqual([row['id'] for row in rows], [current, 'a' * 24, 'b' * 24])
        self.assertTrue(all(row['answered'] and row['open'] is False for row in rows[1:]))
        self.assertTrue(all(row['source'] == 'root/inbox-archive.md' for row in rows[1:]))
        self.assertEqual(archive.read_bytes(), before)
        self.assertEqual((self.root / 'inbox.md').read_text(), raw)

    def test_archive_projection_preserves_crlf_episode_proof_and_deduplicates(self):
        import hashlib
        raw = '- 2026-01-02 10:00 +00:00 from worker: question: Which color?; options: Blue | Gray; recommendation: Blue\r\n'
        identity = 'c' * 24
        receipt = {'id': identity, 'answered': True}
        entry = ('<!-- inbox-archive:' + identity + ':' + hashlib.sha256(raw.encode()).hexdigest() + ' -->\n'
                 '<!-- episode ' + json.dumps(receipt) + ' -->\n' + raw)
        archive = self.root / 'inbox-archive.md'
        before = (entry + entry).encode()
        archive.write_bytes(before)
        rows = self.m.ui_inbox_projection(self.inst, [self.n], [])
        self.assertEqual([row['id'] for row in rows], [identity])
        self.assertTrue(rows[0]['answered'])
        self.assertFalse(rows[0]['open'])
        self.assertEqual(rows[0]['source'], 'root/inbox-archive.md')
        self.assertEqual(archive.read_bytes(), before)

    def row(self, branch='import:sample', start='2026-01-01T00:00Z', ended=''):
        return {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': branch, 'Started': start, 'Ended': ended}

    def run_record(self, row, **overrides):
        return {'node': 'root', 'job_key': self.m.job_key(row), 'thread': 'sample-worker',
                'run': 'sample-run', 'status': 'failed', 'error': 'provider error', **overrides}

    def write_runs(self, records):
        (self.root / '.brain/worker-runs.json').write_text(json.dumps({'version': 1, 'runs': records}))

    def bind(self, row):
        self.n['local']['workers'] = {self.m.job_key(row): 'sample-worker'}

    def test_open_import_age_and_only_current_bound_worker_fault(self):
        old, current, closed = self.row(start='2025-12-01T00:00Z'), self.row(), self.row('job/closed', ended='2026-01-01T01:00Z')
        self.m.write_jobs(self.inst, self.n, [old, current, closed])
        self.bind(current)
        invalid = [None, {}, self.run_record(current, thread='obsolete-worker'),
                   self.run_record(current, node='other'), self.run_record(old), self.run_record(closed),
                   self.run_record(current, run=''), self.run_record(current, status=None)]
        self.write_runs(invalid)
        value = self.m.ui_work_metadata(self.inst, self.n)
        self.assertEqual(set(value), {'import:sample'})
        self.assertEqual(value['import:sample'], {
            'since': '2026-01-01T00:00:00+00:00', 'age_hours': 36.0, 'fault': False, 'failure_category': ''})
        self.write_runs(invalid + [self.run_record(current, error='model at capacity; sample-secret')])
        value = self.m.ui_work_metadata(self.inst, self.n)['import:sample']
        self.assertTrue(value['fault'])
        self.assertEqual(value['failure_category'], 'at capacity')
        for private in ('sample-secret', 'sample-worker', 'sample-run', 'job_key', 'error'):
            self.assertNotIn(private, json.dumps(value))

    def test_latest_current_run_clears_old_failure_and_categories_are_bounded(self):
        row = self.row()
        self.m.write_jobs(self.inst, self.n, [row])
        self.bind(row)
        failure = self.run_record(row, error='USAGE LIMIT for sample-secret')
        self.write_runs([failure])
        self.assertEqual(self.m.ui_work_metadata(self.inst, self.n)[row['Job']]['failure_category'], 'usage limit')
        self.write_runs([failure, self.run_record(row, run='sample-retry', status='running'),
                         self.run_record(row, run='stale-run', thread='obsolete-worker')])
        self.assertFalse(self.m.ui_work_metadata(self.inst, self.n)[row['Job']]['fault'])
        self.write_runs([self.run_record(row, error='unknown sample-secret')])
        self.assertEqual(self.m.ui_work_metadata(self.inst, self.n)[row['Job']]['failure_category'], 'provider error')

    def test_absent_or_malformed_host_snapshot_never_infers_fault(self):
        row = self.row()
        self.m.write_jobs(self.inst, self.n, [row])
        self.bind(row)
        self.assertFalse(self.m.ui_work_metadata(self.inst, self.n)[row['Job']]['fault'])
        path = self.root / '.brain/worker-runs.json'
        for raw in ('{torn', '[]', '{"runs":{}}', '{"runs":[{"node":"root","job_key":[]}]}'):
            path.write_text(raw)
            self.assertFalse(self.m.ui_work_metadata(self.inst, self.n)[row['Job']]['fault'])
        future = self.row('job/future', start='2026-02-01T00:00Z')
        invalid = self.row('job/unknown', start='unknown')
        self.m.write_jobs(self.inst, self.n, [future, invalid])
        value = self.m.ui_work_metadata(self.inst, self.n)
        self.assertEqual(value['job/future']['age_hours'], 0)
        self.assertIsNone(value['job/unknown']['age_hours'])

    def test_blocked_age_and_explicit_waiting_are_separate_from_faults(self):
        template = ('Choose a sample plan — waiting on {}; clears when approved; since: 2026-01-01T00:00Z; '
                    'recommendation: small; default: keep current')
        for who, expected in [('MAPLE', True), ('the operator', True), ('you', True), ('a service team', False)]:
            text = template.format(who)
            (self.root / 'status.md').write_text('# Sample\n\n## Blocked\n\n- ' + text + '\n')
            metadata = self.m.ui_blocked_metadata(self.inst, self.n, text)
            self.assertEqual(metadata['age_hours'], 36.0)
            self.assertEqual(metadata['waiting_on'], who)
            self.assertEqual(metadata['clears_when'], 'approved')
            self.assertIs(metadata['waiting_on_operator'], expected)
            self.assertNotIn('fault', metadata)
        text = 'A sample issue needs attention; since: 2026-01-01T00:00Z; recommendation: inspect; default: wait'
        (self.root / 'status.md').write_text('## Blocked\n- ' + text + '\n')
        metadata = self.m.ui_blocked_metadata(self.inst, self.n, text)
        self.assertEqual(metadata['waiting_on'], '')
        self.assertFalse(metadata['waiting_on_operator'])

    def test_waiting_fields_are_screened_and_legacy_status_api_is_preserved(self):
        (self.root / 'never-name.txt').write_text('sample-hidden\n')
        text = ('Sample issue — waiting on sample-hidden; clears when sample-hidden approves; '
                'since: 2026-01-01T00:00Z; recommendation: inspect; default: wait')
        (self.root / 'status.md').write_text('## Blocked\n- ' + text + '\n')
        metadata = self.m.ui_blocked_metadata(self.inst, self.n, text)
        self.assertNotIn('sample-hidden', json.dumps(metadata))
        self.assertEqual(self.m.blocked_metadata(self.inst, self.n, 'blocked', text)['age_hours'], 36)


if __name__ == '__main__':
    import unittest
    unittest.main()
