"""Frozen-input hotfix contracts; all brains and diagnostic text are fabricated."""
import contextlib
import copy
import datetime
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from test_rehearse import RehearsalFixture, rehearsal, result
from scenario import CORE, Scenario


# Resolve Apple's launcher once. Tiny fixtures and their subprocesses share the
# selected real Git, without invoking developer-tool discovery for each probe.
_TINY_PATH = None
TINY_INPUT_TESTS = {
    'test_json_stdout_with_shared_stderr_warning_is_valid',
    'test_new_changed_and_removed_stderr_cannot_hide_behind_json',
    'test_protected_terms_split_between_channels_stay_private',
    'test_only_exact_due_receipt_transaction_is_an_allowed_read_write',
    'test_arbitrary_inbox_and_outward_writes_still_fail',
    'test_ui_identities_remain_distinct_before_redaction',
    'test_ui_duplicates_readiness_reasons_types_and_keys_still_fail',
    'test_test_only_candidate_rehearses_with_no_regressions_or_raw_publication',
    'test_plain_copy_and_native_copy_keep_independent_inodes',
    'test_macos_attempts_copy_on_write_then_falls_back_without_live_writes',
    'test_existing_destination_is_never_removed', 'test_successful_output_change_still_fails',
    'test_parsed_ui_ready_reason_change_fails', 'test_parsed_ui_key_loss_and_invalid_output_fail',
    'test_local_diff_retains_newline_only_delta_and_refuses_public_directory',
    'test_normalized_differences_are_named_without_publishing_values',
    'test_json_encoding_difference_is_named', 'test_semantic_operands_survive_the_real_output_pipeline',
    'test_ui_complete_semantics_and_structure_are_compared',
    'test_ui_volatility_allowlist_is_exact_and_reported',
    'test_only_rendered_brief_measurements_are_volatile',
    'test_machine_reason_observations_preserve_policy_and_structure',
}
TINY_PRIVATE_TESTS = {
    'test_raw_diff_is_private_external_and_never_published',
    'test_default_lanes_are_private_and_live_commit_does_not_change_result',
    'test_linked_or_repository_local_diff_directory_refuses',
}
TINY_CLOCK_TESTS = {
    'test_git_relative_cutoffs_in_parent_and_helper_use_the_snapshot_clock',
    'test_data_wall_clocks_are_frozen_but_lock_and_elapsed_clocks_stay_real',
    'test_unsupported_git_clock_refuses_before_probing',
}
TINY_INSTANCE = """import datetime, time
class Instance:
    def __init__(self, root): pass
    def zone(self): return datetime.timezone.utc
    def now_dt(self): return datetime.datetime.now(datetime.timezone.utc)
"""


def tiny_environment(case):
    global _TINY_PATH
    if _TINY_PATH is None:
        env = {'PATH': case.env.get('PATH', '')}
        rehearsal.bind_git_executable(env)
        _TINY_PATH = env['PATH']
    case.env['PATH'] = _TINY_PATH


def tiny_rehearsal_fixture(case):
    case.root = case.tmp / 'live'
    case.root.mkdir()
    (case.root / 'instance.md').write_text('---\ntimezone: UTC\n---\n')
    (case.root / 'node.md').write_text('---\nid: root\n---\n')
    (case.root / 'never-name.txt').write_text('invented-canary\n')
    case.fixture = """import sys
print('PREFIXinvented-canary: ' + sys.argv[1] + ' MODE')
"""
    for core in (case.root / 'core', case.tmp / 'diagnostic-candidate'):
        (core / 'bin').mkdir(parents=True)
        (core / 'bin/brain').write_text(case.fixture.replace('MODE', 'baseline').replace('PREFIX', ''))
        (core / 'lib/brain').mkdir(parents=True)
        (core / 'lib/brain/__init__.py').write_text('')
        (core / 'lib/brain/instance.py').write_text(TINY_INSTANCE)
    case.home = case.tmp / 'diagnostic-candidate'
    case.git('init', '-q', '-b', 'main', cwd=case.home)
    case.git('add', '.', cwd=case.home)
    case.git('commit', '-qm', 'tiny fixture', cwd=case.home)
    case.head = case.git('rev-parse', 'HEAD', cwd=case.home).strip()
    case.git('init', '-q', '-b', 'main', cwd=case.root)
    case.git('add', '.', cwd=case.root)
    case.git('commit', '-qm', 'live snapshot', cwd=case.root)
    case.node = dict(id='engine', external=True, home_path=case.home)
    case.instant = datetime.datetime(2035, 1, 2, 12, tzinfo=datetime.timezone.utc)
    case.instance_at = lambda root: SimpleNamespace(root=root, local={'changes_seen': {'root': []}},
        now_dt=lambda: case.instant, node=lambda _: case.node,
        nodes=lambda: [dict(id='root', dir=root, fm={}, local={}, owners=[], parent=None)])
    case.inst = case.instance_at(case.root)
    case.evidence = case.tmp / 'workbench/scratch/rehearse/evidence.json'


@contextlib.contextmanager
def tiny_bindings(case):
    with mock.patch.dict(os.environ, case.env), \
            mock.patch.object(rehearsal._m_instance, 'Instance', side_effect=case.instance_at), \
            mock.patch.object(rehearsal._m_verify, 'ready_refs', return_value=(case.head, case.head)), \
            mock.patch.object(rehearsal._m_verify, 'integration_parent', return_value=case.head), \
            mock.patch.object(rehearsal._m_jobs, 'stamp', return_value='fixture'), \
            mock.patch.object(rehearsal, 'probe_delivery', return_value=[]), \
            mock.patch.object(rehearsal, 'rehearsal_commands', return_value=([
                ('check', ['check'], 'read'), ('hook', ['hook', 'stop'], 'hook')], [])):
        yield


