"""Rules-only snapshot activation using explicit coordinator-supplied evidence."""
import hashlib
import json
import re
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import instance as _m_instance


RULES_BEGIN = '<!-- brain:imported-rules begin -->'
RULES_END = '<!-- brain:imported-rules end -->'
REVIEW_MARK = '<!-- brain:rule-activation '


def rule_source(n):
    return n['dir'] / 'sources/import-proposals.md'


def _read_snapshot(n):
    path = rule_source(n)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    if not raw.strip():
        return None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        data = None
    return raw, digest, data


def _validate_review(inst, n, snapshot, manifest):
    """Validate the entire supplied decision before rendering or mutating instructions."""
    from . import nodes as _m_nodes
    _, digest, data = snapshot
    if not isinstance(data, dict) or not isinstance(manifest, dict):
        raise ValueError('unsupported rules snapshot; ask the coordinator to prepare the documented rules-only JSON format')
    fields = {'version', 'node', 'scope', 'importer', 'importer_runtime', 'provenance', 'rules'}
    if set(data) != fields or type(data.get('version')) is not int or data.get('version') != 1 or data.get('node') != n['id']:
        raise ValueError('unsupported rules-only snapshot or node')
    if set(manifest) != fields | {'snapshot_hash', 'reviewer', 'reviewer_runtime', 'reviewed_at'}:
        raise ValueError('complete supplied evidence is required')
    if type(manifest['version']) is not int or manifest['snapshot_hash'] != digest or any(manifest[k] != data[k] for k in fields):
        raise ValueError('supplied evidence does not cover the complete current snapshot')
    for field in ('importer', 'importer_runtime', 'provenance', 'reviewer', 'reviewer_runtime', 'reviewed_at'):
        value = manifest[field]
        if not isinstance(value, str) or not value.strip() or any(c in value for c in '\n\r\x00'):
            raise ValueError('missing or invalid supplied evidence field: ' + field)
    reviewer = manifest['reviewer']
    if data['importer'] in inst.revoked:
        raise ValueError('revoked importing actor')
    if reviewer not in ({inst.cfg.get('operator')} | set(n['owners'])) or reviewer in inst.revoked:
        raise ValueError('reviewer is not a current authorized non-revoked principal')
    if reviewer == data['importer'] or manifest['reviewer_runtime'] == data['importer_runtime']:
        raise ValueError('self-approval by importer or importing runtime is refused')
    if any(manifest[k] not in inst.runtimes for k in ('importer_runtime', 'reviewer_runtime')):
        raise ValueError('unknown review/import runtime')
    when = inst.parse_time(manifest['reviewed_at'])
    if not when or when > inst.now_dt():
        raise ValueError('invalid or future review time')
    scope = data['scope']
    if not isinstance(scope, list) or len(scope) != len(set(scope)) or set(scope) != set(n['owners']) or set(scope) & set(inst.revoked):
        raise ValueError('rule scope must exactly match current non-revoked node owners')
    rules = data['rules']
    if not isinstance(rules, list) or not rules:
        raise ValueError('complete nonempty rules selection required')
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {'id', 'text', 'source'}:
            raise ValueError('rules only: each rule needs id, text and source')
        if any(not isinstance(rule[k], str) or not rule[k].strip() for k in rule):
            raise ValueError('empty rule field')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', rule['id']) or rule['id'] in seen:
            raise ValueError('ambiguous rule selection')
        if any(x in rule['text'] + rule['source'] for x in ('<!--', '-->', '\x00', '\r')):
            raise ValueError('unsafe rule instruction marker')
        if not rule['source'].startswith(data['provenance'] + ':'):
            raise ValueError('rule source lies outside the supplied provenance')
        seen.add(rule['id'])
    if any(x in json.dumps(manifest) for x in ('<!--', '-->')):
        raise ValueError('unsafe supplied evidence marker')
    if _m_guard.find_terms(json.dumps(manifest, ensure_ascii=False), _m_guard.never_terms(n)):
        raise ValueError('rules or review attribution holds a never-name word')
    if _m_nodes.SECRET_LINE.search(json.dumps(manifest, ensure_ascii=False)):
        raise ValueError('rules or review attribution holds unsafe secret metadata')


