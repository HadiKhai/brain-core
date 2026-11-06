"""Review carry-over across pure rebases, using only invented projects and reviewers."""
import json
from unittest import mock

from scenario import Scenario
import test_review_discipline as _review
import test_verify as _test_verify


class ReviewCarryOver(Scenario):
    job = _test_verify.WorkerReadyPackets.job
    rules = _review.RuleReviewContracts.rules

    def reviewed(self, verdict='pass'):
        r, wt, m, inst, n, receipt = _review.IndependentReviewReceipts.context(self)
        rows = m.read_jobs(n)
        rows[-1]['Review rules digest'] = m.rules_digest(m.applicable_rules(n))
        m.write_jobs(inst, n, rows)
        n = inst.node('a')
        receipt['verdict'] = verdict
        m.record_review_receipt(inst, n, 'job', receipt)
        row = m.open_job(m.read_jobs(n), 'job')
        source = m.review_receipt_path(n, row, receipt['head'])
        return r, wt, m, inst, n, receipt, source

    def move_main_and_rebase(self, wt):
        home = self.tmp / 'app'
        (home / 'notes.txt').write_text('unrelated main change\n')
        self.git('add', 'notes.txt', cwd=home)
        self.git('commit', '-qm', 'main moves', cwd=home)
        self.git('rebase', '-q', 'main', cwd=wt)
        return self.git('rev-parse', 'HEAD', cwd=wt).strip()

    def test_pure_rebase_carries_with_provenance_and_original_untouched(self):
        r, wt, m, inst, n, receipt, source = self.reviewed()
        before = source.read_bytes()
        head = self.move_main_and_rebase(wt)
        self.assertNotEqual(head, receipt['head'])
        self.assertEqual(m.validate_ready_reviews(inst, n, 'job', head), [])
        text = self.b(r, 'review', 'a', '--branch', 'job', '--carry')
        self.assertIn('review receipt carried to ' + head + ' from ' + receipt['head'], text)
        row = m.open_job(m.read_jobs(n), 'job')
        carried = json.loads(m.review_receipt_path(n, row, head).read_text())
        carry = carried['carry']
        self.assertEqual(carried['head'], head)
        self.assertEqual(carry['source_head'], receipt['head'])
        self.assertEqual(carry['source_receipt'], str(source.relative_to(r)))
        self.assertEqual(carry['patch_id'], carry['source_patch_id'])
        self.assertEqual(set(carry['patch_id']), {'stable', 'verbatim'})
        self.assertNotEqual(carry['base'], carry['source_base'])
        self.assertEqual(carry['rules_digest'], row['Review rules digest'])
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(m.validate_ready_reviews(inst, n, 'job', head), [])
        self.assertIs(m.validate_review_receipt(inst, n, row, head, carried), carried)
        self.assertIn('already exists', self.b(r, 'review', 'a', '--branch', 'job', '--carry', ok=False))
        with self.assertRaises(ValueError):  # recording paths never accept a hand-made carry
            m.record_review_receipt(inst, n, 'job', carried)
        source.write_bytes(before.replace(b'"pass"', b'"pass" '))
        with self.assertRaisesRegex(ValueError, 'changed since carry-over'):
            m.validate_review_receipt(inst, n, row, head, carried)

    def test_readiness_persists_the_carry_and_preview_receipts_stay_exact_head(self):
        r, wt, m, inst, n, receipt, source = self.reviewed()
        head = self.move_main_and_rebase(wt)
        row = m.open_job(m.read_jobs(n), 'job')
        with mock.patch.dict('os.environ', self.env, clear=True), mock.patch.object(m, 'request_wake', return_value=True):
            packet, reasons = m.record_ready(inst, n, 'job', 1)
        self.assertEqual(reasons, [])
        self.assertEqual(packet['head'], head)
        self.assertEqual(json.loads(m.review_receipt_path(n, row, head).read_text())['carry']['source_head'],
                         receipt['head'])
        self.assertFalse(m.preview_receipt_path(inst, n, row, head).exists())

    def test_one_changed_byte_refuses(self):
        r, wt, m, inst, n, receipt, source = self.reviewed()
        self.move_main_and_rebase(wt)
        (wt / 'calc.py').write_text('value = 2 \n')  # whitespace only: git patch-id --stable alone ignores it
        self.git('commit', '-q', '--amend', '-a', '--no-edit', cwd=wt)
        head = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        reasons = m.validate_ready_reviews(inst, n, 'job', head)
        self.assertEqual(len(reasons), 1)
        self.assertIn('review carry-over refused: receipt at ' + receipt['head'][:12] + ': patch differs', reasons[0])
        text = self.b(r, 'review', 'a', '--branch', 'job', '--carry', ok=False)
        self.assertIn('patch differs', text)
        row = m.open_job(m.read_jobs(n), 'job')
        self.assertFalse(m.review_receipt_path(n, row, head).exists())

    def test_changed_rules_digest_refuses(self):
        r, wt, m, inst, n, receipt, source = self.reviewed()
        head = self.move_main_and_rebase(wt)
        self.rules(r)
        n = m.Instance(r).node('a')
        reasons = m.validate_ready_reviews(inst, n, 'job', head)
        self.assertIn('review carry-over refused: review rules changed since the review brief', reasons[0])
        rows = m.read_jobs(n)
        rows[-1]['Review rules digest'] = m.rules_digest(m.applicable_rules(n))  # a fresh brief, old receipt
        m.write_jobs(inst, n, rows)
        reasons = m.validate_ready_reviews(inst, n, 'job', head)
        self.assertIn('receipt at ' + receipt['head'][:12] + ': rules digest', reasons[0])
        self.assertIn('differs from current', reasons[0])
        self.assertIn('review carry-over refused', self.b(r, 'review', 'a', '--branch', 'job', '--carry', ok=False))

    def test_blocked_receipt_never_carries(self):
        r, wt, m, inst, n, receipt, source = self.reviewed(verdict='blocked')
        head = self.move_main_and_rebase(wt)
        reasons = m.validate_ready_reviews(inst, n, 'job', head)
        self.assertIn('the latest review of this patch (receipt at ' + receipt['head'][:12] + ') is blocked', reasons[0])
        self.assertIn('is blocked', self.b(r, 'review', 'a', '--branch', 'job', '--carry', ok=False))

    def test_other_jobs_receipt_never_carries(self):
        r, wt, m, inst, n, receipt, source = self.reviewed()
        self.b(r, 'wt', 'add', 'a', 'other')
        other = self.tmp / 'app-work/worktrees/other'
        (other / 'calc.py').write_text('value = 2\n')
        self.git('commit', '-qam', 'fix', cwd=other)
        inst = m.Instance(r)
        n = inst.node('a')
        rows = m.read_jobs(n)
        rows[-1].update({'Runtime': 'codex', 'Model': 'example-model',
                         'Review rules digest': m.rules_digest(m.applicable_rules(n))})
        m.write_jobs(inst, n, rows)
        head = self.git('rev-parse', 'HEAD', cwd=other).strip()
        reasons = m.validate_ready_reviews(inst, n, 'other', head)
        self.assertEqual(reasons, ['missing, stale, blocked or non-independent exact-head review receipt'])
        row = m.open_job(m.read_jobs(n), 'other')
        planted = m.review_receipt_path(n, row, receipt['head'])
        planted.parent.mkdir(parents=True)
        planted.write_bytes(source.read_bytes())
        reasons = m.validate_ready_reviews(inst, n, 'other', head)
        self.assertIn('review carry-over refused: receipt at ' + receipt['head'][:12] + ':', reasons[0])
        self.assertIn('review receipt needs exact code', reasons[0])
        self.assertIn('review carry-over refused', self.b(r, 'review', 'a', '--branch', 'other', '--carry', ok=False))
        self.assertFalse(m.review_receipt_path(n, row, head).exists())
