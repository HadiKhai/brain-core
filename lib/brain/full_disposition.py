"""Red full gates stay red; narrowly qualified quarantines are separate landing evidence."""
import ast
import hashlib
import json
import re
import shlex
from pathlib import Path

from . import common as _m_common
from . import full_gate as _m_full
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import tasks as _m_tasks
from . import verify as _m_verify
from . import verify_lanes as _m_lanes


def native_inventory(path, result, expected_hash):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError('native inventory log changed')
    rows = [json.loads(line[len('test-result: '):]) for line in raw.decode().splitlines()
            if line.startswith('test-result: ')]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError('malformed native test inventory')
    sessions = [r for r in rows if r.get('event') == 'session']
    finishes = [r for r in rows if r.get('event') == 'session_finish']
    completed = [r for r in rows if r.get('event') == 'completed']
    by_test = {}
    for row in rows:
        if row.get('event') == 'phase':
            test = row.get('nodeid')
            if not isinstance(test, str):
                raise ValueError('native phase has no test identity')
            by_test.setdefault(test, []).append(row)
    if len(sessions) != 1 or len(finishes) != 1 or not completed:
        raise ValueError('native test inventory missing or ambiguous')
    if rows[0] is not sessions[0] or rows[-1] is not finishes[0]:
        raise ValueError('native inventory session boundaries changed')
    finish = finishes[0]
    if (finish.get('exitstatus') != result or type(finish.get('exitstatus')) is not int
            or finish.get('incomplete') != [] or type(finish.get('collected')) is not int
            or finish['collected'] != len(completed) or type(finish.get('failed')) is not int
            or finish['failed'] != sum(r.get('outcome') == 'failed' for r in completed)):
        raise ValueError('native test inventory incomplete or inconsistent')
    found = {}
    for row in completed:
        test = row.get('nodeid')
        if (not isinstance(test, str) or test in found or row.get('outcome') not in ('passed', 'failed', 'skipped')
                or not isinstance(row.get('source_path'), str)):
            raise ValueError('native test identity missing or duplicated')
        # Only ordinary unittest methods are supported, including compatibility collectors.
        parts = test.split('::')
        source = row['source_path']
        if (len(parts) != 3 or not re.fullmatch(r'tests/test_[A-Za-z0-9_]+\.py', parts[0])
                or not all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', x) for x in parts[1:])
                or not re.fullmatch(r'tests/test_[A-Za-z0-9_]+\.py', source)):
            raise ValueError('unsupported native test selector')
        phases = by_test.get(test, [])
        whens = [p.get('when') for p in phases]
        if (whens not in (['setup', 'call', 'teardown'], ['setup', 'teardown'])
                or any(p.get('outcome') not in ('passed', 'failed', 'skipped')
                       or p.get('source_path') != source for p in phases)):
            raise ValueError('native test phases missing or inconsistent')
        outcome = ('failed' if any(p['outcome'] == 'failed' for p in phases) else
                   'skipped' if any(p['outcome'] == 'skipped' for p in phases) else 'passed')
        if outcome != row['outcome'] or row['outcome'] == 'passed' and whens != ['setup', 'call', 'teardown']:
            raise ValueError('native completion contradicts its test phases')
        found[test] = {**row, 'source_test': '::'.join([source, *parts[1:]])}
    if set(by_test) != set(found):
        raise ValueError('native inventory has unfinished tests')
    return found


def isolated_command(command, test):
    """Retain the gate's pytest options, removing only parallelism and collection paths."""
    argv = shlex.split(command)
    try:
        index = argv.index('-m')
    except ValueError as error:
        raise ValueError('isolated receipts require a direct python -m pytest gate') from error
    if (argv[index:index + 2] != ['-m', 'pytest'] or index not in (1, 2)
            or index == 2 and argv[1] != '-B'):
        raise ValueError('isolated receipts require a direct python -m pytest gate')
    keep, args = argv[:index + 2], iter(argv[index + 2:])
    for arg in args:
        if arg in ('-n', '--numprocesses', '--max-worker-restart', '--durations'):
            next(args)  # malformed options fail closed
        elif any(arg.startswith(key + '=') for key in ('--numprocesses', '--max-worker-restart', '--durations')):
            continue
        elif arg in ('-q', '-v', '-s', '--serial-after-parallel'):
            keep.append(arg)
        elif arg == '-p':
            plugin = next(args)
            if plugin != 'no:cacheprovider':
                raise ValueError('unsupported pytest plugin override')
            keep.extend([arg, plugin])
        elif arg in ('--timeout', '--timeout-method'):
            keep.extend([arg, next(args)])
        elif arg.startswith(('--timeout=', '--timeout-method=')):
            keep.append(arg)
        elif re.fullmatch(r'tests(?:/test_[A-Za-z0-9_]+\.py(?:::[A-Za-z_][A-Za-z0-9_]*){0,2})?/?', arg):
            continue
        else:
            raise ValueError('unsupported pytest gate option for isolated receipt')
    return shlex.join([*keep, test])


