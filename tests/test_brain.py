"""Compatibility entry for existing `pytest tests/test_brain.py` verify commands.

New focused lanes live in test_<module>.py. Unittest discovery visits those files
itself; pytest directory discovery skips this compatibility entry via conftest.
"""
import importlib
import unittest
from pathlib import Path


for _path in sorted(Path(__file__).parent.glob('test_*.py')):
    if _path.stem == 'test_brain':
        continue
    _module = importlib.import_module(_path.stem)
    for _name, _value in vars(_module).items():
        if isinstance(_value, type) and issubclass(_value, unittest.TestCase) and _value.__module__ == _module.__name__:
            globals()[_name] = _value


del _path, _module, _name, _value


def load_tests(loader, tests, pattern):
    # The lane files are already collected by unittest discovery.
    return unittest.TestSuite()
