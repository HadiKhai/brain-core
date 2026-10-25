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


class AcknowledgementAdvisories(unittest.TestCase):
    def setUp(self):
        self.m=core_loader.SourceFileLoader('ack_advisory_fixture',str(CORE/'bin/brain')).load_module()
        self.now=datetime.datetime(2026,1,1,11,tzinfo=datetime.timezone.utc)

    def test_wait_for_explicitly_unrequired_receipt_is_advised_until_progress(self):
        delivered=fact('handoff_delivered',1,state='done',reason_code='ack_not_required',evidence={'ref':'delivery-a'})
        wait=fact('phase_changed',2,phase='acknowledgement',state='waiting',reason_code='delivery_receipt',evidence={'ref':'delivery-a'})
        result=self.m.acknowledgement_wait_advisories([delivered,wait],KEY,self.now)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['kind'],'needless-acknowledgement-wait')
        progress=fact('phase_changed',3,phase='execution',state='working')
        self.assertEqual(self.m.acknowledgement_wait_advisories([delivered,wait,progress],KEY,self.now),[])

    def test_required_missing_or_different_receipt_does_not_advise(self):
        wait=fact('phase_changed',3,phase='acknowledgement',state='waiting',reason_code='delivery_receipt',evidence={'ref':'delivery-a'})
        self.assertEqual(self.m.acknowledgement_wait_advisories([wait],KEY,self.now),[])
        delivered=fact('handoff_delivered',1,state='done',reason_code='ack_not_required',evidence={'ref':'delivery-b'})
        self.assertEqual(self.m.acknowledgement_wait_advisories([delivered,wait],KEY,self.now),[])
        delivered['data']['evidence']['ref']='delivery-a'
        required=fact('acknowledgement_required',2,evidence={'ref':'delivery-a'})
        self.assertEqual(self.m.acknowledgement_wait_advisories([delivered,required,wait],KEY,self.now),[])


class UnchangedVerificationAdvisories(unittest.TestCase):
    def setUp(self):
        self.m=core_loader.SourceFileLoader('rerun_advisory_fixture',str(CORE/'bin/brain')).load_module()
        self.now=datetime.datetime(2026,1,1,11,tzinfo=datetime.timezone.utc)

    def completed(self,minute,ref,**changes):
        fields={'state':'done','head':HEAD,'base':'c'*40,'inputs':'d'*64,
                'reason_code':'full_pass','evidence':{'ref':ref}}
        fields.update(changes)
        return fact('verify_completed',minute,**fields)

    def test_same_successful_full_inputs_advise_with_both_run_receipts(self):
        first=self.completed(1,'verify-a')
        second=self.completed(2,'verify-b')
        result=self.m.unchanged_verification_advisories([first,second],KEY,self.now)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['kind'],'unchanged-input-rerun')
        self.assertEqual(len(result[0]['evidence']),2)
        duplicate=self.completed(2,'verify-a')
        self.assertEqual(self.m.unchanged_verification_advisories([first,duplicate],KEY,self.now),[])

    def test_changes_forced_reruns_failures_and_missing_inputs_do_not_match(self):
        first=self.completed(1,'verify-a')
        for changes in ({'head':'e'*40},{'base':'e'*40},{'inputs':'e'*64},
                        {'reason_code':'forced_reverify'},{'reason_code':'full_failed'},{'inputs':None}):
            second=self.completed(2,'verify-b',**changes)
            self.assertEqual(self.m.unchanged_verification_advisories([first,second],KEY,self.now),[])


class InfrastructureAdvisories(unittest.TestCase):
    def setUp(self):
        self.m=core_loader.SourceFileLoader('infra_advisory_fixture',str(CORE/'bin/brain')).load_module()
        self.now=datetime.datetime(2026,1,1,11,tzinfo=datetime.timezone.utc)

    def test_explicit_infrastructure_phase_time_exceeding_product_is_reported(self):
        events=[fact('product_work_requested',0,product=True,evidence={'ref':'outcome-a'}),
                fact('phase_changed',5,phase='execution',state='working',product=False),
                fact('phase_changed',25,phase='execution',state='working',product=True),
                fact('phase_changed',35,phase='coordination',state='working')]
        result=self.m.infrastructure_displacement_advisories(events,KEY,self.now)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['infrastructure_phase_seconds'],1200)
        self.assertEqual(result[0]['product_phase_seconds'],600)
        self.assertTrue(result[0]['informational'])
        delivered=fact('product_outcome_delivered',40,state='done',evidence={'ref':'outcome-a'})
        self.assertEqual(self.m.infrastructure_displacement_advisories([*events,delivered],KEY,self.now),[])

    def test_unknown_classification_nonproduct_requests_and_more_product_time_are_quiet(self):
        request=fact('product_work_requested',0,product=True,evidence={'ref':'outcome-a'})
        unknown=fact('phase_changed',5,phase='execution',state='working')
        self.assertEqual(self.m.infrastructure_displacement_advisories([request,unknown],KEY,self.now),[])
        product=fact('phase_changed',5,phase='execution',state='working',product=True)
        infra=fact('phase_changed',55,phase='execution',state='working',product=False)
        self.assertEqual(self.m.infrastructure_displacement_advisories([request,product,infra],KEY,self.now),[])
        self.assertEqual(self.m.infrastructure_displacement_advisories([infra],KEY,self.now),[])


class PreviewTestingAdvisories(unittest.TestCase):
    def setUp(self):
        self.m=core_loader.SourceFileLoader('preview_advisory_fixture',str(CORE/'bin/brain')).load_module()
        self.now=datetime.datetime(2026,1,1,11,tzinfo=datetime.timezone.utc)
        self.runs=[fact('verify_completed',i,state='done',head=HEAD,evidence={'ref':f'run-{i}'}) for i in (1,2,3)]

    def test_three_distinct_runs_need_current_usable_preview_receipt(self):
        result=self.m.preview_testing_advisories(self.runs,KEY,self.now,HEAD)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['test_runs'],3)
        usable=fact('preview_usable',4,state='done',head=HEAD,evidence={'ref':'preview-a'})
        self.assertEqual(self.m.preview_testing_advisories([*self.runs,usable],KEY,self.now,HEAD),[])
        unusable=fact('preview_unusable',5,head=HEAD,evidence={'ref':'preview-b'})
        self.assertEqual(len(self.m.preview_testing_advisories([*self.runs,usable,unusable],KEY,self.now,HEAD)),1)

    def test_stale_preview_duplicate_runs_missing_head_and_exemptions_do_not_claim_proof(self):
        stale=fact('preview_usable',4,state='done',head='c'*40,evidence={'ref':'preview-a'})
        self.assertEqual(len(self.m.preview_testing_advisories([*self.runs,stale],KEY,self.now,HEAD)),1)
        self.assertEqual(self.m.preview_testing_advisories([self.runs[0]]*3,KEY,self.now,HEAD),[])
        self.assertEqual(self.m.preview_testing_advisories(self.runs,KEY,self.now,None),[])
        exempt=fact('preview_exempt',4,head=HEAD,evidence={'ref':'exemption-a'})
        self.assertEqual(self.m.preview_testing_advisories([*self.runs,exempt],KEY,self.now,HEAD),[])