def read_native(n, identity, scope, expected_hash=None):
    path = _m_full.full_receipt_path(n, identity)
    raw = path.read_bytes()
    if expected_hash is not None and hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError('native receipt changed')
    data = json.loads(raw)
    if (not isinstance(data, dict) or type(data.get('version')) is not int
            or data['version'] != 1 or data.get('scope') != scope):
        raise ValueError('wrong native receipt scope')
    return data


def red_receipt(inst, n, branch, identity, head, main, inputs, port_context, expected_hash):
    data = read_native(n, identity, 'full', expected_hash)
    _m_full.validate_full_receipt(inst, n, branch, data, head, main, inputs, port_context, None, result=1)
    if data['packet'].get('verify_scope') != 'full' or data['packet'].get('verify_lanes'):
        raise ValueError('disposition requires a direct complete full gate')
    log = data['packet']['verify_log']
    inventory = native_inventory(Path(log), 1, data['logs'][log])
    failed = {test: row for test, row in inventory.items() if row['outcome'] == 'failed'}
    if not failed:
        raise ValueError('red gate has no native test failures')
    return data, failed


def candidate_context(inst, n, branch):
    # Execution releases the writer lock; never compare only a stale in-memory
    # node after another command changes configuration or checkout bindings.
    n = _m_instance.Instance(inst.root).node(n['id'])
    reasons, head, main = _m_verify.ready_preflight(n, branch)
    if reasons:
        raise ValueError('; '.join(reasons))
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
    if not row:
        raise ValueError('native disposition requires an open job')
    w = n['local']['worktrees'][branch]
    env = _m_verify.verify_env(n, branch, w)
    env.update(BRAIN_ROOT=str(inst.root), BRAIN_VERIFY_HEAD=head,
               BRAIN_VERIFY_BASE=_m_verify.integration_parent(n, main), BRAIN_VERIFY_SCOPE='full')
    binding = _m_jobs.digest([n['fm'], w, str(n['home_path']), _m_lanes.lane_environment(env)])
    return (head, main, _m_verify.verify_inputs(n, w), _m_verify.verify_port_context(n, env),
            _m_jobs.job_key(row), binding, env)


