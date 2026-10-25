"""Opt-in cleanup of proven abandoned test resources on one pinned Docker daemon.

The diagnostic inventory stays read-only. Mutation requires separate node settings,
exact scope and process birth labels, a test namespace, and a complete inventory.
"""
import fnmatch
import json
import os
import re
import shutil
import subprocess
import time

from . import docker_leftovers as _m_leftovers
from . import machine as _m_machine

CLEANUP_SECONDS = 5
CLEANUP_GRACE = 1800
_CLEANUP_ID = re.compile(r'[0-9a-f]{64}')
_CLEANUP_PROJECT = 'com.docker.compose.project'
_CLEANUP_ERRORS = (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, subprocess.SubprocessError)


def cleanup_settings(n):
    fm = n.get('fm', {})
    patterns = fm.get('docker_cleanup_prefixes', '')
    host, daemon = fm.get('docker_cleanup_host', ''), fm.get('docker_cleanup_daemon', '')
    mode = fm.get('docker_cleanup_mode', 'stop')
    if (not isinstance(patterns, str) or not isinstance(host, str) or
            not re.fullmatch(r'unix:///[^\s\x00]+', host) or
            not isinstance(daemon, str) or not daemon or mode not in ('stop', 'remove')):
        return None
    patterns = patterns.split(',')
    if not patterns or any(not re.fullmatch(r'[a-z0-9][a-z0-9_-]*-(?:verify|job)-[a-z0-9_*?-]+', p.strip()) for p in patterns):
        return None
    return host, daemon, [p.strip() for p in patterns], mode


def cleanup_command(binary, host, args, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired('Docker cleanup', CLEANUP_SECONDS)
    env = {k: v for k, v in os.environ.items() if k not in ('DOCKER_HOST', 'DOCKER_CONTEXT')
           and not k.startswith('COMPOSE_')}
    result = subprocess.run([binary, '--host', host] + args, capture_output=True, text=True,
                            timeout=remaining, cwd='/', env=env)
    if result.returncode:
        raise ValueError('Docker cleanup command failed: ' + args[0])
    return result.stdout


def cleanup_endpoint(binary, configured, daemon, deadline):
    # Conflicting selectors never authorize mutation; pin the effective endpoint
    # once, then clear all ambient selectors from subsequent commands.
    if os.environ.get('DOCKER_CONTEXT') and os.environ.get('DOCKER_HOST'):
        raise ValueError('conflicting Docker selectors')
    host = os.environ.get('DOCKER_HOST')
    if not host:
        rows = json.loads(_m_leftovers.leftover_command(binary, ['context', 'inspect'], deadline))
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError('ambiguous Docker context')
        host = rows[0]['Endpoints']['docker']['Host']
    if host != configured:
        raise ValueError('Docker endpoint differs from pinned cleanup endpoint')
    if json.loads(cleanup_command(binary, host, ['info', '--format', '{{json .ID}}'], deadline)) != daemon:
        raise ValueError('Docker daemon differs from pinned cleanup daemon')
    return host


def cleanup_inventory(binary, host, deadline):
    ids = cleanup_command(binary, host, ['ps', '--all', '--quiet', '--no-trunc'], deadline).split()
    if len(ids) > 256 or len(ids) != len(set(ids)) or any(not _CLEANUP_ID.fullmatch(i) for i in ids):
        raise ValueError('invalid cleanup inventory')
    if not ids:
        return []
    rows = json.loads(cleanup_command(binary, host, ['inspect'] + ids, deadline))
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise ValueError('incomplete cleanup inspection')
    seen = set()
    for row in rows:
        identity, labels = row['Id'], row['Config']['Labels']
        if (identity not in ids or identity in seen or not isinstance(labels, dict) or
                any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()) or
                type(row['State']['Running']) is not bool or not isinstance(row.get('Mounts'), list) or
                type(row['HostConfig'].get('AutoRemove')) is not bool):
            raise ValueError('invalid cleanup metadata')
        seen.add(identity)
    return rows


