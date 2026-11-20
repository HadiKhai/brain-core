#!/usr/bin/env python3
"""Run the ordered 30 separate concurrent-admission CLI regressions, retaining each result."""
import hashlib
import json
import pwd
from pathlib import Path
import subprocess
import sys
import tempfile
import time

core = Path(__file__).resolve().parents[2]
node = 'tests/test_machine.py::BoundedMachineAdmission::test_concurrent_real_cli_writers_keep_both_audit_records'
command = [sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', node]
evidence = {'requested_runs': 30, 'nodeid': node, 'python': sys.version.split()[0],
            'source_sha256': {name: hashlib.sha256((core / name).read_bytes()).hexdigest()
                              for name in ('lib/brain/machine.py', 'tests/test_machine.py')}, 'runs': []}
started = time.monotonic()
with tempfile.TemporaryDirectory(prefix='machine-admit-repeat-') as directory:
    for index in range(30):
        before = time.monotonic()
        result = subprocess.run(command, cwd=core, capture_output=True, text=True)
        evidence['runs'].append({'run': index + 1, 'exit_code': result.returncode,
                                 'seconds': round(time.monotonic() - before, 3)})
        print(json.dumps(evidence['runs'][-1]), flush=True)
        if result.returncode:
            failure = Path(__file__).with_name('repeat-concurrency-failure.txt')
            diagnostic = (result.stdout + result.stderr).replace(str(core), '<checkout>')
            diagnostic = diagnostic.replace(str(Path.home()), '<local-home>')
            diagnostic = diagnostic.replace(pwd.getpwuid(os.getuid()).pw_name, '<local-os-user>')
            failure.write_text(diagnostic)
            break
        (Path(directory) / f'{index + 1}.log').write_text(result.stdout + result.stderr)
evidence['passed'] = sum(row['exit_code'] == 0 for row in evidence['runs'])
evidence['seconds'] = round(time.monotonic() - started, 3)
Path(__file__).with_suffix('.json').write_text(json.dumps(evidence, indent=2) + '\n')
print(json.dumps({'passed': evidence['passed'], 'requested': 30, 'seconds': evidence['seconds']}), flush=True)
sys.exit(0 if evidence['passed'] == 30 else 1)