def cmd_full_isolate(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    try:
        before = candidate_context(inst, n, a.branch)
        head, main, inputs, port, key, _, env = before
        red_hash = _m_full.log_digest(_m_full.full_receipt_path(n, a.full_receipt))
        red, failures = red_receipt(inst, n, a.branch, a.full_receipt, head, main, inputs, port, red_hash)
        if red['packet'].get('verify_lane_env') != _m_lanes.lane_environment(env):
            raise ValueError('isolated environment differs from the red gate')
        if a.test not in failures:
            raise ValueError('isolated selector is not a failure of this red gate')
        command = isolated_command(red['packet']['verify_command'], a.test)
        _m_instance.unlock()
        result, log = _m_verify.run_verify(inst, n, a.branch, command, n['local']['worktrees'][a.branch], env)
        if candidate_context(inst, n, a.branch)[:6] != before[:6]:
            raise ValueError('isolated run changed candidate or inputs')
        red_receipt(inst, n, a.branch, a.full_receipt, head, main, inputs, port, red_hash)
        if red_hash != _m_full.log_digest(_m_full.full_receipt_path(n, a.full_receipt)):
            raise ValueError('red receipt changed during isolated execution')
        data = dict(version=1, scope='isolated', job_key=key, head=head, node=n['id'], branch=a.branch,
                    full_receipt=a.full_receipt, full_receipt_sha256=red_hash, test=a.test,
                    source_test=failures[a.test]['source_test'], command=command, result=result,
                    inputs=inputs, verify_port_context=port, checked_at=_m_jobs.stamp(inst))
        identity = _m_full.publish_receipt(n, data, log=log)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        _m_common.die('full-isolate refused: ' + str(error))
    print('full-isolate: exit ' + str(result) + '; isolated receipt ' + identity)
    if result:
        raise _m_common.VerdictExit(result)


def skip_only(n, red_head, head, quarantines, failures):
    home = n['home_path']
    def git(*args):
        result = _m_common.git('--no-replace-objects', *args, cwd=home)
        if result.returncode:
            raise ValueError('quarantine Git evidence unavailable')
        return result.stdout
    # One immediate child commit, with no merges or intervening changes.
    if git('rev-list', '--parents', '-n', '1', head).split() != [head, red_head]:
        raise ValueError('quarantine must be a single child of the red head')
    files = {}
    for row in quarantines:
        source = failures[row['test']]['source_test']
        filename, cls, method = source.split('::')
        files.setdefault(filename, []).append((cls, method, row['reason']))
    changes = git('diff', '--raw', '--no-abbrev', '--no-renames', '-z', red_head, head).split('\0')
    paths = set()
    for i in range(0, len(changes) - 1, 2):
        meta, path = changes[i].split(), changes[i + 1]
        if (len(meta) != 5 or meta[0] not in (':100644', ':100755')
                or meta[0][1:] != meta[1] or meta[4] != 'M'):
            raise ValueError('quarantine permits only ordinary modified test files')
        paths.add(path)
    if paths != set(files):
        raise ValueError('quarantine changes more than the failed test source files')
    for filename, methods in files.items():
        before = ast.parse(git('show', red_head + ':' + filename))
        after = ast.parse(git('show', head + ':' + filename))
        for cls, method, reason in methods:
            owners = [item for item in after.body if isinstance(item, ast.ClassDef) and item.name == cls]
            functions = [item for owner in owners for item in owner.body
                         if isinstance(item, ast.FunctionDef) and item.name == method]
            if len(functions) != 1:
                raise ValueError('quarantined method identity is ambiguous')
            owner = owners[0]
            prefix = after.body[:after.body.index(owner)]
            imported = any(isinstance(item, ast.Import) and any(a.name == 'unittest' and a.asname is None
                           for a in item.names) for item in prefix)
            shadowed = any(
                isinstance(item, ast.Name) and item.id == 'unittest' and isinstance(item.ctx, (ast.Store, ast.Del))
                or isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == 'unittest'
                or isinstance(item, ast.arg) and item.arg == 'unittest'
                or isinstance(item, ast.ExceptHandler) and item.name == 'unittest'
                or isinstance(item, ast.Import) and any(a.asname == 'unittest' for a in item.names)
                or isinstance(item, ast.ImportFrom) and any((a.asname or a.name) in ('unittest', '*') for a in item.names)
                for item in ast.walk(after))
            if not imported or shadowed:
                raise ValueError('native skip requires an unshadowed unittest import before the class')
            function = functions[0]
            matching = [d for d in function.decorator_list if isinstance(d, ast.Call)
                        and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name)
                        and d.func.value.id == 'unittest' and d.func.attr == 'skip' and not d.keywords
                        and len(d.args) == 1 and isinstance(d.args[0], ast.Constant) and d.args[0].value == reason]
            if len(matching) != 1:
                raise ValueError('missing native unittest.skip with the recorded reason')
            function.decorator_list.remove(matching[0])
        # A plain unittest import may accompany the newly added decorators.
        had_import = any(isinstance(item, ast.Import) and any(a.name == 'unittest' and a.asname is None
                         for a in item.names) for item in before.body)
        if not had_import:
            imports = [item for item in after.body if isinstance(item, ast.Import) and len(item.names) == 1
                       and item.names[0].name == 'unittest' and item.names[0].asname is None]
            if len(imports) != 1:
                raise ValueError('quarantine needs one plain unittest import')
            after.body = [item for item in after.body if not (isinstance(item, ast.Import) and len(item.names) == 1
                          and item.names[0].name == 'unittest' and item.names[0].asname is None)]
        if ast.dump(before, include_attributes=False) != ast.dump(after, include_attributes=False):
            raise ValueError('quarantine changed assertions or other code')


