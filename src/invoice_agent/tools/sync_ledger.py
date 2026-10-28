"""Read-only lookups on the ledger for synchronous code.

The graph and the model's tool handlers are synchronous; the ledger client is not, and its
connection must be opened and closed by the same task. This keeps one connection for the whole
scope: a thread runs an event loop whose single session task opens the connection, serves the
calls it receives through a queue, and closes the connection when the scope ends. Calls carry
the OpenTelemetry context of their caller, so their spans stay in the caller's trace.

    with SyncLedgerReader(server) as reader:
        supplier = reader.get_supplier(siren="151757747")
"""

import asyncio
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from types import TracebackType
from typing import Any

from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer
from opentelemetry import context as otel_context

from invoice_agent.observability.tracing import Tracing
from invoice_agent.tools.errors import ToolTransientError
from invoice_agent.tools.ledger import CallPolicy, LedgerConnection, LedgerReader
from ledger.schemas import AccountInfo, BookedInvoice, PurchaseOrderInfo, SupplierInfo

JOIN_TIMEOUT_S = 30


class SyncLedgerReader:
    def __init__(
        self,
        server: MCPServer | StdioServerParameters,
        *,
        policy: CallPolicy | None = None,
        tracing: Tracing | None = None,
    ) -> None:
        self._server = server
        self._policy = policy or CallPolicy()
        self._tracing = tracing
        self._ready: Future[None] = Future()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[Any] | None = None
        self._thread: threading.Thread | None = None

    # --- scope ---------------------------------------------------------------------------

    def __enter__(self) -> "SyncLedgerReader":
        self._thread = threading.Thread(target=self._run, name="ledger-reader", daemon=True)
        self._thread.start()
        try:
            self._ready.result(timeout=self._policy.connect_timeout_s + 10)
        except BaseException:
            self._stop()
            raise
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._stop()

    def _post(self, item: Any) -> bool:
        """Hand an item to the session task. False if the session has already ended.

        When the ledger fails to start, the session task ends, and ``asyncio.run`` closes its
        loop, possibly before the caller learns of the failure: a closed loop is not an error
        here, it only means there is nobody left to tell.
        """
        if self._loop is None or self._queue is None or self._loop.is_closed():
            return False
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:  # the loop closed between the check and the call
            return False
        return True

    def _stop(self) -> None:
        self._post(None)
        if self._thread is not None:
            self._thread.join(timeout=JOIN_TIMEOUT_S)

    def _run(self) -> None:
        asyncio.run(self._session())

    async def _session(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        try:
            async with LedgerConnection(
                self._server, policy=self._policy, tracing=self._tracing
            ) as connection:
                reader = LedgerReader(connection)
                self._ready.set_result(None)
                while (item := await self._queue.get()) is not None:
                    method, arguments, context, future = item
                    token = otel_context.attach(context)
                    try:
                        future.set_result(await getattr(reader, method)(**arguments))
                    except Exception as exc:  # reported to the caller, the session goes on
                        future.set_exception(exc)
                    finally:
                        otel_context.detach(token)
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)

    # --- calls ---------------------------------------------------------------------------

    def _call(self, method: str, **arguments: Any) -> Any:
        if self._loop is None or self._queue is None or self._ready.exception() is not None:
            raise ToolTransientError("the ledger session is not open")
        future: Future[Any] = Future()
        context = otel_context.get_current()
        if not self._post((method, arguments, context, future)):
            raise ToolTransientError("the ledger session has ended")
        # A call may be retried by the client: allow every attempt, plus some slack.
        budget = self._policy.call_timeout_s * self._policy.attempts + 10
        try:
            return future.result(timeout=budget)
        except FutureTimeout as exc:
            raise ToolTransientError(f"{method} timed out") from exc

    def get_supplier(
        self, *, siren: str | None = None, vat_number: str | None = None
    ) -> SupplierInfo | None:
        result: SupplierInfo | None = self._call("get_supplier", siren=siren, vat_number=vat_number)
        return result

    def list_open_purchase_orders(self, supplier_code: str) -> list[PurchaseOrderInfo]:
        result: list[PurchaseOrderInfo] = self._call(
            "list_open_purchase_orders", supplier_code=supplier_code
        )
        return result

    def get_purchase_order(self, number: str) -> PurchaseOrderInfo | None:
        result: PurchaseOrderInfo | None = self._call("get_purchase_order", number=number)
        return result

    def find_invoices(
        self, supplier_siren: str, invoice_number: str | None = None
    ) -> list[BookedInvoice]:
        result: list[BookedInvoice] = self._call(
            "find_invoices", supplier_siren=supplier_siren, invoice_number=invoice_number
        )
        return result

    def get_chart_of_accounts(self, prefix: str = "") -> list[AccountInfo]:
        result: list[AccountInfo] = self._call("get_chart_of_accounts", prefix=prefix)
        return result
