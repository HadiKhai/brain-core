"""Structural rule metadata, portable test references and durable sweep tasks.

This module never classifies prose. Instance rules have explicit comment IDs;
node rules are top-level Markdown bullets in ``Rules for this node``. Unmarked
bullets receive a content ID, excluding their Gate/Applies annotations.
"""
import ast
import json
import re
from pathlib import Path

from . import common
from . import instance
from .rule_syntax import NODE_ID, gate_references, node_rule_id

MARKER = re.compile(r'^<!-- rule:([A-Za-z0-9][A-Za-z0-9_-]*) -->$', re.M)
JS_IDENTIFIER = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')
STATE_VERSION = 1


def _unfenced(text):
    """Blank code fences/comments, preserving offsets and line numbers."""
    lines, fence, comment = [], None, False
    def blank(value):
        return ''.join(char if char in '\r\n' else ' ' for char in value)
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = re.match(r'(`{3,}|~{3,})(.*)', stripped)
        if marker and not comment:
            if fence is None:
                fence = marker[1]
            elif (marker[1][0] == fence[0] and len(marker[1]) >= len(fence)
                  and not marker[2].strip()):
                fence = None
            lines.append(blank(line))
            continue
        if fence is not None:
            lines.append(blank(line))
            continue
        if not comment and MARKER.fullmatch(line.strip()):
            lines.append(line)
            continue
        offset, parts = 0, []
        while offset < len(line):
            if comment:
                end = line.find('-->', offset)
                stop = end + 3 if end >= 0 else len(line)
                parts.append(blank(line[offset:stop]))
                comment = end < 0
            else:
                end = line.find('<!--', offset)
                stop = end if end >= 0 else len(line)
                parts.append(line[offset:stop])
                comment = end >= 0
            offset = stop
        lines.append(''.join(parts))
    return ''.join(lines)


def _rule(ident, text, line, node_id=None):
    # Metadata labels are syntax, never inferred from a rule's wording.
    visible = _unfenced(text)
    gates = re.findall(r'(?:^|[ \t])Gate:[ \t]*([^\n]*?)(?=[ \t]+Applies:|$)', visible, re.M)
    applies = re.findall(r'(?:^|[ \t])Applies:[ \t]*([^\n]*?)(?=[ \t]+Gate:|$)', visible, re.M)
    return {'id': ident, 'text': text, 'line': line, 'node_id': node_id,
            'gate': gates[0].strip() if len(gates) == 1 else None,
            'applies': applies[0].strip() if len(applies) == 1 else None}


def parse_instance_rules(text):
    matches = list(MARKER.finditer(_unfenced(text)))
    return [_rule(match[1], text[match.start():matches[i + 1].start() if i + 1 < len(matches)
                                else len(text)], text.count('\n', 0, match.start()) + 1)
            for i, match in enumerate(matches)]


def parse_node_rules(text, node_id):
    visible = _unfenced(text)
    heading = re.search(r'^## Rules for this node[ \t]*$', visible, re.M)
    if not heading:
        return []
    end = re.search(r'^## ', visible[heading.end():], re.M)
    stop = heading.end() + end.start() if end else len(text)
    starts = list(re.finditer(r'^(?:[-*+] |<!-- rule:[A-Za-z0-9][A-Za-z0-9_-]* -->$)',
                              visible[heading.end():stop], re.M))
    rules, pending = [], None
    for i, start in enumerate(starts):
        offset = heading.end() + start.start()
        finish = heading.end() + starts[i + 1].start() if i + 1 < len(starts) else stop
        body = text[offset:finish]
        marker = MARKER.fullmatch(start[0])
        if marker:
            if pending:
                raise ValueError(node_id + ': rule marker has no bullet')
            pending = (marker[1], offset)
            continue
        ident = node_rule_id(body, pending[0] if pending else None)
        offset = pending[1] if pending else offset
        rules.append(_rule(ident, text[offset:finish], text.count('\n', 0, offset) + 1, node_id))
        pending = None
    if pending:
        raise ValueError(node_id + ': rule marker has no bullet')
    return rules


