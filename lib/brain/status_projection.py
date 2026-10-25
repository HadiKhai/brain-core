"""An explicitly invoked, configured owner of generated facts only."""
from . import common, events, status


def project_owned(inst, n):
    if n['fm'].get('status_projection') != 'owned-facts':
        common.die('status projection requires status_projection=owned-facts')
    if n['archived']:
        common.die('archived status projection refused')
    _, _, opened = events.replay(inst)
    ours = {op.ref for op in events._CURRENT if op.inst.root == inst.root}
    if any(ref not in ours for ref in opened):
        common.die('status projection requires no active or interrupted writer; refresh explicitly')
    path = n['dir'] / 'status.md'
    common.safe_target(path)
    with path.open(newline='') as stream:
        text = stream.read()
    match = status.FACTS_RE.search(text)
    if (text.count(status.FACTS_START) != text.count(status.FACTS_END)
            or text.count(status.FACTS_START) > 1
            or (status.FACTS_START in text and not match)):
        common.die('status projection refuses incomplete or duplicate facts markers')
    view = (events.events_view_without_current(inst, n) if events.source(inst, n) == 'events'
            else events.snapshot_view(inst, n, status.facts_meta(n).get('merged', [])))
    for op in reversed(events._CURRENT):
        if op.inst.root == inst.root and op.command == 'project_status':
            op.projected_views = {n['id']: view}
            break
    # Compare semantic facts first: an idle phase does not rewrite timestamps.
    previous = status.facts_meta(n)
    if match and previous.get('fingerprint') == status.status_fingerprint(view['facts']) \
            and previous.get('merged', []) == list(view['merged']):
        return False
    block = status.facts_block(inst, n, view)
    updated = text[:match.start()] + block + text[match.end():] if match else text + '\n\n' + block + '\n'
    with path.open(newline='') as stream:
        if stream.read() != text:
            common.die('status source changed during projection; retry from a fresh source')
    common.atomic_write(path, updated)
    common.touch(path)
    return True
