"""Append a failure diagnosis to the instance's dated Markdown ledger."""
import re

from . import common, nodes

HEADER = '| # | What failed | First explanation (ours) | Real cause | Found by | Fix |\n|---|---|---|---|---|---|\n'


def add(inst, failed, first, cause, found_by, fix):
    if found_by not in ('hadi', 'agent'):
        raise ValueError('found by must be hadi or agent')
    values = [failed, first, cause, found_by, fix]
    if any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError('ledger fields must be nonempty')
    # Escape table separators and line breaks without creating Markdown structure.
    values = [v.replace('&', '&amp;').replace('|', '&#124;').replace('<', '&lt;').replace('>', '&gt;')
              .replace('\r', ' ').replace('\n', '<br>') for v in values]
    path = inst.root / 'library/failure-ledger.md'
    common.safe_target(path)
    text = path.read_text(encoding='utf-8') if path.exists() else '# Failure ledger\n'
    date = inst.now_dt().date().isoformat()
    heading = re.search(r'^## ' + date + r'(?:[ \t][^\n]*)?(?:\n|$)', text, re.M)
    if heading:
        end = re.search(r'^## ', text[heading.end():], re.M)
        end = heading.end() + end.start() if end else len(text)
        body = text[heading.end():end]
        table = re.search(r'^\|\s*#\s*\|[^\n]*\n\|[-| :]+\n', body, re.M)
        if not table or [cell.strip() for cell in table[0].splitlines()[0].split('|')[1:-1]] != [
                '#', 'What failed', 'First explanation (ours)', 'Real cause', 'Found by', 'Fix']:
            raise ValueError('dated failure ledger table is missing or unrecognised; retained unchanged')
        rows, cursor = [], table.end()
        for line in body[cursor:].splitlines(keepends=True):
            row = re.fullmatch(r'\|\s*(\d+)\s*\|[^\n]*\n?', line)
            if not row:
                break
            rows.append(int(row[1]))
            cursor += len(line)
        number = max(rows, default=0) + 1
        at = heading.end() + cursor
        row = '| ' + str(number) + ' | ' + ' | '.join(values) + ' |\n'
        text = text[:at].rstrip('\n') + '\n' + row + text[at:]
    else:
        text = text.rstrip('\n') + '\n\n## ' + date + '\n\n' + HEADER + '| 1 | ' + ' | '.join(values) + ' |\n'
    common.atomic_write(path, text, create=True)
    common.touch(path)
    inst.commit([path], 'failure ledger: diagnosis added')
    return path


def cmd_ledger(a):
    try:
        path = add(nodes.load(), a.failed, a.first_explanation, a.cause, a.found_by, a.fix)
        print('failure ledger row added: ' + str(path))
    except (OSError, ValueError) as error:
        common.die(str(error))