def annotate_sealed_rules(text, node_id):
    """Retain legacy node rules while declaring missing structural metadata.

    Missing applicability already means all to the landed contracts reader.
    Absence of a supplied gate is recorded explicitly, without judging prose.
    Existing metadata, including malformed declarations, is never overwritten.
    """
    for rule in reversed(parse_node_rules(text, node_id)):
        visible = _unfenced(rule['text'])
        fields = []
        if not re.search(r'(?:^|[ \t])Gate:', visible, re.M):
            fields.append('  Gate: none (legacy rule retained during sealing; no automated gate supplied)')
        if not re.search(r'(?:^|[ \t])Applies:', visible, re.M):
            fields.append('  Applies: all')
        if fields:
            # Offsets refer to the original text; reverse order preserves them.
            start = sum(len(line) for line in text.splitlines(keepends=True)[:rule['line'] - 1])
            end = start + len(rule['text'])
            suffix = ('' if text[:end].endswith('\n') else '\n') + '\n'.join(fields) + '\n'
            text = text[:end] + suffix + text[end:]
    return text


def targets(rule, nodes):
    value = rule['applies']
    if value == 'all':
        selected = list(nodes)
    elif value and all(NODE_ID.fullmatch(item.strip()) for item in value.split(',')):
        selected = [item.strip() for item in value.split(',')]
        if len(set(selected)) != len(selected) or any(item not in nodes for item in selected):
            raise ValueError('Applies names duplicate or unknown nodes')
    else:
        raise ValueError('missing or malformed Applies: all or node IDs')
    if rule['node_id'] and selected != [rule['node_id']]:
        # A node-local rule cannot export its prose into another node.
        if value != 'all':
            raise ValueError('node rule Applies must name only its own node (or all)')
        selected = [rule['node_id']]
    return [nodes[key] for key in selected if nodes[key].get('home_path')
            and not nodes[key].get('archived')]


def _javascript_tests(text):
    """Read literal test()/it() names, ignoring comments and string contents.

    Tokenization checks declaration syntax only. Computed names and generated
    tests need a literal named wrapper test; repository code is never executed.
    """
    tokens, offset = [], 0
    while offset < len(text):
        char = text[offset]
        if char.isspace():
            offset += 1
            continue
        if text.startswith('//', offset):
            end = text.find('\n', offset)
            offset = len(text) if end < 0 else end + 1
            continue
        if text.startswith('/*', offset):
            end = text.find('*/', offset + 2)
            if end < 0:
                return set()
            offset = end + 2
            continue
        if char == '/' and (not tokens or tokens[-1] in (
                ('punct', '='), ('punct', '('), ('punct', '['), ('punct', ','),
                ('punct', ':'), ('punct', '!'), ('punct', '?'), ('punct', ';'),
                ('name', 'return'), ('name', 'throw'))):
            end, bracket = offset + 1, False
            while end < len(text) and text[end] not in '\r\n':
                if text[end] == '\\':
                    end += 2
                    continue
                if text[end] == '[':
                    bracket = True
                elif text[end] == ']':
                    bracket = False
                elif text[end] == '/' and not bracket:
                    break
                end += 1
            if end >= len(text) or text[end] != '/':
                return set()
            tokens.append(('regexp', None))
            offset = end + 1
            continue
        if char in ('"', "'", '`'):
            end = offset + 1
            while end < len(text):
                if text[end] == '\\':
                    end += 2
                elif text[end] == char:
                    break
                else:
                    end += 1
            if end >= len(text):
                return set()
            raw = text[offset:end + 1]
            tokens.append(('string', raw if char != '`' else None))
            offset = end + 1
            continue
        word = JS_IDENTIFIER.match(text, offset)
        if word:
            tokens.append(('name', word[0]))
            offset += len(word[0])
            continue
        tokens.append(('punct', char))
        offset += 1
    names = set()
    for i, token in enumerate(tokens):
        if token not in (('name', 'test'), ('name', 'it')):
            continue
        if i and tokens[i - 1] == ('punct', '.'):
            continue  # an arbitrary object's .test() is not a runner declaration
        end = i + 1
        # Test runner modifiers are identifiers, not inferred prose categories.
        while end + 1 < len(tokens) and tokens[end] == ('punct', '.') and tokens[end + 1][0] == 'name':
            end += 2
        if (end + 2 < len(tokens) and tokens[end] == ('punct', '(')
                and tokens[end + 1][0] == 'string' and isinstance(tokens[end + 1][1], str)
                and tokens[end + 2] == ('punct', ',')):
            try:
                name = ast.literal_eval(tokens[end + 1][1])
            except (ValueError, SyntaxError):
                continue
            if isinstance(name, str):
                names.add(name)
    return names


