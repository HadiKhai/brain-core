#!/usr/bin/env python3
"""Read timing JSONL and durable queue evidence without importing or running Brain."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys

from write_queue_schema import SCHEMA, family, payload_hash

FAMILIES = ('task set', 'handback enqueue', 'inbox add')
IDENTITY = re.compile(r'[0-9a-f]{32}')


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('timestamps require an explicit UTC offset')
    return result


def event_time(value):
    # Native queue_phase rows record Unix seconds; other timing rows record ISO text.
    if type(value) in (int, float) and math.isfinite(value):
        return datetime.fromtimestamp(value, timezone.utc)
    return timestamp(value)


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def summary(rows):
    values = sorted(row['elapsed_s'] for row in rows if number(row.get('elapsed_s')))
    failures = [r for r in rows if r.get('exit_code') not in (0, None) or r.get('success') is False]
    def percentile(p):
        return values[max(0, math.ceil(len(values) * p) - 1)] if values else None
    return dict(count=len(rows), timed=len(values), max_s=max(values) if values else None,
                p50_s=percentile(.5), p95_s=percentile(.95), p99_s=percentile(.99),
                at_least_1s=sum(x >= 1 for x in values), nonzero_exits=len(failures),
                causes=dict(Counter(str(r.get('cause') or 'unknown') for r in failures)))


def read_json(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('expected object: ' + path.name)
    return value


def queue_snapshot(folder, selected, attempted=None):
    """c50 index retries share one receipt; sequence is first index byte offset."""
    errors, requests, receipts, sequences = [], {}, {}, {}
    selected = set(selected)
    accepted_ids = set(selected)
    attempted = set(attempted or ())
    folder = Path(folder)
    if not (folder / 'index').exists():
        return dict(available=False, accepted=len(selected), reconciled=0,
                    pending=None, oldest_age_s=None, errors=['queue index unavailable'])
    try:
        raw = (folder / 'index').read_bytes()  # fixed prefix; never follow a growing stream
        offset = 0
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b'\n'):
                raise ValueError('incomplete queue index line')
            entry = json.loads(line)
            ident = entry[SCHEMA['identity']]
            if not isinstance(ident, str) or not IDENTITY.fullmatch(ident):
                raise ValueError('invalid queue identity')
            request = read_json(folder / (ident + '.request'))
            if (set(entry) != {SCHEMA['identity'], SCHEMA['digest']}
                    or set(request) != {'version', SCHEMA['identity'], SCHEMA['digest'], SCHEMA['payload']}
                    or type(request.get('version')) is not int
                    or request.get('version') != SCHEMA['version']
                    or not isinstance(request.get(SCHEMA['payload']), dict)
                    or request.get(SCHEMA['identity']) != ident
                    or request.get(SCHEMA['digest']) != entry[SCHEMA['digest']]
                    or payload_hash(request[SCHEMA['payload']]) != entry[SCHEMA['digest']]):
                raise ValueError('queue payload hash/identity mismatch')
            if ident not in requests:
                requests[ident], sequences[ident] = request, offset
            offset += len(line)
        # Include every receipt, so orphan/misnamed/duplicate evidence cannot hide.
        for path in sorted(folder.glob('*.receipt')):
            receipt = read_json(path)
            ident = receipt.get(SCHEMA['identity'])
            if ident not in requests or path.name != ident + '.receipt' or ident in receipts:
                raise ValueError('orphan, misnamed or duplicate receipt')
            if (receipt.get(SCHEMA['digest']) != requests[ident][SCHEMA['digest']]
                    or type(receipt.get(SCHEMA['sequence'])) is not int
                    or receipt[SCHEMA['sequence']] != sequences[ident]
                    or receipt.get(SCHEMA['state']) not in ('applied', 'rejected')):
                raise ValueError('receipt hash, sequence or state mismatch')
            receipts[ident] = receipt
        seen_pending = False
        for ident in requests:
            if ident not in receipts:
                seen_pending = True
            elif seen_pending:
                raise ValueError('receipt order has a pending predecessor')
        blocked = read_json(folder / 'blocked') if (folder / 'blocked').exists() else None
    except (OSError, ValueError, KeyError, TypeError) as error:
        errors.append(str(error))
        blocked = None
    # A timeout/nonzero exit can follow durable admission. The index, not the
    # caller's exit status or echoed response, decides acceptance for these IDs.
    durable_attempts = attempted & requests.keys()
    selected.update(durable_attempts)
    missing = sorted(selected - receipts.keys())
    unaccepted = sorted(selected - requests.keys())
    rejected = {i: receipts[i].get(SCHEMA['reason'], 'unknown') for i in selected & receipts.keys()
                if receipts[i][SCHEMA['state']] == 'rejected'}
    pending = requests.keys() - receipts.keys()
    return dict(available=True, accepted=len(selected), indexed=len(requests),
                window_selection='acknowledged IDs plus every observed attempted ID found in the durable index',
                ambiguous_accepted=sorted(durable_attempts - accepted_ids),
                unobserved_indexed=len(requests.keys() - selected),
                reconciled=len(selected & receipts.keys()), missing_receipts=missing,
                missing_requests=unaccepted, rejected=rejected, pending=len(pending),
                oldest_age_s=None, oldest_age_reason='v2 requests have no acceptance timestamp',
                blocked=blocked, errors=errors,
                reconciliation='FAIL' if errors or missing or unaccepted or rejected else
                               ('PASS' if selected and not blocked else 'UNKNOWN'))


def observation_groups(internal, calls):
    """Join only unambiguous call identities; retries are distinct invocations."""
    groups = [[row] for row in internal]
    internal_ids = Counter(r.get('request_id') for r in internal)
    caller_ids = Counter(r.get('request_id') for r in calls)
    internal_calls = Counter(r.get('call_id') for r in internal)
    caller_calls = Counter(r.get('call_id') for r in calls)
    used = set()
    for row in calls:
        match = None
        for i, timing in enumerate(internal):
            if i in used:
                continue
            cid, rid = row.get('call_id'), row.get('request_id')
            same_call = (cid and cid == timing.get('call_id')
                         and internal_calls[cid] == caller_calls[cid] == 1)
            other = timing.get('call_id')
            same_request = (rid and rid == timing.get('request_id') and not (cid and other and cid != other)
                            and internal_ids[rid] == caller_ids[rid] == 1
                            and family(row) is not None and family(row) == family(timing))
            if same_call or same_request:
                match = i
                break
        if match is None:
            groups.append([row])
        else:
            groups[match].append(row)
            used.add(match)
    return groups


def each_call_timed(calls, timing):
    """Give every caller its own timing for the same request; call IDs must not conflict."""
    free = list(timing)
    rid = SCHEMA['request_id']
    def take(row, fits):
        found = next((t for t in free if row.get(rid) and t.get(rid) == row.get(rid) and fits(t)), None)
        if found is not None:
            free.remove(found)
        return found is not None
    # Exact call IDs first, then callers that need an unlabelled timing, then unlabelled callers.
    exact = [r for r in calls if r.get('call_id') and take(r, lambda t, c=r['call_id']: t.get('call_id') == c)]
    labelled = [r for r in calls if r.get('call_id') and r not in exact]
    return (all(take(r, lambda t: not t.get('call_id')) for r in labelled)
            and all(take(r, lambda t: True) for r in calls if not r.get('call_id')))


def global_failures(internal, calls):
    groups = observation_groups(internal, calls)
    confirmed, unknown, with_wait = 0, 0, 0
    incomplete = False
    for group in groups:
        failed = [r for r in group if r.get('success') is False or r.get('exit_code') not in (None, 0)]
        flags = [r.get(SCHEMA['lock_failure']) for r in group]
        if any(flag is True for flag in flags):
            confirmed += 1
        elif any(type(r.get(SCHEMA['lock_failure'])) is not bool for r in failed):
            unknown += 1
            with_wait += any(number(r.get('lock_wait_s')) and r['lock_wait_s'] > 0 for r in group)
        incomplete |= any(type(r.get('success')) is not bool for r in group)
    return dict(confirmed=confirmed, unknown_causes=unknown, failures_with_wait=with_wait,
                gate='FAIL' if confirmed else ('UNKNOWN' if unknown or incomplete or not groups else 'PASS'))


def analyze(paths, queue, start, end, now=None):
    if end <= start:
        raise ValueError('end must be after start')
    now = now or datetime.now(timezone.utc)
    if len({Path(p).resolve() for p in paths}) != len(paths):
        raise ValueError('duplicate event input')
    internal, calls, errors, accepted, starts, attempted = [], [], [], set(), [], set()
    for path in paths:
        with Path(path).open() as stream:
            for lineno, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError('expected event object')
                    at = event_time(row['ts'])
                    if not start <= at < end:
                        continue
                    data = row.get('data')
                    if not isinstance(data, dict):
                        raise ValueError('expected event data')
                    if row.get('kind') == 'timing':
                        internal.append(data)
                    elif row.get('kind') == 'proof_call':
                        calls.append(data)
                    elif row.get('kind') == 'proof_start':
                        starts.append(data)
                        ident = data.get(SCHEMA['request_id'])
                        if family(data) in FAMILIES and isinstance(ident, str) and IDENTITY.fullmatch(ident):
                            attempted.add(ident)
                        continue
                    else:
                        continue
                    ident = data.get(SCHEMA['request_id'])
                    if family(data) in FAMILIES and ident:
                        if not isinstance(ident, str) or not IDENTITY.fullmatch(ident):
                            raise ValueError('invalid attempted request identity')
                        attempted.add(ident)
                    if (data.get('success') is True and ident and family(data) in FAMILIES
                            and data.get('acceptance_echo', True) is True):
                        if not isinstance(ident, str) or not IDENTITY.fullmatch(ident):
                            raise ValueError('invalid accepted request identity')
                        accepted.add(ident)
                except (ValueError, TypeError, KeyError) as error:
                    errors.append(dict(file=str(path), line=lineno, error=str(error)))
    observed = defaultdict(list)
    for row in internal:
        observed[str(row.get('command', 'unknown'))].append(row)
    families = {}
    for name in FAMILIES:
        whole = [r for r in calls if family(r) == name]
        timing = [r for r in internal if family(r) == name]
        attempts = [r.get(SCHEMA['attempts']) for r in timing]
        acquisitions = [r.get(SCHEMA['acquisitions']) for r in timing]
        covered = (bool(timing) and all(type(x) is int and x >= 0 for x in attempts + acquisitions)
                   and each_call_timed(whole, timing))
        used = any(type(x) is int and x > 0 for x in attempts + acquisitions)
        stats = summary(whole)
        stats.update(internal=summary(timing), lock_attempts=sum(attempts) if covered else None,
                     lock_acquisitions=sum(acquisitions) if covered else None,
                     lock_evidence='FAIL' if used else ('PASS' if covered else 'UNKNOWN'),
                     caller_latency='FAIL' if stats['at_least_1s'] or stats['nonzero_exits'] else
                                    ('PASS' if whole and stats['count'] == stats['timed']
                                     and all(r.get('duration_scope') == 'launch_to_exit'
                                             and r.get('success') is True and r.get('exit_code') == 0
                                             for r in whole) else 'UNKNOWN'))
        families[name] = stats
    refresh_internal = [r for r in internal if family(r) == 'status --refresh']
    refresh_calls = [r for r in calls if family(r) == 'status --refresh']
    refresh = []
    for group in observation_groups(refresh_internal, refresh_calls):
        # Use caller duration when matched, but preserve every failure/unknown
        # outcome. An unrelated caller never replaces an internal invocation.
        merged = dict(group[-1])
        failed = [r for r in group if r.get('success') is False or r.get('exit_code') not in (None, 0)]
        merged['success'] = False if failed else (True if all(r.get('success') is True for r in group) else None)
        if failed:
            merged['cause'] = next((r['cause'] for r in failed if r.get('cause')), 'unknown')
        refresh.append(merged)
    q = queue_snapshot(queue, accepted, attempted)
    completed = Counter(r.get('call_id') for r in calls)
    unfinished = [r.get('call_id') for r in starts if completed[r.get('call_id')] != 1]
    unbound = sum(r.get('success') is True and family(r) in FAMILIES
                  and (not r.get(SCHEMA['request_id']) or r.get('acceptance_echo', True) is not True)
                  for r in calls + internal)
    return dict(window=dict(start=start.isoformat(), end=end.isoformat(), half_open=True,
                            captured_at=now.isoformat(), complete=end <= now),
                evidence='INCOMPLETE' if errors or q.get('errors') or unfinished or unbound or end > now
                         else 'COLLECTED', unfinished_calls=unfinished, unbound_acceptances=unbound,
                families=families, observed_commands={k: summary(v) for k, v in sorted(observed.items())},
                global_lock_failures=global_failures(internal, calls),
                refresh=dict(**summary(refresh), source='merged_internal_and_caller',
                             internal_count=len(refresh_internal), caller_count=len(refresh_calls),
                             gate='FAIL' if summary(refresh)['nonzero_exits'] else
                             ('PASS' if refresh and all(r.get('success') is True for r in refresh) else 'UNKNOWN')),
                queue=q, errors=errors,
                limitations=['Internal elapsed_s excludes process startup and telemetry publication.',
                             'Legacy command labels do not distinguish task set, handback enqueue, inbox add or refresh.',
                             'Missing lock-attempt counts never establish lock bypass.',
                             'This phase-one report does not prove effects, recovery, contention overlap or drain capacity.'])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--events', type=Path, action='append', required=True)
    p.add_argument('--queue', type=Path, required=True)
    p.add_argument('--start', type=timestamp, required=True)
    p.add_argument('--end', type=timestamp, required=True)
    args = p.parse_args(argv)
    try:
        report = analyze(args.events, args.queue, args.start, args.end)
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
        return 2 if report['errors'] or report['queue'].get('errors') else 0
    except (OSError, ValueError, TypeError) as error:
        p.exit(2, str(error) + '\n')


if __name__ == '__main__':
    sys.exit(main())
