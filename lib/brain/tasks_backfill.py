"""Additive, replayable imports of node-local commitments; never edits source prose."""
import datetime
import hashlib
import re

from . import common as _m_common
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import tasks as _m_tasks


SOURCES = ('backlog.md', 'build-order.md')


def fingerprint(*parts):
    return hashlib.sha256('\0'.join(parts).encode()).hexdigest()[:16].upper()


def nonwork_source_lines(n, filename='backlog.md'):
    """Explicitly labelled decision/history sections and code examples are not work."""
    path = n['dir'] / filename
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ValueError('backfill source must be a regular node-local file')
    excluded, depth, fence = {}, None, None
    for number, line in enumerate(path.read_text().splitlines(), 1):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            excluded[number] = line
            continue
        heading = re.match(r'^(#{1,6})\s+(.+)', line) if not fence else None
        if heading:
            level, title = len(heading[1]), heading[2].strip().casefold()
            if depth is not None and level <= depth:
                depth = None
            if title.startswith('historical ') or title in ('operator decisions', 'decisions'):
                depth = level
        if fence or depth is not None:
            excluded[number] = line
    return excluded


def candidates(inst, n):
    """Import explicit list entries, not narrative paragraphs or generated facts."""
    entries = []
    for filename in SOURCES:
        path = n['dir'] / filename
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError('backfill source must be a regular node-local file')
        section, generated = '', False
        nonwork = nonwork_source_lines(n, filename)
        observed = datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).isoformat()
        for line_no, raw in enumerate(path.read_text().splitlines(), 1):
            if line_no in nonwork:
                continue
            if '<!-- brain:facts' in raw:
                generated = True
            if generated:
                if 'brain:facts:end' in raw or 'brain:end facts' in raw or '<!-- /brain:facts' in raw:
                    generated = False
                continue
            if raw.startswith('#'):
                section = raw.lstrip('#').strip().lower()
                continue
            match = re.match(r'^(?:[-*] |(\d+)[.)] )(?:\[([ xX])\] )?(.+)$', raw)
            if not match or match[2] and match[2].lower() == 'x':
                continue
            title = match[3].strip()
            key = fingerprint(filename, title)
            ident_match = re.search(r'\b[A-Z][A-Z0-9]+-[0-9]+\b', title)
            ident = ident_match[0] if ident_match else 'TASK-' + key
            # Date is observation time, not an invented date for the original ask.
            dated = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?: [+-]\d{2}:\d{2})?)', title)
            at = dated[1] if dated and inst.parse_time(dated[1]) else observed
            entries.append({'id': ident, 'title': title, 'quote': raw, 'at': at,
                            'date_basis': 'source-timestamp' if dated else 'file-observed',
                            'ref': filename + ':' + str(line_no), 'origin': filename + ':' + key,
                            'order': int(match[1]) if match[1] else len(entries) + 1,
                            'status': 'backlog', 'source': 'local'})
    for row in _m_jobs.read_jobs(n) or []:
        from . import task_reconcile as _m_reconcile
        state = _m_reconcile.job_evidence(n, row)
        if state['state'] != 'live':
            continue
        key = _m_jobs.job_key(row)
        signal = _m_jobs.read_handback(n, row) or {}
        receipt = {'ref': 'job-ledger.md#' + key, 'scope': row.get('Cause') or row['Job'],
                   'raw': row, 'handback': signal, 'head': row.get('Head commit', '')}
        entries.append({'id': row.get('Task') or 'TASK-' + key.upper(), 'title': 'Work on ' + row['Job'],
                        'quote': row['Job'], 'at': row['Started'], 'date_basis': 'job-start',
                        'ref': 'job-ledger.md#' + key, 'origin': 'job:' + key, 'order': len(entries) + 1,
                        'status': 'in-progress', 'job': key, 'branch': row['Job'],
                        'source': 'local', 'raw_receipts': [receipt], 'reconciliation': False})
    # Exact external IDs collapse duplicate prose references. Open job identity wins.
    unique = {}
    for entry in entries:
        if entry['id'] in unique:
            old = unique[entry['id']]
            entry = {**old, **entry, 'refs': old.get('refs', [old['ref']]) + [entry['ref']]}
        unique[entry['id']] = entry
    return list(unique.values())


