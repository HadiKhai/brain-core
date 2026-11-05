"""Small, bounded stdlib Messages API client. No telemetry or implicit retries."""
import json
import os
import subprocess
import sys
import time


class AIUnavailable(Exception):
    """A safe diagnostic code, never provider response text or credentials."""


# A child gives the entire exchange a deadline, including DNS and slow reads.
_REQUEST = '''
import json, os, sys, urllib.request
body = sys.stdin.buffer.read()
request = urllib.request.Request('https://api.anthropic.com/v1/messages', data=body,
    headers={'content-type': 'application/json', 'anthropic-version': '2023-06-01',
             'x-api-key': os.environ['ANTHROPIC_API_KEY']})
try:
    with urllib.request.urlopen(request, timeout=19) as response:
        value = response.read(1048577)
        if len(value) > 1048576:
            raise ValueError('response bound')
        sys.stdout.buffer.write(value)
except Exception:
    sys.exit(1)
'''


def json_request(system, data, schema, model='claude-haiku-5-5'):
    return {'model': model, 'max_tokens': 4096,
            'system': [{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
            'messages': [{'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}],
            'output_config': {'format': {'type': 'json_schema', 'schema': schema}}}


def ai_json(system, data, schema, model='claude-haiku-5-5'):
    if not os.environ.get('ANTHROPIC_API_KEY'):
        raise AIUnavailable('missing_key')
    request = json_request(system, data, schema, model)
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable, '-c', _REQUEST], input=json.dumps(request),
                                text=True, capture_output=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        raise AIUnavailable('timeout') from None
    except OSError:
        raise AIUnavailable('transport') from None
    if result.returncode:
        raise AIUnavailable('provider_or_network')
    try:
        response = json.loads(result.stdout)
        if response.get('stop_reason') != 'end_turn':
            raise ValueError('incomplete response')
        blocks = response['content']
        value = json.loads(''.join(b['text'] for b in blocks if b.get('type') == 'text'))
        return value, {'model': response.get('model'), 'usage': response.get('usage'),
                       'latency_seconds': time.monotonic() - started}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('invalid or incomplete AI response') from None
