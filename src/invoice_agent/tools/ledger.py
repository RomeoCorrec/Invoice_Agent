"""Client of the ledger's MCP server.

``LedgerConnection`` owns the MCP session (a ledger subprocess over stdio, or an in-process
server in tests), gives every call an explicit timeout, and repeats a call a bounded number of
times with exponential backoff when it failed in a way that can heal: a timeout, a dead
connection, a server crash. A refusal is never repeated. Repeating a write is safe because
the ledger's write tool is idempotent.

The tools are split in two facades on purpose: ``LedgerReader`` for lookups, ``LedgerWriter``
for posting. A node that only explains anomalies is given a reader and has no way to write.

Use the connection as an async context manager, entering and leaving it from the same task.
"""

import asyncio
import logging
import random
import re
import sys
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer
from pydantic import TypeAdapter

from invoice_agent.observability.logging import log_event
from invoice_agent.observability.tracing import Tracing
from invoice_agent.tools.errors import ToolError, ToolRefusedError, ToolTransientError
from ledger.schemas import (
    AccountInfo,
    BookedInvoice,
    EntryRequest,
    ErrorCode,
    PostedEntry,
    PurchaseOrderInfo,
    SupplierInfo,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parents[3]
MAX_MESSAGE_CHARS = 200
_CODED = re.compile(
    r"\b(" + "|".join(re.escape(code.value) for code in ErrorCode) + r"):\s*(?P<text>[^\n]*)"
)


@dataclass(frozen=True)
class CallPolicy:
    attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0
    call_timeout_s: float = 10.0
    connect_timeout_s: float = 30.0


def ledger_stdio_server(database_url: str, *, read_only: bool = False) -> StdioServerParameters:
    """Parameters that start ``python -m ledger`` as a subprocess on the given database."""
    args = ["-m", "ledger", *(["--read-only"] if read_only else [])]
    return StdioServerParameters(
        command=sys.executable,
        args=args,
        env={"LEDGER_DATABASE_URL": database_url},
        cwd=REPO_ROOT,
    )


def refusal_or_failure(text: str) -> ToolError:
    """Classify an error result. Only a short, argument-free message is kept."""
    coded = _CODED.search(text)
    if coded:
        message = coded.group("text").split(" [type=")[0].strip()[:MAX_MESSAGE_CHARS]
        return ToolRefusedError(coded.group(1), message)
    if "unknown tool" in text.lower():
        return ToolRefusedError("unknown_tool", "the server does not offer this tool")
    if "validation error" in text:
        return ToolRefusedError(ErrorCode.INVALID_REQUEST.value, "arguments rejected by the tool")
    return ToolTransientError("the tool failed")


def unwrap(structured: dict[str, Any] | None) -> Any:
    """Tools that return a list or nothing are wrapped by the SDK as ``{"result": ...}``."""
    if structured is not None and set(structured) == {"result"}:
        return structured["result"]
    return structured


class LedgerConnection:
    def __init__(
        self,
        server: MCPServer | StdioServerParameters,
        *,
        policy: CallPolicy | None = None,
        tracing: Tracing | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._server = server
        self._policy = policy or CallPolicy()
        self._tracing = tracing or Tracing()
        self._sleep = sleep
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None

    async def __aenter__(self) -> "LedgerConnection":
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
                    logger, "ledger_close_failed", level=logging.WARNING, error=type(exc).__name__
                )

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Call a tool and return its result (``None`` for a null result)."""
        with self._tracing.observation(f"ledger.{tool}", input=arguments) as span:
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
            raise ToolTransientError("could not connect to the ledger") from exc
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
                    "ledger_call_failed",
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
            raise refusal_or_failure(text)
        return unwrap(result.structured_content)


class LedgerReader:
    """Lookups only. Give this, not the connection, to anything that must not write."""

    def __init__(self, connection: LedgerConnection) -> None:
        self._connection = connection

    async def get_supplier(
        self, *, siren: str | None = None, vat_number: str | None = None
    ) -> SupplierInfo | None:
        arguments = {k: v for k, v in {"siren": siren, "vat_number": vat_number}.items() if v}
        found = await self._connection.call("get_supplier", arguments)
        return SupplierInfo.model_validate(found) if found else None

    async def list_open_purchase_orders(self, supplier_code: str) -> list[PurchaseOrderInfo]:
        found = await self._connection.call(
            "list_open_purchase_orders", {"supplier_code": supplier_code}
        )
        return TypeAdapter(list[PurchaseOrderInfo]).validate_python(found)

    async def get_purchase_order(self, number: str) -> PurchaseOrderInfo | None:
        found = await self._connection.call("get_purchase_order", {"number": number})
        return PurchaseOrderInfo.model_validate(found) if found else None

    async def find_invoices(
        self, supplier_siren: str, invoice_number: str | None = None
    ) -> list[BookedInvoice]:
        arguments = {"supplier_siren": supplier_siren}
        if invoice_number is not None:
            arguments["invoice_number"] = invoice_number
        found = await self._connection.call("find_invoices", arguments)
        return TypeAdapter(list[BookedInvoice]).validate_python(found)

    async def get_chart_of_accounts(self, prefix: str = "") -> list[AccountInfo]:
        found = await self._connection.call("get_chart_of_accounts", {"prefix": prefix})
        return TypeAdapter(list[AccountInfo]).validate_python(found)


class LedgerWriter:
    """The only way to post. It is given to the posting node and to nothing else."""

    def __init__(self, connection: LedgerConnection) -> None:
        self._connection = connection

    async def post_entry(self, entry: EntryRequest) -> PostedEntry:
        found = await self._connection.call(
            "post_journal_entry", {"entry": entry.model_dump(mode="json")}
        )
        return PostedEntry.model_validate(found)
