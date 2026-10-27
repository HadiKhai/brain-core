"""Durable tell-before-settle receipts for explicitly requested worktree cleanup."""
import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

from . import common as _m_common
from . import host_rpc as _m_rpc


def cleanup_notice(inst, n, job_key, identity, workspace, rpc):
    """False means retain. An unknown send is never repeated automatically."""
    path = Path(workspace)
    if not path.is_dir() or path.is_symlink():
        return False  # missing checkout: never wake its stale thread
    info = path.stat()
    binding = dict(job_key=job_key, thread=identity, workspace=str(path),
                   device=info.st_dev, inode=info.st_ino)
    key = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    folder = n['dir'] / '.brain' / 'cleanup-notices'
    _m_common.safe_target(folder)
    folder.mkdir(parents=True, exist_ok=True)
    record, lock = folder / (key + '.json'), folder / (key + '.lock')
    _m_common.safe_target(record)
    _m_common.safe_target(lock)
    if record.is_symlink() or lock.is_symlink() or folder.is_symlink():
        return False
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        if record.exists():
            data = json.loads(record.read_text())
            return (isinstance(data, dict) and data.get('version') == 1
                    and data.get('binding') == binding and data.get('state') == 'accepted')
        data = dict(version=1, binding=binding, state='sending', client_request_id='cleanup-' + key)
        _m_common.atomic_write(record, json.dumps(data, sort_keys=True) + '\n', private=True)
        try:
            reply = rpc('t3_thread_send', dict(threadId=identity, mode='auto',
                clientRequestId=data['client_request_id'],
                message='This job is closed and its checkout is awaiting cleanup. Return any final hand-back now; '
                        'do not begin follow-on work here. Cleanup will wait for terminal state and reconciled requests.'))
            if (not isinstance(reply, dict) or reply.get('threadId') != identity
                    or reply.get('delivery') not in ('started', 'queued', 'steered', 'restarted')
                    or not isinstance(reply.get('messageId'), str) or not reply['messageId']
                    or not isinstance(reply.get('runId'), str) or not reply['runId']):
                return False
            data.update(state='accepted', message_id=reply['messageId'], run_id=reply['runId'])
            _m_common.atomic_write(record, json.dumps(data, sort_keys=True) + '\n', private=True)
        except (_m_rpc.HostRPCError, OSError, ValueError):
            return False
        return False  # a send may start a turn; require a separate fresh observation
    finally:
        os.close(fd)
