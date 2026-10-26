"""The native T3 ACP tool bridge; no shell, service install or implicit retries."""
import json
import os
from pathlib import Path
import re
import subprocess


class HostRPCError(RuntimeError):
    """Unavailable transport, rejected operation or unknown delivery outcome."""


def call(tool_name, arguments):
    """Call the app-owned bridge and decode its JSON tool result.

    Callers own authorization, revalidation and durable action receipts. A
    timeout is an unknown outcome, never permission to repeat a mutation.
    """
    prefix = 'mcp__t3_code__'
    if isinstance(tool_name, str) and tool_name.startswith(prefix):
        tool_name = tool_name[len(prefix):]
    if not isinstance(tool_name, str) or not re.fullmatch('[a-z][a-z0-9_]*', tool_name):
        raise HostRPCError('invalid native host tool name')
    if not isinstance(arguments, dict):
        raise HostRPCError('native host arguments must be an object')
    if tool_name == 't3_thread_organize':
        if set(arguments) - {'threadId', 'action', 'snoozedUntil'}:
            raise HostRPCError('thread organization requires singular threadId; unknown arguments refused')
        if not isinstance(arguments.get('threadId'), str) or not arguments['threadId'].strip():
            raise HostRPCError('thread organization requires an explicit singular threadId')
    if tool_name == 't3_thread_organize' and arguments.get('action') == 'settle':
        # This native schema has no expected revision/run/descendant predicate.
        # Read-before/write-after checks cannot make this mutation atomic.
        raise HostRPCError('native atomic settlement unsupported; retain thread until host supplies a conditional mutation')
    try:
        payload = json.dumps(arguments, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError):
        raise HostRPCError('native host arguments are not finite JSON') from None
    node = os.environ.get('T3_ACP_MCP_NODE')
    entry = os.environ.get('T3_ACP_MCP_ENTRYPOINT')
    if not node or not Path(node).is_absolute() or not Path(node).is_file():
        raise HostRPCError('native T3 ACP bridge unavailable: T3_ACP_MCP_NODE must name its executable')
    if entry and (not Path(entry).is_absolute() or not Path(entry).is_file()):
        raise HostRPCError('native T3 ACP bridge entrypoint unavailable')
    command = [node, *([entry] if entry else []), 'acp-mcp-call', tool_name, payload]
    try:
        result = subprocess.run(command, env={**os.environ, 'ELECTRON_RUN_AS_NODE': '1'},
                                capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise HostRPCError(f'{tool_name}: native host timeout; delivery outcome unknown, do not blindly retry') from None
    except OSError:
        raise HostRPCError(f'{tool_name}: native host bridge could not start') from None
    if result.returncode:
        # stdout/stderr can contain capabilities or user content; never echo it.
        raise HostRPCError(f'{tool_name}: native host bridge exited {result.returncode}; delivery outcome unknown')
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise HostRPCError(f'{tool_name}: native host returned invalid JSON; delivery outcome unknown') from None
    if isinstance(value, dict) and value.get('jsonrpc') == '2.0':
        if 'error' in value or 'result' not in value:
            raise HostRPCError(f'{tool_name}: native host RPC rejected the request')
        value = value['result']
    if isinstance(value, dict):
        if value.get('isError'):
            raise HostRPCError(f'{tool_name}: native host tool rejected the request')
        if 'structuredContent' in value:
            return value['structuredContent']
        blocks = value.get('content')
        if isinstance(blocks, list) and len(blocks) == 1 and isinstance(blocks[0], dict) and blocks[0].get('type') == 'text':
            try:
                return json.loads(blocks[0].get('text', ''))
            except (TypeError, ValueError):
                pass  # unstructured success remains opaque; the caller must validate it
    return value