def cleanup_test_member(inst, n, row, patterns, deadline):
    labels = row['Config']['Labels']
    project = labels.get(_CLEANUP_PROJECT, '')
    scope = ':'.join(_m_machine.machine_scope(inst, n['id']))
    if (labels.get('brain.docker-scope') != scope or
            not any(fnmatch.fnmatchcase(project, p) for p in patterns) or
            re.search(r'(?:^|[-_/])(preview|dev|development|production|backup)(?:$|[-_/])',
                      '/'.join((project, row.get('Name', ''))), re.I) or
            labels.get('brain.preview', '').lower() in ('true', 'yes', '1') or
            row['HostConfig']['AutoRemove'] or
            any(m.get('Type') != 'volume' for m in row['Mounts'])):
        return False
    pid, stamp = labels.get('brain.owner-pid', ''), labels.get('brain.owner-stamp', '')
    if not re.fullmatch(r'[1-9][0-9]{0,9}', pid) or not stamp or stamp == 'unknown':
        return False
    if time.time() - _m_leftovers.leftover_timestamp(row['Created']) < CLEANUP_GRACE:
        return False
    # A live but recycled PID is uncertainty, not proof of abandonment.
    return _m_machine.process_stamp(int(pid), deadline=deadline) is None


def cleanup_candidates(inst, n, rows, patterns, deadline):
    """Every peer sharing project/session/volume/network participates in the veto.

    Inspect the entire connected component, including foreign unlabeled peers.
    Never signal by project name or read a compose file from an old checkout.
    """
    def links(row):
        labels = row['Config']['Labels']
        names = {('project', labels[_CLEANUP_PROJECT])} if labels.get(_CLEANUP_PROJECT) else set()
        names.update(('session', labels[k]) for k in _m_leftovers._LEFTOVER_SESSIONS if labels.get(k))
        names.update(('volume', m['Name']) for m in row['Mounts'] if m.get('Type') == 'volume' and m.get('Name'))
        names.update(('network', net.get('NetworkID', name))
                     for name, net in row.get('NetworkSettings', {}).get('Networks', {}).items())
        return names
    groups = [(r, links(r)) for r in rows]
    candidates = []
    for row, connected in groups:
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired('Docker cleanup', CLEANUP_SECONDS)
        pending, members = set(connected), {row['Id']: row}
        while True:
            prior = len(members)
            for other, edges in groups:
                if pending & edges:
                    pending.update(edges)
                    members[other['Id']] = other
            if len(members) == prior:
                break
        if all(cleanup_test_member(inst, n, r, patterns, deadline) for r in members.values()):
            candidates.append(row)
    return candidates


def docker_cleanup_nodes(inst, nodes):
    """Bounded best-effort hook for due and run-close paths; never raises.

    Returns exact stopped IDs and error classes. Callers retain their primary
    outcome. Absent configuration/Docker is a quiet no-op. No host lifecycle calls.
    """
    result = {'stopped': [], 'removed': [], 'errors': []}
    binary = shutil.which('docker')
    if not binary:
        return result
    deadline = time.monotonic() + CLEANUP_SECONDS
    for n in nodes:
        settings = cleanup_settings(n)
        if not settings:
            continue
        try:
            host, daemon, patterns, mode = settings
            host = cleanup_endpoint(binary, host, daemon, deadline)
            rows = cleanup_inventory(binary, host, deadline)
            for row in cleanup_candidates(inst, n, rows, patterns, deadline):
                if not row['State']['Running']:
                    continue
                # Reinspect every peer before each mutation. IDs are immutable;
                # a live owner/new foreign peer vetoes further work.
                current = cleanup_inventory(binary, host, deadline)
                allowed = cleanup_candidates(inst, n, current, patterns, deadline)
                if row['Id'] not in {r['Id'] for r in allowed}:
                    continue
                cleanup_command(binary, host, ['stop', row['Id']], deadline)
                confirmed = cleanup_inventory(binary, host, deadline)
                if any(r['Id'] == row['Id'] and not r['State']['Running'] for r in confirmed):
                    result['stopped'].append(row['Id'])
            if mode == 'remove':
                cleanup_remove(inst, n, binary, host, patterns, deadline, result)
        except _CLEANUP_ERRORS as error:
            result['errors'].append(type(error).__name__)
    return result


