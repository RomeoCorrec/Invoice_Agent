from decimal import Decimal

from invoice_agent.agent.checks import ValidationContext, run_checks
from invoice_agent.domain.invoice import Party
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.purchase_order import PurchaseOrder, PurchaseOrderLine
from invoice_agent.domain.rules.anomaly import AnomalyCode
from tests.factories import make_data, make_invoice, make_purchase_order, make_supplier

SELLER = Party(name="Café des Ateliers SARL", siren="850223454", vat_number="FR84850223454")


def clean_context() -> tuple[ValidationContext, PurchaseOrder]:
    supplier = make_supplier(siren="850223454", vat_number="FR84850223454", iban=None)
    line = PurchaseOrderLine(description="x", quantity="1", unit_price="570.00", amount_ht="570.00")
    order = make_purchase_order(supplier.id, number="PO-2026-0105", lines=(line,))
    context = ValidationContext(
        supplier_candidates=(supplier,), referenced_order=order, open_orders=(order,)
    )
    return context, order


def test_clean_invoice_is_validated_and_linked() -> None:
    context, order = clean_context()
    result = run_checks(make_data(seller=SELLER, iban=None), context)
    assert result.anomalies == ()
    assert result.status is InvoiceStatus.VALIDATED
    assert result.supplier_id == context.supplier_candidates[0].id
    assert result.purchase_order_id == order.id


def test_unknown_supplier_without_reference_like_inv_15() -> None:
    result = run_checks(make_data(purchase_order_ref=None), ValidationContext())
    assert [a.code for a in result.anomalies] == [
        AnomalyCode.UNKNOWN_SUPPLIER,
        AnomalyCode.NO_PO_MATCH,
    ]
    assert result.status is InvoiceStatus.NEEDS_REVIEW
    assert result.supplier_id is None
    assert result.purchase_order_id is None


def test_duplicate_is_reported_and_anomalies_are_in_stable_order() -> None:
    context, order = clean_context()
    context = ValidationContext(
        supplier_candidates=context.supplier_candidates,
        referenced_order=order,
        open_orders=(order,),
        billed_ht={order.id: Decimal("570.00")},
        duplicates=(make_invoice(),),
    )
    result = run_checks(make_data(seller=SELLER, iban=None), context)
    assert [a.code for a in result.anomalies] == [
        AnomalyCode.DUPLICATE_INVOICE,
        AnomalyCode.PO_ALREADY_INVOICED,
    ]
    assert result.purchase_order_id == order.id


def test_same_inputs_give_the_same_result() -> None:
    context, _ = clean_context()
    data = make_data(seller=SELLER, total_ttc="1.00")
    assert run_checks(data, context) == run_checks(data, context)
