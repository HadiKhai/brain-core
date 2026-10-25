"""Evidence scorecard uses fixture receipts; absence is never a zero."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import core_loader

CORE=Path(__file__).resolve().parent.parent


class NightScorecard(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='scorecard-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.m=core_loader.SourceFileLoader('scorecard_fixture',str(CORE/'bin/brain')).load_module()
        self.artifact=self.root/'observation.json'
        self.artifact.write_text('{"fixture":true}\n')
        self.ref={'path':'observation.json','sha256':hashlib.sha256(self.artifact.read_bytes()).hexdigest()}
        self.data={'version':1,'window':{'start':'2026-01-01T20:00:00Z','end':'2026-01-02T06:00:00Z'},
                   'measurements':{},'must_cases':['flow-a']}

    def measure(self,metric,value,complete=True):
        self.data['measurements'][metric]={'value':value,'coverage':self.data['window'] if complete else None,'evidence':[self.ref]}

    def test_missing_incomplete_and_changed_evidence_are_unknown(self):
        result=self.m.night_scorecard(self.root,self.data,['codex','claude'])
        self.assertEqual(result['metrics']['unauthorized_publications']['status'],'unknown')
        self.measure('unauthorized_publications',0,complete=False)
        self.assertEqual(self.m.night_scorecard(self.root,self.data,[])['metrics']['unauthorized_publications']['status'],'unknown')
        self.measure('unauthorized_publications',0)
        self.artifact.write_text('changed')
        self.assertEqual(self.m.night_scorecard(self.root,self.data,[])['metrics']['unauthorized_publications']['status'],'unknown')

    def test_counts_time_limits_and_runtime_matrix_are_scored(self):
        self.measure('context_reexplanations',0)
        self.measure('stale_answers',1)
        self.measure('project_setup_seconds',120)
        self.measure('machine_setup_seconds',899)
        self.measure('machine_setup_commands',3)
        self.measure('must_use_cases', [{'case':'flow-a','runtime':'codex','passed':True}])
        metrics=self.m.night_scorecard(self.root,self.data,['codex','claude'])['metrics']
        self.assertEqual(metrics['context_reexplanations']['status'],'pass')
        self.assertEqual(metrics['stale_answers']['status'],'fail')
        self.assertEqual(metrics['project_setup_seconds']['status'],'fail')
        self.assertEqual(metrics['machine_setup_seconds']['status'],'pass')
        self.assertEqual(metrics['machine_setup_commands']['status'],'pass')
        self.assertEqual(metrics['must_use_cases']['status'],'unknown')
        self.data['measurements']['must_use_cases']['value'].append({'case':'flow-a','runtime':'claude','passed':False})
        self.assertEqual(self.m.night_scorecard(self.root,self.data,['codex','claude'])['metrics']['must_use_cases']['status'],'fail')

    def test_unsafe_refs_and_wrong_coverage_cannot_pass(self):
        self.measure('checkout_edits',0)
        rec=self.data['measurements']['checkout_edits']
        rec['coverage']={'start':'2026-01-01T21:00:00Z','end':'2026-01-02T06:00:00Z'}
        self.assertEqual(self.m.night_scorecard(self.root,self.data,[])['metrics']['checkout_edits']['status'],'unknown')
        rec['coverage']=self.data['window']
        rec['evidence']=[{**self.ref,'path':'../observation.json'}]
        self.assertEqual(self.m.night_scorecard(self.root,self.data,[])['metrics']['checkout_edits']['status'],'unknown')
        rec['evidence']=[self.ref]
        rec['value']=True
        self.assertEqual(self.m.night_scorecard(self.root,self.data,[])['metrics']['checkout_edits']['status'],'unknown')


from scenario import Scenario


class ScorecardCommand(Scenario):
    def test_command_scores_linked_receipts_without_recording_real_night_success(self):
        root=self.init('oak')
        source=root/'source.json'
        source.write_text('{"count":0}\n')
        manifest=root/'night.json'
        window={'start':'2026-01-01T20:00:00Z','end':'2026-01-02T06:00:00Z'}
        manifest.write_text(json.dumps({'version':1,'window':window,'measurements':{
            'unauthorized_publications':{'value':0,'coverage':window,'evidence':[
                {'path':'source.json','sha256':hashlib.sha256(source.read_bytes()).hexdigest()}]}}}))
        result=json.loads(self.b(root,'scorecard','root','--file',str(manifest)))
        self.assertEqual(result['metrics']['unauthorized_publications']['status'],'pass')
        self.assertEqual(result['status'],'unknown')
        self.assertIn('collector',result['scope'])
