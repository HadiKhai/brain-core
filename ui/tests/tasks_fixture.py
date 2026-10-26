"""Task UI examples are synthetic and never read an instance or provider."""

def seed_tasks(state):
    node = state['nodes'][1]
    def row(ident, title, status, **extra):
        return {'id': ident, 'title': title, 'status': status, 'priority': 'P2', 'order': 1,
                'owner': 'Demo developer', 'depends_on': [], 'blocked_by': [], 'blocks': [],
                'decisions': [], 'evidence': [], 'request': {'quote': 'Keep the source request',
                    'at': '2026-01-01T10:00:00Z', 'ref': 'notes.md:3', 'date_basis': 'source-timestamp'}, **extra}
    node['tasks'] = {'source': 'local', 'error': '', 'rows': [
        row('APP-1', 'Export the field report', 'in-progress', branch='job/navigation', blocks=['APP-2'], attempt_count=2),
        row('APP-2', 'Share the finished report', 'ready', depends_on=['APP-1'], blocked_by=['APP-1']),
        row('APP-3', 'Repair recurring parser failure', 'parked', park_reason='Two failed rounds', resume='Narrow the parser contract'),
        row('APP-4', 'Retain the obsolete preview', 'dropped', drop_reason='Explicitly removed from scope'),
        row('APP-6', 'Reconcile an old delivery claim', 'done', delivery_state='unproven'),
        row('APP-5', 'Record the report format', 'done', merge_sha='a' * 40, code_path='report.py', evidence=['Focused tests passed']),
    ]}
    state['nodes'][2]['tasks'] = {'source': 'linear', 'error': '', 'rows': [
        row('DEMO-7', 'Translate the public report', 'ready', owner='Another developer', source='linear',
            external_url='https://example.com/issue/DEMO-7')
    ]}
