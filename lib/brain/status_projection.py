"""An explicitly invoked, configured owner of generated facts only."""
from . import common, events, status, status_refresh


def project_owned(inst, n):
    if n['fm'].get('status_projection') != 'owned-facts':
        common.die('status projection requires status_projection=owned-facts')
    if n['archived']:
        common.die('archived status projection refused')
    path = n['dir'] / 'status.md'
    common.safe_target(path)

    def prepare(current, node):
        _, _, opened = events.replay(current)
        ours = {op.ref for op in events._CURRENT if op.inst.root == current.root}
        if any(ref not in ours for ref in opened):
            common.die('status projection requires no active or interrupted writer; refresh explicitly')
        with (node['dir'] / 'status.md').open(newline='') as stream:
            text = stream.read()
        match = status.FACTS_RE.search(text)
        if (text.count(status.FACTS_START) != text.count(status.FACTS_END)
                or text.count(status.FACTS_START) > 1
                or (status.FACTS_START in text and not match)):
            common.die('status projection refuses incomplete or duplicate facts markers')
        view = (events.events_view_without_current(current, node) if events.source(current, node) == 'events'
                else events.snapshot_view(current, node, status.facts_meta(node).get('merged', [])))
        previous = status.facts_meta(node)
        if match and previous.get('fingerprint') == status.status_fingerprint(view['facts']) \
                and previous.get('merged', []) == list(view['merged']):
            return view, text, text
        block = status.facts_block(current, node, view)
        updated = text[:match.start()] + block + text[match.end():] if match else text + '\n\n' + block + '\n'
        return view, text, updated

    view, text, updated = status_refresh.compute(inst, n, prepare, extra=(inst.root / '.brain/events.jsonl',))
    for op in reversed(events._CURRENT):
        if op.inst.root == inst.root and op.command == 'project_status':
            op.projected_views = {n['id']: view}
            break
    # Compare semantic facts first: an idle phase does not rewrite timestamps.
    if updated == text:
        return False
    with path.open(newline='') as stream:
        if stream.read() != text:
            common.die('status source changed during projection; retry from a fresh source')
    common.atomic_write(path, updated)
    common.touch(path)
    return True
