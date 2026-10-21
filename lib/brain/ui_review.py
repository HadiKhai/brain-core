"""Explicit, local, scope-preserving proposal reviews. No work is performed by polling."""
import difflib
import hashlib
import hmac
import json
import secrets
import threading
import time

from . import common, guard, instance, knowledge


class ReviewError(ValueError):
    pass


def authorized_write(headers, client, port, key):
    """The preview key is not sufficient: a browser must also prove the exact same origin."""
    host = headers.get('Host', '')
    origin = headers.get('Origin', '')
    return (client == '127.0.0.1' and host in (f'127.0.0.1:{port}', f'localhost:{port}')
            and origin == 'http://' + host
            and headers.get('Sec-Fetch-Site', 'same-origin') == 'same-origin'
            and headers.get('Content-Type', '').split(';')[0].strip() == 'application/json'
            and hmac.compare_digest(headers.get('X-Brain-Review', ''), key))


class Reviews:
    TTL = 300

    def __init__(self, root, enabled=False):
        self.root, self.enabled = root, enabled
        self.lock = threading.Lock()  # brain_lock is process-reentrant, so serialize HTTP writers first.
        self.pending = {}

    def catalog(self, inst=None):
        if not self.enabled:
            return {'enabled': False, 'proposals': []}
        inst = inst or instance.Instance(self.root)
        reviewer = inst.cfg.get('operator', '')
        knowledge.review_principal(inst, reviewer)
        rows = []
        nodes = inst.nodes()
        for it in inst.items():
            if it['fm'].get('status') != 'proposed':
                continue
            at = next((n for n in nodes if n['dir'].resolve() == it['path'].parent.parent.resolve()), None)
            if at:
                rows.append(self.describe(inst, 'item', it['fm'].get('id') or it['path'].stem, at, it['path']))
        for sp in knowledge.find_specialists(inst):
            if sp['fm'].get('status') == 'proposed':
                rows.append(self.describe(inst, 'specialist', sp['id'], sp['node'], sp['dir'] / 'role.md'))
        rows = [r for r in rows if r is not None]
        # A source policy can redact the displayed title; identities remain exact opaque selectors.
        return {'enabled': True, 'reviewer': reviewer, 'proposals': rows}

    def describe(self, inst, kind, ident, node, path):
        common.safe_target(path)
        fm, _ = common.read_fm(path)
        terms = (knowledge.learning_terms(inst, fm, [node]) if kind == 'item'
                 else knowledge.specialist_terms(inst, {'dir': path.parent}, [node]))
        if guard.find_terms(json.dumps([ident, node['id'], str(path.relative_to(inst.root)), fm.get('scope', '')]), terms):
            return None  # An unsafe identity cannot be round-tripped through the screened dashboard.
        return {'kind': kind, 'id': ident, 'node': node['id'],
                'title': guard.redact(fm.get('title') or ident, terms),
                'scope': common.lst(fm.get('scope')), 'path': str(path.relative_to(inst.root)),
                'revision': hashlib.sha256(path.read_bytes()).hexdigest()}

    @staticmethod
    def identity(row):
        return row['kind'], row['node'], row['id']

    def prepare(self, payload):
        if not self.enabled:
            raise ReviewError('Write mode is off. Restart brain ui --write to review proposals here.')
        action, requested = payload.get('action'), payload.get('records')
        if action not in ('accept', 'reject') or not isinstance(requested, list) or not 1 <= len(requested) <= 1000:
            raise ReviewError('Select 1–1000 proposals and an accept or reject action.')
        with self.lock, instance.brain_lock(self.root):
            current = self.catalog()
            available = {self.identity(r): r for r in current['proposals']}
            try:
                keys = [self.identity(r) for r in requested]
                if len(set(keys)) != len(keys):
                    raise KeyError()
                records = [available[k] for k in keys]
            except (KeyError, TypeError):
                raise ReviewError('A selected proposal is no longer available. Refresh and review it again.')
            now = time.monotonic()
            self.pending = {k: v for k, v in self.pending.items() if now - v['at'] < self.TTL}
            if len(self.pending) >= 64:
                raise ReviewError('Too many pending confirmations; wait five minutes and try again.')
            token = secrets.token_urlsafe(24)
            self.pending[token] = {'at': now, 'records': records, 'action': action, 'reviewer': current['reviewer']}
            return {'token': token, 'action': action, 'records': records, 'reviewer': current['reviewer']}

    def apply(self, payload):
        if not self.enabled:
            raise ReviewError('Write mode is off.')
        token = payload.get('token')
        if not isinstance(token, str):
            raise ReviewError('A confirmation token is required.')
        with self.lock, instance.brain_lock(self.root):
            prepared = self.pending.pop(token, None)  # one use, including failed or stale writes
            if prepared is None or time.monotonic() - prepared['at'] >= self.TTL:
                raise ReviewError('This confirmation expired or was already used. Review again.')
            inst = instance.Instance(self.root)
            current = self.catalog(inst)
            available = {self.identity(r): r for r in current['proposals']}
            if current['reviewer'] != prepared['reviewer'] or any(
                    available.get(self.identity(r)) != r for r in prepared['records']):
                raise ReviewError('A proposal or its scope changed. Nothing was written. Review again.')
            paths = [self.root / r['path'] for r in prepared['records']]
            before = {p: p.read_text() for p in paths}
            previous = set(common.TOUCHED)
            try:
                common.TOUCHED.clear()
                knowledge.review_proposals(inst, prepared['records'], prepared['action'],
                                           prepared['reviewer'], via='local UI')
            finally:
                common.TOUCHED.clear()
                common.TOUCHED.update(previous)
            changes = []
            for path in paths:
                name = str(path.relative_to(self.root))
                # The diff contains only this reviewed proposal, screened under its source policy.
                record = next(r for r in prepared['records'] if r['path'] == name)
                node = inst.node(record['node'])
                fm, _ = common.read_fm(path)
                terms = (knowledge.learning_terms(inst, fm, [node]) if record['kind'] == 'item'
                         else knowledge.specialist_terms(inst, {'dir': path.parent}, [node]))
                diff = ''.join(difflib.unified_diff(before[path].splitlines(True), path.read_text().splitlines(True),
                                                   fromfile=name + ' before', tofile=name + ' after'))
                changes.append({'path': name, 'diff': guard.redact(diff, terms)})
            return {'action': prepared['action'], 'reviewer': prepared['reviewer'], 'via': 'local UI',
                    'count': len(changes), 'changes': changes,
                    'commit': common.git('rev-parse', 'HEAD', cwd=self.root).stdout.strip()}
