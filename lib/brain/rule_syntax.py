"""Shared structural rule syntax; never infer the meaning of rule prose."""
import hashlib
import re
from pathlib import Path

NODE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*(?:/[A-Za-z0-9][A-Za-z0-9_-]*)*')


def markdown_structure(text):
    """Ignore examples/comments when locating Markdown rule declarations."""
    out, fence, comment = [], None, False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = re.fullmatch(r'<!-- rule:[A-Za-z0-9_-]+ -->', stripped.strip())
        if stripped.startswith(('```', '~~~')) and not comment:
            token = stripped[0]
            fence = token if fence is None else None if fence == token else fence
            out.append(''.join(c if c in '\r\n' else ' ' for c in line))
            continue
        if fence:
            out.append(''.join(c if c in '\r\n' else ' ' for c in line))
            continue
        visible, pos = list(line), 0
        while pos < len(line):
            if comment:
                end = line.find('-->', pos)
                stop = len(line) if end < 0 else end + 3
                for i in range(pos, stop):
                    if visible[i] not in '\r\n':
                        visible[i] = ' '
                comment, pos = end < 0, stop
            else:
                begin = line.find('<!--', pos)
                if begin < 0 or marker:
                    break
                comment, pos = True, begin
        out.append(''.join(visible))
    return ''.join(out)



def node_rule_id(body, explicit=None):
    """Keep the landed contracts identity: ignore annotations/comments and wrapping."""
    identity = re.split(r'[ \t]+(?:Gate|Applies):', markdown_structure(body), maxsplit=1)[0].strip()
    identity = ' '.join(identity.split())
    return explicit or 'N-' + hashlib.sha256(identity.encode()).hexdigest()[:16]


def gate_references(value):
    """Parse capture-compatible [owner:]path::test references or none (reason).

    An absent owner is represented by None; the registry resolves it from the
    declaring node or, for instance rules, bound homes that declare the test.
    Parsing itself never reads files or evaluates a rule's prose.
    """
    if not value:
        raise ValueError('missing or duplicate Gate: metadata')
    if re.fullmatch(r'none \(\S(?:[^\n]*\S)?\)', value):
        return []
    refs = []
    for entry in value.split('; '):
        location, sep, selector = entry.partition('::')
        owner, owner_sep, path = location.partition(':')
        if not owner_sep:
            owner, path = None, location
        relative = Path(path)
        if (not sep or (owner is not None and not NODE_ID.fullmatch(owner)) or not path
                or ':' in path or relative.is_absolute() or '..' in relative.parts
                or '\\' in path or '\x00' in entry or not selector
                or selector != selector.strip() or any(ord(char) < 32 for char in selector)):
            raise ValueError('malformed Gate: use [home-node:]path::test or none (reason)')
        refs.append((owner, path, selector))
    return refs
