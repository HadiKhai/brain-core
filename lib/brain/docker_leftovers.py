"""Report abandoned test Docker resources; every Docker command is read-only.

A node explicitly reserves literal test namespaces with docker_leftover_prefixes.
Session labels alone do not establish which node owns Testcontainers resources.
"""
import datetime
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

from . import common as _m_common
from . import guard as _m_guard
from . import machine as _m_machine
from . import transitions as _m_transitions

LEFTOVER_SECONDS = 5
_LEFTOVER_PROJECT = 'com.docker.compose.project'
_LEFTOVER_SESSIONS = ('org.testcontainers.session-id', 'org.testcontainers.sessionId')
_LEFTOVER_SCOPES = ('brain.scope', 'brain.docker-scope')
_LEFTOVER_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, subprocess.SubprocessError)


def leftover_scope(inst, n):
    return ':'.join(_m_machine.machine_scope(inst, n['id']))


def leftover_patterns(n):
    value = n.get('fm', {}).get('docker_leftover_prefixes', '')
    if not isinstance(value, str):
        return []
    return [p for p in re.split(r'[,\s]+', value) if
            re.fullmatch(r'[a-z0-9][a-z0-9_-]*-(?:verify|job)-[a-z0-9_*?-]+', p)]


def leftover_timestamp(value):
    """Normalize Docker's 0..9 fractional digits for Python 3.9, validate offsets first."""
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})', str(value))
    if not match:
        raise ValueError('invalid Created timestamp')
    base, fraction, zone = match.groups()
    if zone != 'Z' and (int(zone[1:3]) > 23 or int(zone[4:6]) > 59):
        raise ValueError('invalid Created timestamp')
    fraction = '.' + (fraction + '000000')[:6] if fraction else ''
    return datetime.datetime.fromisoformat(base + fraction + ('+00:00' if zone == 'Z' else zone)).timestamp()


def leftover_read(path):
    """Do not block on FIFOs, follow links, or parse unbounded local metadata."""
    fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError('unbounded leftover metadata')
        raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('unbounded leftover metadata')
        return json.loads(raw)


def leftover_records():
    return leftover_read(_m_machine.machine_dir() / 'machine-care-queue.json')


def leftover_check(deadline):
    if time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired('Docker leftover report', LEFTOVER_SECONDS)