def backfill(inst, n, apply=False, link_jobs=True):
    with _m_instance.brain_lock(inst):
        if _m_jobs.read_jobs(n) is None:
            raise ValueError('unrecognized ledger; no backfill written')
        records = _m_tasks.read_records(n)
        requirements = _m_tasks.read_records(n, 'requirements')
        planned = []
        entries = candidates(inst, n)
        known_ids = set(records) | {entry['id'] for entry in entries}
        for entry in entries:
            ident = entry['id']
            origins = {p for r in records.values() for p in r.get('provenance', [])}
            if ident in records or entry['origin'] in origins:
                continue
            # Provider-owned issue references await a pull. Never manufacture a
            # parallel local product issue for an external source.
            if _m_tasks.source(n) == 'linear' and not ident.startswith('TASK-'):
                continue
            req_id = 'REQ-' + fingerprint(entry['origin'])
            ref = entry['ref']
            task = {'version': _m_tasks.VERSION, 'id': ident, 'title': entry['title'], 'kind': 'chore',
                    'status': entry['status'], 'priority': 'P2', 'order': entry['order'],
                    'owner': ','.join(n.get('owners', [])), 'depends_on': [], 'decisions': [], 'evidence': [],
                    'provenance': [entry['origin'], *entry.get('refs', [ref])], 'requirement': req_id,
                    'superseded_by': [], 'scope': [], 'aliases': [], 'unresolved_dependencies': [], 'completion_kind': 'merge', 'last_progress_at': inst.now(), 'strikes': 0, 'source': 'local', 'created': inst.now(), 'updated': inst.now()}
            explicit = re.search(r'\bdepends_on\s*[:=]\s*([A-Za-z0-9_:/,-]+)', entry['title'])
            dependencies = explicit[1].split(',') if explicit else []
            task['depends_on'] = [d for d in dependencies if d in known_ids]
            task['unresolved_dependencies'] = [d for d in dependencies if d not in known_ids]
            for key in ('job', 'branch', 'raw_receipts', 'reconciliation'):
                if key in entry:
                    task[key] = entry[key]
            # Import only explicit links. Ambiguous prose stays backlog.
            task['decisions'] = re.findall(r'\b(?:repo:D[0-9]+|private:D-[A-Za-z0-9-]+)', entry['title'])
            requirement = {'version': _m_tasks.VERSION, 'id': req_id, 'quote': entry['quote'], 'at': entry['at'],
                           'date_basis': entry['date_basis'], 'ref': ref, 'created': inst.now()}
            planned.append((task, requirement))
            records[ident] = task
        # Preflight the whole batch before writing the first record. Requirement
        # existence is checked after staging them in memory, without temp files.
        validation_rows = {ident: {**row, 'requirement': ''} for ident, row in records.items()}
        _m_tasks.validate(n, validation_rows)
        if apply:
            for task, requirement in planned:
                if requirement['id'] not in requirements:
                    _m_tasks.save_record(n, requirement, 'requirements')
                _m_tasks.save_record(n, task)
            jobs = _m_jobs.read_jobs(n) or []
            linked = False
            for row in jobs if link_jobs else []:
                task = next((r for r in records.values() if r.get('job') == _m_jobs.job_key(row) and not row.get('Ended')), None)
                if task and row.get('Task') != task['id']:
                    row['Task'] = task['id']
                    linked = True
            if linked:
                _m_jobs.write_jobs(inst, n, jobs)
            inst.commit([], n['id'] + ': backfill task records')
        return [task for task, _ in planned]


def migrate(inst, nodes):
    """Install/upgrade entry point; additive and safe to repeat on each machine."""
    result = []
    # Fail before touching any node when an existing schema is unsupported.
    for n in nodes:
        for folder in ('tasks', 'requirements', 'rules'):
            _m_tasks.read_records(n, folder)
    for n in nodes:
        # Automatic adoption preserves source ledgers, including parked attempts.
        # Task-side job keys/provenance retain the association; explicit backfill
        # may subsequently attach Task cells without creating duplicate records.
        rows = backfill(inst, n, apply=True, link_jobs=False)
        result.append({'node': n['id'], 'created': len(rows), 'version': _m_tasks.VERSION})
    return result


def cmd_task_migrate(a):
    import json
    from . import nodes as _m_nodes
    inst = _m_nodes.load()
    if a.all and a.node:
        _m_common.die('choose a node or --all')
    nodes = [n for n in inst.nodes() if not n['archived']] if a.all else [inst.node(a.node or 'root')]
    try:
        print(json.dumps(migrate(inst, nodes), ensure_ascii=False))
    except (OSError, ValueError) as error:
        _m_common.die(str(error))


