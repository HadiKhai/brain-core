"""Explicit machine-local external snapshots for optional isolated comparisons."""
import copy
import os
from pathlib import Path


def freeze_declared(inst, directory):
    """Copy declared roots once, then rebind only supported local path operands.

    A declaration is local.json's rehearse_external = {version: 1, roots: [...]}.
    Unknown bindings remain observable through the existing drift detector. Copies
    preserve dirty/untracked/ignored bytes; this is a fixed capture, not an atomic
    historical transaction with concurrent source writers. Return a real Instance
    with independent in-memory bindings, without publishing into the caller.
    """
    from . import rehearse
    declaration = getattr(inst, 'local', {}).get('rehearse_external')
    if declaration is None:
        return inst
    def refuse():
        raise rehearse.RehearsalError('invalid external snapshot declaration',
                                     code='invalid-external-snapshot-declaration')
    if (not isinstance(declaration, dict) or set(declaration) != {'version', 'roots'}
            or declaration['version'] != 1 or not isinstance(declaration['roots'], list)
            or not 1 <= len(declaration['roots']) <= 64):
        refuse()
    roots = []
    for value in declaration['roots']:
        if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
            refuse()
        root = Path(value).expanduser()
        if '..' in root.parts or any(p.is_symlink() for p in (root, *root.parents)):
            refuse()
        root = root.resolve()
        if (not root.is_dir() or root == Path(root.anchor) or root == inst.root
                or root in inst.root.parents or inst.root in root.parents
                or any(root == old or root in old.parents or old in root.parents for old in roots)):
            refuse()
        roots.append(root)
    bindings = []
    for node in inst.nodes():
        local = node.get('local', {})
        bindings.extend(value for value in (local.get('home'), local.get('workbench')) if value)
        bindings.extend(w['path'] for w in local.get('worktrees', {}).values() if w.get('path'))
    # No arbitrary filesystem scan: every root must be an exact supported binding.
    supported = {Path(value).expanduser().absolute() for value in bindings}
    if any(root not in supported for root in roots):
        refuse()
    directory.mkdir()
    mappings = []
    for index, root in enumerate(roots):
        target = directory / str(index)
        rehearse.copy_tree(root, target)
        rehearse.validate_frozen_links(target)
        # Workbenches can contain nested repositories without being Git roots.
        # Sanitize every captured repository, including untracked nested ones.
        for top, dirs, files in os.walk(target, followlinks=False):
            dirs[:] = [name for name in dirs if name != '.git']
            repo = Path(top)
            if (repo / '.git').exists():
                rehearse.materialize_git(repo, repo)
        mappings.append((root, target))
    def remap(value):
        if not isinstance(value, str):
            return value
        path = Path(value).expanduser()
        if path.is_absolute():
            for root, target in mappings:
                try:
                    return str(target / path.relative_to(root))
                except ValueError:
                    pass
        return value
    local = copy.deepcopy(inst.local)
    for node in local.get('nodes', {}).values():
        for key in ('home', 'workbench'):
            if key in node:
                node[key] = remap(node[key])
        for tree in node.get('worktrees', {}).values():
            if 'path' in tree:
                tree['path'] = remap(tree['path'])
    # The declaration is preparation-only; it cannot become a live observation.
    local.pop('rehearse_external')
    # Capturing operands must never publish into the caller, which may be live.
    # The preparation owner alone persists these bindings into its private copy.
    from .instance import Instance
    captured = Instance(inst.root)
    captured.local = local
    return captured
