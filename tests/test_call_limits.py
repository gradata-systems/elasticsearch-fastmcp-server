import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken

from utils.call_limits import CallLimitMiddleware


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def _server(clock, timeout=5.0):
    settings = SimpleNamespace(tool_timeout=timeout, max_repeated_calls=3, repeated_call_window_seconds=300)
    mcp = FastMCP('t', middleware=[CallLimitMiddleware(settings, clock)])
    calls = []

    @mcp.tool
    async def search(index: str, filters: dict | None = None) -> int:
        calls.append(index)
        return len(calls)

    @mcp.tool
    async def slow() -> int:
        await asyncio.sleep(1)
        return 1

    return mcp, calls


def _as(subject):
    return patch('utils.call_limits.get_access_token',
                 return_value=AccessToken(token='t', client_id='c', scopes=[], subject=subject))


async def test_identical_calls_are_refused_after_the_limit_until_the_window_passes():
    clock = Clock()
    mcp, calls = _server(clock)
    arguments = {'index': 'i', 'filters': {'b': 2, 'a': 1}}
    with _as('alice'), patch('utils.call_limits.audit') as audit:
        async with Client(mcp) as client:
            for _ in range(3):
                await client.call_tool('search', arguments)
            # Same arguments in another order are the same call.
            with pytest.raises(ToolError, match='already been made 3 times in the last 300 seconds'):
                await client.call_tool('search', {'filters': {'a': 1, 'b': 2}, 'index': 'i'})
            clock.now += 299
            with pytest.raises(ToolError, match='already been made'):
                await client.call_tool('search', arguments)
            clock.now += 1
            await client.call_tool('search', arguments)
    assert len(calls) == 4
    assert audit.call_args.args == ('call_refused',) and audit.call_args.kwargs['reason'] == 'repeated'


async def test_different_arguments_or_callers_are_counted_separately():
    mcp, calls = _server(Clock())
    async with Client(mcp) as client:
        for subject in ('alice', 'bob'):
            with _as(subject):
                for _ in range(3):
                    await client.call_tool('search', {'index': 'i'})
        with _as('alice'):
            await client.call_tool('search', {'index': 'j'})
    assert len(calls) == 7


async def test_calls_past_the_deadline_are_stopped():
    mcp, _ = _server(Clock(), timeout=0.05)
    with _as('alice'), patch('utils.call_limits.audit') as audit:
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match='stopped after 0.05 seconds'):
                await client.call_tool('slow', {})
    assert audit.call_args.kwargs['reason'] == 'timeout'
