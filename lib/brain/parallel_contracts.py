"""Pure, shared validation of concurrency and forecast arithmetic."""
import re
from decimal import Decimal, InvalidOperation

EXAMPLE = 'concurrency: 8\nsum: 40 x 6 s / 8 = 30 s'
NUMBER = r'(?:[0-9]{1,12}(?:\.[0-9]{1,9})?)'
UNIT = r'(?:s|sec(?:ond)?s?|m|min(?:ute)?s?|h|hours?)'


def seconds(number, unit):
    return Decimal(number) * (3600 if unit.startswith('h') else 60 if unit.startswith('m') else 1)


def parallel_problems(text, defaults=''):
    """Require declarations for timed/paid work; check declared sums exactly.

    Labels between numbers are permitted (``6 cheap lanes x 60 s``).
    Rounding tolerance is half the last displayed decimal place of the total.
    """
    fields = {}
    for name, value in re.findall(r'^[ \t]*(?:[-*][ \t]+)?(concurrency|sum|serial_reason)[ \t]*:[ \t]*([^\n]*)', text, re.I | re.M):
        fields.setdefault(name.lower(), []).append(value.strip())
    fallback = {}
    for name, value in re.findall(r'^[ \t]*(?:[-*][ \t]+)?(concurrency|sum|serial_reason)[ \t]*:[ \t]*([^\n]*)', defaults, re.I | re.M):
        fallback.setdefault(name.lower(), []).append(value.strip())
    for name, values in fallback.items():
        fields.setdefault(name, values)
    timed = re.search(r'\b(?:ETA|deadline|time[ -]?(?:target|box|limit))\b|\btarget[ \t]*:|\b(?:by|before|at)\s+[0-9]{1,2}:[0-9]{2}\b', text, re.I)
    duration = re.search(r'(?<![\w.])' + NUMBER + r'\s*(?:' + UNIT + r'|days?|weeks?)\b|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|half|an?)\s+(?:seconds?|minutes?|hours?|days?|weeks?)\b', text, re.I)
    currency = r'(?:USD|EUR|GBP|CAD|AUD|JPY|dollars?|euros?|pounds?)'
    paid = re.search(r'\bpaid\b|\b' + currency + r'\s*' + NUMBER + r'|(?<![\w.])' + NUMBER
                     + r'\s*' + currency + r'\b|[$€£¥]\s*' + NUMBER, text, re.I)
    if not fields and not (timed or duration or paid):
        return []
    errors = []
    def error(message):
        errors.append(message + '; example:\n' + EXAMPLE)
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
                   + r')(?:\s+[A-Za-z][A-Za-z _.-]*)?')
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
