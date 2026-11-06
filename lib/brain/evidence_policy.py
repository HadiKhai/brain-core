"""Evidence rules enforced at the command that would break them.

A relaunch brief that carries an inherited stop or no-retry clause, a contract
without its declared reading list or scored-work fields, a review of scored work
before a passing real-data result, a model choice measured on one provider, a
score handed back without its scorer and cases read, a scored hand-back that
leads with counts, a second setup correction and a design-referenced UI without
paired screenshots and an independent checklist each refuse here. Meaning (does
inherited text forbid rechecking) is judged by an AI with a schema; code checks
only structure, identity and references.
"""
import hashlib
import json
import math
import os
import re
from pathlib import Path

from . import common as _m_common

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_instance = _m_common.LazyModule(__package__, 'instance')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_ui = _m_common.LazyModule(__package__, 'ui')

SHA256 = re.compile(r'[0-9a-f]{64}')
KINDS = {'real-result': 'real_result', 'model-selection': 'model_selection', 'score-read': 'score_read',
         'setup-correction': 'setup_correction', 'design-check': 'design_check'}
DATA_CLASSES = ('public', 'internal', 'restricted', 'sealed')
NEVER_LEAVES = ('restricted', 'sealed')
CHECKLIST = ('buttons', 'spacing', 'links', 'validation', 'navigation', 'consistency')
QUALITY_HEADING = '## What was removed / kept'
KEY_LINE = re.compile(r'[ \t]*(?:[-*][ \t]+)?([a-z_][a-z0-9_]*)[ \t]*:[ \t]*(.*?)[ \t]*')
ITEM_LINE = re.compile(r'[ \t]*[-*+][ \t]+(.+?)[ \t]*')


def repo_of(inst, n):
    return n['home_path'] if n.get('external') else inst.root


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def local_file(inst, n, value):
    """A declared artifact path: absolute, or relative to the node's home."""
    if not text(value):
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_of(inst, n) / path
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return path if path.is_file() else None


def sections(contract_text):
    return {k.lower(): v for k, v in _m_ui.md_sections(contract_text or '').items()}


def contract_text(n, row):
    path = _m_jobs.contract_file(n, row) if row else None
    return path.read_text() if path and path.is_file() else ''


def key_values(body):
    fields = {}
    for line in (body or '').splitlines():
        match = KEY_LINE.fullmatch(line)
        if match:
            fields.setdefault(match[1], []).append(match[2])
    return fields


def items(body):
    return [m[1].strip('`') for m in (ITEM_LINE.fullmatch(line) for line in (body or '').splitlines()) if m]


# Contract declarations ---------------------------------------------------------

def scored_work(contract):
    """The contract's ``## Scored work`` declarations, or None when the job is not scored."""
    body = sections(contract).get('scored work')
    if body is None:
        return None
    fields = key_values(body)
    return {k: v[0] if len(v) == 1 else v for k, v in fields.items()}


def scored_problems(inst, n, scored):
    problems = []
    for key in ('test_set', 'test_set_sha256', 'threshold', 'cap', 'scorer'):
        if not isinstance(scored.get(key), str) or not scored[key]:
            problems.append('## Scored work needs one ' + key + ': line')
    if problems:
        return problems
    path = local_file(inst, n, scored['test_set'])
    if not SHA256.fullmatch(scored['test_set_sha256']):
        problems.append('test_set_sha256 must be 64 lowercase hex')
    elif path is None:
        problems.append('test_set must name a readable file (absolute or relative to the home)')
    elif sha256_file(path) != scored['test_set_sha256']:
        problems.append('test_set_sha256 does not match the frozen test set')
    try:
        threshold = float(scored['threshold'])
        if not math.isfinite(threshold) or threshold <= 0:
            raise ValueError
    except ValueError:
        problems.append('threshold must be a positive number')
    if not re.fullmatch(r'[1-9][0-9]{0,5}', scored['cap']):
        problems.append('cap must be a positive whole number of items')
    if Path(scored['scorer']).is_absolute() or '..' in Path(scored['scorer']).parts:
        problems.append('scorer must be a path inside the repository')
    choice = scored.get('model_choice', 'no')
    if choice not in ('yes', 'no'):
        problems.append('model_choice must be yes or no')
    if choice == 'yes':
        if scored.get('data_class') not in DATA_CLASSES:
            problems.append('model_choice needs data_class: ' + '|'.join(DATA_CLASSES))
        if not re.fullmatch(r'[2-9]', scored.get('providers', '2')):
            problems.append('providers must be a whole number from 2 to 9')
    return problems


