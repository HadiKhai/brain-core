"""Small, deterministic evidence gate for worker results and typed questions."""
import hashlib
import math
import os
import re
import stat
from pathlib import Path

MAX_REPORT = 512 * 1024
FAILURE = re.compile(r'\b(?:blocked|failed|parked|refused|two[ -]strikes|below[ -]target|missed (?:its |the )?target)\b', re.I)
NEGATED = re.compile(r'\b(?:no|not|never|zero|0)\s+(?:blocked|failed|parked|refused|two[ -]strikes|below[ -]target)\b', re.I)


def measurement(result=None, target=None, direction='at-least'):
    if result is None and target is None:
        return None
    if (type(result) not in (int, float) or type(target) not in (int, float)
            or not math.isfinite(result) or not math.isfinite(target)
            or direction not in ('at-least', 'at-most')):
        raise ValueError('measurement needs finite result and target, and direction at-least or at-most')
    return {'result': result, 'target': target, 'direction': direction}


def required(state, summary, measured=None, restart=None):
    state = re.sub(r'[\s_-]+', '-', state.strip().casefold()) if isinstance(state, str) else None
    rotation = state == 'blocked' and restart == 'compactions'
    return (state in ('blocked', 'failed', 'parked', 'refused', 'two-strikes') and not rotation
            or bool(FAILURE.search(NEGATED.sub('', summary or '')))
            or bool(measured and (measured['result'] < measured['target'] if measured['direction'] == 'at-least'
                                 else measured['result'] > measured['target'])))


def parse(text):
    """Read one report once. The labelled format deliberately needs no YAML/LLM."""
    if not isinstance(text, str):
        raise ValueError('dissection required; missing: Markdown ## Dissection text')
    section, group, fence = False, None, None
    cause, covered, recommendation = [], [], []
    cases, fixes = [], []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            marker = stripped[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        if line.startswith('## '):
            if section:
                break
            section = line.strip() == '## Dissection'
            continue
        if not section:
            continue
        if line.startswith('### '):
            group = line.strip()[4:].lower()
            continue
        label, separator, value = line.partition(':')
        if separator and label in ('Cause', 'Items covered', 'Recommendation'):
            {'Cause': cause, 'Items covered': covered, 'Recommendation': recommendation}[label].append(value.strip())
            continue
        item = re.fullmatch(r'(\d+)\.\s+(.+)', line)
        if item and group in ('cases', 'fixes'):
            (cases if group == 'cases' else fixes).append((int(item[1]), item[2]))
    missing = []
    if not section:
        missing.append('## Dissection section')
    if len(cause) != 1 or not cause[0]:
        missing.append('one nonempty Cause: line')
    if len(covered) != 1 or not re.fullmatch(r'\d+', covered[0]):
        missing.append('Items covered: nonnegative integer')
    if [n for n, _ in cases] != [1, 2, 3]:
        missing.append('### Cases with exactly 3 numbered cases (1-3)')
    for rank, case in cases:
        ref = re.match(r'Ref:\s*([^\s;,]+)', case)
        token = ref[1].strip('`\'"') if ref else ''
        if (not any(c.isalnum() for c in token) or
                token.casefold() in ('none', 'n/a', 'na', 'tbd', 'todo', 'unknown')):
            missing.append('case ' + str(rank) + ' stored-record Ref: (path, id or URL)')
    if [n for n, _ in fixes] not in ([1, 2], [1, 2, 3]):
        missing.append('### Fixes with 2-3 ranked fixes (1-2 or 1-3)')
    if (len(recommendation) != 1 or recommendation[0] not in [str(n) for n, _ in fixes]):
        missing.append('Recommendation: ranked fix number')
    if missing:
        raise ValueError('dissection required; missing: ' + '; '.join(missing))
    return {'cause': cause[0], 'items_covered': int(covered[0]),
            'cases': [v for _, v in cases], 'fixes': [v for _, v in fixes],
            'recommendation': int(recommendation[0])}


def read_report(path):
    if not path:
        raise ValueError('dissection required; missing: --report or --dissection with ## Dissection')
    try:
        source = Path(path).expanduser().resolve()
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('dissection report must be a regular file')
            raw = stream.read(MAX_REPORT + 1)
    except (OSError, RuntimeError) as error:
        raise ValueError('dissection required; missing: readable --report or --dissection file') from error
    if len(raw) > MAX_REPORT:
        raise ValueError('dissection report exceeds 512 KiB')
    try:
        text = raw.decode('utf-8')
    except UnicodeError as error:
        raise ValueError('dissection report must be UTF-8 text') from error
    evidence = parse(text)
    return {'source': str(source), 'sha256': hashlib.sha256(raw).hexdigest(), **evidence}


def handback(a):
    measured = measurement(getattr(a, 'result', None), getattr(a, 'target', None),
                           getattr(a, 'target_direction', None) or 'at-least')
    source = getattr(a, 'dissection', None) or getattr(a, 'report', None)
    signature = [a.state, a.summary, getattr(a, 'restart', None), measured, source]
    saved = getattr(a, '_dissection_snapshot', None)
    if isinstance(saved, dict) and saved.get('signature') == signature:
        return saved
    evidence = (read_report(source) if getattr(a, 'dissection', None)
                or required(a.state, a.summary, measured, getattr(a, 'restart', None)) else None)
    a._dissection_snapshot = {'signature': signature, 'measurement': measured, 'evidence': evidence}
    return a._dissection_snapshot
