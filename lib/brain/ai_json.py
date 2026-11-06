"""Bounded Messages API / subscription CLI transports, without implicit retries."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


class AIUnavailable(Exception):
    """Safe public code; optional bounded detail is private local evidence only."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.detail = detail


CLI_OUTPUT_BYTES = 4096
CLI_DETAIL_BYTES = 16384
CLI_FAILURE_STAGES = ('timeout', 'transport', 'nonzero_exit', 'provider_error',
                      'response_subtype', 'json_decode', 'response_object',
                      'structured_output_missing', 'structured_output_type',
                      'classification_schema')


def cli_detail(stage, started, result=None, error=None):
    """Retain exact raw byte tails, not a lossy decode, within a fixed size."""
    detail = {'transport': 'claude_cli', 'stage': stage,
              'latency_seconds': time.monotonic() - started,
              'returncode': getattr(result, 'returncode', None)}
    for name in ('stdout', 'stderr'):
        value = getattr(result, name, None) if result is not None else getattr(error, name, None)
        if value is None and name == 'stdout':
            value = getattr(error, 'output', None)
        raw = value if isinstance(value, bytes) else (value or '').encode('utf-8', 'backslashreplace')
        detail[name + '_tail_b64'] = base64.b64encode(raw[-CLI_OUTPUT_BYTES:]).decode('ascii')
        detail[name + '_bytes'] = len(raw)
        detail[name + '_truncated'] = len(raw) > CLI_OUTPUT_BYTES
    if error is not None:
        detail.update(error_type=type(error).__name__,
                      error_message=str(error).encode('utf-8', 'backslashreplace')[-2048:].decode('utf-8', 'ignore'))
        if isinstance(error, OSError):
            detail['errno'] = error.errno
        if isinstance(error, subprocess.TimeoutExpired):
            detail['timeout_seconds'] = error.timeout
    return detail


class _CLIUsage(dict):
    """Public usage stays unchanged; raw response survives only until validation."""

    def __init__(self, value, detail):
        super().__init__(value)
        self.detail = detail


def cli_schema_detail(usage, error):
    detail = dict(getattr(usage, 'detail', {}) or {})
    detail.update(transport='claude_cli', stage='classification_schema',
                  error_type=type(error).__name__,
                  error_message=str(error).encode('utf-8', 'backslashreplace')[-2048:].decode('utf-8', 'ignore'))
    return detail


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
    Only auth's home/user identity and executable lookup cross the environment
    boundary. macOS keychain lookup requires USER even when HOME is present.
    """
    env = {k: os.environ[k] for k in ('HOME', 'PATH', 'USER', 'LOGNAME') if k in os.environ}
    env.update(BRAIN_CAPTURE_CHILD='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    argv = ['claude', '-p', '--model', model, '--system-prompt', system,
            '--json-schema', json.dumps(schema), '--output-format', 'json', '--tools', '',
            '--no-session-persistence', '--setting-sources', '', '--safe-mode',
            '--settings', json.dumps({'disableAllHooks': True, 'autoMemoryEnabled': False}),
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--disable-slash-commands', '--no-chrome']
    started = time.monotonic()
    result = None
    try:
        # Do not honor a parent TMPDIR inside a project or the auth home.
        with tempfile.TemporaryDirectory(prefix='brain-capture-', dir='/tmp') as cwd:
            result = subprocess.run(argv, input=json.dumps(data, ensure_ascii=False).encode('utf-8'),
                                    cwd=cwd, env=env, capture_output=True,
                                    timeout=20, check=False)
    except subprocess.TimeoutExpired as error:
        raise AIUnavailable('timeout', cli_detail('timeout', started, error=error)) from None
    except OSError as error:
        raise AIUnavailable('transport', cli_detail('transport', started, result, error)) from None
    if result.returncode:
        raise AIUnavailable('provider_or_network', cli_detail('nonzero_exit', started, result))
    stage = 'json_decode'
    try:
        response = json.loads(result.stdout)
        stage = 'response_object'
        if not isinstance(response, dict):
            raise ValueError('CLI response must be an object')
        if response.get('is_error') or response.get('subtype') != 'success':
            detail = cli_detail('provider_error' if response.get('is_error') else 'response_subtype', started, result)
            detail['response_subtype'] = str(response.get('subtype')).encode('utf-8', 'backslashreplace')[-256:].decode('utf-8', 'ignore')
            raise AIUnavailable('provider_or_network', detail)
        stage = 'structured_output_missing'
        value = response['structured_output']
        stage = 'structured_output_type'
        if not isinstance(value, dict):
            raise ValueError('missing structured object')
        return value, _CLIUsage({'transport': 'claude_cli', 'model': model, 'usage': response.get('usage'),
                                'latency_seconds': time.monotonic() - started},
                               cli_detail('classification_schema', started, result))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise AIUnavailable('cli_invalid_response', cli_detail(stage, started, result, error)) from None
