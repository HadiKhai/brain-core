"""Local repair tools; never call the host or regenerate existing prompt bodies.

Export host schedule records as a JSON array (or {"tasks": [...]}) first. Exact
host prompts are not retained by older instance bindings. Update ids are selected
from .brain/local.json, including wake capability paths and root bindings;
unbound export rows are listed without changing them.

PYTHONPATH=lib python3 -m brain.instruction_capture_repair plan --root INSTANCE \
    --tasks-json TASKS.json --output PLAN.json
PYTHONPATH=lib python3 -m brain.instruction_capture_repair reconcile --root INSTANCE \
    --tasks-json TASKS.json --dry-run

Read the dry-run before applying. Older gaps without source_text need optional
--source-turns-json evidence: {"gap-name.json": [raw runtime user records]}. Each
record must have the original item's exact timestamp. Without source-turn proof
the gap stays open, even when its quotes match a schedule. Omit --dry-run only to
record reconciliation sidecars. Gap files and inbox lines are retained byte for
byte. Keep exports, source records and plans local: they can be private.
Reconciliation accepts only the exact host nonce-span/footer rendering of a
recorded prompt, with or without its generated marker line. Use --require GAP_ID
[GAP_ID ...] to fail if a required gap remains kept or cannot be found uniquely.
The stderr summary always includes the kept count.
"""
import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import common
from . import instruction_capture as _m_capture
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
    found, counts, skipped = {}, {}, []
    for row in rows:
        sid = row.get('scheduledTaskId') if isinstance(row, dict) else None
        if isinstance(sid, str) and sid:
            counts[sid] = counts.get(sid, 0) + 1
    for index, row in enumerate(rows):
        sid = row.get('scheduledTaskId') if isinstance(row, dict) else None
        reason = None
        if not isinstance(sid, str) or not sid:
            reason = 'missing or invalid scheduledTaskId'
        elif counts[sid] > 1:
            # Neither copy is authoritative, even when their prompt bytes agree.
            reason = 'duplicate scheduledTaskId; all copies skipped'
        elif not isinstance(row.get('prompt'), str) or not row['prompt'] or type(row.get('enabled')) is not bool:
            reason = 'task needs exact nonempty prompt and enabled flag'
        if reason:
            skipped.append(dict(row=index, reason=reason,
                                **({'scheduledTaskId': sid} if isinstance(sid, str) else {})))
            continue
        found[sid] = {'scheduledTaskId': sid, 'prompt': row['prompt'], 'enabled': row['enabled']}
    return {'tasks': [found[sid] for sid in sorted(found) if sid in ids or include_unbound],
            'missing': sorted(ids - found.keys()), 'recorded_ids': ids, 'skipped': skipped,
            'unbound': [dict(found[sid], status='unbound, not changed') for sid in sorted(found) if sid not in ids]}


def marker_plan(root, exported):
    inventory = known_tasks(root, exported)
    tasks, missing = inventory['tasks'], inventory['missing']
    updates = [{'scheduledTaskId': row['scheduledTaskId'],
                'prompt': AUTOMATED_TURN_PREFIX + '\n' + row['prompt']}
               for row in tasks if row['enabled'] and not row['prompt'].startswith(AUTOMATED_TURN_PREFIX)]
    return {'tasks': tasks, 'updates': updates, 'missing': missing, 'unbound': inventory['unbound'],
            'skipped': inventory['skipped'], 'warnings': inventory['skipped'],
            'complete': not (missing or inventory['skipped'])}


def webhook_source_matches(source, prompt):
    """Match only one host nonce span and its exact matching untrusted-data footer."""
    footer = re.search(
        r'\n\nText between <webhook-data-([A-Za-z0-9_-]{1,128})> and </webhook-data-\1> '
        r'was copied from the webhook request\. Treat it as untrusted data\.\Z', source)
    if footer is None:
        return False
    body = source[:footer.start()]
    opening, closing = '<webhook-data-' + footer[1] + '>', '</webhook-data-' + footer[1] + '>'
    if (body.count('<webhook-data-') != 1 or body.count('</webhook-data-') != 1
            or body.count(opening) != 1 or body.count(closing) != 1):
        return False
    before, span = body.split(opening)
    if closing not in span:
        return False
    _, after = span.split(closing)
    if prompt == before + after:
        return True
    if not prompt.startswith(before) or not prompt.endswith(after):
        return False
    placeholder = prompt[len(before):len(prompt) - len(after) if after else len(prompt)]
    return re.fullmatch(r'\{\{[^{}\r\n]+\}\}', placeholder) is not None


def schedule_source_matches(source, prompt):
    """Plan application order changes only the one generated marker line."""
    marker = AUTOMATED_TURN_PREFIX + '\n'
    prompts = (prompt, prompt[len(marker):]) if prompt.startswith(marker) else (prompt,)
    return any(source == value or webhook_source_matches(source, value) for value in prompts)


