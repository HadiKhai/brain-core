"""Machine-local environment overrides for a single registered worktree."""
import re

from . import common as _m_common
from . import nodes as _m_nodes


RESERVED_JOB_ENV = {'HOME', 'MAIN', 'WORKTREE', 'BRANCH', 'NODE', 'GIT_DIR', 'GIT_WORK_TREE',
                    'PYTHONHOME', 'PYTHONPATH'}


def checked_job_env(values):
    if not isinstance(values, dict):
        raise ValueError('job environment overrides must be an object')
    for key, value in values.items():
        if (not isinstance(key, str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', key)
                or key in RESERVED_JOB_ENV or key.startswith(('BRAIN_', 'T3_', 'GIT_CONFIG_'))
                or (value is not None and (not isinstance(value, str) or '\0' in value))):
            raise ValueError('invalid or reserved job environment override')
    return values


def apply_job_env(env, w):
    result = dict(env)
    for key, value in checked_job_env(w.get('env', {})).items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def cmd_job_env(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    w = n['local'].get('worktrees', {}).get(a.branch)
    if not w:
        _m_common.die('job environment requires a registered worktree')
    try:
        values = dict(checked_job_env(w.get('env', {})))
        for pair in a.pairs:
            if '=' not in pair:
                raise ValueError('job environment needs NAME=value pairs')
            key, value = pair.split('=', 1)
            values[key] = value
        for key in a.unset:
            values[key] = None
        checked_job_env(values)
    except ValueError as error:
        _m_common.die(str(error))
    w['env'] = values
    inst.save_local()
    print('job environment: saved ' + str(len(values)) + ' local overrides; values omitted')
