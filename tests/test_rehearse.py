"""Fabricated rehearsal comparisons and real sandbox mutation checks."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from brain import rehearse as rehearsal
from scenario import Scenario, CORE


def result(text='', code=0, seconds=0.01, unexpected=0):
    return dict(text=text, code=code, seconds=seconds, unexpected=unexpected, timeout=False)


class RehearsalComparison(unittest.TestCase):
    def test_clean_candidate_and_normalization(self):
        self.assertEqual([], rehearsal.compare({'check': result('✓ all good')}, {'check': result('✓ all good')}))
        self.assertEqual(rehearsal.normalize('{"b":2,"a":1}'), rehearsal.normalize('{"a":1,"b":2}'))
        self.assertEqual(rehearsal.normalize('/tmp/one 2026-01-02 03:04', ['/tmp/one']),
                         rehearsal.normalize('/tmp/two 2026-02-03 04:05', ['/tmp/two']))

    def test_all_four_incident_classes_and_hook(self):
        before = {'check': result('✓ good'), 'fix': result('✓ good'),
                  'ready:0:0': result('ready'), 'ready:1:0': result('ready'),
                  'due:0': result('[]'), 'due:1': result('[]'),
                  'brief:0': result('Stop when: ask for consent!'), 'hook': result()}
        after = {'check': result('✗ cannot heal', 1), 'fix': result('✗ cannot heal', 1),
                 'ready:0:0': result('not ready: main resolution', 1),
                 'ready:1:0': result('not ready: main resolution', 1),
                 'due:0': result('[{"kind":"load","text":"noise"}]'),
                 'due:1': result('[{"kind":"load","text":"noise"}]'),
                 'brief:0': result('quiet'), 'hook': result(seconds=1.1)}
        flags = rehearsal.compare(before, after)
        for flag in ('fix: new check errors', 'ready:0:0: ready became not ready',
                     'ready:1:0: ready became not ready', 'due:0: new due items',
                     'due:1: new due items', 'brief:0: brief warning disappeared', 'hook: latency exceeds 1 s'):
            self.assertIn(flag, flags)

    def test_additional_duplicate_errors_and_due_items_are_not_lost(self):
        for key, text in [('check', '✗ same'), ('due:0', '[{"kind":"a"}]')]:
            new = text + '\n' + text if key == 'check' else '[{"kind":"a"},{"kind":"a"}]'
            self.assertTrue(rehearsal.compare({key: result(text)}, {key: result(new)}))

    def test_unexpected_write_and_invalid_due_fail(self):
        self.assertIn('ui: unexpected writes', rehearsal.compare({'ui': result()}, {'ui': result(unexpected=1)}))
        self.assertIn('due:0: invalid due output', rehearsal.compare({'due:0': result('[]')}, {'due:0': result('oops')}))

    def test_sandbox_unavailable_refuses(self):
        with mock.patch.object(rehearsal.shutil, 'which', return_value=None):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.sandbox_prefix(Path('/tmp/fabricated'))


@unittest.skipUnless(sys.platform == 'darwin', 'requires macOS OS sandbox')
class RehearsalSandbox(Scenario):
    def runner(self, script, args=(), terms=()):
        lane = self.tmp / 'lane'
        lane.mkdir(exist_ok=True)
        (lane / 'brain').mkdir(exist_ok=True)
        core = lane / 'brain/core'
        (core / 'bin').mkdir(parents=True, exist_ok=True)
        (core / 'bin/brain').write_text(script)
        return rehearsal.run_command(core, lane, list(args), 'read', [], terms)

    def test_os_boundary_preserves_live_brain_home_and_git(self):
        live = self.repo(self.tmp / 'live', {'note.md': 'original\n'})
        outside_home = self.tmp / 'operator'
        outside_home.mkdir()
        note = outside_home / 'note'
        note.write_text('original home')
        original = rehearsal.snapshot(live)
        script = 'from pathlib import Path\nPath(' + repr(str(live / 'note.md')) + ').write_text("mutated")\n'
        r = self.runner(script)
        self.assertNotEqual(0, r['code'])
        self.assertEqual(original, rehearsal.snapshot(live))
        r = self.runner('from pathlib import Path\nPath(' + repr(str(note)) + ').write_text("mutated")\n')
        self.assertNotEqual(0, r['code'])
        self.assertEqual('original home', note.read_text())
        r = self.runner('import socket\nsocket.create_connection(("127.0.0.1", 9))\n')
        self.assertNotEqual(0, r['code'])

    def test_caught_external_write_still_fails_and_empty_directory_is_seen(self):
        outside = self.tmp / 'outside'
        outside.write_text('unchanged')
        script = ('from pathlib import Path\ntry:\n    Path(' + repr(str(outside)) +
                  ').write_text("changed")\nexcept OSError:\n    pass\n')
        r = self.runner(script)
        self.assertEqual(0, r['code'])
        self.assertEqual(1, r['unexpected'])
        self.assertEqual('unchanged', outside.read_text())
        r = self.runner('from pathlib import Path\nPath("unapproved").mkdir()\n')
        self.assertEqual(1, r['unexpected'])

    def test_unexpected_copy_mutation_is_detected(self):
        r = self.runner('from pathlib import Path\nPath("node.md").write_text("changed")\n')
        self.assertEqual(0, r['code'])
        self.assertEqual(1, r['unexpected'])

    def test_never_name_output_fails_without_publishing_it(self):
        with self.assertRaisesRegex(rehearsal.RehearsalError, '^protected output refused$'):
            self.runner('print("invented-canary")', terms=[('invented-canary', 'term')])


class RehearsalEvidence(Scenario):
    def test_required_ready_gate_and_head_main_invalidation(self):
        r = self.init('sample')
        home = self.repo(self.tmp / 'app')
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'set', 'a', 'verify=true', 'rehearse=required')
        self.b(r, 'wt', 'add', 'a', 'job')
        wt = self.tmp / 'app-work/worktrees/job'
        self.git('commit', '--allow-empty', '-qm', 'change', cwd=wt)
        self.assertIn('passing rehearsal missing', self.b(r, 'ready', 'a', '--branch', 'job', '--record', '--reviews', '1', ok=False))
        head = self.git('rev-parse', 'job', cwd=home).strip()
        main = self.git('rev-parse', 'main', cwd=home).strip()
        import hashlib
        path = self.tmp / 'app-work/scratch/rehearse' / (hashlib.sha256(b'job').hexdigest() + '.json')
        path.parent.mkdir(parents=True)
        evidence = dict(version=1, node='a', branch='job', head=head, main=main,
                        integration_base=main, passed=True, commands=7)
        path.write_text(json.dumps(evidence))
        self.assertIn('ready', self.b(r, 'ready', 'a', '--branch', 'job', '--record', '--reviews', '1'))
        packet = json.loads(next((r / 'nodes/a/job-handbacks').glob('*.json')).read_text())['ready']
        self.assertEqual(evidence, packet['rehearsal'])
        self.assertEqual('ready', self.b(r, 'ready', 'a', '--branch', 'job').strip())
        self.git('commit', '--allow-empty', '-qm', 'new head', cwd=wt)
        self.assertIn('passing rehearsal missing', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        self.git('commit', '--allow-empty', '-qm', 'new main', cwd=home)
        self.assertIn('passing rehearsal missing', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))

@unittest.skipUnless(sys.platform == 'darwin', 'requires macOS OS sandbox')
class RehearsalPipeline(Scenario):
    def test_committed_candidate_clean_pass_and_all_incident_mutations(self):
        """Actual subprocess orchestration with fabricated installed/candidate CLIs."""
        from brain.instance import Instance
        r = self.init('sample')
        home = self.tmp / 'candidate'
        (home / 'bin').mkdir(parents=True)
        fixture = '''import sys
command = sys.argv[1]
mode = MODE
if command == 'due':
    print('[{"kind":"load","text":"new noise"}]' if mode else '[]')
elif command in ('check',):
    print('✗ upgrade cannot heal' if mode else '✓ checks pass')
    sys.exit(1 if mode else 0)
elif command == 'ready':
    print('not ready: changed main resolution' if mode else 'ready')
    sys.exit(1 if mode else 0)
elif command == 'brief':
    print('quiet' if mode else 'Stop when: ask for consent!')
elif command == 'ui':
    print('{}')
elif command == 'status':
    print('updated')
'''
        baseline = fixture.replace('MODE', 'False')
        (home / 'bin/brain').write_text(baseline)
        self.git('init', '-q', '-b', 'main', cwd=home)
        self.git('add', '.', cwd=home)
        self.git('commit', '-qm', 'fixture', cwd=home)
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'wt', 'add', 'a', 'job')
        # Register a second open record to prove every ready record is exercised.
        self.b(r, 'wt', 'add', 'a', 'other')
        (r / 'core/bin/brain').write_text(baseline)
        original = rehearsal.snapshot(r)
        inst = Instance(r)
        n = inst.node('a')
        clean = rehearsal.rehearse(inst, n, 'job')
        self.assertTrue(clean['passed'], clean['regressions'])
        self.assertEqual(original, rehearsal.snapshot(r))
        wt = self.tmp / 'candidate-work/worktrees/job'
        (wt / 'bin/brain').write_text(fixture.replace('MODE', 'True'))
        self.git('commit', '-qam', 'incident mutations', cwd=wt)
        bad = rehearsal.rehearse(inst, n, 'job')
        self.assertFalse(bad['passed'])
        for flag in ('fix: new check errors', 'ready:1:0: ready became not ready',
                     'ready:1:1: ready became not ready', 'due:0: new due items',
                     'due:1: new due items', 'brief:0: brief warning disappeared'):
            self.assertIn(flag, bad['regressions'])
        self.assertEqual(original, rehearsal.snapshot(r))
        # No private fixture command text is retained in public evidence.
        self.assertNotIn('upgrade cannot heal', json.dumps(bad))

class RehearsalDelivery(unittest.TestCase):
    def test_real_post_user_agent_status_codes_and_no_redirect_or_url_output(self):
        import http.server
        import threading
        from types import SimpleNamespace
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get('User-Agent'),
                             self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(int(self.path.split('/')[1]))
                self.send_header('Location', '/204/redirected-secret')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        nodes = [dict(id='root' if i == 0 else 'n' + str(i),
                      local=dict(coordinator='thread', wake=base + '/' + str(status) + '/capability'))
                 for i, status in enumerate((204, 403, 302))]
        inst = SimpleNamespace(node=lambda _: nodes[0], local_of=lambda _: {})
        try:
            statuses = rehearsal.probe_delivery(inst, nodes)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual([204, 403, 302], [s['status'] for s in statuses])
        self.assertEqual(['wake:1: HTTP 403', 'wake:2: HTTP 302'], rehearsal.delivery_regressions(statuses))
        self.assertEqual(3, len(seen))
        self.assertTrue(all(agent and 'Python-urllib' not in agent for _, agent, _ in seen))
        self.assertTrue(all(body == b'{"event":"brain-wake"}' for _, _, body in seen))
        self.assertNotIn('capability', json.dumps(statuses))
        self.assertNotIn(base, json.dumps(statuses))

    def test_shared_probe_is_preferred(self):
        from types import SimpleNamespace
        node = dict(id='root', local=dict(coordinator='t', wake='https://example.invalid/private'))
        inst = SimpleNamespace(node=lambda _: node, local_of=lambda _: {})
        with mock.patch.object(rehearsal._m_hosts, 'wake_probe', return_value=403, create=True) as probe:
            self.assertEqual([dict(coordinator=0, status=403)], rehearsal.probe_delivery(inst, [node]))
            probe.assert_called_once_with(inst, node)

@unittest.skipUnless(sys.platform == 'darwin', 'requires macOS OS sandbox')
class RehearsalNativeCore(Scenario):
    def test_real_core_cli_clean_candidate_and_byte_identical_source(self):
        import shutil
        r = self.init('sample')
        home = self.tmp / 'core-home'
        shutil.copytree(CORE, home, symlinks=True, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        self.git('init', '-q', '-b', 'main', cwd=home)
        self.git('add', '.', cwd=home)
        self.git('commit', '-qm', 'candidate core fixture', cwd=home)
        self.b(r, 'new', 'core', '--title', 'Core', '--home', str(home))
        self.b(r, 'wt', 'add', 'core', 'job')
        original_brain = rehearsal.snapshot(r)
        original_home = rehearsal.snapshot(home)
        output = self.b(r, 'rehearse', 'core', '--branch', 'job')
        self.assertIn('rehearse: passed', output)
        self.assertEqual(original_brain, rehearsal.snapshot(r))
        self.assertEqual(original_home, rehearsal.snapshot(home))
        files = list((self.tmp / 'core-home-work/scratch/rehearse').glob('*.json'))
        self.assertEqual(1, len(files))
        evidence = json.loads(files[0].read_text())
        self.assertTrue(evidence['passed'])
        self.assertEqual(11, evidence['commands'])