def import_register(inst, n, path, apply=False):
    """Import stopped-attempt evidence as open reconciliation, never as closure.

    Input is a versioned node-keyed JSON register prepared from source evidence.
    Raw receipts remain intact, including contradictory/retired attempts.
    """
    import json
    from pathlib import Path
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('register must be a bounded regular JSON file')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('obligations'), list):
        raise ValueError('unsupported stopped-work register')
    plan = []
    seen_aliases = {}
    seen_sources = set()
    for entry in data['obligations']:
        if entry.get('node') != n['id']:
            continue
        required = ('source_id', 'title', 'source_ref')
        if any(not isinstance(entry.get(k), str) or not entry[k].strip() for k in required):
            raise ValueError('register entry needs source identity, title and reference')
        if entry['source_id'] in seen_sources:
            raise ValueError('duplicate obligation source id; combine its raw receipts before importing')
        seen_sources.add(entry['source_id'])
        receipts = entry.get('receipts')
        if not isinstance(receipts, list) or not receipts or any(not isinstance(r, dict) or not r.get('ref') or not r.get('scope') for r in receipts):
            raise ValueError('stopped-work receipts must retain source references and scope')
        aliases = entry.get('aliases', [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) or not a for a in aliases):
            raise ValueError('invalid stopped-work aliases')
        for alias in aliases:
            if alias in seen_aliases and seen_aliases[alias] != entry['source_id']:
                raise ValueError('one attempt alias cannot be assigned to two obligations')
            seen_aliases[alias] = entry['source_id']
        suffix = hashlib.sha256(entry['source_id'].encode()).hexdigest()[:16].upper()
        ident = 'TASK-' + suffix
        existing = _m_tasks.read_records(n).get(ident)
        if existing:
            if entry['source_id'] not in existing.get('provenance', []):
                raise ValueError('stopped-work identity collision')
            continue
        plan.append((entry, ident))
    if apply:
        with _m_instance.brain_lock(inst):
            for entry, ident in plan:
                row = _m_tasks.upsert_source_task(inst, n, entry['source_id'], entry['title'], kind='research',
                    source_ref=entry['source_ref'], description='Reconcile the preserved scope and receipts before dispatch or closure.')
                row.update(aliases=entry.get('aliases', []), raw_receipts=entry['receipts'], reconciliation=True,
                           next_action=entry.get('next_action') or 'Map each remaining acceptance item to scoped delivery or an explicit decision',
                           completion_kind='report')
                _m_tasks.save_record(n, row)
            inst.commit([], n['id'] + ': retain stopped-attempt reconciliation evidence')
    return [{'task': ident, 'source_id': entry['source_id'], 'status': 'backlog', 'reconciliation': True} for entry, ident in plan]


def delivery_mapping(n, base, head):
    """Bounded Git proof candidates; mappings alone never close an obligation."""
    import subprocess
    if not _m_tasks.SHA.fullmatch(base or '') or not _m_tasks.SHA.fullmatch(head or ''):
        return {'kind': 'unknown', 'reason': 'exact baseline and head are required'}
    if base == head:
        return {'kind': 'unchanged', 'base': base, 'head': head}
    home = n.get('home_path')
    if not home or _m_common.git('merge-base', '--is-ancestor', base, head, cwd=home).returncode:
        return {'kind': 'unknown', 'reason': 'source ancestry is unavailable'}
    paths = _m_common.git('diff', '--name-only', base, head, cwd=home).stdout.splitlines()
    if not paths:
        return {'kind': 'unchanged', 'base': base, 'head': head}
    if not _m_common.git('merge-base', '--is-ancestor', head, 'refs/heads/main', cwd=home).returncode:
        return {'kind': 'exact', 'main_sha': head, 'base': base, 'head': head, 'code_paths': paths}
    commits = _m_common.git('rev-list', '--reverse', base + '..' + head, cwd=home).stdout.splitlines()
    if not commits or len(commits) > 100:
        return {'kind': 'unknown', 'reason': 'source range exceeds the bounded patch comparison'}
    def patch_ids(text):
        result = subprocess.run(['git', 'patch-id', '--stable'], input=text, capture_output=True, text=True, cwd=home)
        if result.returncode:
            return {}
        return dict(line.split()[:2] for line in result.stdout.splitlines() if len(line.split()) >= 2)
    main = _m_common.git('log', '--max-count=500', '--no-merges', '--pretty=format:%H', '--patch', '--binary', 'refs/heads/main', cwd=home)
    main_ids = patch_ids(main.stdout)
    matched = []
    for commit in commits:
        text = _m_common.git('show', '--pretty=format:%H', '--patch', '--binary', commit, cwd=home).stdout
        ids = patch_ids(text)
        if len(ids) != 1 or next(iter(ids)) not in main_ids:
            return {'kind': 'unknown', 'reason': 'not every source patch maps to bounded main history', 'head': head, 'base': base}
        matched.append(main_ids[next(iter(ids))])
    return {'kind': 'patch-equivalent', 'main_sha': matched[-1], 'mapped_commits': matched,
            'base': base, 'head': head, 'code_paths': paths, 'scope': 'patch equivalence; acceptance and runtime proof still required'}
