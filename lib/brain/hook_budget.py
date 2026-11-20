"""Hook-aware lock budgets from adapter declarations and retained measurements."""
import json
import math
import os

from . import common


SETTINGS = {'pretool': 'BRAIN_PRETOOL_LOCK_WAIT_SECONDS',
            'refresh': 'BRAIN_REFRESH_LOCK_SECONDS'}


def derivation(kind):
    if kind not in SETTINGS:
        raise ValueError('unknown hook lock budget')
    folder = common.CORE / 'adapters/runtimes'
    try:
        measured = json.loads((folder / 'hook-budget.json').read_text())
        samples = measured['runtime_samples_s']
        if measured.get('version') != 1 or not isinstance(samples, list) or not samples:
            raise ValueError('invalid hook runtime measurements')
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in samples):
            raise ValueError('invalid measured hook runtime')
        timeouts = []
        for path in sorted(folder.glob('*-hooks.json')):
            hooks = json.loads(path.read_text())['hooks']
            groups = [hooks.get('PreToolUse', [])] if kind == 'pretool' else hooks.values()
            for blocks in groups:
                for block in blocks:
                    for hook in block['hooks']:
                        value = hook['timeout']
                        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                            raise ValueError('invalid declared hook timeout')
                        timeouts.append(value)
        if not timeouts:
            raise ValueError('no declared adapter hook timeout')
        timeout, headroom = min(timeouts), max(samples)
        if headroom >= timeout:
            raise ValueError('measured hook runtime leaves no lock budget')
        return {'adapter_timeout_s': timeout, 'measured_headroom_s': headroom,
                'default_s': timeout - headroom}
    except (OSError, KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError('hook lock budget unavailable: ' + str(error)) from error


def seconds(kind):
    derived = derivation(kind)
    raw = os.environ.get(SETTINGS[kind])
    value = float(raw) if raw is not None else derived['default_s']
    if not math.isfinite(value) or not 0 <= value <= derived['default_s']:
        raise ValueError(SETTINGS[kind] + ' must fit the adapter timeout minus measured headroom')
    return value
