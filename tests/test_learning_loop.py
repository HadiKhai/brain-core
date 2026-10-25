"""Enforcement metrics and task production use scoped fabricated correction evidence."""
import importlib.util
import json
from unittest import mock
import core_loader
from scenario import CORE, Scenario


class LearningEnforcement(Scenario):
    def fixture(self):
        r = self.init('alice')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'specialist', 'new', 'editor', '--title', 'Editor')
        folder = r / 'specialists/editor'
        (folder / 'direction.md').write_text('## Principles\n### one-place: Avoid repetition\nenforced_by: wish\n'
                                           '### bounded: Bound output\nenforced_by: checks/bounds.py\n')
        return r, folder

    def core(self):
        loader = core_loader.SourceFileLoader('enforcement_core', str(CORE / 'bin/brain'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        core = importlib.util.module_from_spec(spec)
        loader.exec_module(core)
        return core

    def record(self, r, text):
        self.b(r, 'lesson', 'editor', '--from', 'app', '--principle', 'one-place', text)

    def test_missing_checks_wish_metrics_and_twice_broken_notice(self):
        r, folder = self.fixture()
        self.assertIn('enforcing check missing', self.b(r, 'check', ok=None))
        metrics = json.loads(self.b(r, 'enforcement', 'root', '--json'))
        editor = next(x for x in metrics if x['target'] == 'editor')
        self.assertEqual((editor['wishes'], editor['total'], editor['wish_percent']), (1, 2, 50.0))
        self.record(r, 'the title repeats itself')
        self.assertNotIn('turn into a check', self.b(r, 'due', 'root'))
        self.record(r, 'the subtitle repeats the title')
        notice = [x for x in json.loads(self.b(r, 'due', 'root', '--json')) if x['kind'] == 'enforcement']
        self.assertEqual(len(notice), 1)
        self.assertIn('turn into a check', notice[0]['text'])
        (r / 'checks').mkdir()
        (r / 'checks/bounds.py').write_text('pass\n')
        self.assertNotIn('enforcing check missing', self.b(r, 'check', ok=None))
        facts = self.b(r, 'status', 'root', '--refresh')
        self.assertIn('wishes', (r / 'status.md').read_text())
        ui = json.loads(self.b(r, 'ui', '--json'))
        self.assertEqual(next(x for x in ui['specialists'] if x['id'] == 'editor')['enforcement']['wish_percent'], 50.0)

    def test_task_specs_are_stable_and_only_explicit_materialization_writes(self):
        r, folder = self.fixture()
        self.record(r, 'a repeated title')
        self.record(r, 'a repeated breadcrumb')
        core = self.core()
        inst = core.Instance(r)
        n = inst.node('root')
        specs = core.enforcement_task_specs(inst, n)
        self.assertEqual(len(specs), 1)
        before = {p: p.read_bytes() for p in folder.iterdir() if p.is_file()}
        self.b(r, 'due', 'root', '--json')
        self.assertEqual(before, {p: p.read_bytes() for p in folder.iterdir() if p.is_file()})
        fake = mock.Mock(return_value={'id': 'TASK-example'})
        core.materialize_enforcement_tasks(inst, n, upsert=fake)
        self.assertEqual(fake.call_args[0][2], specs[0]['source_id'])
        self.record(r, 'a repeated summary')
        self.assertEqual(core.enforcement_task_specs(core.Instance(r), n)[0]['source_id'], specs[0]['source_id'])
        (folder / 'direction.md').write_text((folder / 'direction.md').read_text().replace('enforced_by: wish', 'enforced_by: checks/bounds.py'))
        self.assertEqual(core.enforcement_task_specs(core.Instance(r), n), [])

    def test_inactive_specialists_and_out_of_scope_lessons_are_excluded(self):
        r, folder = self.fixture()
        self.record(r, 'one')
        self.record(r, 'two')
        text = (folder / 'lessons.md').read_text().replace('scope alice:', 'scope other:')
        (folder / 'lessons.md').write_text(text)
        self.assertEqual(self.core().enforcement_task_specs(self.core().Instance(r), self.core().Instance(r).node('root')), [])
        (folder / 'role.md').write_text((folder / 'role.md').read_text().replace('status: active', 'status: proposed'))
        self.assertFalse(any(x['target'] == 'editor' for x in json.loads(self.b(r, 'enforcement', 'root', '--json'))))

    def test_lesson_capture_invokes_mutation_hook_for_the_holder(self):
        import argparse
        r, folder = self.fixture()
        core = self.core()
        inst = core.Instance(r)
        module = core._owners['materialize_enforcement_tasks']
        with mock.patch.object(core._owners['cmd_lesson']._m_nodes, 'load', return_value=inst), \
                mock.patch.object(module, 'materialize_enforcement_tasks') as sync:
            core.cmd_lesson(argparse.Namespace(node=False, at=None, specialist='editor', source='app',
                                              principle='one-place', text='a correction'))
        self.assertEqual(sync.call_args[0][1]['id'], 'root')

    def test_role_rules_accept_corrections_and_keep_the_declaring_source(self):
        r, folder = self.fixture()
        role = folder / 'role.md'
        role.write_text(role.read_text() + '\n## Standards\n### link-labels: Distinct navigation labels\nenforced_by: wish\n')
        for text in ('first repeated link label', 'second repeated link label'):
            self.b(r, 'lesson', 'editor', '--from', 'app', '--principle', 'link-labels', text)
        core = self.core()
        inst = core.Instance(r)
        spec = next(spec for spec in core.enforcement_task_specs(inst, inst.node('root')) if spec['principle'] == 'link-labels')
        self.assertEqual(spec['source_ref'], 'specialists/editor/role.md')
        metric = next(row for row in json.loads(self.b(r, 'enforcement', 'root', '--json')) if row['target'] == 'editor')
        self.assertEqual((metric['total'], metric['wishes']), (3, 2))
