#!/usr/bin/env python3
"""Fail closed on missing/duplicate unit ownership and unknown files in tests/."""
import collections
import json
import os
from pathlib import Path
import shlex
import sys
import subprocess

import lanes

sys.path.insert(0, str(lanes.ROOT / 'lib'))
from brain.verify_lanes import matches


def canonical_targets(root, files):
    # A fresh process avoids cached test modules from another root, and observes
    # conditional/assigned TestCase classes exactly as test_brain.py does.
    script = '''import importlib, json, sys, unittest
sys.path.insert(0, sys.argv[1])
targets = []
for file in json.loads(sys.argv[2]):
    module = importlib.import_module(file[:-3])
    for name, value in vars(module).items():
        if isinstance(value, type) and issubclass(value, unittest.TestCase) and value.__module__ == module.__name__:
            targets.append('tests/' + file + '::' + name)
print(json.dumps(targets))
'''
    try:
        result = subprocess.run([sys.executable, '-B', '-c', script, str(root / 'tests'), json.dumps(files)],
                                cwd=root, capture_output=True, text=True, check=True, timeout=10)
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError) as error:
        raise ValueError('cannot collect canonical test classes') from error


def repository_files(root):
    for folder, directories, files in os.walk(root):
        directories[:] = [d for d in directories if d not in ('.git', '__pycache__', '.pytest_cache')]
        for name in files:
            if name != '.git':
                yield (Path(folder) / name).relative_to(root).as_posix()


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
             if p.is_file() and not {'__pycache__', '.pytest_cache'} & set(p.parts)}
    expected = {name for name in files if Path(name).name.startswith('test_') and name.endswith('.py')}
    errors = []
    if not expected:
        errors.append('no unit test files found')
    for name in sorted(expected | set(owners)):
        if name not in expected:
            errors.append('missing test: ' + name)
        if len(owners[name]) != 1:
            errors.append('test needs exactly one lane: ' + name + ' (' + ', '.join(owners[name]) + ')')
    classified = expected | set(lanes.SUPPORT) | set(lanes.OUTSIDE_JOB_LANES)
    errors.extend('unclassified tests/ file: ' + name for name in sorted(files - classified))
    errors.extend('missing tests/ file: ' + name for name in sorted(classified - files))
    for lane in mapping:
        missing = set(lanes.SHARED_INPUTS) - set(lane['inputs'])
        if missing:
            errors.append('missing shared inputs: ' + lane['name'] + ': ' + ', '.join(sorted(missing)))
        for path in sorted((root / 'lib/brain').glob('*.py')):
            relative = path.relative_to(root).as_posix()
            if path.stem not in lanes.SELECTIVE_MODULES and not matches(relative, lane['inputs']):
                errors.append('nonselective module input missing: ' + lane['name'] + ': ' + relative)
    for path in sorted(repository_files(root)):
        if not matches(path, lanes.OUTSIDE_INPUTS) and not any(matches(path, lane['inputs']) for lane in mapping):
            errors.append('file has no lane inputs: ' + path)
    if errors:
        raise ValueError('\n'.join(errors))
    canonical = canonical_targets(root, sorted(expected - {lanes.COMPATIBILITY}))
    if collections.Counter(targets) != collections.Counter(canonical):
        raise ValueError('lane targets must include every canonical class exactly once')
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
