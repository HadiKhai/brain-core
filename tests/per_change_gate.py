#!/usr/bin/env python3
"""Select source-derived unit lanes conservatively, then run cheap checks and guard."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import check_lanes
import lanes

# Reuse the audited import closure and startup rules of the repository lane map.
# No timestamp cache: each invocation reads the current source and Git diff.
ROOT = Path(__file__).resolve().parent.parent


def git(root, *args):
    result = subprocess.run(['git', '--no-optional-locks', *args], cwd=root,
                            capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError('Git change inventory unavailable')
    return os.fsdecode(result.stdout)


def changed_paths(root, base):
    if not base:
        raise ValueError('integration base unavailable')
    base = git(root, 'rev-parse', '--verify', '--end-of-options', base + '^{commit}').strip()
    git(root, 'merge-base', '--is-ancestor', base, 'HEAD')
    # No rename heuristic, line splitting or shell interpretation of paths.
    paths = git(root, 'diff', '--name-only', '-z', '--no-renames', base, '--').split('\0')
    paths += git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
    return sorted(set(paths) - {''})


def select(root=ROOT, base=None, full=False):
    start = time.monotonic()
    plan = dict(scope='full', reason='batch/nightly', files=[], targets=['tests/test_brain.py'])
    try:
        mapping = lanes.lane_map(root)
        check_lanes.check(root, mapping)
        if not full:
            paths = changed_paths(root, base)
            plan['changed'] = paths
            selected = set()
            for path in paths:
                if check_lanes.matches(path, lanes.SHARED_INPUTS) or path == 'tests/test_brain.py':
                    raise ValueError('shared/root input changed')
                if check_lanes.matches(path, lanes.OUTSIDE_INPUTS):
                    continue
                consumers = [lane for lane in mapping if check_lanes.matches(path, lane['inputs'])]
                if not consumers or not (root / path).is_file():
                    raise ValueError('unknown or removed input')
                for lane in consumers:
                    selected.update(shlex.split(lane['command'], comments=True)[7:])
            targets = sorted(selected)
            plan.update(scope='change', reason='source import map', targets=targets,
                        files=sorted({target.split('::', 1)[0] for target in targets}))
            all_targets = {target for lane in mapping
                           for target in shlex.split(lane['command'], comments=True)[7:]}
            if selected == all_targets:
                plan.update(scope='full', reason='all unit files depend on changed input',
                            targets=['tests/test_brain.py'])
    except (OSError, ValueError, SyntaxError, UnicodeError, RecursionError, subprocess.SubprocessError) as error:
        plan.update(scope='full', reason=str(error), targets=['tests/test_brain.py'])
    if plan['scope'] == 'full':
        plan['files'] = sorted('tests/' + p.name for p in (root / 'tests').glob('test_*.py')
                               if p.name != 'test_brain.py')
    plan['selection_seconds'] = time.monotonic() - start
    return plan


def run(plan, root, jobs, execute=subprocess.run):
    # Native verification supplies NODE. Refuse a standalone execution that could
    # accidentally use the user's current/default instance for its guard.
    node, instance = os.environ.get('NODE'), os.environ.get('BRAIN_ROOT')
    if not node or not instance:
        print('per-change gate requires native NODE and BRAIN_ROOT', file=sys.stderr)
        return 2
    from brain.gate_lock import gate_inherited_fd
    inherited = gate_inherited_fd(Path(instance), os.environ, node)
    pass_fds = () if inherited is None else (inherited,)
    commands = [
        [sys.executable, '-B', str(root / 'tests/check_lanes.py')],
        [sys.executable, '-B', str(root / 'bin/brain'), 'guard', node, str(root)],
    ]
    if plan['targets']:
        commands.append([sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                         '-n', str(jobs), '--max-worker-restart=0', '--serial-after-parallel',
                         *plan['targets']])
    for command in commands:
        result = execute(command, cwd=root, pass_fds=pass_fds)
        if result.returncode:
            return result.returncode
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default=os.environ.get('BRAIN_VERIFY_BASE'))
    parser.add_argument('--train', action='store_true')
    parser.add_argument('--nightly', action='store_true')
    parser.add_argument('--plan', action='store_true', help='print selection without executing any checks')
    parser.add_argument('--jobs', type=int, default=min(16, os.cpu_count() or 1))
    args = parser.parse_args(argv)
    if not 1 <= args.jobs <= min(32, os.cpu_count() or 1):
        parser.error('--jobs must fit available CPUs (1..32)')
    plan = select(base=args.base, full=args.train or args.nightly or os.environ.get('BRAIN_VERIFY_SCOPE') == 'full')
    print(json.dumps(plan, sort_keys=True), flush=True)
    return 0 if args.plan else run(plan, ROOT, args.jobs)


if __name__ == '__main__':
    sys.exit(main())
