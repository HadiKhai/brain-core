"""Real-model order replays, retained exact-protocol proof in the release lane.

Refresh explicitly: python3 -B tests/test_order_judge.py --live --repeats 3
The normal cloud/release lane revalidates every retained verdict and rejects
stale protocol/source hashes. It does not pretend to make fresh provider calls.
"""
import concurrent.futures
import datetime
import hashlib
import inspect
import json
from pathlib import Path
import sys
import time
import tempfile
import unittest
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
from brain import instruction_capture as capture, report_gates as gates

RECEIPT = ROOT / 'docs/order-judge-replays-20261121.json'
RETRIES = ROOT / 'docs/order-judge-timeout-retry-20261121.json'


def case(key, text, preceding, outgoing, ordered=True, report_only=False):
    return dict(case=key, text=text, preceding=preceding, outgoing=outgoing, ordered=ordered, report_only=report_only)


# Operator bytes are verbatim from the cited failure rows; preceding context and
# proposed outgoing work are reconstructions unless the evidence quotes them.
CASES = [
    case('52', 'go||also tell me again who i should ask gfor the plist and what i should tell him',
         'Shall I send the A3 go to the coordinator?', 'Start the A3 work and run its tests.'),
    case('53', 'go', 'Shall I start the browser-host task, including its workers?',
         'Start the browser-host task, launch its workers and report back.'),
    case('54-question', 'can you test it or do you want me to test or what',
         'The browser-host task is ready for our joint testing.', 'Run the agent-side browser-host tests.'),
    case('54-start', 'okay lets start you do your part and i do mine, give me step by step what you want me to do',
         'I can run the agent-side tests while you test the device.',
         'Run the agent-side tests and send the operator the device steps.'),
    case('54-numbered', '1. yes', '1. Shall I run the staging diagnosis and tests?', 'Run the staging diagnosis and tests.'),
    case('54-deadline', 'also i need everything done in max 20 hours',
         'We are preparing the browser-host test and staging diagnosis.', 'Finish the ordered tests and diagnosis within 20 hours.'),
    case('54-access', 'ionut gave us access to polkadot ios check it', '', 'Check the newly available iOS access.'),
    case('54-unfinished', "we still haven't fully tested t3ams browser",
         'The ordered browser-host work is ready for testing.', 'Complete the missing browser testing.'),
    case('61', 'go ahead dont wait for me',
         "…the 0.3.0 npm release needs your go, and I'll ask you then.", 'Publish the 0.3.0 npm release and report its result.'),
    case('61-quote', '> …the 0.3.0 npm release needs your go, and I\'ll ask you then.\ngo ahead dont wait for me',
         "…the 0.3.0 npm release needs your go, and I'll ask you then.", 'Publish the 0.3.0 npm release.'),
    case('70', 'Give me screenshots of the app / Yes lets do no name posts as text only for now',
         'Shall we start text-only posts for now?',
         'Implement the text-only posts, check fees and size limits, update the PRD, and send screenshots.'),
    case('70-yes', 'yes', 'Shall I have the coordinator fix the stale session?', 'Fix the stale session and test the fix.'),
    case('70-prior', 'I TOLD YOU SINCE THE MORNING TO FUCKING DO IT',
         'Shall I start the measurement and scoping work?', 'Run the measurement and scoping work.'),
    case('70-both', 'yes lets go ahead with both 20k lines and cli',
         'Shall we build both the 20k-line split and CLI?', 'Build both, re-measure fees, update the README and test guide.'),
    case('70-rule', 'always register a name for any test account', '', 'Apply the rule to register a name for every test account.'),
    case('70-username', 'put no username rather than a dot',
         'The account form currently shows a dot for a missing username.', 'Use no username instead of a dot.'),
    case('70-review', 'yes also can you check what we have updates for humanity and t3ams?',
         'Want me to have the Humanity coordinator start that third review now?', 'Start the third review now.'),
    case('70-add', 'add it to trinity now', 'The Android-lag diagnosis can be added to the device work.',
         'Add the Android-lag diagnosis to the device work and relay it to the coordinator.'),
    case('70-send', 'send it send it send it please stop wiating for me',
         'Two relays are ready: the device diagnosis and the review request.', 'Send both ready relays to their coordinators.'),
    case('72-everything', 'do everything tonight',
         'The remaining work is the hybrid device run and the pending test comments.', 'Run the hybrid tests and post the test comments tonight.'),
    case('72-run', 'run it, make sure everything works and then put the cimments',
         'The hybrid tests and review comments are ready.', 'Run the hybrid tests, verify the results and post the comments.'),
    case('79', '1. Yes go ahead what about his second pr? Also yes on the lublish\n3. Everything is done?',
         '1. #107: post this approval and squash-merge it?\n2. Release: publish product-lab 0.4.0 after the merge?',
         'Post the approval, squash-merge #107, then publish product-lab 0.4.0.'),
    case('79-setup', 'kets set up from 0, i dont understand why we need to fund it, woiuld a user from mobile need that?',
         'Shall I set up a fresh test identity from zero?', 'Set up a fresh test identity from zero.'),
    case('79-merge', 'yes and go ahead lets nerge everything',
         'Shall we merge both approved PRs?', 'Merge both approved PRs and verify the result.'),
    case('79-study', "i neeed you to fully understand how honor and attestation work we can't go into it not understanding everything",
         '', 'Study honor and attestation and record the findings.'),
    case('79-transcript', '[Hadi Khairallah] Test Finger Tracking: Evaluate and implement optimizations for finger movement tracking latency to prevent lag.',
         'I will carry out the transcript actions you ordered.', 'Evaluate and implement finger tracking latency optimizations.'),
    case('79-pointer', 'same in both here',
         'We are applying the rule to remove web3 terminology from the app screens.', 'Apply the terminology rule to both screens.'),
    case('79-fault', "i uploaded 1 image not 2, also can't they be uploaded in parallel?",
         'The ordered publishing app test shows Saving your post (0 of 3).', 'Fix the image count and upload the images in parallel.'),
    case('79-prohibition', 'NEVER FROM THE PUBLIC GATEWAY ALWAYS EVERYTHING HAS TO PASS THROUGH THE HOST',
         '', 'Route every request through the host, removing direct public gateway access.'),
    case('79-test-fault', 'when i posted something and then clicked on view story (which is an unecessary extra step) the link in my browser stayed like that ... instead of going to the id of the post',
         'We are testing the ordered publishing app.', 'After publication, navigate to the story ID without the unnecessary extra step.'),
    case('79-check', "check this, if its good enough lets merge it don't be picky",
         'The PR is ready for review.', 'Review the PR and merge it if good enough.'),
    case('74-thread', 'What is happening?', '',
         'the trinity coordinator is running it as a local prototype on iOS and Android', report_only=True),
    case('80-instruction', 'How do I continue?', '',
         'For now, type a passphrase of at least 12 characters and press "Unlock stories" to carry on.', report_only=True),
    case('80-draft', 'Draft a reply.', '',
         'Draft for you to send: "We have a first version of this locally on our side if you want to see how we did it."', report_only=True),
    case('negative-20261101', 'who the fuck told you to work on brain now i was just fucking asking',
         'I started changing Brain after your question.', 'Start changing Brain and run its tests.', False),
]