def design_screens(contract):
    """``## Design reference`` lines ``- <screen>: <reference artifact>``."""
    body = sections(contract).get('design reference')
    if body is None:
        return None
    screens = {}
    for entry in items(body):
        screen, separator, reference = entry.partition(':')
        if not separator or not screen.strip() or not reference.strip():
            screens[None] = entry
            continue
        screens.setdefault(screen.strip(), []).append(reference.strip().strip('`'))
    return screens


def design_problems(inst, n, screens):
    if None in screens or not screens:
        return ['## Design reference lists each touched screen as - <screen>: <reference screenshot or prototype file>']
    problems = []
    for screen, refs in screens.items():
        if len(refs) != 1:
            problems.append('design screen ' + screen + ' is listed more than once')
        elif local_file(inst, n, refs[0]) is None:
            problems.append('design reference for ' + screen + ' is not a readable file')
    return problems


def reading_required(inst, n):
    cfg = inst.cfg if isinstance(inst.cfg, dict) else {}
    return str(n['fm'].get('contract_reading_list', cfg.get('contract_reading_list', ''))).lower() in (
        'required', 'true', 'yes', '1', 'on')


def reading_problems(inst, n, contract):
    found = sections(contract)
    entries, proven = items(found.get('reading list')), items(found.get('proven'))
    problems = []
    if not entries:
        problems.append('## Reading list with - <path>[:<first>-<last>] lines (exact files and lines to read first)')
    for entry in entries:
        match = re.fullmatch(r'([^\s:]+)(?::([1-9][0-9]*)-([1-9][0-9]*))?(?:\s+.*)?', entry)
        path = local_file(inst, n, match[1]) if match else None
        if path is None:
            problems.append('reading list entry is not a readable home file: ' + entry[:80])
            continue
        if match[2]:
            first, last = int(match[2]), int(match[3])
            with open(path, 'rb') as stream:
                count = sum(1 for _ in stream)
            if first > last or last > count:
                problems.append('reading list range is outside ' + match[1] + ' (' + str(count) + ' lines)')
    if not proven or any(not re.search(r'\bRef:\s*\S', entry) for entry in proven):
        problems.append('## Proven with - <what is proven, not to re-check>; Ref: <test, commit or file> lines')
    return problems


def contract_problems(inst, n, contract):
    """Structural problems in a contract's evidence declarations, checked at registration."""
    problems = []
    scored = scored_work(contract)
    if scored is not None:
        problems += scored_problems(inst, n, scored)
    screens = design_screens(contract)
    if screens is not None:
        problems += design_problems(inst, n, screens)
    if reading_required(inst, n):
        problems += reading_problems(inst, n, contract)
    return problems


# Receipts ----------------------------------------------------------------------

def receipt_dir(inst, n, row):
    return (inst.root / '.brain/evidence' / hashlib.sha256(n['id'].encode()).hexdigest() /
            _m_jobs.job_key(row))


def receipt_path(inst, n, row, kind, head):
    if not SHA256.fullmatch(head or '') and not re.fullmatch(r'[0-9a-f]{40}', head or ''):
        raise ValueError('invalid evidence head')
    return receipt_dir(inst, n, row) / (kind + '-' + head + '.json')


