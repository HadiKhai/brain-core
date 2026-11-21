#!/usr/bin/env python3
"""Exercise the worktree launcher on disposable instances with synthetic local transports."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

CORE = Path(__file__).resolve().parents[2]
BRAIN = CORE / 'bin/brain'


def proof(folder):
    home = folder / 'home'
    home.mkdir()
    root = folder / 'instance'
    env = {k: v for k, v in os.environ.items() if not k.startswith(('BRAIN_', 'CODEX_', 'CLAUDE_', 'T3CODE_'))
           and k != 'ANTHROPIC_API_KEY'}
    env.update(HOME=str(home), BRAIN_NO_REEXEC='1', GIT_AUTHOR_NAME='Synthetic proof',
               GIT_AUTHOR_EMAIL='proof@example.invalid', GIT_COMMITTER_NAME='Synthetic proof',
               GIT_COMMITTER_EMAIL='proof@example.invalid', BRAIN_LOCK_WAIT='0')

    def run(*args, data=None, extra=None, expected=0, show=True):
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(BRAIN), *args], cwd=root if root.exists() else folder,
                                env=dict(env, **(extra or {})), input=json.dumps(data) if data is not None else None,
                                capture_output=True, text=True, timeout=40)
        if show:
            print('$ worktree/bin/brain ' + ' '.join(args), flush=True)
            print('exit=' + str(result.returncode) + ' seconds=' + str(round(time.monotonic() - started, 3)) +
                  ' stdout=' + repr(result.stdout) + ' stderr=' + repr(result.stderr), flush=True)
        assert result.returncode == expected, result.stderr
        return result

    def git(*args):
        subprocess.run(['git', '-C', str(root), *args], env=env, capture_output=True, check=True)

    def commit(message):
        git('add', '.')
        git('commit', '-qm', message)

    run('init', str(root), '--operator', 'fixture-owner', '--title', 'Synthetic turn holds', '--tz', 'UTC', show=False)
    env['BRAIN_ROOT'] = str(root)
    run('changes', 'root', '--seen', show=False)

    # 1: real failed refresh and stop while a separate writer owns the lock.
    before = (root / 'status.md').stat().st_mtime
    (root / 'inbox.md').write_text('- 2026-01-01T00:00:00Z from worker: Fixture completed.\n')
    os.utime(root / 'inbox.md', (before + 1, before + 1))
    commit('fixture: committed records')
    with (root / '.brain/lock').open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        run('status', 'root', '--refresh', expected=1)
        stopped = run('hook', 'stop', data={'cwd': str(root), 'session_id': 'reader'})
        assert not stopped.stdout
    pending = next((root / '.brain/stop-refresh').glob('*.json'))
    print('contended stop retained:', pending.read_text().strip(), flush=True)
    run('now')  # the next quiet command starts the real bounded refresh
    deadline = time.monotonic() + 30
    while pending.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if pending.exists():
        print('refresh diagnostic:', pending.with_suffix('.log').read_text()
              if pending.with_suffix('.log').exists() else 'worker did not start', flush=True)
    assert not pending.exists(), 'quiet command did not finish the queued refresh'
    print('quiet-command refresh finished; stale mark cleared:',
          'Facts stale since:' not in (root / 'status.md').read_text(), flush=True)

    # Local CLI provider; no paid service, account token or network access.
    stub_dir = folder / 'stub-bin'
    stub_dir.mkdir()
    mode, calls = folder / 'provider-mode', folder / 'provider-calls'
    stub = stub_dir / 'claude'
    stub.write_text('#!' + sys.executable + '\n' + '''import json, pathlib, sys, time
data = json.loads(sys.stdin.read())
mode = pathlib.Path(MODE).read_text()
with pathlib.Path(CALLS).open('a') as stream: stream.write(mode + '\\n')
if mode == 'timeout': time.sleep(60)
if mode == 'invalid': answer = {'messages': []}
elif 'outgoing' in data:
    answer = {'items': [{'kind': 'work_order', 'quote': data['outgoing'], 'support': [], 'operator_quote': ''}],
              'prior_cancelled': False}
else:
    answer = {'messages': [{'id': m['id'], 'items': [{'kind': 'question', 'quote': m['text'],
        'speaker': 'operator', 'speaker_evidence': m['text']}]} for m in data['messages']]}
print(json.dumps({'subtype': 'success', 'is_error': False, 'structured_output': answer}))
'''.replace('MODE', repr(str(mode))).replace('CALLS', repr(str(calls))))
    stub.chmod(0o700)
    env['PATH'] = str(stub_dir) + os.pathsep + env['PATH']
    mode.write_text('question')
    run('set', 'root', 'coordinator=fixture-thread', 'coordinator_sessions=fixture-session', show=False)
    config = root / 'instance.md'
    config.write_text(config.read_text().replace('---\n', '---\nreport_gate_order_policy: warn\n', 1))
    commit('fixture: declared warning policy')
    transcript = folder / 'transcript.jsonl'
    transcript.write_text(json.dumps({'type': 'user', 'timestamp': '2026-01-01T12:00:00Z',
        'message': {'role': 'user', 'content': 'Could we continue the fixture?'}}) + '\n')
    data = {'cwd': str(root), 'session_id': 'fixture-session', 'transcript_path': str(transcript),
            'tool_name': 'mcp__t3_code__t3_thread_send',
            'tool_input': {'threadId': 'fixture-destination', 'message': 'Continue the fixture trial.'}}
    allowed = run('hook', 'pretool', data=data)
    result = json.loads(allowed.stdout)['hookSpecificOutput']
    assert 'permissionDecision' not in result
    # The fixture send sink receives exactly the input returned by the real hook.
    sink = folder / 'send-sink.json'
    sink.write_text(json.dumps(result['updatedInput']))
    print('synthetic send sink received:', json.loads(sink.read_text())['threadId'], flush=True)
    warning = [line for line in (root / 'inbox.md').read_text().splitlines() if 'order-gate-warning:' in line]
    assert len(warning) == 1
    print(warning[0], flush=True)

    # 3: actual wake command, disabled, accepted-without-run, and consumed.
    for name in ('disabled', 'unconfirmed', 'confirmed'):
        run('new', name, '--title', 'Synthetic wake ' + name, show=False)
        command = ('true' if name == 'disabled' else 'exit 0' if name == 'unconfirmed' else
                   'T3CODE_THREAD_ID=fixture-confirmed ' + str(BRAIN) + ' due confirmed >/dev/null')
        run('set', name, 'coordinator=fixture-' + name, 'wake=' + command, show=False)
        run('wake', name, 'synthetic-proof-' + name, expected=0 if name == 'confirmed' else 2)

    # 4: the real stop hook, two actual transport timeouts inside the adapter bound.
    transcript.write_text(json.dumps({'type': 'user', 'timestamp': '2026-01-01T12:30:00Z',
        'message': {'role': 'user', 'content': 'Preserve every original fixture source.'}}) + '\n')
    mode.write_text('timeout')
    start_count = len(calls.read_text().splitlines())
    stopped = run('hook', 'stop', data={'cwd': str(root), 'session_id': 'fixture-session',
                  'transcript_path': str(transcript), 'last_assistant_message': 'relay only'})
    assert not stopped.stdout
    attempts = len(calls.read_text().splitlines()) - start_count
    print('capture provider attempts:', attempts, flush=True)
    assert attempts == 2
    gaps = [line for line in (root / 'inbox.md').read_text().splitlines() if 'instruction-capture-gap:' in line]
    assert len(gaps) == 1
    print(gaps[0], flush=True)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='brain-turn-holds-proof-') as scratch:
        proof(Path(scratch))
    print('scratch instance and synthetic transports removed; provider spend $0', flush=True)
