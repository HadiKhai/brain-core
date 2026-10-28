#!/usr/bin/env python3
"""Plan or execute marked inbox/task writes against only the brain-core proof task."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def invoke(brain, root, args, data, emit, timeout):
    """Measure launch-to-exit, including interpreter, output and teardown."""
    emit(dict(ts=utcnow(), kind='proof_start', data=data))
    started = time.perf_counter()
    env = {**os.environ, 'BRAIN_ROOT': str(root), 'BRAIN_NO_REEXEC': '1'}
    for key in ('BRAIN_LOCK_HELD', 'BRAIN_CORE', 'BRAIN_QUEUE_RUNNER'):
        env.pop(key, None)
    try:
        result = subprocess.run([str(brain), *args], cwd=root, env=env, capture_output=True,
                                text=True, timeout=timeout)
        elapsed = time.perf_counter() - started
        cause = result.stderr.strip()[:2000] if result.returncode else None
        success, code = result.returncode == 0, result.returncode
        # A zero exit alone is insufficient: queue acceptance must echo our ID.
        accepted = data.get('request_id') in result.stdout.split() if data.get('request_id') else False
    except (OSError, subprocess.TimeoutExpired) as error:
        elapsed = time.perf_counter() - started
        success, code, accepted, cause = False, -1, False, type(error).__name__
    record = dict(data, elapsed_s=elapsed, success=success, exit_code=code,
                  cause=cause, acceptance_echo=accepted, duration_scope='launch_to_exit')
    emit(dict(ts=utcnow(), kind='proof_call', data=record))
    return record


def plan(count, run):
    task = 'TASK-WRITE-QUEUE-PROOF-' + run.upper()
    result = []
    for i in range(count):
        ident = uuid.uuid4().hex
        marker = 'write-queue proof ' + run + ' sample ' + str(i)
        if i % 2:
            args = ['task', 'set', 'brain-core', task, 'next_action=' + marker, '--request-id', ident]
            command = 'task set'
        else:
            args = ['inbox', 'brain-core', '--add', marker, '--request-id', ident]
            command = 'inbox add'
        result.append((args, dict(command=command, request_id=ident, run_id=run, call_id=ident)))
    return task, result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--brain', type=Path, required=True, help='installed launcher, recorded in the plan')
    p.add_argument('-n', type=int, default=20, help='total inbox/task submissions, alternating')
    p.add_argument('--concurrency', type=int, default=4)
    p.add_argument('--timeout', type=float, default=30)
    p.add_argument('--output', type=Path, help='new exclusive JSONL journal, required with --execute')
    p.add_argument('--execute', action='store_true', help='perform real writes; operator deployment authority required')
    a = p.parse_args(argv)
    if a.n < 2 or a.n > 10000 or not 1 <= a.concurrency <= 20 or not 0 < a.timeout <= 600:
        p.error('require 2 <= n <= 10000, 1 <= concurrency <= 20, 0 < timeout <= 600')
    root, brain = a.root.resolve(), a.brain.resolve()
    run = uuid.uuid4().hex
    task, operations = plan(a.n, run)
    setup = ['task', 'add', 'brain-core', task, '--title', 'Write queue proof ' + run]
    if not a.execute:
        print(json.dumps(dict(root=str(root), brain=str(brain), task=task, setup=setup,
                              operations=[args for args, _ in operations],
                              refresh=['status', 'brain-core', '--refresh']), indent=2))
        return 0
    # Brain resolves BRAIN_ROOT upward, so --root must itself be an instance.
    if not a.output or not brain.is_file() or not all(
            (root / name).is_file() for name in ('instance.md', 'node.md', 'nodes/brain-core/node.md')):
        p.error('--execute requires --output, a launcher and an instance root with a brain-core node')
    lock = threading.Lock()
    # Exclusive creation prevents overwriting earlier evidence or any live node file.
    with a.output.open('x', encoding='utf-8') as stream:
        def emit(row):
            with lock:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        emit(dict(ts=utcnow(), kind='proof_run', data=dict(run_id=run, task=task, count=a.n,
                  brain=str(brain), root=str(root), concurrency=a.concurrency)))
        ready = invoke(brain, root, setup, dict(command='task add', call_id=run, run_id=run), emit, a.timeout)
        if not ready['success']:
            return 1
        with ThreadPoolExecutor(max_workers=a.concurrency) as pool:
            results = list(pool.map(lambda op: invoke(brain, root, op[0], op[1], emit, a.timeout), operations))
        refresh = invoke(brain, root, ['status', 'brain-core', '--refresh'],
                         dict(command='status --refresh', call_id=uuid.uuid4().hex, run_id=run), emit, a.timeout)
        return 0 if all(r['success'] and r['acceptance_echo'] for r in results) and refresh['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
