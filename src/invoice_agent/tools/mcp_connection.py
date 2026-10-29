"""The connection to one of the project's MCP servers (the ledger, the mailroom).

``McpConnection`` owns the MCP session (a subprocess over stdio, or an in-process server in
tests), gives every call an explicit timeout, and repeats a call a bounded number of times with
exponential backoff when it failed in a way that can heal: a timeout, a dead connection, a
server crash. A refusal is never repeated. Repeating a write is safe because the write tools
of both servers are idempotent.

What differs from one server to the next is its name (used in traces and logs) and the codes of
its refusals; everything else is the same code, written once.

Use the connection as an async context manager, entering and leaving it from the same task.
"""

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Self

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer

from invoice_agent.observability.logging import log_event
from invoice_agent.observability.tracing import Tracing
from invoice_agent.tools.errors import ToolError, ToolRefusedError, ToolTransientError

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 200
INVALID_REQUEST = "invalid_request"


@dataclass(frozen=True)
class CallPolicy:
    attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0
    call_timeout_s: float = 10.0
    connect_timeout_s: float = 30.0


def coded_pattern(codes: Iterable[str]) -> re.Pattern[str]:
    return re.compile(
        r"\b(" + "|".join(re.escape(code) for code in codes) + r"):\s*(?P<text>[^\n]*)"
    )


def classify_failure(text: str, coded: re.Pattern[str]) -> ToolError:
    """Classify an error result. Only a short, argument-free message is kept."""
    found = coded.search(text)
    if found:
        message = found.group("text").split(" [type=")[0].strip()[:MAX_MESSAGE_CHARS]
        return ToolRefusedError(found.group(1), message)
    if "unknown tool" in text.lower():
        return ToolRefusedError("unknown_tool", "the server does not offer this tool")
    if "validation error" in text:
        return ToolRefusedError(INVALID_REQUEST, "arguments rejected by the tool")
    return ToolTransientError("the tool failed")


def unwrap(structured: dict[str, Any] | None) -> Any:
    """Tools that return a list or nothing are wrapped by the SDK as ``{"result": ...}``."""
    if structured is not None and set(structured) == {"result"}:
        return structured["result"]
    return structured


class McpConnection:
    def __init__(
        self,
        server: MCPServer | StdioServerParameters,
        *,
        service: str,
        codes: Iterable[str],
        policy: CallPolicy | None = None,
        tracing: Tracing | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._server = server
        self._service = service
        self._coded = coded_pattern(codes)
        self._policy = policy or CallPolicy()
        self._tracing = tracing or Tracing()
        self._sleep = sleep
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None

    async def __aenter__(self) -> Self:
        await self._open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:  # closing a dead session must not hide the real error
                log_event(
                    logger,
                    f"{self._service}_close_failed",
                    level=logging.WARNING,
                    error=type(exc).__name__,
                )

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Call a tool and return its result (``None`` for a null result)."""
        with self._tracing.observation(f"{self._service}.{tool}", input=arguments) as span:
            try:
                result = await self._call_with_retries(tool, arguments)
            except ToolError as exc:
                span.update(level="ERROR", status_message=type(exc).__name__)
                raise
            span.update(output=result)
            return result

    # --- internals -----------------------------------------------------------------------

    async def _open(self) -> Client:
        if self._client is not None:
            return self._client
        stack = AsyncExitStack()
        try:
            client = await asyncio.wait_for(
                stack.enter_async_context(Client(self._server)),
                timeout=self._policy.connect_timeout_s,
            )
        except Exception as exc:
            await stack.aclose()
            raise ToolTransientError(f"could not connect to the {self._service}") from exc
        self._stack, self._client = stack, client
        return client

    async def _call_with_retries(self, tool: str, arguments: dict[str, Any]) -> Any:
        policy = self._policy
        failure: ToolTransientError | None = None
        for attempt in range(1, policy.attempts + 1):
            try:
                return await self._call_once(tool, arguments)
            except ToolTransientError as exc:
                failure = exc
                await self.aclose()  # the next attempt opens a fresh session
                log_event(
                    logger,
                    f"{self._service}_call_failed",
                    level=logging.WARNING,
                    tool=tool,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < policy.attempts:
                    delay = min(policy.max_delay_s, policy.base_delay_s * 2 ** (attempt - 1))
                    await self._sleep(delay * (0.5 + random.random() / 2))
        assert failure is not None
        raise failure

    async def _call_once(self, tool: str, arguments: dict[str, Any]) -> Any:
        client = await self._open()
        timeout = self._policy.call_timeout_s
        try:
            result = await asyncio.wait_for(
                # the SDK's own read timeout is the backstop, ours fires first
                client.call_tool(tool, arguments, read_timeout_seconds=timeout + 1),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise ToolTransientError(f"{tool} timed out after {timeout:g}s") from exc
        except Exception as exc:
            raise ToolTransientError(f"{tool} failed: {type(exc).__name__}") from exc
        if result.is_error:
            text = " ".join(getattr(part, "text", "") for part in result.content)
            raise classify_failure(text, self._coded)
        return unwrap(result.structured_content)
