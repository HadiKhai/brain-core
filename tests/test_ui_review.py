"""Explicit local review security and scope preservation, using invented proposals only."""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from unittest import mock
from scenario import Scenario, CORE

sys.path.insert(0, str(CORE / 'lib'))
from brain import common, instance, knowledge, ui_review


class LocalReviews(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.init('meadow', operator='Demo reviewer')
        self.b(self.root, 'new', 'garden', '--title', 'Garden')
        self.proposal(self.root / 'library' / 'lesson-a.md', 'lesson-a', 'Demo reviewer')
        self.proposal(self.root / 'nodes/garden/library/lesson-b.md', 'lesson-b', 'Demo reviewer, Demo colleague')
        role = self.root / 'specialists' / 'layout-guide' / 'role.md'
        self.proposal(role, 'layout-guide', 'Demo reviewer')
        self.reviews = ui_review.Reviews(self.root, True)
        self.processes = []
        common.TOUCHED.clear()

    def tearDown(self):
        for p in self.processes:
            p.terminate()
            try: p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()
        for p in self.processes:
            if p.stdout: p.stdout.close()
            if p.stderr: p.stderr.close()
        common.TOUCHED.clear()
        super().tearDown()

    def proposal(self, path, ident, scope):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\nid: ' + ident + '\ntitle: Synthetic ' + ident + '\nstatus: proposed\nscope: ' + scope + '\nsource_node: root\n---\nA made-up lesson for interface checks.\n')

    def select(self, rows=None, action='accept'):
        return self.reviews.prepare({'action': action, 'records': rows or self.reviews.catalog()['proposals']})

    def test_bulk_shared_core_path_preserves_every_scope_and_records_exact_changes(self):
        catalog = self.reviews.catalog()
        scopes = {r['path']: r['scope'] for r in catalog['proposals']}
        prepared = self.select()
        before = knowledge.review_proposals
        with mock.patch.object(knowledge, 'review_proposals', wraps=before) as shared:
            with mock.patch.dict(os.environ, self.env, clear=True):
                result = self.reviews.apply({'token': prepared['token']})
        shared.assert_called_once()
        self.assertEqual(result['count'], 3)
        self.assertEqual(result['reviewer'], 'Demo reviewer')
        for change in result['changes']:
            fm, _ = common.read_fm(self.root / change['path'])
            self.assertEqual(common.lst(fm['scope']), scopes[change['path']])
            self.assertEqual(fm['status'], 'active')
            self.assertEqual(fm['accepted_by'], 'Demo reviewer')
            self.assertEqual(fm['accepted_via'], 'local UI')
            self.assertIn('-status: proposed', change['diff'])
            self.assertIn('+status: active', change['diff'])
        self.assertTrue(result['commit'])
        with self.assertRaises(ui_review.ReviewError): self.reviews.apply({'token': prepared['token']})

    def test_stale_scope_and_missing_selection_fail_before_any_write(self):
        prepared = self.select()
        path = self.root / 'library/lesson-a.md'
        path.write_text(path.read_text().replace('scope: Demo reviewer', 'scope: Demo reviewer, Demo colleague'))
        before = {r['path']: (self.root / r['path']).read_bytes() for r in prepared['records']}
        with self.assertRaisesRegex(ui_review.ReviewError, 'changed'): self.reviews.apply({'token': prepared['token']})
        for name, text in before.items(): self.assertEqual((self.root / name).read_bytes(), text)
        with self.assertRaises(ui_review.ReviewError): self.select([{'kind': 'item', 'id': '../node', 'node': 'root'}])

    def test_current_never_name_policy_and_link_safety_apply_to_the_whole_batch(self):
        prepared = self.select()
        (self.root / 'never-name.txt').write_text('made-up\n')
        with self.assertRaises(ui_review.ReviewError): self.reviews.apply({'token': prepared['token']})
        self.assertTrue(all(common.read_fm(self.root / r['path'])[0]['status'] == 'proposed' for r in prepared['records']))
        (self.root / 'never-name.txt').unlink()
        path = self.root / 'library/lesson-a.md'
        alias = self.tmp / 'alias.md'
        os.link(path, alias)
        safe_catalog = self.reviews.catalog()
        self.assertNotIn("lesson-a", [r["id"] for r in safe_catalog["proposals"]])
        self.assertEqual(common.read_fm(alias)[0]['status'], 'proposed')

    def test_default_off_and_expired_confirmations_never_write(self):
        disabled = ui_review.Reviews(self.root)
        self.assertEqual(disabled.catalog(), {'enabled': False, 'proposals': []})
        with self.assertRaises(ui_review.ReviewError): disabled.prepare({'action': 'accept', 'records': []})
        token = self.select()['token']
        self.reviews.pending[token]['at'] -= 301
        with self.assertRaises(ui_review.ReviewError): self.reviews.apply({'token': token})

    def test_reject_specialist_uses_same_validation_and_review_attribution(self):
        record = next(r for r in self.reviews.catalog()['proposals'] if r['kind'] == 'specialist')
        prepared = self.select([record], 'reject')
        with mock.patch.dict(os.environ, self.env, clear=True): self.reviews.apply({'token': prepared['token']})
        fm, _ = common.read_fm(self.root / record['path'])
        self.assertEqual(fm['status'], 'rejected')
        self.assertEqual(fm['rejected_by'], 'Demo reviewer')
        self.assertEqual(fm['scope'], 'Demo reviewer')

    def test_write_waits_for_cli_instance_lock_and_commits_only_reviewed_files(self):
        import fcntl
        import threading
        prepared = self.select()
        unrelated = self.root / 'status.md'
        unrelated.write_text(unrelated.read_text() + '\nUnrelated synthetic draft.\n')
        holder = subprocess.Popen([sys.executable, '-c',
            'import fcntl,sys;f=open(sys.argv[1],"a+");fcntl.flock(f,fcntl.LOCK_EX);print("locked",flush=True);sys.stdin.read(1)',
            str(self.root / '.brain/lock')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), 'locked')
        result = []
        contended = threading.Event()
        flock = fcntl.flock
        def observed_flock(fd, operation):
            try:
                return flock(fd, operation)
            except BlockingIOError:
                contended.set()  # actual kernel refusal, while the CLI holds it
                raise
        def apply():
            with mock.patch.dict(os.environ, self.env, clear=True):
                result.append(self.reviews.apply({'token': prepared['token']}))
        worker = threading.Thread(target=apply)
        try:
            with mock.patch.object(fcntl, 'flock', side_effect=observed_flock):
                worker.start()
                self.assertTrue(contended.wait(5), 'review never attempted the held instance lock')
                self.assertTrue(worker.is_alive())
                self.assertTrue(all(common.read_fm(self.root / r['path'])[0]['status'] == 'proposed' for r in prepared['records']))
        finally:
            holder.communicate('x', timeout=10)
            if worker.ident is not None:
                worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]['count'], 3)
        self.assertIn(' M status.md', self.git('status', '--short', cwd=self.root))
        self.assertNotIn('Unrelated synthetic draft.', self.git('show', 'HEAD:status.md', cwd=self.root))

    def test_failed_commit_restores_files_and_exact_previous_index_entries(self):
        selected = self.reviews.catalog()['proposals']
        target = self.root / selected[0]['path']
        self.git('add', '--', str(target.relative_to(self.root)), cwd=self.root)
        staged = self.git('show', ':' + str(target.relative_to(self.root)), cwd=self.root)
        target.write_text(target.read_text() + '\nUnstaged synthetic draft.\n')
        prepared = self.select()
        before = {r['path']: (self.root / r['path']).read_bytes() for r in prepared['records']}
        index = self.git('ls-files', '--stage', cwd=self.root)
        hooks = self.tmp / 'hooks'
        hooks.mkdir()
        hook = hooks / 'pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n'); hook.chmod(0o755)
        self.git('config', 'core.hooksPath', str(hooks), cwd=self.root)
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(ui_review.ReviewError, 'restored'):
                self.reviews.apply({'token': prepared['token']})
        for name, data in before.items(): self.assertEqual((self.root / name).read_bytes(), data)
        self.assertEqual(self.git('ls-files', '--stage', cwd=self.root), index)
        self.assertEqual(self.git('show', ':' + str(target.relative_to(self.root)), cwd=self.root), staged)

    def test_later_file_failure_restores_already_written_proposals(self):
        prepared = self.select()
        before = {r['path']: (self.root / r['path']).read_bytes() for r in prepared['records']}
        real_write = common.write_fm
        calls = []
        def fail_second(*args):
            calls.append(args[0])
            if len(calls) == 2: raise OSError('synthetic second write failure')
            return real_write(*args)
        with mock.patch.object(common, 'write_fm', side_effect=fail_second):
            with self.assertRaisesRegex(ui_review.ReviewError, 'restored'):
                self.reviews.apply({'token': prepared['token']})
        self.assertEqual(len(calls), 2)
        for name, data in before.items(): self.assertEqual((self.root / name).read_bytes(), data)

    def test_whole_instance_outward_link_precondition_matches_cli(self):
        prepared = self.select()
        outside = self.tmp / 'outside.txt'; outside.write_text('Synthetic outside file.\n')
        (self.root / 'library/outside.md').symlink_to(outside)
        with self.assertRaisesRegex(ui_review.ReviewError, 'link'):
            self.reviews.apply({'token': prepared['token']})
        self.assertTrue(all(common.read_fm(self.root / r['path'])[0]['status'] == 'proposed' for r in prepared['records']))

    def test_catalog_screens_titles_and_does_not_export_unsafe_identities(self):
        (self.root / 'never-name.txt').write_text('lesson-a\nSynthetic\n')
        value = json.dumps(self.reviews.catalog())
        self.assertNotIn('lesson-a', value)
        self.assertNotIn('Synthetic', value)

    def test_duplicate_item_identity_cannot_review_a_different_file(self):
        duplicate = self.root / 'nodes/garden/library/duplicate.md'
        self.proposal(duplicate, 'lesson-a', 'Demo reviewer')
        prepared = self.select([r for r in self.reviews.catalog()['proposals'] if r['path'].endswith('duplicate.md')])
        with self.assertRaises(ui_review.ReviewError): self.reviews.apply({'token': prepared['token']})
        self.assertEqual(common.read_fm(duplicate)[0]['status'], 'proposed')
        self.assertEqual(common.read_fm(self.root / 'library/lesson-a.md')[0]['status'], 'proposed')

    def test_same_origin_local_key_required_even_with_a_valid_preview_key(self):
        headers = {'Host': '127.0.0.1:4747', 'Origin': 'http://127.0.0.1:4747',
                   'Content-Type': 'application/json', 'X-Brain-Review': 'synthetic-key', 'Sec-Fetch-Site': 'same-origin'}
        self.assertTrue(ui_review.authorized_write(headers, '127.0.0.1', 4747, 'synthetic-key'))
        for bad in ({'Origin': 'https://example.com'}, {'Origin': ''}, {'Host': 'localhost:9999'},
                    {'X-Brain-Review': ''}, {'Content-Type': 'text/plain'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertFalse(ui_review.authorized_write({**headers, **bad}, '127.0.0.1', 4747, 'synthetic-key'))
        self.assertFalse(ui_review.authorized_write(headers, '192.0.2.1', 4747, 'synthetic-key'))

    def test_http_write_mode_rejects_cross_origin_before_core_paths_and_refreshes_state(self):
        env = {**self.env, 'BRAIN_ROOT': str(self.root), 'BRAIN_CONFIG': str(self.tmp / 'brains.json')}
        p = subprocess.Popen([sys.executable, str(CORE / 'bin/brain'), 'ui', '--write', '--port', '0', '--every', '.05'],
                             cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(p)
        p.stdout.readline()
        line = p.stdout.readline()
        import re
        url = re.search(r'(http://127\.0\.0\.1:\d+/\?k=[\w-]+)', line)
        self.assertIsNotNone(url, line + (p.stderr.read() if p.poll() is not None else ''))
        url = url.group(1)
        origin, key = url.split('/?k=')
        def send(path, body, extra):
            req = urllib.request.Request(origin + path + '?k=' + key, data=json.dumps(body).encode(),
                     headers={'Content-Type': 'application/json', 'X-Brain-Review': key, **extra})
            return urllib.request.urlopen(req, timeout=40)
        with self.assertRaises(urllib.error.HTTPError) as error:
            send('/api/review/prepare', {'action': 'accept', 'records': []}, {'Origin': 'https://example.com'})
        self.assertEqual(error.exception.code, 403)
        catalog = json.load(urllib.request.urlopen(origin + '/api/review?k=' + key))
        prepared = json.load(send('/api/review/prepare', {'action': 'reject', 'records': [catalog['proposals'][0]]}, {'Origin': origin}))
        result = json.load(send('/api/review/apply', {'token': prepared['token']}, {'Origin': origin}))
        self.assertEqual(result['count'], 1)
        state = json.load(urllib.request.urlopen(origin + '/api/state?k=' + key))
        self.assertTrue(state['review']['enabled'])
        self.assertIn('rejected', [r['status'] for r in state['items']['all']])
