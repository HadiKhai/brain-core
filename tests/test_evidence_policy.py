"""Evidence rules refuse at the command that breaks them; invented projects only."""
import hashlib
import importlib
import json
import os
from unittest import mock

import core_loader
from scenario import CORE, Scenario

SECTIONS = "mode: untimed\n\n" + "".join("## " + field + "\nSynthetic scope.\n\n" for field in
                                         ("Outcome", "Authorized", "Discretion", "Stop when", "Needs consent", "Done means"))
CHECKLIST = ('buttons', 'spacing', 'links', 'validation', 'navigation', 'consistency')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EvidencePolicy(Scenario):
    def job(self):
        r = self.init('alice')
        home = self.repo(self.tmp / 'app', {'calc.py': 'value = 1\n',
                                            'test_calc.py': 'def test_value():\n    assert True\n\n\n'
                                                            'class TestCalc:\n    def test_value(self):\n'
                                                            '        assert True\n',
                                            'score.py': 'def score(expected, got):\n    return float(expected == got)\n'})
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'set', 'a', 'runtime=claude')
        self.b(r, 'wt', 'add', 'a', 'job')
        wt = self.tmp / 'app-work/worktrees/job'
        return r, home, wt

    def core(self, root):
        return core_loader.SourceFileLoader('evidence_policy_fixture', str(CORE / 'bin/brain')).load_module()

    def head(self, wt):
        return self.git('rev-parse', 'HEAD', cwd=wt).strip()

    def commit(self, wt, message, files=None):
        for name, body in (files or {}).items():
            (wt / name).write_text(body)
        self.git('add', '-A', cwd=wt)
        self.git('commit', '--allow-empty', '-qm', message, cwd=wt)
        return self.head(wt)

    def contract(self, r, extra='', ok=True):
        path = self.tmp / 'contract.md'
        path.write_text(SECTIONS + extra)
        return self.b(r, 'jobs', 'a', '--branch', 'job', '--contract-file', str(path), ok=ok)

    def fixed_set(self):
        path = self.tmp / 'real-set.jsonl'
        path.write_text('{"input": "invented item one"}\n{"input": "invented item two"}\n'
                        '{"input": "invented item three"}\n')
        return path

    def scored(self, path, extra=''):
        return ('## Scored work\ntest_set: ' + str(path) + '\ntest_set_sha256: ' + sha(path) +
                '\nthreshold: 0.8\ncap: 20\nscorer: score.py\n' + extra + '\n')

    def evidence(self, r, kind, data, ok=True):
        path = self.tmp / (kind + '.json')
        path.write_text(json.dumps(data))
        return self.b(r, 'evidence', 'a', '--branch', 'job', '--kind', kind, '--file', str(path), ok=ok)

    def receipt(self, r, wt, kind, **fields):
        m = self.core(r)
        row = m.open_job(m.read_jobs(m.Instance(r).node('a')), 'job')
        return {'version': 1, 'kind': kind, 'node': 'a', 'branch': 'job', 'job_key': m.job_key(row),
                'head': self.head(wt), **fields}

    def outputs(self, test_set):
        lines = [line for line in test_set.read_text().splitlines() if line.strip()]
        return [{'input': line, 'output': 'output for ' + line} for line in lines]

    def real_result(self, r, wt, test_set, score, run=None, **fields):
        head = self.head(wt)
        record = {'head': head, 'test_set_sha256': sha(test_set), 'items': 10, 'score': score,
                  'outputs': [{'input_sha256': hashlib.sha256(o['input'].encode()).hexdigest(),
                               'output_sha256': hashlib.sha256(o['output'].encode()).hexdigest()}
                              for o in self.outputs(test_set)],
                  'scorer': {'path': 'score.py', 'blob': self.git('rev-parse', 'HEAD:score.py', cwd=wt).strip()}}
        record.update(run or {})
        path = self.tmp / ('real-run-' + head[:12] + '-' + str(score) + '.json')
        path.write_text(json.dumps(record))
        data = self.receipt(r, wt, 'real_result', test_set_sha256=sha(test_set), items=10, score=score,
                            threshold=0.8, test_set_check={'reference_score': 1.0, 'method': 'gold answers scored'},
                            run_ref=str(path), run_sha256=sha(path))
        data.update(fields)
        return data

    def review(self, r, ok=True):
        return self.b(r, 'review', 'a', '--branch', 'job', '--built-by', 'claude', ok=ok)

    # R60: real result before review

    def test_scored_review_needs_passing_capped_real_result_and_zero_escalates(self):
        r, home, wt = self.job()
        test_set = self.fixed_set()
        self.assertIn('test_set_sha256 does not match', self.contract(
            r, self.scored(test_set).replace(sha(test_set), '0' * 64), ok=False))
        self.contract(r, self.scored(test_set))
        self.assertIn('no review before a real result', self.review(r, ok=False))
        self.assertIn('cannot reach its own threshold', self.evidence(r, 'real-result', self.real_result(
            r, wt, test_set, 0.9, test_set_check={'reference_score': 0.5, 'method': 'gold answers scored'}), ok=False))
        self.assertIn('contract cap', self.evidence(r, 'real-result', self.real_result(r, wt, test_set, 0.9, items=21),
                                                    ok=False))
        # A receipt must cite the stored run that produced its number, at this head.
        self.assertIn('stored run output file', self.evidence(r, 'real-result', self.real_result(
            r, wt, test_set, 0.9, run_ref=str(self.tmp / 'no-such-run.json')), ok=False))
        self.assertIn('same items and score', self.evidence(r, 'real-result', self.real_result(
            r, wt, test_set, 0.9, run={'score': 0.1}), ok=False))
        self.assertIn('same items and score', self.evidence(r, 'real-result', self.real_result(
            r, wt, test_set, 0.9, run={'head': '0' * 40}), ok=False))
        self.assertIn('no review before a real result', self.review(r, ok=False))
        self.evidence(r, 'real-result', self.real_result(r, wt, test_set, 0))
        self.assertIn('zero real score for job', (r / 'nodes/a/inbox.md').read_text())
        self.assertIn('real score is zero', self.review(r, ok=False))
        self.commit(wt, 'partial fix')
        self.evidence(r, 'real-result', self.real_result(r, wt, test_set, 0.5))
        self.assertIn('below the threshold 0.8', self.review(r, ok=False))
        self.commit(wt, 'fix')
        self.evidence(r, 'real-result', self.real_result(r, wt, test_set, 0.85))
        self.assertIn('Review round 1 (full)', self.review(r))

    # R61: models across providers

    def selection(self, r, wt, test_set, providers, run=None, drop=()):
        candidates, blob = [], self.git('rev-parse', 'HEAD:score.py', cwd=wt).strip()
        for i, (provider, model) in enumerate(providers):
            measured = {'score': 0.8 + i / 100, 'cost_usd': 0.1, 'latency_s': 2.0}
            record = {'provider': provider, 'model': model, 'items': 10, 'test_set_sha256': sha(test_set),
                      'scorer': {'path': 'score.py', 'blob': blob}, **measured, **(run or {})}
            for key in drop:
                record.pop(key)
            path = self.tmp / ('run-' + str(len(list(self.tmp.glob('run-*.json')))) + '.json')
            path.write_text(json.dumps(record))
            candidates.append({'provider': provider, 'model': model, 'run_ref': str(path), 'run_sha256': sha(path),
                               **measured})
        return self.receipt(r, wt, 'model_selection', test_set_sha256=sha(test_set), items=10, data_class='public',
                            candidates=candidates, chosen={'provider': providers[0][0], 'model': providers[0][1]})

    def test_model_choice_needs_same_set_measurements_across_providers(self):
        r, home, wt = self.job()
        test_set = self.fixed_set()
        self.assertIn('model_choice needs data_class', self.contract(r, self.scored(test_set, 'model_choice: yes'),
                                                                     ok=False))
        self.contract(r, self.scored(test_set, 'model_choice: yes\ndata_class: public'))
        self.evidence(r, 'real-result', self.real_result(r, wt, test_set, 0.9))
        self.assertIn('measured across providers on the test set first', self.review(r, ok=False))
        one = self.selection(r, wt, test_set, [('vendor-a', 'alpha-1'), ('vendor-a', 'alpha-2')])
        self.assertIn('1 provider(s) measured, 2 required', self.evidence(r, 'model-selection', one, ok=False))
        forged = self.selection(r, wt, test_set, [('vendor-a', 'alpha-1'), ('vendor-b', 'beta-1')])
        forged['candidates'][1]['model'] = 'beta-2'
        self.assertIn('must match its own run receipt', self.evidence(r, 'model-selection', forged, ok=False))
        good = self.selection(r, wt, test_set, [('vendor-a', 'alpha-1'), ('vendor-b', 'beta-1')])
        self.evidence(r, 'model-selection', good)
        self.assertIn('Review round 1 (full)', self.review(r))
        self.contract(r, self.scored(test_set, 'model_choice: yes\ndata_class: restricted'))
        self.assertIn('never leave', self.evidence(r, 'model-selection', {**good, 'data_class': 'restricted'},
                                                   ok=False))

    def test_candidate_metrics_and_scorer_are_the_ones_each_stored_run_measured(self):
        r, home, wt = self.job()
        test_set = self.fixed_set()
        self.contract(r, self.scored(test_set, 'model_choice: yes\ndata_class: public'))
        self.evidence(r, 'real-result', self.real_result(r, wt, test_set, 0.9))
        pairs = [('vendor-a', 'alpha-1'), ('vendor-b', 'beta-1')]
        # Hashed runs that measured something else, or nothing, cannot carry the supplied numbers.
        invented = self.selection(r, wt, test_set, pairs, run={'score': 0.1, 'cost_usd': 20.0, 'latency_s': 60.0})
        self.assertIn('the ones its run receipt measured', self.evidence(r, 'model-selection', invented, ok=False))
        unmeasured = self.selection(r, wt, test_set, pairs, drop=('score', 'cost_usd', 'latency_s'))
        self.assertIn('the ones its run receipt measured', self.evidence(r, 'model-selection', unmeasured, ok=False))
        other = self.selection(r, wt, test_set, pairs, run={'scorer': {'path': 'score.py', 'blob': '0' * 40}})
        self.assertIn('contract scorer with its blob', self.evidence(r, 'model-selection', other, ok=False))
        self.evidence(r, 'model-selection', self.selection(r, wt, test_set, pairs))

    # R66: read a number before reporting it

    def dissected(self, extra=''):
        path = self.tmp / 'report.md'
        path.write_text(extra + '## Dissection\nCause: Synthetic shortfall.\nItems covered: 3\n### Cases\n'
                        '1. Ref: case-a; One.\n2. Ref: case-b; Two.\n3. Ref: case-c; Three.\n'
                        '### Fixes\n1. Repair.\n2. Retain.\nRecommendation: 1\n')
        return path

    def quality(self, test_set, wrong=None):
        source = self.tmp / 'source-document.txt'
        source.write_text('Invented source document the items were read against.\n')
        item = hashlib.sha256(test_set.read_text().splitlines()[0].encode()).hexdigest()
        return ('## What was removed / kept\n### Right\n1. Ref: ' + item + '; Source: ' + str(source) + '@' +
                sha(source) + '; Removed: duplicate of another item\n### Wrong\n' +
                (wrong or 'None found: read all removed items against the set') + '\n\n')

    def artifact(self, name, body):
        path = self.tmp / name
        path.write_text(body)
        return {'ref': str(path), 'sha256': sha(path)}

    def member_cases(self, test_set, output=None):
        cases, prefix = [], 'forged-' if output else 'member-'
        for i, item in enumerate(self.outputs(test_set)):
            given = self.artifact(prefix + 'in-' + str(i), item['input'])
            got = self.artifact(prefix + 'out-' + str(i), output(i) if output else item['output'])
            cases.append({'input_ref': given['ref'], 'input_sha256': given['sha256'], 'output_ref': got['ref'],
                          'output_sha256': got['sha256'], 'check': 'exact match'})
        return cases

    def score_read_fixture(self):
        r, home, wt = self.job()
        test_set = self.fixed_set()
        self.contract(r, self.scored(test_set))
        run = self.real_result(r, wt, test_set, 0.5)
        self.evidence(r, 'real-result', run)
        blob = self.git('rev-parse', 'HEAD:score.py', cwd=wt).strip()
        read = self.receipt(r, wt, 'score_read', scorer={'path': 'score.py', 'blob': blob},
                            cases=self.member_cases(test_set), run_ref=run['run_ref'], run_sha256=run['run_sha256'],
                            measures='exact string equality per item', does_not_measure='near-miss spellings')
        return r, test_set, read

    def test_score_read_cases_are_members_of_the_frozen_set_and_bound_run(self):
        r, test_set, read = self.score_read_fixture()
        # Three hashed files that are not items of the frozen set prove nothing about it.
        strangers = []
        for i in range(3):
            given, got = self.artifact('stranger-in-' + str(i), 'unrelated ' + str(i)), self.artifact('o' + str(i), 'x')
            strangers.append({'input_ref': given['ref'], 'input_sha256': given['sha256'], 'output_ref': got['ref'],
                              'output_sha256': got['sha256'], 'check': 'exact match'})
        self.assertIn('item of the frozen test set', self.evidence(r, 'score-read', {**read, 'cases': strangers},
                                                                   ok=False))
        # Real items, but outputs the bound run never produced.
        forged = self.member_cases(test_set, output=lambda i: 'invented better output ' + str(i))
        self.assertIn('the one the bound run recorded', self.evidence(r, 'score-read', {**read, 'cases': forged},
                                                                       ok=False))
        self.evidence(r, 'score-read', read)

    def test_relaunch_judge_egress_covers_task_file_data_class_and_retention(self):
        r, home, wt = self.job()
        self.commit(wt, 'earlier attempt', {'calc.py': 'value = 2\n'})
        task = 'Finish the calc change. The earlier builder found the sole attempt spent: do not retry the migration.'
        verdict = self.tmp / 'verdict.json'
        verdict.write_text(json.dumps({'restricts': True, 'authority': 'inherited', 'quote': 'do not retry the migration'}))
        self.env['BRAIN_JUDGE_FIXTURE'] = str(verdict)
        self.b(r, 'set', 'a', 'ai_judge_egress=allowed')
        inline = ('brief', 'a', '--branch', 'job', '--task', task, '--allow-unexplained')
        self.assertIn('Inherited-text check not run', self.b(r, *inline))  # unclassified: fail closed
        self.b(r, 'set', 'a', 'data_class=restricted')
        self.assertIn('Inherited-text check not run', self.b(r, *inline))
        self.b(r, 'set', 'a', 'data_class=internal')
        self.assertIn('Inherited-text check not run', self.b(r, *inline))  # retention unconfirmed
        self.b(r, 'set', 'a', 'ai_judge_retention=zero')
        self.assertIn('context, not proof', self.b(r, *inline, ok=False))
        self.b(r, 'set', 'a', 'data_class=public')
        outside = self.tmp / 'private-task.md'
        outside.write_text(task + '\n')
        from_file = ('brief', 'a', '--branch', 'job', '--task-file', str(outside), '--allow-unexplained')
        self.assertIn('Inherited-text check not run', self.b(r, *from_file))
        inside = home / 'task.md'
        inside.write_text(task + '\n')
        self.assertIn('context, not proof', self.b(r, 'brief', 'a', '--branch', 'job', '--task-file', str(inside),
                                                   '--allow-unexplained', ok=False))

    def test_scored_handback_reports_a_number_only_after_scorer_and_three_cases_are_read(self):
        r, home, wt = self.job()
        test_set = self.fixed_set()
        self.contract(r, self.scored(test_set))
        handback = ('handback', 'a', '--branch', 'job', '--state', 'blocked', '--summary', 'below target on real data',
                    '--result', '0.5', '--target', '0.8', '--report', str(self.dissected(self.quality(test_set))))
        self.assertIn('only after reading its scorer and 3 real cases', self.b(r, *handback, ok=False))
        run = self.real_result(r, wt, test_set, 0.5)
        self.evidence(r, 'real-result', run)
        blob = self.git('rev-parse', 'HEAD:score.py', cwd=wt).strip()
        cases = self.member_cases(test_set)
        read = self.receipt(r, wt, 'score_read', scorer={'path': 'score.py', 'blob': blob}, cases=cases,
                            run_ref=run['run_ref'], run_sha256=run['run_sha256'],
                            measures='exact string equality per item', does_not_measure='near-miss spellings')
        self.assertIn('exactly 3 real cases', self.evidence(r, 'score-read', {**read, 'cases': cases[:2]}, ok=False))
        self.assertIn('its blob at this head', self.evidence(
            r, 'score-read', {**read, 'scorer': {'path': 'score.py', 'blob': '0' * 40}}, ok=False))
        # Case references must be stored artifacts, not labels.
        unbound = [{**case, 'input_ref': 'set:' + str(i)} for i, case in enumerate(cases)]
        self.assertIn('input_ref must be a stored file', self.evidence(r, 'score-read', {**read, 'cases': unbound},
                                                                       ok=False))
        self.assertIn('cites the run behind', self.evidence(r, 'score-read', {**read, 'run_sha256': '0' * 64}, ok=False))
        self.evidence(r, 'score-read', read)
        self.assertNotIn('only after reading its scorer', self.b(r, *handback))

    def test_reported_result_is_the_score_its_score_read_covers(self):
        r, test_set, read = self.score_read_fixture()  # the real result and its read are 0.5
        self.evidence(r, 'score-read', read)
        report = self.dissected(self.quality(test_set))

        def handback(value):
            return ('handback', 'a', '--branch', 'job', '--state', 'blocked', '--summary', 'below target on real data',
                    '--result', value, '--target', '0.8', '--report', str(report))
        self.assertIn('is not the score the score read covers', self.b(r, *handback('0.99'), ok=False))
        self.assertNotIn('score read covers', self.b(r, *handback('0.5')))

    # R69: quality before counts

    def test_scored_done_handback_opens_with_source_judged_removed_and_kept_examples(self):
        r, home, wt = self.job()
        test_set = self.fixed_set()
        self.contract(r, self.scored(test_set))
        done = ('handback', 'a', '--branch', 'job', '--state', 'done', '--summary', 'filter run finished',
                '--reviews', '1', '--lessons', 'none: fixture')
        counts_first = self.tmp / 'counts.md'
        counts_first.write_text('## Totals\nDropped 228 of 300.\n\n## What was removed / kept\n### Right\n'
                                '1. Ref: item-4; Source: set:4; Removed: duplicate of item-3\n'
                                '### Wrong\nNone found: read all 12 removed items against the set\n')
        self.assertIn('before any totals', self.b(r, *done, '--report', str(counts_first), ok=False))
        preamble = self.tmp / 'preamble.md'
        preamble.write_text('Dropped 228 of 300.\n\n' + self.quality(test_set))
        self.assertIn('before any totals', self.b(r, *done, '--report', str(preamble), ok=False))
        # A blocked or failed result is still a result: it also leads with quality.
        for state in ('blocked', 'failed'):
            shortfall = ('handback', 'a', '--branch', 'job', '--state', state, '--summary', 'below target',
                         '--result', '0.5', '--target', '0.8', '--report', str(self.dissected('Dropped 228 of 300.\n')))
            self.assertIn('before any totals', self.b(r, *shortfall, ok=False))
        missing = self.tmp / 'missing.md'
        missing.write_text('## What was removed / kept\n### Right\nSeveral were fine.\n')
        out = self.b(r, *done, '--report', str(missing), ok=False)
        self.assertIn('### Right needs numbered examples', out)
        self.assertIn('### Wrong needs numbered examples', out)
        self.evidence(r, 'real-result', self.real_result(r, wt, test_set, 0.9))
        good = self.tmp / 'good.md'
        quality = self.quality(test_set, wrong='1. Ref: ' + hashlib.sha256(
            test_set.read_text().splitlines()[1].encode()).hexdigest() + '; Source: ' + str(self.tmp / 'source-document.txt') +
            '@' + sha(self.tmp / 'source-document.txt') + '; Removed: a pronoun fragment; name the subject instead')
        good.write_text('# Filter run\n\n' + quality + '## Totals\nDropped 2 of 10.\n')
        self.assertNotIn('What was removed / kept', self.b(r, *done, '--report', str(good), ok=None))

    def test_quality_examples_cite_items_of_the_bound_run_and_stored_sources(self):
        r, home, wt = self.job()
        test_set = self.fixed_set()
        self.contract(r, self.scored(test_set))
        done = ('handback', 'a', '--branch', 'job', '--state', 'done', '--summary', 'filter run finished',
                '--reviews', '1', '--lessons', 'none: fixture')
        labels = self.tmp / 'labels.md'
        labels.write_text('## What was removed / kept\n### Right\n'
                          '1. Ref: nonexistent-item; Source: nonexistent-source; Kept: reads fine\n'
                          '### Wrong\nNone found: read all items against the set\n')
        self.assertIn("this head's real result", self.b(r, *done, '--report', str(labels), ok=False))
        self.evidence(r, 'real-result', self.real_result(r, wt, test_set, 0.9))
        out = self.b(r, *done, '--report', str(labels), ok=False)
        self.assertIn('is not an item the bound real run recorded', out)
        self.assertIn('is not a stored file with its sha256', out)
        good = self.tmp / 'bound.md'
        good.write_text(self.quality(test_set))
        stranger = hashlib.sha256(b'{"input": "not in the set"}').hexdigest()
        unrecorded = self.tmp / 'unrecorded.md'
        unrecorded.write_text(good.read_text().replace(good.read_text().split('Ref: ')[1].split(';')[0], stranger))
        self.assertIn('is not an item the bound real run recorded', self.b(r, *done, '--report', str(unrecorded),
                                                                             ok=False))
        self.assertNotIn('bound real run', self.b(r, *done, '--report', str(good), ok=None))
        (self.tmp / 'source-document.txt').write_text('The source changed after it was cited.\n')
        self.assertIn('is not a stored file with its sha256', self.b(r, *done, '--report', str(good), ok=False))

    # R58: one task-local setup correction

    def test_one_source_proven_setup_correction_within_original_bounds(self):
        r, home, wt = self.job()
        self.contract(r)
        m = self.core(r)
        contract = m.contract_file(m.Instance(r).node('a'), m.open_job(m.read_jobs(m.Instance(r).node('a')), 'job'))
        correction = self.receipt(r, wt, 'setup_correction', contract_sha256=sha(contract), correction='selector',
                                  before='test_calc.py::test_values', after='test_calc.py::TestCalc::test_value',
                                  bounds='original')
        for wrong in ('test_calc.py::test_missing', 'test_calc.py::MissingClass::test_value',
                      'test_calc.py::test_value::test_value', 'calc.py::value'):
            self.assertIn('must exist in the source', self.evidence(
                r, 'setup-correction', {**correction, 'after': wrong}, ok=False))
        self.assertIn('original bounds', self.evidence(r, 'setup-correction', {**correction, 'bounds': 'doubled'},
                                                       ok=False))
        self.assertIn('selector or start_format fix only', self.evidence(
            r, 'setup-correction', {**correction, 'correction': 'assertion'}, ok=False))
        self.evidence(r, 'setup-correction', correction)
        self.assertIn('one setup correction for this job is spent', self.evidence(
            r, 'setup-correction', {**correction, 'before': 'test_calc.py::test_value', 'after': 'test_calc.py'},
            ok=False))
        self.b(r, 'wt', 'add', 'a', 'job2')
        wt2 = self.tmp / 'app-work/worktrees/job2'
        path = self.tmp / 'contract2.md'
        path.write_text(SECTIONS + 'setup_corrections: 0\n')
        self.b(r, 'jobs', 'a', '--branch', 'job2', '--contract-file', str(path))
        row = m.open_job(m.read_jobs(m.Instance(r).node('a')), 'job2')
        strict = {**correction, 'branch': 'job2', 'job_key': m.job_key(row), 'head': self.head(wt2),
                  'contract_sha256': sha(m.contract_file(m.Instance(r).node('a'), row))}
        source = self.tmp / 'strict.json'
        source.write_text(json.dumps(strict))
        self.assertIn('allows no setup correction', self.b(r, 'evidence', 'a', '--branch', 'job2', '--kind',
                                                           'setup-correction', '--file', str(source), ok=False))

    # R54: contract reading list

    def test_contract_reading_list_names_exact_ranges_and_proven_references(self):
        r, home, wt = self.job()
        self.contract(r)  # optional by default
        self.b(r, 'set', 'a', 'contract_reading_list=required')
        out = self.contract(r, ok=False)
        self.assertIn('## Reading list', out)
        self.assertIn('## Proven', out)
        self.assertIn('range is outside calc.py', self.contract(
            r, '## Reading list\n- calc.py:1-9\n\n## Proven\n- value exists; Ref: calc.py\n', ok=False))
        self.assertIn('## Proven', self.contract(r, '## Reading list\n- calc.py:1-1\n\n## Proven\n- value exists\n',
                                                 ok=False))
        self.contract(r, '## Reading list\n- calc.py:1-1 the value\n\n## Proven\n- value exists; Ref: calc.py\n')
        self.assertIn('## Reading list', self.review(r))

    # R53: a hand-back is context, not proof

    def test_relaunch_brief_refuses_inherited_no_retry_clause_and_keeps_operator_limits(self):
        r, home, wt = self.job()
        self.commit(wt, 'earlier attempt', {'calc.py': 'value = 2\n'})
        task = 'Finish the calc change. The earlier builder found the sole attempt spent: do not retry the migration.'
        verdict = self.tmp / 'verdict.json'
        brief = ('brief', 'a', '--branch', 'job', '--task', task, '--allow-unexplained')
        verdict.write_text(json.dumps({'restricts': True, 'authority': 'inherited',
                                       'quote': 'the sole attempt spent: do not retry the migration'}))
        self.env['BRAIN_JUDGE_FIXTURE'] = str(verdict)
        # Without explicit egress authority the brief text never reaches the judge.
        self.assertIn('Inherited-text check not run', self.b(r, *brief))
        self.b(r, 'set', 'a', 'ai_judge_egress=allowed')
        self.b(r, 'set', 'a', 'data_class=public')
        self.assertIn('context, not proof', self.b(r, *brief, ok=False))
        private = self.tmp / 'private-trace.md'
        private.write_text('Earlier transcript excerpt.\n')
        self.assertIn('Inherited-text check not run', self.b(r, *brief, '--context-file', str(private)))
        m = self.core(r)
        policy = importlib.import_module(m.read_jobs.__module__.rsplit('.', 1)[0] + '.evidence_policy')
        def leaked(data):
            raise AssertionError('sealed brief text reached the judge')
        with mock.patch.dict(os.environ, self.env, clear=True):
            inst = m.Instance(r)
            sealed = {**inst.node('a'), 'sealed': True}
            self.assertEqual(policy.relaunch_problems(inst, sealed, task, [], judge=leaked)[1], [policy.NO_EGRESS_LINE])
        verdict.write_text(json.dumps({'restricts': True, 'authority': 'operator', 'quote': 'do not retry the migration'}))
        out = self.b(r, *brief)
        self.assertIn('Inherited conclusions are context, not proof', out)
        verdict.write_text(json.dumps({'restricts': True, 'authority': 'inherited', 'quote': 'not in the brief'}))
        self.b(r, *brief)  # a quote that is not in the text is not evidence
        self.env.pop('BRAIN_JUDGE_FIXTURE')
        self.b(r, *brief)  # no judge while testing: fail open

    # R12: UI matches its reference

    def design(self, r, wt, reference, build, **fields):
        data = self.receipt(r, wt, 'design_check',
                            screens=[{'screen': 'Home', 'reference': {'path': str(reference), 'sha256': sha(reference)},
                                      'build': {'path': str(build), 'sha256': sha(build)}}],
                            checklist={k: {'verdict': 'pass', 'note': 'compared side by side'} for k in CHECKLIST},
                            checker={'threadId': 'fixture-checker', 'runtime': 'codex', 'model': 'fixture-model'})
        data.update(fields)
        return data

    def ready(self, r, wt):
        m = self.core(r)
        verify = importlib.import_module(m.read_jobs.__module__.rsplit('.', 1)[0] + '.verify')
        with mock.patch.dict(os.environ, self.env, clear=True):
            inst = m.Instance(r)
            return verify.ready_policy_reasons(inst, inst.node('a'), 'job', self.head(wt), preview=False)

    def test_every_touched_screen_requires_paired_screenshots_and_independent_checklist(self):
        r, home, wt = self.job()
        reference, build = self.tmp / 'home-reference.png', self.tmp / 'home-build.png'
        reference.write_bytes(b'invented reference pixels')
        build.write_bytes(b'invented build pixels')
        self.assertIn('not a readable file', self.contract(r, '## Design reference\n- Home: missing.png\n', ok=False))
        self.contract(r, '## Design reference\n- Home: ' + str(reference) + '\n')
        self.assertIn('design reference needs reference/build screenshots', '; '.join(self.ready(r, wt)))
        self.assertIn('different runtime than the builder', self.evidence(r, 'design-check', self.design(
            r, wt, reference, build, checker={'threadId': 'fixture-checker', 'runtime': 'claude', 'model': 'm'}),
            ok=False))
        self.assertIn('differ from the build', self.evidence(r, 'design-check', self.design(r, wt, reference, reference),
                                                             ok=False))
        self.assertIn('every touched screen', self.evidence(r, 'design-check', self.design(r, wt, reference, build,
                                                                                            screens=[]), ok=False))
        rows = dict(self.design(r, wt, reference, build)['checklist'])
        rows.pop('links')
        self.assertIn('design checklist rates', self.evidence(r, 'design-check', self.design(
            r, wt, reference, build, checklist=rows), ok=False))
        self.evidence(r, 'design-check', self.design(r, wt, reference, build))
        self.assertFalse([reason for reason in self.ready(r, wt) if 'design' in reason])
        self.commit(wt, 'restyle')
        failed = self.design(r, wt, reference, build)
        failed['checklist']['spacing'] = {'verdict': 'fail', 'note': 'card padding 12px, reference 16px'}
        self.evidence(r, 'design-check', failed)
        self.assertIn('design checklist failed: spacing', '; '.join(self.ready(r, wt)))
