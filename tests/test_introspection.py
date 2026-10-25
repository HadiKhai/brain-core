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


class ReviewClassRecurrence(unittest.TestCase):
    def setUp(self):
        self.m=core_loader.SourceFileLoader('review_class_fixture',str(CORE/'bin/brain')).load_module()

    def receipt(self,thread,head=HEAD,kind='unsafe_path',key=KEY):
        return {'version':1,'job_key':key,'head':head,'observed_at':'2026-01-01T10:00:00Z',
                'reviewer':{'threadId':thread,'runtime':'codex','model':'example-model','fresh_context':True,'context_reused':False,'built_change':False},
                'verdict':'blocked','findings':[{'id':'f-a','class':kind,'severity':'major','state':'open'}]}

    def test_repeated_explicit_class_across_independent_receipts_has_evidence(self):
        first=self.receipt('review-a')
        second=self.receipt('review-b','c'*40,'Unsafe Path')
        result=self.m.review_class_advisories([first,second],KEY)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['failure_class'],'unsafe_path')
        self.assertEqual(result[0]['review_count'],2)
        self.assertEqual(len(result[0]['evidence']),2)
        self.assertEqual(self.m.review_class_advisories([first,first],KEY),[])

    def test_other_jobs_minor_findings_and_reused_context_do_not_establish_recurrence(self):
        first=self.receipt('review-a')
        wrong=self.receipt('review-b',key='d'*24)
        minor=self.receipt('review-c')
        minor['findings'][0]['severity']='minor'
        reused=self.receipt('review-d')
        reused['reviewer']['context_reused']=True
        self.assertEqual(self.m.review_class_advisories([first,wrong,minor,reused],KEY),[])


class PermissionAdvisories(unittest.TestCase):
    def setUp(self):
        self.m=core_loader.SourceFileLoader('permission_advisory_fixture',str(CORE/'bin/brain')).load_module()
        self.now=datetime.datetime(2026,1,1,11,tzinfo=datetime.timezone.utc)

    def test_request_inside_exact_observed_authorization_is_advised(self):
        grant=fact('authorization_granted',1,phase='execution',head=HEAD,reason_code='run_checks',evidence={'ref':'grant-a'})
        request=fact('permission_requested',2,phase='execution',head=HEAD,reason_code='run_checks',evidence={'ref':'grant-a'})
        result=self.m.permission_request_advisories([grant,request],KEY,self.now)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['kind'],'repeated-permission-request')
        self.assertEqual(len(result[0]['evidence']),2)
        self.assertTrue(result[0]['informational'])

    def test_scope_changes_revocation_and_unknown_authority_do_not_advise(self):
        grant=fact('authorization_granted',1,phase='execution',head=HEAD,reason_code='run_checks',evidence={'ref':'grant-a'})
        request=fact('permission_requested',4,phase='execution',head=HEAD,reason_code='run_checks',evidence={'ref':'grant-a'})
        self.assertEqual(self.m.permission_request_advisories([request],KEY,self.now),[])
        revoked=fact('authorization_revoked',3,phase='execution',head=HEAD,reason_code='run_checks',evidence={'ref':'grant-a'})
        self.assertEqual(self.m.permission_request_advisories([grant,revoked,request],KEY,self.now),[])
        changed=fact('phase_changed',3,phase='review',state='working',head=HEAD)
        self.assertEqual(self.m.permission_request_advisories([grant,changed,request],KEY,self.now),[])
        wrong=fact('permission_requested',2,phase='execution',head='c'*40,reason_code='run_checks',evidence={'ref':'grant-a'})
        self.assertEqual(self.m.permission_request_advisories([grant,wrong],KEY,self.now),[])
