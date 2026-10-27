"""Content-addressed display sections; source and action checks always run fresh."""
import hashlib
import json
from pathlib import Path

# Bind derived strings to the running implementation, including rendering helpers.
_CODE = hashlib.sha256(b''.join(p.name.encode() + p.read_bytes()
                              for p in sorted(Path(__file__).parent.glob('*.py')))).hexdigest()


class Sections:
    def __init__(self, cache, node, context):
        self.cache, self.node, self.context = cache, node, context

    def render(self, name, inputs, build):
        # Preserve dictionary order: it can be observable in generated facts.
        fingerprint = hashlib.sha256(json.dumps([_CODE, self.context, inputs],
                                                default=str).encode()).hexdigest()
        key = 'status-facts:' + hashlib.sha256((self.node + ':' + name).encode()).hexdigest()
        context = hashlib.sha256(json.dumps([_CODE, self.context], default=str).encode()).hexdigest()
        old = self.cache.get(key)
        entries = old.get('entries', []) if isinstance(old, dict) and old.get('context') == context else []
        entries = entries[:2] if isinstance(entries, list) else []
        for entry in entries:
            if (isinstance(entry, dict) and entry.get('fingerprint') == fingerprint
                    and isinstance(entry.get('lines'), list)
                    and all(isinstance(line, str) for line in entry['lines'])):
                return entry['lines']
        lines = build()
        # Two generations accommodate alternating merge baselines without
        # rewriting the database on every command. Inputs remain hash-only.
        if len(json.dumps(lines).encode()) <= 1024 * 1024:
            entry = {'fingerprint': fingerprint, 'lines': lines}
            self.cache.put(key, {'context': context, 'entries': [entry, *entries[:1]]})
        return lines
