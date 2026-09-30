"""Limits on tool calls as a whole: how long one may run, and how often the same call may repeat.

Some clients, or the models behind them, get stuck calling one tool with the same arguments over
and over. Each repeat costs Elasticsearch a query and returns what the caller already has, so after
a few identical calls in a short time further ones are refused with an explanation.
"""
import asyncio
import json
import time
from collections import deque

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from config import Settings
from security.audit import audit

# Forget calls not seen for a whole window once this many different calls are remembered.
_PRUNE_ABOVE = 1000


class CallLimitMiddleware(Middleware):
    def __init__(self, settings: Settings, clock=time.monotonic):
        self._timeout = settings.tool_timeout
        self._max_repeats = settings.max_repeated_calls
        self._window = settings.repeated_call_window_seconds
        self._clock = clock
        self._calls: dict[tuple[str, str, str], deque[float]] = {}

    def _allow(self, key: tuple[str, str, str]) -> bool:
        """Whether a call to `key` may run, recording it if so. Refused calls aren't recorded, so a caller
        stuck in a loop gets a few more attempts once each window passes, but no more."""
        now = self._clock()
        if len(self._calls) > _PRUNE_ABOVE:
            self._calls = {k: times for k, times in self._calls.items() if times and now - times[-1] < self._window}
        times = self._calls.setdefault(key, deque())
        while times and now - times[0] >= self._window:
            times.popleft()
        if len(times) >= self._max_repeats:
            return False
        times.append(now)
        return True

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool, arguments = context.message.name, context.message.arguments or {}
        token = get_access_token()
        key = (token.subject if token else '', tool, json.dumps(arguments, sort_keys=True, default=str))
        if not self._allow(key):
            audit('call_refused', reason='repeated', tool=tool, arguments=arguments)
            raise ToolError(
                f"This exact call has already been made {self._max_repeats} times in the last {self._window} "
                f"seconds, and repeating it returns the same result. Use the earlier result, or change the "
                f"arguments.")
        try:
            async with asyncio.timeout(self._timeout):
                return await call_next(context)
        except TimeoutError:
            audit('call_refused', reason='timeout', tool=tool, arguments=arguments)
            raise ToolError(f"The call was stopped after {self._timeout:g} seconds; narrow the time range, add "
                            f"filters or aggregate, rather than trying it again unchanged.") from None