def branch_head(inst, n, branch):
    result = _m_common.git('rev-parse', '--verify', 'refs/heads/' + branch + '^{commit}', cwd=repo_of(inst, n))
    if result.returncode:
        raise ValueError('cannot resolve the job branch')
    return result.stdout.strip()


def read_receipt(inst, n, row, kind, head):
    path = receipt_path(inst, n, row, kind, head)
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    validate_receipt(inst, n, row, head, kind, data)
    return data


def corrections(inst, n, row):
    folder = receipt_dir(inst, n, row)
    return sorted(folder.glob('setup_correction-*.json')) if folder.is_dir() else []


def validate_receipt(inst, n, row, head, kind, data):
    if (not isinstance(data, dict) or data.get('version') != 1 or data.get('kind') != kind or
            data.get('node') != n['id'] or data.get('branch') != row['Job'] or
            data.get('job_key') != _m_jobs.job_key(row) or data.get('head') != head):
        raise ValueError(kind.replace('_', ' ') + ' receipt needs version 1, this kind, node, branch, job_key and the exact head')
    contract = contract_text(n, row)
    validator = {'real_result': validate_real_result, 'model_selection': validate_model_selection,
                 'score_read': validate_score_read, 'setup_correction': validate_setup_correction,
                 'design_check': validate_design_check}[kind]
    validator(inst, n, row, head, contract, data)
    return data


def require_scored(inst, n, contract, what):
    scored = scored_work(contract)
    if scored is None or scored_problems(inst, n, scored):
        raise ValueError(what + ' needs a registered contract with a valid ## Scored work section')
    path = local_file(inst, n, scored['test_set'])
    if path is None or sha256_file(path) != scored['test_set_sha256']:
        raise ValueError('the frozen test set changed since the contract; register a new contract')
    return scored


def validate_real_result(inst, n, row, head, contract, data):
    scored = require_scored(inst, n, contract, 'a real result')
    check = data.get('test_set_check')
    if data.get('test_set_sha256') != scored['test_set_sha256']:
        raise ValueError('real result must be on the contract\'s frozen test set (test_set_sha256)')
    if type(data.get('items')) is not int or not 1 <= data['items'] <= int(scored['cap']):
        raise ValueError('real result items must be from 1 to the contract cap ' + scored['cap'])
    if not number(data.get('score')) or data['score'] < 0:
        raise ValueError('real result needs a nonnegative score')
    if not number(data.get('threshold')) or data['threshold'] != float(scored['threshold']):
        raise ValueError('real result threshold must equal the contract threshold ' + scored['threshold'])
    if not isinstance(check, dict) or not number(check.get('reference_score')) or not text(check.get('method')):
        raise ValueError('real result needs test_set_check {reference_score, method}: proof the test data can pass')
    if check['reference_score'] < data['threshold']:
        raise ValueError('the fixed test set cannot reach its own threshold; repair the test set before scoring')
    if not text(data.get('run_ref')):
        raise ValueError('real result needs run_ref: the stored run output it was scored from')


