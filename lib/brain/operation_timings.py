"""Opt-in command and runtime-hook timings; payloads never enter telemetry."""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from . import common, events, instance, nodes, timings

MAX_PROCESSES = 100
SAMPLE_SECONDS = .1


def context(ref=None):
    # Do not fall back to the machine's default Brain for unrelated folders.
    root = nodes.root_for_path(Path.cwd())
    if root is None:
        common.die('timing requires a Brain folder or registered home/worktree')
    inst = instance.Instance(root)
    node = inst.node(ref) if ref else timings.timing_node(inst)
    return inst, node


def publish(inst, node, command, elapsed, exit_code, source, threshold, processes=None):
    # Only a screened executable/tool basename. No argv, paths, outputs or ids.
    from . import guard
    terms = [term for n in inst.nodes() for term in guard.never_terms(n)]
    name = nodes.import_text(Path(command).name, terms, metadata=True)[:64]
    data = dict(command=name, elapsed_s=round(elapsed, 6), exit_code=exit_code,
                success=exit_code == 0 if exit_code is not None else None,
                slow=elapsed > threshold, source=source)
    if data['slow'] and processes is not None:
        data['processes'] = processes
        data['process_sampling'] = dict(interval_s=SAMPLE_SECONDS, limit=MAX_PROCESSES,
                                       approximate=True)
    events.append_line(inst.root / '.brain/timings/events.jsonl',
                       dict(ts=time.time(), node=events.node_ref(node['id']), kind='timing',
                            actor='brain', ref=uuid.uuid4().hex, data=data))


def cpu_seconds(value):
    days, sep, value = value.rpartition('-')
    total = int(days) * 86400 if sep else 0
    for part, factor in zip(reversed(value.split(':')), (1, 60, 3600)):
        total += float(part) * factor
    return round(total, 6)


def sample_processes(pid, observed, elapsed):
    # ps never receives or returns command lines, environments or file names.
    result = subprocess.run(['ps', '-axo', 'pid=,ppid=,time=,lstart='],
                            capture_output=True, text=True, timeout=1)
    if result.returncode:
        return
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) != 4:
            continue
        try:
            child, parent = int(parts[0]), int(parts[1])
            rows[child] = (parent, cpu_seconds(parts[2]), parts[3])
        except ValueError:
            continue
    selected = {pid}
    for _ in range(MAX_PROCESSES):
        descendants = {child for child, row in rows.items() if row[0] in selected}
        expanded = selected | descendants
        if expanded == selected:
            break
        selected = expanded
    for child in sorted(selected):
        if child not in rows:
            continue
        parent, cpu, started = rows[child]
        identity = (child, started)
        if identity not in observed:
            if len(observed) >= MAX_PROCESSES:
                continue
            observed[identity] = dict(pid=child, ppid=parent, cpu_s=cpu,
                                      first_seen_s=round(elapsed, 6), last_seen_s=round(elapsed, 6))
        observed[identity].update(cpu_s=cpu, last_seen_s=round(elapsed, 6))


def cmd_time(a):
    timings.select_command('time', persist=False)  # one operation record, no wrapper profile
    inst, node = context(a.node)
    argv = a.command[1:] if a.command[:1] == ['--'] else a.command
    if not argv or not math.isfinite(a.threshold) or a.threshold < 0:
        common.die('time needs a command and a finite nonnegative threshold')
    started, observed, code = time.perf_counter(), {}, 127
    try:
        # Inherit cwd, environment and stdio; never invoke a shell implicitly.
        child = subprocess.Popen(argv)
    except OSError as error:
        code = 127 if isinstance(error, FileNotFoundError) else 126
        print('brain time: command could not be started', file=sys.stderr)
    else:
        try:
            while True:
                try:
                    code = child.wait(timeout=SAMPLE_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    try:
                        sample_processes(child.pid, observed, time.perf_counter() - started)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
        except KeyboardInterrupt:
            # Terminal SIGINT also reaches the child. Never leave an owned child
            # running when only the wrapper received the interrupt.
            child.send_signal(2)
            code = child.wait()
    elapsed = time.perf_counter() - started
    try:
        publish(inst, node, argv[0], elapsed, code, 'wrapper', a.threshold, list(observed.values()))
    except (Exception, SystemExit):
        pass  # telemetry damage never changes the executed command's status
    raise SystemExit(code if code >= 0 else 128 - code)


def hook_main():
    """Read one runtime Pre/PostToolUse JSON event, fail open and stay silent."""
    try:
        payload = json.loads(sys.stdin.buffer.read(1024 * 1024))
        if not isinstance(payload, dict):
            return
        phase = payload.get('hook_event_name')
        if phase not in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure'):
            return
        # Scope is the actual hook working folder, never an untrusted cwd field.
        if nodes.root_for_path(Path.cwd()) is None:
            return
        inst, node = context()
        session, call = payload.get('session_id'), payload.get('tool_use_id') or payload.get('call_id')
        tool = payload.get('tool_name')
        if not all(isinstance(x, str) and 0 < len(x) <= 4096 for x in (session, call, tool)):
            return
        identity = hashlib.sha256(json.dumps([session, call]).encode()).hexdigest()
        path = inst.root / '.brain/timings/pending' / (identity + '.json')
        with common.directory_target(path, create=phase == 'PreToolUse', private=True) as target:
            if phase == 'PreToolUse':
                # Duplicate Pre events cannot reset the starting clock.
                fd = target.open(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(fd, 'w') as stream:
                    # Python 3.9 on macOS has a process-relative monotonic epoch;
                    # separate hook processes must pair using wall time instead.
                    json.dump(dict(wall=time.time(),
                                   node=events.node_ref(node['id'])), stream)
                return
            with os.fdopen(target.open(), 'rb') as stream:
                st = os.fstat(stream.fileno())
                start = json.load(stream)
                target.check_file(stream.fileno())
            elapsed = time.time() - start['wall']
            # Backward clock jumps and stale pairs provide no useful duration.
            if not 0 <= elapsed <= 86400:
                return
            if start['node'] != events.node_ref(node['id']):
                return
            # Claim this pair before publishing: duplicate Post events are inert.
            if not target.unlink_owned(path.name, (st.st_dev, st.st_ino)):
                return
        response = payload.get('tool_response')
        code = response.get('exit_code') if isinstance(response, dict) else None
        if type(code) is not int:
            code = 1 if phase == 'PostToolUseFailure' or payload.get('is_error') is True else None
        publish(inst, node, tool, elapsed, code, 'hook', timings.SLOW_SECONDS)
    except (Exception, SystemExit):
        pass
