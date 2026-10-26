"""Short-lived optional ONNX worker. Not imported by the core CLI."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.request


def prepare_model(folder, download, staging):
    # Import only the stdlib parent module, including immutable artifact hashes.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from brain.semantic import MODEL_FILES, MODEL_ID, MODEL_REVISION
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / 'manifest.json'
    if folder.is_symlink() or manifest.is_symlink():
        raise ValueError('unsafe model path')
    try:
        saved = json.loads(manifest.read_text())
    except (OSError, ValueError):
        saved = {}
    complete = saved.get('model') == MODEL_ID and all(
        (folder / name).is_file() and not (folder / name).is_symlink()
        and (folder / name).stat().st_size == size for name, (_, size) in MODEL_FILES.items())
    if complete:
        return
    if not download or os.environ.get('BRAIN_USAGE_TESTING') == '1':
        raise ValueError('model unavailable offline')
    for name, (expected, size) in MODEL_FILES.items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.parent.is_symlink():
            raise ValueError('unsafe model path')
        descriptor, temporary = tempfile.mkstemp(prefix='.download-', dir=staging)
        try:
            digest, count = hashlib.sha256(), 0
            url = 'https://huggingface.co/Xenova/multilingual-e5-small/resolve/' + MODEL_REVISION + '/' + name
            with os.fdopen(descriptor, 'wb') as output, urllib.request.urlopen(url, timeout=8) as response:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > size:
                        raise ValueError('oversized model')
                    digest.update(chunk)
                    output.write(chunk)
            if count != size or digest.hexdigest() != expected:
                raise ValueError('model integrity mismatch')
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    manifest.write_text(json.dumps({'model': MODEL_ID}) + '\n')


def run(request):
    folder = Path(request['folder'])
    prepare_model(folder, request['download'], request['staging'])
    if request['setup']:
        return []
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer
    tokenizer = Tokenizer.from_file(str(folder / 'tokenizer.json'))
    tokenizer.enable_truncation(max_length=512)
    tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(folder / 'onnx/model_quantized.onnx'),
                                   sess_options=options, providers=['CPUExecutionProvider'])
    inputs = {x.name for x in session.get_inputs()}
    vectors = []
    for offset in range(0, len(request['texts']), 8):
        encoded = tokenizer.encode_batch(request['texts'][offset:offset + 8])
        feed = {'input_ids': np.array([e.ids for e in encoded], dtype=np.int64),
                'attention_mask': np.array([e.attention_mask for e in encoded], dtype=np.int64)}
        if 'token_type_ids' in inputs:
            feed['token_type_ids'] = np.array([e.type_ids for e in encoded], dtype=np.int64)
        output = session.run(None, feed)[0]
        mask = feed['attention_mask'][..., None]
        pooled = (output * mask).sum(axis=1) / mask.sum(axis=1)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        vectors.extend((pooled / norms).tolist())
    return vectors


if __name__ == '__main__':
    try:
        print(json.dumps({'vectors': run(json.load(sys.stdin))}))
    except Exception:
        sys.exit(1)
