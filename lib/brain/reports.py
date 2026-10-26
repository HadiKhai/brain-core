"""Reports mechanics for Brain."""
import hashlib
import json
import os
import re
import stat
import time
import urllib.parse
from pathlib import Path

from . import common as _m_common
from . import nodes as _m_nodes
from . import worktrees as _m_worktrees


# Binary GiB. Logical file bytes;
# no content reads, allocated-block accounting, or APFS clone-size claims.
REPORT_WARNING_BYTES = 1024 ** 3
REPORT_SIZE_ENTRIES = 10000
REPORT_SIZE_SECONDS = 0.1


def report_size_warnings(inst, nodes):
    """Bound all report metadata work per check; never follow directory links."""
    warnings, seen = [], set()
    remaining = REPORT_SIZE_ENTRIES
    deadline = time.monotonic() + REPORT_SIZE_SECONDS

    def size(folder):
        nonlocal remaining
        total, pending, unknown = 0, [folder], False
        while pending:
            if remaining <= 0 or time.monotonic() >= deadline:
                return total, False
            current = pending.pop()
            if any(p.is_symlink() for p in (current, *current.parents)):
                return total, False
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        remaining -= 1
                        if remaining < 0 or time.monotonic() >= deadline:
                            return total, False
                        value = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(value.st_mode):
                            pending.append(Path(entry.path))
                        elif stat.S_ISREG(value.st_mode):
                            total += value.st_size
                        else:
                            unknown = True  # link/special targets are never read
                        if total > REPORT_WARNING_BYTES:
                            return total, True  # lower bound already proves warning
            except OSError:
                return total, False
        return total, not unknown

    for node in nodes:
        if remaining <= 0 or time.monotonic() >= deadline:
            warnings.append('reports: remaining report roots unknown (metadata budget)')
            break
        if (node.get('external') and not node.get('home_path')
                and not node.get('local', {}).get('workbench')):
            warnings.append(node['id'] + ': reports: size unknown (workbench not bound)')
            continue
        root = _m_worktrees.workbench(inst, node) / 'reports'
        if root in seen:
            continue
        seen.add(root)
        label = node['id'] + ': reports'
        if any(p.is_symlink() for p in (root, *root.parents)):
            warnings.append(label + ': size unknown (linked report root)')
            continue
        try:
            if not root.exists():
                continue
            with os.scandir(root) as folders:
                for entry in folders:
                    remaining -= 1
                    if remaining < 0 or time.monotonic() >= deadline:
                        warnings.append(label + ': remaining folder sizes unknown (metadata budget)')
                        break
                    if entry.is_symlink():
                        warnings.append(label + '/' + entry.name + ': size unknown (link)')
                    elif entry.is_dir(follow_symlinks=False):
                        total, known = size(Path(entry.path))
                        if total > REPORT_WARNING_BYTES:
                            warnings.append(label + '/' + entry.name + ': report folder exceeds 1 GiB; review retained bulk copies')
                        elif not known:
                            warnings.append(label + '/' + entry.name + ': size unknown (metadata budget or unreadable)')
        except OSError:
            warnings.append(label + ': size unknown (unreadable)')
    return warnings


def preview_receipt_file(inst, n):
    return inst.root / '.brain/preview-evidence' / (hashlib.sha256(n['id'].encode()).hexdigest() + '.json')


