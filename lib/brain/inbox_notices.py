"""Retained terminal machine notices expire only after successful delivery."""
import re

from . import reminders as _m_reminders


def terminal_notice(line):
    # Requests, worker failures and actionable holds must never expire as telemetry.
    if re.search(r'<!-- (?:host-question|handback|worker-failure|communication):', line):
        return False
    return bool(re.fullmatch(
        r'- \S.*? from machine: (?:command completed: command completed successfully|'
        r'held step exited: command exited with status [0-9]+)'
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