def validate_model_selection(inst, n, row, head, contract, data):
    scored = require_scored(inst, n, contract, 'a model selection')
    if scored.get('model_choice') != 'yes':
        raise ValueError('declare model_choice: yes in ## Scored work before recording a model selection')
    if scored['data_class'] in NEVER_LEAVES or data.get('data_class') != scored['data_class']:
        raise ValueError('model selection data_class must match the contract; restricted and sealed data never '
                         'leave, so compare providers on public or internal items only')
    if data.get('test_set_sha256') != scored['test_set_sha256'] or type(data.get('items')) is not int or data['items'] < 1:
        raise ValueError('model selection must name the frozen test set and its item count')
    candidates = data.get('candidates')
    if not isinstance(candidates, list) or len(candidates) < 2:
        raise ValueError('model selection needs at least two measured candidates')
    seen = set()
    for candidate in candidates:
        if (not isinstance(candidate, dict) or not all(text(candidate.get(k)) for k in ('provider', 'model', 'run_ref'))
                or not SHA256.fullmatch(str(candidate.get('run_sha256'))) or
                not all(number(candidate.get(k)) and candidate[k] >= 0 for k in ('score', 'cost_usd', 'latency_s'))):
            raise ValueError('each candidate needs provider, model, run_ref, run_sha256, score, cost_usd and latency_s')
        identity = (candidate['provider'], candidate['model'])
        if identity in seen:
            raise ValueError('candidates must be distinct provider/model pairs')
        seen.add(identity)
        path = local_file(inst, n, candidate['run_ref'])
        if path is None or sha256_file(path) != candidate['run_sha256']:
            raise ValueError('candidate run_ref must be the stored run receipt with run_sha256')
        try:
            run = json.loads(path.read_text())
        except (OSError, ValueError, UnicodeError):
            raise ValueError('candidate run receipt must be JSON') from None
        if (not isinstance(run, dict) or run.get('provider') != candidate['provider'] or
                run.get('model') != candidate['model'] or run.get('items') != data['items'] or
                run.get('test_set_sha256') != scored['test_set_sha256']):
            raise ValueError('candidate identity, items and test set must match its own run receipt')
    providers = {provider for provider, _ in seen}
    if len(providers) < int(scored.get('providers', '2')):
        raise ValueError('a model choice is measured across providers: ' + str(len(providers)) + ' provider(s) '
                         'measured, ' + scored.get('providers', '2') + ' required')
    chosen = data.get('chosen')
    if not isinstance(chosen, dict) or (chosen.get('provider'), chosen.get('model')) not in seen:
        raise ValueError('the chosen model must be one of the measured candidates')


def scorer_blob(inst, n, head, path):
    result = _m_common.git('rev-parse', '--verify', '--quiet', head + ':' + path, cwd=repo_of(inst, n))
    return result.stdout.strip() if not result.returncode else None


def validate_score_read(inst, n, row, head, contract, data):
    scored = require_scored(inst, n, contract, 'a score read')
    scorer = data.get('scorer')
    blob = scorer_blob(inst, n, head, scored['scorer'])
    if blob is None:
        raise ValueError('the contract scorer ' + scored['scorer'] + ' is not in the head; a score needs its scorer')
    if not isinstance(scorer, dict) or scorer.get('path') != scored['scorer'] or scorer.get('blob') != blob:
        raise ValueError('score read must name the contract scorer and its blob at this head (git rev-parse <head>:<path>)')
    cases = data.get('cases')
    if (not isinstance(cases, list) or len(cases) != 3 or
            any(not isinstance(c, dict) or not all(text(c.get(k)) for k in ('input_ref', 'output_ref', 'check'))
                for c in cases) or len({c['input_ref'] for c in cases}) != 3):
        raise ValueError('score read needs exactly 3 real cases, each with distinct input_ref, output_ref and check')
    if not text(data.get('measures')) or not text(data.get('does_not_measure')):
        raise ValueError('score read needs measures and does_not_measure in plain words')


def validate_setup_correction(inst, n, row, head, contract, data):
    path = _m_jobs.contract_file(n, row)
    if not path.is_file() or data.get('contract_sha256') != sha256_file(path):
        raise ValueError('a setup correction binds the original registered contract (contract_sha256)')
    allowed = key_values(contract).get('setup_corrections', ['1'])
    if len(allowed) != 1 or allowed[0] not in ('0', '1'):
        raise ValueError('contract setup_corrections must be 0 or 1')
    if allowed[0] == '0':
        raise ValueError('this contract allows no setup correction (setup_corrections: 0); hand back for its disposition')
    if data.get('correction') not in ('selector', 'start_format'):
        raise ValueError('a setup correction is a selector or start_format fix only')
    if not text(data.get('before')) or not text(data.get('after')) or data['before'] == data['after']:
        raise ValueError('a setup correction needs a different before and after')
    if data['correction'] == 'selector':
        file, separator, name = data['after'].partition('::')
        source = _m_common.git('show', head + ':' + file, cwd=repo_of(inst, n)) if separator else None
        last = name.split('::')[-1].split('[')[0]
        if (source is None or source.returncode or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', last) or
                not re.search(r'^\s*(?:async\s+)?(?:def|class)\s+' + re.escape(last) + r'\b', source.stdout, re.M)):
            raise ValueError('the corrected selector must exist in the source at this head (path::name)')
    if data.get('bounds') != 'original':
        raise ValueError('a setup correction runs within the original bounds (bounds: original)')