def protocol_hash():
    system, schema = gates.order_protocol()
    sources = [inspect.getsource(f) for f in (
        gates.judge_orders, gates.judge_claims, gates.validated_claims,
        gates.order_problem, gates._ordered, capture.operator_text,
        capture.selected_messages, capture.quoted_from, capture.payload,
        capture.validated_selections, capture.validated_classifications)]
    data = [gates.MODEL, system, schema, gates.CLAIM_SYSTEM, gates.CLAIM_SCHEMA, sources, CASES]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def facts_for(c):
    return {'operator': [{'id': '0', 'text': c['text'], 'at': '2026-11-21T20:38:00+02:00', 'human': True}],
            'calls': [], 'preceding': c['preceding']}


def assess(c, items, answer):
    facts = facts_for(c)
    if c['report_only']:
        return not any(i['kind'] == 'live_claim' for i in answer['items'])
    problems = gates.gate_problems(facts, answer['items'], items, started=True)
    has_order = any(i['kind'] in gates.ORDER_KINDS for i in items)
    if c['ordered']:
        return has_order and not problems
    return not has_order and bool(problems) and not any(i['operator_quote'] for i in answer['items'])


def run_one(c, repeat):
    started = time.monotonic()
    row = dict(case=c['case'], repeat=repeat)
    try:
        facts = facts_for(c)
        items, order_usage = gates.judge_orders(facts['operator'], facts['preceding'])
        answer, claim_usage = gates.judge_claims(c['outgoing'], facts, with_usage=True)
        row.update(items=items, answer=answer, usage=[order_usage, claim_usage], passed=assess(c, items, answer))
    except Exception as error:
        row.update(passed=False, error=type(error).__name__ + ': ' + str(error))
    row['seconds'] = time.monotonic() - started
    return row