class FrozenInputs(RehearsalFixture):
    terms = [('invented-canary', 'node'), ('invented-willow', 'node'),
             ('private-branch-a', 'branch'), ('private-branch-b', 'branch')]

    def setUp(self):
        if self._testMethodName in TINY_INPUT_TESTS:
            Scenario.setUp(self)
            tiny_environment(self)
            self.probes = 0
        else:
            super().setUp()

    def probe(self, stdout, stderr='', command='ui', mutation=None):
        self.probes += 1
        lane = self.tmp / ('probe-' + str(self.probes))
        (lane / 'brain').mkdir(parents=True)
        process = mock.Mock(returncode=0, pid=123)
        def communicate(*args, **kwargs):
            if mutation:
                mutation(lane)
            return stdout, stderr
        process.communicate.side_effect = communicate
        with mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                mock.patch.object(rehearsal.subprocess, 'Popen', return_value=process), \
                mock.patch.object(rehearsal.os, 'killpg'):
            return rehearsal.run_command(lane / 'brain/core', lane, [command], 'read', [], self.terms)

    def ui(self):
        def job(branch, ready):
            return dict(branch=branch, ready=dict(ready=ready, reasons=[] if ready else ['held']))
        return dict(nodes=[dict(id='invented-canary', worktrees=[job('private-branch-a', True),
                                                               job('private-branch-b', False)]),
                           dict(id='invented-willow', worktrees=[])], commits_total=7)

    def test_json_stdout_with_shared_stderr_warning_is_valid(self):
        for command, stdout in [('due:0', '[]'), ('ui', json.dumps(self.ui()))]:
            with self.subTest(command=command):
                old = self.probe(stdout, 'warning: invented-canary scan partial\n', command.split(':')[0])
                new = self.probe(stdout, 'warning: invented-canary scan partial\n', command.split(':')[0])
                self.assertEqual([], rehearsal.compare({command: old}, {command: new}))
                self.assertEqual(json.loads(rehearsal.redact_output(stdout, self.terms)), json.loads(new['stdout']))
                self.assertEqual('warning: […] scan partial', new['stderr'])
                self.assertNotIn('invented-canary', json.dumps(new))

    def test_new_changed_and_removed_stderr_cannot_hide_behind_json(self):
        for command, stdout in [('due:0', '[]'), ('ui', '{}'), ('check', '✓ good')]:
            old = self.probe(stdout, 'warning: old\n', command.split(':')[0])
            for warning in ('warning: old\nwarning: new\n', 'warning: changed\n', ''):
                with self.subTest(command=command, warning=warning):
                    new = self.probe(stdout, warning, command.split(':')[0])
                    self.assertIn(command + ': stderr changed', rehearsal.compare({command: old}, {command: new}))
        old = self.probe('[]', command='due')
        malformed = self.probe('not JSON', 'warning: scan partial', command='due')
        self.assertIn('due:0: invalid due output', rehearsal.compare({'due:0': old}, {'due:0': malformed}))
        old = self.probe('[]', 'warning: invented-canary', command='due')
        changed = self.probe('[]', 'warning: invented-willow', command='due')
        self.assertEqual(old['stderr'], changed['stderr'])
        self.assertIn('due:0: stderr changed', rehearsal.compare({'due:0': old}, {'due:0': changed}))

    def test_protected_terms_split_between_channels_stay_private(self):
        row = self.probe('invented-', 'canary', command='check')
        self.assertEqual('[…]', row['text'])
        self.assertNotIn('invented-', row['stdout'])
        self.assertNotIn('canary', row['stderr'])
        self.assertNotIn('invented-canary', json.dumps(row))

    def test_only_exact_due_receipt_transaction_is_an_allowed_read_write(self):
        def transaction(lane):
            state = lane / 'brain/.brain'
            state.mkdir()
            (state / 'docker-leftovers.json').write_text('{}')
            (state / 'docker-leftovers.lock').touch()
        row = self.probe('[]', command='due', mutation=transaction)
        self.assertEqual(0, row['unexpected'])
        for rel in ('brain/.brain/docker-leftovers.json.bak', 'brain/.brain/docker-leftovers.lock/child',
                    'brain/inbox.md', 'brain/nodes/sample/inbox.md', 'home/docker-leftovers.json',
                    'brain/.brain/unknown.json'):
            with self.subTest(path=rel):
                self.assertFalse(rehearsal.allowed_write(rel, 'read'))

    def test_arbitrary_inbox_and_outward_writes_still_fail(self):
        lane = self.tmp / 'sandbox'
        core = lane / 'brain/core'
        (core / 'bin').mkdir(parents=True)
        outside = self.tmp / 'outside'
        outside.write_text('unchanged')
        script = ('from pathlib import Path\nPath("inbox.md").write_text("unexpected")\n'
                  'try:\n    Path(' + repr(str(outside)) + ').write_text("changed")\n'
                  'except OSError:\n    pass\nprint("[]")\n')
        (core / 'bin/brain').write_text(script)
        with mock.patch.dict(os.environ, self.env):
            row = rehearsal.run_command(core, lane, ['due'], 'read', [])
        self.assertEqual(0, row['code'])
        self.assertEqual('unchanged', outside.read_text())
        self.assertEqual(2, row['unexpected'])
        self.assertIn('due:0: unexpected writes', rehearsal.compare({'due:0': row}, {'due:0': row}))

    def test_ui_identities_remain_distinct_before_redaction(self):
        raw = json.dumps(self.ui())
        old, new = self.probe(raw), self.probe(raw)
        self.assertEqual([], rehearsal.compare({'ui': old}, {'ui': new}))
        for term, _ in self.terms:
            self.assertNotIn(term, json.dumps(new))

    def test_ui_duplicates_readiness_reasons_types_and_keys_still_fail(self):
        value = self.ui()
        old = self.probe(json.dumps(value))
        duplicate_node = copy.deepcopy(value)
        duplicate_node['nodes'].append(copy.deepcopy(value['nodes'][0]))
        duplicate_branch = copy.deepcopy(value)
        duplicate_branch['nodes'][0]['worktrees'][1]['branch'] = 'private-branch-a'
        swapped = copy.deepcopy(value)
        jobs = swapped['nodes'][0]['worktrees']
        jobs[0]['ready'], jobs[1]['ready'] = jobs[1]['ready'], jobs[0]['ready']
        changed_reason = copy.deepcopy(value)
        changed_reason['nodes'][0]['worktrees'][1]['ready']['reasons'] = ['invented-willow']
        previous_reason = copy.deepcopy(value)
        previous_reason['nodes'][0]['worktrees'][1]['ready']['reasons'] = ['invented-canary']
        renamed = copy.deepcopy(value)
        renamed['nodes'][0]['id'], renamed['nodes'][1]['id'] = 'invented-willow', 'invented-canary'
        for current in (duplicate_node, duplicate_branch):
            row = self.probe(json.dumps(current))
            self.assertIn('ui: invalid output', rehearsal.compare({'ui': row}, {'ui': row}))
        for current in (swapped, changed_reason, renamed, dict(value, commits_total=True),
                        dict(nodes=value['nodes'])):
            with self.subTest(current=current):
                new = self.probe(json.dumps(current))
                flags = rehearsal.compare({'ui': old}, {'ui': new})
                self.assertIn('ui: semantic output changed', flags)
                self.assertNotIn('ui: invalid output', flags)
        swapped_row = self.probe(json.dumps(swapped))
        self.assertIn('ui: readiness changed', rehearsal.compare({'ui': old}, {'ui': swapped_row}))
        before = self.probe(json.dumps(previous_reason))
        after = self.probe(json.dumps(changed_reason))
        self.assertIn('ui: readiness changed', rehearsal.compare({'ui': before}, {'ui': after}))

    def test_test_only_candidate_rehearses_with_no_regressions_or_raw_publication(self):
        """Same runtime, two warning/receipt due probes and protected UI rows."""
        tiny_rehearsal_fixture(self)
        (self.root / 'never-name.txt').write_text('\n'.join(term for term, _ in self.terms) + '\n')
        fixture = '''import json, sys
from pathlib import Path
if sys.argv[1] == 'due':
    Path('.brain').mkdir(exist_ok=True)
    Path('.brain/docker-leftovers.json').write_text('{}')
    Path('.brain/docker-leftovers.lock').touch()
    print('[]')
    print('Docker leftover report partial: ValueError', file=sys.stderr)
elif sys.argv[1] == 'ui':
    print(UI)
'''.replace('UI', repr(json.dumps(self.ui())))
        for core in (self.root / 'core', self.home):
            (core / 'bin/brain').write_text(fixture)
        self.git('commit', '-qam', 'runtime fixture', cwd=self.home)
        runtime = self.git('rev-parse', 'HEAD:bin', cwd=self.home).strip()
        (self.home / 'tests').mkdir()
        (self.home / 'tests/test_fixture.py').write_text('# test-only change\n')
        self.git('add', 'tests', cwd=self.home)
        self.git('commit', '-qm', 'test-only candidate', cwd=self.home)
        self.head = self.git('rev-parse', 'HEAD', cwd=self.home).strip()
        self.assertEqual(runtime, self.git('rev-parse', 'HEAD:bin', cwd=self.home).strip())
        commands = [('due:1', ['due'], 'read'), ('due:4', ['due'], 'read'),
                    ('ui', ['ui', '--json'], 'read'), ('hook', ['hook', 'stop'], 'hook')]
        printed = io.StringIO()
        with tiny_bindings(self), \
                mock.patch.object(rehearsal, 'rehearsal_commands', return_value=(commands, [])), \
                mock.patch.object(rehearsal._m_nodes, 'find_root', return_value=self.root), \
                mock.patch.object(rehearsal, 'evidence_path', return_value=self.evidence), \
                contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            rehearsal.cmd_rehearse(SimpleNamespace(id='engine', branch='job'))
        evidence = json.loads(self.evidence.read_text())
        self.assertTrue(evidence['passed'], evidence['regressions'])
        self.assertEqual([], evidence['regressions'])
        self.assertEqual(0, evidence['changed_outputs'])
        self.assertEqual(4, evidence['commands'])
        publication = self.evidence.read_text() + printed.getvalue()
        self.assertIn('regressions=0', printed.getvalue())
        for secret in [*(term for term, _ in self.terms), 'Docker leftover report partial',
                       'commits_total', 'worktrees', str(self.tmp)]:
            self.assertNotIn(secret, publication)

    def run_rehearsal(self, *args, **kwargs):
        if self._testMethodName in {'test_normalized_differences_are_named_without_publishing_values',
                                    'test_json_encoding_difference_is_named',
                                    'test_ui_volatility_allowlist_is_exact_and_reported',
                                    'test_machine_reason_observations_preserve_policy_and_structure'}:
            tiny_rehearsal_fixture(self)
            with tiny_bindings(self), mock.patch.object(rehearsal, 'sandbox_self_check'), \
                    mock.patch.object(rehearsal, 'run_lane', side_effect=kwargs['outputs']):
                return rehearsal.rehearse(self.inst, self.node, 'job')
        return super().run_rehearsal(*args, **kwargs)

    def probe_output(self, text, name, command='ui'):
        lane = self.tmp / name
        (lane / 'brain').mkdir(parents=True)
        process = mock.Mock(returncode=0, pid=123)
        process.communicate.return_value = (text, '')
        with mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                mock.patch.object(rehearsal.subprocess, 'Popen', return_value=process), \
                mock.patch.object(rehearsal.os, 'killpg'):
            return rehearsal.run_command(lane / 'brain/core', lane, [command], 'read', [lane / 'brain'])

    def test_semantic_operands_survive_the_real_output_pipeline(self):
        operands = [('2035-01-01 12:00', '2035-01-02 12:00'), ('10 bytes', '20 bytes')]
        for index, (before, after) in enumerate(operands):
            for command in ('ui', 'check', 'fix', 'brief:0', 'ready:0:0', 'due:0'):
                with self.subTest(command=command, operand=before):
                    def render(value):
                        reason = 'limit deadline or quota: ' + value
                        if command == 'ui':
                            return json.dumps({'nodes': [{'id': 'engine', 'worktrees': [
                                {'branch': 'job', 'ready': {'ready': False, 'reasons': [reason]}}]}]})
                        if command.startswith('due:'):
                            return json.dumps([{'node': 'root', 'kind': 'limit', 'id': value}])
                        if command.startswith('ready:'):
                            return 'not ready: ' + reason
                        return ('✗ ' if command in ('check', 'fix') else 'warning: ') + reason
                    old = self.probe_output(render(before), 'old-' + command.replace(':', '-') + str(index), command)
                    new = self.probe_output(render(after), 'new-' + command.replace(':', '-') + str(index), command)
                    self.assertTrue(rehearsal.compare({command: old}, {command: new}))
                    if command in ('check', 'fix'):
                        self.assertNotIn(after, json.dumps(list(new['error_diagnostics'].values())))

    def test_ui_complete_semantics_and_structure_are_compared(self):
        old = {'nodes': [{'id': 'engine', 'worktrees': [], 'jobs': {'open': 7}}], 'commits_total': 7}
        mutations = [dict(old, nodes=[]), dict(old, nodes=[dict(old['nodes'][0], id='other')]),
                     dict(old, nodes=old['nodes'] * 2), dict(old, commits_total=0),
                     dict(old, nodes=[dict(old['nodes'][0], jobs={'open': 0})]),
                     dict(old, nodes=[dict(old['nodes'][0], jobs={'open': True})]),
                     dict(old, nodes=[dict(old['nodes'][0], worktrees=None)])]
        for index, current in enumerate(mutations):
            with self.subTest(current=current):
                before = self.probe_output(json.dumps(old), 'before-ui-' + str(index))
                after = self.probe_output(json.dumps(current), 'after-ui-' + str(index))
                self.assertTrue(rehearsal.compare({'ui': before}, {'ui': after}))

    def test_ui_volatility_allowlist_is_exact_and_reported(self):
        old = {'nodes': [], 'machine': {'load': [1, 2, 3], 'load_per_cpu': [0.1, 0.2, 0.3],
               'mem_free_gb': 10, 'disk_free_gb': 20, 'thermal': 'normal', 'held': [], 'cpus': 7}}
        current = dict(old, machine=dict(old['machine'], load=[2, 3, 4], load_per_cpu=[0.2, 0.3, 0.4],
                                         mem_free_gb=11, disk_free_gb=21))
        before = self.probe_output(json.dumps(old), 'volatile-before')
        after = self.probe_output(json.dumps(current), 'volatile-after')
        outcome = self.run_rehearsal(outputs=[dict(ui=before, hook=result()), dict(ui=after, hook=result())])
        self.assertTrue(outcome['passed'], outcome['regressions'])
        expected = ['ui.machine.disk_free_gb', 'ui.machine.load.0', 'ui.machine.load.1', 'ui.machine.load.2',
                    'ui.machine.load_per_cpu.0', 'ui.machine.load_per_cpu.1', 'ui.machine.load_per_cpu.2',
                    'ui.machine.mem_free_gb']
        self.assertEqual([dict(command='ui', patterns=expected)], outcome['normalized_differences'])
        for mutation in [dict(old['machine'], cpus=0), dict(old['machine'], thermal='pressure'),
                         dict(old['machine'], held=['limit']), dict(old['machine'], load=[1, 2, 3, 4]),
                         dict(old['machine'], mem_free_gb=None), dict(old['machine'], mem_free_gb=-1),
                         dict(old['machine'], mem_free_gb=float('nan')),
                         {key: value for key, value in old['machine'].items() if key != 'mem_free_gb'}]:
            with self.subTest(mutation=mutation):
                self.assertNotEqual(rehearsal.semantic_ui(old), rehearsal.semantic_ui(dict(old, machine=mutation)))
        nested = dict(old, policy={'load': 7}, brain={'now': '2035-01-01 12:00'})
        for current in (dict(nested, policy={'load': 0}),
                        dict(nested, brain={'now': '2035-01-02 12:00'})):
            self.assertNotEqual(rehearsal.semantic_ui(nested), rehearsal.semantic_ui(current))

    def test_only_rendered_brief_measurements_are_volatile(self):
        ending = '. Shorten task/context or use path references; retain limits, guard and hand-back instructions.'
        old = 'warning: brief is 10 bytes (brief_max_kb=8); largest sections: Task: 7 bytes' + ending
        new = old.replace('10 bytes', '11 bytes').replace('7 bytes', '8 bytes')
        before = self.probe_output(old, 'measured-old', 'brief')
        after = self.probe_output(new, 'measured-new', 'brief')
        self.assertEqual([], rehearsal.compare({'brief:0': before}, {'brief:0': after}))
        self.assertIn('brief-rendered-byte-counts', before['normalization_tokens'])
        self.assertNotEqual(before['normalization_tokens']['brief-rendered-byte-counts'],
                            after['normalization_tokens']['brief-rendered-byte-counts'])
        for prefix in ('', 'warning: ', 'brain: '):
            self.assertEqual(rehearsal.semantic_brief_warning(prefix + old.removeprefix('warning: ')),
                             rehearsal.semantic_brief_warning(prefix + new.removeprefix('warning: ')))
        for index, changed in enumerate((new.replace('brief_max_kb=8', 'brief_max_kb=9'),
                                          new.replace('retain limits', 'ignore limits'),
                                          new.replace('Task:', 'Policy:'),
                                          new.replace('11 bytes', '<size> bytes'),
                                          old.replace('10 bytes', '<size> bytes').replace('7 bytes', '<size> bytes'))):
            with self.subTest(changed=changed):
                after = self.probe_output(changed, 'measured-changed-' + str(index), 'brief')
                self.assertIn('brief:0: brief warning disappeared',
                              rehearsal.compare({'brief:0': before}, {'brief:0': after}))

    def test_machine_reason_observations_preserve_policy_and_structure(self):
        reasons = ['5-minute load/CPU 2.00 > 1.3', 'free memory 3 GB < 6 GB', 'free disk 4 GB < 10 GB']
        newer = ['5-minute load/CPU 3.00 > 1.3', 'free memory 4 GB < 6 GB', 'free disk 5 GB < 10 GB']
        old = dict(nodes=[], machine=dict(held=reasons, sample_reasons=reasons))
        current = dict(nodes=[], machine=dict(held=newer, sample_reasons=newer))
        before = self.probe_output(json.dumps(old), 'reasons-before')
        after = self.probe_output(json.dumps(current), 'reasons-after')
        outcome = self.run_rehearsal(outputs=[dict(ui=before, hook=result()), dict(ui=after, hook=result())])
        self.assertTrue(outcome['passed'], outcome['regressions'])
        patterns = sorted('ui.machine.' + field + '.' + str(i) + '.' + name
                          for field in ('held', 'sample_reasons')
                          for i, name in enumerate(('load5-observation', 'free-memory-observation', 'free-disk-observation')))
        self.assertEqual([dict(command='ui', patterns=patterns)], outcome['normalized_differences'])
        for changed in ([newer[0].replace('> 1.3', '> 9'), *newer[1:]],
                        [newer[0], newer[1].replace('< 6', '< 8'), newer[2]],
                        [newer[0], newer[1], newer[2].replace('GB', 'MB')],
                        ['thermal pressure'], newer[:2], [newer[0].replace('3.00', '<reading>'), *newer[1:]]):
            with self.subTest(changed=changed):
                candidate = dict(old, machine=dict(held=changed, sample_reasons=newer))
                self.assertNotEqual(rehearsal.semantic_ui(old), rehearsal.semantic_ui(candidate))

    def test_plain_copy_and_native_copy_keep_independent_inodes(self):
        source = self.tmp / 'source'
        source.mkdir()
        (source / 'note').write_text('original')
        os.link(source / 'note', source / 'second')
        original = rehearsal.snapshot(source)
        for platform, name in [('linux', 'plain'), (sys.platform, 'native')]:
            dest = self.tmp / name
            with mock.patch.object(rehearsal.sys, 'platform', platform):
                rehearsal.copy_tree(source, dest)
            self.assertNotEqual((source / 'note').stat().st_ino, (dest / 'note').stat().st_ino)
            self.assertNotEqual((dest / 'note').stat().st_ino, (dest / 'second').stat().st_ino)
            (dest / 'note').write_text('private mutation')
            self.assertEqual('original', (dest / 'second').read_text())
            self.assertEqual(original, rehearsal.snapshot(source))
        self.assertFalse(list(self.tmp.glob('.rehearse-copy-*')))

    def test_macos_attempts_copy_on_write_then_falls_back_without_live_writes(self):
        source = self.repo(self.tmp / 'source', {'note': 'original'})
        (source / 'alias').symlink_to(source / 'note')
        original = rehearsal.snapshot(source)
        dest = self.tmp / 'private/copy'
        dest.parent.mkdir()
        def unavailable(args, **kwargs):
            self.assertEqual('/bin/cp', args[0])
            self.assertIn('-c', args)
            partial = Path(args[-1])
            partial.mkdir()
            (partial / 'incomplete').write_text('partial clone')
            return subprocess.CompletedProcess(args, 1, b'', b'unsupported')
        with mock.patch.object(rehearsal.sys, 'platform', 'darwin'), \
                mock.patch.object(rehearsal.subprocess, 'run', side_effect=unavailable) as cp:
            rehearsal.copy_tree(source, dest)
        cp.assert_called_once()
        self.assertFalse((dest / 'incomplete').exists())
        self.assertEqual('original', (dest / 'alias').read_text())
        (dest / 'note').write_text('candidate write')
        self.assertEqual(original, rehearsal.snapshot(source))
        self.assertEqual([dest], list(dest.parent.iterdir()))

    def test_existing_destination_is_never_removed(self):
        source = self.repo(self.tmp / 'source')
        dest = self.tmp / 'existing'
        dest.mkdir()
        (dest / 'keep').write_text('untouched')
        with self.assertRaises(FileExistsError):
            rehearsal.copy_tree(source, dest)
        self.assertEqual('untouched', (dest / 'keep').read_text())

    def test_successful_output_change_still_fails(self):
        flags = rehearsal.compare({'ready:0:0': result('ready')},
                                  {'ready:0:0': result('not ready: changed reason')})
        self.assertIn('ready:0:0: ready became not ready', flags)

    def test_parsed_ui_ready_reason_change_fails(self):
        def ui(reason):
            return result(json.dumps({'nodes': [{'id': 'engine', 'worktrees': [
                {'branch': 'job', 'ready': {'ready': False, 'reasons': [reason], 'packet': None}}]}]}))
        flags = rehearsal.compare({'ui': ui('existing reason')}, {'ui': ui('candidate regression')})
        self.assertIn('ui: readiness changed', flags)

    def test_parsed_ui_key_loss_and_invalid_output_fail(self):
        for new in ('{}', 'not JSON'):
            with self.subTest(new=new):
                flags = rehearsal.compare({'ui': result('{"nodes": []}')}, {'ui': result(new)})
                self.assertTrue(flags)

    def test_private_sidecar_retains_exact_transaction_operands_even_when_lanes_equal(self):
        inst = SimpleNamespace(root=self.tmp / 'source')
        node = dict(home_path=self.tmp / 'candidate')
        directory = self.tmp / 'transaction-local-only'
        evidence = dict(before={'events.jsonl': {'hex': b'old sample bytes'.hex()}},
                        after={'events.jsonl': {'hex': b'new sample bytes'.hex()}},
                        observations=[{'type': 'sample-observation'}], verdict='unproven')
        row = dict(stdout='', stderr='', code=0, timeout=False, transaction_evidence=evidence)
        rehearsal.write_local_diff(inst, node, directory, {'refresh': row}, {'refresh': row})
        path = next(directory.glob('*.diff'))
        records = [json.loads(line) for line in path.read_text().splitlines() if line.startswith('{')][1:]
        self.assertEqual(2, len(records))
        self.assertEqual(['installed', 'candidate'], [r['lane'] for r in records])
        self.assertEqual([evidence, evidence], [r['transaction_evidence'] for r in records])
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_local_diff_retains_newline_only_delta_and_refuses_public_directory(self):
        inst = SimpleNamespace(root=self.tmp / 'source')
        node = dict(home_path=self.tmp / 'candidate')
        directory = self.tmp / 'local-only'
        row = dict(stdout='same\n', stderr='', code=0, timeout=False,
                   writes=[dict(path='brain/.brain/events.jsonl', operation='modified', unexpected=True)],
                   denied_attempts=[dict(event='open', path='/fabricated/private-target')])
        rehearsal.write_local_diff(inst, node, directory, {'check': row},
                                   {'check': dict(row, stdout='same')})
        raw = next(directory.glob('*.diff')).read_text()
        self.assertIn('No newline at end of stdout', raw)
        self.assertEqual(2, raw.count('brain/.brain/events.jsonl'))
        self.assertEqual(2, raw.count('/fabricated/private-target'))
        self.assertEqual(0o600, stat.S_IMODE(next(directory.glob('*.diff')).stat().st_mode))
        directory.chmod(0o755)
        with self.assertRaises(rehearsal.RehearsalError):
            rehearsal.write_local_diff(inst, node, directory, {}, {})


    def test_normalized_differences_are_named_without_publishing_values(self):
        outputs = []
        for side, date, size in [('before', '2035-01-01 12:00', '10'),
                                 ('after_', '2035-01-02 12:00', '20')]:
            trace = {}
            raw = 'warning: /private/' + side + ' ' + date + ' ' + size + ' bytes'
            text = rehearsal.normalize(raw, ['/private/' + side], trace=trace)
            outputs.append({'brief:0': dict(result(text), normalization_tokens=trace), 'hook': result()})
        outcome = self.run_rehearsal(outputs=outputs)
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertEqual([dict(command='brief:0', patterns=['copy-paths', 'timestamps', 'warning-byte-sizes'])],
                         outcome['normalized_differences'])
        self.assertNotIn('/private/', json.dumps(outcome))

    def test_json_encoding_difference_is_named(self):
        outputs = []
        for raw in ('{"b": 2, "a": 1}', '{"a":1,"b":2}'):
            trace = {}
            text = rehearsal.normalize(raw, trace=trace)
            outputs.append({'ui': dict(result(text), normalization_tokens=trace), 'hook': result()})
        outcome = self.run_rehearsal(outputs=outputs)
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertEqual([dict(command='ui', patterns=['json-encoding'])], outcome['normalized_differences'])

    def test_snapshot_records_one_clock_before_lanes(self):
        clock = datetime.datetime(2035, 1, 2, 12, 0, tzinfo=datetime.timezone.utc)
        seen = []
        def inspect(source, *args):
            clock_file = source.parent / 'rehearse-clock'
            self.assertTrue(clock_file.is_file(), 'snapshot clock missing')
            seen.append(clock_file.read_text())
        outcome = self.run_rehearsal(inspect_lane=inspect, now=clock)
        self.assertEqual([clock.isoformat(), clock.isoformat()], seen)
        self.assertEqual(clock.isoformat(), outcome['frozen_now'])

    def test_one_snapshot_survives_live_edits_and_is_cleaned_up(self):
        sources = []
        copies = []
        def inspect(source, core, folder, nodes, commands):
            sources.append(source)
            self.assertNotEqual(self.root, source)
            self.assertEqual('original\n', (source / 'note.md').read_text())
            copies.append(rehearsal.snapshot(source))
        outcome = self.run_rehearsal(lambda: self.write('note.md', 'busy writer'),
                                    during_run=True, inspect_lane=inspect)
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertEqual(2, len(sources))
        self.assertEqual(sources[0], sources[1])
        self.assertEqual(copies[0], copies[1])
        self.assertFalse(sources[0].exists())
        self.assertEqual('busy writer', (self.root / 'note.md').read_text())

    def test_copy_time_live_edits_do_not_change_frozen_input(self):
        seen = []
        def inspect(source, *args):
            seen.append((source / 'note.md').read_text())
        outcome = self.run_rehearsal(lambda: self.write('note.md', 'busy writer'), inspect_lane=inspect)
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertEqual(['original\n', 'original\n'], seen)

    def test_snapshot_write_refuses_and_cleans_up(self):
        sources = []
        def inspect(source, *args):
            sources.append(source)
            (source / 'note.md').write_text('unexpected snapshot mutation')
        outcome = self.run_rehearsal(inspect_lane=inspect)
        self.assertFalse(outcome['passed'])
        self.assertIn('frozen brain changed during rehearsal; evidence refused', outcome['regressions'])
        self.assertEqual('original\n', (self.root / 'note.md').read_text())
        self.assertFalse(sources[0].exists())

    def test_absolute_internal_links_read_only_the_frozen_target(self):
        (self.root / 'brief.md').symlink_to(self.root / 'note.md')
        (self.root / 'aliases').mkdir()
        (self.root / 'aliases/nested').symlink_to(self.root / 'brief.md')
        (self.root / 'notes-alias').symlink_to(self.root / 'aliases', target_is_directory=True)
        reads = []
        def inspect(source, core, folder, *args):
            folder.mkdir()
            lane = folder / 'brain'
            rehearsal.copy_tree(source, lane)
            reads.append((source / 'brief.md').read_text())
            self.assertEqual('original\n', (lane / 'notes-alias/nested').read_text())
            self.assertEqual(lane / 'note.md', (lane / 'brief.md').resolve())
            return {'brief:0': result((lane / 'brief.md').read_text()), 'hook': result()}
        outcome = self.run_rehearsal(lambda: self.write('note.md', 'live edit'),
                                    during_run=True, inspect_lane=inspect)
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertEqual(['original\n', 'original\n'], reads)
        self.assertEqual(str(self.root / 'note.md'), os.readlink(self.root / 'brief.md'))

    def test_link_back_to_live_tree_and_cyclic_links_refuse_before_probes(self):
        # A relative path can leave the copied root and re-enter the live brain.
        for target in (os.path.relpath(self.root / 'note.md', self.root), 'brief.md'):
            link = self.root / 'brief.md'
            if target == 'note.md':
                target = '../live/note.md'
            link.symlink_to(target)
            try:
                with self.assertRaises(rehearsal.RehearsalError):
                    self.run_rehearsal(inspect_lane=lambda *args: self.fail('unsafe link reached a probe'))
            finally:
                link.unlink()

    def test_candidate_changes_link_refused_before_notice_read(self):
        (self.home / 'CHANGES.md').symlink_to(self.root / 'note.md')
        self.git('add', 'CHANGES.md', cwd=self.home)
        self.git('commit', '-qm', 'escaping candidate link', cwd=self.home)
        self.head = self.git('rev-parse', 'HEAD', cwd=self.home).strip()
        with self.assertRaises(rehearsal.RehearsalError):
            self.run_rehearsal(inspect_lane=lambda *args: self.fail('unsafe candidate reached probes'))

    def test_actual_core_replacement_rejects_links_before_commands(self):
        (self.home / 'leak').symlink_to(self.root / 'note.md')
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(rehearsal, 'run_command', side_effect=lambda *a: self.fail('unsafe lane executed')):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.run_lane(self.root, self.home, self.tmp / 'lane', [], [('check', ['check'], 'read')])

    def test_unversioned_core_links_round_trip_through_evidence(self):
        (self.root / 'core/alias').symlink_to(self.root / 'core/bin/brain')
        (self.root / 'core/brain-data').symlink_to(self.root / 'note.md')
        identity = rehearsal.installed_core_commit(self.root)
        outcome = self.run_rehearsal()
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertEqual(identity, outcome['installed_core_commit'])
        node = dict(id='engine', external=True, home_path=self.home)
        path = self.tmp / 'evidence.json'
        path.write_text(json.dumps(outcome))
        with mock.patch.object(rehearsal, 'evidence_path', return_value=path), \
                mock.patch.object(rehearsal, 'node_instance', return_value=SimpleNamespace(root=self.root)):
            read = lambda: rehearsal.read_evidence(node, 'job', outcome['head'], outcome['main'], outcome['integration_base'])
            self.assertEqual(outcome, read())
            (self.root / 'core/bin/brain').write_text('changed installed core')
            self.assertIsNone(read())

    def test_core_submodule_metadata_cannot_advance_after_capture(self):
        self.git('rm', '-qr', 'core', cwd=self.root)
        nested = self.repo(self.tmp / 'versioned-core')
        (nested / 'bin').mkdir()
        (nested / 'bin/brain').write_text('captured core')
        self.git('add', 'bin', cwd=nested)
        self.git('commit', '-qm', 'core executable', cwd=nested)
        self.git('submodule', 'add', '-q', str(nested), 'core', cwd=self.root)
        self.git('commit', '-qam', 'versioned core', cwd=self.root)
        original = self.git('rev-parse', 'HEAD', cwd=self.root / 'core').strip()
        index = Path(self.git('rev-parse', '--path-format=absolute', '--git-path', 'index', cwd=self.root / 'core').strip())
        captured_index = hashlib.sha256(index.read_bytes()).hexdigest()
        def advance():
            (self.root / 'core/bin/brain').write_text('new live core')
            self.git('commit', '-qam', 'upgrade core', cwd=self.root / 'core')
        def inspect(source, core, *args):
            self.assertEqual('captured core', (source / 'core/bin/brain').read_text())
            self.assertEqual(captured_index, hashlib.sha256((source / 'core/.git/index').read_bytes()).hexdigest())
        outcome = self.run_rehearsal(advance, inspect_lane=inspect)
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertEqual(original, outcome['installed_core_commit'])
        self.assertNotEqual(original, rehearsal.installed_core_commit(self.root))

    def test_external_worktree_administration_is_captured_once(self):
        worktree = self.tmp / 'source-worktree'
        self.git('worktree', 'add', '-q', '-b', 'snapshot-fixture', str(worktree), cwd=self.root)
        self.root = worktree
        original = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        def inspect(source, *args):
            self.assertEqual(original, self.git('rev-parse', 'HEAD', cwd=source).strip())
        outcome = self.run_rehearsal(
            lambda: self.git('commit', '--allow-empty', '-qm', 'advance live worktree', cwd=self.root),
            inspect_lane=inspect)
        self.assertTrue(outcome['passed'], outcome['regressions'])

    def test_submodule_and_external_worktree_config_locks_stay_live_only(self):
        self.git('rm', '-qr', 'core', cwd=self.root)
        nested = self.repo(self.tmp / 'lock-core')
        (nested / 'bin').mkdir()
        (nested / 'bin/brain').write_text('fixture')
        self.git('add', 'bin', cwd=nested)
        self.git('commit', '-qm', 'core executable', cwd=nested)
        self.git('submodule', 'add', '-q', str(nested), 'core', cwd=self.root)
        self.git('commit', '-qam', 'versioned core', cwd=self.root)
        worktree = self.tmp / 'locked-worktree'
        self.git('worktree', 'add', '-q', '-b', 'lock-fixture', str(worktree), cwd=self.root)
        self.git('submodule', 'update', '--init', cwd=worktree)
        original_root = self.root
        for root, repository in [(original_root, original_root / 'core'), (worktree, worktree)]:
            self.root = root
            common = Path(self.git('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=repository).strip())
            lock = common / 'config.lock'
            lock.write_text('live writer lock')
            try:
                with self.subTest(layout=root.name):
                    try:
                        outcome = self.run_rehearsal()
                    except rehearsal.RehearsalError:
                        self.fail('copied writer lock refused a valid frozen source')
                    self.assertTrue(outcome['passed'])
                    self.assertEqual('live writer lock', lock.read_text())
            finally:
                lock.unlink()
        self.root = original_root

    def test_identical_baseline_failure_is_named_in_evidence_and_printed(self):
        old = {'brief:0': result('refused: brief limit is exceeded', code=1), 'hook': result()}
        outcome = self.run_rehearsal(outputs=[old, old])
        warning = 'brief:0: identical baseline failure'
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertIn(warning, outcome['warnings'])
        node = dict(id='engine', external=True, home_path=self.home)
        inst = SimpleNamespace(root=self.root, nodes=lambda: [], node=lambda _: node)
        evidence = self.tmp / 'evidence.json'
        printed = io.StringIO()
        with mock.patch.object(rehearsal._m_nodes, 'find_root', return_value=self.root), \
                mock.patch.object(rehearsal._m_instance, 'Instance', return_value=inst), \
                mock.patch.object(rehearsal, 'evidence_path', return_value=evidence), \
                mock.patch.object(rehearsal, 'rehearse', return_value=outcome), \
                contextlib.redirect_stdout(printed):
            rehearsal.cmd_rehearse(SimpleNamespace(id='engine', branch='job'))
        self.assertIn(warning, json.loads(evidence.read_text())['warnings'])
        self.assertIn('warning: ' + warning, printed.getvalue())
        self.assertNotIn('refused: brief limit is exceeded', evidence.read_text() + printed.getvalue())

    def test_baseline_timeouts_and_writes_still_refuse(self):
        for field, value in [('timeout', True), ('unexpected', 1)]:
            with self.subTest(field=field):
                old = {'brief:0': result('failure', code=1), 'hook': result()}
                new = {'brief:0': result('failure', code=1), 'hook': result()}
                old['brief:0'][field] = value
                outcome = self.run_rehearsal(outputs=[old, new])
                self.assertFalse(outcome['passed'])
                self.assertIn('brief:0: baseline incomplete', outcome['regressions'])
                self.assertNotIn('brief:0: identical baseline failure', outcome.get('warnings', []))

    def test_candidate_only_failure_still_refuses(self):
        old = {'brief:0': result('healthy'), 'hook': result()}
        new = {'brief:0': result('failed', code=1), 'hook': result()}
        outcome = self.run_rehearsal(outputs=[old, new])
        self.assertFalse(outcome['passed'])
        self.assertIn('brief:0: new command failure', outcome['regressions'])

    def test_identical_non_json_due_failure_warns_and_different_result_refuses(self):
        old = {'due:0': result('cannot render due', code=1), 'hook': result()}
        outcome = self.run_rehearsal(outputs=[old, old])
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertEqual(['due:0: identical baseline failure'], outcome['warnings'])
        new = {'due:0': result('[]'), 'hook': result()}
        outcome = self.run_rehearsal(outputs=[old, new])
        self.assertFalse(outcome['passed'])
        self.assertIn('due:0: invalid due output', outcome['regressions'])
        self.assertNotIn('due:0: baseline incomplete', outcome['regressions'])

    def test_path_lengths_cannot_reorder_largest_brief_sections(self):
        from brain import briefs
        fixed_size = []
        orders = []
        def inspect(source, core, folder, nodes, commands):
            variable = '## Variable\n' + (str(folder / 'brain') + '\n') * 2 + 'x' * 3000 + '\n'
            if not fixed_size:
                fixed_size.append(len(variable.encode()) - 1)
            fixed = '## Fixed\n' + 'x' * (fixed_size[0] - len('## Fixed\n'))
            printed = io.StringIO()
            with mock.patch.object(briefs._m_guard, 'no_terms_in'), \
                    contextlib.redirect_stderr(printed):
                with self.assertRaises(SystemExit) as caught:
                    # P18 names source inputs, never headings parsed from rendered Markdown.
                    briefs.checked_brief({'fm': {'brief_max_kb': 1}}, variable + fixed, worker=False,
                                         inputs=[('Variable', variable), ('Fixed', fixed)])
            text = printed.getvalue() + str(caught.exception)
            orders.append(text.index('Variable:') < text.index('Fixed:'))
            return {'brief:0': result(rehearsal.normalize(text, [folder / 'brain']), code=1),
                    'hook': result()}
        outcome = self.run_rehearsal(inspect_lane=inspect)
        self.assertEqual([True, True], orders)
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertIn('brief:0: identical baseline failure', outcome['warnings'])


class BaselineDiagnostics(RehearsalFixture):
    def publish(self, outcome):
        node = dict(id='engine', external=True, home_path=self.home)
        inst = SimpleNamespace(root=self.root, nodes=lambda: [], node=lambda _: node)
        evidence = self.tmp / 'evidence.json'
        printed = io.StringIO()
        with mock.patch.object(rehearsal._m_nodes, 'find_root', return_value=self.root), \
                mock.patch.object(rehearsal._m_instance, 'Instance', return_value=inst), \
                mock.patch.object(rehearsal, 'evidence_path', return_value=evidence), \
                mock.patch.object(rehearsal, 'rehearse', return_value=outcome), \
                contextlib.redirect_stdout(printed):
            if outcome['passed']:
                rehearsal.cmd_rehearse(SimpleNamespace(id='engine', branch='job'))
            else:
                with self.assertRaises(SystemExit) as caught:
                    rehearsal.cmd_rehearse(SimpleNamespace(id='engine', branch='job'))
                self.assertEqual(1, caught.exception.code)
        self.assertEqual(outcome, json.loads(evidence.read_text()))
        for diagnostic in outcome['diagnostics']:
            self.assertIn('diagnostic: ' + json.dumps(diagnostic, ensure_ascii=False), printed.getvalue())
        return evidence.read_text() + printed.getvalue()

    def test_completed_failed_baselines_allow_advisory_changes(self):
        for key in ('check', 'fix', 'ready:0:0'):
            with self.subTest(key=key):
                old = {key: result('✗ unchanged error\nold advisory', code=1), 'hook': result()}
                new = {key: result('✗ unchanged error\nnew advisory', code=1), 'hook': result()}
                outcome = self.run_rehearsal(outputs=[old, new])
                self.assertTrue(outcome['passed'], outcome['regressions'])
                self.assertEqual([], outcome['warnings'])
                self.assertEqual([], outcome['diagnostics'])

    def test_changed_exit_code_still_refuses_without_incomplete_baseline(self):
        outcome = self.run_rehearsal(outputs=[
            {'fix': result('✗ unchanged error', code=1), 'hook': result()},
            {'fix': result('✗ unchanged error', code=2), 'hook': result()}])
        self.assertFalse(outcome['passed'])
        self.assertEqual(['fix: new command failure'], outcome['regressions'])
        self.assertEqual([], outcome['diagnostics'])

    def test_incomplete_baseline_records_conditions_and_exit_codes(self):
        for conditions in (['timeout'], ['unexpected writes'], ['timeout', 'unexpected writes']):
            with self.subTest(conditions=conditions):
                old = result('private baseline output', code=7)
                old['timeout'] = 'timeout' in conditions
                old['unexpected'] = 2 if 'unexpected writes' in conditions else 0
                outcome = self.run_rehearsal(outputs=[{'fix': old, 'hook': result()},
                    {'fix': result('private candidate output', code=3), 'hook': result()}])
                self.assertFalse(outcome['passed'])
                expected = dict(command='fix', reason='baseline incomplete', conditions=conditions,
                                exit_codes=dict(baseline=7, candidate=3))
                self.assertIn(expected, outcome['diagnostics'])
                saved_and_printed = self.publish(outcome)
                self.assertIn(json.dumps(expected, ensure_ascii=False), saved_and_printed)
                self.assertNotIn('private baseline output', saved_and_printed)
                self.assertNotIn('private candidate output', saved_and_printed)

    def test_added_errors_are_redacted_in_evidence_and_output(self):
        # Real run_command normalization/redaction, then real evidence generation/publication.
        terms = [('invented-canary', 'term'), ('STRASSE', 'term')]
        for key in ('check', 'fix'):
            with self.subTest(key=key):
                lane = self.tmp / key
                (lane / 'brain').mkdir(parents=True)
                raw = ('✗ unchanged error\n'
                       '✗ engine: invented-canary Straße failed /fabricated/private/report.md\n'
                       '✗ engine: added error at ' + str(lane / 'brain/nodes/engine/status.md') + '\n'
                       '✗ engine: added error at ' + str(lane / 'brain/nodes/engine/status.md') + '\n'
                       'advisory must stay private')
                process = mock.Mock(returncode=1, pid=123)
                process.communicate.return_value = (raw, '')
                with mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                        mock.patch.object(rehearsal.subprocess, 'Popen', return_value=process), \
                        mock.patch.object(rehearsal.os, 'killpg'):
                    new = rehearsal.run_command(lane / 'brain/core', lane, [], 'read', [lane / 'brain'], terms)
                outcome = self.run_rehearsal(outputs=[
                    {key: result('✗ unchanged error', code=1), 'hook': result()},
                    {key: new, 'hook': result()}])
                self.assertIn(key + ': new check errors', outcome['regressions'])
                expected_lines = ['✗ […]: […] […] failed <path>',
                                  '✗ […]: added error at <path>', '✗ […]: added error at <path>']
                self.assertIn(dict(command=key, reason='new check errors', added_errors=expected_lines),
                              outcome['diagnostics'])
                saved_and_printed = self.publish(outcome)
                for line in expected_lines:
                    self.assertIn(line, saved_and_printed)
                for private in ('engine:', 'invented-canary', 'Straße', '/fabricated', str(lane),
                                'nodes/engine', 'status.md', 'advisory must stay private', '✗ unchanged error'):
                    self.assertNotIn(private.casefold(), saved_and_printed.casefold())

    def test_identical_failed_check_and_fix_keep_named_warnings(self):
        old = dict(check=result('✗ existing', code=1), fix=result('✗ existing', code=1), hook=result())
        outcome = self.run_rehearsal(outputs=[old, old])
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertEqual(['check: identical baseline failure', 'fix: identical baseline failure'], outcome['warnings'])
        self.assertEqual([], outcome['diagnostics'])
        printed = self.publish(outcome)
        for warning in outcome['warnings']:
            self.assertIn('warning: ' + warning, printed)

    def test_unquoted_spaced_path_and_never_name_are_private_at_publication(self):
        for key in ('check', 'fix'):
            for protect_prefix in (False, True):
                with self.subTest(key=key, protect_prefix=protect_prefix):
                    lane = self.tmp / (key + str(protect_prefix))
                    (lane / 'brain').mkdir(parents=True)
                    raw = ('✗ engine: invented-canary home /fabricated/private-client '
                           'alpha-private report not found on this machine')
                    terms = [('invented-canary', 'term')]
                    if protect_prefix:
                        # Redaction must not erase the path marker before we
                        # recognize its ambiguous, whitespace-containing span.
                        terms.append(('/fabricated/private-client', 'path term'))
                    process = mock.Mock(returncode=1, pid=123)
                    process.communicate.return_value = (raw, '')
                    with mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                            mock.patch.object(rehearsal.subprocess, 'Popen', return_value=process), \
                            mock.patch.object(rehearsal.os, 'killpg'):
                        new = rehearsal.run_command(lane / 'brain/core', lane, [], 'read', [], terms)
                    outcome = self.run_rehearsal(outputs=[
                        {key: result('healthy'), 'hook': result()}, {key: new, 'hook': result()}])
                    self.assertFalse(outcome['passed'])
                    self.assertIn(key + ': new check errors', outcome['regressions'])
                    saved_and_printed = self.publish(outcome)
                    for private in ('invented-canary', '/fabricated', 'private-client',
                                    'alpha-private', 'report not found', 'engine:'):
                        self.assertNotIn(private, saved_and_printed)
                    self.assertIn(dict(command=key, reason='new check errors',
                        added_errors=['✗ <redacted path diagnostic>']), outcome['diagnostics'])


class StableComparisons(unittest.TestCase):
    def test_diagnostic_paths_and_short_private_ids_preserve_error_meaning(self):
        for path in ('"/private/path with spaces/report.md"', "'~/private/report.md'",
                     'nodes/private/status.md', '<copy>/nodes/private/status.md',
                     r'C:\private\report.md', 'private.md'):
            with self.subTest(path=path):
                self.assertEqual('✗ […]: cannot heal <path>',
                    rehearsal.diagnostic_line('✗ a: cannot heal ' + path, [], ['a']))
        self.assertEqual('✗ […]: unknown principal […]',
            rehearsal.diagnostic_line("✗ a: unknown principal 'fabricated-owner'", [], ['a']))

    def test_due_text_changes_preserve_identity_and_duplicates_still_flag(self):
        item = dict(node='sample', kind='hold', id='one', text='old description')
        changed = {**item, 'text': 'rewritten description'}
        before = {'due:0': result(json.dumps([item]))}
        after = {'due:0': result(json.dumps([changed]))}
        self.assertEqual([], rehearsal.compare(before, after))
        after['due:0']['text'] = json.dumps([changed, changed])
        self.assertIn('due:0: new due items', rehearsal.compare(before, after))
        for field, value in [('node', 'other'), ('kind', 'other'), ('id', 'two')]:
            with self.subTest(field=field):
                after['due:0']['text'] = json.dumps([{**changed, field: value}])
                self.assertIn('due:0: new due items', rehearsal.compare(before, after))

    def test_brief_sizes_normalize_but_removed_warning_still_flags(self):
        def warning(size, section):
            return ('warning: brief is ' + size + ' bytes (brief_max_kb=8); '
                    'largest sections: Task: ' + section + ' bytes. Retain limits!')
        before = {'brief:0': result(rehearsal.normalize(warning('20582', '8816')))}
        after = {'brief:0': result(rehearsal.normalize(warning('20575', '8809')))}
        self.assertEqual([], rehearsal.compare(before, after))
        after['brief:0']['text'] = 'quiet'
        self.assertIn('brief:0: brief warning disappeared', rehearsal.compare(before, after))
        after['brief:0']['text'] = before['brief:0']['text'].replace('Retain limits!', 'Ignore consent!')
        self.assertIn('brief:0: brief warning disappeared', rehearsal.compare(before, after))

    def test_warning_normalization_preserves_everything_except_size_numbers(self):
        original = 'warning: payload is 10 bytes; retain consent!'
        before = {'brief:0': result(rehearsal.normalize(original))}
        for changed in ('warning: payload is 20  bytes; retain consent!',
                        'warning: payload is 20\tbytes; retain consent!',
                        'warning: payload is 20 byte; retain consent!',
                        'warning: payload is 20 KiB; retain consent!',
                        'warning: payload is 20 bytes; revoke consent!'):
            with self.subTest(changed=changed):
                after = {'brief:0': result(rehearsal.normalize(changed))}
                self.assertIn('brief:0: brief warning disappeared', rehearsal.compare(before, after))
        self.assertEqual(rehearsal.normalize(original),
                         rehearsal.normalize(original.replace('10 bytes', '25 bytes')))

    def test_size_normalization_preserves_internal_line_separators(self):
        for separator in ('\r\n', '\r', '\v', '\u2028'):
            with self.subTest(separator=repr(separator)):
                text = 'warning: 10 bytes' + separator + 'retain consent!'
                self.assertEqual(text.replace('10 bytes', '<size> bytes'), rehearsal.normalize(text))

    def test_non_warning_byte_counts_are_not_normalized(self):
        self.assertNotEqual(rehearsal.normalize('payload: 10 bytes'), rehearsal.normalize('payload: 20 bytes'))

    def test_expected_notice_cannot_exempt_duplicate_of_existing_identity(self):
        notice = dict(node='root', kind='core-changes', text='candidate notice')
        before = {'due:0': result(json.dumps([{**notice, 'text': 'existing notice'}]))}
        after = {'due:0': result(json.dumps([notice, notice]))}
        self.assertIn('due:0: new due items',
                      rehearsal.compare(before, after, {'due:0': notice}))


class CandidateNotices(RehearsalFixture):
    def test_only_candidate_changes_notice_is_expected(self):
        # Real CHANGES parsing and notice attribution; fabricated subprocess results.
        old_changes = '## 2026-01-01: Existing entry\n\nOriginal body\n'
        new_changes = '## 2026-01-02: New entry\n\nNew body\n' + old_changes
        (self.root / 'core/CHANGES.md').write_text(old_changes)
        (self.home / 'CHANGES.md').write_text(new_changes)
        self.git('add', 'CHANGES.md', cwd=self.home)
        self.git('commit', '-qm', 'new notice', cwd=self.home)
        self.head = self.git('rev-parse', 'HEAD', cwd=self.home).strip()
        notice = dict(node='root', kind='core-changes', text=(
            'core changed: 1 unread entry (2026-01-02: New entry); '
            'read them: brain changes root, then brain changes root --seen'))
        fixture_node = dict(id='root', local={}, dir=self.root, fm={})
        inst = SimpleNamespace(root=self.root, local={'changes_seen': {'root': ['2026-01-01: Existing entry']}},
                               nodes=lambda: [fixture_node],
                               now_dt=lambda: datetime.datetime(2035, 1, 3, tzinfo=datetime.timezone.utc))
        node = dict(id='engine', external=True, home_path=self.home)
        def run(items):
            with mock.patch.dict(os.environ, self.env), \
                    mock.patch.object(rehearsal, 'sandbox_self_check'), \
                    mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                    mock.patch.object(rehearsal._m_verify, 'ready_refs', return_value=(self.head, self.head)), \
                    mock.patch.object(rehearsal._m_verify, 'integration_parent', return_value=self.head), \
                    mock.patch.object(rehearsal._m_instance, 'Instance', return_value=inst), \
                    mock.patch.object(rehearsal, 'isolated_probe_instance', return_value=inst), \
                    mock.patch.object(rehearsal._m_jobs, 'read_jobs', return_value=[]), \
                    mock.patch.object(rehearsal._m_jobs, 'stamp', return_value='fixture'), \
                    mock.patch.object(rehearsal._m_guard, 'never_terms', return_value=[]), \
                    mock.patch.object(rehearsal, 'probe_delivery', return_value=[]), \
                    mock.patch.object(rehearsal, 'run_lane', side_effect=[
                        {'due:0': result('[]'), 'hook': result()},
                        {'due:0': result(json.dumps(items)), 'hook': result()}]):
                return rehearsal.rehearse(inst, node, 'job')
        self.assertTrue(run([notice])['passed'])
        for items in [[notice, dict(node='root', kind='hold', id='new', text='extra due')],
                      [{**notice, 'text': 'unexplained new core notice'}],
                      [notice, notice]]:
            with self.subTest(items=items):
                self.assertIn('due:0: new due items', run(items)['regressions'])
        (self.home / 'CHANGES.md').write_text(old_changes)
        self.git('commit', '-qam', 'no candidate notice', cwd=self.home)
        self.head = self.git('rev-parse', 'HEAD', cwd=self.home).strip()
        self.assertIn('due:0: new due items', run([notice])['regressions'])


@unittest.skipUnless(sys.platform == 'darwin', 'requires macOS OS sandbox')
class FrozenClock(Scenario):
    def prepare_private_fixture(self):
        tiny_rehearsal_fixture(self)
        worktree = self.tmp / 'diagnostic-candidate-work/worktrees/job'
        worktree.parent.mkdir(parents=True)
        self.git('worktree', 'add', '-q', '-b', 'job', str(worktree), cwd=self.home)

    def publish_private_diff(self, passed=True):
        printed = io.StringIO()
        with tiny_bindings(self), \
                mock.patch.object(rehearsal._m_nodes, 'find_root', return_value=self.root), \
                mock.patch.object(rehearsal, 'evidence_path', return_value=self.evidence), \
                contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            if passed:
                rehearsal.cmd_rehearse(SimpleNamespace(id='engine', branch='job'))
            else:
                with self.assertRaises(SystemExit) as caught:
                    rehearsal.cmd_rehearse(SimpleNamespace(id='engine', branch='job'))
                self.assertEqual(1, caught.exception.code)
        return printed.getvalue()

    def test_raw_diff_is_private_external_and_never_published(self):
        self.prepare_private_fixture()
        worktree = self.tmp / 'diagnostic-candidate-work/worktrees/job'
        (worktree / 'bin/brain').write_text(self.fixture.replace('MODE', 'candidate').replace('PREFIX', '✗ '))
        self.git('commit', '-qam', 'candidate regression', cwd=worktree)
        self.head = self.git('rev-parse', 'HEAD', cwd=worktree).strip()
        original = rehearsal.snapshot(self.root)
        printed = self.publish_private_diff(passed=False)
        evidence = self.evidence.read_text()
        files = list((self.evidence.parent / 'local-only').glob('*.diff'))
        self.assertEqual(1, len(files))
        raw = files[0].read_text()
        self.assertIn('-invented-canary: check baseline', raw)
        self.assertIn('+✗ invented-canary: check candidate', raw)
        self.assertNotIn('invented-canary', printed + evidence)
        self.assertNotIn(str(files[0]), printed + evidence)
        self.assertEqual(0o600, stat.S_IMODE(files[0].stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(files[0].parent.stat().st_mode))
        self.assertEqual(original, rehearsal.snapshot(self.root))
        self.assertFalse(json.loads(evidence)['passed'])

    def test_default_lanes_are_private_and_live_commit_does_not_change_result(self):
        self.prepare_private_fixture()
        original = rehearsal.snapshot(self.root)
        lane = rehearsal.run_lane
        seen = []
        def inspect(source, core, folder, nodes, commands, **kwargs):
            seen.append((source, folder))
            self.assertNotEqual(self.root, source)
            self.assertEqual(source.parent, folder.parent)
            if len(seen) == 1:
                self.assertEqual(original, rehearsal.snapshot(self.root))
                (self.root / 'live-only').write_text('concurrent writer')
                self.git('add', 'live-only', cwd=self.root)
                self.git('commit', '-qm', 'live-only drift', cwd=self.root)
            return lane(source, core, folder, nodes, commands, **kwargs)
        with mock.patch.object(rehearsal, 'run_lane', side_effect=inspect):
            printed = self.publish_private_diff()
        self.assertIn('rehearse: passed', printed)
        self.assertEqual(2, len(seen))
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertTrue(all(not path.exists() for pair in seen for path in pair))
        self.assertEqual('concurrent writer', (self.root / 'live-only').read_text())

    def test_linked_or_repository_local_diff_directory_refuses(self):
        self.prepare_private_fixture()
        # A sidecar must never follow a link or live within any repository.
        core_fixture = self.tmp / 'running-core'
        (core_fixture / 'lib/brain').mkdir(parents=True)
        (core_fixture / 'lib/brain/rehearse.py').write_text('tiny running core')
        for target in (self.root, self.home, core_fixture):
            with self.subTest(target=target):
                outside = rehearsal.snapshot(target)
                with self.assertRaises(rehearsal.RehearsalError):
                    with mock.patch.object(rehearsal, '__file__', str(core_fixture / 'lib/brain/rehearse.py')):
                        rehearsal.write_local_diff(self.inst, self.node, target / 'local-only', {}, {})
                self.assertEqual(outside, rehearsal.snapshot(target))
        self.evidence.parent.mkdir(parents=True)
        (self.evidence.parent / 'local-only').symlink_to(self.root, target_is_directory=True)
        outside = rehearsal.snapshot(self.root)
        with self.assertRaises(rehearsal.RehearsalError):
            rehearsal.write_local_diff(self.inst, self.node, self.evidence.parent / 'local-only', {}, {})
        self.assertEqual(outside, rehearsal.snapshot(self.root))


    def setUp(self):
        super().setUp()
        tiny_environment(self)
        if self._testMethodName in TINY_PRIVATE_TESTS:
            return
        self.source = self.repo(self.tmp / 'source', {'instance.md': '---\ntimezone: UTC\n---\n',
            'status.md': '## Blocked\n- sample hold; since: 2035-01-01 00:00 +00:00; recommendation: ask; default: wait\n'})
        self.old = self.source / 'core'
        self.new = self.tmp / 'candidate'
        for index, core in enumerate((self.old, self.new)):
            if self._testMethodName in TINY_CLOCK_TESTS:
                (core / 'lib/brain').mkdir(parents=True)
                (core / 'lib/brain/__init__.py').write_text('')
                (core / 'lib/brain/instance.py').write_text(TINY_INSTANCE)
                for module in ('ui', 'jobs', 'usage'):
                    (core / ('lib/brain/' + module + '.py')).write_text('import time\n')
            else:
                shutil.copytree(CORE / 'lib', core / 'lib', ignore=shutil.ignore_patterns('__pycache__'))
            (core / 'bin').mkdir()
            # Model two real wall-clock readings ten minutes apart, without sleeping.
            path = core / 'lib/brain/instance.py'
            text = path.read_text().replace('datetime.datetime.now(z)',
                'datetime.datetime(2035, 1, 3, 0, ' + str(index * 10 + 1) + ', tzinfo=z)')
            path.write_text(text)
            (core / 'bin/brain').write_text('''import json, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from brain.instance import Instance
from brain import status
inst = Instance(Path.cwd())
n = dict(id='sample', dir=Path.cwd(), archived=False)
rows = status.blocked_entries(inst, n)
if sys.argv[1] == 'age':
    child = subprocess.check_output([sys.executable, '-B', '-c',
        'import sys; from pathlib import Path; sys.path.insert(0, str(Path.cwd() / "core/lib")); from brain.instance import Instance; print(Instance(Path.cwd()).now_dt().timestamp())'], text=True).strip()
    print(json.dumps(dict(age=rows[0]['age_hours'], escalated=rows[0]['escalated'], child_epoch=float(child))))
else:
    print(json.dumps([dict(node='sample', kind='blocked-overdue', id='hold',
        age_hours=row['age_hours'], escalated=row['escalated']) for row in rows if row['escalated']]))
''')
        self.clock = datetime.datetime(2035, 1, 2, 12, tzinfo=datetime.timezone.utc)
        (self.source.parent / 'rehearse-clock').write_text(self.clock.isoformat())

    def pair(self, command):
        with mock.patch.dict(os.environ, self.env):
            return [rehearsal.run_lane(self.source, core, self.tmp / name, [],
                       [('due:0', [command], 'read')])['due:0']
                    for core, name in ((self.old, 'before'), (self.new, 'after_'))]

    def test_probes_minutes_apart_have_identical_age_fields_and_helper_clock(self):
        before, after = self.pair('age')
        for row in (before, after):
            self.assertEqual(0, row['code'], row['text'])
            self.assertFalse(row['timeout'])
            self.assertEqual(0, row['unexpected'])
        self.assertEqual(before['text'], after['text'])
        fields = json.loads(before['text'])
        self.assertEqual(36.0, fields['age'])
        self.assertFalse(fields['escalated'])
        # Numeric epoch survives path/time normalization, proving helper pinning too.
        self.assertEqual(self.clock.timestamp(), fields['child_epoch'])

    def test_git_relative_cutoffs_in_parent_and_helper_use_the_snapshot_clock(self):
        script = '''import json, subprocess, sys
cutoff = subprocess.check_output(['git', 'rev-parse', '--since=24 hours ago'], text=True, stderr=subprocess.PIPE).strip()
child = subprocess.check_output([sys.executable, '-B', '-c',
    "import subprocess; print(subprocess.check_output(['git', 'rev-parse', '--since=24 hours ago'], text=True, stderr=subprocess.PIPE).strip())"], text=True).strip()
print(json.dumps(dict(cutoff=cutoff, child=child)))
'''
        for core in (self.old, self.new):
            (core / 'bin/brain').write_text(script)
        before, after = self.pair('cutoff')
        expected = '--max-age=' + str(int(self.clock.timestamp()) - 86400)
        for row in (before, after):
            self.assertEqual(0, row['code'], row['text'])
            self.assertEqual(dict(cutoff=expected, child=expected), json.loads(row['text']))

    def test_data_wall_clocks_are_frozen_but_lock_and_elapsed_clocks_stay_real(self):
        script = '''import json, time
from brain import instance, ui, jobs, usage
print(json.dumps(dict(ui=ui.time.time(), jobs=jobs.time.time(), usage=usage.time.time(),
    lock=instance.time.time(), elapsed=ui.time.monotonic(), monotonic=time.monotonic())))
'''
        for core in (self.old, self.new):
            (core / 'bin/brain').write_text(script)
        before, after = self.pair('clocks')
        for row in (before, after):
            self.assertEqual(0, row['code'], row['text'])
            fields = json.loads(row['text'])
            for key in ('ui', 'jobs', 'usage'):
                self.assertEqual(self.clock.timestamp(), fields[key])
            self.assertNotEqual(self.clock.timestamp(), fields['lock'])
            self.assertGreaterEqual(fields['monotonic'], fields['elapsed'])
            self.assertLess(fields['monotonic'] - fields['elapsed'], 1)

    def test_unsupported_git_clock_refuses_before_probing(self):
        run = rehearsal.subprocess.run
        def unsupported(args, **kwargs):
            if '--since=24 hours ago' in args:
                return rehearsal.subprocess.CompletedProcess(args, 0, '--max-age=1\n', '')
            return run(args, **kwargs)
        with mock.patch.object(rehearsal.subprocess, 'run', side_effect=unsupported), \
                mock.patch.object(rehearsal.subprocess, 'Popen', wraps=rehearsal.subprocess.Popen) as popen:
            with self.assertRaisesRegex(rehearsal.RehearsalError, 'Git frozen clock binding failed'):
                self.pair('age')
        self.assertFalse(any('audit.py' in str(call) for call in popen.call_args_list))

    def test_changed_escalation_threshold_still_flags_a_real_regression(self):
        path = self.new / 'lib/brain/status.py'
        path.write_text(path.read_text().replace('BLOCKED_HOURS = 48', 'BLOCKED_HOURS = 24'))
        before, after = self.pair('due')
        self.assertEqual(0, before['code'], before['text'])
        self.assertEqual(0, after['code'], after['text'])
        self.assertEqual([], json.loads(before['text']))
        self.assertTrue(json.loads(after['text'])[0]['escalated'])
        self.assertEqual(36.0, json.loads(after['text'])[0]['age_hours'])
        self.assertIn('due:0: new due items', rehearsal.compare({'due:0': before}, {'due:0': after}))

    def import_write_attempt(self, helper_only):
        outside = self.tmp / 'outside-note'
        outside.write_text('unchanged')
        for core in (self.old, self.new):
            path = core / 'lib/brain/instance.py'
            path.write_text(path.read_text() + '\nimport sys\nif ' +
                ('sys.argv[0] == "-c"' if helper_only else 'True') + ':\n' +
                '    try:\n        open(' + repr(str(outside)) + ', "w").write("attempt")\n' +
                '    except OSError:\n        pass\n')
        before, after = self.pair('age')
        self.assertEqual('unchanged', outside.read_text())
        for row in (before, after):
            self.assertEqual(0, row['code'], row['text'])
            self.assertGreater(row['unexpected'], 0)
        self.assertIn('due:0: unexpected writes', rehearsal.compare({'due:0': before}, {'due:0': after}))

    def test_clock_import_caught_write_is_audited(self):
        self.import_write_attempt(False)

    def test_helper_clock_import_caught_write_is_audited(self):
        self.import_write_attempt(True)

    def helper_binding_failure(self, ignored=False):
        for core in (self.old, self.new):
            path = core / 'lib/brain/instance.py'
            path.write_text(path.read_text() + '\nimport sys\nif sys.argv[0] == "-c":\n    Instance = None\n')
            if ignored:
                (core / 'bin/brain').write_text('import subprocess, sys\nsubprocess.run([sys.executable, "-B", "-c", "pass"])\nprint("healthy")\n')
        with self.assertRaisesRegex(rehearsal.RehearsalError, 'clock binding failed'):
            self.pair('age')

    def test_helper_binding_failure_cannot_be_shared_baseline_failure(self):
        self.helper_binding_failure()

    def test_ignored_helper_binding_failure_still_refuses(self):
        self.helper_binding_failure(ignored=True)

    def test_clock_binding_failure_refuses_instead_of_becoming_shared_baseline_failure(self):
        for core in (self.old, self.new):
            (core / 'lib/brain/instance.py').write_text('Instance = None\n')
        with self.assertRaisesRegex(rehearsal.RehearsalError, 'probe frozen clock binding failed'):
            self.pair('age')

    def test_invalid_snapshot_clock_refuses_before_probes(self):
        (self.source.parent / 'rehearse-clock').write_text('2035-01-02T12:00:00')
        with self.assertRaises(rehearsal.RehearsalError), \
                mock.patch.object(rehearsal, 'run_command', side_effect=AssertionError('must not probe')):
            self.pair('age')
