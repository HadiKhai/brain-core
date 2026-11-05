"""Isolated native lane execution; the parent owns shared resource cleanup."""
import json
import sys
import os
import stat
from pathlib import Path

from . import common, instance, verify


def run_lane_worker():
    payload = json.load(sys.stdin)
    inst = instance.Instance(Path(payload['root']))
    n = inst.node(payload['node'])
    lane = payload['lane']
    keep = payload['log_keep']
    if type(keep) is not int or keep < 1:
        raise ValueError('lane log retention must be positive')
    verify.VERIFY_LOG_KEEP = keep
    lease = payload['queue_fd']
    if lease is not None:
        info = os.fstat(lease)
        if type(lease) is not int or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('invalid inherited lane lease descriptor')
    with verify.command_reservation(inst, n, payload['branch'], command=lane['command'],
                                    workers=lane.get('workers'), cleanup=False) as token:
        result, log = verify.run_verify_stream(inst, n, payload['branch'], lane['command'],
                                               payload['worktree'], payload['env'], token, lane=lane['name'],
                                               inherited_fds=() if lease is None else (lease,))
    common.atomic_write(Path(sys.argv[1]), json.dumps([result, log]), private=True)


if __name__ == '__main__':
    run_lane_worker()
