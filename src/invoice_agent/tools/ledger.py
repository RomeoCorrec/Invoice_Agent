"""Client of the ledger's MCP server.

``LedgerConnection`` is the generic connection (``mcp_connection``) for the ledger: a subprocess
over stdio, or an in-process server in tests, with explicit timeouts, bounded retries for what
can heal, and no retry for a refusal. Repeating a write is safe because the ledger's write tool
is idempotent.

The tools are split in two facades on purpose: ``LedgerReader`` for lookups, ``LedgerWriter``
for posting. A node that only explains anomalies is given a reader and has no way to write.

Use the connection as an async context manager, entering and leaving it from the same task.
"""

import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer
from pydantic import TypeAdapter

from invoice_agent.observability.tracing import Tracing
from invoice_agent.tools.errors import ToolError
from invoice_agent.tools.mcp_connection import (
    MAX_MESSAGE_CHARS,
    CallPolicy,
    McpConnection,
    classify_failure,
    coded_pattern,
    unwrap,
)
from ledger.schemas import (
    AccountInfo,
    BookedInvoice,
    EntryRequest,
    ErrorCode,
    PostedEntry,
    PurchaseOrderInfo,
    SupplierInfo,
)

__all__ = [
    "MAX_MESSAGE_CHARS",
    "CallPolicy",
    "LedgerConnection",
    "LedgerReader",
    "LedgerWriter",
    "ledger_stdio_server",
    "refusal_or_failure",
    "unwrap",
]

REPO_ROOT = Path(__file__).parents[3]
_CODED = coded_pattern(code.value for code in ErrorCode)


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
    """Classify an error result of the ledger. Only a short, argument-free message is kept."""
    return classify_failure(text, _CODED)


class LedgerConnection(McpConnection):
    def __init__(
        self,
        server: MCPServer | StdioServerParameters,
        *,
        policy: CallPolicy | None = None,
        tracing: Tracing | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        options: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
        super().__init__(
            server,
            service="ledger",
            codes=(code.value for code in ErrorCode),
            policy=policy,
            tracing=tracing,
            **options,
        )


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
