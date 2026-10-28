"""Focused advisory policy tests with synthetic records only."""
import argparse
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

import core_loader

CORE = Path(__file__).resolve().parent.parent


class SeparationPolicy(unittest.TestCase):
    def setUp(self):
        self.m = core_loader.SourceFileLoader('advisory_policy', str(CORE / 'bin/brain')).load_module()
        self.n = {'fm': {'separation_policy': 'required'}}
        self.a = argparse.Namespace(why_separate=None, allow_unexplained=False)
        self.guard = mock.patch.object(self.m, 'no_terms_in')
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def test_required_policy_refuses_absent_reason(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.m.separation_lines(self.a, self.n, None)

    def test_required_policy_accepts_explicit_contract_default_and_urgent_reasons(self):
        self.a.why_separate = 'Independent interface ownership.'
        self.assertIn(self.a.why_separate, self.m.separation_lines(self.a, self.n, None)[0])
        self.a.why_separate = None
        self.assertIn('Independent review', self.m.separation_lines(self.a, self.n, None, 'Independent review')[0])
        with mock.patch.object(self.m, 'read_jobs', return_value=[{}]), \
             mock.patch.object(self.m, 'open_job', return_value={'Job': 'job/example'}), \
             mock.patch.object(self.m, 'contract_file') as contract:
            contract.return_value.is_file.return_value = True
            contract.return_value.read_text.return_value = '## Why separate\nIndependent storage ownership.\n'
            self.assertIn('Independent storage', self.m.separation_lines(self.a, self.n, 'job/example')[0])
        self.a.allow_unexplained = True
        self.assertIn('explicitly waived', self.m.separation_lines(self.a, self.n, None)[0])

    def test_legacy_warning_and_invalid_policy(self):
        self.n['fm'] = {}
        with contextlib.redirect_stderr(io.StringIO()) as stream:
            self.m.separation_lines(self.a, self.n, None)
        self.assertIn('warning:', stream.getvalue())
        self.n['fm']['separation_policy'] = 'silently-ignore'
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.m.separation_lines(self.a, self.n, None)


class NestedBuildOrder(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.folder = tempfile.TemporaryDirectory(prefix='advisory-test-')
        self.addCleanup(self.folder.cleanup)
        self.m = core_loader.SourceFileLoader('nested_order', str(CORE / 'bin/brain')).load_module()
        self.n = {'dir': Path(self.folder.name), 'fm': {'parallel_jobs': '8'}, 'archived': False}

    def tasks(self, text, rows=()):
        (self.n['dir'] / 'backlog.md').write_text(text)
        return self.m.runnable_work(self.n, list(rows))

    def test_nested_and_numbered_tasks_inherit_parent_blocks(self):
        text = '''## Build order
- Milestone
  * [ ] First
  + [ ] Second
- [x] Completed group
  - [ ] Not authorized
- Deferred group (parked: dependency)
  - [ ] Also not authorized
- Choice group (waits: operator)
  - [ ] Still not authorized
1. [ ] Third
## Other
- [ ] Outside
'''
        self.assertEqual(self.tasks(text), ['First', 'Second', 'Third'])

    def test_exact_code_span_branch_punctuation_and_plain_boundaries(self):
        rows = [{'Job': 'job/plan,extra', 'Ended': ''}, {'Job': 'job/a;b', 'Ended': ''}]
        text = '''## Build order
- [ ] Active `job/plan,extra`
  - [ ] Child of active work
- [ ] Active job/a;b
- [ ] Different `job/plan`
- [ ] Next
'''
        self.assertEqual(self.tasks(text, rows), ['Different `job/plan`', 'Next'])
        self.assertEqual(self.tasks('## Build order\n- [ ] Other `job/plan,extra`\n',
                                    [{'Job':'job/plan','Ended':''}]), ['Other `job/plan,extra`'])

    def test_build_section_depth_comments_fences_and_capacity(self):
        text = '''### Build order
  - [ ] First
  ```md
  - [ ] Example
  ```
<!-- - [ ] Hidden -->
#### Stage
2) [ ] Second
### Other
- [ ] Outside
'''
        self.assertEqual(self.tasks(text), ['First', 'Second'])
        self.n['fm']['parallel_jobs'] = '1'
        self.assertEqual(self.tasks(text), ['First'])


class CoordinatorAdvisories(unittest.TestCase):
    def setUp(self):
        import datetime
        import tempfile
        from types import SimpleNamespace
        self.folder = tempfile.TemporaryDirectory(prefix='coordinator-advisory-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.now = datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.timezone.utc)
        self.m = core_loader.SourceFileLoader('coordinator_advisory', str(CORE / 'bin/brain')).load_module()
        self.n = {'id': 'app', 'dir': self.root / 'nodes/app', 'local': {'coordinator': 'fixture-thread'}, 'fm': {}, 'archived': False}
        self.inst = SimpleNamespace(root=self.root, now_dt=lambda:self.now, nodes=lambda:[self.n],
                                    local_of=lambda nid: {})
        (self.root / '.brain').mkdir()

    def pending(self, count=1, age=0):
        import json
        import os
        directory = self.m.wake_directory(self.inst, self.n)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ('a'*64+'.pending')
        path.write_text(json.dumps({'items':[{'kind':'handback','id':format(i,'064x')} for i in range(count)]}))
        stamp = self.now.timestamp() - age
        os.utime(path, (stamp,stamp))
        return path

    def host(self, **changes):
        import json
        record = {'node':'app','thread':'fixture-thread','run':'fixture-run','status':'active',
                  'started_at':'2026-01-01T11:35:00+00:00','queued_wakes':[{'id':'wake-a','queued_at':'2026-01-01T11:59:00+00:00'}]}
        record.update(changes)
        (self.root / '.brain/coordinator-runs.json').write_text(json.dumps({'version':1,'observed_at':self.now.isoformat(),'runs':[record]}))

    def test_queue_thresholds_and_stable_episode(self):
        path = self.pending(count=3, age=600)
        self.assertEqual(self.m.coordinator_advisories(self.inst), [])
        self.pending(count=4, age=600)
        items = self.m.coordinator_advisories(self.inst)
        self.assertEqual([i['kind'] for i in items], ['coordinator-queue'])
        episode = items[0]['episode']
        import datetime
        self.now += datetime.timedelta(minutes=1)
        self.assertEqual(self.m.coordinator_advisories(self.inst)[0]['episode'], episode)
        path.unlink()
        self.assertEqual(self.m.coordinator_advisories(self.inst), [])
        self.pending(count=1, age=601)
        self.assertEqual(len(self.m.coordinator_advisories(self.inst)),1)

    def doorbell(self, stem, first, count, age):
        import json
        import os
        directory = self.m.wake_directory(self.inst, self.n)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (stem*64+'.pending')
        path.write_text(json.dumps({'items':[{'kind':'handback','id':format(i,'064x')} for i in range(first, first+count)]}))
        stamp = self.now.timestamp() - age
        os.utime(path, (stamp,stamp))

    def test_doorbells_inside_rate_window_are_transport_not_queue(self):
        window = self.m.WAKE_INTERVAL + self.m.COORDINATOR_TRANSPORT_GRACE
        self.doorbell('a', 0, 6, window - 5)
        self.doorbell('b', 6, 3, 1)
        self.assertEqual(self.m.coordinator_advisories(self.inst), [])
        view = self.m.coordinator_queue_view(self.inst, self.n, self.now.timestamp())
        self.assertEqual((view['count'], view['transport'], view['episode']), (0, 9, None))
        import datetime
        self.now += datetime.timedelta(seconds=10)
        items = self.m.coordinator_advisories(self.inst)
        self.assertEqual([i['kind'] for i in items], ['coordinator-queue'])
        self.assertIn('9 queued item(s)', items[0]['text'])
        hosts = self.m.coordinator_advisories.__globals__['_m_hosts']
        held = {'known':True,'item_count':4,'oldest_at':self.now.timestamp()-30,'unconsumed_wake':False}
        with mock.patch.object(hosts,'wake_turn_queue',return_value=held,create=True):
            self.assertEqual(self.m.coordinator_advisories(self.inst), [])
            held['unconsumed_wake'] = True
            self.assertEqual([i['kind'] for i in self.m.coordinator_advisories(self.inst)], ['coordinator-queue'])

    def test_backlog_episode_is_stable_across_new_doorbells_and_refires_only_when_worse(self):
        import datetime
        self.doorbell('a', 0, 4, 300)
        first = self.m.coordinator_advisories(self.inst)
        self.assertEqual(len(first), 1)
        for minute in range(3):
            self.doorbell('bcd'[minute], 10 + minute, 1, 0)
            self.now += datetime.timedelta(minutes=1)
            items = self.m.coordinator_advisories(self.inst)
            self.assertEqual([i['episode'] for i in items], [first[0]['episode']])
        self.doorbell('e', 20, 1, 0)
        doubled = self.m.coordinator_advisories(self.inst)
        self.assertIn('8 queued item(s)', doubled[0]['text'])
        self.assertNotEqual(doubled[0]['episode'], first[0]['episode'])
        self.now += datetime.timedelta(minutes=10)
        self.assertEqual(self.m.coordinator_advisories(self.inst)[0]['episode'], doubled[0]['episode'])
        self.now += datetime.timedelta(minutes=13)
        aged = self.m.coordinator_advisories(self.inst)
        self.assertNotIn(aged[0]['episode'], (first[0]['episode'], doubled[0]['episode']))

    def test_backlog_episode_survives_coalesced_delivery_and_never_refires_on_improvement(self):
        hosts = self.m.coordinator_advisories.__globals__['_m_hosts']
        stuck = {'known':True,'item_count':4,'oldest_at':self.now.timestamp()-300,'unconsumed_wake':False}
        with mock.patch.object(hosts,'wake_turn_queue',return_value=stuck,create=True):
            first = self.m.coordinator_advisories(self.inst)
        delivered = {**stuck, 'oldest_at': self.now.timestamp()-5, 'unconsumed_wake': True}
        with mock.patch.object(hosts,'wake_turn_queue',return_value=delivered,create=True):
            items = self.m.coordinator_advisories(self.inst)
        self.assertEqual([i['episode'] for i in items], [first[0]['episode']])
        self.assertIn('oldest waiting 5 min', items[0]['text'])
        def snapshot(count, at='11:55'):
            self.host(status='completed', queued_wakes=[{'id':'wake-'+str(i),'queued_at':'2026-01-01T'+at+':00+00:00'}
                                                        for i in range(count)])
            return [i['episode'] for i in self.m.coordinator_advisories(self.inst)]
        (worse,) = snapshot(8)
        self.assertNotEqual(worse, first[0]['episode'])
        self.assertEqual([snapshot(7), snapshot(2), snapshot(8)], [[worse]] * 3)
        self.assertNotIn(worse, snapshot(16))
        snapshot(0)
        self.assertNotIn(worse, snapshot(8, at='11:58'))

    def test_transient_unknown_projection_keeps_the_raised_backlog_and_partial_records_are_ignored(self):
        import json
        hosts = self.m.coordinator_advisories.__globals__['_m_hosts']
        known = {'known':True,'item_count':4,'oldest_at':self.now.timestamp()-300,'unconsumed_wake':True}
        with mock.patch.object(hosts,'wake_turn_queue',return_value=known,create=True):
            first = self.m.coordinator_advisories(self.inst)
        with mock.patch.object(hosts,'wake_turn_queue',side_effect=[{'known':False}, {'known':False}, known],create=True):
            self.assertEqual(self.m.coordinator_advisories(self.inst), first)
        (self.root / '.brain/coordinator-queue.json').write_text(json.dumps({'version':1,'conditions':{
            'app':{'thread':'fixture-thread','start':self.now.timestamp()-300}}}))
        with mock.patch.object(hosts,'wake_turn_queue',return_value=known,create=True):
            self.assertEqual(self.m.coordinator_advisories(self.inst), first)

    def test_ui_projection_reads_recorded_backlog_without_lock_or_write(self):
        import importlib
        readmodel = importlib.import_module(self.m.coordinator_advisories.__globals__['__package__'] + '.ui_readmodel')
        hosts = self.m.coordinator_advisories.__globals__['_m_hosts']
        known = {'known':True,'item_count':4,'oldest_at':self.now.timestamp()-300,'unconsumed_wake':True}
        lock, record = self.root / '.brain/coordinator-queue.lock', self.root / '.brain/coordinator-queue.json'
        with mock.patch.object(hosts,'wake_turn_queue',return_value=known,create=True):
            with readmodel.observe(self.inst), mock.patch('fcntl.flock', side_effect=AssertionError('projection lock')):
                self.assertEqual(len(self.m.coordinator_advisories(self.inst)), 1)
            self.assertFalse(lock.exists() or record.exists())
            first = self.m.coordinator_advisories(self.inst)
            stored = record.read_bytes()
            known['oldest_at'] = self.now.timestamp()-5  # delivery: only the recorded start keeps the episode
            with readmodel.observe(self.inst), mock.patch('fcntl.flock', side_effect=AssertionError('projection lock')):
                self.assertEqual(self.m.coordinator_advisories(self.inst), first)
            self.assertEqual(record.read_bytes(), stored)

    def test_transport_only_doorbells_raise_no_busy_advisory(self):
        import importlib
        observations = importlib.import_module(self.m.coordinator_advisories.__globals__['__package__'] + '.host_observations')
        active = {'app': {'node':'app','thread':'fixture-thread','run':'fixture-run','status':'active',
                          'started':self.now.timestamp()-1500,'queued':None}}
        self.doorbell('a', 0, 5, 10)
        with mock.patch.object(observations,'host_coordinator_runs',return_value=active):
            self.assertEqual(self.m.coordinator_advisories(self.inst), [])
            self.doorbell('a', 0, 5, 900)
            self.assertEqual(sorted(i['kind'] for i in self.m.coordinator_advisories(self.inst)),
                             ['coordinator-busy', 'coordinator-queue'])

    def test_fresh_host_snapshot_count_overrides_native_doorbells(self):
        self.doorbell('a', 0, 6, 900)
        self.assertEqual(len(self.m.coordinator_advisories(self.inst)), 1)
        self.host(status='completed', queued_wakes=[])
        self.assertEqual(self.m.coordinator_advisories(self.inst), [])
        self.host(status='completed', queued_wakes=[{'id':'wake-'+str(i),'queued_at':'2026-01-01T11:59:30+00:00'}
                                                    for i in range(5)])
        items = self.m.coordinator_advisories(self.inst)
        self.assertEqual([i['kind'] for i in items], ['coordinator-queue'])
        self.assertIn('5 queued item(s)', items[0]['text'])
        view = self.m.coordinator_queue_view(self.inst, self.n, self.now.timestamp(),
                                             self.m.coordinator_host_runs(self.inst, self.now.timestamp())['app'])
        self.assertEqual((view['source'], view['transport']), ('fresh_host_snapshot', 0))

    def test_busy_requires_fresh_matching_active_run_and_queued_wakes(self):
        self.host()
        self.assertEqual([i['kind'] for i in self.m.coordinator_advisories(self.inst)], ['coordinator-busy'])
        self.host(thread='old-thread')
        self.assertEqual(self.m.coordinator_advisories(self.inst),[])
        self.host(status='completed')
        self.assertEqual(self.m.coordinator_advisories(self.inst),[])
        self.host(queued_wakes=[])
        self.assertEqual(self.m.coordinator_advisories(self.inst),[])
        self.host()
        import datetime
        self.now += datetime.timedelta(minutes=4)
        self.assertEqual(self.m.coordinator_advisories(self.inst),[])

    def test_coalesced_queue_hook_survives_pending_file_removal_without_claiming_busy(self):
        hosts=self.m.coordinator_advisories.__globals__['_m_hosts']
        snapshot={'known':True,'item_count':4,'oldest_at':self.now.timestamp()-660,'unconsumed_wake':True}
        with mock.patch.object(hosts,'wake_turn_queue',return_value=snapshot,create=True):
            items=self.m.coordinator_advisories(self.inst)
            self.assertEqual([i['kind'] for i in items],['coordinator-queue'])
            view=self.m.coordinator_queue_view(self.inst,self.n,self.now.timestamp())
            self.assertEqual(view['count'],4)
            self.assertEqual(view['source'],'coalesced_native_queue')
        with mock.patch.object(hosts,'wake_turn_queue',return_value={'known':False},create=True):
            self.assertEqual(self.m.coordinator_advisories(self.inst),[])
            self.assertIsNone(self.m.coordinator_queue_view(self.inst,self.n,self.now.timestamp())['count'])

    def test_missing_malformed_future_and_linked_observations_do_not_invent_activity(self):
        self.assertEqual(self.m.coordinator_advisories(self.inst),[])
        self.host(started_at='2026-01-01T12:01:00+00:00')
        self.assertEqual(self.m.coordinator_advisories(self.inst),[])
        self.host(queued_wakes=[{'id':'wake-a','queued_at':'2026-01-01T12:01:00+00:00'}])
        self.assertEqual(self.m.coordinator_advisories(self.inst),[])
        self.host()
        path=self.root / '.brain/coordinator-runs.json'
        path.unlink()
        path.symlink_to(self.root / 'elsewhere')
        self.assertEqual(self.m.coordinator_advisories(self.inst),[])


from scenario import Scenario


class CoordinatorWakeDelivery(Scenario):
    def test_root_wakes_once_per_busy_run_then_rearms_for_new_run(self):
        import datetime
        import json
        import os
        root = self.init('oak')
        self.b(root, 'new', 'app', '--title', 'Application')
        self.b(root, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-send')
        self.b(root, 'set', 'app', 'coordinator=fixture-app')
        m = core_loader.SourceFileLoader('coordinator_delivery', str(CORE / 'bin/brain')).load_module()
        now = datetime.datetime(2026,1,1,12,tzinfo=datetime.timezone.utc)
        data = {'version':1,'observed_at':now.isoformat(),'runs':[{'node':'app','thread':'fixture-app',
                'run':'fixture-run','status':'active','started_at':'2026-01-01T11:35:00Z',
                'queued_wakes':[{'id':'w','queued_at':'2026-01-01T11:59:00Z'}]}]}
        snapshot = root / '.brain/coordinator-runs.json'
        snapshot.write_text(json.dumps(data))
        with mock.patch.dict(os.environ,self.env,clear=True), mock.patch.object(m.Instance,'now_dt',return_value=now), \
             mock.patch.object(m,'wake_start'), mock.patch.object(m,'wake_send',return_value=True) as send, \
             contextlib.redirect_stdout(io.StringIO()):
            inst = m.Instance(root)
            m.watch_once(inst)
            m.wake_drain(inst,inst.node('root'),once=True)
            self.assertEqual(send.call_count,1)
            m.watch_once(inst)
            m.wake_drain(inst,inst.node('root'),once=True)
            self.assertEqual(send.call_count,1)
            data['runs'][0]['run']='fixture-new-run'
            snapshot.write_text(json.dumps(data))
            m.watch_once(inst)
            directory=m.wake_directory(inst,inst.node('root'))
            (directory / 'last').write_text('0')
            m.wake_drain(inst,inst.node('root'),once=True)
            self.assertEqual(send.call_count,2)
