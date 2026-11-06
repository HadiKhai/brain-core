"""Local repair tools; never call the host or regenerate existing prompt bodies.

Export host schedule records as a JSON array (or {"tasks": [...]}) first. Exact
host prompts are not retained by older instance bindings. All ids are selected
from .brain/local.json, including wake capability paths and root bindings.

PYTHONPATH=lib python3 -m brain.instruction_capture_repair plan --root INSTANCE \
    --tasks-json TASKS.json --output PLAN.json
PYTHONPATH=lib python3 -m brain.instruction_capture_repair reconcile --root INSTANCE \
    --tasks-json TASKS.json --dry-run

Omit --dry-run only to record reconciliation sidecars. Gap files and inbox lines
are retained byte for byte. Keep exports and plans local: prompts can be private.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import common
from .briefs import AUTOMATED_TURN_PREFIX


REASON = 'automated scheduled prompt (no marker at the time); operator_text accepted sdk origin'


def recorded_schedule_ids(root):
    """Read recorded task ids, never construct them from node names or titles."""
    local = json.loads((root / '.brain/local.json').read_text())
    ids = set()
    for field in ('schedules', 'host_wake_tasks'):
        for task in local.get(field, {}).values():
            if isinstance(task, str) and task:
                ids.add(task)
    for node in local.get('nodes', {}).values():
        for field in ('wake', 'wake_default'):
            url = node.get(field)
            if not isinstance(url, str):
                continue
            # Wake URLs store the actual task id in one percent-encoded path
            # component. Never emit the URL's bearer capability in a plan.
            for part in urlsplit(url).path.split('/'):
                task = unquote(part)
                if task.startswith('scheduled-task:'):
                    ids.add(task)
    return ids


def known_tasks(root, exported, include_unbound=False):
    """Join exported prompts to recorded ids, optionally retaining historical tasks."""
    rows = exported.get('tasks') if isinstance(exported, dict) else exported
    if not isinstance(rows, list):
        raise ValueError('tasks JSON must be an array or an object with tasks')
    ids = recorded_schedule_ids(root)
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('scheduledTaskId'), str):
            continue
        if row['scheduledTaskId'] not in ids and not include_unbound:
            continue
        sid = row['scheduledTaskId']
        if sid in found:
            raise ValueError('duplicate recorded task in export: ' + sid)
        if not isinstance(row.get('prompt'), str) or not row['prompt'] or type(row.get('enabled')) is not bool:
            raise ValueError('recorded task needs exact prompt and enabled flag: ' + sid)
        found[sid] = {'scheduledTaskId': sid, 'prompt': row['prompt'], 'enabled': row['enabled']}
    return [found[sid] for sid in sorted(found)], sorted(ids - found.keys())


def marker_plan(root, exported):
    tasks, missing = known_tasks(root, exported)
    updates = [{'scheduledTaskId': row['scheduledTaskId'],
                'prompt': AUTOMATED_TURN_PREFIX + '\n' + row['prompt']}
               for row in tasks if row['enabled'] and not row['prompt'].startswith(AUTOMATED_TURN_PREFIX)]
    return {'tasks': tasks, 'updates': updates, 'missing': missing, 'complete': not missing}


def reconcile_gaps(root, exported, dry_run=True):
    """All quotes must be exact substrings of ONE known schedule prompt.

    No meaning is inferred, and no partial match can close a mixed operator gap.
    Disabled schedules remain valid historical provenance. A sidecar binds the
    disposition to the original gap bytes and the supporting prompt's digest.
    """
    # A retired schedule can still explain a historical gap. Its exact exported
    # prompt remains evidence even after its local binding has been replaced.
    tasks, missing = known_tasks(root, exported, include_unbound=True)
    base = root / '.brain/instruction-capture'
    matches, warnings = [], []
    for path in sorted((base / 'gaps').glob('*.json')):
        try:
            common.safe_target(path)
            raw = path.read_bytes()
            gap = json.loads(raw)
            if not isinstance(gap, dict) or gap.get('kind') != 'instruction-capture-gap':
                raise ValueError('invalid gap record')
            items = gap.get('items')
            if not isinstance(items, list) or not items or any(
                    not isinstance(item, dict) or not isinstance(item.get('quote'), str)
                    or not item['quote'] for item in items):
                continue
            task = next((row for row in tasks if all(item['quote'] in row['prompt'] for item in items)), None)
            if task is None:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            target = base / 'reconciliations' / path.name
            common.safe_target(target)
            if target.exists():
                previous = json.loads(target.read_text())
                if previous.get('gap_sha256') == digest and previous.get('disposition') == 'non_operator':
                    continue
                raise ValueError('existing reconciliation does not match gap')
            record = {'gap': path.name, 'gap_sha256': digest, 'disposition': 'non_operator',
                      'reason': REASON, 'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      'scheduledTaskId': task['scheduledTaskId'],
                      'prompt_sha256': hashlib.sha256(task['prompt'].encode()).hexdigest()}
            if not dry_run:
                if path.read_bytes() != raw:
                    raise ValueError('gap changed during reconciliation')
                common.atomic_write(target, json.dumps(record, ensure_ascii=False) + '\n', private=True, create=True)
            matches.append(record)
        except (OSError, ValueError, UnicodeError) as error:
            warnings.append({'gap': path.name, 'error': str(error)})
    return {'dry_run': dry_run, 'reconciliations': matches, 'warnings': warnings, 'missing': missing,
            'complete': not (missing or warnings)}


def repair_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('plan', 'reconcile'))
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--tasks-json', required=True, type=Path, help='raw local host schedule export; no host calls')
    parser.add_argument('--output', type=Path, help='private JSON plan/result file')
    parser.add_argument('--dry-run', action='store_true', help='list reconciliation candidates without writing sidecars')
    args = parser.parse_args(argv)
    if args.action == 'plan' and not args.output:
        parser.error('plan requires --output')
    try:
        exported = json.loads(args.tasks_json.read_text())
        if args.action == 'plan':
            result = marker_plan(args.root, exported)
        elif args.dry_run:
            result = reconcile_gaps(args.root, exported)
        else:
            from .instance import Instance, brain_lock
            with brain_lock(Instance(args.root)):
                result = reconcile_gaps(args.root, exported, dry_run=False)
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            common.atomic_write(args.output, encoded, private=True, create=True)
        else:
            print(encoded, end='')
        return 0 if result['complete'] else 1
    except (OSError, ValueError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(repair_main())
