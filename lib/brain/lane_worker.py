"""Isolated native lane execution; the parent owns shared resource cleanup."""
import json
import sys
from pathlib import Path

from . import common, instance, verify


def run_lane_worker():
    payload = json.load(sys.stdin)
    inst = instance.Instance(Path(payload['root']))
    n = inst.node(payload['node'])
    lane = payload['lane']
    with verify.command_reservation(inst, n, payload['branch'], command=lane['command'],
                                    workers=lane.get('workers'), cleanup=False) as token:
        result, log = verify.run_verify_stream(inst, n, payload['branch'], lane['command'],
                                               payload['worktree'], payload['env'], token, lane=lane['name'])
    common.atomic_write(Path(sys.argv[1]), json.dumps([result, log]), private=True)


if __name__ == '__main__':
    run_lane_worker()
