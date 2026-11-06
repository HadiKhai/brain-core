"""Coordinator-only settings in a host-created private worktree, before first input."""
import json
import os
from pathlib import Path
import re

from . import common as _m_common
from . import instance as _m_instance
_m_host = _m_common.LazyModule(__package__, 'host_lifecycle')
_m_launch_policy = _m_common.LazyModule(__package__, 'launch_policy')


def profile_home():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))).expanduser()


def install_profile(inst, node_id, workspace, home=None, connectors=True, skills=True):
    """No provider/user/project defaults change; a foreign or main checkout refuses."""
    inst.node(node_id)
    workspace = Path(workspace).expanduser()
    if workspace.is_symlink() or workspace.resolve() == inst.root.resolve():
        raise ValueError('coordinator profile requires its own native worktree')
    def git_path(root, *args):
        result = _m_common.git('rev-parse', *args, cwd=root)
        if result.returncode:
            raise ValueError('coordinator workspace is not a registered worktree')
        return (root / result.stdout.strip()).resolve()
    if (git_path(workspace, '--show-toplevel') != workspace.resolve() or
            git_path(workspace, '--git-common-dir') != git_path(inst.root, '--git-common-dir')):
        raise ValueError('coordinator profile refuses a foreign checkout')
    directory = workspace / '.claude'
    if directory.is_symlink():
        raise ValueError('coordinator settings directory must be private')
    directory.mkdir(exist_ok=True)
    path = directory / 'settings.local.json'
    marker = {'root': str(inst.root.resolve()), 'node': node_id, 'workspace': str(workspace.resolve())}
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.stat().st_nlink != 1:
            raise ValueError('linked local settings are preserved')
        _m_common.safe_target(path)
        previous = json.loads(path.read_text())
        if previous.get('_brainCoordinator') != marker:
            raise ValueError('existing local settings belong to another session; preserved')
    data = {'_brainCoordinator': marker, 'env': {'BRAIN_ROOT': marker['root']}}
    # Keep instructions/runtime only: a coordinator must not silently edit a
    # committed stale copy of canonical inbox/status/handover files.
    patterns = ['/AGENTS.md', '/CLAUDE.md', '/.claude/', '/core/']
    node = inst.node(node_id)
    while node.get('parent'):
        relative = node['dir'].relative_to(inst.root).as_posix()
        patterns += ['/' + relative + '/AGENTS.md', '/' + relative + '/CLAUDE.md']
        node = node['parent']
    sparse = _m_common.git('sparse-checkout', 'set', '--no-cone', *patterns, cwd=workspace)
    if sparse.returncode:
        raise ValueError('coordinator sparse workspace preparation failed; first input withheld')
    if connectors:
        data['disableClaudeAiConnectors'] = True
        data['env']['ENABLE_CLAUDEAI_MCP_SERVERS'] = 'false'
    config = Path(home) / '.claude' if home is not None else profile_home()
    if skills:
        plugins = config / 'plugins/installed_plugins.json'
        installed = json.loads(plugins.read_text()).get('plugins', {}) if plugins.exists() else {}
        data['enabledPlugins'] = {key: False for key in installed}
        # Disable user skills by their declared structural names. Core's local
        # skills and the host-injected t3-code MCP server remain available.
        brain_names = {path.name for path in (_m_common.CORE / 'skills').iterdir() if path.is_dir()}
        disabled = {}
        for root, dirs, files in os.walk(str(config / 'skills'), followlinks=True):
            if len(Path(root).parts) - len(config.parts) > 8:
                dirs[:] = []
                continue
            if 'SKILL.md' not in files:
                continue
            body = (Path(root) / 'SKILL.md').read_text()
            match = re.search(r'^name:[ \t]*[\"\']?([^\n\"\']+)', body, re.M)
            name = match[1].strip() if match else Path(root).name
            for key in {name, Path(root).name}:
                if key not in brain_names:
                    disabled[key] = 'off'
        data['skillOverrides'] = disabled
    _m_common.atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + '\n', private=True)
    return {'settings': str(path), 'workspace': str(workspace.resolve()),
            'kept': ['Brain skills', 't3-code'], 'connectors_off': connectors, 'skills_trimmed': skills}


def launch_scoped(inst, n, rpc, ticket, message, receipt=None, publish=None):
    """Launch idle, install per-launch settings, then deliver idempotent first input."""
    receipt = receipt if receipt is not None else {}
    if not receipt.get('new'):
        selection = receipt.get('modelSelection') or _m_launch_policy.coordinator_selection(inst, n)
        _m_launch_policy.require_selection(inst, n, 'coordinator', selection)
        result = _m_host.host_object(rpc, 't3_thread_launch', {'title': n['id'] + ' coordinator',
            'clientRequestId': 'coordinator-launch-' + ticket,
            'projectId': n['local'].get('host_project') or n['local'].get('home_project'),
            'workspaceStrategy': {'type': 'worktree', 'baseRef': 'HEAD', 'startFromOrigin': False},
            'modelSelection': selection})
        if not isinstance(result.get('threadId'), str) or not result['threadId']:
            raise ValueError('coordinator launch outcome unknown; retain intent')
        receipt['new'] = result['threadId']
        if publish:
            publish(receipt)
    observed = _m_host.host_object(rpc, 't3_thread_read',
        {'threadId': receipt['new'], 'limit': 1, 'maxCharsPerItem': 100}).get('thread')
    if not isinstance(observed, dict) or observed.get('threadId') != receipt['new']:
        raise ValueError('unknown native coordinator thread observation; intent retained')
    if not isinstance(observed.get('worktreePath'), str):
        raise ValueError('native coordinator workspace preparation pending; idle thread retained')
    if observed.get('activeRunId') or observed.get('runCount', 0):
        workspace = Path(observed['worktreePath'])
        settings = workspace / '.claude/settings.local.json'
        marker = {'root': str(inst.root.resolve()), 'node': n['id'], 'workspace': str(workspace.resolve())}
        if (not receipt.get('profile_ready') or str(workspace.resolve()) != receipt.get('workspace')
                or settings.is_symlink() or not settings.is_file()
                or json.loads(settings.read_text()).get('_brainCoordinator') != marker):
            raise ValueError('coordinator first input is running; profile readiness unknown, retain for reconciliation')
        profile = {'workspace': str(workspace.resolve()), 'settings': str(settings)}
    else:
        profile = install_profile(inst, n['id'], observed['worktreePath'])
    receipt['workspace'] = profile['workspace']
    receipt['profile_ready'] = True
    if publish:
        publish(receipt)
    state = ('\n\nPrivate launch workspace: settings only. The canonical Brain state root is ' +
             str(inst.root) + '; node folder: ' + str(n['dir']) +
             '. Use absolute paths there for state reads/edits; BRAIN_ROOT routes commands there. '
             'The private checkout is not a second writable instance.\n')
    _m_host.host_object(rpc, 't3_thread_send', {'threadId': receipt['new'], 'mode': 'queue',
        'clientRequestId': 'coordinator-start-' + ticket, 'message': message + state})
    return {'thread': receipt['new'], 'workspace': profile['workspace'], 'settings': profile['settings']}


def cmd_coordinator_profile(a):
    from . import nodes
    inst = nodes.load()
    print(json.dumps(install_profile(inst, a.id, a.workspace), indent=2))
