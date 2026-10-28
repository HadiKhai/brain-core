"""Exact-code preview observations with three source comparisons per presented URL."""
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import common as _m_common
from . import instance as _m_instance

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_ui = _m_common.LazyModule(__package__, 'ui')
_m_lanes = _m_common.LazyModule(__package__, 'verify_lanes')


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
                'ports': count, 'urls': declared, 'home': str(home),
                'worktree': str(path), 'start': worktree['start']}
    except (OSError, TypeError, KeyError, RuntimeError) as error:
        raise ValueError('job preview binding unavailable') from error


def job_preview_generation(inst, n, row, head):
    """Generation token to include in an explicit ``scope: job`` receipt."""
    return job_preview_binding(inst, n, row, head)['generation']


def preview_receipt_skeleton(inst, node_id, branch):
    """Read current job authority without recording a browser observation.

    Re-read before returning to refuse authority that changes during collection.
    The recorder remains responsible for validating authority at publication.
    """
    def collect():
        live = _m_instance.Instance(inst.root)
        n = live.node(node_id)
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        if not row:
            raise ValueError('preview receipt skeleton requires an open job')
        repo = n.get('home_path') if n.get('external') else None
        if repo is None or not repo.is_dir():
            raise ValueError('preview receipt skeleton requires a bound external home')
        result = _m_common.git('rev-parse', '--verify', 'refs/heads/' + branch + '^{commit}',
                               cwd=repo, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
        head = result.stdout.strip()
        if result.returncode or not re.fullmatch(r'[0-9a-f]{40,64}', head):
            raise ValueError('preview receipt skeleton requires a current job branch head')
        binding = job_preview_binding(live, n, row, head)
        data = {'version': 1, 'scope': 'job', 'node': node_id,
                'job_key': _m_jobs.job_key(row), 'branch': branch, 'head': head,
                'worktree_generation': binding['generation'],
                'binding': {**{key: binding[key] for key in
                              ('home', 'worktree', 'start', 'port', 'ports', 'urls')},
                            'created': binding['created'].isoformat()},
                'checked_at': None, 'renderer_threadId': None,
                'urls': [{'url': 'http://127.0.0.1:' + str(binding['port']) + '/',
                          'rendered': None, 'render_evidence': None,
                          'items': [{'page_item': None, 'source_ref': None, 'source_item': None}
                                    for _ in range(3)]}]}
        return data, n, row

    data, n, row = collect()
    roots = [Path(data['binding'][key]) for key in ('home', 'worktree')]

    def signature():
        # Fence the final collection too: it may have loaded local/ledger data
        # before a later Git query observes an authority mutation. Signature
        # reads do not take a lock or publish a cache/observation.
        live = _m_instance.Instance(inst.root)
        current_node = live.node(node_id)
        worktree = current_node['local'].get('worktrees', {}).get(branch)
        if not current_node.get('home_path') or not isinstance(worktree, dict):
            raise ValueError('preview receipt skeleton authority changed during read; retry')
        logical_roots = [current_node['home_path'], Path(worktree['path']).expanduser()]
        paths = [n['dir'] / _m_jobs.JOBS_FILE, _m_jobs.contract_file(n, row)]
        for root in roots:
            paths.extend(_m_instance.git_state_paths(root))
        identities = []
        # Canonical roots alone miss retargeted logical paths or their ancestors.
        for root in [*roots, *logical_roots]:
            resolved = root.resolve(strict=True)
            stat = resolved.stat()
            identities.append((str(root), str(resolved), stat.st_dev, stat.st_ino))
        return _m_instance.state_signature(live, current_node, extra=paths), identities

    before = signature()
    current, _, _ = collect()
    if signature() != before or current != data:
        raise ValueError('preview receipt skeleton authority changed during read; retry')
    return data


def cmd_preview_receipt_skeleton(a):
    try:
        # load() registers an instance on first use; this accessor must never write.
        inst = _m_instance.Instance(_m_nodes.find_root())
        data = preview_receipt_skeleton(inst, a.id, a.branch)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        _m_common.die(str(error))
    print(json.dumps(data, indent=2, sort_keys=True))


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


def preview_requirement(inst, n, branch, head, packet=None):
    """Classify the exact integration diff; uncertainty always requires a receipt.

    UI declarations describe rendered inputs, including shared dependencies. Lane
    names alone are not enough unless an explicit ui/rendered lane exists.
    """
    evidence = dict(status='required', reason='preview classification uncertain',
                    head=head, base=None, paths=[], source=None, globs=[])
    try:
        cfg = inst.cfg if isinstance(inst.cfg, dict) else {}
        required = str(n['fm'].get('requires_preview_evidence', cfg.get('requires_preview_evidence', ''))).lower()
        if not preview_urls(n) and required not in ('true', 'yes', '1', 'on'):
            return {**evidence, 'status': 'optional', 'reason': 'preview is optional'}
        # Read live policy and refs, even within a dashboard's Git read snapshot.
        # Fence before loading any policy: a post-load fence can bless stale data.
        policy_paths = [n['dir'] / _m_jobs.JOBS_FILE]
        policy_before = _m_instance.state_signature(inst, n, extra=policy_paths)
        live = _m_instance.Instance(inst.root)
        n = next((node for node in live.nodes() if node['id'] == n['id']), None)
        if n is None:
            return evidence
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        if not row or not n.get('external') or not n.get('home_path'):
            return evidence
        repo = n['home_path']
        env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
        from . import verify as _m_verify
        object_query = _m_verify.immutable_git_query(repo)

        def query(*args):
            result = object_query(*args)
            if result.returncode or '\ufffd' in result.stdout:
                raise ValueError('unreadable preview diff')
            return result.stdout

        if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head):
            return evidence
        if query('rev-parse', '--verify', 'refs/heads/' + branch + '^{commit}').strip() != head:
            return evidence
        ref = _m_verify.integration_ref(repo, env=env)
        if not ref:
            return evidence
        if n['fm'].get('merge', 'local') != 'pr':
            ref = ref.replace('refs/remotes/origin/', 'refs/heads/', 1)
        current_base = query('rev-parse', '--verify', ref + '^{commit}').strip()
        # A train may retain an older integration base. It must be an ancestor
        # of today's base, never a caller-selected newer/narrower diff.
        base = (packet or {}).get('integration_base', current_base)
        if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', base):
            return evidence
        query('merge-base', '--is-ancestor', base, current_base)
        query('merge-base', '--is-ancestor', base, head)
        evidence['base'] = base
        # Classification needs the full path set, not similarity scores. A
        # rename's delete/add pair retains both boundary paths and avoids
        # mutable worktree attributes influencing rename similarity.
        raw = query('diff', '--name-status', '-z', '--no-renames', '--no-ext-diff',
                    '--no-textconv', '--ignore-submodules=none', base, head, '--')
        fields = raw.split('\0')
        if fields.pop() != '':
            raise ValueError('unterminated preview diff')
        paths = []
        while fields:
            status = fields.pop(0)
            count = 2 if re.fullmatch(r'[RC][0-9]+', status) else 1
            if not re.fullmatch(r'[AMDT]|[RC][0-9]+', status) or len(fields) < count:
                raise ValueError('unknown preview diff status')
            for _ in range(count):
                path = fields.pop(0)
                if not path or path.startswith('/') or any(p in ('', '.', '..') for p in path.split('/')):
                    raise ValueError('invalid preview diff path')
                paths.append(path)
        evidence['paths'] = sorted(set(paths))
        groups = []
        if 'ui_globs' in n['fm']:
            raw_globs = n['fm']['ui_globs']
            if not isinstance(raw_globs, str):
                return evidence
            patterns = (json.loads(raw_globs) if raw_globs.strip().startswith('[')
                        else _m_common.lst(raw_globs))
            groups.append(patterns)
            evidence['source'] = 'ui_globs'
        else:
            w = n['local'].get('worktrees', {}).get(branch)
            if not isinstance(w, dict):
                return evidence
            lane_env = _m_verify.verify_env(n, branch, w)
            if 'verify_lane_env' in (packet or {}):
                lane_env = _m_lanes.recorded_lane_environment(lane_env, packet['verify_lane_env'])
            elif (packet or {}).get('version') == 1 and n['fm'].get('verify_lanes_from'):
                return evidence  # Legacy full packets did not retain the map environment.
            scope = (packet or {}).get('verify_scope', 'full')
            lane_env.update(BRAIN_ROOT=str(inst.root), BRAIN_VERIFY_BASE=base,
                            BRAIN_VERIFY_HEAD=head, BRAIN_VERIFY_SCOPE=scope)
            lanes, _ = _m_lanes.lane_map(n, w, lane_env)
            selected = {lane['name'] for lane in lanes if lane['name'] in ('ui', 'rendered')}
            if not selected:
                return evidence
            while True:
                expanded = selected | {name for lane in lanes if lane['name'] in selected for name in lane['requires']}
                if expanded == selected:
                    break
                selected = expanded
            evidence['source'] = 'lanes:' + ','.join(sorted(selected))
            for lane in lanes:
                if lane['name'] in selected:
                    if lane['always']:
                        return {**evidence, 'reason': 'rendered lane always applies'}
                    groups.extend([lane['inputs'], lane['strict_inputs']])
        if not groups or not any(groups):
            return evidence
        for patterns in groups:
            if not isinstance(patterns, list) or any(not isinstance(p, str) for p in patterns):
                return evidence
            if patterns and not any(not p.startswith('!') for p in patterns):
                return evidence
            for pattern in patterns:
                _m_lanes.glob_regex(pattern)
        evidence['globs'] = groups
        if (query('rev-parse', '--verify', 'refs/heads/' + branch + '^{commit}').strip() != head or
                query('rev-parse', '--verify', ref + '^{commit}').strip() != current_base):
            return {**evidence, 'reason': 'preview classification refs changed'}
        current = _m_instance.Instance(inst.root)
        current_node = next((node for node in current.nodes() if node['id'] == n['id']), None)
        if current_node is None or _m_instance.state_signature(current, current_node, extra=policy_paths) != policy_before:
            return {**evidence, 'reason': 'preview classification policy changed'}
        if any(_m_lanes.matches(path, patterns) for path in paths for patterns in groups):
            return {**evidence, 'reason': 'rendered paths touched'}
        return {**evidence, 'status': 'not_applicable',
                'reason': 'preview: not applicable, no rendered paths touched'}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError):
        return evidence


def validate_ready_preview(inst, n, branch, head, packet=None):
    try:
        scope = preview_requirement(inst, n, branch, head, packet)
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
        # A supplied receipt remains authoritative even for a backend-only diff:
        # malformed or stale observations must never become an exemption.
        if scope['status'] == 'not_applicable' and not path.exists():
            return []
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
