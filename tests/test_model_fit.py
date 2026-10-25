"""Model-fit learning uses comparable recorded outcomes and keeps explicit pins."""
import argparse
import json
import unittest

import core_loader
from scenario import CORE, Scenario


def observation(name, model='swift', *, tokens=100, reviews=1, outcome='merged', **fields):
    return {'Job': 'job/' + name, 'Started': '2026-01-01T10:00Z', 'Ended': '2026-01-01T11:00Z',
            'Outcome': outcome, 'Runtime': 'codex', 'Class': 'build', 'Effort': 'high',
            'Model': model, 'Models': 'codex:' + model + ' 100%', 'Reviews': str(reviews),
            'Fresh in': str(tokens), 'Cache read': '0', 'Cache write': '0', 'Output': '0',
            'Active h': '0.5', 'Usage': 'host: fixture', **fields}


class ModelFitLearning(unittest.TestCase):
    def setUp(self):
        self.m = core_loader.SourceFileLoader('fit_fixture', str(CORE / 'bin/brain')).load_module()
        self.rows = [observation('swift-' + str(i)) for i in range(3)]
        self.rows += [observation('steady-' + str(i), 'steady', tokens=200) for i in range(3)]

    def candidates(self, rows=None):
        return self.m.model_fit_candidates(self.rows if rows is None else rows,
                                          'build', 'codex', 'high', {'swift', 'steady'})

    def test_lower_cost_wins_with_equal_observed_quality(self):
        fits = self.candidates()
        self.assertEqual([f['model'] for f in fits], ['swift', 'steady'])
        self.assertEqual((fits[0]['merged'], fits[0]['samples'], fits[0]['median_tokens']), (3, 3, 100))

    def test_quality_proxies_rank_before_cost_and_new_outcomes_change_choice(self):
        self.rows += [observation('failed', outcome='failed')]
        self.assertEqual(self.candidates()[0]['model'], 'steady')
        self.rows = [observation(str(i), reviews=2, tokens=10) for i in range(3)]
        self.rows += [observation('steady-' + str(i), 'steady', tokens=300) for i in range(3)]
        self.assertEqual(self.candidates()[0]['model'], 'steady')

    def test_sparse_unknown_mixed_open_parked_and_other_classes_are_not_samples(self):
        invalid = [observation('sparse'), observation('wrong-class', Class='docs'),
                   observation('wrong-runtime', Runtime='claude'), observation('wrong-effort', Effort='low'),
                   observation('open', Ended=''), observation('park', outcome='closed'),
                   observation('unknown', Usage='not counted'), observation('switched', Switches='engine -> other'),
                   observation('different', Models='codex:other 100%'),
                   observation('mixed', Models='codex:swift 50%, codex:other 50%'),
                   observation('unreviewed', reviews=0), observation('missing', **{'Fresh in': ''}),
                   observation('nan', **{'Active h': 'nan'})]
        self.assertEqual(self.candidates(invalid), [])

    def test_duplicate_rows_do_not_inflate_confidence_and_conflicts_are_excluded(self):
        row = observation('only')
        self.assertEqual(self.candidates([row, row, row]), [])
        conflicts = self.rows + [{**self.rows[0], 'Fresh in': '999'}]
        self.assertEqual([f['model'] for f in self.candidates(conflicts)], ['steady'])

    def test_declared_allowlist_limits_learned_selection(self):
        fits = self.m.model_fit_candidates(self.rows, 'build', 'codex', 'high', {'steady'})
        self.assertEqual([f['model'] for f in fits], ['steady'])


class ModelFitRouting(Scenario):
    def test_opted_in_unpinned_route_learns_and_respects_every_configured_pin(self):
        root = self.init('oak')
        self.m = core_loader.SourceFileLoader('fit_route_fixture', str(CORE / 'bin/brain')).load_module()
        inst = self.m.Instance(root)
        node = inst.node('root')
        rows = [observation('sample-' + str(i)) for i in range(3)]
        self.m.write_jobs(inst, node, rows)
        a = argparse.Namespace(model=None, effort=None, branch=None)
        self.assertEqual(self.m.class_model_selection(inst, node, a, 'codex', 'build')[0], 'runtime default')
        node['fm'].update(model_fit='on', model_fit_models_codex='swift,steady')
        selected, source = self.m.class_model_selection(inst, node, a, 'codex', 'build')
        self.assertEqual(selected, 'swift')
        self.assertIn('model fit', source)
        for cfg, key in ((inst.cfg, 'route_model_codex'), (inst.cfg, 'route_model_codex_build'),
                         (node['fm'], 'route_model_codex'), (node['fm'], 'route_model_codex_build')):
            cfg[key] = 'pinned'
            self.assertEqual(self.m.class_model_selection(inst, node, a, 'codex', 'build')[0], 'pinned')
            cfg.pop(key)
        a.model = 'explicit'
        self.assertEqual(self.m.class_model_selection(inst, node, a, 'codex', 'build'), ('explicit', '--model'))
        a.model = None
        a.effort = 'low'
        self.assertEqual(self.m.class_model_selection(inst, node, a, 'codex', 'build')[0], 'runtime default')
        a.effort = None
        node['fm']['model_fit_models_codex'] = 'steady'
        self.assertEqual(self.m.class_model_selection(inst, node, a, 'codex', 'build')[0], 'runtime default')

    def test_cli_reports_learning_evidence_without_mutating_config(self):
        root = self.init('oak')
        m = core_loader.SourceFileLoader('fit_cli_fixture', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(root)
        node = inst.node('root')
        rows = [observation('sample-' + str(i)) for i in range(3)]
        m.write_jobs(inst, node, rows)
        self.b(root, 'set', 'root', 'model_fit_models_codex=swift')
        before = (root / 'node.md').read_text()
        result = json.loads(self.b(root, 'model-fit', 'root', '--class', 'build', '--runtime', 'codex', '--effort', 'high'))
        self.assertEqual(result['recommendation'], 'swift')
        self.assertEqual(result['candidates'][0]['samples'], 3)
        self.assertEqual((root / 'node.md').read_text(), before)
        self.b(root, 'set', 'root', 'model_fit=on', 'runtime=codex')
        brief = self.b(root, 'brief', 'root', '--task', 'Inspect sample', '--class', 'build')
        self.assertIn('"model": "swift"', brief)
        self.assertIn('model=model fit (build/codex/high;', brief)

