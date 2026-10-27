"""Normalize phase-one write-queue evidence; update field bindings here only."""
import hashlib
import json

# c50 bef8ea46: v2 .request/.receipt files and index byte-offset sequences.
SCHEMA = dict(version=2, identity='id', digest='hash', payload='payload',
              sequence='sequence', state='state', reason='reason',
              attempts='lock_attempts', acquisitions='lock_acquisitions',
              lock_failure='lock_wait_failure', request_id='request_id')


def payload_hash(payload):
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n'
    return hashlib.sha256(raw.encode()).hexdigest()


def family(data):
    command, action = data.get('command'), data.get('action')
    if command == 'task set' or (command == 'task' and action == 'set'):
        return 'task set'
    if command == 'handback enqueue' or (command == 'handback' and data.get('enqueue') is True):
        return 'handback enqueue'
    if command == 'inbox add' or (command == 'inbox' and action == 'add'):
        return 'inbox add'
    if command == 'status --refresh' or (command == 'status' and data.get('refresh') is True):
        return 'status --refresh'
    return None
