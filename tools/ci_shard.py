#!/usr/bin/env python3
"""Collect once, partition whole test files deterministically, and run one shard."""
import argparse
import collections
import contextlib
import io
import json
import math
import os
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SHARDS = 8


def collect():
    import pytest
    sys.path.insert(0, str(ROOT / 'tests'))
    import lanes
    # Match the canonical compatibility entry: imported helper classes belong
    # only to their defining module, never to every module that imports them.
    files = sorted(path.name for path in (ROOT / 'tests').glob('test_*.py')
                   if path.name != lanes.COMPATIBILITY)
    targets = lanes.test_targets(ROOT, files)

    class Inventory:
        def pytest_collection_finish(self, session):
            self.nodeids = [item.nodeid for item in session.items]

    inventory, output = Inventory(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = pytest.main(['--collect-only', '-q', '-p', 'no:cacheprovider', *targets],
                           plugins=[inventory])
    if code != 0:
        raise RuntimeError(output.getvalue())
    ids = inventory.nodeids
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('empty or duplicate collected inventory')
    return ids


def partition(files, durations):
    """Longest first, then lowest load; path and shard number break every tie."""
    if len(files) != len(set(files)):
        raise ValueError('duplicate file inventory')
    weights = {}
    for name in sorted(files):
        weight = durations[name] if name in durations else (ROOT / name).stat().st_size / 1000
        if type(weight) not in (int, float) or not math.isfinite(weight) or weight < 0:
            raise ValueError('invalid duration for ' + name)
        weights[name] = weight
    shards, loads = [[] for _ in range(SHARDS)], [0.0] * SHARDS
    for name in sorted(files, key=lambda name: (-weights[name], name)):
        index = min(range(SHARDS), key=lambda index: (loads[index], index))
        shards[index].append(name)
        loads[index] += weights[name]
    flattened = [name for shard in shards for name in shard]
    if collections.Counter(flattened) != collections.Counter(files) or not all(shards):
        raise ValueError('shards must cover every collected file exactly once')
    return [sorted(shard) for shard in shards]


def write_json(path, value):
    draft = path.with_suffix('.next')
    draft.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    draft.replace(path)


class ProgressJUnit:
    """Atomic partial JUnit survives interruption before pytest's final XML write."""
    def __init__(self, directory):
        self.directory = directory
        self.started = time.time()
        self.pending, self.completed = {}, {}
        self.flush()

    def pytest_runtest_logstart(self, nodeid, location):
        self.pending[nodeid] = []
        self.flush()

    def pytest_runtest_logreport(self, report):
        phases = self.pending.setdefault(report.nodeid, [])
        phases.append(report)
        if report.when == 'teardown':
            self.completed[report.nodeid] = self.pending.pop(report.nodeid)
        self.flush()

    def flush(self):
        suite = ET.Element('testsuite', name='partial', tests=str(len(self.completed) + len(self.pending)),
                           time=str(time.time() - self.started))
        for nodeid, phases in sorted({**self.completed, **self.pending}.items()):
            case = ET.SubElement(suite, 'testcase', classname=nodeid.rsplit('::', 1)[0],
                                 name=nodeid.rsplit('::', 1)[-1],
                                 time=str(sum(report.duration for report in phases)))
            failed = [report for report in phases if report.failed]
            skipped = [report for report in phases if report.skipped]
            if failed:
                ET.SubElement(case, 'failure', message='test phase failed').text = '\n'.join(
                    str(report.longrepr) for report in failed)
            elif nodeid in self.pending:
                ET.SubElement(case, 'error', message='incomplete: shard stopped before teardown')
            elif skipped:
                ET.SubElement(case, 'skipped').text = str(skipped[0].longrepr)
        draft = self.directory / 'partial.next'
        ET.ElementTree(suite).write(draft, encoding='utf-8', xml_declaration=True)
        draft.replace(self.directory / 'partial.xml')
        write_json(self.directory / 'timing.json', dict(started=self.started, ended=time.time(),
                   wall_seconds=time.time() - self.started, complete=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'run'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--shard', type=int, choices=range(SHARDS))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.command == 'plan':
        ids = collect()
        files = sorted({nodeid.split('::', 1)[0] for nodeid in ids})
        timing = ROOT / 'tools/ci_durations.json'
        durations = json.loads(timing.read_text())['seconds'] if timing.exists() else {}
        shards = partition(files, durations)
        # The actual pytest collection, rather than a filename glob, is the authority.
        assert set().union(*(set(shard) for shard in shards)) == set(files)
        write_json(args.plan, dict(nodeids=ids, shards=shards))
        print('Partition verified: {} tests in {} files, {} disjoint shards'.format(
            len(ids), len(files), SHARDS))
        return 0
    if args.shard is None or args.output is None:
        parser.error('run requires --shard and --output')
    plan = json.loads(args.plan.read_text())
    files = plan['shards'][args.shard]
    expected = sorted(nodeid for nodeid in plan['nodeids'] if nodeid.split('::', 1)[0] in files)
    targets = sorted({'::'.join(nodeid.split('::')[:2]) for nodeid in expected})
    args.output.mkdir(parents=True, exist_ok=True)
    write_json(args.output / 'expected.json', expected)
    import pytest
    progress = ProgressJUnit(args.output)
    code = pytest.main(['-q', '-p', 'no:cacheprovider', '-n', 'auto', '--serial-after-parallel',
                        '--junitxml=' + str(args.output / 'junit.xml'),
                        '--completed-results=' + str(args.output / 'completed-results.jsonl'),
                        *targets], plugins=[progress])
    progress.flush()
    write_json(args.output / 'timing.json', dict(started=progress.started, ended=time.time(),
               wall_seconds=time.time() - progress.started, complete=True, exit_code=int(code)))
    return int(code)


if __name__ == '__main__':
    sys.exit(main())