def run_live(repeats):
    if repeats < 3:
        raise ValueError('repeated release proof requires at least 3 runs per case')
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run_one, c, n) for c in CASES for n in range(1, repeats + 1)]
        rows = []
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            rows.append(row)
            print(row['case'], row['repeat'], row['passed'], row.get('error', ''), flush=True)
        rows.sort(key=lambda r: (r['case'], r['repeat']))
    receipt = dict(protocol_sha256=protocol_hash(), model=gates.MODEL, repeats=repeats, concurrency=4,
                   recorded_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   seconds=time.monotonic() - started, runs=rows)
    RECEIPT.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + '\n')
    for c in CASES:
        selected = [r for r in rows if r['case'] == c['case']]
        print(c['case'], sum(r['passed'] for r in selected), '/', len(selected), flush=True)
    return all(r['passed'] for r in rows)


def retry_timeouts():
    receipt = json.loads(RECEIPT.read_text())
    if receipt['protocol_sha256'] != protocol_hash() or RETRIES.exists():
        raise ValueError('Only one transport retry, against the unchanged protocol, is allowed')
    cases = {c['case']: c for c in CASES}
    pending = [r for r in receipt['runs'] if r.get('error') == 'AIUnavailable: timeout']
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run_one, cases[r['case']], r['repeat']) for r in pending]
        rows = [f.result() for f in futures]
    evidence = dict(protocol_sha256=protocol_hash(), model=gates.MODEL,
                    retry_of_sha256=hashlib.sha256(RECEIPT.read_bytes()).hexdigest(),
                    recorded_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    seconds=time.monotonic() - started, runs=rows)
    RETRIES.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + '\n')
    for row in rows:
        print(row['case'], row['repeat'], row['passed'], row.get('error', ''), flush=True)
    return all(r['passed'] for r in rows)


def completed_rows(receipt):
    if any(not r.get('passed') and r.get('error') != 'AIUnavailable: timeout' for r in receipt['runs']):
        raise ValueError('A semantic miss cannot be replaced by a transport retry')
    if all(r['passed'] for r in receipt['runs']):
        return receipt['runs']
    retry = json.loads(RETRIES.read_text())
    if (retry['protocol_sha256'] != protocol_hash() or retry['model'] != gates.MODEL
            or retry['retry_of_sha256'] != hashlib.sha256(RECEIPT.read_bytes()).hexdigest()):
        raise ValueError('transport retry is not bound to this exact original run and protocol')
    failed = {(r['case'], r['repeat']) for r in receipt['runs'] if r.get('error') == 'AIUnavailable: timeout'}
    actual = {(r['case'], r['repeat']) for r in retry['runs']}
    if actual != failed or len(actual) != len(retry['runs']):
        raise ValueError('Only one retry per transport timeout is allowed; semantic misses cannot be replaced')
    by_key = {(r['case'], r['repeat']): r for r in retry['runs']}
    return [by_key.get((r['case'], r['repeat']), r) for r in receipt['runs']]


