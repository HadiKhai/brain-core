"""Exact-code preview observations with three source comparisons per presented URL."""
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import common as _m_common
from . import guard as _m_guard
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import ui as _m_ui


def preview_urls(n):
    urls = []
    if n['local'].get('preview'):
        urls.append(n['local']['preview'])
    extra = n['local'].get('review_urls', [])
    if isinstance(extra, str):
        extra = json.loads(extra) if extra.strip() else []
    if not isinstance(extra, list) or any(not isinstance(u, str) for u in extra):
        raise ValueError('review_urls must be a list of presented URLs')
    return list(dict.fromkeys(urls + extra))


def preview_receipt_path(inst, n, row, head):
    if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40,64}', head):
        raise ValueError('invalid preview head')
    return inst.root / '.brain/preview-receipts' / hashlib.sha256(n['id'].encode()).hexdigest() / _m_jobs.job_key(row) / (head + '.json')


def job_preview_binding(inst, n, row, head):
    """Read current authority, never a caller's cached worktree or job declarations.

    A port block authorizes loopback URLs only. An exact URL can also be declared
    in the ledger's JSON ``Preview URLs`` cell or the contract's matching section.
    Physical directory identities prevent receipt reuse after path recreation.
    """
    def refuse():
        raise ValueError('job preview requires a current bound worktree at this head')

    def query(path, *args):
        # Receipt authority is live even inside a dashboard Git-read snapshot.
        result = _m_common.git(*args, cwd=path, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
        if result.returncode or not result.stdout.strip():
            refuse()
        return result.stdout.strip()

    def identity(path):
        stat = path.stat()
        return [stat.st_dev, stat.st_ino]

    try:
        live = _m_instance.Instance(inst.root)
        current_node = next((node for node in live.nodes() if node['id'] == n['id']), None)
        if current_node is None:
            refuse()
        current = _m_jobs.open_job(_m_jobs.read_jobs(current_node) or [], row['Job'])
        if not current or _m_jobs.job_key(current) != _m_jobs.job_key(row):
            refuse()
        worktree = current_node['local'].get('worktrees', {}).get(row['Job'])
        if (not current_node['external'] or not current_node.get('home_path') or
                not isinstance(worktree, dict)):
            refuse()
        created = live.parse_time(worktree.get('created'))
        if not created or created > live.now_dt() or not worktree.get('start'):
            refuse()
        path = Path(worktree['path']).expanduser().resolve(strict=True)
        home = current_node['home_path'].resolve(strict=True)
        if (Path(query(path, 'rev-parse', '--show-toplevel')).resolve() != path or
                query(path, 'symbolic-ref', 'HEAD') != 'refs/heads/' + row['Job'] or
                query(path, 'rev-parse', 'HEAD') != head or
                query(home, 'rev-parse', '--verify', 'refs/heads/' + row['Job'] + '^{commit}') != head):
            refuse()
        common = (path / query(path, 'rev-parse', '--git-common-dir')).resolve(strict=True)
        if common != (home / query(home, 'rev-parse', '--git-common-dir')).resolve(strict=True):
            refuse()
        admin = (path / query(path, 'rev-parse', '--git-dir')).resolve(strict=True)
        port, count = worktree.get('port'), worktree.get('ports', 1)
        if (type(port) is not int or type(count) is not int or
                port < 1 or count < 1 or port + count - 1 > 65535):
            refuse()
        declared = json.loads(current.get('Preview URLs') or '[]')
        if not isinstance(declared, list) or any(not isinstance(url, str) for url in declared):
            refuse()
        contract = _m_jobs.contract_file(current_node, current)
        if contract.exists():
            sections = {key.lower(): value for key, value in _m_ui.md_sections(contract.read_text()).items()}
            declared += [re.sub(r'^[-*+]\s+', '', line.strip()).strip('`')
                         for line in sections.get('preview urls', '').splitlines() if line.strip()]
        generation = _m_jobs.digest([_m_jobs.job_key(current), str(home), str(path),
                                    worktree['created'], worktree['start'], port, count,
                                    identity(path), str(admin), identity(admin),
                                    str(common), identity(common), sorted(set(declared))])
        return {'generation': generation, 'created': created, 'port': port,
                'ports': count, 'urls': declared}
    except (OSError, TypeError, KeyError, RuntimeError) as error:
        raise ValueError('job preview binding unavailable') from error


def job_preview_generation(inst, n, row, head):
    """Generation token to include in an explicit ``scope: job`` receipt."""
    return job_preview_binding(inst, n, row, head)['generation']


def job_preview_url(url, binding):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
        return False
    if url in binding['urls']:
        return True
    return (parsed.hostname in ('localhost', '127.0.0.1', '::1') and
            parsed.port is not None and binding['port'] <= parsed.port < binding['port'] + binding['ports'])


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
    if data.get('scope') == 'job':
        binding = job_preview_binding(inst, n, row, head)
        if data.get('worktree_generation') != binding['generation'] or checked < binding['created']:
            refuse()
        if (not isinstance(routes, list) or not routes or any(not isinstance(route, dict) or
                not isinstance(route.get('url'), str) or not job_preview_url(route['url'], binding)
                for route in routes)):
            refuse()
        expected = list(dict.fromkeys(route['url'] for route in routes))
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
        inst = _m_instance.Instance(inst.root)
        n = inst.node(n['id'])
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        if not row:
            raise ValueError('preview receipt requires an open job')
        repo = n.get('home_path') if n.get('external') else inst.root
        if repo is None or not repo.is_dir():
            raise ValueError('preview receipt requires a bound home')
        paths = lambda: [n['dir'] / _m_jobs.JOBS_FILE, *_m_instance.git_state_paths(repo)]
        with _m_instance.released(inst, n, extra=paths):
            head = _m_common.git('rev-parse', '--verify', 'refs/heads/' + branch + '^{commit}', cwd=repo).stdout.strip()
            validate_preview_receipt(inst, n, row, head, data)
            serialized = json.dumps(data, sort_keys=True) + '\n'
            _m_guard.no_terms_in(n, serialized, 'preview sample receipt')
        path = preview_receipt_path(inst, n, row, head)
        _m_common.safe_target(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _m_common.atomic_write(path, serialized, private=True)
        return path


def validate_ready_preview(inst, n, branch, head, packet=None):
    try:
        urls = preview_urls(n)
        cfg = inst.cfg if isinstance(inst.cfg, dict) else {}
        required = str(n['fm'].get('requires_preview_evidence', cfg.get('requires_preview_evidence', ''))).lower()
        if not urls and required not in ('true', 'yes', '1', 'on'):
            # Optional previews remain optional, but a supplied job receipt must
            # still be checked against the current binding on every read.
            optional_row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
            optional_path = preview_receipt_path(inst, n, optional_row, head) if optional_row else None
            if not optional_path or not optional_path.exists():
                return []
            optional_data = json.loads(optional_path.read_text())
            if not isinstance(optional_data, dict) or optional_data.get('scope') != 'job':
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
    _m_instance.unlock()
    inst = _m_nodes.load()
    n = inst.node(a.id)
    try:
        data = json.loads(Path(a.file).expanduser().read_text())
        record_preview_receipt(inst, n, a.branch, data)
    except (OSError, ValueError, TypeError) as error:
        _m_common.die(str(error))
    print('preview source-sample receipt recorded (supplied browser observation; no live browser check by this command)')
