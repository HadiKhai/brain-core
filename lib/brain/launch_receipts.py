"""Read-only checks of declared routing against returned host receipt metadata."""
import json
import sys
from pathlib import Path

from . import common as _m_common

_m_nodes = _m_common.LazyModule(__package__, 'nodes')


RECEIPT_MAX_BYTES = 256 * 1024


def receipt_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 200 and value.isprintable() and
            not any(c.isspace() for c in value) and value.lower() not in ('unknown', 'runtime default'))


def receipt_options(value):
    """Normalize explicit object/descriptor-array options; missing is never empty."""
    if isinstance(value, list):
        out = {}
        for item in value:
            if (not isinstance(item, dict) or not receipt_id(item.get('id')) or
                    'value' not in item or item['id'] in out):
                raise ValueError('unknown options')
            out[item['id']] = item['value']
    elif isinstance(value, dict):
        out = dict(value)
    else:
        raise ValueError('unknown options')
    if len(out) > 128 or any(not receipt_id(k) or type(v) not in (str, bool) for k, v in out.items()):
        raise ValueError('unknown options')
    return out


def check_launch_receipt(selection, receipt):
    """Compare metadata only. No host is called and execution is never attested."""
    result = {'status': 'unknown', 'fields': [], 'execution_verified': False}
    if isinstance(receipt, dict) and 'structuredContent' in receipt:
        if any(k in receipt for k in ('providerInstanceId', 'model', 'options')):
            return result
        receipt = receipt['structuredContent']
    if not isinstance(selection, dict) or not isinstance(receipt, dict):
        return result
    declared = selection.get('providerInstanceId', selection.get('instanceId'))
    if ('instanceId' in selection and 'providerInstanceId' in selection and
            selection['instanceId'] != selection['providerInstanceId']):
        return result
    actual = receipt.get('providerInstanceId')
    if not all(receipt_id(v) for v in (declared, actual, selection.get('model'), receipt.get('model'))):
        return result
    try:
        expected_options = receipt_options(selection.get('options'))
        actual_options = receipt_options(receipt.get('options'))
    except ValueError:
        return result
    different = [field for field, expected, returned in (
        ('providerInstanceId', declared, actual), ('model', selection['model'], receipt['model']),
        ('options', expected_options, actual_options)) if expected != returned]
    return {**result, 'status': 'mismatch' if different else 'match',
            'fields': different or ['providerInstanceId', 'model', 'options']}


def receipt_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate receipt key')
        out[key] = value
    return out


def read_receipt_file(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(RECEIPT_MAX_BYTES + 1)
    if len(raw) > RECEIPT_MAX_BYTES:
        raise ValueError('receipt exceeds byte bound')
    return json.loads(raw, object_pairs_hook=receipt_object)


def cmd_launch_check(a):
    inst = _m_nodes.load()
    inst.node(a.id)  # Resolve the caller's node; this check creates no state or host action.
    try:
        result = check_launch_receipt(read_receipt_file(a.selection_file), read_receipt_file(a.receipt_file))
    except (OSError, ValueError, UnicodeError):
        result = {'status': 'unknown', 'fields': [], 'execution_verified': False}
    print(json.dumps(result, sort_keys=True))
    if result['status'] != 'match':
        sys.exit(1)