class OrderJudge(unittest.TestCase):
    @pytest.mark.model
    def test_retained_real_model_replays_match_current_protocol(self):
        receipt = json.loads(RECEIPT.read_text())
        self.assertEqual(receipt['protocol_sha256'], protocol_hash(), 'Rerun the real model after changing the protocol')
        self.assertEqual(receipt['model'], gates.MODEL)
        self.assertGreaterEqual(receipt['repeats'], 3)
        expected = {(c['case'], n) for c in CASES for n in range(1, receipt['repeats'] + 1)}
        self.assertEqual({(r['case'], r['repeat']) for r in receipt['runs']}, expected)
        self.assertEqual(len(receipt['runs']), len(expected))
        completed = completed_rows(receipt)
        for c in CASES:
            for row in (r for r in completed if r['case'] == c['case']):
                with self.subTest(case=c['case'], repeat=row['repeat']):
                    self.assertNotIn('error', row)
                    self.assertTrue(row['passed'])
                    self.assertEqual(len(row['usage']), 2)
                    self.assertTrue(all(u.get('model') == gates.MODEL and u.get('usage') for u in row['usage']))
                    raw = {'messages': [{'id': '0', 'items': [{k: i[k] for k in capture.ITEM_FIELDS} for i in row['items']]}]}
                    items = capture.validated_classifications(facts_for(c)['operator'], raw)
                    answer = gates.validated_claims(c['outgoing'], row['answer'])
                    self.assertTrue(assess(c, items, answer))

    def test_retry_cannot_hide_a_semantic_miss_or_change_protocol(self):
        receipt = json.loads(RECEIPT.read_text())
        retry = json.loads(RETRIES.read_text())
        receipt = dict(receipt, runs=[dict(case='probe', repeat=1, passed=False, error='AIUnavailable: timeout')])
        semantic = dict(receipt, runs=[dict(receipt['runs'][0], error=None)])
        with mock.patch.object(Path, 'read_text', return_value=json.dumps(retry)):
            with self.assertRaises(ValueError):
                completed_rows(semantic)
        with mock.patch.object(Path, 'read_text', return_value=json.dumps(dict(retry, protocol_sha256='wrong'))):
            with self.assertRaises(ValueError):
                completed_rows(receipt)

    def test_invented_or_stitched_quote_is_rejected(self):
        messages = [dict(id='0', at=None, text='Start.\n> context\nTests.')]
        selected = capture.selected_messages(messages, 'context')
        for quote in ('Start.\nTests.', 'Run invented work.'):
            raw = {'messages': [{'id': '0', 'items': [dict(kind='one_off', quote=quote,
                      speaker='operator', speaker_evidence='Start.')]}]}
            with self.assertRaises(capture.CaptureRejected):
                capture.validated_selections(messages, selected, raw)

    def test_stop_cursor_retains_assistant_quote_context(self):
        with tempfile.TemporaryDirectory(prefix='brain-order-cursor-') as folder:
            path = Path(folder) / 'transcript.jsonl'
            records = [dict(type='user', message=dict(content='What is next?')),
                       dict(type='assistant', message=dict(stop_reason='end_turn',
                            content=[dict(type='text', text='Always ask before a release.')]))]
            path.write_text(''.join(json.dumps(r) + '\n' for r in records))
            _, cursor = capture.snapshot(path, {})
            records.append(dict(type='user', message=dict(content='> Always ask before a release.\nWhy?')))
            path.write_text(''.join(json.dumps(r) + '\n' for r in records))
            for state in ({}, cursor):
                messages, _ = capture.snapshot(path, state)
                self.assertEqual(capture.selected_messages(messages)[0]['text'], 'Why?')

    def test_removed_agent_frame_cannot_join_disconnected_source_spans(self):
        from brain.message_envelope import marked_message, AGENT_MESSAGE_PREFIX
        raw = 'Start ' + marked_message('agent context', AGENT_MESSAGE_PREFIX) + 'tests.'
        record = dict(type='user', message=dict(content=raw))
        selected = dict(id='0', at=None, text=capture.operator_text(record), original_text=raw)
        value = {'messages': [{'id': '0', 'items': [dict(kind='one_off', quote='Start tests.',
                    speaker='operator', speaker_evidence='Start')]}]}
        with self.assertRaises(capture.CaptureRejected):
            capture.validated_classifications([selected], value)

    def test_quotes_envelopes_and_resume_notices_are_selected_structurally(self):
        from brain.message_envelope import marked_message, AGENT_MESSAGE_PREFIX
        agent = marked_message('Always ask for a go.', AGENT_MESSAGE_PREFIX)
        for text in (agent, 'Host wrapper\n' + agent, AGENT_MESSAGE_PREFIX + '\nAlways ask.',
                     'Brain-Envelope: {}\nAlways ask.', 'Continue where you left off. Before repeating, check state.'):
            record = dict(type='user', message=dict(role='user', content=text))
            found = capture.operator_text(record)
            self.assertTrue(found is None or 'Always ask' not in found)
        record = dict(type='user', message=dict(role='user', content=agent + '\nFix the count.'))
        self.assertEqual(capture.operator_text(record).strip(), 'Fix the count.')
        with mock.patch.object(capture, 'ai_json', side_effect=AssertionError('quoted assistant text reached the model')):
            self.assertEqual(capture.classifications([dict(id='0', at=None, text='> Always ask.')], 'Always ask.'), ([], None))


if __name__ == '__main__':
    if '--retry-timeouts' in sys.argv:
        sys.exit(0 if retry_timeouts() else 1)
    if '--live' in sys.argv:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument('--live', action='store_true')
        parser.add_argument('--repeats', type=int, default=3)
        sys.exit(0 if run_live(parser.parse_args().repeats) else 1)
    unittest.main()