def validate_design_check(inst, n, row, head, contract, data):
    screens = design_screens(contract)
    if not screens or design_problems(inst, n, screens):
        raise ValueError('a design check needs a registered contract with a valid ## Design reference section')
    pairs = data.get('screens')
    if not isinstance(pairs, list) or len(pairs) != len(screens):
        raise ValueError('a design check pairs every touched screen in ## Design reference')
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get('screen') not in screens:
            raise ValueError('a design check pairs every touched screen in ## Design reference')
        reference, build = pair.get('reference'), pair.get('build')
        declared = local_file(inst, n, screens[pair['screen']][0])
        for artifact in (reference, build):
            path = local_file(inst, n, artifact.get('path')) if isinstance(artifact, dict) else None
            if path is None or artifact.get('sha256') != sha256_file(path):
                raise ValueError('each screen needs reference and build screenshots with sha256')
        if local_file(inst, n, reference['path']) != declared or reference['sha256'] == build['sha256']:
            raise ValueError('reference must be the declared design file and differ from the build screenshot')
    if len({pair['screen'] for pair in pairs}) != len(screens):
        raise ValueError('a design check pairs every touched screen in ## Design reference')
    checklist = data.get('checklist')
    if (not isinstance(checklist, dict) or set(checklist) != set(CHECKLIST) or
            any(not isinstance(v, dict) or v.get('verdict') not in ('pass', 'fail') or not text(v.get('note'))
                for v in checklist.values())):
        raise ValueError('a design checklist rates ' + ', '.join(CHECKLIST) + ' with verdict pass/fail and a note')
    checker = data.get('checker')
    if not isinstance(checker, dict) or not all(text(checker.get(k)) for k in ('threadId', 'runtime', 'model')):
        raise ValueError('a design check names its checker threadId, runtime and model')
    known = set()
    for ref, _ in _m_jobs.bound_worker_threads(n, row['Job']):
        known.update(_m_jobs.thread_reference_ids(ref))
    if checker['runtime'] not in inst.runtimes or checker['runtime'] == row.get('Runtime') or checker['threadId'] in known:
        raise ValueError('the design checklist is checked by a different runtime than the builder, in another thread')


def record_receipt(inst, n, branch, kind, data):
    with _m_instance.brain_lock(inst):
        inst = _m_instance.Instance(inst.root)
        n = inst.node(n['id'])
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        if not row:
            raise ValueError('evidence needs an open job')
        head = branch_head(inst, n, branch)
        if kind == 'setup_correction' and corrections(inst, n, row):
            raise ValueError('the one setup correction for this job is spent; hand back blocked for a disposition')
        validate_receipt(inst, n, row, head, kind, data)
        serialized = json.dumps(data, sort_keys=True) + '\n'
        _m_guard.no_terms_in(n, serialized, 'evidence receipt')
        path = receipt_path(inst, n, row, kind, head)
        _m_common.safe_target(path)
        if kind != 'setup_correction' and path.exists() and path.read_text() != serialized:
            raise ValueError('this head already has a different ' + kind.replace('_', ' ') + ' receipt')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _m_common.atomic_write(path, serialized, private=True)
        if kind == 'real_result' and data['score'] == 0:
            from . import transitions
            transitions.append_inbox(n['dir'], 'brain evidence', 'zero real score for ' + branch + ' at ' +
                                     head[:12] + ': diagnose now; no review until it passes', inst)
        return path


