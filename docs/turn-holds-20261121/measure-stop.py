#!/usr/bin/env python3
"""Measure both real stop launchers on one restored scratch instance; no provider calls."""
import argparse
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

CORE = Path(__file__).resolve().parents[2]
BASE = '6c716deda35684ee186fcc6c7e75b54c5b7b087a'


def measure(samples):
    head = subprocess.check_output(['git', '-C', str(CORE), 'rev-parse', 'HEAD'], text=True).strip()
    source_trees = {key: subprocess.check_output(
        ['git', '-C', str(CORE), 'rev-parse', head + ':' + key], text=True).strip()
        for key in ('lib', 'bin', 'adapters')}
    assert not subprocess.check_output(['git', '-C', str(CORE), 'diff', 'HEAD', '--',
                                       'lib', 'bin', 'adapters']), 'runtime sources must be committed'
    with tempfile.TemporaryDirectory(prefix='brain-stop-before-after-') as temporary:
        folder = Path(temporary).resolve()
        baseline = folder / 'baseline-core'
        baseline.mkdir()
        archive = subprocess.check_output(['git', '-C', str(CORE), 'archive', BASE])
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            for member in bundle.getmembers():
                assert not Path(member.name).is_absolute() and '..' not in Path(member.name).parts
            bundle.extractall(baseline)
        home = folder / 'home'
        home.mkdir()
        root, seed = folder / 'instance', folder / 'seed'
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('BRAIN_', 'CODEX_', 'CLAUDE_', 'T3CODE_')) and k != 'ANTHROPIC_API_KEY'}
        env.update(HOME=str(home), BRAIN_NO_REEXEC='1', BRAIN_LOCK_WAIT='0',
                   GIT_AUTHOR_NAME='Synthetic proof', GIT_AUTHOR_EMAIL='proof@example.invalid',
                   GIT_COMMITTER_NAME='Synthetic proof', GIT_COMMITTER_EMAIL='proof@example.invalid')

        def command(core, args, payload=None, expected=0):
            started = time.perf_counter_ns()
            result = subprocess.run([sys.executable, '-B', str(core / 'bin/brain'), *args],
                                    cwd=root if root.exists() else folder, env=env, text=True,
                                    input=json.dumps(payload) if payload is not None else None,
                                    capture_output=True, timeout=30)
            elapsed = (time.perf_counter_ns() - started) / 1e9
            assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
            return result, elapsed

        command(baseline, ['init', str(root), '--operator', 'fixture-owner', '--title', 'Stop timing fixture',
                           '--tz', 'UTC'])
        env['BRAIN_ROOT'] = str(root)
        command(baseline, ['set', 'root', 'coordinator=fixture-coordinator'])
        command(baseline, ['changes', 'root', '--seen'])
        status = root / 'status.md'
        inbox = root / 'inbox.md'
        inbox.write_text('- 2026-01-01T00:00:00Z from worker: Fixture records committed.\n')
        stamp = max(time.time(), status.stat().st_mtime + 0.001)
        os.utime(inbox, (stamp, stamp))
        subprocess.run(['git', '-C', str(root), 'add', 'inbox.md'], env=env, check=True, capture_output=True)
        subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'fixture: committed records'],
                       env=env, check=True, capture_output=True)
        transcript = folder / 'automated.jsonl'
        transcript.write_text(json.dumps({'type': 'user', 'promptSource': 'system',
            'timestamp': '2026-01-01T00:00:00Z',
            'message': {'role': 'user', 'content': 'Synthetic automated bookkeeping turn.'}}) + '\n')
        payload = {'cwd': str(root), 'session_id': 'fixture-coordinator',
                   'transcript_path': str(transcript), 'last_assistant_message': 'Fixture bookkeeping recorded.'}
        # One instance identity and absolute path. Restore exact tracked/private
        # bytes and mtimes before every sample; core/ is immutable throughout.
        shutil.copytree(root, seed, ignore=shutil.ignore_patterns('core'))

        def restore():
            for child in root.iterdir():
                if child.name != 'core':
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
            for child in seed.iterdir():
                if child.is_dir():
                    shutil.copytree(child, root / child.name)
                else:
                    shutil.copy2(child, root / child.name)

        rows = []
        for condition in ('quiet', 'contended'):
            for repeat in range(samples):
                # Alternate order to avoid giving one version every warm run.
                versions = [('before', baseline), ('after', CORE)]
                if repeat % 2:
                    versions.reverse()
                for version, core in versions:
                    restore()
                    with contextlib.ExitStack() as stack:
                        if condition == 'contended':
                            stream = stack.enter_context((root / '.brain/lock').open('a+'))
                            fcntl.flock(stream, fcntl.LOCK_EX)
                        refresh, refresh_seconds = command(core, ['status', 'root', '--refresh'],
                                                           expected=1 if condition == 'contended' else 0)
                        result, seconds = command(core, ['hook', 'stop'], payload)
                    answer = json.loads(result.stdout) if result.stdout.strip() else None
                    blocked = isinstance(answer, dict) and answer.get('decision') == 'block'
                    assert blocked == (condition == 'contended' and version == 'before'), (condition, version, answer)
                    pending = list((root / '.brain/stop-refresh').glob('*.json'))
                    assert bool(pending) == (condition == 'contended' and version == 'after')
                    rows.append({'condition': condition, 'version': version, 'repeat': repeat + 1,
                                 'hook_wall_seconds': seconds, 'hook_exit': result.returncode,
                                 'hook_blocked': blocked, 'hook_stdout': result.stdout, 'hook_stderr': result.stderr,
                                 'refresh_wall_seconds': refresh_seconds, 'refresh_exit': refresh.returncode,
                                 'refresh_stdout': refresh.stdout, 'refresh_stderr': refresh.stderr,
                                 'stale_receipt': json.loads(pending[0].read_text()) if pending else None})
                    print(json.dumps({'condition': condition, 'version': version, 'repeat': repeat + 1,
                                      'hook_wall_seconds': seconds, 'blocked': blocked}), flush=True)
        summary = []
        for condition in ('quiet', 'contended'):
            for version in ('before', 'after'):
                selected = [r for r in rows if r['condition'] == condition and r['version'] == version]
                summary.append({'condition': condition, 'version': version, 'n': len(selected),
                    'median_hook_wall_seconds': statistics.median(r['hook_wall_seconds'] for r in selected),
                    'blocked': sum(r['hook_blocked'] for r in selected),
                    'median_refresh_wall_seconds': statistics.median(r['refresh_wall_seconds'] for r in selected)})
        return {'before_sha': BASE, 'after_implementation_sha': head, 'source_trees': source_trees,
                'python': sys.version,
                'same_instance': True, 'restored_before_every_sample': True,
                'sample_order': 'conditions serial; before/after order alternates per repeat',
                'concurrency': 1, 'serial_reason': 'Each pair must exclusively own the same restored scratch instance.',
                'scope': 'Real bound coordinator stop hook, automated turn, no AI calls; hook wall time excludes the preceding refresh.',
                'contention': 'Instance lock held by harness through real refresh refusal and real hook invocation; lock wait set to zero for refusal.',
                'provider_spend_usd': 0, 'summary': summary, 'samples': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert args.samples > 0
    result = measure(args.samples)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result['summary'], indent=2), flush=True)
