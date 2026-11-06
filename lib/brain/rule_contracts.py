"""Verbatim inherited rules and structural per-rule review evidence.

Only explicit Markdown structure and Applies fields select rules. This module
does not infer applicability, compliance or meaning from instruction wording.
"""
import hashlib
import json
import re

from .rule_syntax import gate_references, markdown_structure as _structure, node_rule_id

RULES_START = '<!-- brain:applicable-rules -->'
RULES_END = '<!-- brain:applicable-rules:end -->'


def without_rules(text):
    """Replace generated snapshots; their reserved markers are never rule text."""
    return re.sub(re.escape(RULES_START) + r'.*?' + re.escape(RULES_END), '', text, flags=re.S)


def rule_sources(n):
    """Nearest boundary is the inheritance fence, including for instance rules."""
    chain = []
    while n:
        chain.append(n)
        if n.get('sealed'):
            break
        n = n.get('parent')
    return list(reversed(chain))


def rule_input_paths(n):
    return [source['dir'] / name for source in rule_sources(n)
            for name in ('AGENTS.md', 'library/instance-rules.md')]


def _read(path):
    # Preserve source bytes, including CRLF and indentation, in generated briefs.
    return path.read_bytes().decode('utf-8') if path.exists() else ''


def _applies(text, node_id):
    values = re.findall(r'(?:^|[ \t])Applies:[ \t]*([^\n]*?)(?=[ \t]+Gate:|$)', _structure(text), re.M)
    if not values:
        return True
    if len(values) != 1 or not values[0]:
        raise ValueError('rule needs one nonempty Applies field')
    targets = [part.strip() for part in values[0].split(',')]
    if any(not part for part in targets) or ('all' in targets and targets != ['all']):
        raise ValueError('invalid rule Applies field')
    return targets == ['all'] or node_id in targets


def applicable_rules(n):
    rules = []
    sources = rule_sources(n)
    for source in sources:
        if 'dir' not in source:
            continue
        path = source['dir'] / 'AGENTS.md'
        text = _read(path)
        full_path = source['dir'] / 'library/instance-rules.md'
        full = _read(full_path) if not source.get('parent') else ''
        anchors = list(re.finditer(r'^<!-- rule:(R[0-9]+) -->\r?$', _structure(full), re.M))
        blocks = {}
        for i, anchor in enumerate(anchors):
            key = anchor.group(1)
            if key in blocks:
                raise ValueError('duplicate instance rule id: ' + key)
            blocks[key] = full[anchor.end():anchors[i + 1].start() if i + 1 < len(anchors) else len(full)]
        lines, active, start = text.splitlines(keepends=True), False, None
        pending, current_id = None, None

        def add(end):
            if start is None:
                return
            body = ''.join(lines[start:end])
            if RULES_START in body or RULES_END in body:
                raise ValueError('rule text contains reserved generated-block marker')
            numbered = re.match(r'^[-*+] (R[0-9]+)\b', body)
            key = numbered.group(1) if numbered else 'node:' + source['id'] + ':' + (
                node_rule_id(body, current_id))
            metadata = blocks.get(key, body)
            if _applies(metadata, n['id']):
                rules.append({'id': key, 'text': body,
                              'source': str(path.relative_to(sources[0]['dir'])), 'line': start + 1})

        for i, line in enumerate(_structure(text).splitlines(keepends=True)):
            if line.startswith('## '):
                add(i)
                start = None
                active = line[3:].strip() in ('Instance rules', 'Rules for this node')
                pending, current_id = None, None
            elif active and re.fullmatch(r'<!-- rule:[A-Za-z0-9_-]+ -->', line.strip()):
                add(i)
                start = None
                pending = line.strip()[10:-4]
            elif active and re.match(r'^[-*+] ', line):
                add(i)
                start = i
                current_id, pending = pending, None
        add(len(lines))
    ids = [rule['id'] for rule in rules]
    if len(ids) != len(set(ids)):
        raise ValueError('inherited rule ids must be unique')
    return rules


def rules_digest(rules):
    payload = [{key: rule[key] for key in ('id', 'text')} for rule in rules]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def checked_rules(n):
    """Generators report a bounded refusal; validation callers retain exceptions."""
    try:
        return applicable_rules(n)
    except (OSError, UnicodeError, ValueError) as error:
        from . import common
        common.die('cannot assemble applicable rules: ' + str(error))


