"""Declared tool-action guards and exact, single-use local allowances.

This is a conservative textual command guard, not a shell parser or a sandbox.
Only an explicit match denies. Hook errors fail open, as stop-hook errors do.
"""
import fnmatch
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import instance as _m_instance
from . import nodes as _m_nodes


SHELL_TOOLS = ('Bash', 'bash', 'exec_command', 'functions.exec_command')


def forbidden_actions(n):
    rules = []
    while n:
        path = n['dir'] / 'AGENTS.md'
        if path.is_file():
            section = re.search(r'^## Forbidden actions[^\n]*\n(.*?)(?=^## |\Z)', path.read_text(), re.M | re.S)
            if section:
                for line in section.group(1).splitlines():
                    if line.startswith('- ') and line[2:].strip():
                        rules.append(line[2:].strip().strip('`'))
        if n.get('sealed'):
            break
        n = n.get('parent')
    return rules


def tool_action(data):
    tool, args = data.get('tool_name'), data.get('tool_input')
    if not isinstance(tool, str) or not isinstance(args, dict):
        raise ValueError('missing tool identity or arguments')
    cwd = data.get('cwd')
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError('missing absolute working folder')
    cwd = str(Path(cwd).resolve())
    if tool in SHELL_TOOLS:
        command = args.get('command', args.get('cmd'))
        if not isinstance(command, str) or not command.strip():
            raise ValueError('missing shell command')
        # Command runners can select a different working directory in their arguments.
        workdir = args.get('workdir', cwd)
        if not isinstance(workdir, str) or not Path(workdir).is_absolute():
            raise ValueError('invalid tool working folder')
        return {'tool': 'shell', 'command': command, 'cwd': str(Path(workdir).resolve())}
    return {'tool': tool, 'arguments': args, 'cwd': cwd}


def action_fingerprint(action):
    return hashlib.sha256(json.dumps(action, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def allowance_path(inst, n):
    return inst.root / '.brain/action-allowances' / (hashlib.sha256(n['id'].encode()).hexdigest() + '.json')


def allowance_state(inst, n):
    path = allowance_path(inst, n)
    if not path.exists():
        return {'version': 1, 'node': n['id'], 'allowances': {}, 'events': []}
    _m_common.safe_target(path)
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or data.get('version') != 1 or data.get('node') != n['id'] or
            not isinstance(data.get('allowances'), dict) or not isinstance(data.get('events'), list)):
        raise ValueError('invalid allowance state')
    return data


def save_allowances(inst, n, state):
    path = allowance_path(inst, n)
    _m_common.safe_target(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _m_common.atomic_write(path, json.dumps(state, sort_keys=True) + '\n')
    os.chmod(path, 0o600)


def matching_action(rules, action):
    for rule in rules:
        if rule.startswith('tool:'):
            if fnmatch.fnmatchcase(action['tool'], rule[5:].strip()):
                return True
        elif action['tool'] == 'shell':
            pattern = rule[6:].strip() if rule.startswith('shell:') else rule
            # Search the whole command, including compounds and wrappers. Quoted text may
            # conservatively match too; textual rules do not prove arbitrary shell semantics.
            if fnmatch.fnmatchcase(action['command'], '*' + pattern + '*'):
                return True
    return False


def pretool_decision(inst, n, data):
    action = tool_action(data)
    rules = forbidden_actions(n)
    if not matching_action(rules, action):
        return None
    digest = action_fingerprint(action)
    policy = action_fingerprint({'rules': rules})
    with _m_instance.brain_lock(inst):
        state = allowance_state(inst, n)
        allowed = state['allowances'].get(digest)
        if isinstance(allowed, dict) and allowed.get('policy') == policy:
            del state['allowances'][digest]
            state['events'].append({'event': 'consumed', 'action': digest, 'at': inst.now()})
            save_allowances(inst, n, state)
            return None
    return 'Forbidden action matched this node\'s declared tool rules; an exact --once allowance is required.'


def cmd_allow(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    cwd = Path(a.cwd).expanduser().resolve() if a.cwd else n.get('home_path')
    if not cwd:
        _m_common.die('allow requires --cwd for a node without a local home')
    anchors = [n['dir'], n.get('home_path'), *[w.get('path') for w in n['local'].get('worktrees', {}).values()]]
    if not any(Path(p).resolve() == cwd or Path(p).resolve() in cwd.parents for p in anchors if p):
        _m_common.die('allow working folder must belong to the selected node')
    action = tool_action({'cwd': str(cwd), 'tool_name': a.tool, 'tool_input': {'command': a.action}})
    if action['tool'] != 'shell':
        _m_common.die('allow currently supports declared shell commands only')
    rules = forbidden_actions(n)
    if not matching_action(rules, action):
        _m_common.die('the exact action does not match a forbidden rule; no allowance issued')
    digest = action_fingerprint(action)
    with _m_instance.brain_lock(inst):
        state = allowance_state(inst, n)
        if digest in state['allowances']:
            _m_common.die('an unconsumed allowance already exists for this exact action')
        state['allowances'][digest] = {'policy': action_fingerprint({'rules': rules}), 'issued_at': inst.now()}
        state['events'].append({'event': 'issued', 'action': digest, 'at': inst.now()})
        save_allowances(inst, n, state)
    print('one-shot allowance recorded for this exact node, working folder and command')


def cmd_hook_pretool(a):
    try:
        data = json.loads(sys.stdin.read() or '{}')
        if not isinstance(data, dict):
            return
        cwd = data.get('cwd')
        if not isinstance(cwd, str):
            return
        root = _m_nodes.root_for_path(Path(cwd))
        if not root:
            return
        inst = _m_instance.Instance(root)
        matches = []
        for n in inst.nodes():
            anchors = [n['dir'], n.get('home_path'), *[w.get('path') for w in n['local'].get('worktrees', {}).values()]]
            depths = [len(Path(p).resolve().parts) for p in anchors if p and
                      (Path(p).resolve() == Path(cwd).resolve() or Path(p).resolve() in Path(cwd).resolve().parents)]
            if depths:
                matches.append((max(depths), n))
        if not matches:
            return
        nearest = [n for depth, n in matches if depth == max(d for d, _ in matches)]
        if len(nearest) != 1:
            return
        reason = None
        sender = data.get('session_id')
        if isinstance(sender, str) and sender.strip():
            from . import messages as _m_messages
            guard = getattr(_m_messages, 'worker_message_guard', None)
            if guard:
                reason = guard(inst, sender, data.get('tool_name'), data.get('tool_input'))
        reason = reason or pretool_decision(inst, nearest[0], data)
        if reason:
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                  'permissionDecision': 'deny', 'permissionDecisionReason': _m_guard.redact(reason, _m_guard.never_terms(nearest[0]))}}))
    except (Exception, SystemExit):
        return  # Missing policy, malformed input or unavailable bookkeeping never breaks a runtime.
