"""Read local approval projections without starting a host or model call."""
import sqlite3
import time
from contextlib import closing
from datetime import datetime


APPROVAL_AFTER_S = 120
APPROVAL_QUERY_S = 0.008
APPROVAL_SQL = """
SELECT r.runtime_request_id, r.thread_id, t.project_id, t.title,
       r.created_at, i.payload_json
FROM orchestration_v2_projection_runtime_requests r
     INDEXED BY orchestration_v2_projection_requests_recovery_idx
JOIN orchestration_v2_projection_threads t ON t.thread_id = r.thread_id
JOIN orchestration_v2_projection_turn_items i
     INDEXED BY orchestration_v2_projection_turn_items_node_ordinal_idx
     ON i.node_id = r.node_id AND i.thread_id = r.thread_id
WHERE r.status = 'pending' AND r.resolved_at IS NULL
  AND t.deleted_at IS NULL AND i.type = 'approval_request' AND i.status = 'waiting'
"""


def rule_approval(local, fire, db_path, now_ts, log=None):
    """One indexed read; existing fire owns request dedupe, refires and retries.

    local is the already-loaded machine node config. Unknown/ambiguous mappings
    cannot route private command text. Never open a missing database for creation.
    """
    import json
    import re
    import sys

    log = log or (lambda message: print(message, file=sys.stderr))
    projects = {}
    for node, config in local.items():
        if (not isinstance(node, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', node)
                or not isinstance(config, dict)):
            continue
        for key in ('home_project', 'host_project'):
            project = config.get(key)
            if isinstance(project, str) and project:
                projects.setdefault(project, set()).add(node)
    if not projects:
        return
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=0.001)) as conn:
            deadline = time.monotonic() + APPROVAL_QUERY_S
            conn.set_progress_handler(lambda: time.monotonic() >= deadline, 100)
            rows = conn.execute(APPROVAL_SQL).fetchall()
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError('approval query deadline')
    except (OSError, ValueError, sqlite3.Error):
        log('watcher: approval source unavailable, changed schema or query deadline; skipped')
        return
    invalid = False
    seen = set()
    for request, thread, project, title, created, payload in rows:
        owners = projects.get(project, set())
        if len(owners) != 1:
            invalid = invalid or len(owners) > 1
            continue
        try:
            item = json.loads(payload)
            if (not isinstance(item, dict) or item.get('requestId') != request
                    or not isinstance(item.get('prompt'), str) or not item['prompt'].strip()
                    or not isinstance(thread, str) or not thread
                    or not isinstance(request, str) or not request
                    or not isinstance(title, str)):
                raise ValueError('invalid approval')
            at = datetime.fromisoformat(created.replace('Z', '+00:00'))
            if at.tzinfo is None:
                raise ValueError('timestamp has no offset')
            age = now_ts - at.timestamp()
        except (TypeError, ValueError, AttributeError, OverflowError):
            invalid = True
            continue
        if age <= APPROVAL_AFTER_S or request in seen:
            continue
        seen.add(request)
        node = next(iter(owners))
        clean = lambda value: ' '.join(value.split())
        reason = ('watcher: approval waiting for %s: %s (t3-thread://%s), %.1f min; '
                  'command: %s. Only the operator can approve; surface this request.' %
                  (node, clean(title)[:120], thread, age / 60, clean(item['prompt'])[:200]))
        fire(node, 'approval:' + request, reason)
    if invalid:
        log('watcher: approval malformed record or ambiguous project mapping; skipped')
