#!/usr/bin/env python3
"""Repository-owned job lanes; print JSON without importing the core or running tests.

Ownership is explicit: a new test must be assigned before the coverage gate passes.
Inputs follow the transitive relative-import graph, not just test file names. This
intentionally overincludes the strongly connected core. The CLI dispatcher and
fixture loader import every module, but only dispatch/call selected functions:
train entry points in tests (including imported test helpers and embedded Python)
are therefore scanned separately. Import-time code/signatures are fingerprinted
in every command, so changing those forces the entire map to be reverified.

The compatibility entry reexports the canonical classes; catch-all runs an import
smoke test, not their methods a second time. Live agents/browser checks are outside
the configured unit verify command; check_lanes.py inventories them explicitly.
"""
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_LANES = (
    'briefs', 'check', 'cli', 'connect', 'guard', 'history', 'hosts', 'jobs',
    'knowledge', 'machine', 'messages', 'nodes', 'rehearse', 'rotation',
    'schedules', 'spend', 'status', 'sync', 'train', 'transitions', 'ui',
    'upgrade', 'usage', 'verify', 'verify_lanes', 'watch', 'worktrees',
)
LANE_TESTS = {name: ('test_' + name + '.py',) for name in MODULE_LANES}
LANE_TESTS.update({
    'train': ('test_train.py', 'test_train_review.py'),
    'jobs': ('test_jobs.py', 'test_train_ejection.py'),
    'verify': ('test_verify.py', 'test_verify_train_hooks.py'),
    'review': ('test_review_discipline.py',),
    'catch-all': ('test_repo_lanes.py',),
})
COMPATIBILITY = 'test_brain.py'
OUTSIDE_JOB_LANES = {
    'live.py': 'real runtime/model calls; separate live verification',
    'ui-checks.js': 'browser assertions; separate browser verification',
}
SUPPORT = ('scenario.py', 'core_loader.py', 'conftest.py', 'lanes.py', 'check_lanes.py')
SHARED_INPUTS = (
    'lib/brain/common.py', 'lib/brain/instance.py', 'lib/brain/cli.py',
    'lib/brain/__init__.py', 'tests/scenario.py', 'tests/core_loader.py',
    'tests/conftest.py', 'tests/lanes.py', 'tests/check_lanes.py',
    'bin/**', 'templates/**', 'skills/**', 'adapters/**', 'ui/**', '!ui/**/*.png',
    'install.sh', 'MODEL.md', 'CHANGES.md',
)


def parse(path):
    return ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


def imports(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            found.update([node.module.split('.')[0]] if node.module else [a.name for a in node.names])
        elif isinstance(node, ast.Import):
            found.update(a.name.split('.')[1] for a in node.names if a.name.startswith('brain.'))
        elif isinstance(node, ast.ImportFrom) and node.module == 'brain':
            found.update(a.name for a in node.names)
    return found


def closure(graph, seeds):
    found, pending = set(), list(seeds)
    while pending:
        name = pending.pop()
        if name not in found:
            found.add(name)
            pending.extend(graph.get(name, ()))
    return found


def test_sources(root, files):
    """Include reused test helpers, and code snippets run in child interpreters."""
    pending, seen = list(files) + ['scenario.py'], set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        tree = parse(root / 'tests' / name)
        yield tree
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            pending.extend(n + '.py' for n in names if n.startswith('test_'))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                try:
                    embedded = ast.parse(node.value)
                except (SyntaxError, ValueError):
                    continue
                yield embedded


def startup_fingerprint(trees):
    """Module imports, constants, class bodies and function definition-time code."""
    startup = []
    for name, tree in sorted(trees.items()):
        nodes = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nodes.append(ast.dump(ast.FunctionDef(
                    name=node.name, args=node.args, body=[], decorator_list=node.decorator_list,
                    returns=node.returns, type_comment=node.type_comment)))
            else:
                nodes.append(ast.dump(node))
        startup.append((name, nodes))
    return hashlib.sha256(json.dumps(startup).encode()).hexdigest()


def test_targets(root, files):
    # Pytest otherwise also collects TestCase classes imported as helper bases.
    # The compatibility entry includes only locally defined classes, so do likewise.
    targets = []
    for file in files:
        tree = parse(root / 'tests' / file)
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith('test_')
               for n in tree.body):
            raise ValueError('assign standalone pytest functions explicitly: ' + file)
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        if not classes:
            raise ValueError('no canonical test classes: ' + file)
        targets.extend('tests/' + file + '::' + name for name in classes)
    return targets


def lane_map(root=ROOT):
    trees = {p.stem: parse(p) for p in sorted((root / 'lib/brain').glob('*.py'))}
    if not trees:
        raise ValueError('no core modules found')
    graph = {name: imports(tree) for name, tree in trees.items()}
    # Keep all transitive calls, even callbacks resolved through module attributes
    # at call time. Do not treat CLI dispatch as an unconditional call to train.
    train_symbols = {n.name for n in trees['train'].body
                     if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    startup = startup_fingerprint(trees)
    lanes = []
    for name, files in sorted(LANE_TESTS.items()):
        seeds = {'nodes', 'common', 'instance'}  # Scenario.init/project and CLI startup
        if name == 'catch-all':
            seeds.update(trees)  # scripts inspect the whole graph and compatibility imports
        elif name != 'cli':
            seeds.add(name)
        helper_inputs = set()
        for tree in test_sources(root, files):
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and node.attr in train_symbols
                        and not (isinstance(node.value, ast.Name) and node.value.id == 'self')):
                    seeds.add('train')
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    # Direct CLI dispatch, module imports, and getattr-style exports.
                    if node.value in {'train', '.train', '_m_train', 'cmd_train'}:
                        seeds.add('train')
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == 'getattr' and len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value in train_symbols):
                    seeds.add('train')
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                             else [node.module or ''])
                    helper_inputs.update('tests/' + n + '.py' for n in names if n.startswith('test_'))
        dependencies = closure(graph, seeds)
        # The entry imports all modules. Their definition-time behavior is covered
        # by startup above; their callable bodies are covered by these inputs.
        inputs = set(SHARED_INPUTS) | helper_inputs
        inputs.update('lib/brain/' + module + '.py' for module in dependencies)
        inputs.update('tests/' + file for file in files)
        if name == 'catch-all':
            inputs.add('tests/' + COMPATIBILITY)
        lanes.append(dict(name=name,
                          command='python3 -B -m pytest -q -p no:cacheprovider '
                                  + ' '.join(test_targets(root, files))
                                  + ' # startup:' + startup,
                          inputs=sorted(inputs)))
    return lanes


if __name__ == '__main__':
    print(json.dumps(lane_map(), sort_keys=True))
