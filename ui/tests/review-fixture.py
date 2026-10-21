"""Disposable synthetic Brain for real browser -> HTTP -> core review tests."""
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

core = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix='brain-review-ui-') as folder:
    tmp = Path(folder)
    root = tmp / 'meadow'
    env = {**os.environ, 'HOME': folder, 'BRAIN_CONFIG': str(tmp / 'brains.json'), 'BRAIN_NO_REEXEC': '1',
           'GIT_AUTHOR_NAME': 'Demo', 'GIT_AUTHOR_EMAIL': 'demo@example.invalid', 'GIT_COMMITTER_NAME': 'Demo',
           'GIT_COMMITTER_EMAIL': 'demo@example.invalid', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
           'BRAIN_USAGE_TESTING': '1', 'BRAIN_USAGE_TEST_NO_KEYCHAIN': '1', 'BRAIN_MACHINE_TESTING': '1',
           'BRAIN_MACHINE_READING': json.dumps({'cpus': 8, 'load': [1, 1, 1], 'load_per_cpu': [.125] * 3,
                 'thermal': 'normal', 'mem_free_gb': 32, 'disk_free_gb': 200})}
    for name in ('BRAIN_ROOT', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID'):
        env.pop(name, None)
    def brain(*args):
        subprocess.run([sys.executable, str(core / 'bin/brain'), *args], cwd=tmp, env=env,
                       check=True, stdout=subprocess.DEVNULL)
    brain('init', str(root), '--operator', 'Demo reviewer', '--title', 'Meadow review', '--tz', 'UTC')
    env['BRAIN_ROOT'] = str(root)
    brain('new', 'garden', '--title', 'Garden')
    for parent, ident in ((root, 'lesson-a'), (root / 'nodes/garden', 'lesson-b')):
        (parent / 'library').mkdir(exist_ok=True)
        (parent / 'library' / (ident + '.md')).write_text('---\nid: ' + ident + '\ntitle: Synthetic ' + ident + '\nkind: lesson\nstatus: proposed\nscope: Demo reviewer\nsource_node: ' + ('root' if parent == root else 'garden') + '\n---\nA made-up review proposal.\n')
    role = root / 'specialists/layout-guide/role.md'
    role.parent.mkdir(parents=True)
    role.write_text('---\nid: layout-guide\ntitle: Synthetic layout guide\nstatus: proposed\nscope: Demo reviewer\n---\nA specialist for synthetic layout reviews.\n')
    process = subprocess.Popen([sys.executable, str(core / 'bin/brain'), 'ui', '--port', '0',
                               *(['--write'] if '--write' in sys.argv else [])], cwd=root, env=env,
                               stdout=subprocess.PIPE, stderr=sys.stderr, text=True)
    def stop(*_):
        process.terminate()
        process.wait(timeout=10)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for line in process.stdout:
            match = re.search(r'http://127\.0\.0\.1:\d+/\?k=[\w-]+', line)
            if match:
                print(match.group(0), flush=True)
                break
        process.wait()
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=10)