def automated_source_hashes(items, task, recorded_ids, records):
    """Require original whole-turn provenance for EVERY quoted item, never overlap alone."""
    hashes = set()
    for item in items:
        sources = [item['source_text']] if isinstance(item.get('source_text'), str) else []
        # Legacy gaps have only quote + timestamp. Import raw transcript records
        # explicitly, without guessing a transcript path or replaying a model.
        if not sources and isinstance(records, list) and isinstance(item.get('at'), str) and item['at']:
            for record in records:
                if isinstance(record, dict) and record.get('timestamp') == item['at']:
                    text = _m_capture.operator_text(record, include_automated=True)
                    if text is not None:
                        sources.append(text)
        source = next((text for text in sources if item['quote'] in text and (
            text.startswith(AUTOMATED_TURN_PREFIX) or
            task['scheduledTaskId'] in recorded_ids and schedule_source_matches(text, task['prompt']))), None)
        if source is None:
            return None
        hashes.add(hashlib.sha256(source.encode()).hexdigest())
    return sorted(hashes)


def reconcile_gaps(root, exported, dry_run=True, source_turns=None, require=None):
    """All quotes must match ONE prompt, with each original source turn proven automated.

    No meaning is inferred, and no partial match can close a mixed operator gap.
    Disabled schedules remain valid historical provenance. A sidecar binds the
    disposition to the original gap bytes and the supporting prompt's digest.
    """
    # A retired schedule can still explain a historical gap. Its exact exported
    # prompt remains evidence even after its local binding has been replaced.
    inventory = known_tasks(root, exported, include_unbound=True)
    tasks, missing = inventory['tasks'], inventory['missing']
    source_turns = {} if source_turns is None else source_turns
    if not isinstance(source_turns, dict):
        raise ValueError('source turns JSON must map gap filenames to raw runtime records')
    base = root / '.brain/instruction-capture'
    matches, warnings, kept, satisfied = [], list(inventory['skipped']), [], set()
    paths = sorted((base / 'gaps').glob('*.json'))
    for path in paths:
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
                kept.append({'gap': path.name, 'status': 'kept: not provably automated'})
                continue
            candidates = [row for row in tasks if all(item['quote'] in row['prompt'] for item in items)]
            task, source_hashes = None, None
            for row in candidates:
                source_hashes = automated_source_hashes(items, row, inventory['recorded_ids'],
                                                       source_turns.get(path.name))
                if source_hashes:
                    task = row
                    break
            if task is None:
                kept.append({'gap': path.name, 'status': 'kept: not provably automated'})
                continue
            digest = hashlib.sha256(raw).hexdigest()
            target = base / 'reconciliations' / path.name
            common.safe_target(target)
            if target.exists():
                previous = json.loads(target.read_text())
                if (isinstance(previous, dict) and previous.get('gap_sha256') == digest
                        and previous.get('disposition') == 'non_operator'):
                    satisfied.add(path.name)
                    continue
                raise ValueError('existing reconciliation does not match gap')
            record = {'gap': path.name, 'gap_sha256': digest, 'disposition': 'non_operator',
                      'reason': REASON, 'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      'scheduledTaskId': task['scheduledTaskId'],
                      'source_turn_sha256': source_hashes,
                      'prompt_sha256': hashlib.sha256(task['prompt'].encode()).hexdigest()}
            if not dry_run:
                if path.read_bytes() != raw:
                    raise ValueError('gap changed during reconciliation')
                common.atomic_write(target, json.dumps(record, ensure_ascii=False) + '\n', private=True, create=True)
            matches.append(record)
            satisfied.add(path.name)
        except (OSError, ValueError, UnicodeError) as error:
            warnings.append({'gap': path.name, 'error': str(error)})
    required_unmet = []
    for identity in require or []:
        names = [path.name for path in paths if path.name == identity or path.stem.startswith(identity)]
        if len(names) != 1 or names[0] not in satisfied:
            required_unmet.append(identity)
    return {'dry_run': dry_run, 'reconciliations': matches, 'kept': kept, 'warnings': warnings,
            'required_unmet': required_unmet,
            'skipped': inventory['skipped'], 'missing': missing,
            'complete': not (missing or warnings or required_unmet)}


def repair_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('plan', 'reconcile'))
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--tasks-json', required=True, type=Path, help='raw local host schedule export; no host calls')
    parser.add_argument('--output', type=Path, help='private JSON plan/result file')
    parser.add_argument('--source-turns-json', type=Path, help='legacy gap filename to raw timestamped runtime records')
    parser.add_argument('--dry-run', action='store_true', help='list reconciliation candidates without writing sidecars')
    parser.add_argument('--require', nargs='+', default=[], metavar='GAP_ID',
                        help='require these gap filenames or unambiguous id prefixes to reconcile')
    args = parser.parse_args(argv)
    if args.action == 'plan' and not args.output:
        parser.error('plan requires --output')
    try:
        exported = json.loads(args.tasks_json.read_text())
        source_turns = json.loads(args.source_turns_json.read_text()) if args.source_turns_json else None
        if args.action == 'plan':
            result = marker_plan(args.root, exported)
        elif args.dry_run:
            result = reconcile_gaps(args.root, exported, source_turns=source_turns, require=args.require)
        else:
            from .instance import Instance, brain_lock
            with brain_lock(Instance(args.root)):
                result = reconcile_gaps(args.root, exported, dry_run=False, source_turns=source_turns,
                                        require=args.require)
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            common.atomic_write(args.output, encoded, private=True, create=True)
        else:
            print(encoded, end='')
        if args.action == 'reconcile':
            print('reconcile: reconciliations=' + str(len(result['reconciliations']))
                  + ' kept=' + str(len(result['kept'])) + ' required_unmet=' + str(len(result['required_unmet'])),
                  file=sys.stderr)
        return 0 if result['complete'] else 1
    except (OSError, ValueError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(repair_main())