def validate_disposition(inst, n, branch, data, head, main, inputs, port, require_tasks=True):
    if (type(data.get('version')) is not int or data['version'] != 1 or data.get('scope') != 'disposition'
            or data.get('head') != head or data.get('node') != n['id'] or data.get('branch') != branch):
        raise ValueError('disposition identifies another quarantine candidate')
    red_id = data['full_receipt']
    red_hash = data['full_receipt_sha256']
    red = read_native(n, red_id, 'full', red_hash)
    red_head = red['packet']['head']
    red, failures = red_receipt(inst, n, branch, red_id, red_head, main, inputs, port, red_hash)
    if data.get('full_receipt_sha256') != red_hash or data.get('job_key') != red['job_key']:
        raise ValueError('original red receipt changed')
    rows = data.get('quarantines')
    if (not isinstance(rows, list) or len(rows) != len(failures)
            or any(not isinstance(r, dict) or not isinstance(r.get('test'), str) for r in rows)
            or {r['test'] for r in rows} != set(failures)):
        raise ValueError('quarantine must cover exactly every red-gate failure')
    for row in rows:
        reason = row.get('reason')
        if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
            raise ValueError('quarantine requires a nonempty reason')
        isolated = read_native(n, row['isolated_receipt'], 'isolated', row['isolated_sha256'])
        path = _m_full.full_receipt_path(n, row['isolated_receipt'])
        if (row.get('isolated_sha256') != _m_full.log_digest(path) or isolated.get('full_receipt') != red_id
                or isolated.get('full_receipt_sha256') != red_hash or isolated.get('head') != red_head
                or isolated.get('job_key') != data['job_key'] or isolated.get('node') != n['id']
                or isolated.get('branch') != branch or isolated.get('test') != row['test']
                or isolated.get('source_test') != failures[row['test']]['source_test']
                or type(isolated.get('result')) is not int or isolated['result'] != 0
                or isolated.get('inputs') != inputs or isolated.get('verify_port_context') != port
                or isolated.get('command') != isolated_command(red['packet']['verify_command'], row['test'])
                or not inst.parse_time(isolated.get('checked_at'))
                or isolated.get('log_sha256') != _m_full.log_digest(isolated['log'])):
            raise ValueError('isolated receipt is failed, changed or bound to another head')
        inventory = native_inventory(Path(isolated['log']), 0, isolated['log_sha256'])
        if (set(inventory) != {row['test']} or inventory[row['test']]['outcome'] != 'passed'
                or inventory[row['test']]['source_test'] != isolated['source_test']):
            raise ValueError('failure did not pass alone')
        if require_tasks:
            task = _m_tasks.read_records(n, ident=row['task']).get(row['task'])
            if not task or task_source(data, row) not in task.get('provenance', []):
                raise ValueError('quarantine tracking task missing or mismatched')
    skip_only(n, red_head, head, rows, failures)
    return {**red['packet'], 'head': head, 'verify_reused': True, 'full_disposition': True}


def task_source(data, row):
    return 'full-quarantine:' + data['full_receipt_sha256'] + ':' + data['head'] + ':' + row['test']


def cmd_full_quarantine(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    try:
        before = candidate_context(inst, n, a.branch)
        head, main, inputs, port, key, _, _ = before
        request = json.loads(Path(a.file).read_text())
        red_id = request['full_receipt']
        data = dict(version=1, scope='disposition', node=n['id'], branch=a.branch, job_key=key,
                    head=head, full_receipt=red_id,
                    full_receipt_sha256=_m_full.log_digest(_m_full.full_receipt_path(n, red_id)),
                    quarantines=request['quarantines'], checked_at=_m_jobs.stamp(inst))
        for row in data['quarantines']:
            row['isolated_sha256'] = _m_full.log_digest(_m_full.full_receipt_path(n, row['isolated_receipt']))
        validate_disposition(inst, n, a.branch, data, head, main, inputs, port, require_tasks=False)
        with _m_instance.brain_lock(inst):
            if candidate_context(inst, n, a.branch)[:6] != before[:6]:
                raise ValueError('quarantine candidate changed before publication')
            for row in data['quarantines']:
                task = _m_tasks.upsert_source_task(inst, n, task_source(data, row),
                    'Repair quarantined test ' + row['test'], kind='test', description=row['reason'],
                    source_ref='full receipt ' + red_id + '; quarantine head ' + head)
                row['task'] = task['id']
            validate_disposition(inst, n, a.branch, data, head, main, inputs, port)
            if candidate_context(inst, n, a.branch)[:6] != before[:6]:
                raise ValueError('quarantine candidate changed during publication')
            identity = _m_full.publish_receipt(n, data)
            inst.commit([], n['id'] + ': quarantine disposition and tracking tasks')
    except (OSError, ValueError, KeyError, TypeError, StopIteration, SyntaxError) as error:
        _m_common.die('full-quarantine refused: ' + str(error))
    print('full-quarantine: red full exit 1 retained; disposition receipt ' + identity + '; no hand-back published')