def _test_exists(home, relative, selector, cache):
    try:
        # One validation observes each source once. Repeated rule references do
        # not re-resolve/stat the same file thousands of times; no cache survives
        # this validation call.
        key = ('source', str(home), relative)
        if key not in cache:
            root = Path(home).resolve()
            path = root / relative
            resolved = path.resolve()
            cache[key] = None
            if root not in resolved.parents or not path.is_file():
                return False
            # Inspect declarations without importing or executing repository code.
            if path.suffix == '.py':
                cache[key] = ast.parse(path.read_bytes(), filename=relative)
            elif path.suffix in ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'):
                cache[key] = _javascript_tests(path.read_text())
            else:
                return False
        tree = cache[key]
        if tree is None:
            return False
        if isinstance(tree, set):
            return selector in tree
        parts = selector.split('::')
        if any(not part.isidentifier() for part in parts):
            return False
        if len(parts) == 1:
            definitions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and node.name == selector]
            definitions += [method for node in tree.body if isinstance(node, ast.ClassDef)
                            for method in node.body if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and method.name == selector]
            return len(definitions) == 1 and selector.startswith('test_')
        candidates = tree.body
        for part in parts:
            found = next((node for node in candidates if isinstance(node, (ast.ClassDef,
                         ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == part), None)
            if found is None:
                return False
            candidates = found.body
        if isinstance(found, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return found.name.startswith('test_')
        return False
    except (OSError, ValueError, SyntaxError, UnicodeError):
        return False


def read_registry(inst):
    nodes = {node['id']: node for node in inst.nodes()}
    path = inst.root / 'library/instance-rules.md'
    rules = parse_instance_rules(path.read_text()) if path.exists() else []
    for node in nodes.values():
        path = node['dir'] / 'AGENTS.md'
        if path.exists():
            rules.extend(parse_node_rules(path.read_text(), node['id']))
    seen = set()
    for rule in rules:
        key = (rule['node_id'], rule['id'])
        if key in seen:
            raise ValueError(rule['id'] + ': duplicate rule id')
        seen.add(key)
    return rules, nodes


def _validated_rules(rules, nodes):
    problems, cache, sweep_ids, invalid = [], {}, {}, set()
    for index, rule in enumerate(rules):
        label = (rule['node_id'] + ':' if rule['node_id'] else '') + rule['id']
        try:
            selected = targets(rule, nodes)
            refs = gate_references(rule['gate'])
            for node in selected:
                task_key = (node['id'], rule['id'])
                if task_key in sweep_ids:
                    other, other_label = sweep_ids[task_key]
                    problems.append(f"rule {label}: sweep ID collision with {other_label} in {node['id']}")
                    invalid.update((index, other))
                sweep_ids[task_key] = (index, label)
            for owner, path, test in refs:
                if owner is None:
                    # Preserve capture's stored unqualified references. A local
                    # rule supplies its owner; instance refs can live in any
                    # bound home, independently of the rule's Applies scope.
                    candidates = ([nodes[rule['node_id']]] if rule['node_id'] else nodes.values())
                    if not any(node.get('home_path') and
                               _test_exists(node['home_path'], path, test, cache) for node in candidates):
                        problems.append(f'rule {label}: missing Gate test {path}::{test} in owning home')
                        invalid.add(index)
                    continue
                node = nodes.get(owner)
                if node is None or not node.get('home_path'):
                    problems.append(f'rule {label}: unknown or unbound Gate home {owner}')
                    invalid.add(index)
                elif not _test_exists(node['home_path'], path, test, cache):
                    problems.append(f"rule {label}: {owner}: missing Gate test {path}::{test}")
                    invalid.add(index)
        except ValueError as error:
            problems.append(f'rule {label}: {error}')
            invalid.add(index)
    return [rule for index, rule in enumerate(rules) if index not in invalid], problems


def validate_registry(inst, registry=None):
    """Return diagnostics with rule IDs. Never write state or open tasks."""
    try:
        rules, nodes = registry if registry is not None else read_registry(inst)
    except (OSError, ValueError) as error:
        return ['rule registry: ' + str(error)]
    return _validated_rules(rules, nodes)[1]


def sync_sweeps(inst):
    """Explicit ``brain check`` write: acknowledge only fully persisted sweeps.

    A crash before checkpoint publication replays existing matching tasks. An
    unrelated task ID collision refuses instead of consuming the new rule.
    """
    from . import tasks
    if common.outward_links(inst.root):
        raise ValueError('outbound links prevent sweep publication')
    with instance.brain_lock(inst):
        if common.outward_links(inst.root):
            raise ValueError('outbound links prevent sweep publication')
        current = instance.Instance(inst.root)
        registry = read_registry(current)
        rules, nodes = registry
        valid, problems = _validated_rules(rules, nodes)
        path = inst.root / '.brain/rule-registry.json'
        common.safe_target(path)
        if not path.exists():
            # Installation takes a baseline; the existing-rule audit is a
            # separate activity. Only additions after this checkpoint sweep.
            keys = [json.dumps([rule['node_id'], rule['id']]) for rule in rules]
            common.atomic_write(path, json.dumps({'version': STATE_VERSION, 'rules': keys}, indent=2) + '\n', private=True)
            return problems
        saved = json.loads(path.read_text())
        if (not isinstance(saved, dict) or saved.get('version') != STATE_VERSION
                or not isinstance(saved.get('rules'), list)
                or any(not isinstance(key, str) for key in saved['rules'])):
            raise ValueError('invalid rule registry state; restore its checkpoint')
        known, keys, changed, task_paths = set(saved['rules']), [], False, []
        for rule in rules:
            key = json.dumps([rule['node_id'], rule['id']])
            if key in known:
                keys.append(key)
                continue
            if rule not in valid:
                continue
            description = ('Read-only sweep. Report findings or no change; do not rebuild or merge.\n\n'
                           + rule['text'].strip())
            for node in targets(rule, nodes):
                ident = 'SWEEP-' + rule['id']
                task_paths.append(tasks.record_path(node, ident))
                provenance = ['rule-registry:' + key]
                rows = tasks.read_records(node, ident=ident)
                if ident in rows:
                    if rows[ident].get('provenance') != provenance or rows[ident].get('description') != description:
                        raise ValueError(rule['id'] + ': sweep task collision in ' + node['id'])
                    continue
                row, _ = tasks.prepare_mutation(current, node, ident, {
                    'title': 'Sweep existing work against rule ' + rule['id'],
                    'status': 'ready', 'source': 'local', 'description': description,
                    'kind': 'research', 'completion_kind': 'report',
                    'provenance': provenance}, create=True)
                tasks.save_record(node, row)
                changed = True
            keys.append(key)
        # Commit tasks before acknowledging rules. A retry also commits tasks
        # already persisted by a previous interrupted attempt.
        if changed or set(keys) != known:
            current.commit(task_paths, 'check: open rule sweep tasks')
            common.atomic_write(path, json.dumps({'version': STATE_VERSION, 'rules': keys}, indent=2) + '\n', private=True)
        return problems