def leftover_command(binary, args, deadline):
    # Keep an explicit vocabulary even if a future caller supplies new arguments.
    allowed = args == ['context', 'inspect'] or (
        len(args) >= 3 and args[0] == '--host' and args[1].startswith('unix:///') and
        (args[2:] == ['ps', '--all', '--quiet', '--no-trunc'] or
         (len(args) == 4 and args[2] == 'inspect' and re.fullmatch(r'[0-9a-f]{64}', args[3]))))
    if not allowed:
        raise ValueError('non-read-only Docker command refused')
    leftover_check(deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired('Docker leftover report', LEFTOVER_SECONDS)
    env = {k: v for k, v in os.environ.items() if not k.startswith('COMPOSE_') and
           (args[0] != '--host' or k not in ('DOCKER_CONTEXT', 'DOCKER_HOST'))}
    result = subprocess.run([binary] + args, capture_output=True, text=True,
                            timeout=remaining, cwd='/', env=env)
    if result.returncode:
        raise ValueError('Docker unavailable')
    # Callers retain successful output before testing the deadline again.
    return result.stdout


def leftover_owner(labels, records, scope, deadline, observations):
    """Only positive identity/liveness proof may suppress a report.

    Reservation parent and child have separate birth identities. A reused PID or
    unknown probe is unknown ownership, even if the numeric PID currently exists.
    """
    proof, foreign = False, False
    label = labels.get('brain.owner-pid')
    valid_label = label is None or re.fullmatch(r'[1-9][0-9]{0,9}', label) is not None
    token = labels.get('brain.reservation')
    record = None
    if token is not None:
        matches = [r for r in records if isinstance(r, dict) and r.get('token') == token]
        if len(matches) != 1 or not _m_machine.machine_queue.valid(matches[0]):
            return 'unknown', False, proof, foreign
        record = matches[0]
        proof = record.get('scope') == scope
        foreign = 'scope' in record and not proof
        identities = [record['owner']] + ([record['child']] if 'child' in record else [])
        if (not valid_label or any(who[0] > 2147483647 for who in identities) or
                (label is not None and int(label) not in [who[0] for who in identities])):
            return 'unknown', False, proof, foreign
        states = []
        for pid, expected in identities:
            key = (pid, expected)
            if key not in observations['cache']:
                state = 'unknown'
                if expected != 'unknown' and observations['error'] is None:
                    try:
                        current = _m_machine.process_stamp(pid, deadline=deadline)
                        state = 'gone' if current is None else 'alive' if current == expected else 'unknown'
                    except _LEFTOVER_ERRORS as error:
                        observations['error'] = error
                observations['cache'][key] = state
            states.append(observations['cache'][key])
        if 'unknown' in states:
            return 'unknown', False, proof, foreign
        pid = int(label) if label is not None else record.get('child', record['owner'])[0]
        alive = 'alive' in states
    elif not valid_label:
        return 'unknown', False, proof, foreign
    elif label is not None:
        pid = int(label)
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except (OSError, OverflowError):
            return 'unknown', False, proof, foreign
    else:
        return 'unknown', False, proof, foreign
    owner = {'pid': pid}
    if record:
        owner['record'] = token
    if labels.get('brain.job'):
        owner['job'] = labels['brain.job']
    return owner, alive, proof, foreign


def leftover_groups(inst, n, rows, records, host, deadline, observations):
    scope = _m_machine.machine_scope(inst, n['id'])
    patterns, groups, metadata = leftover_patterns(n), {}, {}
    for row in rows:
        labels = row['Config']['Labels']
        metadata[row['Id']] = leftover_owner(labels, records, scope, deadline, observations)
        if labels.get(_LEFTOVER_PROJECT):
            groups.setdefault(('compose', labels[_LEFTOVER_PROJECT]), []).append(row)
        for session in {labels[k] for k in _LEFTOVER_SESSIONS if labels.get(k)}:
            groups.setdefault(('session', session), []).append(row)
    # Union overlapping projects/sessions once. Repeated graph walks can turn a
    # long overlap chain into unbounded CPU work outside the subprocess budget.
    roots = {r['Id']: r['Id'] for r in rows}
    def root(identity):
        while roots[identity] != identity:
            roots[identity] = roots[roots[identity]]
            identity = roots[identity]
        return identity
    for members in groups.values():
        for row in members[1:]:
            roots[root(row['Id'])] = root(members[0]['Id'])
    components = {}
    for row in rows:
        components.setdefault(root(row['Id']), []).append(row)
    out = []
    for (kind, name), members in groups.items():
        if kind == 'compose' and not any(fnmatch.fnmatchcase(name, p) for p in patterns):
            continue
        if kind == 'session' and not any(
                any(r['Config']['Labels'].get(k) == ':'.join(scope) for k in _LEFTOVER_SCOPES) or metadata[r['Id']][2]
                for r in members):
            continue
        # Include every overlapping session/project peer in live/foreign/preview vetoes.
        veto = False
        for r in components[root(members[0]['Id'])]:
            labels = r['Config']['Labels']
            project = labels.get(_LEFTOVER_PROJECT, '')
            owner, alive, proof, foreign = metadata[r['Id']]
            identity = '/'.join([project, str(r.get('Name', '')), labels.get('com.docker.compose.service', ''),
                                 *[labels[k] for k in _LEFTOVER_SESSIONS if labels.get(k)]])
            scoped = any(labels.get(k) == ':'.join(scope) for k in _LEFTOVER_SCOPES) or proof
            if ((not project and not scoped) or alive or foreign or any(k in labels and labels[k] != ':'.join(scope) for k in _LEFTOVER_SCOPES) or
                    labels.get('brain.preview', '').lower() not in ('', 'false', '0') or
                    re.search(r'(?:^|[-_/])preview(?:$|[-_/])', identity, re.I) or
                    (project and not any(fnmatch.fnmatchcase(project, p) for p in patterns))):
                veto = True
        if veto:
            continue
        owners = [metadata[r['Id']][0] for r in members]
        owner = owners[0] if all(o == owners[0] for o in owners) else 'unknown'
        errors, created = [], []
        for row in members:
            try:
                created.append(leftover_timestamp(row.get('Created')))
            except ValueError:
                errors.append('invalid Created timestamp')
        ids = sorted(r['Id'] for r in members)
        key = hashlib.sha256(json.dumps([host, kind, name]).encode()).hexdigest()
        out.append({'key': key, 'kind': kind, 'name': name, 'containers': ids, 'owner': owner,
                    'age_seconds': max(0, int(time.time() - min(created))) if len(created) == len(members) else None,
                    'state': ','.join(sorted({r['State'].get('Status', 'unknown') for r in members})),
                    'errors': sorted(set(errors))})
    return out


def leftover_scan(inst, n, binary, deadline, result):
    rows = []
    observations = {'cache': {}, 'error': None}
    try:
        records = leftover_records()
        if not isinstance(records, list):
            records = []
    except _LEFTOVER_ERRORS:
        records = []
    host = os.environ.get('DOCKER_HOST') if not os.environ.get('DOCKER_CONTEXT') else None
    if not host:
        contexts = json.loads(leftover_command(binary, ['context', 'inspect'], deadline))
        host = contexts[0]['Endpoints']['docker']['Host']
    if not isinstance(host, str) or not host.startswith('unix:///'):
        raise ValueError('Docker endpoint is not local')
    leftover_check(deadline)
    ids = leftover_command(binary, ['--host', host, 'ps', '--all', '--quiet', '--no-trunc'], deadline).split()
    if len(ids) > 256 or len(ids) != len(set(ids)) or any(not re.fullmatch(r'[0-9a-f]{64}', i) for i in ids):
        raise ValueError('ambiguous Docker inventory')
    for identity in ids:
        leftover_check(deadline)
        inspected = json.loads(leftover_command(binary, ['--host', host, 'inspect', identity], deadline))
        if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
            raise ValueError('invalid Docker inspection')
        row = inspected[0]
        if row.get('Id') != identity or not isinstance(row.get('State'), dict):
            raise ValueError('invalid Docker container')
        config = row.get('Config')
        if not isinstance(config, dict):
            raise ValueError('invalid Docker Config')
        labels = config.get('Labels')
        if (not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()) or
                not isinstance(row['State'].get('Status', 'unknown'), str)):
            raise ValueError('invalid Docker metadata')
        row['Config']['Labels'] = labels
        rows.append(row)
        # Retain each successful inspection even if the next JSON document fails.
        result['leftovers'] = leftover_groups(inst, n, rows, records, host, deadline, observations)
        if observations['error'] is not None:
            raise observations['error']
        leftover_check(deadline)
    return result


