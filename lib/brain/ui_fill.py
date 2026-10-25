"""Bounded fill scheduling shared by the dashboard bootstrap and refresh."""
import time


def advance_warm(spend, seconds=1.0):
    """Drain existing usage caches in short slices, preserving incomplete truth."""
    deadline = time.monotonic() + seconds
    while True:
        changed = spend.advance()
        if spend.value.get('complete') is not False or time.monotonic() >= deadline:
            return changed


def complete(state):
    """An absent aggregate is unavailable; an explicit partial is still filling."""
    return not isinstance(state.get('spend'), dict) or state['spend'].get('complete') is not False
