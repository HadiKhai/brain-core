"""Optional real-core rehearsal diagnostic; run explicitly, outside per-change lanes.

python3 -B -m pytest -q -p no:cacheprovider tests/nightly_rehearse.py::RehearsalNativeCore::test_real_core_cli_clean_candidate_and_byte_identical_source
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from brain import rehearse as rehearsal
from scenario import Scenario, CORE


@unittest.skipUnless(sys.platform == 'darwin', 'requires macOS OS sandbox')
class RehearsalNativeCore(Scenario):
    retain_rehearsal_evidence = True

    def test_real_core_cli_clean_candidate_and_byte_identical_source(self):
        try:
            self._real_core_cli_clean_candidate_and_byte_identical_source()
        except BaseException as error:
            self.native_evidence_exception(error)
            raise
        finally:
            self.retain_native_evidence()

    def _real_core_cli_clean_candidate_and_byte_identical_source(self):
        import http.server
        import shutil
        import threading
        self.update_native_evidence(stage='optional-setup')
        r = self.init('sample')
        self.update_native_evidence(root=str(r))
        home = self.tmp / 'core-home'
        shutil.copytree(CORE, home, symlinks=True, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        self.git('init', '-q', '-b', 'main', cwd=home)
        self.git('add', '.', cwd=home)
        self.git('commit', '-qm', 'candidate core fixture', cwd=home)
        self.b(r, 'new', 'engine', '--title', 'Engine', '--home', str(home))
        self.b(r, 'wt', 'add', 'engine', 'job')
        self.b(r, 'check', '--fix')
        # Model an upgrade from a core whose check did not probe delivery. The
        # candidate check must see the real status cached into its throwaway copy.
        entry = r / 'core/bin/brain'
        shutil.copy2(entry, entry.with_name('installed-entry'))
        entry.write_text('import sys, runpy\nfrom pathlib import Path\n'
                         'if sys.argv[1:2] == ["check"]:\n    print("✓ checks pass")\n'
                         'else:\n    runpy.run_path(str(Path(__file__).with_name("installed-entry")), run_name="__main__")\n')
        posts = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                posts.append(True)
                self.send_response(204)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        local_file = r / '.brain/local.json'
        local = json.loads(local_file.read_text())
        local['nodes'].setdefault('root', {}).update(coordinator='thread', wake='http://127.0.0.1:' + str(server.server_port))
        local_file.write_text(json.dumps(local))
        original_brain = rehearsal.snapshot(r)
        original_home = rehearsal.snapshot(home)
        self.update_native_evidence(stage='optional-command')
        try:
            import argparse
            # Control only the parent's elapsed timer; the real CLI subprocesses,
            # sandbox, delivery probe and their timeout clocks still run normally.
            ticks = iter(range(100))
            clock = mock.Mock()
            clock.monotonic.side_effect = lambda: next(ticks) / 4
            with mock.patch.dict(os.environ, self.env), \
                    mock.patch.object(rehearsal, 'time', clock), \
                    mock.patch.object(rehearsal._m_nodes, 'find_root', return_value=r), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                try:
                    rehearsal.cmd_rehearse(argparse.Namespace(id='engine', branch='job'))
                except SystemExit:
                    evidence = list((self.tmp / 'core-home-work/scratch/rehearse').glob('*.json'))
                    self.fail(stdout.getvalue() + '\n' + '\n'.join(p.read_text() for p in evidence))
            output = stdout.getvalue()
            self.update_native_evidence(stage='optional-assertions')
            self.assertIn('rehearse: passed', output)
            self.assertEqual(1, len(posts))
            self.assertEqual(original_brain, rehearsal.snapshot(r))
            self.assertEqual(original_home, rehearsal.snapshot(home))
            files = list((self.tmp / 'core-home-work/scratch/rehearse').glob('*.json'))
            self.assertEqual(1, len(files))
            evidence = json.loads(files[0].read_text())
            self.assertTrue(evidence['passed'])
            self.assertEqual(11, evidence['commands'])
            self.assertEqual(.25, evidence['hook_seconds'])
            self.assertEqual(44, clock.monotonic.call_count)  # before/after for both lanes
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
