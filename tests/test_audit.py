import json
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken

from security.audit import AuditMiddleware, audit, audit_logger, configure_audit_log

TOKEN = AccessToken(token='t', client_id='c', scopes=[], subject='alice-sub',
                    claims={'preferred_username': 'alice', 'azp': 'openwebui'})


@pytest.fixture
def records():
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(json.loads(record.getMessage()))

    handler = Capture()
    audit_logger.addHandler(handler)
    audit_logger.setLevel(logging.INFO)
    yield captured
    audit_logger.removeHandler(handler)


def test_event_includes_identity(records):
    with patch('security.audit.get_access_token', return_value=TOKEN):
        audit('es_search', index='idx')
    assert records[0] | {'ts': None} == {
        'ts': None, 'event': 'es_search', 'call_id': None, 'sub': 'alice-sub',
        'username': 'alice', 'client_id': 'openwebui', 'index': 'idx'}


async def test_middleware_records_success_and_failure_with_shared_call_id(records):
    mcp = FastMCP('t', middleware=[AuditMiddleware()])

    @mcp.tool
    def ok(x: int) -> int:
        audit('es_search', index='idx')
        return x

    @mcp.tool
    def boom() -> int:
        raise ToolError('nope')

    with patch('security.audit.get_access_token', return_value=TOKEN):
        async with Client(mcp) as client:
            await client.call_tool('ok', {'x': 1})
            with pytest.raises(Exception):
                await client.call_tool('boom', {})

    search, ok_call, boom_call = records
    assert search['event'] == 'es_search' and search['call_id'] == ok_call['call_id'] is not None
    assert ok_call | {'ts': None, 'call_id': None, 'duration_ms': None} == {
        'ts': None, 'call_id': None, 'duration_ms': None, 'event': 'tool_call', 'sub': 'alice-sub',
        'username': 'alice', 'client_id': 'openwebui', 'tool': 'ok', 'arguments': {'x': 1}, 'outcome': 'success'}
    assert boom_call['outcome'] == 'error' and boom_call['error'] == 'nope'


async def test_middleware_records_resource_reads(records):
    mcp = FastMCP('t', middleware=[AuditMiddleware()])

    @mcp.resource('data://{name}')
    def data(name: str) -> str:
        audit('es_search', index='idx')
        return name

    with patch('security.audit.get_access_token', return_value=TOKEN):
        async with Client(mcp) as client:
            await client.read_resource('data://x')

    search, read = records
    assert search['call_id'] == read['call_id'] is not None
    assert read['event'] == 'resource_read' and read['uri'] == 'data://x' and read['outcome'] == 'success'


@pytest.mark.skipif(sys.platform == 'win32', reason='Windows cannot rename a file that is open')
def test_audit_file_is_reopened_after_rotation(tmp_path):
    path = tmp_path / 'audit.jsonl'
    configure_audit_log(path)
    try:
        audit('tool_call', tool='before')
        path.rename(tmp_path / 'audit.jsonl.1')
        audit('tool_call', tool='after')
    finally:
        for handler in audit_logger.handlers:
            handler.close()
        audit_logger.handlers.clear()
    assert json.loads((tmp_path / 'audit.jsonl.1').read_text())['tool'] == 'before'
    assert json.loads(path.read_text())['tool'] == 'after'
