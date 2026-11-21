"""Profile inbox and wake revalidation on a private, cloned Brain (never a live root)."""
import argparse
import contextlib
import cProfile
import io
import json
import os
from pathlib import Path
import pstats
import sys
import time
from unittest import mock


HERE = Path(__file__).resolve().parent
CORE = HERE.parent.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(CORE / 'lib'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('label')
    parser.add_argument('command', choices=['inbox', 'wake'])
    args = parser.parse_args()
    # Instance initialization already excludes node_modules when copying core.
    # Keep large, private profile clones there so test fixtures never copy them.
    root = HERE / 'node_modules' / args.label
    if not root.is_dir() or not root.resolve().is_relative_to(HERE / 'node_modules'):
        raise SystemExit('create a scratch clone before profiling')
    # A copied absolute link must never lead a writer back to the real instance.
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(folder) / name
            if path.is_symlink() and not path.resolve().is_relative_to(root):
                path.unlink()
    os.environ.update(BRAIN_ROOT=str(root), BRAIN_NO_REEXEC='1',
                      BRAIN_CONFIG=str(root / '.profile-brains.json'), GIT_OPTIONAL_LOCKS='0')
    # Refuse Python writes into the live instance even if a future call ignores BRAIN_ROOT.
    live = str(Path('/Users/hadikhai/Brain'))
    def audit(event, values):
        if event == 'open' and isinstance(values[0], (str, bytes)):
            path = os.fsdecode(values[0])
            flags = values[2]
            if path.startswith(live + '/') and isinstance(flags, int) and flags & (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                raise RuntimeError('profile attempted a live-instance write')
    sys.addaudithook(audit)
    os.chdir(root)
    from brain import command_reads, hosts, instance, nodes, status, tasks, watch
    try:
        from brain import wake_reads
    except ImportError:
        wake_reads = None  # the baseline has no drain-local reuse helper
    inst = instance.Instance(root)
    target = inst.node('root')
    profile = cProfile.Profile()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(nodes, 'load', return_value=inst))
        stack.enter_context(mock.patch.object(hosts, 'wake_start'))
        stack.enter_context(mock.patch.object(hosts, 'wake_schedule'))
        stack.enter_context(mock.patch.object(hosts, 'wake_send', side_effect=AssertionError('no transport')))
        stack.enter_context(mock.patch.object(tasks, 'provider_pull_on_wake'))
        if args.command == 'inbox':
            action = lambda: status.cmd_inbox(argparse.Namespace(
                id='root', add=None, new=False, json=True, weekly=False))
            inventory = None
        else:
            # Twelve independent pending files exercise the same per-file due scan
            # used by wake_drain, without sending, starting timers or consuming turns.
            # Receipt and source reconciliation are real, on the cloned instance.
            directory = hosts.wake_directory(inst, target)
            directory.mkdir(parents=True, exist_ok=True)
            pending = []
            for number in range(12):
                path = directory / (format(number + 1, '064x') + '.pending')
                refs = [{'id': watch.watch_digest(['due-episode'])[:16] + format(number + 1, '048x'),
                         'kind': 'question', 'question': True}]
                path.write_text(json.dumps({'items': hosts.wake_item_refs(refs), 'explicit_kinds': True}))
                pending.append(path)
            inventory = len(pending)
            def action():
                # The public drain calls this same read side for each queue file.
                # A single scope is supplied by the implementation after the fix.
                scope = (wake_reads.wake_read_scope if wake_reads is not None else
                         getattr(hosts, 'wake_read_scope', contextlib.nullcontext))
                with scope(inst):
                    return [hosts.wake_actionable_pending(inst, p) for p in pending]
        started = time.perf_counter()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), command_reads.snapshot():
            result = profile.runcall(action)
        elapsed = time.perf_counter() - started
    path = HERE / (args.label + '-' + args.command + '.prof')
    profile.dump_stats(path)
    stats = pstats.Stats(profile)
    rows = []
    selected = ('lifecycle_read', 'shortest_ids', 'due_items', '_due_items',
                'wake_actionable_pending', 'watch_groups', 'raw_decode')
    for (filename, line, function), (primitive, calls, own, total, _) in stats.stats.items():
        if function in selected or (filename.endswith('status.py') and function == '<genexpr>' and calls > 10000):
            rows.append(dict(file=Path(filename).name, line=line, function=function,
                             primitive_calls=primitive, calls=calls, own_seconds=own, cumulative_seconds=total))
        if function == 'loads' and filename.endswith('/json/__init__.py'):
            for (caller_file, caller_line, caller), values in stats.stats[(filename, line, function)][4].items():
                if caller == 'lifecycle_read' and caller_file.endswith('/reminders.py'):
                    rows.append(dict(file='reminders.py', line=caller_line, function='lifecycle_json_parse',
                                     primitive_calls=values[0], calls=values[1], own_seconds=values[2],
                                     cumulative_seconds=values[3]))
    report = dict(label=args.label, command=args.command, seconds=elapsed, inventory=inventory,
                  output_bytes=len(output.getvalue()), result=result if args.command == 'wake' else None,
                  rows=rows)
    (HERE / (args.label + '-' + args.command + '.json')).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
