"""Content-addressed display sections; source and action checks always run fresh."""
import hashlib
import json
from pathlib import Path

from . import usage_cache

# Bind derived strings to the running implementation, including rendering helpers.
_CODE = hashlib.sha256(b''.join(p.name.encode() + p.read_bytes()
                              for p in sorted(Path(__file__).parent.glob('*.py')))).hexdigest()


class Sections:
    def __init__(self, cache, node, context):
        self.cache, self.node, self.context = cache, node, context

    def render(self, name, inputs, build):
        # Preserve dictionary order: it can be observable in generated facts.
        fingerprint = hashlib.sha256(json.dumps([_CODE, self.context, inputs],
                                                ensure_ascii=False, default=str).encode()).hexdigest()
        key = 'status-facts:' + hashlib.sha256((self.node + ':' + name).encode()).hexdigest()
        old = self.cache.get(key)
        if (isinstance(old, dict) and old.get('fingerprint') == fingerprint
                and isinstance(old.get('lines'), list)
                and all(isinstance(line, str) for line in old['lines'])):
            return old['lines']
        lines = build()
        # Fixed slots per node, bounded payloads; no raw inputs enter private state.
        if len(json.dumps(lines).encode()) <= 1024 * 1024:
            self.cache.put(key, {'fingerprint': fingerprint, 'lines': lines})
        return lines