def _activation_block(manifest):
    lines = [RULES_BEGIN, REVIEW_MARK + json.dumps(manifest, sort_keys=True, ensure_ascii=False) + ' -->',
             '## Reviewed imported rules', '',
             'Review: supplied evidence; this record does not authenticate identity.', '']
    for rule in manifest['rules']:
        lines += ['### ' + rule['id'], rule['text'], '', 'Source: ' + rule['source'], '']
    return '\n'.join(lines + [RULES_END])


def _legacy_rule_rejection(inst, raw):
    """Preserve the legacy explicit rejection acknowledgement, without activating rules."""
    try:
        # Match read_fm's universal-newline decoding while preserving raw snapshot bytes.
        text = raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
        match = re.match(r'^---\n(.*?)\n---\n?', text, re.S)
    except UnicodeError:
        return False
    if not match:
        return False
    fields = dict(line.split(':', 1) for line in match[1].splitlines() if ':' in line)
    fields = {key.strip(): value.strip() for key, value in fields.items()}
    actor = fields.get('reviewed_by')
    when = inst.parse_time(fields.get('reviewed_at', ''))
    authorized = ({inst.cfg.get('operator')} | set(inst.principals)) - set(inst.revoked)
    return bool(fields.get('status') == 'rejected' and actor in authorized and when and when <= inst.now_dt())


def pending_rule_snapshot(inst, n):
    try:
        snapshot = _read_snapshot(n)
    except OSError:
        return {'digest': 'unreadable', 'count': 1, 'path': str(rule_source(n).relative_to(inst.root))}
    if snapshot is None:
        return None
    raw, digest, data = snapshot
    if _legacy_rule_rejection(inst, raw):
        return None
    count = len(data['rules']) if isinstance(data, dict) and isinstance(data.get('rules'), list) else 1
    path = n['dir'] / 'AGENTS.md'
    text = path.read_text() if path.exists() else ''
    markers = re.findall(re.escape(REVIEW_MARK) + r'(.*?) -->', text)
    if len(markers) == 1:
        try:
            manifest = json.loads(markers[0])
            _validate_review(inst, n, snapshot, manifest)
            if text.count(RULES_BEGIN) == text.count(RULES_END) == 1 and _activation_block(manifest) in text:
                return None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            pass
    return {'digest': digest, 'count': max(1, count), 'path': str(rule_source(n).relative_to(inst.root))}


def activate_rules(inst, n, manifest, commit=True):
    with _m_instance.brain_lock(inst):
        inst = _m_instance.Instance(inst.root)
        n = inst.node(n['id'])
        snapshot = _read_snapshot(n)
        if snapshot is None:
            raise ValueError('no pending rule snapshot')
        _validate_review(inst, n, snapshot, manifest)
        target = n['dir'] / 'AGENTS.md'
        _m_common.safe_target(target)
        text = target.read_text() if target.exists() else ''
        block = _activation_block(manifest)
        if RULES_BEGIN in text or RULES_END in text:
            if text.count(RULES_BEGIN) != 1 or text.count(RULES_END) != 1 or text.index(RULES_BEGIN) > text.index(RULES_END):
                raise ValueError('ambiguous activation destination; ask the coordinator')
            start, end = text.index(RULES_BEGIN), text.index(RULES_END) + len(RULES_END)
            text = text[:start] + block + text[end:]
        else:
            text = text.rstrip() + '\n\n' + block + '\n'
        # Recheck the source after validation, including non-native import writers.
        if _read_snapshot(n) != snapshot:
            raise ValueError('rule snapshot changed during review; no activation')
        # Instructions, provenance and acknowledgement share one atomic replacement.
        _m_common.atomic_write(target, text)
        _m_common.touch(target)
        if commit:
            inst.commit([target], n['id'] + ': activate reviewed rule snapshot')
        return target


def cmd_proposal_activation(a):
    from . import nodes as _m_nodes
    inst = _m_nodes.load()
    if not a.id or a.only or a.json or not a.review_manifest:
        _m_common.die('--approve-all requires one node and --review-manifest, without --only/--json')
    try:
        manifest = json.loads(Path(a.review_manifest).read_text())
        target = activate_rules(inst, inst.node(a.id), manifest)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        _m_common.die(str(error))
    print(a.id + ': activated complete rules snapshot from supplied evidence in ' + target.name)
