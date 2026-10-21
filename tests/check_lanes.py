#!/usr/bin/env python3
"""Fail closed on missing/duplicate unit ownership and unknown files in tests/."""
import collections
import json
from pathlib import Path
import shlex
import sys

import lanes


def check(root=lanes.ROOT, mapping=None):
    mapping = lanes.lane_map(root) if mapping is None else mapping
    owners = collections.defaultdict(list)
    targets = []
    for lane in mapping:
        words = shlex.split(lane['command'], comments=True)
        if words[:6] != ['python3', '-B', '-m', 'pytest', '-q', '-p'] or words[6:7] != ['no:cacheprovider']:
            raise ValueError('unexpected test command: ' + lane['name'])
        for word in words[7:]:
            if not word.startswith('tests/test_') or '.py::' not in word:
                raise ValueError('unexpected test target: ' + word)
            targets.append(word)
        for file in {word.split('::', 1)[0][6:] for word in words[7:]}:
            owners[file].append(lane['name'])
    # test_brain has no distinct tests: its reexports are checked by the catch-all smoke.
    if 'catch-all' not in {lane['name'] for lane in mapping}:
        raise ValueError('compatibility entry has no catch-all lane')
    owners[lanes.COMPATIBILITY].append('catch-all')
    files = {p.relative_to(root / 'tests').as_posix() for p in (root / 'tests').rglob('*')
             if p.is_file() and '__pycache__' not in p.parts}
    expected = {name for name in files if Path(name).name.startswith('test_') and name.endswith('.py')}
    errors = []
    if not expected:
        errors.append('no unit test files found')
    for name in sorted(expected | set(owners)):
        if name not in expected:
            errors.append('missing test: ' + name)
        if len(owners[name]) != 1:
            errors.append('test needs exactly one lane: ' + name + ' (' + ', '.join(owners[name]) + ')')
    canonical = lanes.test_targets(root, sorted(expected - {lanes.COMPATIBILITY}))
    if collections.Counter(targets) != collections.Counter(canonical):
        errors.append('lane targets must include every canonical class exactly once')
    classified = expected | set(lanes.SUPPORT) | set(lanes.OUTSIDE_JOB_LANES)
    errors.extend('unclassified tests/ file: ' + name for name in sorted(files - classified))
    errors.extend('missing tests/ file: ' + name for name in sorted(classified - files))
    for lane in mapping:
        missing = set(lanes.SHARED_INPUTS) - set(lane['inputs'])
        if missing:
            errors.append('missing shared inputs: ' + lane['name'] + ': ' + ', '.join(sorted(missing)))
    if errors:
        raise ValueError('\n'.join(errors))
    return dict(lanes=len(mapping), unit_files=len(expected),
                outside_job_lanes=lanes.OUTSIDE_JOB_LANES)


def main():
    try:
        result = check()
    except (OSError, SyntaxError, ValueError) as error:
        print('lane coverage: FAIL\n' + str(error), file=sys.stderr)
        return 1
    print('lane coverage: ' + json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
