"""Optional local embeddings. Core imports remain standard-library-only."""
import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import common as _m_common
from . import semantic_runtime as _m_semantic_runtime

MODEL_REVISION = '761b726dd34fb83930e26aab4e9ac3899aa1fa78'
MODEL_ID = 'multilingual-e5-small-int8-' + MODEL_REVISION
MODEL_FILES = {
    'onnx/model_quantized.onnx': ('f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193', 118308185),
    'tokenizer.json': ('0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39', 17082730),
}


def semantic_capability(inst):
    """Cheap install/upgrade/check preflight: never imports a model or downloads."""
    if os.environ.get('BRAIN_SEMANTIC_OFFLINE') == '1' or os.environ.get('BRAIN_USAGE_TESTING') == '1':
        return False, 'offline or test mode'
    if inst.local.get('semantic', {}).get('enabled', True) is False:
        return False, 'disabled on this machine'
    try:
        if any(importlib.util.find_spec(name) is None for name in ('onnxruntime', 'tokenizers', 'numpy')):
            return False, 'optional onnxruntime/tokenizers/numpy unavailable'
        from . import machine as _m_machine
        reading = _m_machine.machine_reading(inst)
        if reading.get('mem_free_gb') is None or reading['mem_free_gb'] < 2:
            return False, 'less than 2 GB available RAM or unknown capacity'
    except (ImportError, AttributeError, OSError, ValueError, TypeError):
        return False, 'runtime capacity unavailable'
    return True, 'local ONNX available'


def semantic_mode(inst):
    capable, why = semantic_capability(inst)
    return 'recall mode: ' + ('local semantic (lexical fallback)' if capable else 'lexical (' + why + ')')


def model_folder(inst):
    return inst.root / '.brain' / 'semantic' / 'model'


def local_embed(inst, texts, setup=False):
    capable, why = semantic_capability(inst)
    if not capable:
        raise ValueError(why)
    folder = model_folder(inst)
    _m_common.safe_target(folder / 'manifest.json')
    request = {'folder': str(folder), 'texts': texts, 'setup': setup,
               'download': os.environ.get('BRAIN_SEMANTIC_NO_DOWNLOAD') != '1'}
    # No worker/daemon survives this call. A cold first use gets a small budget;
    # explicit setup may take longer. Partial downloads are owned and removed.
    folder.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.setup-', dir=folder.parent) as staging:
        request['staging'] = staging
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('semantic_runtime.py'))],
                                input=json.dumps(request), text=True, capture_output=True,
                                timeout=180 if setup else 12)
    if result.returncode:
        raise ValueError('local embedding unavailable')
    return json.loads(result.stdout)['vectors']


def normalized(vector):
    if (not isinstance(vector, list) or not vector or len(vector) > 2048
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)):
        raise ValueError('invalid embedding')
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise ValueError('invalid embedding')
    return [v / norm for v in vector]


def semantic_documents(inst, node):
    """Only this node's notes and its own report indexes, never another owner set."""
    from . import guard as _m_guard
    from . import worktrees as _m_worktrees
    roots = [node['dir']]
    try:
        roots.append(_m_worktrees.workbench(inst, node))
    except (OSError, ValueError, SystemExit):
        pass
    paths = [node['dir'] / name for name in ('knowledge.md', 'decisions.md')]
    for root in roots:
        paths.extend(sorted((root / 'reports').glob('*/index.md')))
    seen = set()
    for path in paths:
        try:
            resolved = path.resolve()
            if resolved in seen or not path.is_file() or path.stat().st_size > 1024 * 1024:
                continue
            if not any(root.resolve() in resolved.parents for root in roots):
                continue
            seen.add(resolved)
            text = path.read_text()
            if path.name == 'decisions.md' and not any(line.startswith('## ') for line in text.splitlines() if not line.startswith('## YYYY')):
                continue
            if not text.strip() or _m_guard.find_terms(text, _m_guard.never_terms(node)):
                continue
            title = next((line.lstrip('# ').strip() for line in text.splitlines() if line.startswith('# ')), path.stem)
            reference = _m_common.tilde(path)
            yield {'path': path, 'fm': {'id': 'document-' + hashlib.sha256(reference.encode()).hexdigest()[:16],
                   'title': title, 'kind': 'document', 'status': 'active', 'scope': ', '.join(node['owners']),
                   'source_node': node['id'], 'source_ref': reference}}
        except (OSError, ValueError):
            continue


def semantic_ranked(inst, node, problem, embedder=None):
    """Return active and proposed results, or None on any optional-path failure.

    Scope/content filtering precedes embedding and cache access. Cache hits bind
    model, exact content and dimensions; deletion/revocation takes effect on read.
    """
    from . import knowledge as _m_knowledge
    try:
        if embedder is None:
            if not semantic_capability(inst)[0]:
                return None
            embedder = lambda texts: local_embed(inst, texts)
        items = list(_m_knowledge.knowledge_candidates(inst, node, states=('active', 'proposed')))
        items.extend(semantic_documents(inst, node))
        documents = []
        for item in items:
            fm, body = _m_common.read_fm(item['path'])
            text = ' '.join(str(fm.get(k, '')) for k in ('title', 'keywords')) + '\n' + body
            documents.append('passage: ' + text)
        key = hashlib.sha256(node['id'].encode()).hexdigest()
        path = inst.root / '.brain' / 'semantic' / (key + '.json')
        _m_common.safe_target(path)
        try:
            cache = json.loads(path.read_text())
        except (OSError, ValueError):
            cache = {}
        old = cache.get('items', {}) if isinstance(cache, dict) and cache.get('model') == MODEL_ID else {}
        if not isinstance(old, dict):
            old = {}
        hashes = [hashlib.sha256(text.encode()).hexdigest() for text in documents]
        missing = list(dict.fromkeys(h for h in hashes if h not in old))
        by_hash = dict(zip(hashes, documents))
        batch = ['query: ' + problem] + [by_hash[h] for h in missing]
        vectors = embedder(batch)
        if not isinstance(vectors, list) or len(vectors) != len(batch):
            return None
        query = normalized(vectors[0])
        fresh = {h: normalized(v) for h, v in zip(missing, vectors[1:])}
        current = {h: normalized(fresh[h] if h in fresh else old[h]) for h in hashes}
        if any(len(v) != len(query) for v in current.values()):
            return None
        found, waiting = [], []
        for item, digest in zip(items, hashes):
            score = sum(a * b for a, b in zip(query, current[digest]))
            if score <= 0.7:
                continue
            fm = item['fm']
            item_id = fm.get('id', '')
            row = (-score, item_id, ' '.join(fm.get('title', item_id).split()),
                   ['local semantic cosine: {:.4f}'.format(score)], item)
            (waiting if fm.get('status') == 'proposed' else found).append(row)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _m_common.atomic_write(path, json.dumps({'model': MODEL_ID, 'items': current}) + '\n')
        except (OSError, ValueError):
            pass  # retrieval still works on read-only/contended cache storage
        return sorted(found, key=lambda r: r[:2]), sorted(waiting, key=lambda r: r[:2])
    except (Exception, SystemExit):
        # An optional model, index, dependency or inference failure cannot break recall.
        return None
