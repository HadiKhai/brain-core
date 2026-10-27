"""Measure one explicit panel; never imported by ordinary collection or CLI runs.

Run subprocess and inprocess modes separately against the SAME frozen checkout.
Only the existing Scenario result adapter is selected; raw subprocesses, fixture
initialization, Git and verifier children remain real in both modes.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from unittest import mock

import pytest


# Only the two methods per class selected by the stage-3 panel are executed.
# Exact classes below were audited for their panel methods and transitive setup;
# this is NOT permission to convert every method in these classes.
PANEL_ELIGIBLE = {
    ('test_verify_lanes', 'VerifyLanes'),
    ('test_nodes', 'TeamHomes'),
    ('test_train', 'MergeTrain'),
    ('test_train_review', 'TrainReviewFixes'),
    ('test_check', 'RoundTwo'),
    ('test_jobs', 'Workflow'),
    ('test_jobs', 'UnifiedHandbacks'),
    ('test_watch_extension', 'WatchExtension'),
    ('test_due_lifecycle', 'DueLifecycle'),
    ('test_usage', 'Usage'),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('subprocess', 'inprocess'))
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--core', type=Path, required=True)
    args = parser.parse_args()
    if hashlib.sha256(args.selection.read_bytes()).hexdigest() != '23bed7a9a958d5d415f69c70202eef29d19ca7dc33a1b81c73295979e1a4cf36':
        parser.error('selection must be the unchanged stage-3 sample.txt')
    selection = args.selection.read_text().splitlines()
    if len(selection) != 64 or len(set(selection)) != 64 or any(len(x.split('::')) != 3 for x in selection):
        parser.error('requires exactly 64 unique explicit test methods')
    root = args.core.resolve()
    if Path.cwd().resolve() != root:
        parser.error('run from this owned checkout root')
    output = args.report.resolve() / args.mode
    output.mkdir()
    metrics_dir = output / 'metrics'
    metrics_dir.mkdir()
    os.environ['BRAIN_PANEL_METRICS'] = str(metrics_dir)
    # One literal target in both modes avoids a mode-dependent Git environment.
    # Git itself records even native shell and isolated (-I -S) descendants.
    trace = args.report.resolve() / 'git-trace-active.jsonl'
    if trace.exists():
        parser.error('an earlier active Git trace must be dispositioned first')
    os.environ['GIT_TRACE2_EVENT'] = str(trace)
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'panel_probe'))
    sys.path.insert(0, str(root / 'tests'))
    import metrics
    metrics.install()
    import scenario
    sources = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
               for folder in ('lib/brain', 'tests', 'bin') for p in (root / folder).rglob('*')
               if p.is_file() and '__pycache__' not in p.parts}
    meta = dict(mode=args.mode, sources=sources, selection=selection, cpus=os.cpu_count(),
                load_before=os.getloadavg(), started=time.time(), per_test=[])
    with contextlib.ExitStack() as stack:
        original_setup = scenario.Scenario.setUp
        def setup(case):
            original_setup(case)
            # Direct APIs and native CLI children must fingerprint one identical
            # effective import path. The fixture's cleanup restores the caller.
            probe_path = str(Path(__file__).resolve().parent / 'panel_probe')
            case.env['PYTHONPATH'] = probe_path + os.pathsep + str(root / 'tests')
            os.environ['PYTHONPATH'] = case.env['PYTHONPATH']
        stack.enter_context(mock.patch.object(scenario.Scenario, 'setUp', setup))
        class Panel:
            def pytest_collection_modifyitems(self, items):
                if [item.nodeid for item in items] != selection:
                    raise pytest.UsageError('collection differs from the exact ordered panel')
                seen = set()
                for item in items:
                    cls = item.cls
                    if cls is None or cls in seen or not issubclass(cls, scenario.Scenario):
                        continue
                    seen.add(cls)
                    eligible = (cls.__module__, cls.__name__) in PANEL_ELIGIBLE
                    use = args.mode == 'inprocess' and (eligible or cls.__dict__.get('cli_in_process', False))
                    stack.enter_context(mock.patch.object(cls, 'cli_in_process', use))

            @pytest.hookimpl(hookwrapper=True)
            def pytest_runtest_protocol(self, item, nextitem):
                metrics.CURRENT[0] = item.nodeid
                before, start, wall_start = os.getloadavg(), time.monotonic(), time.time()
                yield
                meta['per_test'].append(dict(test=item.nodeid, seconds=time.monotonic() - start,
                                             start=start, end=time.monotonic(),
                                             wall_start=wall_start, wall_end=time.time(),
                                             load_before=before, load_after=os.getloadavg()))

        original = scenario.Scenario._in_process_result
        def result(case, argv, cwd, launcher, command):
            metrics.emit('brain_inprocess', command=[str(launcher), *argv])
            return original(case, argv, cwd, launcher, command)
        stack.enter_context(mock.patch.object(scenario.Scenario, '_in_process_result', result))
        original_command = scenario.Scenario.command_result
        def command_result(case, *argv, **kwargs):
            start = time.monotonic()
            try:
                value = original_command(case, *argv, **kwargs)
                metrics.emit('brain_helper_time', seconds=time.monotonic() - start,
                             in_process=getattr(value, 'in_process', False))
                return value
            except BaseException:
                metrics.emit('brain_helper_exception', seconds=time.monotonic() - start)
                raise
        stack.enter_context(mock.patch.object(scenario.Scenario, 'command_result', command_result))
        start = time.monotonic()
        with (output / 'pytest.log').open('x') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            status = pytest.main(['-q', '-p', 'no:cacheprovider', '-n0', '--maxfail=1', *selection,
                                 '--completed-results=' + str(output / 'completed.jsonl')], plugins=[Panel()])
        meta.update(exitstatus=int(status), seconds=time.monotonic() - start,
                    load_after=os.getloadavg(), finished=time.time())
        (output / 'metadata.json').write_text(json.dumps(meta, indent=2) + '\n')
        if trace.exists():
            trace.rename(output / 'git-trace.jsonl')
    print(json.dumps({k: v for k, v in meta.items() if k not in ('sources', 'per_test', 'selection')}), flush=True)
    return int(status)


if __name__ == '__main__':
    raise SystemExit(main())
