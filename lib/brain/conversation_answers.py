"""Exact conversation-answer evidence, queued by hooks and applied by inbox reads."""
import hashlib
import json
import os
import stat

from . import common as _m_common
from . import guard as _m_guard
from . import hosts as _m_hosts
from . import nodes as _m_nodes
from . import reminders as _m_reminders


def question_text(n, question):
    parts = [question['question']]
    options = question.get('options')
    for option in options if isinstance(options, list) else []:
        if isinstance(option, dict):
            parts += [option[key] for key in ('label', 'description') if isinstance(option.get(key), str)]
    terms = _m_guard.never_terms(n)
    text = ' '.join(_m_nodes.import_text('\n'.join(parts), terms).split())
    return _m_nodes.import_text(text, terms)


def question_origin(question, data):
    session, call = data.get('session_id'), question.get('_source_call')
    if not isinstance(session, str) or not session or not isinstance(call, str) or not call:
        return None
    return hashlib.sha256(json.dumps([session, call, question.get('id') or question['question']],
                                    sort_keys=True).encode()).hexdigest()


def question_marker(n, question, data):
    text, origin = question_text(n, question), question_origin(question, data)
    digest = hashlib.sha256(((origin + '\0') if origin else '').encode() + text.encode()).hexdigest()
    return '<!-- host-question:' + digest + ' -->'


