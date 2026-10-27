"""Enforcement metadata and idempotent work proposals from scoped corrections."""
import importlib
import re
from pathlib import Path

from . import common as _m_common
from . import instance as _m_instance

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_knowledge = _m_common.LazyModule(__package__, 'knowledge')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_recurrence = _m_common.LazyModule(__package__, 'recurrence')


def resolve_enforcing_check(node, value):
    """Check names are portable paths inside the bound home, never shell commands."""
    root = node.get('home_path') or node['dir']
    relative = Path(value)
    if not value or value == 'wish' or relative.is_absolute() or '..' in relative.parts or '\x00' in value:
        return None
    path = root / relative
    try:
        if root.resolve() not in path.resolve().parents or not path.is_file():
            return None
    except (OSError, ValueError):
        return None
    return path


def enforcement_rules(folder):
    rules = {key: {**rule, 'file': 'direction.md'}
             for key, rule in _m_knowledge.direction_principles(folder / 'direction.md').items()}
    for name in ('role.md', 'standards.md'):
        extra = _m_knowledge.direction_principles(folder / name,
                    sections=('## Principles', '## Standards', '## Checklist', '## Never'))
        if set(extra) & set(rules):
            raise ValueError('ambiguous principle ids across direction and standards')
        rules.update({key: {**rule, 'file': name} for key, rule in extra.items()})
    return rules


def visible_corrections(inst, node, target):
    nodes = inst.nodes()
    terms = _m_knowledge.learning_terms(inst, target['fm'], [node], nodes)
    for lesson in _m_knowledge.parse_lessons(target['dir'] / 'lessons.md'):
        source = _m_instance.resolve_ref(nodes, lesson['from'])
        if not source or lesson.get('malformed'):
            continue
        if '*' not in lesson['scope'] and not set(node['owners']) <= set(lesson['scope']):
            continue
        if target['type'] == 'node':
            current = source
            while current and set(current['owners']) == set(node['owners']):
                if current['id'] == node['id']:
                    break
                current = current['parent']
            else:
                continue
        policy = terms + _m_guard.never_terms(source)
        if target['type'] == 'node' and source['id'] == node['id']:
            policy = _m_guard.never_terms(node['parent']) if node['parent'] else []
        if not _m_guard.find_terms('\n'.join(lesson[k] for k in ('text', 'principle', 'enforced_by')), policy):
            yield lesson


def enforcement_metrics(inst, node):
    out = []
    for target in _m_recurrence.synthesis_targets(inst, node):
        if 'error' in target:
            continue
        try:
            rules = enforcement_rules(target['dir'])
            terms = _m_knowledge.learning_terms(inst, target['fm'], [node])
            if _m_guard.find_terms(str(rules) + target['id'], terms):
                continue
            wishes = sum(rule['enforced_by'] == 'wish' for rule in rules.values())
            out.append({'node': node['id'], 'target': target['id'], 'type': target['type'],
                'total': len(rules), 'wishes': wishes,
                'wish_percent': round(100.0 * wishes / len(rules), 1) if rules else 0.0,
                'principles': [{'id': ident, 'enforced_by': rule['enforced_by'],
                    'exists': resolve_enforcing_check(node, rule['enforced_by']) is not None}
                    for ident, rule in sorted(rules.items())]})
        except (OSError, ValueError, SystemExit):
            continue
    return out


def enforcement_problems(inst, node):
    problems = []
    for target in _m_recurrence.synthesis_targets(inst, node):
        if 'error' in target:
            continue
        try:
            rules = enforcement_rules(target['dir'])
        except (OSError, ValueError, SystemExit):
            problems.append(f"{node['id']}: unreadable or ambiguous enforcement metadata")
            continue
        for ident, rule in rules.items():
            if rule['enforced_by'] != 'wish' and resolve_enforcing_check(node, rule['enforced_by']) is None:
                problems.append(_m_guard.redact(f"{node['id']}: {target['type']} {target['id']}, {ident}: enforcing check missing or outside home",
                                                _m_knowledge.learning_terms(inst, target['fm'], [node])))
    return problems


def enforcement_task_specs(inst, node):
    out = []
    for target in _m_recurrence.synthesis_targets(inst, node):
        if 'error' in target:
            continue
        try:
            rules = enforcement_rules(target['dir'])
            lessons = list(visible_corrections(inst, node, target))
            terms = _m_knowledge.learning_terms(inst, target['fm'], [node])
            for ident, rule in sorted(rules.items()):
                evidence = [lesson for lesson in lessons if lesson['principle'] == ident and lesson['enforced_by'] == 'wish']
                if rule['enforced_by'] != 'wish' or len(evidence) < 2:
                    continue
                label = f"{target['type']} {target['id']}: {ident}"
                if _m_guard.find_terms(label, terms):
                    continue
                identity = _m_recurrence.synthesis_digest([node['id'], target['type'], target['id'], ident])
                out.append({'source_id': 'enforcement-' + identity,
                            'title': 'Turn into a check: ' + label, 'principle': ident,
                            'target': target['id'], 'target_type': target['type'], 'count': len(evidence),
                            'source_ref': str((target['dir'] / rule['file']).relative_to(inst.root))})
        except (OSError, ValueError, SystemExit):
            continue
    return out


def enforcement_due(inst, node):
    return [{'node': node['id'], 'kind': 'enforcement', 'episode': spec['source_id'],
             'text': 'turn into a check: ' + spec['title'].split(': ', 1)[1] + f" ({spec['count']} scoped corrections)",
             'target': spec['target'], 'principle': spec['principle']}
            for spec in enforcement_task_specs(inst, node)]


def materialize_enforcement_tasks(inst, node, upsert=None, require_tasks=False):
    """Called only by explicit mutations (lesson capture or enforcement --sync-tasks)."""
    if upsert is None:
        try:
            tasks = importlib.import_module('.tasks', __package__)
        except ModuleNotFoundError as error:
            if error.name != __package__ + '.tasks':
                raise
            if require_tasks:
                _m_common.die('task domain unavailable; enforcement notices retained for synchronization')
            return []  # independent task module may not be installed yet
        upsert = tasks.upsert_source_task
    return [upsert(inst, node, spec['source_id'], spec['title'], kind='chore',
                   description=f"Repeated wish for {spec['target']} / {spec['principle']}; add an enforcing check.",
                   source_ref=spec['source_ref']) for spec in enforcement_task_specs(inst, node)]


def cmd_enforcement(a):
    import json
    inst = _m_nodes.load()
    node = inst.node(a.id)
    if a.sync_tasks:
        rows = materialize_enforcement_tasks(inst, node, require_tasks=True)
        print(f'{len(rows)} enforcement task(s) synchronized')
        return
    metrics = enforcement_metrics(inst, node)
    if a.json:
        print(json.dumps(metrics, indent=1))
    else:
        for row in metrics:
            print(f"{row['type']} {row['target']}: {row['wishes']}/{row['total']} wishes ({row['wish_percent']}%)")
