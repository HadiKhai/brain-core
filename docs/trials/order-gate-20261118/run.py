"""One trial arm: run every case through the hook's own pretool_reason with real AI calls (claude-haiku-5-5)."""
import argparse, concurrent.futures, datetime, json, sys, tempfile, threading
from pathlib import Path
ap = argparse.ArgumentParser(); ap.add_argument('--lib'); ap.add_argument('--out'); ap.add_argument('--held', type=int, default=0)
ap.add_argument('--workers', type=int, default=6); ap.add_argument('--only', default=''); ap.add_argument('--reps', type=int, default=1); a = ap.parse_args()
sys.path.insert(0, a.lib); sys.path.insert(0, str(Path(__file__).parent))
from brain import report_gates as rg, ai_json as client
from cases import CASES
CASES = [c for c in CASES if not a.only or c['id'] in a.only.split(',')]
costs, lock = [], threading.Lock()
real = client.subprocess.run
def spy(*args, **kw):
    r = real(*args, **kw)
    try:
        out = json.loads(r.stdout); out = next(e for e in reversed(out) if e.get('type') == 'result') if isinstance(out, list) else out
        with lock: costs.append(float(out.get('total_cost_usd') or 0))
    except Exception:
        with lock: costs.append(None)
    return r
client.subprocess.run = spy
local = threading.local()
def unavailable(error):
    local.error = (str(error) + " " + json.dumps(getattr(error, "detail", None), default=str))[:1500]
rg._unavailable = unavailable
class Inst:
    def __init__(self, root): self.root, self.cfg = root, {}
    def now(self): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def records(turns):
    out, t = [], 0
    for preceding, text in turns:
        if preceding:
            out.append({'type': 'assistant', 'message': {'id': 'm' + str(len(out)), 'stop_reason': 'end_turn',
                        'content': [{'type': 'text', 'text': preceding}]}})
        t += 1
        out.append({'type': 'user', 'promptSource': 'sdk', 'turnOrigin': 'sdk', 'entrypoint': 't3-code',
                    'timestamp': '2026-11-18T10:%02d:00Z' % t, 'message': {'role': 'user', 'content': text}})
    return out
def gate(root, recs, case):
    path = root / 'turn.jsonl'
    path.write_text(''.join(json.dumps(r) + '\n' for r in recs))
    n = {'id': 'trial', 'fm': {'report_gate_held_turns': a.held} if a.held else {}, 'parent': None,
         'local': {'coordinator': 'coord-host', 'coordinator_sessions': ['coord-runtime']}}
    args = {'threadId': 'builder-thread', 'message': case['text']} if case['tool'].endswith('send') else {'prompt': case['text']}
    data = {'tool_name': case['tool'], 'tool_input': args, 'session_id': 'coord-runtime', 'transcript_path': str(path)}
    return rg.pretool_reason(Inst(root), n, data)
def run(case):
    local.error = None
    root = Path(tempfile.mkdtemp(prefix='trial-' + case['id'] + '-'))
    (root / '.brain').mkdir()
    recs = records(case['turns'])
    if case.get('held'):  # turn 1's send first, then the same send on the next turn
        first = gate(root, records(case['turns'][:1]), case)
        if first is None and not local.error:
            return case['id'], None, 'turn-1 passed'
    reason = gate(root, recs, case)
    return case['id'], reason, ('ERROR ' + local.error) if local.error else None
with concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
    jobs = [c for c in CASES for _ in range(a.reps)]
    results = list(pool.map(run, jobs))
rows = []
for (cid, reason, note), case in zip(results, jobs):
    got = 'block' if reason else 'pass'
    rows.append({'id': cid, 'want': case['want'], 'got': got, 'ok': got == case['want'], 'reason': reason, 'note': note})
known = [c for c in costs if c is not None]
json.dump({'rows': rows, 'calls': len(costs), 'cost_usd': sum(known), 'unknown_cost_calls': len(costs) - len(known)},
          open(a.out, 'w'), indent=1)
print(a.out, 'ok', sum(r['ok'] for r in rows), '/', len(rows), 'calls', len(costs), 'usd', round(sum(known), 5))