def cmd_evidence(a):
    _m_instance.unlock()
    inst = _m_nodes.load()
    n = inst.node(a.id)
    try:
        data = json.loads(Path(a.file).expanduser().read_text())
        path = record_receipt(inst, n, a.branch, KINDS[a.kind], data)
    except (OSError, ValueError, TypeError, KeyError) as error:
        _m_common.die(_m_guard.redact(str(error), _m_guard.never_terms(n)))
    print(a.kind + ' receipt recorded for the exact head (supplied evidence; the core does not rerun it): ' +
          _m_common.tilde(path))


# Gates -------------------------------------------------------------------------

def review_admission(inst, n, row, head):
    """Reasons a review may not start: scored work reviews only a passing real result."""
    contract = contract_text(n, row)
    scored = scored_work(contract)
    if scored is None:
        return []
    command = 'brain evidence ' + n['id'] + ' --branch ' + row['Job'] + ' --kind '
    try:
        result = read_receipt(inst, n, row, 'real_result', head)
    except (OSError, ValueError) as error:
        return [str(error)]
    if result is None:
        return ['no review before a real result: run a small capped check on real data against the fixed test '
                'set and record it with `' + command + 'real-result --file <json>`']
    if result['score'] == 0:
        return ['the real score is zero (sent to the inbox): diagnose it; no review until it passes']
    if result['score'] < result['threshold']:
        return ['real score ' + format(result['score'], 'g') + ' is below the threshold ' +
                format(result['threshold'], 'g') + ': no review until it passes']
    if scored.get('model_choice') == 'yes':
        try:
            selection = read_receipt(inst, n, row, 'model_selection', head)
        except (OSError, ValueError) as error:
            return [str(error)]
        if selection is None:
            return ['a model choice is measured across providers on the test set first: `' + command +
                    'model-selection --file <json>`']
    return []


def handback_problems(inst, n, row, a):
    """A scored hand-back reports a number only after its scorer is read, and quality before counts."""
    scored = scored_work(contract_text(n, row))
    if scored is None or a.state not in ('done', 'blocked', 'failed'):
        return []
    problems = []
    try:
        head = branch_head(inst, n, row['Job'])
        if getattr(a, 'result', None) is not None and read_receipt(inst, n, row, 'score_read', head) is None:
            problems.append('a score is reported only after reading its scorer and 3 real cases: `brain evidence ' +
                            n['id'] + ' --branch ' + row['Job'] + ' --kind score-read --file <json>`, or hand '
                            'back without --result and name the number as unread')
    except (OSError, ValueError) as error:
        problems.append(str(error))
    if a.state == 'done':
        problems += quality_problems(getattr(a, 'report', None))
    return problems


def quality_problems(report):
    """The report opens with what was removed / kept, sorted right and wrong with examples."""
    from .dissection import MAX_REPORT
    try:
        path = Path(report).expanduser() if report else None
        body = path.read_bytes()[:MAX_REPORT].decode('utf-8') if path and path.is_file() else ''
    except (OSError, UnicodeError):
        body = ''
    if not body:
        return ['a scored hand-back needs --report opening with "' + QUALITY_HEADING + '"']
    first, group, fence, groups = None, None, None, {'right': [], 'wrong': [], 'none': []}
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            fence = None if fence == stripped[0] else (fence or stripped[0])
            continue
        if fence is not None:
            continue
        if line.startswith('## '):
            if first is not None:
                break
            first = line.strip()
            continue
        if first != QUALITY_HEADING:
            continue
        if line.startswith('### '):
            group = line.strip()[4:].strip().lower()
            continue
        if group not in ('right', 'wrong'):
            continue
        example = re.fullmatch(r'\d+\.\s+Ref:\s*(\S+);\s*Source:\s*(\S+);\s*(Kept|Removed):\s*(\S.*)', line.strip())
        if example:
            groups[group].append(example.groups())
        elif group == 'wrong' and re.fullmatch(r'None found:\s*\S.*', line.strip()):
            groups['none'].append(line)
    if first != QUALITY_HEADING:
        return ['a scored hand-back report opens (first ## section, before any totals) with "' + QUALITY_HEADING + '"']
    problems = []
    if not groups['right']:
        problems.append('### Right needs numbered examples: N. Ref: <item>; Source: <source>; Kept|Removed: <why>')
    if not groups['wrong'] and not groups['none']:
        problems.append('### Wrong needs numbered examples, or "None found: <how the items were read>"')
    return problems


