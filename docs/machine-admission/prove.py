#!/usr/bin/env python3
"""Prove hold/admit/expiry with this checkout's CLI, real disk readings and disposable state."""
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

core = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix='brain-admission-proof-') as directory:
    home = Path(directory).resolve()
    root = home / 'instance'
    env = {k: v for k, v in os.environ.items() if not k.startswith('BRAIN_')}
    env.update(HOME=str(home), BRAIN_NO_REEXEC='1', PYTHONDONTWRITEBYTECODE='1',
               BRAIN_USAGE_TEST_NO_KEYCHAIN='1', TZ='UTC')
    for key in ('XDG_CONFIG_HOME', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME'):
        env.pop(key, None)
    durations = []
    def run(*args, expected=0):
        started = time.monotonic()
        result = subprocess.run([sys.executable, '-B', str(core / 'bin/brain'), *args],
                                cwd=root if root.exists() else home, env=env,
                                capture_output=True, text=True, timeout=30)
        durations.append(time.monotonic() - started)
        output = (result.stdout + result.stderr).strip()
        # The proof publishes only aggregate disk measurements and generic audit fields.
        output = output.replace(str(home), '<temporary-home>')
        import pwd
        output = output.replace(pwd.getpwuid(os.getuid()).pw_name, '<local-os-user>')
        print(('$ brain ' + ' '.join(args)).replace(str(home), '<temporary-home>') + '\n' + output + '\nexit=' + str(result.returncode))
        assert result.returncode == expected, output
        return result.stdout
    run('init', str(root), '--operator', 'operator', '--title', 'Admission proof', '--tz', 'Asia/Beirut')
    # Read the live machine via the production module, without fixture injection.
    sys.path.insert(0, str(core / 'lib'))
    from brain.ui import machine_load
    measured = machine_load()['disk_free_gb']
    policy = home / '.local/state/brain/machine-care.json'
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps({'disk_free_gb': measured + 1, 'max_hold_minutes': 0,
        'cpu_idle_min_pct': 0, 'load5_per_cpu': os.cpu_count() or 1,
        'memory_free_gb': 0, 'memory_pressure_max_pct': 100}))
    digest = hashlib.sha256(policy.read_bytes()).hexdigest()
    print('measured free disk GB=' + str(measured) + '; declared fixture reserve GB=' + str(measured + 1))
    run('heavy', expected=1)
    until = (datetime.datetime.now(ZoneInfo('Asia/Beirut')) + datetime.timedelta(seconds=4)).replace(microsecond=0)
    print('instance timezone=Asia/Beirut; subprocess OS timezone=UTC')
    run('machine', 'admit', '--disk-floor', str(measured + 2), '--until', until.strftime('%H:%M:%S'), expected=1)
    # The refusal does a real resource sample. Give the subsequent admission a
    # fresh deadline covering its three pre-expiry CLI calls at that measured cost.
    window = math.ceil(durations[-1] * len(('admit', 'heavy', 'list'))) + 1
    until = (datetime.datetime.now(ZoneInfo('Asia/Beirut')) + datetime.timedelta(seconds=window)).replace(microsecond=0)
    print('proof admission window seconds=' + str(window) + '; based on measured sampling command seconds=' + str(round(durations[-1], 3)))
    admission_output = run('machine', 'admit', '--disk-floor', str(measured), '--until', until.strftime('%H:%M:%S'),
        '--reason', 'one disposable proof admission')
    assert json.loads(admission_output)['active']['until'] == until.timestamp()
    run('heavy')
    run('machine', 'admit', 'list')
    time.sleep(max(0, until.timestamp() - time.time()) + 0.1)
    run('heavy', expected=1)
    listing = json.loads(run('machine', 'admit', 'list'))
    assert listing['active'] is None and [e['action'] for e in listing['events']] == ['admit', 'expire']
    assert json.loads(run('machine', 'admit', 'list')) == listing
    assert hashlib.sha256(policy.read_bytes()).hexdigest() == digest
    print('PASS: instance-zone deadline, tightening refusal, hold -> admitted -> expired hold, one expiry audit; base policy SHA256 unchanged=' + digest)
