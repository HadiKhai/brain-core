"""Bounded readiness waits and explicitly released native child barriers."""
import os
import threading
from time import monotonic


def wait_until(predicate, timeout=5, interval=.01):
    """Return the first truthy observation, or False at the real deadline.

    Keep the real deadline independent of injected product clocks. Exceptions
    belong to the caller; a broken observation must never become a timeout.
    """
    deadline = monotonic() + timeout
    wake = threading.Event()
    while True:
        result = predicate()
        if result:
            return result
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        wake.wait(min(interval, remaining))


class FileGate:
    """A fixture-owned FIFO: release may precede the child's blocking read."""

    def __init__(self, path):
        self.path = path
        os.mkfifo(path)
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.released = False

    def release(self):
        if not self.released:
            os.write(self.fd, b'1')
            self.released = True

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            self.path.unlink(missing_ok=True)

    def code(self, timeout=30):
        """Standalone child code; no test imports or inherited descriptors."""
        return ("import os,select\n"
                f"_gate_fd = os.open({str(self.path)!r}, os.O_RDONLY | os.O_NONBLOCK)\n"
                "try:\n"
                f" assert select.select([_gate_fd], [], [], {timeout!r})[0], 'release barrier timed out'\n"
                " assert os.read(_gate_fd, 1) == b'1', 'release barrier closed'\n"
                "finally:\n os.close(_gate_fd)\n")