def validate_preview_evidence(inst, n, data):
    """Bind a browser-authored tour to its configured address and clean source revision."""
    if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1:
        raise ValueError('unsupported preview receipt')
    url, branch, head = (data.get(k) for k in ('url', 'branch', 'head'))
    addresses = [n['local'].get('preview'), n['fm'].get('preview_url')]
    if not isinstance(url, str) or not url.strip() or url not in addresses:
        raise ValueError('preview address changed or is not configured')
    if not isinstance(branch, str) or not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head):
        raise ValueError('preview source revision is invalid')
    binding = n['local'].get('worktrees', {}).get(branch)
    source = Path(binding['path']) if branch and binding and binding.get('path') else n['home_path'] if not branch else None
    if source is None or not source.is_dir():
        raise ValueError('preview source is not bound on this machine')
    current = _m_common.git('rev-parse', '--verify', 'HEAD', cwd=source)
    if current.returncode or current.stdout.strip() != head:
        raise ValueError('preview source revision changed')
    dirty = _m_common.git('--no-optional-locks', 'status', '--porcelain', '--untracked-files=normal', cwd=source)
    if dirty.returncode or dirty.stdout.strip():
        raise ValueError('preview source has uncommitted changes')
    captured = inst.parse_time(data.get('checked_at'))
    if captured is None or captured.timestamp() > inst.now_dt().timestamp() + 300:
        raise ValueError('preview check time is invalid')

    def artifact(value, suffix):
        if not isinstance(value, dict) or not isinstance(value.get('path'), str) or not isinstance(value.get('sha256'), str):
            raise ValueError('preview artifact is invalid')
        path = Path(value['path'])
        if not path.is_absolute() or path.suffix.lower() != suffix or not re.fullmatch(r'[0-9a-f]{64}', value['sha256']):
            raise ValueError('preview artifact path or digest is invalid')
        if not path.is_file() or not 0 < path.stat().st_size <= 20 * 1024 * 1024:
            raise ValueError('preview artifact is missing or too large')
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != value['sha256']:
            raise ValueError('preview artifact changed')
        return path.resolve(), body

    tour, body = artifact(data.get('tour'), '.md')
    configured_tour = n['local'].get('preview_tour')
    if not isinstance(configured_tour, str) or not configured_tour.strip():
        raise ValueError('preview tour is not configured')
    bound_tour = Path(configured_tour).expanduser()
    if not bound_tour.is_absolute():
        bound_tour = n['dir'] / bound_tour
    if bound_tour.resolve() != tour:
        raise ValueError('preview tour binding changed')
    images = data.get('screenshots')
    if not isinstance(images, list) or not 1 <= len(images) <= 100:
        raise ValueError('preview tour needs screenshots')
    references = set()
    for match in re.finditer(r'!\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))[^)]*\)', body.decode('utf-8')):
        references.add((tour.parent / urllib.parse.unquote(match.group(1) or match.group(2))).resolve())
    seen = set()
    for image in images:
        path, content = artifact(image, '.png')
        if (path in seen or path not in references or len(content) < 24
                or content[:8] != b'\x89PNG\r\n\x1a\n' or content[12:16] != b'IHDR'
                or not int.from_bytes(content[16:20], 'big') or not int.from_bytes(content[20:24], 'big')):
            raise ValueError('preview screenshots must be distinct PNG images linked from the tour')
        seen.add(path)
    return {'state': 'verified', 'head': head, 'branch': branch, 'screenshots': len(images),
            'checked_at': data['checked_at']}


def preview_evidence(inst, n):
    path = preview_receipt_file(inst, n)
    if not path.exists():
        return {'state': 'missing', 'reason': 'no source-bound screenshot tour recorded'}
    try:
        return validate_preview_evidence(inst, n, json.loads(path.read_text()))
    except (OSError, ValueError, TypeError, KeyError):
        return {'state': 'stale', 'reason': 'preview evidence no longer matches its source or artifacts'}


def cmd_preview(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    if a.record:
        try:
            data = json.loads(Path(a.record).expanduser().read_text())
            result = validate_preview_evidence(inst, n, data)
        except (OSError, ValueError, TypeError, KeyError):
            _m_common.die('preview: receipt, clean source revision or tour artifacts failed validation; prior receipt retained')
        path = preview_receipt_file(inst, n)
        _m_common.safe_target(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _m_common.atomic_write(path, json.dumps(data, sort_keys=True) + '\n', private=True)
    else:
        result = preview_evidence(inst, n)
    print(json.dumps(result) if a.json else 'preview: ' + result['state'] +
          ('; ' + result['reason'] if result.get('reason') else '; source and screenshot tour match'))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "report"


def cmd_report(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    wb = _m_worktrees.workbench(inst, n, create=True)
    d = wb / "reports" / f"{inst.now_dt().strftime('%Y-%m-%d')}-{slug(a.topic)}"
    d.mkdir(parents=True, exist_ok=True)
    idx = d / "index.md"
    if not idx.exists():
        idx.write_text(
            f"# {a.topic}\n\nCreated: {inst.now()} for {n['id']}\n\n"
            "## Answer\n\n<!-- the conclusion, in a few lines -->\n\n"
            "## Findings\n\n<!-- each with its evidence: the command run, the numbers, the hash or file -->\n\n"
            "## Controls\n\n<!-- what would have failed if the finding were wrong, and whether it did -->\n\n"
            "## Not run\n\n<!-- what wasn't checked, and why; \"none\" only if truly none -->\n\n"
            "## Lessons\n\n<!-- what the next job should know: a pitfall, a tool's quirk, a method that worked; "
            "or \"none: <reason>\". The coordinator turns reusable ones into library items -->\n\n"
            "## Files\n\n## How to reproduce\n\n<!-- the script in scripts/ that made this, and how to run it -->\n")
    print(d)


def cmd_reports(a):
    inst = _m_nodes.load()
    for n in inst.nodes():
        if a.id and n["id"] != a.id:
            continue
        try:
            wb = _m_worktrees.workbench(inst, n)
        except SystemExit:
            continue
        for d in sorted((wb / "reports").glob("*/index.md"), reverse=True) if (wb / "reports").is_dir() else []:
            title = next((l[2:] for l in d.read_text().splitlines() if l.startswith("# ")), d.parent.name)
            print(f"{n['id']:<24} {d.parent.name[:10]}  {title}  ({_m_common.tilde(d.parent)})")


# ---------- schedules ----------
