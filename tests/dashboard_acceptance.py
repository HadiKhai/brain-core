"""Real dashboard acceptance against an isolated, synthetic on-disk instance.

No mocked API or injected browser snapshot. Run --prepare once, then --sample
for three fresh serving processes, or --serve for browser acceptance.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time
import urllib.request

CORE = Path(__file__).resolve().parents[1]


def environment(folder):
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith(('BRAIN_', 'CODEX_', 'CLAUDE_')):
            env.pop(key)
    env.update(HOME=str(folder), BRAIN_CONFIG=str(folder / 'config/brains.json'),
               BRAIN_ROOT=str(folder / 'meadow'), BRAIN_NO_REEXEC='1',
               PYTHONDONTWRITEBYTECODE='1', GIT_AUTHOR_NAME='Demo',
               GIT_AUTHOR_EMAIL='demo@example.invalid', GIT_COMMITTER_NAME='Demo',
               GIT_COMMITTER_EMAIL='demo@example.invalid', GIT_CONFIG_GLOBAL='/dev/null',
               GIT_CONFIG_NOSYSTEM='1', BRAIN_USAGE_TESTING='1', BRAIN_USAGE_TEST_NO_KEYCHAIN='1')
    return env


def prepare(folder, nodes=24, transcripts=96, events=300):
    if nodes < 1 or transcripts < 1 or events < 1:
        raise ValueError('acceptance needs nodes and nonempty usage sources')
    folder.mkdir(mode=0o700)  # refuse to overwrite or reuse an existing instance
    env = environment(folder)
    root = folder / 'meadow'
    def brain(*args):
        subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'), *args],
                       cwd=folder, env=env, check=True, capture_output=True, timeout=30)
    brain('init', str(root), '--operator', 'Demo reviewer', '--title', 'Meadow workspace', '--tz', 'UTC')
    started = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).isoformat().replace('+00:00', 'Z')
    expected = {'nodes': ['root'], 'jobs': [], 'tasks': [], 'items': [], 'specialists': [], 'questions': []}
    for i in range(nodes):
        ident = 'project-%02d' % i
        brain('new', ident, '--title', 'Field notebook %02d' % i)
        node = root / 'nodes' / ident
        expected['nodes'].append(ident)
        branch = 'job/notebook-%02d' % i
        expected['jobs'].append(branch)
        status = node / 'status.md'
        text = status.read_text().replace('| --- | --- | --- | --- |',
            '| --- | --- | --- | --- |\n| %s | [Notebook thread %02d](https://example.invalid/thread-%02d) (Codex) | - | building |' % (branch, i, i))
        status.write_text(text)
        (node / 'job-ledger.md').write_text('<!-- brain:job-ledger v3 -->\n\n'
            '| Job | Started | Ended | Hours | Outcome | Runtime | Reviews |\n'
            '| --- | --- | --- | --- | --- | --- | --- |\n'
            '| %s | %s | | | | Codex | 0 |\n' % (branch, started))
        question = 'Choose notebook cover %02d?' % i
        expected['questions'].append(question)
        (node / 'inbox.md').write_text('# Inbox\n\n- %s from worker: question: %s\n' % (started, question))
        (node / 'tasks').mkdir(exist_ok=True)
        task = 'TASK-%02d' % i
        expected['tasks'].append(task)
        (node / 'tasks' / (task + '.md')).write_text('---\nversion: 1\nid: "%s"\ntitle: "Prepare notebook %02d"\n'
            'status: "ready"\nkind: "feature"\npriority: "P2"\nupdated: "%s"\ncreated: "%s"\n---\nReview the made-up notebook.\n' % (task, i, started, started))
        for j in range(4):
            item = 'lesson-%02d-%02d' % (i, j)
            expected['items'].append(item)
            path = node / 'library' / (item + '.md')
            path.parent.mkdir(exist_ok=True)
            path.write_text('---\nid: %s\ntitle: Notebook lesson %02d %02d\nkind: lesson\nstatus: active\n'
                            'scope: Demo reviewer\nsource_node: %s\n---\nKeep the review close to its context.\n' % (item, i, j, ident))
        # Ordinary source notes make discovery representative without manufacturing API data.
        notes = node / 'notes'
        notes.mkdir()
        for j in range(96):
            (notes / ('note-%03d.md' % j)).write_text('# Notebook observation\n\nCheck the next reading.\n')
    for i in range(6):
        ident = 'notebook-guide-%02d' % i
        expected['specialists'].append(ident)
        role = root / 'specialists' / ident / 'role.md'
        role.parent.mkdir()
        role.write_text('---\nid: %s\ntitle: Notebook guide %02d\nstatus: active\nscope: Demo reviewer\n'
                        'runtime: Codex\n---\nReview notebook structure.\n' % (ident, i))
    (root / 'schedules.md').write_text('# Schedules\n\n## notebook-review\n\nwhen: Daily\nnode: project-00\nbrief: Review the notebook.\n')
    cfg = folder / 'config'
    codex = folder / 'runtime'
    cfg.mkdir(exist_ok=True)
    (cfg / 'subscriptions.json').write_text(json.dumps({'subscriptions': [
        {'id': 'demo-codex', 'label': 'Notebook plan', 'runtime': 'codex', 'config_dir': str(codex), 'brains': ['*']}]}))
    sessions = codex / 'sessions'
    sessions.mkdir(parents=True)
    total = 0
    for i in range(transcripts):
        cwd = root / 'nodes' / ('project-%02d' % (i % nodes))
        rows = [{'timestamp': started, 'type': 'session_meta', 'payload': {
            'id': 'demo-session-%04d' % i, 'cwd': str(cwd), 'originator': 'Demo'}},
            {'timestamp': started, 'type': 'turn_context', 'payload': {'model': 'demo-model'}}]
        for j in range(events):
            rows.append({'timestamp': started, 'type': 'token_usage_record', 'payload': {
                'thread_id': 'demo-session-%04d' % i, 'response_id': 'response-%04d-%04d' % (i, j),
                'usage': {'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 20, 'total_tokens': 120}}})
        data = ''.join(json.dumps(row, separators=(',', ':')) + '\n' for row in rows)
        (sessions / ('session-%04d.jsonl' % i)).write_text(data)
        total += len(data.encode())
    subprocess.run(['git', 'add', '-A'], cwd=root, env=env, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-qm', 'Seed synthetic dashboard acceptance'], cwd=root, env=env, check=True, capture_output=True)
    manifest = {'expected': expected, 'transcripts': transcripts, 'usage_events': transcripts * events,
                'processed_tokens': transcripts * events * 120, 'transcript_bytes': total,
                'business_files': len(business_snapshot(root)), 'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def business_snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and not set(p.relative_to(root).parts) & {'.git', '.brain', 'core'}}


def read(url, endpoint):
    key = url.split('?k=', 1)[1]
    base = url.split('/?k=', 1)[0]
    with urllib.request.urlopen(base + endpoint + '?k=' + key, timeout=10) as response:
        return json.load(response)


def complete(state):
    return (not state['brain'].get('filling_in') and isinstance(state.get('spend'), dict)
            and state['spend'].get('complete') is True)


def validate(state, manifest):
    expected = manifest['expected']
    assert complete(state), 'metadata and usage must both be complete'
    assert sorted(n['id'] for n in state['nodes']) == sorted(expected['nodes'])
    assert sorted(r['Job'] for n in state['nodes'] for r in n['jobs']['rows']) == sorted(expected['jobs'])
    assert sorted(r['id'] for n in state['nodes'] for r in n['tasks']['rows']) == sorted(expected['tasks'])
    assert sorted(r['id'] for r in state['items']['all']) == sorted(expected['items'])
    assert sorted(r['id'] for r in state['specialists']) == sorted(expected['specialists'])
    assert all(any(q in r['text'] for r in state['inbox']) for q in expected['questions'])
    assert state['spend']['files'] == state['spend']['read'] == manifest['transcripts']
    assert state['spend']['since_week']['processed'] == manifest['processed_tokens']
    assert [s['id'] for s in state['schedules']] == ['notebook-review']


def launch(folder, port):
    if not 1 <= port <= 65535:
        raise ValueError('use the explicitly assigned job port')
    # A busy port fails in the product. Never discover/stop another server or switch ports.
    log = (folder / 'server.log').open('a')
    process = subprocess.Popen([sys.executable, '-B', str(CORE / 'bin/brain'), 'ui', '--port', str(port), '--every', '0.2'],
                               cwd=folder / 'meadow', env=environment(folder), stdout=log, stderr=log)
    record = folder / 'meadow/.brain/ui-servers' / (str(port) + '.json')
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and process.poll() is None:
        if record.exists():
            data = json.loads(record.read_text())
            if data['pid'] == process.pid:
                log.close()
                return process, 'http://127.0.0.1:%d/?k=%s' % (port, data['key'])
        time.sleep(0.02)
    stop(process)
    log.close()
    raise RuntimeError('owned server did not start; inspect server.log')


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def sample(folder, port, count=3):
    manifest = json.loads((folder / 'manifest.json').read_text())
    before = business_snapshot(folder / 'meadow')
    runs = []
    for _ in range(count):
        started = time.monotonic()
        process, url = launch(folder, port)
        try:
            deadline = started + 90
            while True:
                state = read(url, '/api/state')
                if complete(state):
                    break
                assert time.monotonic() < deadline, 'complete fill exceeded 90 seconds'
                assert not read(url, '/api/loading')['failed'], 'read-model initialization failed'
                time.sleep(0.02)
            elapsed = time.monotonic() - started
            validate(state, manifest)
            # A genuine warm refill must observe changed metadata AND appended
            # usage, rather than timing a cached HTTP response or metadata alone.
            note = folder / 'meadow/nodes/project-00/status.md'
            transcript = folder / 'runtime/sessions/session-0000.jsonl'
            old_note, old_transcript = note.read_bytes(), transcript.read_bytes()
            marker = 'Acceptance warm refill %d' % len(runs)
            warm_start = time.monotonic()
            note.write_text(note.read_text().replace('## Now\n', '## Now\n\n' + marker + '\n'))
            with transcript.open('a') as stream:
                json.dump({'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z'),
                           'type': 'token_usage_record', 'payload': {'thread_id': 'demo-session-0000',
                           'response_id': 'acceptance-warm-%d' % len(runs),
                           'usage': {'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 20, 'total_tokens': 120}}},
                          stream, separators=(',', ':'))
                stream.write('\n')
            while True:
                state = read(url, '/api/state')
                current = next(n for n in state['nodes'] if n['id'] == 'project-00')
                if (complete(state) and marker in current['status'].get('Now', '') and
                        state['spend']['since_week']['processed'] == manifest['processed_tokens'] + 120):
                    break
                assert time.monotonic() - warm_start < 90, 'warm refill exceeded 90 seconds'
                assert not read(url, '/api/loading')['failed'], 'warm refill failed'
                time.sleep(0.02)
            warm_elapsed = time.monotonic() - warm_start
            validate(state, {**manifest, 'processed_tokens': manifest['processed_tokens'] + 120})
            runs.append({'complete_fill_seconds': elapsed, 'usage_files': state['spend']['read'],
                         'warm_refill_seconds': warm_elapsed,
                         'processed_tokens': state['spend']['since_week']['processed']})
        finally:
            stop(process)
            if 'old_note' in locals():
                note.write_bytes(old_note)
                transcript.write_bytes(old_transcript)
                del old_note, old_transcript
        assert business_snapshot(folder / 'meadow') == before, 'dashboard changed business files'
    result = {'runs': runs, 'startup_median_seconds': statistics.median(r['complete_fill_seconds'] for r in runs),
              'warm_median_seconds': statistics.median(r['warm_refill_seconds'] for r in runs)}
    assert result['warm_median_seconds'] <= 10, 'complete warm fill exceeded ten seconds'
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--port', type=int, help='explicitly assigned job port (required for serving)')
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--sample', action='store_true')
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args()
    if (args.sample or args.serve) and args.port is None:
        parser.error('--sample and --serve require the assigned --port')
    if args.prepare:
        prepared = prepare(args.folder.resolve())
        print(json.dumps({k: v for k, v in prepared.items() if k != 'expected'}, indent=2))
    if args.sample:
        result = sample(args.folder.resolve(), args.port)
        (args.folder / 'timings.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    if args.serve:
        process, url = launch(args.folder.resolve(), args.port)
        (args.folder / 'browser-target.json').write_text(json.dumps({'url': url, 'pid': process.pid}))
        print(url, flush=True)
        try:
            process.wait()
        except KeyboardInterrupt:
            pass
        finally:
            stop(process)
