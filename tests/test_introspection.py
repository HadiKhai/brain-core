"""Causal views and metrics use explicit observed metadata, never transcript guesses."""
import datetime
import hashlib
import unittest
from pathlib import Path

import core_loader

CORE=Path(__file__).resolve().parent.parent
KEY='a'*24
HEAD='b'*40


def fact(kind, minute, **fields):
    data={'fact_type':kind,'job_key':KEY,'origin':'observed','at':f'2026-01-01T10:{minute:02d}:00+00:00',**fields}
    return {'id':hashlib.sha256(repr(data).encode()).hexdigest()[:32],'kind':'fact','data':data,'ref':'example'}


class CausalIntrospection(unittest.TestCase):
    def setUp(self):
        self.m=core_loader.SourceFileLoader('introspection_fixture',str(CORE/'bin/brain')).load_module()
        self.row={'Job':'job/example','Started':'2026-01-01T10:00:00Z','Ended':'','Outcome':''}
        self.now=datetime.datetime(2026,1,1,11,tzinfo=datetime.timezone.utc)

    def test_phase_blocker_owner_dependency_and_next_gate_follow_explicit_facts(self):
        events=[fact('phase_changed',5,phase='review',state='waiting',reason_code='review_pending',depends_on=['c'*24],head=HEAD)]
        result=self.m.causal_job_view(self.row,KEY,events,{KEY:self.row},self.now,'Implement an example outcome.')
        self.assertEqual(result['outcome'],'Implement an example outcome.')
        self.assertEqual(result['phase'],'review')
        self.assertEqual(result['owner'],'reviewer')
        self.assertEqual(result['clears_when'],'a review verdict is recorded')
        self.assertEqual(result['next_gate'],'review')
        self.assertEqual(result['dependencies'][0]['state'],'unknown')
        self.assertTrue(result['evidence'])
        empty=self.m.causal_job_view(self.row,KEY,[],{},self.now,None)
        self.assertIsNone(empty['phase'])
        self.assertIsNone(empty['owner'])

    def test_future_and_wrong_job_facts_cannot_change_current_phase(self):
        wrong=fact('phase_changed',5,phase='finished',state='done')
        wrong['data']['job_key']='d'*24
        future=fact('phase_changed',5,phase='finished',state='done')
        future['data']['at']='2026-01-02T10:05:00Z'
        view=self.m.causal_job_view(self.row,KEY,[wrong,future],{},self.now,None)
        self.assertIsNone(view['phase'])
        self.assertEqual(view['evidence'],[])

    def test_communication_metrics_count_recorded_merges_waits_and_decision_turn_share(self):
        start=datetime.datetime(2026,1,1,10,tzinfo=datetime.timezone.utc)
        events=[fact('question_opened',5,evidence={'ref':'question-a'}),
                fact('question_answered',15,evidence={'ref':'question-a'}),
                fact('question_opened',20,evidence={'ref':'question-b'}),
                fact('turn_completed',25,reason_code='decision',evidence={'ref':'turn-a'}),
                fact('turn_completed',35,reason_code='routine',evidence={'ref':'turn-b'})]
        merged={**self.row,'Ended':'2026-01-01T10:30:00Z','Outcome':'merged'}
        metrics=self.m.communication_metrics([merged],events,start,self.now,{'count':4,'oldest_wait_seconds':700,'source':'native'})
        self.assertEqual(metrics['queue_depth'],4)
        self.assertEqual(metrics['merges_per_hour'],1)
        self.assertEqual(metrics['question_wait_seconds']['answered_mean'],600)
        self.assertEqual(metrics['question_wait_seconds']['oldest_open'],2400)
        self.assertEqual(metrics['decision_turn_share'],0.5)
        self.assertEqual(metrics['turns_observed'],2)
        unknown=self.m.communication_metrics([],[],start,self.now,{'count':0,'oldest_wait_seconds':None,'source':'native'})
        self.assertIsNone(unknown['decision_turn_share'])
        self.assertIsNone(unknown['question_wait_seconds']['answered_mean'])


from scenario import Scenario
from unittest import mock
import contextlib
import io
import json
import os


class IntrospectionCommand(Scenario):
    def test_explicit_events_feed_causal_cli_and_missing_metrics_stay_unknown(self):
        root=self.init('oak')
        m=core_loader.SourceFileLoader('causal_command_fixture',str(CORE/'bin/brain')).load_module()
        with mock.patch.dict(os.environ,self.env,clear=True):
            inst=m.Instance(root)
            n=inst.node('root')
            row={**dict.fromkeys(m.JOB_COLS,''),'Job':'job/example','Started':inst.now_dt().isoformat()}
            m.write_jobs(inst,n,[row])
            key=m.job_key(row)
            m.record_fact(inst,n,'phase_changed',job_key=key,phase='review',state='waiting',reason_code='review_pending',head=HEAD)
        result=json.loads(self.b(root,'introspect','root','--branch','job/example'))
        self.assertEqual(result['jobs'][0]['owner'],'reviewer')
        self.assertEqual(result['jobs'][0]['blocker'],'review_pending')
        self.assertIsNone(result['metrics']['decision_turn_share'])
        self.assertIn('recorded',result['metrics']['scope'])
