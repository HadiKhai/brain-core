"""Boot-scoped host identity for local lifecycle authority, never a hostname.

Unlike a PID or a boot timestamp, a kernel boot UUID distinguishes hosts sharing
an instance. Unsupported or unreadable identity grants no lifecycle authority.
This does not establish ownership of remote resources or descendant shutdown.
"""
import os
from pathlib import Path
import re
import subprocess
import sys


def lifecycle_host_identity():
    try:
        if sys.platform.startswith('linux'):
            value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        elif sys.platform == 'darwin':
            result = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'],
                capture_output=True, text=True, timeout=2, env={**os.environ, 'LC_ALL': 'C'})
            if result.returncode:
                return None
            value = result.stdout.strip()
        else:
            return None
        if re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value):
            return sys.platform + ':' + value.lower()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None
