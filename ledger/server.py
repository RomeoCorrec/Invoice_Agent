"""The ledger as an MCP server: five read tools and one write tool.

A server built with ``writable=False`` has no write tool at all, so a client that only needs to
look things up can be given a server that cannot change anything. Refusals the ledger
anticipated become tool errors whose message starts with a stable code; anything else is a
crash the SDK reports without detail, so no internals reach the client.
"""

import logging
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from invoice_agent.domain.types import NonEmptyStr, Siren
from invoice_agent.observability.logging import log_event
from ledger.schemas import (
    AccountInfo,
    BookedInvoice,
    EntryRequest,
    LedgerRefusal,
    PostedEntry,
    PurchaseOrderInfo,
    SupplierInfo,
)
from ledger.store import LedgerStore

logger = logging.getLogger("ledger.server")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def _call[T](tool: str, action: Callable[[], T], **fields: Any) -> T:
    """Run a store call, turning an anticipated refusal into a tool error and logging both."""
    try:
        result = action()
    except LedgerRefusal as refusal:
        log_event(logger, "tool_refused", tool=tool, code=refusal.code.value, **fields)
        raise ToolError(str(refusal)) from None
    log_event(logger, "tool_called", tool=tool, **fields)
    return result


def build_server(store: LedgerStore, *, writable: bool = True) -> MCPServer:
    server = MCPServer(
        "ledger",
        instructions=(
            "Mock accounting system for supplier invoices. Reading tools look up suppliers, "
            "purchase orders, booked invoices and the chart of accounts. Posting an entry "
            "requires an idempotency key; posting twice with the same key writes once."
        ),
    )

    @server.tool(annotations=READ_ONLY)
    def get_supplier(
        siren: Siren | None = None, vat_number: NonEmptyStr | None = None
    ) -> SupplierInfo | None:
        """Find a supplier by SIREN or by VAT number. Returns null when there is none."""
        return _call("get_supplier", lambda: store.get_supplier(siren=siren, vat_number=vat_number))

    @server.tool(annotations=READ_ONLY)
    def list_open_purchase_orders(supplier_code: NonEmptyStr) -> list[PurchaseOrderInfo]:
        """List the open purchase orders of a supplier, oldest first, with their lines."""
        return _call(
            "list_open_purchase_orders",
            lambda: store.list_open_purchase_orders(supplier_code),
            supplier_code=supplier_code,
        )

    @server.tool(annotations=READ_ONLY)
    def get_purchase_order(number: NonEmptyStr) -> PurchaseOrderInfo | None:
        """Get one purchase order by its number. Returns null when it does not exist."""
        return _call("get_purchase_order", lambda: store.get_purchase_order(number), number=number)

    @server.tool(annotations=READ_ONLY)
    def find_invoices(
        supplier_siren: Siren, invoice_number: NonEmptyStr | None = None
    ) -> list[BookedInvoice]:
        """List the invoices already booked for a supplier, optionally one invoice number."""
        return _call("find_invoices", lambda: store.find_invoices(supplier_siren, invoice_number))

    @server.tool(annotations=READ_ONLY)
    def get_chart_of_accounts(prefix: str = "") -> list[AccountInfo]:
        """List the accounts of the chart, optionally those whose number starts with a prefix."""
        return _call("get_chart_of_accounts", lambda: store.get_chart_of_accounts(prefix))

    if writable:

        @server.tool(annotations=IDEMPOTENT_WRITE)
        def post_journal_entry(entry: EntryRequest) -> PostedEntry:
            """Post one balanced entry for a supplier invoice.

            Posting again with the same idempotency key and the same content returns the
            entry already written (replayed=true). The same key with different content, or a
            second entry for an invoice already booked, is refused.
            """
            return _call(
                "post_journal_entry",
                lambda: store.post_entry(entry),
                idempotency_key=entry.idempotency_key,
            )

    return server
