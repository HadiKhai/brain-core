"""Retained terminal machine notices expire only after successful delivery."""
import copy
import hashlib
import json
import re

from . import common
from . import reminders as _m_reminders


def terminal_notice(line):
    # Requests, worker failures and actionable holds must never expire as telemetry.
    if re.search(r'<!-- (?:host-question|handback|worker-failure|communication):', line):
        return False
    from . import machine
    event = re.search(r'<!-- machine-wait:[0-9a-f]{32}:(completed|exited|resumed) -->$', line)
    if machine.machine_wait_notice_line(line) and event:
        prefix = {'completed': 'command completed:', 'exited': 'held step exited:',
                  'resumed': 'held step resumed automatically:'}[event[1]]
        return ' from machine: ' + prefix in line
    return bool(re.fullmatch(
        r'- \S.*? from machine: (?:command completed: command completed successfully|'
        r'held step exited: command exited with status -?[0-9]+)'
        r'(?: <!-- machine-wait:[0-9a-f]{32}:(?:completed|exited) -->)?', line))


def notice_pending(line, record):
    return not record['answered'] and not (record.get('expired') is True and terminal_notice(line))


def expire(inst, nodes, delivered=None, data=None):
    """Keep every source byte and episode; delivered=None explicitly drains notices."""
    if data is None:
        with _m_reminders.lifecycle_store(inst) as state:
            return expire(inst, nodes, delivered, state)
    changes = []
    for n in nodes:
        path = n['dir'] / 'inbox.md'
        lines = [line for line in path.read_text().splitlines() if line.startswith('- ')] if path.exists() else []
        current = _m_reminders.reconcile_requests(inst, n, lines, data)
        for line in lines:
            key = _m_reminders.inbox_source_id(n, line)
            record = current[key]
            if (terminal_notice(line) and notice_pending(line, record) and
                    (delivered is None or record['id'] in delivered)):
                record.update(expired=True, seen_at=inst.now(), expiry_rule='terminal-machine-notice-v1')
                changes.append({'node': n['id'], 'episode': record['id'], 'source_id': key})
    return changes


def archive_plan(inst, n, data):
    """Retain exact handled lines and their episode proof before shortening the inbox."""
    source, archive = n['dir'] / 'inbox.md', n['dir'] / 'inbox-archive.md'
    for path in (source, archive):
        common.safe_target(path)
        if path.is_symlink():
            raise ValueError('inbox archive paths must be regular node-local files')
    text = source.read_bytes().decode() if source.exists() else ''
    old = archive.read_bytes().decode() if archive.exists() else ''
    lines = text.splitlines(keepends=True)
    current = _m_reminders.reconcile_requests(
        inst, n, [line.rstrip('\r\n') for line in lines if line.startswith('- ')], copy.deepcopy(data))
    kept, additions, episodes = [], [], []
    for raw in lines:
        line = raw.rstrip('\r\n')
        record = current.get(_m_reminders.inbox_source_id(n, line)) if line.startswith('- ') else None
        if record is None or notice_pending(line, record):
            kept.append(raw)
            continue
        # Proof and original bytes survive even if the following inbox replacement fails.
        digest = hashlib.sha256(raw.encode()).hexdigest()
        marker = '<!-- inbox-archive:' + record['id'] + ':' + digest + ' -->'
        if marker not in old:
            additions.append(marker + '\n<!-- episode ' + json.dumps(record, sort_keys=True) + ' -->\n' +
                             raw + ('' if raw.endswith('\n') else '\n'))
        episodes.append(record['id'])
    archived = old + (('\n' if old and not old.endswith('\n') else '') + ''.join(additions))
    return {'source': source, 'archive': archive, 'inbox_text': ''.join(kept),
            'archive_text': archived, 'episodes': episodes}


def publish_archive(plan):
    if plan['episodes']:
        # Always durably publish the retained copy first; retry deduplicates its markers.
        common.atomic_write(plan['archive'], plan['archive_text'])
        common.atomic_write(plan['source'], plan['inbox_text'])
