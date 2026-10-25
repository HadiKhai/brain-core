"""Opt-in stdlib Linear adapter. Product fields are provider-owned.

Only explicit title/description creation or issue/state UUIDs leave this module;
internal job, requirement and decision metadata never enters API variables.
"""
import json
import os
import urllib.error
import urllib.request
import uuid

from . import common as _m_common
from . import instance as _m_instance
from . import tasks as _m_tasks

ENDPOINT = 'https://api.linear.app/graphql'
ISSUE_FIELDS = '''id identifier title description priority url updatedAt team { id }
 assignee { id name } state { id type name }
 inverseRelations(first: 100) { nodes { type issue { identifier } } pageInfo { hasNextPage } }'''
PULL = '''query TaskIssues($team: ID!, $after: String) {
 issues(first: 100, after: $after, filter: {team: {id: {eq: $team}}}) {
 nodes { ''' + ISSUE_FIELDS + ''' } pageInfo { hasNextPage endCursor } } }'''
STATUS = {'triage': 'backlog', 'backlog': 'backlog', 'unstarted': 'ready', 'started': 'in-progress',
          'completed': 'done', 'canceled': 'dropped'}


def uid(value):
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError('Linear configuration or response has an invalid UUID')


def request(query, variables):
    token = os.environ.get('BRAIN_LINEAR_TOKEN')
    if not token:
        raise ValueError('Linear tasks need BRAIN_LINEAR_TOKEN in this process environment')
    auth = token if token.startswith('lin_api_') else 'Bearer ' + token
    req = urllib.request.Request(ENDPOINT, data=json.dumps({'query': query, 'variables': variables}).encode(),
                                 headers={'Content-Type': 'application/json', 'Authorization': auth}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as stream:
            data = json.loads(stream.read(8 * 1024 * 1024 + 1))
        if not isinstance(data, dict) or data.get('errors') or not isinstance(data.get('data'), dict):
            raise ValueError('Linear rejected the task request; mirror retained')
        return data['data']
    except (OSError, ValueError) as error:
        # Provider messages can include request variables or credential details.
        raise ValueError('Linear task request failed; mirror and pending state retained') from None


def config(n):
    if _m_tasks.source(n) != 'linear':
        raise ValueError('this node does not use Linear tasks')
    return uid(n['fm'].get('tasks.linear.team_id'))


def collect(n):
    team = config(n)
    issues, cursor, seen = [], None, set()
    for _ in range(100):
        result = request(PULL, {'team': team, 'after': cursor}).get('issues')
        if not isinstance(result, dict) or not isinstance(result.get('nodes'), list) or not isinstance(result.get('pageInfo'), dict):
            raise ValueError('invalid Linear issue page; mirror retained')
        issues.extend(result['nodes'])
        page = result['pageInfo']
        if not page.get('hasNextPage'):
            return issues
        cursor = page.get('endCursor')
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ValueError('invalid Linear pagination; mirror retained')
        seen.add(cursor)
    raise ValueError('Linear task pagination limit reached; narrow the team scope')


def pull(inst, n):
    issues = collect(n)  # network never holds this module's instance lock
    team = config(n)
    with _m_instance.brain_lock(inst):
        old = _m_tasks.read_records(n)
        rows = {k: dict(v) for k, v in old.items()}
        for issue in issues:
            ident = issue.get('identifier')
            _m_tasks.record_path(n, ident)
            if uid(issue.get('team', {}).get('id')) != team:
                raise ValueError('Linear returned another team; mirror retained')
            previous = rows.get(ident, {})
            if previous and previous.get('source') != 'linear':
                raise ValueError('provider issue collides with a local task id; mirror retained')
            state = issue.get('state') or {}
            status = STATUS.get(state.get('type'))
            if not status or not isinstance(issue.get('title'), str) or not issue['title'].strip():
                raise ValueError('invalid Linear task state or title; mirror retained')
            relations = issue.get('inverseRelations') or {}
            if relations.get('pageInfo', {}).get('hasNextPage') or not isinstance(relations.get('nodes'), list):
                raise ValueError('incomplete Linear dependency page; mirror retained')
            dependencies = [r['issue']['identifier'] for r in relations['nodes'] if r.get('type') == 'blocks']
            assignee = issue.get('assignee') or {}
            row = {'version': _m_tasks.VERSION, 'id': ident, 'kind': 'feature', 'order': 0, 'decisions': [],
                   'evidence': [], 'provenance': [], 'superseded_by': [], 'scope': [], 'aliases': [], 'unresolved_dependencies': [], 'completion_kind': 'merge', 'last_progress_at': inst.now(), 'strikes': 0, 'created': inst.now(), **previous,
                   'source': 'linear', 'title': issue['title'], 'description': issue.get('description') or '',
                   'status': status, 'priority': {0: 'P2', 1: 'P0', 2: 'P1', 3: 'P2', 4: 'P3'}.get(issue.get('priority'), 'P2'),
                   'owner': assignee.get('name') or '', 'depends_on': dependencies,
                   'external_url': issue.get('url') or '',
                   'provider': {'id': uid(issue.get('id')), 'team': team, 'state_id': uid(state.get('id')),
                                'state_type': state['type'], 'synced_at': inst.now(), 'updated_at': issue.get('updatedAt')}}
            if previous.get('status') == 'parked':
                row['status'] = 'parked'
            if status == 'dropped':
                row['drop_reason'] = 'Provider state: canceled'
            # Source updates do not pretend to be local work activity.
            row['updated'] = previous.get('updated') or inst.now()
            if previous and any(previous.get(key) != row.get(key) for key in ('title', 'description', 'status', 'depends_on')):
                row['last_progress_at'] = inst.now()
            if status == 'done' and previous.get('status') != 'done':
                row['event_pending'] = 'task_landed'
            rows[ident] = row
        _m_tasks.validate(n, rows)
        for ident, row in rows.items():
            if row != old.get(ident):
                _m_tasks.save_record(n, row)
                if row.get('event_pending'):
                    _m_tasks.emit_task_landed(inst, n, row)
        inst.commit([], n['id'] + ': pull external tasks')
    return list(rows.values())


def write_state(inst, n, ident, state_type):
    """Persist intent before writing; retries send only provider UUIDs."""
    team = config(n)
    if state_type not in ('started', 'completed'):
        raise ValueError('only started/completed job transitions write to Linear')
    with _m_instance.brain_lock(inst):
        rows = _m_tasks.read_records(n)
        row = dict(rows[ident])
        if row.get('source') != 'linear' or row.get('provider', {}).get('team') != team:
            raise ValueError('task is not owned by this provider team')
        row['provider_pending'] = state_type
        _m_tasks.save_record(n, row)
        inst.commit([], n['id'] + ': pending external task state')
    key = 'tasks.linear.' + ('started_state_id' if state_type == 'started' else 'done_state_id')
    state_id = uid(n['fm'].get(key))
    provider_id = uid(row['provider']['id'])
    data = request('mutation TaskState($id: String!, $state: String!) { issueUpdate(id: $id, input: {stateId: $state}) { success issue { id } } }',
                   {'id': provider_id, 'state': state_id}).get('issueUpdate') or {}
    if not data.get('success') or (data.get('issue') or {}).get('id') != provider_id:
        raise ValueError('Linear did not confirm task state; pending state retained')
    with _m_instance.brain_lock(inst):
        current = _m_tasks.read_records(n)[ident]
        if current.get('provider_pending') == state_type:
            current.pop('provider_pending', None)
            _m_tasks.save_record(n, current)
            inst.commit([], n['id'] + ': external task state confirmed')


def sync(inst, n):
    pull(inst, n)
    for row in _m_tasks.read_records(n).values():
        if row.get('provider_pending'):
            write_state(inst, n, row['id'], row['provider_pending'])


def create(inst, n, title, description=''):
    """Search by normalized title in the team before creating a product issue."""
    team = config(n)
    if not title.strip():
        raise ValueError('title required')
    matches = [r for r in collect(n) if r.get('title', '').strip().casefold() == title.strip().casefold()]
    if len(matches) > 1:
        raise ValueError('multiple existing Linear issues match; choose the existing issue explicitly')
    if not matches:
        # Only explicitly supplied product text is sent, never internal record data.
        data = request('mutation TaskCreate($team: String!, $title: String!, $description: String!) { issueCreate(input: {teamId: $team, title: $title, description: $description}) { success issue { identifier } } }',
                       {'team': team, 'title': title, 'description': description}).get('issueCreate') or {}
        if not data.get('success'):
            raise ValueError('Linear issue creation was not confirmed')
        ident = (data.get('issue') or {}).get('identifier')
    else:
        ident = matches[0]['identifier']
    pull(inst, n)
    return _m_tasks.read_records(n)[ident]
