import json

import pytest

from evals.context import load_reference
from evals.memory_ledger import MemoryLedger
from invoice_agent.agent.graph.explain_node import read_only_tools
from invoice_agent.tools.errors import ToolRefusedError
from tests.factories import IBAN_KEY


@pytest.fixture(scope="module")
def ledger() -> MemoryLedger:
    return MemoryLedger(load_reference(IBAN_KEY))


def test_a_supplier_is_found_by_siren_or_vat_number_with_a_masked_iban(
    ledger: MemoryLedger,
) -> None:
    supplier = ledger.get_supplier(siren="151757747")
    assert supplier is not None and supplier.code == "SUP-001"
    assert supplier.iban_masked and "FR7699053965263585803695442" not in supplier.model_dump_json()
    assert ledger.get_supplier(vat_number=supplier.vat_number) == supplier
    assert ledger.get_supplier(siren="000000000") is None


def test_a_lookup_without_a_key_is_refused_like_the_ledger_does(ledger: MemoryLedger) -> None:
    with pytest.raises(ToolRefusedError) as refusal:
        ledger.get_supplier()
    assert refusal.value.code == "invalid_request"


def test_open_purchase_orders_and_one_order_by_number(ledger: MemoryLedger) -> None:
    orders = ledger.list_open_purchase_orders("SUP-001")
    assert orders and all(order.status == "open" for order in orders)
    assert ledger.get_purchase_order(orders[0].number) == orders[0]
    assert ledger.get_purchase_order("PO-NOPE") is None
    assert ledger.list_open_purchase_orders("SUP-999") == []


def test_there_is_no_history_in_a_single_document_evaluation(ledger: MemoryLedger) -> None:
    assert ledger.find_invoices("151757747") == []


def test_the_production_tools_run_on_it(ledger: MemoryLedger) -> None:
    tools = {tool.name: tool for tool in read_only_tools(ledger)}
    assert set(tools) == {
        "get_supplier",
        "list_open_purchase_orders",
        "get_purchase_order",
        "find_invoices",
    }
    found = json.loads(tools["get_supplier"].handler({"siren": "151757747"}))
    assert found["code"] == "SUP-001"
    assert tools["get_supplier"].handler({"siren": "000000000"}) == "null"
    orders = json.loads(tools["list_open_purchase_orders"].handler({"supplier_code": "SUP-001"}))
    assert orders[0]["number"] == "PO-2026-0101"
