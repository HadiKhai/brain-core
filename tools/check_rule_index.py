#!/usr/bin/env python3
"""Check a compact rule index against its complete, independently retained rules."""
import argparse
from collections import Counter
from pathlib import Path
import re


def check(index, rules, budget=4000, baseline_rules=None, baseline_index=None):
    section = index.split('## Instance rules\n', 1)[1]
    entries = re.findall(r'^- (R\d+) (\S.*)$', section, re.M)
    anchors = re.findall(r'^<!-- rule:(R\d+) -->$', rules, re.M)
    indexed = Counter(key for key, _ in entries)
    complete = Counter(anchors)
    errors = []
    if not entries or not anchors:
        errors.append('missing index entries or full-rule anchors')
    if indexed != complete or any(n != 1 for n in indexed.values()):
        errors.append('index and full rules must have identical, unique IDs')
    # Every anchor must introduce exactly one complete top-level rule.
    blocks = re.split(r'^<!-- rule:R\d+ -->\n', rules, flags=re.M)[1:]
    if any(len(re.findall(r'^- \*\*', block, re.M)) != 1 for block in blocks):
        errors.append('each anchor must introduce one complete rule')
    if len(section) > budget:
        errors.append('index exceeds character budget')
    if baseline_rules is not None:
        # Subsequence check permits annotations, without deleting or rewriting any line.
        remaining = iter(rules.splitlines())
        if not all(any(candidate == line for candidate in remaining)
                   for line in baseline_rules.splitlines()):
            errors.append('original full-rule text changed or disappeared')
    if baseline_index is not None:
        old_section = baseline_index.split('## Instance rules\n', 1)[1]
        old_entries = re.findall(r'^- \*\*.*$', old_section, re.M)
        if any(line not in rules for line in old_entries):
            errors.append('original index requirements missing from full rules')
        operator = baseline_index.split('## About the operator\n', 1)[1].split('## Instance rules\n', 1)[0]
        if operator != index.split('## About the operator\n', 1)[1].split('## Instance rules\n', 1)[0]:
            errors.append('operator block changed')
    return {'rules': len(anchors), 'index_chars': len(section),
            'total_chars': len(index), 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('index', type=Path)
    parser.add_argument('rules', type=Path)
    parser.add_argument('--budget', type=int, default=4000)
    parser.add_argument('--baseline-rules', type=Path)
    parser.add_argument('--baseline-index', type=Path)
    args = parser.parse_args()
    result = check(args.index.read_text(), args.rules.read_text(), args.budget,
                   args.baseline_rules.read_text() if args.baseline_rules else None,
                   args.baseline_index.read_text() if args.baseline_index else None)
    import json
    print(json.dumps(result, indent=2))
    return bool(result['errors'])


if __name__ == '__main__':
    raise SystemExit(main())
