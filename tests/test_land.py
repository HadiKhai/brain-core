"""Batch landing and supplied host evidence use disposable fabricated projects."""
import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
from unittest import mock

import core_loader
import test_jobs
import test_train
from scenario import CORE, Scenario


class BatchLand(Scenario):
    fixture_init_in_process = True
    project = test_train.MergeTrain.project
    job = test_train.MergeTrain.job
    batches = test_train.MergeTrain.batches
    ledger = test_train.MergeTrain.ledger

    def module(self, root):
        m = core_loader.SourceFileLoader('batch_land_fixture', str(CORE / 'bin/brain')).load_module()
        with mock.patch.dict(os.environ, self.env, clear=True):
            inst = m.Instance(root)
        return m, inst, inst.node('a')

    def task_job(self, ident, branch):
        self.b(self.root, 'task', 'add', 'a', ident, 'status=ready', 'scope=' + branch,
               '--title', 'Deliver ' + branch)
        self.b(self.root, 'wt', 'add', 'a', branch, '--task-id', ident)
        local = json.loads((self.root / '.brain/local.json').read_text())
        wt = Path(local['nodes']['a']['worktrees'][branch]['path'])
        (wt / branch).write_text(branch + '\n')
        self.git('add', branch, cwd=wt)
        self.git('commit', '-qm', branch, cwd=wt)
        self.b(self.root, 'handback', 'a', '--branch', branch, '--state', 'done',
               '--summary', 'Synthetic result', '--reviews', '1')

    def test_native_land_completes_delivered_task_ids_at_main_and_unblocks_dependents(self):
        root, home = self.project()
        self.task_job('DELIVER-1', 'one')
        self.task_job('DELIVER-2', 'two')
        self.b(root, 'task', 'add', 'a', 'FOLLOW', 'status=blocked', 'depends_on=DELIVER-1,DELIVER-2',
               '--title', 'Wait for both')
        self.b(root, 'task', 'add', 'a', 'UNRELATED', '--title', 'Separate work')
        self.b(root, 'land', 'a', '--jobs', 'one', 'two')
        main = self.git('rev-parse', 'main', cwd=home).strip()
        rows = {r['id']: r for r in json.loads(self.b(root, 'task', 'list', 'a', '--json'))}
        for index, branch in enumerate(('one', 'two'), 1):
            task = rows['DELIVER-' + str(index)]
            self.assertEqual(task['status'], 'done')
            self.assertEqual(task['merge_sha'], main)
            self.assertEqual(task['code_path'], branch)
            self.assertEqual(task['attempts'][0]['state'], 'closed')
            self.assertTrue(task['landing_event'])
        self.assertEqual(rows['FOLLOW']['status'], 'ready')
        self.assertEqual(rows['UNRELATED']['status'], 'backlog')
        self.b(root, 'task', 'check', 'a')

    def test_native_manual_landing_uses_main_commit_beyond_the_delivered_job_head(self):
        root, home = self.project()
        self.task_job('DELIVER', 'one')
        branch_head = self.git('rev-parse', 'one', cwd=home).strip()
        self.b(root, 'task', 'add', 'a', 'FOLLOW', 'status=blocked', 'depends_on=DELIVER',
               '--title', 'Follow delivered job')
        self.git('merge', '--ff-only', 'one', cwd=home)
        (home / 'later').write_text('Separate later work\n')
        self.git('add', 'later', cwd=home)
        self.git('commit', '-qm', 'Later main work', cwd=home)
        main = self.git('rev-parse', 'main', cwd=home).strip()
        self.assertNotEqual(branch_head, main)
        self.b(root, 'jobs', 'a', '--branch', 'one', '--close')
        rows = {r['id']: r for r in json.loads(self.b(root, 'task', 'list', 'a', '--json'))}
        self.assertEqual(rows['DELIVER']['status'], 'done')
        self.assertEqual(rows['DELIVER']['merge_sha'], main)
        self.assertEqual(rows['DELIVER']['code_path'], 'one')
        self.assertEqual(rows['FOLLOW']['status'], 'ready')
        self.assertIn(branch_head, ' '.join(rows['DELIVER']['evidence']))

    def test_foreign_occupied_same_tip_never_owns_publication(self):
        root, home = self.project()
        self.job('one')
        m, inst, n = self.module(root)
        git = m.git
        outputs = []
        def race(*args, **kwargs):
            if args[:2] == ('merge', '--ff-only'):
                self.git('merge', '--ff-only', args[-1], cwd=home)
                result = git(*args, **kwargs)
                outputs.append(result.stdout)
                return result
            return git(*args, **kwargs)
        before = self.ledger()
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(root)}, clear=True), \
                mock.patch.object(m, 'git', side_effect=race), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'publication ownership'):
                m.cmd_land(argparse.Namespace(id='a', jobs=['one']))
        self.assertEqual(len(outputs), 1)
        self.assertIn('Already up to date.', outputs[0])
        data = self.batches()[0]
        self.assertEqual(data['state'], 'publication_failed')
        self.assertIsNot(data.get('publication_succeeded'), True)
        self.assertEqual(self.ledger(), before)
        self.assertEqual(self.git('rev-parse', 'main', cwd=home).strip(), data['tip'])
        self.assertIn('unresolved publication', self.b(root, 'land', 'a', ok=False))
        self.assertEqual(self.ledger(), before)

    def test_late_main_attachment_preserves_stale_checkout_without_success(self):
        root, home = self.project()
        self.job('one')
        self.git('checkout', '--detach', '-q', cwd=home)
        linked = self.tmp / 'late-main'
        m, inst, n = self.module(root)
        git = m.git
        saved = []
        def race(*args, **kwargs):
            if args[:2] == ('update-ref', '-m'):
                self.git('worktree', 'add', str(linked), 'main', cwd=home)
                gitdir = Path(self.git('rev-parse', '--absolute-git-dir', cwd=linked).strip())
                saved.append(((gitdir / 'index'), (gitdir / 'index').read_bytes(),
                              (linked / 'shared').read_bytes()))
            return git(*args, **kwargs)
        before = self.ledger()
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(root)}, clear=True), \
                mock.patch.object(m, 'git', side_effect=race), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'occupancy changed'):
                m.cmd_land(argparse.Namespace(id='a', jobs=['one']))
        self.assertEqual(len(saved), 1)
        index, index_bytes, shared = saved[0]
        self.assertEqual(index.read_bytes(), index_bytes)
        self.assertEqual((linked / 'shared').read_bytes(), shared)
        self.assertFalse((linked / 'one').exists())
        data = self.batches()[0]
        self.assertEqual(self.git('rev-parse', 'main', cwd=home).strip(), data['tip'])
        self.assertEqual(data['state'], 'publication_failed')
        self.assertIsNot(data.get('publication_succeeded'), True)
        self.assertEqual(self.ledger(), before)

    def classification(self, study):
        root, home = self.project()
        self.job('one')
        base = self.git('rev-parse', 'main', cwd=home).strip()
        name = 'import:' + base if study else 'missing-real-branch'
        if study:
            self.b(root, 'jobs', 'a', '--branch', name, '--start')
        else:
            self.b(root, 'wt', 'add', 'a', name)
            m, inst, n = self.module(root)
            self.git('worktree', 'remove', n['local']['worktrees'][name]['path'], cwd=home)
            self.git('branch', '-D', name, cwd=home)
        m, inst, n = self.module(root)
        rows = m.read_jobs(n)
        packet = m.handback_file(n, rows[0]).read_bytes()
        before = dict(rows[1])
        out = self.b(root, 'land', 'a', '--jobs', 'one', ok=study)
        if study:
            self.assertIn('merged 1 job(s); 1 full verify run(s)', out)
            after = m.read_jobs(n)
            self.assertEqual(after[0]['Outcome'], 'merged')
            self.assertEqual({k:v for k,v in after[1].items() if v},
                             {k:v for k,v in before.items() if v})
        else:
            self.assertIn('missing commit refs/heads/' + name, out)
            self.assertEqual(m.read_jobs(n), rows)
            self.assertEqual(self.git('rev-parse', 'main', cwd=home).strip(), base)
        self.assertEqual(m.handback_file(n, rows[0]).read_bytes(), packet)

    def test_open_import_is_excluded_from_classification(self):
        self.classification(True)

    def test_missing_real_branch_still_refuses_classification(self):
        self.classification(False)

    def test_valid_first_invalid_second_closure_is_all_or_none(self):
        root, home = self.project()
        self.job('one')
        self.job('two')
        m, inst, n = self.module(root)
        rows = m.read_jobs(n)
        before = self.ledger()
        packets = [m.handback_file(n, r).read_bytes() for r in rows]
        entries = [dict(key=m.job_key(r), branch=r['Job'],
                        head=self.git('rev-parse', r['Job'], cwd=home).strip(),
                        base=self.git('rev-parse', 'main', cwd=home).strip(), commits=1) for r in rows]
        entries[1]['branch'] = 'wrong'
        data = dict(accepted=entries, ended=m.stamp(inst), batch='train/fixture', publication_succeeded=True)
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'ledger job changed'):
                m.finish(inst, n, self.tmp / 'journal.json', data)
        self.assertEqual(self.ledger(), before)
        self.assertEqual([m.handback_file(n, r).read_bytes() for r in rows], packets)

    def test_batch_land_closes_exact_jobs_and_requires_fresh_safe_settlement(self):
        root, home = self.project()
        for branch in ('one', 'two'):
            self.job(branch)
        m, inst, n = self.module(root)
        for row in m.read_jobs(n):
            self.b(root, 'set', 'a', 'worker:' + m.job_key(row) + '=worker-' + row['Job'])
        out = self.b(root, 'land', 'a', '--jobs', 'one', 'two')
        self.assertIn('merged 2 job(s); 1 full verify run(s)', out)
        self.assertIn('settlement pending', out)
        m, inst, n = self.module(root)
        rows = m.read_jobs(n)
        self.assertEqual([r['Outcome'] for r in rows], ['merged', 'merged'])
        self.assertTrue(all(not r['Settled'] for r in rows))
        self.assertEqual(self.git('status', '--porcelain', cwd=home), '')
        path = test_jobs.settlement_export(self, root, 'a', all_closed=True)
        evidence = json.loads(Path(path).read_text())
        before = self.ledger()
        for field, value in (('state', 'running'), ('state', 'waiting'), ('state', 'unknown'),
                             ('pending_requests', ['question']), ('settled', False)):
            bad = copy.deepcopy(evidence)
            bad['jobs'][1]['threads'][0][field] = value
            with open(path, 'w') as f:
                json.dump(bad, f)
            self.b(root, 'jobs', 'a', '--settled', '--all-closed', '--attestation-file', path, ok=False)
            self.assertEqual(self.ledger(), before)
        with open(path, 'w') as f:
            json.dump(evidence, f)
        out = self.b(root, 'jobs', 'a', '--settled', '--all-closed', '--attestation-file', path)
        self.assertIn('coordinator-supplied evidence; no independent live host verification', out)
        self.assertTrue(all(r['Settled'] for r in m.read_jobs(n)))

    def unresolved(self):
        root, home = self.project()
        wt = self.job('one')
        m, inst, n = self.module(root)
        row = m.read_jobs(n)[0]
        tip = self.git('rev-parse', 'one', cwd=home).strip()
        self.git('merge', '--ff-only', 'one', cwd=home)
        folder = n['dir'] / 'job-trains'
        folder.mkdir()
        data = dict(version=1, state='verified', tip=tip, batch='train/fixture', accepted=[
            dict(key=m.job_key(row), branch='one', head=tip, base=tip, commits=1)])
        (folder / 'fixture.json').write_text(json.dumps(data))
        return root, home, wt, folder / 'fixture.json', data

    def test_unknown_publication_cannot_close_by_ancestry(self):
        root, home, wt, path, data = self.unresolved()
        before = self.ledger()
        for state in ('verified', 'publishing', 'publication_failed'):
            data['state'] = state
            path.write_text(json.dumps(data))
            out = self.b(root, 'jobs', 'a', '--branch', 'one', '--close', ok=False)
            self.assertIn('publication', out)
            self.assertEqual(self.ledger(), before)

    def test_unknown_publication_cannot_prune_or_remove_worktree(self):
        root, home, wt, path, data = self.unresolved()
        before = self.ledger()
        out = self.b(root, 'wt', 'prune', 'a', '--offline')
        self.assertIn('publication', out)
        self.assertTrue(wt.is_dir())
        self.b(root, 'wt', 'rm', 'a', 'one', ok=False)
        self.assertTrue(wt.is_dir())
        self.assertEqual(self.ledger(), before)

    def test_finish_refuses_key_matching_a_different_branch(self):
        root, home = self.project()
        self.job('one')
        m, inst, n = self.module(root)
        row = m.read_jobs(n)[0]
        before = self.ledger()
        data = dict(accepted=[dict(key=m.job_key(row), branch='other', head='a' * 40,
                                  base='b' * 40, commits=1)], ended=m.stamp(inst), batch='train/fixture')
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'ledger job changed'):
                m.finish(inst, n, self.tmp / 'journal.json', data)
        self.assertEqual(self.ledger(), before)

    def test_only_conflicting_jobs_get_rebase_advice_and_keep_packets(self):
        root, home = self.project()
        self.job('one', {'shared': 'landed\n'})
        self.job('clean')
        self.job('conflict', {'shared': 'other\n'})
        m, inst, n = self.module(root)
        # Normalize existing review evidence before observing landing's changes.
        # write_jobs repairs legacy display counts from retained ready packets.
        with mock.patch.dict(os.environ, self.env, clear=True):
            m.write_jobs(inst, n, m.read_jobs(n))
        before = {r['Job']: (m.handback_file(n, r).read_bytes(), dict(r))
                  for r in m.read_jobs(n) if r['Job'] != 'one'}
        out = self.b(root, 'train', 'a', '--jobs', 'one')
        self.assertIn('rebase required (conflicts with landed main): conflict', out)
        self.assertNotIn('rebase required (conflicts with landed main): clean', out)
        for row in m.read_jobs(n):
            if row['Job'] in before:
                packet, original = before[row['Job']]
                # Ledger schema may add empty train columns to every row.
                self.assertEqual({k: v for k, v in row.items() if v},
                                 {k: v for k, v in original.items() if v})
                self.assertEqual(m.handback_file(n, row).read_bytes(), packet)
        self.assertIn('main', self.b(root, 'ready', 'a', '--branch', 'clean', ok=False))
        ledger = (n['dir'] / 'job-ledger.md').read_bytes()
        with mock.patch.dict(os.environ, self.env, clear=True):
            ordinary = m.ready_state(inst, n, 'clean', display=True)
            self.assertFalse(ordinary['ready'])
            self.assertIn('main moved', '; '.join(ordinary['reasons']))

            # Text due, JSON due and UI due all consume these same items.
            due = m.due_items(inst, n, include_stale=False, include_advice=False)
            handbacks = {item['job']: item for item in due if item['kind'] == 'handback'}
            clean = handbacks[m.job_key(before['clean'][1])]
            conflict = handbacks[m.job_key(before['conflict'][1])]
            with self.subTest(advice='compatible recorded-base packet'):
                self.assertFalse(clean['ready'])
                self.assertIn('eligible for batch', clean['text'])
                self.assertRegex(clean['text'], r'exact[- ]result.*verif')
                self.assertNotIn('rebase', json.dumps(clean).lower())
                self.assertNotIn('ready to merge', clean['text'])
            with self.subTest(advice='actual conflict'):
                self.assertFalse(conflict['ready'])
                self.assertIn('merge-tree is not clean', conflict['reasons'])
                self.assertIn('rebase', conflict['text'])
                self.assertNotIn('eligible for batch', json.dumps(conflict))

            # PR mode and unrelated readiness failures must retain their refusals.
            with self.subTest(advice='PR mode'), mock.patch.dict(n['fm'], {'merge': 'pr'}):
                due = m.due_items(inst, n, include_stale=False, include_advice=False)
                clean = next(item for item in due if item['kind'] == 'handback'
                             and item['job'] == m.job_key(before['clean'][1]))
                self.assertFalse(clean['ready'])
                self.assertIn('main moved', '; '.join(clean['reasons']))
                self.assertNotIn('eligible for batch', json.dumps(clean))
            with self.subTest(advice='verify command changed'), mock.patch.dict(n['fm'], {'verify': 'false'}):
                due = m.due_items(inst, n, include_stale=False, include_advice=False)
                clean = next(item for item in due if item['kind'] == 'handback'
                             and item['job'] == m.job_key(before['clean'][1]))
                self.assertFalse(clean['ready'])
                self.assertIn('node verify missing, changed or failed', clean['reasons'])
                self.assertNotIn('eligible for batch', json.dumps(clean))

            # Watch deliberately omits readiness; this path must stay unevaluated.
            rows = m.read_jobs(n)
            worktrees = {b: {**w, **m.local_job_state(n, b, w)}
                         for b, w in n['local'].get('worktrees', {}).items()}
            signals = {m.job_key(r): m.job_signal(inst, n, r) for r in rows or []}
            state = {'jobs': rows, 'worktrees': worktrees, 'handbacks': signals,
                     'inbox': m.inbox_lines(n)}
            with mock.patch.object(m, 'ready_state', side_effect=AssertionError('readiness evaluated')):
                due = m.due_items(inst, n, evidence=(state, '', ''), include_stale=False,
                                  include_advice=False, include_ready=False)
            clean = next(item for item in due if item['kind'] == 'handback'
                         and item['job'] == m.job_key(before['clean'][1]))
            self.assertFalse(clean['ready'])
            self.assertNotIn('eligible for batch', json.dumps(clean))
            self.assertFalse(m.ready_state(inst, n, 'clean')['ready'])
        self.assertEqual((n['dir'] / 'job-ledger.md').read_bytes(), ledger)
        for branch, (packet, _) in before.items():
            row = next(row for row in m.read_jobs(n) if row['Job'] == branch)
            self.assertEqual(m.handback_file(n, row).read_bytes(), packet)
