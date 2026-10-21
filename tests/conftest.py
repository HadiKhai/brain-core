"""Avoid collecting the legacy aggregate entry alongside its individual lanes."""
from pathlib import Path


def pytest_ignore_collect(collection_path, config):
    if collection_path.name != 'test_brain.py':
        return None
    explicitly_selected = any(
        Path(arg.split('::', 1)[0]).name == 'test_brain.py'
        for arg in config.args)
    return not explicitly_selected