def answer_records(path):
    """Bound the snapshot; partial leading/final JSON records never become evidence."""
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return []
    with os.fdopen(fd, 'rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        start = max(0, size - _m_hosts.HOOK_TRANSCRIPT_LIMIT)
        stream.seek(start)
        data = stream.read(_m_hosts.HOOK_TRANSCRIPT_LIMIT)
    lines = data.split(b'\n')
    if start:
        lines = lines[1:]
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def conversation_answer_pairs(records):
    calls, current, pairs = {}, None, []
    def questions(identity, args):
        if not isinstance(identity, str) or not isinstance(args, dict):
            return
        calls[identity] = [dict(q, _source_call=identity) for q in args.get('questions', [])
                           if isinstance(q, dict) and isinstance(q.get('question'), str)]
    def answered(identity, answers):
        if not isinstance(answers, dict):
            return
        remaining = []
        for question in calls.get(identity, []):
            value = answers.get(question.get('id') or question['question'])
            values = value.get('answers') if isinstance(value, dict) else [value] if isinstance(value, str) else None
            if isinstance(values, list) and values and all(isinstance(v, str) and v.strip() for v in values):
                pairs.append((question, '\n'.join(values), 'answered-tool-result'))
            else:
                remaining.append(question)
        calls[identity] = remaining
    for record in records:
        kind, message = record.get('type'), record.get('message', {})
        blocks = message.get('content', []) if isinstance(message, dict) else []
        if kind == 'assistant' and isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'AskUserQuestion':
                    questions(block.get('id'), block.get('input'))
                    current = block.get('id')
        if kind == 'user' and isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get('type') == 'tool_result' and not block.get('is_error'):
                    value = block.get('content')
                    if (isinstance(value, str) and value.startswith('User has answered your questions:')
                            and len(calls.get(block.get('tool_use_id'), [])) == 1):
                        question = calls[block['tool_use_id']][0]
                        text = value[len('User has answered your questions:'):].strip()
                        if text:
                            pairs.append((question, text, 'answered-tool-result'))
                            calls[block['tool_use_id']] = []
                        continue
                    try:
                        value = json.loads(value) if isinstance(value, str) else value
                    except ValueError:
                        continue
                    answered(block.get('tool_use_id'), value.get('answers') if isinstance(value, dict) else None)
        item = record.get('payload', {}) if kind in ('response_item', 'event_msg') else record
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'function_call' and item.get('name', '').split('.')[-1] == 'request_user_input':
            try:
                args = item.get('arguments', {})
                questions(item.get('call_id'), json.loads(args) if isinstance(args, str) else args)
                current = item.get('call_id')
            except ValueError:
                pass
        if item.get('type') == 'function_call_output':
            try:
                output = item.get('output', {})
                output = json.loads(output) if isinstance(output, str) else output
                answered(item.get('call_id'), output.get('answers') if isinstance(output, dict) else None)
            except ValueError:
                pass
        if _m_hosts.hook_operator_message(record):
            content = item.get('message') if item.get('type') == 'user_message' else item.get('content', blocks)
            if kind == 'user':
                content = blocks
            if isinstance(content, list):
                content = '\n'.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') in ('text', 'input_text'))
            pending = calls.get(current, [])
            # A single adjacent operator reply selecting an offered option is
            # positive evidence. Free text, clarifications and multiple asks
            # remain open unless the runtime supplies an exact answer result.
            if isinstance(content, str) and len(pending) == 1:
                selected = content.strip().rstrip('.!').casefold()
                offered = pending[0].get('options')
                options = [o['label'].casefold() for o in offered if isinstance(o, dict) and isinstance(o.get('label'), str)] if isinstance(offered, list) else []
                if selected and selected in options:
                    pairs.append((pending[0], content.strip(), 'conversation-option'))
                    calls[current] = []
            current = None
    return pairs


def queue_conversation_answers(inst, n, data):
    path = data.get('transcript_path')
    if not isinstance(path, str) or not path or _m_common.outward_links(inst.root):
        return
    inbox = n['dir'] / 'inbox.md'
    lines = [line for line in inbox.read_text().splitlines() if line.startswith('- ')] if inbox.is_file() else []
    lines = _m_reminders.request_lines(inst, n, lines, readonly=True)
    folder = inst.root / '.brain/wake/conversation-answers'
    for question, answer, source in conversation_answer_pairs(answer_records(path)):
        origin = question_origin(question, data)
        if origin is None:
            continue  # no session/call evidence: never resolve an unknown origin
        marker = question_marker(n, question, data)
        matches = [line for line in lines if marker in line]
        if len(matches) != 1:
            continue
        episode = _m_reminders.request_episode_id(inst, n, matches[0])
        text = _m_nodes.import_text(answer, _m_guard.never_terms(n))
        if not text.strip():
            continue
        evidence = hashlib.sha256(json.dumps([origin, source, answer], sort_keys=True).encode()).hexdigest()
        identity = hashlib.sha256(json.dumps([n['id'], episode, evidence]).encode()).hexdigest()
        target = folder / (identity + '.json')
        _m_common.safe_target(target)
        folder.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.with_suffix('.done').exists():
            continue
        if len(list(folder.glob('*.json'))) >= _m_reminders.LIFECYCLE_LIMIT:
            raise ValueError('answer evidence queue reached its bound; retain it for repair')
        _m_common.atomic_write(target, json.dumps({'node': n['id'], 'episode': episode,
            'marker': marker, 'answer': text, 'origin': origin, 'evidence': evidence,
            'source': source, 'at': inst.now(), 'state': 'pending'}, sort_keys=True) + '\n', private=True)


def answer_lines(inst, n, lines, readonly=False, lifecycle_data=None):
    if _m_common.outward_links(inst.root):
        return _m_reminders.request_lines(inst, n, lines, readonly=True, lifecycle_data=lifecycle_data)
    folder = inst.root / '.brain/wake/conversation-answers'
    if not readonly and folder.is_dir():
        if lifecycle_data is None:
            try:
                with _m_reminders.lifecycle_store(inst) as data:
                    return answer_lines(inst, n, lines, lifecycle_data=data)
            except (OSError, ValueError):
                return _m_reminders.request_lines(inst, n, lines, readonly=True)
        _m_reminders.reconcile_requests(inst, n, lines, lifecycle_data)
        for path in sorted(folder.glob('*.json'))[:_m_reminders.LIFECYCLE_LIMIT]:
            _m_common.safe_target(path)
            try:
                receipt = json.loads(path.read_text())
                if receipt.get('node') != n['id'] or receipt.get('state') != 'pending':
                    continue
                matches = [line for line in lines if receipt.get('marker') and receipt['marker'] in line
                           and _m_reminders.request_episode_id(inst, n, line, lifecycle_data) == receipt.get('episode')]
                if len(matches) != 1:
                    continue  # stale or unknown evidence is retained, never applied to a successor
                key = _m_reminders.inbox_source_id(n, matches[0])
                current = lifecycle_data['inbox'].get(n['id'], {}).get(key, {})
                if not current.get('answered'):
                    _m_reminders.record_answer(inst, n, matches[0], receipt['episode'], receipt['answer'], lifecycle_data)
                    lifecycle_data['answers'][-1]['evidence'] = {k: receipt[k] for k in ('source', 'origin', 'evidence', 'at')}
                    continue  # do not consume evidence before the lifecycle transaction commits
                persisted = _m_reminders.lifecycle_read(inst)
                durable = persisted['inbox'].get(n['id'], {}).get(key, {})
                if (durable.get('id') != receipt['episode'] or durable.get('answered') is not True
                        or not any(a.get('id') == receipt['episode'] and
                                   a.get('evidence', {}).get('evidence') == receipt['evidence'] for a in persisted['answers'])):
                    continue
                receipt['state'] = 'applied'
                # Retain actual answer evidence; never erase it as stale cleanup.
                _m_common.safe_target(path.with_suffix('.done'))
                _m_common.atomic_write(path.with_suffix('.done'), json.dumps(receipt, sort_keys=True) + '\n', private=True)
                path.unlink()
            except (OSError, ValueError, KeyError):
                continue  # unknown evidence cannot close a request
    return _m_reminders.request_lines(inst, n, lines, readonly=readonly, lifecycle_data=lifecycle_data)
