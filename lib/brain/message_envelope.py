"""Byte-bounded nonoperator envelopes. Uncovered text is always normal input."""
import hashlib
import json
import datetime

AGENT_MESSAGE_PREFIX = 'Agent message: sent by another agent, not the operator.'
AUTOMATED_TURN_PREFIX = 'Automated turn: no operator present; cancellations are host events.'
PREFIXES = (AGENT_MESSAGE_PREFIX, AUTOMATED_TURN_PREFIX)
HEADER = b'Brain-Envelope: '


def read_envelope(raw, offset=0):
    """Return (prefix, body, end) only for a complete length/digest-bound frame."""
    prefix = next((p for p in PREFIXES if raw.startswith(p.encode() + b'\n\n', offset)), None)
    if prefix is None:
        return None
    start = offset + len(prefix.encode()) + 2
    if not raw.startswith(HEADER, start):
        return None
    boundary = raw.find(b'\n\n', start, start + 512)
    if boundary < 0:
        return None
    try:
        meta = json.loads(raw[start + len(HEADER):boundary])
        if (not isinstance(meta, dict) or set(meta) not in ({'bytes', 'sha256'}, {'bytes', 'sha256', 'sent_at'})
                or type(meta['bytes']) is not int or meta['bytes'] < 0
                or not isinstance(meta['sha256'], str)):
            return None
        if 'sent_at' in meta and not valid_stamp(meta['sent_at']):
            return None
        begin = boundary + 2
        end = begin + meta['bytes']
        if end > len(raw):
            return None
        body = raw[begin:end]
        if hashlib.sha256(body).hexdigest() != meta['sha256']:
            return None
        return prefix, body.decode('utf-8'), end
    except (ValueError, UnicodeError):
        return None


def unmarked_message(text, prefix):
    raw = text.encode('utf-8')
    frame = read_envelope(raw)
    return frame[1] if frame and frame[0] == prefix and frame[2] == len(raw) else None


def valid_stamp(value):
    """A full offset-aware ISO timestamp; this checks structure, not authorship."""
    try:
        return isinstance(value, str) and datetime.datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def marked_message(text, prefix, sent_at=None):
    """Keep exact body bytes. A supplied clock stamp refreshes a complete frame.

    Legacy frames remain readable; a marker alone is ordinary body data. The
    timestamp belongs to the envelope, never to text inferred from its body.
    """
    body = unmarked_message(text, prefix)
    if body is not None:
        if sent_at is None:
            return text
        text = body
    raw = text.encode('utf-8')
    meta = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    if sent_at is not None:
        if not valid_stamp(sent_at):
            raise ValueError('message stamp requires an offset-aware ISO timestamp')
        meta['sent_at'] = sent_at
    meta = json.dumps(meta, sort_keys=True)
    return prefix + '\n\n' + HEADER.decode() + meta + '\n\n' + text


def fully_marked(text):
    """Prove the whole SDK newline-joined batch, including every frame's body.

    Newlines between complete frames (or from a formatter's final print) carry
    no unmarked content. Any other remainder, including a legacy prefix, keeps
    the entire original turn available to the classifier.
    """
    return bool(frames(text))


def frames(text):
    """(prefix, body) of every frame when the whole input is fully marked, else []."""
    raw, offset, found = text.encode('utf-8'), 0, []
    while offset < len(raw):
        frame = read_envelope(raw, offset)
        if frame is None:
            return []
        found.append(frame[:2])
        offset = frame[2]
        while offset < len(raw) and raw[offset:offset + 1] == b'\n':
            offset += 1
    return found
