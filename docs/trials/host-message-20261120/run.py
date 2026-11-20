"""Run actual messaging CLIs and hook entrypoints in an owned disposable instance.

No host send, installed-core change, model call or private input is involved.
Run from any folder: python3 -B /absolute/path/to/this/run.py
"""
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo


HERE = Path(__file__).resolve().parent
CORE = HERE.parents[2]
sys.path.insert(0, str(CORE / 'lib'))
from brain.message_envelope import AGENT_MESSAGE_PREFIX, fully_marked, marked_message, unmarked_message


def metadata(message):
    return json.loads(message.split('\n\n')[1].removeprefix('Brain-Envelope: '))


def main():
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='fixture-', dir=HERE) as directory:
        root = Path(directory)
        (root / 'instance.md').write_text(
            '---\nname: fixture\noperator: fixture\nprincipals: fixture\n'
            'runtimes: codex\nhost: t3\ntimezone: Asia/Kathmandu\n---\n')
        (root / 'node.md').write_text(
            '---\nid: root\ntitle: Fixture\nowners: fixture\nhome: here\n---\n')
        (root / 'inbox.md').write_text('# Fixture inbox\n')
        (root / 'config.json').write_text(json.dumps({
            'default': 'fixture', 'brains': {'fixture': {'root': str(root)}}}))
        env = {**os.environ, 'BRAIN_ROOT': str(root), 'BRAIN_NO_REEXEC': '1',
               'BRAIN_CONFIG': str(root / 'config.json'), 'PYTHONDONTWRITEBYTECODE': '1'}
        def cli(args, payload=None):
            result = subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'), *args],
                                    cwd=root, env=env, input=payload, capture_output=True)
            if result.returncode:
                raise RuntimeError(result.stderr.decode())
            return result.stdout.decode('utf-8')
        body = '23:42 typed label\r\n  Exact café evidence.  '
        (root / 'message.txt').write_bytes(body.encode())
        commands = {
            'agent-message': ['agent-message', '--message-file', str(root / 'message.txt')],
            'msg': ['msg', 'root', 'question', '23:42 typed label stays', '--branch', 'fixture'],
            'notify': ['notify', 'root', 'fyi', '23:42 typed label stays', '--key', 'fixture-stamp'],
        }
        before = datetime.datetime.now(ZoneInfo('Asia/Kathmandu')).replace(microsecond=0)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(commands)) as pool:
            results = dict(zip(commands, pool.map(cli, commands.values())))
        after = datetime.datetime.now(ZoneInfo('Asia/Kathmandu'))
        stamps = {}
        for name in ('agent-message', 'msg'):
            stamp = metadata(results[name])['sent_at']
            actual = datetime.datetime.fromisoformat(stamp)
            assert before <= actual <= after
            assert actual.utcoffset() == before.utcoffset()
            stamps[name] = stamp
        prepared = results['agent-message'][:-1]
        assert unmarked_message(prepared, AGENT_MESSAGE_PREFIX) == body
        assert not fully_marked(prepared + '\nOperator remainder')
        receipt = next(iter(json.loads((root / '.brain/communications.json').read_text())['notices'].values()))
        assert '- ' + receipt['at'] + ' from notification:' in (root / 'inbox.md').read_text()
        assert receipt['text'] == '23:42 typed label stays'

        def hook(tool, args):
            payload = json.dumps({'cwd': str(root), 'session_id': 'fixture-coordinator',
                                  'tool_name': tool, 'tool_input': args}).encode()
            return json.loads(cli(['hook', 'pretool'], payload))['hookSpecificOutput']
        args = {'threadId': 'fixture-child', 'mode': 'queue',
                'message': marked_message(body, AGENT_MESSAGE_PREFIX, sent_at='2000-01-01T00:00:00+00:00'),
                'clientRequestId': 'fixture-stable'}
        hook_before = datetime.datetime.now(ZoneInfo('Asia/Kathmandu')).replace(microsecond=0)
        output = hook('mcp__t3_code__t3_thread_send', args)
        hook_after = datetime.datetime.now(ZoneInfo('Asia/Kathmandu'))
        rewritten = output['updatedInput']
        hook_stamp = metadata(rewritten['message'])['sent_at']
        assert hook_before <= datetime.datetime.fromisoformat(hook_stamp) <= hook_after
        assert 'permissionDecision' not in output
        assert unmarked_message(rewritten['message'], AGENT_MESSAGE_PREFIX) == body
        assert {k: v for k, v in rewritten.items() if k != 'message'} == {
            k: v for k, v in args.items() if k != 'message'}
        (root / 'AGENTS.md').write_text('## Forbidden actions\n- tool: *t3_thread_send\n')
        denied = hook('mcp__t3_code__t3_thread_send', args)
        assert denied['permissionDecision'] == 'deny'
        assert denied['permissionDecisionReason'].startswith('brain-hook:pretool: ')
        assert 'updatedInput' not in denied
        print(json.dumps({
            'clock_window': [before.isoformat(), after.isoformat()], 'formatter_stamps': stamps,
            'notification_at': receipt['at'], 'hook_stamp': hook_stamp,
            'hook_clock_window': [hook_before.isoformat(), hook_after.isoformat()],
            'denial': denied, 'body_bytes_preserved': True, 'other_arguments_preserved': True,
            'operator_remainder_not_hidden': True, 'permission_not_granted': True,
            'concurrent_commands': len(commands), 'elapsed_s': round(time.monotonic() - started, 6),
        }, indent=2))


if __name__ == '__main__':
    main()
