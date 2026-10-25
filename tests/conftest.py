"""Canonical collection and flushed controller-side test evidence."""
import importlib.metadata
import json
import os
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption('--completed-results', metavar='PATH',
                     help='create an exclusive JSONL stream of test phases and completions')


def pytest_configure(config):
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
            terminal.write_line('test-result: ' + line)
            terminal.flush()

    def pytest_sessionstart(self, session):
        timeout = self.config.getoption('timeout')
        self.emit(dict(event='session', timeout=(timeout if timeout is not None else self.config.getini('timeout')),
                       timeout_method=self.config.getini('timeout_method'),
                       timeout_plugin=importlib.metadata.version('pytest-timeout')))

    def pytest_runtest_logreport(self, report):
        phase = dict(event='phase', nodeid=report.nodeid, when=report.when,
                     outcome=report.outcome, duration=report.duration)
        if report.failed:
            phase['longrepr'] = str(report.longrepr)
        self.emit(phase)
        phases = self.pending.setdefault(report.nodeid, [])
        phases.append(phase)
        if report.when == 'teardown':
            outcome = ('failed' if any(p['outcome'] == 'failed' for p in phases) else
                       'skipped' if any(p['outcome'] == 'skipped' for p in phases) else 'passed')
            self.emit(dict(event='completed', nodeid=report.nodeid, outcome=outcome,
                           duration=sum(p['duration'] for p in phases)))
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
