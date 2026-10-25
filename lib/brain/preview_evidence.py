"""Exact-code preview observations with three source comparisons per presented URL."""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import common as _m_common
from . import guard as _m_guard
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import nodes as _m_nodes


def preview_urls(n):
    urls = []
    if n['local'].get('preview'):
        urls.append(n['local']['preview'])
    extra = n['local'].get('review_urls', [])
    if not isinstance(extra, list) or any(not isinstance(u, str) for u in extra):
        raise ValueError('review_urls must be a list of presented URLs')
    return list(dict.fromkeys(urls + extra))


def preview_receipt_path(inst, n, row, head):
    if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40,64}', head):
        raise ValueError('invalid preview head')
    return inst.root / '.brain/preview-receipts' / hashlib.sha256(n['id'].encode()).hexdigest() / _m_jobs.job_key(row) / (head + '.json')


def validate_preview_receipt(inst, n, row, head, data):
    def refuse():
        raise ValueError('preview receipt needs exact code, current URLs, rendering evidence and three distinct matching source samples per URL')
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1 or
            data.get('node') != n['id'] or data.get('branch') != row['Job'] or
            data.get('job_key') != _m_jobs.job_key(row) or data.get('head') != head):
        refuse()
    checked = inst.parse_time(data.get('checked_at')) if isinstance(data.get('checked_at'), str) else None
    started = inst.parse_time(row.get('Started'))
    if not checked or checked > inst.now_dt() or (started and checked < started):
        refuse()
    if not isinstance(data.get('renderer_threadId'), str) or not data['renderer_threadId'].strip():
        refuse()
    routes = data.get('urls')
    expected = preview_urls(n)
    if (not isinstance(routes, list) or not routes or any(not isinstance(r, dict) for r in routes) or
            {r.get('url') for r in routes if isinstance(r.get('url'), str)} != set(expected) or len(routes) != len(expected)):
        refuse()
    for route in routes:
        try:
            parsed = urlsplit(route['url'])
        except (KeyError, TypeError, ValueError):
            refuse()
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or route.get('rendered') is not True:
            refuse()
        if not isinstance(route.get('render_evidence'), str) or not route['render_evidence'].strip():
            refuse()
        items = route.get('items')
        if not isinstance(items, list) or len(items) < 3:
            refuse()
        pages, sources = set(), set()
        for item in items:
            if (not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip()
                    for k in ('page_item', 'source_ref', 'source_item')) or item['page_item'] != item['source_item']):
                refuse()
            pages.add(item['page_item'])
            sources.add((item['source_ref'], item['source_item']))
        if len(pages) < 3 or len(sources) < 3:
            refuse()
    return data


def record_preview_receipt(inst, n, branch, data):
    with _m_instance.brain_lock(inst):
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        if not row:
            raise ValueError('preview receipt requires an open job')
        repo = n.get('home_path') if n.get('external') else inst.root
        head = _m_common.git('rev-parse', '--verify', 'refs/heads/' + branch + '^{commit}', cwd=repo).stdout.strip()
        validate_preview_receipt(inst, n, row, head, data)
        serialized = json.dumps(data, sort_keys=True) + '\n'
        _m_guard.no_terms_in(n, serialized, 'preview sample receipt')
        path = preview_receipt_path(inst, n, row, head)
        _m_common.safe_target(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _m_common.atomic_write(path, serialized)
        return path


def validate_ready_preview(inst, n, branch, head, packet=None):
    try:
        urls = preview_urls(n)
        required = n['fm'].get('requires_preview_evidence', inst.cfg.get('requires_preview_evidence', '')).lower()
        if not urls and required not in ('true', 'yes', '1', 'on'):
            return []
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        if not row:
            return ['preview source samples require an open job']
        path = preview_receipt_path(inst, n, row, head)
        validate_preview_receipt(inst, n, row, head, json.loads(path.read_text()))
    except (OSError, ValueError, TypeError, KeyError):
        return ['missing or invalid exact-head preview rendering and three-source-sample receipt']
    return []


def cmd_preview_receipt(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    try:
        data = json.loads(Path(a.file).expanduser().read_text())
        record_preview_receipt(inst, n, a.branch, data)
    except (OSError, ValueError, TypeError) as error:
        _m_common.die(str(error))
    print('preview source-sample receipt recorded (supplied browser observation; no live browser check by this command)')
