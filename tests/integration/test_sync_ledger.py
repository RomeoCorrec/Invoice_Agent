"""Synchronous lookups on the ledger, as the model's tool handlers use them."""

import sys
import threading
from collections.abc import Iterator
from uuid import uuid4

import pytest
from langfuse import Langfuse
from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from invoice_agent.observability.tracing import Tracing, mask
from invoice_agent.tools.errors import ToolRefusedError, ToolTransientError
from invoice_agent.tools.ledger import CallPolicy
from invoice_agent.tools.sync_ledger import SyncLedgerReader


def reader_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "ledger-reader"]


def test_lookups_return_typed_models(ledger_server: MCPServer) -> None:
    with SyncLedgerReader(ledger_server) as reader:
        supplier = reader.get_supplier(siren="151757747")
        assert supplier is not None and supplier.supplier_account == "401MERC"
        assert reader.get_supplier(siren="000000000") is None
        [first, *_] = reader.list_open_purchase_orders("SUP-001")
        assert first.number == "PO-2026-0101"
        assert reader.get_purchase_order("PO-NOPE") is None
        assert reader.find_invoices("151757747") == []
        assert "44566" in [a.number for a in reader.get_chart_of_accounts("44")]


def test_the_session_ends_with_the_scope(ledger_server: MCPServer) -> None:
    with SyncLedgerReader(ledger_server) as reader:
        reader.get_supplier(siren="151757747")
        assert len(reader_threads()) == 1
    assert reader_threads() == []


def test_a_refusal_of_the_ledger_reaches_the_caller(ledger_server: MCPServer) -> None:
    with SyncLedgerReader(ledger_server) as reader:
        with pytest.raises(ToolRefusedError) as refusal:
            reader.get_supplier()  # neither a SIREN nor a VAT number
        assert refusal.value.code == "invalid_request"
        assert reader.get_supplier(siren="151757747") is not None  # the session is still usable


def test_a_ledger_that_cannot_start_fails_the_scope_without_hanging() -> None:
    broken = StdioServerParameters(command=sys.executable, args=["-c", "import sys; sys.exit(3)"])
    policy = CallPolicy(attempts=1, connect_timeout_s=10, call_timeout_s=5)
    with pytest.raises(ToolTransientError), SyncLedgerReader(broken, policy=policy):
        pass
    assert reader_threads() == []


@pytest.fixture
def traced() -> Iterator[tuple[Tracing, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-test-{uuid4().hex}",
        secret_key="sk-test",
        base_url="http://127.0.0.1:9",
        span_exporter=exporter,
        mask=mask,
    )
    yield Tracing(client), exporter
    client.shutdown()


def test_a_lookup_stays_in_the_trace_of_its_caller(
    ledger_server: MCPServer, traced: tuple[Tracing, InMemorySpanExporter]
) -> None:
    tracing, exporter = traced
    with (
        SyncLedgerReader(ledger_server, tracing=tracing) as reader,
        tracing.observation("explain", as_type="generation"),
    ):
        reader.get_supplier(siren="151757747")
    tracing.flush()
    spans = {s.name: s for s in exporter.get_finished_spans()}
    call, parent = spans["tool.get_supplier"], spans["explain"]
    assert call.context.trace_id == parent.context.trace_id
    assert call.parent is not None and call.parent.span_id == parent.context.span_id
