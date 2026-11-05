#!/usr/bin/env python3
"""Build/check a hash-bound instance patch; never writes the instance source."""
import argparse
import ast
import difflib
import hashlib
from pathlib import Path


def candidate(base):
    source = Path(__file__).with_name('watcher_approval.py').read_text()
    marker = '# ---------------------------------------------------------------- context size\n'
    call = '    snapshot = heavy_snapshot()\n'
    rule = '  question   '
    for anchor in (marker, call, rule):
        if base.count(anchor) != 1:
            raise ValueError('watcher integration anchor changed')
    result = base.replace(marker, source + '\n\n' + marker)
    result = result.replace(call, call + "    rule_approval(snapshot[2], fire, HOME / '.t3/userdata/statev2.sqlite', now())\n")
    result = result.replace(rule, '  approval   a T3 approval request waiting more than 2 minutes in a mapped node project\n' + rule)
    result = result.replace('Reads files, git and Postgres only;', 'Reads files, local T3 SQLite, git and Postgres only;')
    ast.parse(result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--base-sha256', required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--check', action='store_true')
    args = p.parse_args()
    raw = args.base.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != args.base_sha256:
        p.error('base sha256 mismatch; source untouched')
    base = raw.decode()
    result = candidate(base)
    patch = ''.join(difflib.unified_diff(base.splitlines(True), result.splitlines(True),
                                       fromfile='a/scripts/watcher.py', tofile='b/scripts/watcher.py'))
    artifacts = {'watcher.patch': patch, 'watcher.candidate.py': result,
                 'watcher.base.sha256': digest + '\n',
                 'watcher.candidate.sha256': hashlib.sha256(result.encode()).hexdigest() + '\n'}
    if args.check:
        if any((args.out / name).read_text() != value for name, value in artifacts.items()):
            p.error('artifact mismatch; regenerate and review')
    else:
        args.out.mkdir(parents=True, exist_ok=True)
        for name, value in artifacts.items():
            (args.out / name).write_text(value)
    print('watcher approval patch: checked' if args.check else 'watcher approval patch: generated')


if __name__ == '__main__':
    main()
