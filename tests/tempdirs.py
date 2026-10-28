"""One owned run root, shared by pytest workers and fixture subprocesses."""
import atexit
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile

_root = None
_owned = False
_previous = None


def configure():
    global _root, _owned, _previous
    if _root is not None:
        return _root
    _previous = (tempfile.tempdir, os.environ.get('TMPDIR'),
                 os.environ.get('BRAIN_TEST_RUN_ROOT'))
    inherited = os.environ.get('BRAIN_TEST_RUN_ROOT')
    if inherited and Path(inherited).is_dir():
        _root = Path(inherited)
    else:
        # Ordinary fixtures must never be nested inside an installed live Brain.
        _root = Path(tempfile.mkdtemp(prefix='brain-test-run-')).resolve()
        _owned = True
    temporary = _root / 'tmp'
    temporary.mkdir(mode=0o700, exist_ok=True)
    os.environ['BRAIN_TEST_RUN_ROOT'] = str(_root)
    os.environ['TMPDIR'] = str(temporary)
    tempfile.tempdir = str(temporary)
    return _root


def fixture_parent(persistent=None):
    root = configure()
    if persistent is None:
        return root / 'tmp'
    # Only the existing explicit rehearsal fixtures need non-OS-aged storage.
    # Publish its ownership in the shared root so only the controller retires it.
    with (root / 'persistent.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = root / 'persistent.json'
        parents = json.loads(manifest.read_text()) if manifest.exists() else {}
        key = str(Path(persistent).resolve())
        if key not in parents:
            directory = Path(tempfile.mkdtemp(prefix='brain-test-persistent-', dir=persistent))
            try:
                parents[key] = str(directory.resolve())
                draft = root / 'persistent.next'
                draft.write_text(json.dumps(parents))
                draft.replace(manifest)
            except BaseException:
                shutil.rmtree(directory)
                raise
        return Path(parents[key])


def retain(path):
    # An unsaved native diagnostic must survive even the run-level fallback.
    retained = configure() / 'retained'
    retained.mkdir(mode=0o700, exist_ok=True)
    (retained / path.name).write_text(str(path))


def _remove(directory):
    def retry(function, path, error):
        if isinstance(error[1], FileNotFoundError):
            return
        if isinstance(error[1], PermissionError):
            try:
                os.chmod(Path(path).parent, 0o700)
                if not Path(path).is_symlink():
                    os.chmod(path, 0o700)
                if Path(path).is_dir() and not Path(path).is_symlink():
                    shutil.rmtree(path, onerror=retry)
                else:
                    function(path)
            except OSError:
                pass  # A second traversal retries directories made readable.
    for _ in range(2):
        try:
            shutil.rmtree(directory, onerror=retry)
        except OSError:
            pass
        if not directory.exists():
            return True
    return False


def cleanup():
    global _root, _owned, _previous
    if _root is None:
        return
    root, owned = _root, _owned
    tempfile.tempdir, old_tmpdir, old_root = _previous
    for key, value in (('TMPDIR', old_tmpdir), ('BRAIN_TEST_RUN_ROOT', old_root)):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    if owned and root.exists():
        try:
            retained = [Path(marker.read_text()) for marker in (root / 'retained').glob('*')]
            manifest = root / 'persistent.json'
            persistent = json.loads(manifest.read_text()) if manifest.exists() else {}
            for directory in [*(Path(p) for p in persistent.values()), root]:
                if any(p.exists() and (p == directory or directory in p.parents) for p in retained):
                    continue
                if not _remove(directory):
                    return  # Keep ownership for the atexit retry; never fail pytest teardown.
        except (OSError, ValueError):
            return  # Unknown retention or ownership must preserve bytes for retry.
    _root, _owned, _previous = None, False, None


atexit.register(cleanup)
