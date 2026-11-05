"""Bounded Messages API / subscription CLI transports, without implicit retries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


class AIUnavailable(Exception):
    """A safe diagnostic code, never provider response text or credentials."""


class AIResponseInvalid(ValueError):
    """The provider answered but its response cannot establish classification."""


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
        return cli_json(system, data, schema, model)
    request = json_request(system, data, schema, model)
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable, '-I', '-c', _REQUEST], input=json.dumps(request),
                                cwd=Path(__file__).resolve().parent,
                                env={'ANTHROPIC_API_KEY': os.environ['ANTHROPIC_API_KEY']},
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
        raise AIResponseInvalid('invalid or incomplete AI response') from None


def cli_json(system, data, schema, model):
    """Keep subscription auth, but load no parent session or customization.

    Safe mode preserves OAuth/keychain access (unlike --bare). Empty setting
    sources, explicit settings and MCP configuration also fence customization.
    Only auth's home and executable lookup cross the parent environment boundary.
    """
    env = {k: os.environ[k] for k in ('HOME', 'PATH') if k in os.environ}
    env.update(BRAIN_CAPTURE_CHILD='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    argv = ['claude', '-p', '--model', model, '--system-prompt', system,
            '--json-schema', json.dumps(schema), '--output-format', 'json', '--tools', '',
            '--no-session-persistence', '--setting-sources', '', '--safe-mode',
            '--settings', json.dumps({'disableAllHooks': True, 'autoMemoryEnabled': False}),
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--disable-slash-commands', '--no-chrome']
    started = time.monotonic()
    try:
        # Do not honor a parent TMPDIR inside a project or the auth home.
        with tempfile.TemporaryDirectory(prefix='brain-capture-', dir='/tmp') as cwd:
            result = subprocess.run(argv, input=json.dumps(data, ensure_ascii=False),
                                    cwd=cwd, env=env, text=True, capture_output=True,
                                    timeout=20, check=False)
    except subprocess.TimeoutExpired:
        raise AIUnavailable('timeout') from None
    except OSError:
        raise AIUnavailable('transport') from None
    if result.returncode:
        raise AIUnavailable('provider_or_network')
    try:
        response = json.loads(result.stdout)
        if response.get('is_error') or response.get('subtype') != 'success':
            raise AIUnavailable('provider_or_network')
        value = response['structured_output']
        if not isinstance(value, dict):
            raise ValueError('missing structured object')
        return value, {'transport': 'claude_cli', 'model': model, 'usage': response.get('usage'),
                       'latency_seconds': time.monotonic() - started}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AIUnavailable('cli_invalid_response') from None
