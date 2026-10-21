"""Import an isolated modular core for the existing scenario tests.

The old suite patched one loaded script. Forward those patches to the module that
owns each name, and load a fresh package per fixture so caches never leak between
tests. Historical single-file cores still use Python's ordinary source loader.
"""
import importlib
import importlib.machinery
import importlib.util
import itertools
import sys
import types
from pathlib import Path


_serial = itertools.count()


class CoreModule(types.ModuleType):
    def __getattr__(self, name):
        owner = self._owners.get(name)
        if owner is None:
            raise AttributeError(name)
        return getattr(owner, name)

    def __setattr__(self, name, value):
        owner = self.__dict__.get('_owners', {}).get(name)
        if owner is None:
            super().__setattr__(name, value)
        else:
            setattr(owner, name, value)

    def __delattr__(self, name):
        owner = self.__dict__.get('_owners', {}).get(name)
        if owner is None:
            super().__delattr__(name)
        else:
            delattr(owner, name)


class SourceFileLoader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        package_dir = Path(self.path).resolve().parent.parent / 'lib' / 'brain'
        if not package_dir.is_dir():
            return super().exec_module(module)
        package_name = '_brain_fixture_{}'.format(next(_serial))
        spec = importlib.util.spec_from_file_location(
            package_name, package_dir / '__init__.py',
            submodule_search_locations=[str(package_dir)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = package
        spec.loader.exec_module(package)
        owners = {}
        for path in sorted(package_dir.glob('*.py')):
            if path.stem == '__init__':
                continue
            part = importlib.import_module('.' + path.stem, package_name)
            for name in vars(part):
                if not name.startswith('__') and not name.startswith('_m_'):
                    owners[name] = part
        module._owners = owners
        module.__class__ = CoreModule