def rendered_rule_text(text, node_id):
    """Project only structural annotations; prose and the canonical digest stay intact.

    Explicit owners identify homes without inspecting rule meaning or consulting
    the naming guard. Foreign references hide their entire location/selector so
    those fields cannot repeat a home name. Unowned and own-home refs stay exact.
    """
    visible = _structure(text)
    fields = list(re.finditer(r'(?:^|[ \t])(Gate|Applies):[ \t]*([^\r\n]*?)'
                              r'(?=[ \t]+(?:Gate|Applies):|\r?$)', visible, re.M))
    for field in reversed(fields):
        start, end = field.span(2)
        original = text[start:end]
        if original != visible[start:end]:
            # Embedded comments are not owner metadata. Preserve ambiguous fields
            # intact and let the ordinary guard refuse any private name.
            continue
        value = original.strip()
        if field[1] == 'Gate':
            try:
                refs = gate_references(value)
            except ValueError:
                continue  # A prose label is not a structurally valid Gate ref.
            if not refs:  # none (reason) is metadata, but has no foreign owner.
                continue
            entries = value.split('; ')
            rendered = '; '.join(entry if owner in (None, node_id) else 'other home'
                                 for entry, (owner, _, _) in zip(entries, refs))
        else:
            entries = re.split(r'(,[ \t]*)', value)
            for i in range(0, len(entries), 2):
                if entries[i].strip() not in ('all', node_id):
                    entries[i] = 'other home'
            rendered = ''.join(entries)
        if rendered != value:
            # Preserve surrounding spaces, CRLF and every byte outside metadata.
            leading = len(original) - len(original.lstrip())
            trailing = len(original) - len(original.rstrip())
            text = text[:start] + original[:leading] + rendered + (
                original[len(original) - trailing:] if trailing else '') + text[end:]
    return text


def rules_lines(n, rules=None):
    rules = checked_rules(n) if rules is None else rules
    if not rules:
        return []
    out = [RULES_START, '## Applicable rules (verbatim)', '',
           'Every rule below applies unless its own wording says otherwise. Read full indexed rules before acting.',
           'Full instance wording: library/instance-rules.md at <!-- rule:Rnn -->.',
           'Rules digest: ' + rules_digest(rules), '']
    for rule in rules:
        out += ['### Rule ' + rule['id'], '',
                'Source: ' + rule['source'] + ':' + str(rule['line']), '',
                rendered_rule_text(rule['text'], n['id']), '']
    return out + [RULES_END, '']


def review_rule_lines(n, rules=None):
    rules = checked_rules(n) if rules is None else rules
    return ['## Per-rule review table', '',
            'Review every injected rule, in addition to the contract. Return a JSON object with',
            '`rules_digest` and `rule_checks` (also copied into the review identity receipt).',
            'rules_digest: ' + rules_digest(rules),
            'Required rule ids: ' + ', '.join(rule['id'] for rule in rules),
            'Each rule_checks row is {"id": "<rule id>", "status": "pass|fail|n/a",',
            '"evidence": ["<file>:<positive line number>"], "reason": "<explanation>"}.',
            'pass and fail require at least one file:line. n/a requires a nonempty reason.',
            'A fail row blocks readiness. Do not claim semantic compliance from this structural check.', '']


def validate_rule_checks(n, data, rules=None, required=False):
    rules = applicable_rules(n) if rules is None else rules
    if not rules and not required and not (isinstance(data, dict) and ('rule_checks' in data or 'rules_digest' in data)):
        return
    expected = {rule['id'] for rule in rules}
    rows = data.get('rule_checks') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError('review missing per-rule table; missing rule ids: ' + ', '.join(sorted(expected)))
    seen, failed = set(), []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            raise ValueError('invalid per-rule review row')
        key = row['id']
        if key in seen or key not in expected:
            raise ValueError('duplicate or unknown review rule id: ' + key)
        seen.add(key)
        status, evidence = row.get('status'), row.get('evidence')
        if status not in ('pass', 'fail', 'n/a') or not isinstance(evidence, list):
            raise ValueError('invalid per-rule review status/evidence: ' + key)
        if any(not isinstance(ref, str) or not re.fullmatch(r'[^\s\x00-\x1f]+:[1-9][0-9]*', ref) for ref in evidence):
            raise ValueError('review needs file:line evidence for rule id: ' + key)
        if status in ('pass', 'fail') and not evidence:
            raise ValueError('review needs file:line evidence for rule id: ' + key)
        if status == 'n/a' and (not isinstance(row.get('reason'), str) or not row['reason'].strip()):
            raise ValueError('review n/a needs a reason for rule id: ' + key)
        if status == 'fail':
            failed.append(key)
    missing = expected - seen
    if missing:
        raise ValueError('review missing rule ids: ' + ', '.join(sorted(missing)))
    if data.get('rules_digest') != rules_digest(rules):
        raise ValueError('review rules changed; re-review current rule ids: ' + ', '.join(sorted(expected)))
    if failed and data.get('verdict') == 'pass':
        raise ValueError('review failed rule ids: ' + ', '.join(sorted(failed)))
