"""Compact, screened reviewer sheets. These commands do not activate proposals."""
import json
import re
import shlex

from . import nodes as _m_nodes


def proposal_claim(body):
    text = re.sub(r'<!--.*?-->', '', body or '', flags=re.S).strip()
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip() and not p.lstrip().startswith('#')]
    claim = ' '.join((paragraphs[0] if paragraphs else text).split())
    return claim[:397] + '...' if len(claim) > 400 else claim or '(read the source)'


def reviewer_brief(rows, screens, as_json=False):
    sheets = []
    for group, terms in zip(rows, screens):
        node = shlex.quote(group['node'])
        for item in group['items']:
            key = shlex.quote(item['id'])
            sheets.append({'node': group['node'], 'id': item['id'], 'kind': item['kind'], 'title': item['title'],
                'claim': proposal_claim(item['body']), 'citation': item['ref'] + ' · ' + item['provenance'],
                'source': item['path'], 'scope': item['scope'],
                'accept': f'brain item accept {key} --reviewed-by <principal>',
                'reject': f'brain item reject {key}'})
        for role in group['specialists']:
            key = shlex.quote(role['id'])
            sheets.append({'node': group['node'], 'id': role['id'], 'kind': 'specialist', 'title': role['title'],
                'claim': proposal_claim(role['body']), 'citation': role['path'], 'source': role['path'],
                'scope': role['scope'], 'accept': f'brain specialist accept {key} --at {node} --reviewed-by <principal>',
                'reject': 'Leave inactive; record rejection in the role source (no specialist reject CLI).'})
        if group['rules']:
            snapshot = group.get('rules_snapshot') or {}
            sheets.append({'node': group['node'], 'id': 'rules', 'kind': 'rules', 'title': 'Imported rules',
                'claim': proposal_claim(group['rules_body']), 'citation': group['rules'], 'source': group['rules'],
                'scope': group['owners'], 'snapshot': snapshot.get('digest'),
                'accept': f'brain proposals {node} --approve-all --review-manifest <review.json>',
                'reject': 'Leave inactive; record the independent rejection in the rules source.'})
        # Metadata, source paths and command tokens receive the same source union
        # screening as the existing full proposal view.
        start = len(sheets) - len(group['items']) - len(group['specialists']) - bool(group['rules'])
        for i in range(start, len(sheets)):
            sheets[i] = json.loads(_m_nodes.import_text(json.dumps(sheets[i], ensure_ascii=False), terms))
    if as_json:
        print(json.dumps(sheets, indent=1, ensure_ascii=False))
        return
    if not sheets:
        print('nothing waiting for review')
        return
    print('Independent proposal review: verify each claim against its citation and full source.\n'
          'Recommend accept/reject with reasons; activation requires an authorized principal.\n')
    for sheet in sheets:
        print(f"{sheet['node']} · {sheet['id']}: {sheet['title']} ({sheet['kind']})")
        print('Claim: ' + sheet['claim'])
        print('Citation: ' + sheet['citation'])
        print('Read full: ' + sheet['source'])
        print('Scope: ' + ', '.join(sheet['scope']))
        print('Accept: ' + sheet['accept'])
        print('Reject: ' + sheet['reject'] + '\n')
