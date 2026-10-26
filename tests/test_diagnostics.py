"""Source-first diagnosis with bounded, recorded setup attempts."""
import json
import unittest
from pathlib import Path

import core_loader
from scenario import Scenario

CORE=Path(__file__).resolve().parent.parent


class DiagnosticPolicy(unittest.TestCase):
    def setUp(self):
        self.m=core_loader.SourceFileLoader('diagnostic_fixture',str(CORE/'bin/brain')).load_module()
        self.sources=[{'path':'module.py','line':1,'blob':'a'*40}]

    def test_source_readable_answer_never_admits_a_probe(self):
        state=self.m.diagnostic_plan(None,'a'*40,'Which config is read?',self.sources,'source','Setting is explicit.')
        self.assertEqual(state['mode'],'source')
        self.assertFalse(state['probe_allowed'])
        self.assertIsNone(state['pending'])

    def test_two_failed_setups_refuse_third_and_preserve_evidence(self):
        state=self.m.diagnostic_plan(None,'a'*40,'Which config is read?',self.sources,'ambiguous','Call chain is dynamic.')
        first=state['pending']
        again=self.m.diagnostic_plan(state,'a'*40,'Which config is read?',self.sources,'ambiguous','Call chain is dynamic.')
        self.assertEqual(again['pending'],first)
        state=self.m.diagnostic_outcome(state,first,'setup-failed')
        state=self.m.diagnostic_plan(state,'a'*40,'Which config is read?',self.sources,'ambiguous','Call chain is dynamic.')
        second=state['pending']
        self.assertNotEqual(first,second)
        state=self.m.diagnostic_outcome(state,second,'setup-failed')
        frozen=json.dumps(state,sort_keys=True)
        with self.assertRaisesRegex(ValueError,'two probe setups failed'):
            self.m.diagnostic_plan(state,'a'*40,'Which config is read?',self.sources,'ambiguous','Call chain is dynamic.')
        self.assertEqual(json.dumps(state,sort_keys=True),frozen)
        with self.assertRaisesRegex(ValueError,'current pending'):
            self.m.diagnostic_outcome(state,first,'observed')

    def test_probe_needs_canonical_source_and_stated_ambiguity(self):
        for sources,reason in (([], 'Unclear'),(self.sources,'')):
            with self.assertRaises(ValueError):
                self.m.diagnostic_plan(None,'a'*40,'Question',sources,'ambiguous',reason)


class DiagnosticCommand(Scenario):
    def test_cli_binds_source_to_job_head_and_records_two_failed_attempts(self):
        root=self.init('oak')
        home=self.repo(self.tmp/'code/app',{'module.py':'SETTING = "example"\n'})
        self.b(root,'new','app','--title','Application','--home',str(home))
        self.b(root,'wt','add','app','job/example')
        base=('diagnose','app','plan','--branch','job/example','--question','Which setting?',
              '--source','module.py:1','--finding','ambiguous','--reason','Call site requires runtime evidence.')
        first=json.loads(self.b(root,*base))
        self.assertTrue(first['probe_allowed'])
        self.assertEqual(first['sources'][0]['line'],1)
        self.assertEqual(json.loads(self.b(root,*base))['pending'],first['pending'])
        def failure(attempt):
            return self.b(root,'diagnose','app','outcome','--branch','job/example','--question','Which setting?',
                          '--attempt',attempt,'--state','setup-failed')
        failure(first['pending'])
        second=json.loads(self.b(root,*base))
        failure(second['pending'])
        self.assertIn('two probe setups failed',self.b(root,*base,ok=False))
        invalid=(*base[:base.index('--source')], '--source','../outside.py:1','--finding','source','--reason','Answer')
        self.assertIn('repo-relative',self.b(root,*invalid,ok=False))
        self.assertIn('line is outside',self.b(root,*base[:base.index('--source')],
            '--source','module.py:999','--finding','source','--reason','Answer',ok=False))
