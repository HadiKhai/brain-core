"""Validate the completed pair and publish per-test counts, Git occupancy and load."""
import argparse
import bisect
import csv
import datetime
import json
from pathlib import Path
import re


def union(intervals):
    total, end = 0.0, float('-inf')
    for start, stop in sorted(intervals):
        assert stop >= start, (start, stop)
        total += max(0.0, stop - max(end, start))
        end = max(end, stop)
    return total


def stamp(value):
    return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def read_mode(root, mode):
    folder = root / mode
    meta = json.loads((folder / 'metadata.json').read_text())
    completed = [json.loads(line) for line in (folder / 'completed.jsonl').read_text().splitlines()]
    finish = [r for r in completed if r['event'] == 'session_finish']
    assert len(finish) == 1 and finish[0]['exitstatus'] == 0
    assert finish[0]['collected'] == 64 and finish[0]['failed'] == 0 and not finish[0]['incomplete']
    results = [r for r in completed if r['event'] == 'completed']
    assert len(results) == 64 and {r['nodeid'] for r in results} == set(meta['selection'])
    assert all(r['outcome'] == 'passed' for r in results)
    events = [json.loads(line) for path in (folder / 'metrics').glob('process-*.jsonl')
              for line in path.read_text().splitlines()]
    assert not any(r['event'] in ('spawn_failed', 'brain_helper_exception') for r in events)
    times = meta['per_test']
    assert [r['test'] for r in times] == meta['selection']
    windows = {r['test']: r for r in times}
    by_test = {r['test']: [] for r in times}
    outside = []
    for row in events:
        (by_test[row['test']] if row['test'] in by_test else outside).append(row)
    trace = {}
    for line in (folder / 'git-trace.jsonl').read_text().splitlines():
        row = json.loads(line)
        if row['event'] in ('start', 'exit'):
            session = trace.setdefault(row['sid'], {})
            assert row['event'] not in session, row
            session[row['event']] = row
    assert all('start' in r for r in trace.values())
    trace_outside, incomplete = [], []
    trace_tests = {r['test']: [] for r in times}
    wall_starts = [r['wall_start'] for r in times]
    for sid, session in trace.items():
        beginning = session['start']
        start = stamp(beginning['time']) - beginning['t_abs']
        index = bisect.bisect_right(wall_starts, start) - 1
        target = times[index] if index >= 0 and start <= times[index]['wall_end'] else None
        pid = int(re.search(r'-P([0-9a-fA-F]+)$', sid.split('/')[-1]).group(1), 16)
        row = dict(sid=sid, pid=pid, start=start, command=beginning['argv'])
        if 'exit' in session:
            end = session['exit']
            row.update(end=stamp(end['time']), seconds=end['t_abs'], code=end['code'])
        else:
            incomplete.append(row)
        if target:
            trace_tests[target['test']].append(row)
        else:
            trace_outside.append(row)
    rows = []
    result_map = {r['nodeid']: r for r in results}
    for nodeid in meta['selection']:
        own = by_test[nodeid]
        window = windows[nodeid]
        sync = [r for r in own if r['event'] == 'git_time' and r['kind'] == 'subprocess_run']
        streaming = [r for r in own if r['event'] == 'git_time' and r['kind'] == 'direct_popen_lifetime']
        spawns = [r for r in own if r['event'] == 'spawn']
        git_spawns = [r for r in spawns if r['git']]
        traced = trace_tests[nodeid]
        git_pids = {r['child_pid'] for r in git_spawns}
        helpers = [r for r in own if r['event'] == 'brain_helper_time']
        clipped = [(max(r['wall_start'], window['wall_start']), min(r['wall_end'], window['wall_end']))
                   for r in sync if r['wall_end'] > window['wall_start'] and r['wall_start'] < window['wall_end']]
        sync_occupancy = union(clipped)
        internal_occupancy = union([(r['start'], r['end']) for r in traced if 'end' in r])
        rows.append(dict(test=nodeid, wall_seconds=window['seconds'], worker_seconds=result_map[nodeid]['duration'],
                         git_sync_calls=len(sync), git_sync_sum_seconds=sum(r['seconds'] for r in sync),
                         git_sync_union_seconds=sync_occupancy,
                         git_sync_share_percent=100 * sync_occupancy / window['seconds'],
                         git_streaming_spans=len(streaming),
                         git_streaming_lifetime_seconds=sum(r['seconds'] for r in streaming),
                         git_python_launches=len(git_spawns), git_trace_starts=len(traced),
                         git_trace_exits=sum('end' in r for r in traced),
                         git_trace_matched_python_pids=sum(r['pid'] in git_pids for r in traced),
                         git_trace_internal_union_seconds=internal_occupancy,
                         git_trace_internal_share_percent=100 * internal_occupancy / window['seconds'],
                         python_observed_subprocesses=len(spawns), brain_native_launches=sum(r['brain'] for r in spawns),
                         brain_inprocess_calls=sum(r['event'] == 'brain_inprocess' for r in own),
                         brain_helper_calls=len(helpers), brain_helper_seconds=sum(r['seconds'] for r in helpers),
                         load_before=window['load_before'], load_after=window['load_after']))
    additive = ('worker_seconds', 'git_sync_calls', 'git_sync_sum_seconds', 'git_sync_union_seconds',
                'git_streaming_spans', 'git_streaming_lifetime_seconds', 'git_python_launches',
                'git_trace_starts', 'git_trace_exits', 'git_trace_matched_python_pids',
                'git_trace_internal_union_seconds', 'python_observed_subprocesses',
                'brain_native_launches', 'brain_inprocess_calls', 'brain_helper_calls', 'brain_helper_seconds')
    summary = {key: sum(r[key] for r in rows) for key in additive}
    summary.update(mode=mode, collected=64, passed=64, wall_seconds=meta['seconds'],
                   git_sync_share_percent=100 * summary['git_sync_union_seconds'] / meta['seconds'],
                   git_trace_internal_share_percent=100 * summary['git_trace_internal_union_seconds'] / meta['seconds'],
                   load_before=meta['load_before'], load_after=meta['load_after'],
                   outside_events=outside, outside_git_traces=trace_outside, incomplete_git_traces=incomplete)
    (folder / 'analysis.json').write_text(json.dumps(dict(summary=summary, per_test=rows), indent=2) + '\n')
    with (folder / 'per-test.csv').open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return meta, summary, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    before_meta, before, before_rows = read_mode(args.report, 'subprocess')
    after_meta, after, after_rows = read_mode(args.report, 'inprocess')
    assert before_meta['sources'] == after_meta['sources'], 'source drift between modes'
    assert before_meta['selection'] == after_meta['selection']
    paired = [dict(test=b['test'], before=b, after=a, seconds_saved=b['wall_seconds']-a['wall_seconds'])
              for b, a in zip(before_rows, after_rows)]
    summary = dict(before=before, after=after,
                   wall_seconds_saved=before['wall_seconds']-after['wall_seconds'],
                   wall_saved_percent=100*(1-after['wall_seconds']/before['wall_seconds']),
                   worker_seconds_saved=before['worker_seconds']-after['worker_seconds'],
                   worker_saved_percent=100*(1-after['worker_seconds']/before['worker_seconds']),
                   paired=paired)
    (args.report / 'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k!='paired'}, indent=2))


if __name__ == '__main__':
    main()