# Relaunch briefs ---------------------------------------------------------------

INHERITED_SYSTEM = (
    'You read the text of a brief that relaunches a software job after an earlier agent worked on it. '
    'Decide whether the text tells the new agent not to re-check or retry something because of what an '
    'earlier agent concluded: for example that a retry is forbidden, the only attempt is spent, or that the '
    'cause is unknown so it must stop. A limit the operator or the job contract set themselves (with a '
    'decision, an order or a contract field named as the source) is operator authority, not inherited. '
    'Return restricts true only for a restriction on rechecking or retrying; authority says where it comes '
    'from; quote is the exact restricting words copied from the text (empty when restricts is false).')
INHERITED_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['restricts', 'authority', 'quote'],
    'properties': {'restricts': {'type': 'boolean'},
                   'authority': {'type': 'string', 'enum': ['inherited', 'operator', 'none']},
                   'quote': {'type': 'string'}}}


def inherited_judge(data):
    """AI judgement; none while testing unless a fixture verdict is supplied."""
    if os.environ.get('BRAIN_USAGE_TESTING') == '1':
        fixture = os.environ.get('BRAIN_JUDGE_FIXTURE')
        return json.loads(Path(fixture).read_text()) if fixture else None
    from .ai_json import ai_json
    value, _ = ai_json(INHERITED_SYSTEM, data, INHERITED_SCHEMA)
    return value


def relaunch_problems(brief_text, judge=None):
    """Refuse a relaunch brief that carries an inherited no-retry or stop clause.

    An unavailable or malformed judgement lets the brief through; the brief's own
    resume section still says inherited conclusions are context, not proof.
    """
    if not text(brief_text):
        return []
    try:
        value = (judge or inherited_judge)({'brief': brief_text})
    except Exception:
        return []
    if (not isinstance(value, dict) or type(value.get('restricts')) is not bool or
            value.get('authority') not in ('inherited', 'operator', 'none') or not isinstance(value.get('quote'), str)):
        return []
    quote = value['quote'].strip()
    if value['restricts'] and value['authority'] == 'inherited' and quote and quote in brief_text:
        return ['a relaunch brief is context, not proof: remove the inherited restriction "' + quote[:160] +
                '" (the new builder re-checks inherited conclusions against the code), or cite the operator decision '
                'or contract field it comes from']
    return []


RESUME_LINE = ("Inherited conclusions are context, not proof: before building on one your change depends on, "
               "recheck it against the current code. Restrictions an earlier agent concluded (no retry, attempt "
               "spent, cause unknown so stop) do not carry forward; only the operator's and this contract's limits bind.")


# Ready -------------------------------------------------------------------------

def validate_ready_design(inst, n, branch, head, packet=None):
    """A design-referenced UI is ready only with paired screenshots and an independent checklist."""
    try:
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        contract = contract_text(n, row)
        if design_screens(contract) is None:
            return []
        data = read_receipt(inst, n, row, 'design_check', head)
    except (OSError, ValueError, TypeError, KeyError) as error:
        return ['design check: ' + str(error)]
    if data is None:
        return ['UI with a design reference needs reference/build screenshots of every touched screen and a design '
                'checklist from a different runtime: `brain evidence ' + n['id'] + ' --branch ' + branch +
                ' --kind design-check --file <json>`']
    failed = sorted(k for k, v in data['checklist'].items() if v['verdict'] != 'pass')
    return (['design checklist failed: ' + ', '.join(failed) + '; fix before the operator sees it'] if failed else [])
