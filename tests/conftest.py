"""Canonical collection and flushed controller-side test evidence."""
import importlib.metadata
import json
import math
import os
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption('--serial-after-parallel', action='store_true',
                     help='apply the canonical serial tail to selected unit files too')
    parser.addoption('--completed-results', metavar='PATH',
                     help='create an exclusive JSONL stream of test phases and completions')
    parser.addoption('--duration-order', metavar='PATH',
                     help='schedule longer tests first using prior completed-results JSONL')


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    path = config.getoption('--duration-order')
    if not path or not items:
        return
    durations, suffixes = {}, {}
    try:
        with open(path, encoding='utf-8') as stream:
            for line in stream:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError('expected result objects')
                if row.get('event') != 'completed':
                    continue
                nodeid, duration = row.get('nodeid'), row.get('duration')
                if (not isinstance(nodeid, str) or '::' not in nodeid
                        or type(duration) not in (int, float)
                        or not math.isfinite(duration) or duration < 0):
                    raise ValueError('invalid completed duration')
                durations[nodeid] = max(durations.get(nodeid, 0), duration)
                # Canonical compatibility collection reexports classes from the
                # module lanes. Keep their class/method identity across both.
                suffix = nodeid.split('::', 1)[1]
                suffixes[suffix] = max(suffixes.get(suffix, 0), duration)
        if not durations:
            raise ValueError('no completed durations')
        def score(item):
            return durations.get(item.nodeid, suffixes.get(item.nodeid.split('::', 1)[-1], 0))
        if not any(item.nodeid in durations or item.nodeid.split('::', 1)[-1] in suffixes for item in items):
            raise ValueError('no matching collected tests')
        # Stable sorting changes only order, never the collected inventory.
        items.sort(key=lambda item: -score(item))
    except (OSError, ValueError, TypeError) as error:
        raise pytest.UsageError('cannot use duration-order: ' + str(error)) from error


def pytest_configure(config):
    workers = getattr(config.option, 'numprocesses', 0)
    if isinstance(workers, int) and workers > (os.cpu_count() or 1):
        raise pytest.UsageError('workers exceed available CPU cores')
    # xdist forwards reports to this single writer; workers never open the path.
    if not hasattr(config, 'workerinput'):
        stream = None
        path = config.getoption('--completed-results')
        if path:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                stream = os.fdopen(fd, 'w', encoding='utf-8')
            except OSError as error:
                raise pytest.UsageError('cannot create completed-results: ' + str(error)) from error
        config.pluginmanager.register(CompletedResults(config, stream), 'completed-results-writer')


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    return os.cpu_count() or 1


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_xdist_make_scheduler(config, log):
    if config.option.collectonly or not (config.getoption('--serial-after-parallel') or any(
            Path(arg.split('::', 1)[0]).name == 'test_brain.py' for arg in config.args)):
        return None
    from xdist.scheduler.load import LoadScheduling
    from lanes import SERIAL_CLASSES, SERIAL_TESTS

    class SerialAfterParallel(LoadScheduling):
        def __init__(self, config, log):
            super().__init__(config, log)
            self.serial_node = None
            self.serial_indices = []
            self.serial_started = False

        @property
        def tests_finished(self):
            if self.serial_indices and not self.serial_started:
                return False
            return super().tests_finished

        @property
        def has_pending(self):
            return bool(self.serial_indices and not self.serial_started) or super().has_pending

        def announce(self, lane, count, workers):
            writer = config.pluginmanager.getplugin('completed-results-writer')
            if writer is not None:
                writer.emit(dict(event='lane', lane=lane, tests=count, workers=workers))

        def schedule(self):
            if self.collection is not None:
                for node in self.nodes:
                    self.check_schedule(node)
                return
            assert self.collection_is_completed
            if not self._check_nodes_have_same_collection():
                return
            self.collection = next(iter(self.node2collection.values()))
            self.serial_indices = [i for i, name in enumerate(self.collection)
                                   if len(name.split('::')) > 2 and (
                                       name.split('::')[1] in SERIAL_CLASSES
                                       or name.split('::', 1)[1] in SERIAL_TESTS)]
            serial = set(self.serial_indices)
            self.pending[:] = [i for i in range(len(self.collection)) if i not in serial]
            self.maxschedchunk = 2  # bounded lookahead and balanced duration order
            if len(self.nodes) == 1 or not self.serial_indices:
                self.serial_started = True
                self.pending.extend(self.serial_indices)
            else:
                self.serial_node = self.nodes[-1]
            self.announce('parallel', len(self.pending), len(self.nodes) - bool(self.serial_node))
            for node in self.nodes:
                self.check_schedule(node)
            if not self.pending:
                for node in self.nodes:
                    if node is not self.serial_node:
                        node.shutdown()
            self.start_serial_if_ready()

        def check_schedule(self, node, duration=0):
            if node is self.serial_node and not self.serial_started:
                return
            super().check_schedule(node, duration)

        def remove_node(self, node):
            crashed = super().remove_node(node)
            self.start_serial_if_ready()
            return crashed

        def start_serial_if_ready(self):
            if (self.serial_node is None or self.serial_started or self.pending
                    or any(node is not self.serial_node for node in self.nodes)):
                return
            self.serial_started = True
            self.pending[:] = self.serial_indices
            self.announce('serial', len(self.pending), 1)
            self.check_schedule(self.serial_node)
            if not self.pending:
                self.serial_node.shutdown()

    return SerialAfterParallel(config, log)


