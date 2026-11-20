"""Measure the normal root refresh beside owned run-output writes; emit aggregates only."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, required=True)
parser.add_argument('--brain', type=Path, required=True)
parser.add_argument('--instance', required=True)
parser.add_argument('--runs', type=int, required=True)
parser.add_argument('--writer-interval', type=float, required=True)
args = parser.parse_args()
owned = [args.root / folder / ('status-refresh-lock-proof-' + str(os.getpid()))
         for folder in ('full-gates', 'spec-runs', 'job-attempts')]
for folder in owned:
    folder.mkdir(parents=True)
stop = threading.Event()
writes = [0]
def writer():
    while not stop.is_set():
        for folder in owned:
            (folder / 'output.log').write_text(str(writes[0]) + '\n')
        writes[0] += len(owned)
        stop.wait(args.writer_interval)
worker = threading.Thread(target=writer)
worker.start()
results = []
timings = args.root / '.brain/timings/events.jsonl'
try:
    # Refreshes publish the same status file, so the repeats are intentionally
    # serial. Each overlaps the independent output writer.
    for _ in range(args.runs):
        offset = timings.stat().st_size if timings.exists() else 0
        initial = writes[0]
        started = time.monotonic()
        run = subprocess.Popen([str(args.brain), '-b', args.instance, 'status', 'root', '--refresh'],
                               cwd=args.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env={**os.environ, 'BRAIN_NO_REEXEC': '1'})
        sample_stop = threading.Event()
        held = [0.0]
        def sample_lock():
            since = None
            while not sample_stop.is_set():
                try:
                    owner = (args.root / '.brain/lock').read_text().strip()
                except OSError:
                    owner = ''
                now = time.monotonic()
                if owner == str(run.pid):
                    since = now if since is None else since
                    held[0] = max(held[0], now - since)
                else:
                    since = None
                sample_stop.wait(args.writer_interval)
        sampler = threading.Thread(target=sample_lock)
        sampler.start()
        stdout, stderr = run.communicate()
        sample_stop.set()
        sampler.join()
        elapsed = time.monotonic() - started
        records = []
        if timings.exists():
            with timings.open() as stream:
                stream.seek(offset)
                for line in stream:
                    try:
                        row = json.loads(line)
                        if row.get('data', {}).get('command') == 'status':
                            records.append(row['data'])
                    except ValueError:
                        pass
        result = dict(exit=run.returncode, wall_s=elapsed, output_writes=writes[0] - initial,
                      max_sampled_own_hold_s=held[0], hold_sample_interval_s=args.writer_interval,
                      bounded_admission='retrying with bounded admission' in stderr,
                      status_timing_records=[{key: row.get(key) for key in
                          ('success', 'elapsed_s', 'lock_hold_s', 'lock_wait_s', 'phases')} for row in records])
        if run.returncode:
            # Emit only the known structural error, never private output.
            result['input_churn_error'] = 'inputs kept changing through' in stderr
        results.append(result)
        print(json.dumps(result), flush=True)
finally:
    stop.set()
    worker.join()
    for folder in owned:
        shutil.rmtree(folder)
