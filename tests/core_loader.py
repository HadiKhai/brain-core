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
from functools import lru_cache
from pathlib import Path


_serial = itertools.count()


@lru_cache(maxsize=256)
def _compiled_source(path, source):
    # Cache only immutable code, never a module or its initialized globals. Read
    # the source on every load: even same-size edits with restored mtimes count.
    # Bounding this cache also bounds retention of temporary installed cores.
    return compile(source, path, 'exec', dont_inherit=True)


class _FixtureSourceLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        return _compiled_source(self.path, self.get_data(self.path))


class _FixtureFinder:
    def __init__(self, package_name):
        self.prefix = package_name + '.'

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self.prefix):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = _FixtureSourceLoader(fullname, spec.loader.path)
        return spec


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
        while True:
            package_name = '_brain_fixture_{}'.format(next(_serial))
            if not any(key == package_name or key.startswith(package_name + '.')
                       for key in sys.modules):
                break
        # Expose ownership before imports so a caller can clean a failed load.
        self.package_name = package_name
        spec = importlib.util.spec_from_file_location(
            package_name, package_dir / '__init__.py',
            loader=_FixtureSourceLoader(package_name, str(package_dir / '__init__.py')),
            submodule_search_locations=[str(package_dir)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = package
        finder = _FixtureFinder(package_name)
        sys.meta_path.insert(0, finder)
        try:
            spec.loader.exec_module(package)
            owners = {}
            for path in sorted(package_dir.glob('*.py')):
                if path.stem == '__init__':
                    continue
                part = importlib.import_module('.' + path.stem, package_name)
                for name in vars(part):
                    if not name.startswith('__') and not name.startswith('_m_'):
                        owners[name] = part
            # The flat fixture API predates the task row hook with the same name.
            owners['job_started'] = importlib.import_module('.jobs', package_name)
            # The legacy flat API's structured digest belongs to jobs; the
            # transaction observer's later byte digest is a different contract.
            owners['digest'] = importlib.import_module('.jobs', package_name)
        finally:
            sys.meta_path.remove(finder)
        module._owners = owners
        module.__class__ = CoreModule
