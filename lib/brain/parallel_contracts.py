"""Pure, shared validation of concurrency and forecast arithmetic."""
import re
from decimal import Decimal, InvalidOperation

EXAMPLE = 'mode: timed\nconcurrency: 8\nsum: 40 x 6 s / 8 = 30 s'
NUMBER = r'(?:[0-9]{1,12}(?:\.[0-9]{1,9})?)'
UNIT = r'(?:s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)'


def seconds(number, unit):
    return Decimal(number) * (3600 if unit.startswith('h') else 60 if unit.startswith('m') else 1)


FIELD_LINE = re.compile(r'^[ \t]*(?:[-*][ \t]+)?(mode|concurrency|sum|serial_reason)[ \t]*:[ \t]*([^\n]*)', re.I | re.M)


def declared_fields(text):
    fields = {}
    for name, value in FIELD_LINE.findall(text):
        fields.setdefault(name.lower(), []).append(value.strip())
    return fields


def inline_contract(text):
    """Recognize declared contract syntax, never infer a mode from prose."""
    return bool(declared_fields(text) or re.search(
        r'^##[ \t]+(?:Class|Outcome|Authorized|Discretion|Stop when|Needs consent|Done means|Budget)[ \t]*$', text, re.I | re.M))


def parallel_problems(text, defaults=''):
    """Every new contract declares its mode; timed/paid plans need checked sums.

    Mode is authoritative; there is deliberately no prose/cost/time detector.
    A task may inherit declarations from its already validated bound contract.
    """
    fields, fallback = declared_fields(text), declared_fields(defaults)
    own_mode = fields.get('mode')
    for name, values in fallback.items():
        fields.setdefault(name, values)
    errors = []
    def error(message):
        errors.append(message + '; example:\n' + EXAMPLE)
    modes = fields.get('mode', [])
    if not modes:
        error('missing mode: (timed, paid or untimed)')
        return errors
    if len(modes) != 1:
        error('duplicate mode:')
        return errors
    mode = modes[0].casefold()
    if mode not in ('timed', 'paid', 'untimed'):
        error('unknown mode: must be timed, paid or untimed')
        return errors
    if own_mode and fallback.get('mode') and mode != fallback['mode'][0].casefold():
        error('mode: must match the bound contract mode')
        return errors
    if mode == 'untimed' and not any(name in fields for name in ('concurrency', 'sum', 'serial_reason')):
        return []
    for name in ('concurrency', 'sum'):
        if not fields.get(name):
            error('missing ' + name + ':')
        elif len(fields[name]) != 1:
            error('duplicate ' + name + ':')
    concurrency = fields.get('concurrency', [''])[0]
    if fields.get('concurrency') and not re.fullmatch(r'[1-9][0-9]{0,11}', concurrency):
        error('concurrency: must be an integer >= 1')
    if concurrency == '1' and (len(fields.get('serial_reason', [])) != 1 or not fields['serial_reason'][0]):
        error('concurrency: 1 needs serial_reason: (e.g. serial_reason: ordered writes share state)')
    sums = fields.get('sum', [])
    if len(sums) == 1:
        pattern = (r'(' + NUMBER + r')\s*(?:[A-Za-z][A-Za-z _-]*\s+)?[x×*]\s*(' + NUMBER + r')\s*('
                   + UNIT + r')\s*/\s*([1-9][0-9]{0,11})\s*=\s*(' + NUMBER + r')\s*(' + UNIT
                   + r')(?:[ \t]+(?:per (?:lane )?round|total|overall))?')
        match = re.fullmatch(pattern, sums[0], re.I) if len(sums[0]) <= 512 else None
        if not match:
            error('sum: must state calls x seconds / concurrency = total')
        else:
            calls, average, unit, divisor, total, total_unit = match.groups()
            try:
                actual = Decimal(calls) * seconds(average, unit.lower()) / Decimal(divisor)
                stated = seconds(total, total_unit.lower())
                tolerance = seconds(Decimal(10) ** Decimal(total).as_tuple().exponent / 2, total_unit.lower())
                if divisor != concurrency:
                    error('sum: divisor must match concurrency:')
                if abs(actual - stated) > tolerance:
                    error('sum: arithmetic does not match stated total within rounding')
            except (InvalidOperation, OverflowError):
                error('sum: numeric values are out of range')
    return errors
