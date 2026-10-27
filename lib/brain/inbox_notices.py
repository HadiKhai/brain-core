"""Retain handled inbox episodes while bounding routine notification traffic."""
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


def informational_notice(line):
    """Only explicit routine producers/prefixes; uncertain requests stay open."""
    from . import docker_leftovers
    if re.search(r'<!-- (?:host-question|handback|worker-failure):|[?？]|\b(?:please|choose|needs?|blocked|failure|failed|request|answer|approve|confirm|which|why|decision required|action required|can you|could you|should we)\b', line, re.I):
        return False
    if terminal_notice(line) or docker_leftovers.leftover_report_line(line):
        return True
    return bool(re.match(r'- .*? from [^:]+: (?:fyi|landing|landed)(?:\s|:|$)', line, re.I))


def notice_time(inst, n, line):
    match = re.match(r'- (.*?) from ', line)
    stamp = inst.parse_time(match[1]) if match else None
    # Docker's legacy report writer has no timestamp. Use its immutable Git
    # introduction; an uncommitted report starts aging at the source mtime.
    if stamp is None and line.startswith('- Docker leftovers (report only): '):
        from . import command_reads
        dates = command_reads.history_dates(inst, n['dir'] / 'inbox.md', line)
        if dates:
            stamp = inst.parse_time(dates[0])
        else:
            import datetime
            stamp = datetime.datetime.fromtimestamp((n['dir'] / 'inbox.md').stat().st_mtime, inst.zone())
    return stamp


def automatic_expiry(inst, n, line, record, seen):
    if not informational_notice(line):
        return
    receipt = seen.get(record['id'], {})
    reported = inst.parse_time(receipt.get('reported_at')) if isinstance(receipt, dict) else None
    stamp = notice_time(inst, n, line)
    now = inst.now_dt()
    acted = reported is not None and reported <= now and (stamp is None or reported >= stamp)
    if acted or (stamp is not None and (now - stamp).total_seconds() > 86400):
        record.update(expired=True, seen_at=record.get('seen_at') or inst.now(),
                      expiry_rule='owner-inbox-seen-v1' if acted else 'routine-notice-24h-v1')
        return True
    return False


def notice_pending(line, record):
    return not record['answered'] and not (record.get('expired') is True and (terminal_notice(line) or informational_notice(line)))


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


def archive_plan(inst, n, data, automatic=False, seen=None):
    """Retain exact handled lines and their episode proof before shortening the inbox."""
    source, archive = n['dir'] / 'inbox.md', n['dir'] / 'inbox-archive.md'
    for path in (source, archive):
        common.safe_target(path)
        if path.is_symlink():
            raise ValueError('inbox archive paths must be regular node-local files')
    def snapshot(path):
        with common.directory_target(path) as target:
            generation = target.generation(target.stat())
            text = target.read_bytes().decode() if generation else ''
            if target.generation(target.stat()) != generation:
                raise OSError('inbox archive input changed while planning')
            return text, generation
    text, source_generation = snapshot(source)
    old, archive_generation = snapshot(archive)
    lines = text.splitlines(keepends=True)
    planned = copy.deepcopy(data)
    current = _m_reminders.reconcile_requests(
        inst, n, [line.rstrip('\r\n') for line in lines if line.startswith('- ')], planned)
    kept, additions, episodes = [], [], []
    for raw in lines:
        line = raw.rstrip('\r\n')
        record = current.get(_m_reminders.inbox_source_id(n, line)) if line.startswith('- ') else None
        if record is not None and automatic and not record['answered']:
            if not automatic_expiry(inst, n, line, record, seen or {}):
                kept.append(raw)
                continue
        if record is None or notice_pending(line, record):
            kept.append(raw)
            continue
        # Proof and original bytes survive even if the following inbox replacement fails.
        digest = hashlib.sha256(raw.encode()).hexdigest()
        marker = '<!-- inbox-archive:' + record['id'] + ':' + digest + ' -->'
        entry = (marker + '\n<!-- episode ' + json.dumps(record, sort_keys=True) + ' -->\n' +
                 raw + ('' if raw.endswith('\n') else '\n'))
        if entry not in old:
            additions.append(entry)
        episodes.append(record['id'])
    archived = old + (('\n' if old and not old.endswith('\n') else '') + ''.join(additions))
    return {'source': source, 'archive': archive, 'inbox_text': ''.join(kept),
            'archive_text': archived, 'episodes': episodes,
            'source_generation': source_generation, 'archive_generation': archive_generation,
            'lifecycle': planned}


