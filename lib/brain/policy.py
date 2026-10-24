"""Explicit reviewed publication decisions; prose is never interpreted as an ACL."""
import json

from . import knowledge as _m_knowledge
from . import guard as _m_guard


def _policy_text(n, text):
    from . import nodes as _m_nodes
    return _m_nodes.import_text(text, _m_guard.never_terms(n))


def limit_snapshot(n):
    chain, out = [], []
    while n:
        chain.append(n)
        if n.get('sealed'):
            break
        n = n.get('parent')
    for node in reversed(chain):
        for index, text in enumerate(_m_knowledge.limits_of(node), 1):
            out.append({'source': node['id'] + '/brief.md#Limits:' + str(index), 'text': text})
    return out


def publication_decision(inst, n, action):
    """Return allow/deny/unknown and its explanation. An allow names this node and action."""
    if action not in ('push', 'pr'):
        raise ValueError('unsupported publication action; ask the coordinator')
    limits = limit_snapshot(n)
    detail = 'ask the coordinator for an explicit reviewed ' + action + ' policy'
    if limits:
        detail += '; unmapped limits: ' + '; '.join('"' + r['text'] + '" (' + r['source'] + ')' for r in limits)
    unknown = {'decision': 'unknown', 'reason': _policy_text(n, detail)}
    path = n['dir'] / 'publication-policy.json'
    try:
        data = json.loads(path.read_text())
        reviewer = data.get('reviewer')
        reviewed_at = inst.parse_time(data.get('reviewed_at'))
        authorized = {inst.cfg.get('operator')} | set(n['owners'])
        if (type(data.get('version')) is not int or data.get('version') != 1 or data.get('node') != n['id'] or reviewer not in authorized
                or reviewer in inst.revoked or not reviewed_at or reviewed_at > inst.now_dt()):
            return unknown
        rules = data['rules']
        if not isinstance(rules, list) or [{'source': r['source'], 'text': r['text']} for r in rules] != limits:
            return unknown
        decisions = []
        for rule in rules:
            decision = rule['actions'].get(action, 'unknown')
            if decision not in ('allow', 'deny', 'unknown', 'not-applicable'):
                return unknown
            decisions.append(decision)
            if decision == 'deny':
                return {'decision': 'deny', 'reason': _policy_text(n, 'refused by "' + rule['text'] + '" (' + rule['source'] + ')')}
        if 'unknown' in decisions:
            return unknown
        # Standing consent is explicit and scoped; merely selecting merge=pr never supplies it.
        consent = data.get('consent', {})
        if 'consent' in data and (not isinstance(consent, dict) or set(consent) != {'node', 'actions'}
                        or consent.get('node') != n['id'] or not isinstance(consent.get('actions'), list)
                        or not consent['actions'] or any(a not in ('push', 'pr') for a in consent['actions'])
                        or len(consent['actions']) != len(set(consent['actions']))):
            return unknown
        if 'allow' in decisions or (consent.get('node') == n['id'] and action in consent.get('actions', [])):
            return {'decision': 'allow', 'reason': _policy_text(n, 'reviewed ' + action + ' policy for ' + n['id'])}
        return unknown
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return unknown


def validate_merge(inst, n, mode):
    """Pure settings hook. Return None or raise ValueError before any settings are published."""
    if mode not in ('local', 'pr'):
        raise ValueError('merge must be local or pr')
    if mode == 'local':
        return
    decision = publication_decision(inst, n, 'pr')
    if decision['decision'] != 'allow':
        raise ValueError(_m_guard.redact(decision['reason'], _m_guard.never_terms(n)))


def publication_instructions(inst, n):
    from . import land_checks as _m_land_checks
    lines = ['## Publication policy', '']
    decisions = []
    for action in ('push', 'pr'):
        decision = publication_decision(inst, n, action)
        decisions.append(action + ': ' + decision['decision'] + ' — ' + decision['reason'])
    lines += ['; '.join(decisions) + '. Selecting a merge mode grants no outward permission. Before any supported push/PR step, '
              'recheck policy and current land-check evidence; ask the coordinator on unknown policy.', '']
    lines += _m_land_checks.land_check_instructions(n)
    return [_m_guard.redact(line, _m_guard.never_terms(n)) for line in lines]


def cmd_policy(a):
    from . import common as _m_common
    from . import nodes as _m_nodes
    inst = _m_nodes.load()
    n = inst.node(a.id)
    if a.json:
        if a.action:
            _m_common.die('choose --json or --action')
        template = {'version': 1, 'node': n['id'], 'reviewer': '', 'reviewed_at': '',
                    'rules': [{**r, 'actions': {'push': 'unknown', 'pr': 'unknown'}} for r in limit_snapshot(n)]}
        if _m_nodes.SECRET_LINE.search(json.dumps(template)):
            _m_common.die('policy template holds unsafe secret metadata; ask the coordinator to edit it')
        print(_m_guard.redact(json.dumps(template, indent=2), _m_guard.never_terms(n)))
        return
    if not a.action:
        _m_common.die('policy requires --action push|pr or --json')
    result = publication_decision(inst, n, a.action)
    if result['decision'] != 'allow':
        _m_common.die(_m_guard.redact(result['reason'], _m_guard.never_terms(n)))
    print(_m_guard.redact(result['reason'], _m_guard.never_terms(n)))