class CompletedResults:
    def __init__(self, config, stream):
        self.config = config
        self.stream = stream
        self.pending = {}

    def emit(self, event):
        line = json.dumps(event, sort_keys=True)
        if self.stream is not None:
            self.stream.write(line + '\n')
            self.stream.flush()
        terminal = self.config.pluginmanager.getplugin('terminalreporter')
        if terminal is not None:
            # Pytest's progress writer can leave a percentage or status letter
            # on the current line without setting currentfspath. Force a real
            # newline so every native event has an unambiguous record boundary.
            terminal.write_line('\ntest-result: ' + line)
            terminal.flush()

    def pytest_sessionstart(self, session):
        timeout = self.config.getoption('timeout')
        self.emit(dict(event='session', timeout=(timeout if timeout is not None else self.config.getini('timeout')),
                       timeout_method=self.config.getini('timeout_method'),
                       timeout_plugin=importlib.metadata.version('pytest-timeout')))

    def pytest_runtest_logreport(self, report):
        phase = dict(event='phase', nodeid=report.nodeid, when=report.when,
                     outcome=report.outcome, duration=report.duration, source_path=report.location[0])
        if report.failed:
            phase['longrepr'] = str(report.longrepr)
        self.emit(phase)
        phases = self.pending.setdefault(report.nodeid, [])
        phases.append(phase)
        if report.when == 'teardown':
            outcome = ('failed' if any(p['outcome'] == 'failed' for p in phases) else
                       'skipped' if any(p['outcome'] == 'skipped' for p in phases) else 'passed')
            self.emit(dict(event='completed', nodeid=report.nodeid, outcome=outcome,
                           duration=sum(p['duration'] for p in phases), source_path=report.location[0]))
            del self.pending[report.nodeid]

    def pytest_sessionfinish(self, session, exitstatus):
        self.emit(dict(event='session_finish', exitstatus=int(exitstatus),
                       collected=session.testscollected, failed=session.testsfailed,
                       incomplete=sorted(self.pending)))

    def pytest_unconfigure(self, config):
        if self.stream is not None:
            self.stream.close()


def pytest_ignore_collect(collection_path, config):
    if collection_path.name != 'test_brain.py':
        return None
    explicitly_selected = any(
        Path(arg.split('::', 1)[0]).name == 'test_brain.py'
        for arg in config.args)
    return not explicitly_selected