def archive_replace(target, text, generation):
    target.atomic_write(text, expected=generation)


def publish_archive(plan):
    if plan['episodes']:
        import fcntl
        import os
        # Share the native append lock, retaining the original inode until replacement.
        # Appenders recheck their opened inode after acquiring this lock.
        with common.directory_target(plan['source']) as source:
            with os.fdopen(source.open(), 'rb') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                source.check_file(stream.fileno())
                if source.generation(os.fstat(stream.fileno())) != plan['source_generation']:
                    raise OSError('inbox changed after archive planning; replan before publishing')
                with common.directory_target(plan['archive']) as archive:
                    # Always durably publish the retained copy first, with both snapshots fenced.
                    archive_replace(archive, plan['archive_text'], plan['archive_generation'])
                    archive_replace(source, plan['inbox_text'], plan['source_generation'])


def autoarchive(inst, nodes):
    """One short writer batch on an ordinary inbox read; weekly/UI reads stay pure."""
    from . import instance, status
    with instance.brain_lock(inst):
        if common.outward_links(inst.root):
            raise ValueError('inbox autoarchive refuses outward links')
        saved, _ = status.inbox_seen_snapshot(inst)
        if not isinstance(saved, dict):
            raise ValueError('invalid inbox seen state')
        reporters = saved.get('reporters', {}) if saved.get('version') == 2 else {}
        if not isinstance(reporters, dict):
            raise ValueError('invalid inbox reporter state')
        plans = []
        # Persist selected episode identities before removing any source line.
        with _m_reminders.lifecycle_store(inst) as data:
            for n in nodes:
                seen = reporters.get(n['id'], {})
                if not isinstance(seen, dict):
                    raise ValueError('invalid owning inbox reporter state')
                plan = archive_plan(inst, n, data, automatic=True, seen=seen)
                if plan['episodes']:
                    data.update(plan['lifecycle'])
                    plans.append((n, plan))
        paths = []
        try:
            for n, plan in plans:
                publish_archive(plan)
                paths.extend([plan['source'], plan['archive']])
                with _m_reminders.lifecycle_store(inst) as data:
                    _m_reminders.reconcile_requests(inst, n,
                        [line for line in plan['inbox_text'].splitlines() if line.startswith('- ')], data)
        finally:
            # A later node's refusal must not strand earlier durable removals
            # outside Git. Commit only successfully published node pairs.
            if paths:
                inst.commit_settings_batch(paths, 'Archive handled inbox notices')
        return sum(len(plan['episodes']) for _, plan in plans)


def cmd_inbox_seen(a):
    """An explicit action receipt for one owning node, never a subtree-wide clear."""
    from . import nodes, status
    inst = nodes.load()
    n = inst.node(a.id)
    with _m_reminders.lifecycle_store(inst) as data:
        lines = status.inbox_lines(n, readonly=True, lifecycle_data=data)
        items = [{'id': _m_reminders.request_episode_id(inst, n, line, data), 'line': line} for line in lines]
        item = _m_reminders.resolve_lifecycle_id(items, a.item_id)
        _m_reminders.record_answer(inst, n, item['line'], item['id'], a.reason, data)
        data['answers'][-1]['disposition'] = 'acted-on'
    autoarchive(inst, [n])
    print('handled inbox episode archived; source and action receipt retained')
