"""Synthetic host projections: approval ages, routing, schema and read-only bounds."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location('watcher_approval', Path(__file__).resolve().parents[1] / 'tools/watcher_approval.py')
approval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(approval)


class WatcherApproval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.sqlite'
        self.now = 1793880000
        self.local = {'oak': {'home_project': 'home', 'host_project': 'host'}}
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE orchestration_v2_projection_runtime_requests (
                runtime_request_id TEXT PRIMARY KEY, thread_id TEXT, node_id TEXT,
                status TEXT, resolved_at TEXT, created_at TEXT);
            CREATE INDEX orchestration_v2_projection_requests_recovery_idx
                ON orchestration_v2_projection_runtime_requests(thread_id) WHERE status='pending';
            CREATE TABLE orchestration_v2_projection_threads (
                thread_id TEXT PRIMARY KEY, project_id TEXT, title TEXT, deleted_at TEXT);
            CREATE TABLE orchestration_v2_projection_turn_items (
                node_id TEXT, thread_id TEXT, type TEXT, status TEXT, payload_json TEXT);
            CREATE INDEX orchestration_v2_projection_turn_items_node_ordinal_idx
                ON orchestration_v2_projection_turn_items(node_id);
        ''')
        self.events, self.logs = [], []

    def add(self, request='r1', age=121, project='home', status='pending',
            item_status='waiting', kind='approval_request', prompt='git status',
            payload=None, created=None, deleted=None, resolved=None):
        from datetime import datetime, timezone
        thread = 'thread-' + request
        created = created or datetime.fromtimestamp(self.now-age, timezone.utc).isoformat()
        self.conn.execute('INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?)',
                          (thread, project, 'Build\nexample', deleted))
        self.conn.execute('INSERT INTO orchestration_v2_projection_runtime_requests VALUES (?,?,?,?,?,?)',
                          (request, thread, request, status, resolved, created))
        self.conn.execute('INSERT INTO orchestration_v2_projection_turn_items VALUES (?,?,?,?,?)',
                          (request, thread, kind, item_status, payload if payload is not None else json.dumps({'requestId': request, 'prompt': prompt})))
        self.conn.commit()

    def run_rule(self):
        approval.rule_approval(self.local, lambda *args: self.events.append(args),
                               self.db, self.now, self.logs.append)

    def test_strict_two_minute_boundary_and_both_project_bindings(self):
        for i, age in enumerate((119, 120, 120.1, 4200)):
            self.add(str(i), age=age, project='host' if i == 3 else 'home')
        self.run_rule()
        self.assertEqual([e[1] for e in self.events], ['approval:2', 'approval:3'])
        self.assertIn('70.0 min', self.events[1][2])
        self.assertIn('oak: Build example (t3-thread://thread-3)', self.events[1][2])

    def test_resolved_cancelled_expired_deleted_and_user_questions_do_not_fire(self):
        for i, status in enumerate(('resolved', 'cancelled', 'expired')):
            self.add(str(i), status=status)
        self.add('done', item_status='completed')
        self.add('question', kind='user_input')
        self.add('gone', deleted='2026-01-01')
        self.add('resolved', resolved='2026-01-01')
        self.run_rule()
        self.assertEqual(self.events, [])

    def test_unmapped_and_ambiguous_projects_never_route_command_text(self):
        self.local['elm'] = {'host_project': 'home'}
        self.add('foreign', project='foreign')
        self.add('ambiguous')
        self.run_rule()
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.logs), 1)

    def test_prompt_is_one_line_bounded_and_new_request_keeps_own_identity(self):
        self.add(prompt='git\n' + 'x' * 300)
        self.add('r2')
        self.run_rule()
        self.assertEqual(len(self.events), 2)
        self.assertNotIn('\n', self.events[0][2])
        self.assertIn('command: ' + ('git ' + 'x'*300)[:200] + '.', self.events[0][2])
        self.assertEqual(self.events[1][1], 'approval:r2')

    def test_corrupt_json_mismatched_identity_and_bad_dates_fail_closed_once(self):
        self.add('json', payload='{')
        self.add('identity', payload=json.dumps({'requestId': 'wrong', 'prompt': 'git status'}))
        self.add('date', created='bad')
        self.add('naive', created='2026-01-01T00:00:00')
        self.add('future', age=-10)
        self.add('ok')
        self.run_rule()
        self.assertEqual([e[1] for e in self.events], ['approval:ok'])
        self.assertEqual(len(self.logs), 1)

    def test_schema_drift_and_missing_source_do_not_crash_or_create(self):
        self.conn.execute('DROP INDEX orchestration_v2_projection_requests_recovery_idx')
        self.run_rule()
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.logs), 1)
        self.db = self.db.with_name('absent.sqlite')
        self.run_rule()
        self.assertFalse(self.db.exists())

    def test_one_select_is_read_only_and_resolution_stops_notifications(self):
        self.add()
        connect = sqlite3.connect
        traced = []
        def opened(*args, **kwargs):
            self.assertTrue(args[0].endswith('?mode=ro'))
            self.assertTrue(kwargs['uri'])
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(traced.append)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('CREATE TABLE forbidden (x)')
            return conn
        with mock.patch.object(approval.sqlite3, 'connect', side_effect=opened):
            self.run_rule()
        self.assertEqual(len([s for s in traced if s.lstrip().startswith('SELECT')]), 1)
        self.conn.execute("UPDATE orchestration_v2_projection_runtime_requests SET status='resolved'")
        self.conn.commit()
        self.events.clear()
        self.run_rule()
        self.assertEqual(self.events, [])

    def test_query_deadline_fails_closed(self):
        self.add()
        with mock.patch.object(approval, 'APPROVAL_QUERY_S', -1):
            self.run_rule()
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.logs), 1)
