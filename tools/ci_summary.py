#!/usr/bin/env python3
"""Summarize every shard, failing closed on missing or incomplete evidence."""
import argparse
import collections
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def summarize(root, plan):
    totals = collections.Counter()
    starts, ends, walls = [], [], []
    seen, bad = [], False
    print('| Shard | Passed | Failed | Skipped | Total | Wall seconds |')
    print('| --- | ---: | ---: | ---: | ---: | ---: |')
    for index, files in enumerate(plan['shards']):
        directory = root / ('shard-' + str(index))
        timing_path = directory / 'timing.json'
        xml = directory / 'junit.xml'
        if not xml.exists():
            xml = directory / 'partial.xml'
        counts = collections.Counter()
        if xml.exists():
            for case in ET.parse(xml).iter('testcase'):
                key = ('failed' if case.find('failure') is not None or case.find('error') is not None
                       else 'skipped' if case.find('skipped') is not None else 'passed')
                counts[key] += 1
        else:
            bad = True
        expected = sorted(nodeid for nodeid in plan['nodeids'] if nodeid.split('::', 1)[0] in files)
        records = directory / 'completed-results.jsonl'
        completed = []
        if records.exists():
            for line in records.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    bad = True
                    continue
                if row.get('event') == 'completed':
                    completed.append(row)
        ids = [row['nodeid'] for row in completed]
        seen.extend(ids)
        if sorted(ids) != expected or collections.Counter(row['outcome'] for row in completed) != +counts:
            bad = True
        wall = 'missing'
        if timing_path.exists():
            timing = json.loads(timing_path.read_text())
            wall = '{:.2f}'.format(timing['wall_seconds'])
            walls.append(timing['wall_seconds'])
            starts.append(timing['started'])
            ends.append(timing['ended'])
            bad |= not timing.get('complete') or timing.get('exit_code') != 0
        else:
            bad = True
        totals.update(counts)
        print('| {} | {} | {} | {} | {} | {} |'.format(index, counts['passed'], counts['failed'],
              counts['skipped'], sum(counts.values()), wall))
    if sorted(seen) != sorted(plan['nodeids']):
        bad = True
    print('\nOverall: {} passed, {} failed, {} skipped, {} total.'.format(
        totals['passed'], totals['failed'], totals['skipped'], sum(totals.values())))
    if starts:
        print('Test wall span: {:.2f}s; sum of shard wall times: {:.2f}s.'.format(
            max(ends) - min(starts), sum(walls)))
    print('Local baseline: 2924 passed, 0 failed, 5 skipped, 2929 total (6a0f0d9f).')
    print('Inventory and completion evidence: ' + ('FAIL' if bad else 'PASS'))
    return int(bad or totals['failed'] > 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    return summarize(args.results, json.loads(args.plan.read_text()))


if __name__ == '__main__':
    sys.exit(main())