def leftover_text(row):
    owner = row['owner']
    who = ', '.join(f'{k}={v}' for k, v in sorted(owner.items())) if isinstance(owner, dict) else 'unknown'
    age = str(row['age_seconds']) + 's' if row['age_seconds'] is not None else 'unknown (invalid Created timestamp)'
    return (f"{row['kind']} {row['name']}; owner: {who}; age: {age}; state: {row['state']}; "
            f"containers: {','.join(row['containers'])}")


def docker_leftover_nodes(inst, nodes):
    """One shared deadline and nonblocking episode transaction across the selected nodes.

    Failure aborts the tick, preserves known facts/receipts, and marks the result partial.
    Inbox publication precedes the delivery receipt; the marker makes crash retries idempotent.
    """
    result = {'leftovers': [], 'partial': False, 'errors': []}
    selected = [n for n in nodes if leftover_patterns(n)]
    if not selected:
        return result
    deadline = time.monotonic() + LEFTOVER_SECONDS
    binary = shutil.which('docker')
    store = None
    path = inst.root / '.brain/docker-leftovers.json'
    lock = path.with_suffix('.lock')
    try:
        _m_common.safe_target(path)
        _m_common.safe_target(lock)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'r+') as held:
            if not stat.S_ISREG(os.fstat(held.fileno()).st_mode) or os.fstat(held.fileno()).st_nlink != 1:
                raise ValueError('unsafe Docker report lock')
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                store = leftover_read(path)
                if not isinstance(store, dict) or store.get('version') != 1 or not isinstance(store.get('nodes'), dict):
                    raise ValueError('invalid Docker report receipt')
            except FileNotFoundError:
                store = {'version': 1, 'nodes': {}}
            for n in selected:
                key = leftover_scope(inst, n)
                old = store['nodes'].get(key, {})
                current = {'leftovers': []}
                failed = False
                try:
                    leftover_check(deadline)
                    if not binary:
                        raise FileNotFoundError('Docker unavailable')
                    leftover_scan(inst, n, binary, deadline, current)
                except _LEFTOVER_ERRORS as error:
                    failed = result['partial'] = True
                    result['errors'].append(type(error).__name__)
                active = dict(old) if failed else {}
                for row in current['leftovers']:
                    prior = old.get(row['key'], {})
                    if failed and prior:
                        known = prior['row']
                        row = {**row, 'containers': sorted(set(known['containers'] + row['containers'])),
                               'owner': row['owner'] if known['owner'] == row['owner'] else 'unknown',
                               'state': ','.join(sorted(set(known['state'].split(',') + row['state'].split(',')))),
                               'errors': sorted(set(known['errors'] + row['errors'])), 'unconfirmed': True,
                               'age_seconds': max(known['age_seconds'], row['age_seconds'])
                                   if known['age_seconds'] is not None and row['age_seconds'] is not None else None}
                    active[row['key']] = {'row': row, 'episode': prior.get('episode') or os.urandom(12).hex(),
                                          'delivered': prior.get('delivered', False)}
                store['nodes'][key] = active
                # Save new episode identity before publishing its inbox marker.
                _m_common.atomic_write(path, json.dumps(store))
                for entry in active.values():
                    result['leftovers'].append(entry['row'])
                    if not entry['delivered']:
                        marker = '<!-- docker-leftover:' + entry['episode'] + ' -->'
                        text = _m_guard.redact(' '.join(leftover_text(entry['row']).split()), _m_guard.never_terms(n))
                        try:
                            _m_transitions.inbox_append(n['dir'], '- Docker leftovers (report only): ' + text + ' ' + marker + '\n',
                                                        marker, nonblocking=True)
                            entry['delivered'] = True
                        except OSError:
                            result['partial'] = failed = True
                            result['errors'].append('inbox unavailable')
                _m_common.atomic_write(path, json.dumps(store))
                if failed:
                    break
    except _LEFTOVER_ERRORS as error:
        result['partial'] = True
        result['errors'].append(type(error).__name__)
    if result['partial']:
        # Aborting before a later node or during publication must not conceal its
        # previously known inventory. Partial facts are explicitly unconfirmed.
        if isinstance(store, dict) and isinstance(store.get('nodes'), dict):
            seen = {r['key'] for r in result['leftovers']}
            for n in selected:
                entries = store['nodes'].get(leftover_scope(inst, n), {})
                if isinstance(entries, dict):
                    for entry in entries.values():
                        row = entry.get('row') if isinstance(entry, dict) else None
                        if isinstance(row, dict) and isinstance(row.get('key'), str) and row['key'] not in seen:
                            result['leftovers'].append({**row, 'unconfirmed': True})
                            seen.add(row['key'])
        result['leftovers'] = [{**r, 'unconfirmed': True} for r in result['leftovers']]
        print('Docker leftover report partial: ' + ', '.join(result['errors']), file=sys.stderr)
    return result