def cleanup_resource_inventory(binary, host, kind, deadline):
    identities = cleanup_command(binary, host, [kind, 'ls', '--quiet', '--no-trunc']
                                 if kind == 'network' else [kind, 'ls', '--quiet'], deadline).split()
    expression = _CLEANUP_ID if kind == 'network' else re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}')
    if (len(identities) > 256 or len(set(identities)) != len(identities) or
            any(not expression.fullmatch(identity) for identity in identities)):
        raise ValueError('invalid resource inventory')
    if not identities:
        return []
    rows = json.loads(cleanup_command(binary, host, [kind, 'inspect'] + identities, deadline))
    key = 'Id' if kind == 'network' else 'Name'
    if (not isinstance(rows, list) or len(rows) != len(identities) or
            {r[key] for r in rows} != set(identities)):
        raise ValueError('incomplete resource inventory')
    return rows


def cleanup_resource_owned(inst, n, row, kind, patterns, deadline):
    # Use precisely the container ownership policy for orphan resource labels,
    # including an absent recorded owner, age, namespace and node scope.
    surrogate = {'Config': {'Labels': row.get('Labels')}, 'Name': row.get('Name', ''),
                 'Created': row.get('Created' if kind == 'network' else 'CreatedAt'),
                 'HostConfig': {'AutoRemove': False}, 'Mounts': []}
    if not isinstance(row.get('Labels'), dict):
        return False
    if kind == 'network' and row.get('Containers') != {}:
        return False
    return cleanup_test_member(inst, n, surrogate, patterns, deadline)


def cleanup_remove(inst, n, binary, host, patterns, deadline, result):
    # No force and no -v: a concurrent container restart vetoes rm at the daemon,
    # and container deletion never implicitly deletes an unrelated/anonymous volume.
    rows = cleanup_inventory(binary, host, deadline)
    for row in cleanup_candidates(inst, n, rows, patterns, deadline):
        if row['State']['Running'] or row['State'].get('Status') not in ('exited', 'dead', 'created'):
            continue
        current = cleanup_inventory(binary, host, deadline)
        allowed = cleanup_candidates(inst, n, current, patterns, deadline)
        if not any(r['Id'] == row['Id'] and not r['State']['Running'] for r in allowed):
            continue
        cleanup_command(binary, host, ['rm', row['Id']], deadline)
        if not any(r['Id'] == row['Id'] for r in cleanup_inventory(binary, host, deadline)):
            result['removed'].append(row['Id'])
    for kind in ('volume', 'network'):
        for row in cleanup_resource_inventory(binary, host, kind, deadline):
            try:
                if not cleanup_resource_owned(inst, n, row, kind, patterns, deadline):
                    continue
                identity = row['Id' if kind == 'network' else 'Name']
                current = cleanup_inventory(binary, host, deadline)
                if kind == 'volume':
                    attached = any(m.get('Type') == 'volume' and m.get('Name') == identity
                                   for c in current for m in c['Mounts'])
                else:
                    attached = any(net.get('NetworkID') == identity
                                   for c in current for net in c.get('NetworkSettings', {}).get('Networks', {}).values())
                if attached:
                    continue
                fresh = json.loads(cleanup_command(binary, host, [kind, 'inspect', identity], deadline))
                if fresh != [row] or not cleanup_resource_owned(inst, n, fresh[0], kind, patterns, deadline):
                    continue
                # Docker refuses an in-use volume/network even when attachment
                # races the inventory. Never force/disconnect/remove images.
                cleanup_command(binary, host, [kind, 'rm', identity], deadline)
                remaining = cleanup_resource_inventory(binary, host, kind, deadline)
                if not any(r['Id' if kind == 'network' else 'Name'] == identity for r in remaining):
                    result['removed'].append(kind + ':' + identity)
            except _CLEANUP_ERRORS as error:
                result['errors'].append(type(error).__name__)
