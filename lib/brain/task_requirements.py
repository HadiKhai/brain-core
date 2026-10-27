"""Immutable requirement sources and an explicit, generated current-rules index."""
import json
import re

from . import common as _m_common

_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_tasks = _m_common.LazyModule(__package__, 'tasks')

RULE_MARK = '<!-- brain:current-rules v1 -->'


def rules_text(n, rules):
    active = [r for r in rules.values() if r.get('status') == 'active']
    lines = ['# Current rules', '', RULE_MARK, '', 'Generated from explicit rule records; source diaries are retained.', '']
    for row in sorted(active, key=lambda r: r['id']):
        lines += ['## ' + row['id'], '', row['text'], '', 'Source: ' + row['ref'] + ' (' + row['at'] + ')', '']
    archived = [r for r in rules.values() if r.get('status') != 'active']
    if archived:
        lines += ['## Archived rules', '']
        lines += ['- ' + r['id'] + ': ' + r.get('superseded_by', r['status']) for r in sorted(archived, key=lambda r: r['id'])]
        lines += ['']
    return '\n'.join(lines)


def add_rule(inst, n, ident, text, at, ref, supersedes=None):
    if not re.fullmatch(r'D-[A-Za-z0-9-]+', ident) or not text.strip() or not ref.strip() or not inst.parse_time(at):
        raise ValueError('rule requires a D- id, text, absolute date and source')
    path = n['dir'] / 'rules.md'
    _m_common.safe_target(path)
    if path.exists() and RULE_MARK not in path.read_text():
        raise ValueError('rules.md is manually maintained; preserve it before generating an index')
    rules = _m_tasks.read_records(n, 'rules')
    if ident in rules or supersedes and supersedes not in rules:
        raise ValueError('rule already exists or superseded rule not found')
    if supersedes and rules[supersedes].get('status') != 'active':
        raise ValueError('only an active rule can be superseded')
    row = {'version': _m_tasks.VERSION, 'id': ident, 'text': text, 'at': at, 'ref': ref,
           'status': 'active', 'created': inst.now()}
    if supersedes:
        rules[supersedes].update(status='superseded', superseded_by=ident)
    rules[ident] = row
    # Preflight every target before the first write.
    for key in rules:
        _m_tasks.record_path(n, key, 'rules')
    if supersedes:
        _m_tasks.save_record(n, rules[supersedes], 'rules')
    _m_tasks.save_record(n, row, 'rules')
    _m_common.atomic_write(path, rules_text(n, rules))
    _m_common.touch(path)
    inst.commit([], n['id'] + ': current rule ' + ident)
    return row


def cmd_task_sources(a):
    inst = _m_nodes.load()
    n = inst.node(a.node)
    try:
        if a.action == 'rule-add':
            rows = [add_rule(inst, n, a.rule_id, a.text, a.at, a.ref, a.supersedes)]
        else:
            rows = list(_m_tasks.read_records(n, 'rules' if a.action == 'rules' else 'requirements').values())
            if a.action == 'rules':
                rows = [r for r in rows if a.all or r.get('status') == 'active']
            if a.record_id:
                rows = [r for r in rows if r['id'] == a.record_id]
                if not rows:
                    raise ValueError('record not found')
        print(json.dumps(rows, ensure_ascii=False))
    except (OSError, ValueError) as error:
        _m_common.die(str(error))


def register(actions):
    p = actions.add_parser('rule-add', help='record an explicit current rule, preserving superseded records')
    p.add_argument('node'); p.add_argument('rule_id')
    p.add_argument('--text', required=True); p.add_argument('--at', required=True); p.add_argument('--ref', required=True)
    p.add_argument('--supersedes'); p.set_defaults(f=cmd_task_sources)
    for action in ('requirements', 'rules'):
        p = actions.add_parser(action)
        p.add_argument('node'); p.add_argument('record_id', nargs='?'); p.add_argument('--all', action='store_true')
        p.set_defaults(f=cmd_task_sources)
