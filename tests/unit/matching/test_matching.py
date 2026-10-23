from decimal import Decimal
from uuid import UUID, uuid4

from invoice_agent.domain.invoice import Party
from invoice_agent.domain.purchase_order import PurchaseOrder, PurchaseOrderLine
from invoice_agent.domain.rules.anomaly import AnomalyCode
from invoice_agent.matching.purchase_order import match_purchase_order, remaining_ht
from invoice_agent.matching.supplier import resolve_supplier
from tests.factories import make_data, make_purchase_order, make_supplier


def order(supplier_id: UUID, number: str, total: str, **overrides: object) -> PurchaseOrder:
    line = PurchaseOrderLine(description="x", quantity="1", unit_price=total, amount_ht=total)
    return make_purchase_order(supplier_id, number=number, lines=(line,), **overrides)


def codes(anomalies: tuple[object, ...]) -> list[AnomalyCode]:
    return [a.code for a in anomalies]  # type: ignore[attr-defined]


# --- suppliers ---------------------------------------------------------------------------


def test_supplier_is_found_by_siren() -> None:
    mercier = make_supplier(siren="151757747", vat_number="FR67151757747")
    other = make_supplier(siren="419058185", vat_number="FR53419058185")
    assert resolve_supplier(Party(name="x", siren="419058185"), [mercier, other]) is other


def test_supplier_falls_back_to_vat_when_siren_is_wrong() -> None:
    """INV-19: altered SIREN, correct VAT number."""
    mercier = make_supplier(siren="151757747", vat_number="FR67151757747")
    seller = Party(name="Papeterie Mercier SARL", siren="151757748", vat_number="FR67151757747")
    assert resolve_supplier(seller, [mercier]) is mercier


def test_siren_wins_over_vat_when_they_disagree() -> None:
    a = make_supplier(siren="151757747", vat_number="FR11111111111")
    b = make_supplier(siren="419058185", vat_number="FR67151757747")
    seller = Party(name="x", siren="151757747", vat_number="FR67151757747")
    assert resolve_supplier(seller, [b, a]) is a


def test_name_alone_never_identifies_a_supplier() -> None:
    mercier = make_supplier(name="Papeterie Mercier SARL")
    assert resolve_supplier(Party(name="Papeterie Mercier SARL"), [mercier]) is None


def test_unknown_supplier() -> None:
    assert resolve_supplier(Party(name="x", siren="963203419"), [make_supplier()]) is None


# --- purchase orders ---------------------------------------------------------------------


def test_referenced_order_with_room_left_is_matched() -> None:
    supplier = make_supplier()
    po = order(supplier.id, "PO-1", "570.00")
    result = match_purchase_order(
        make_data(purchase_order_ref="PO-1"),
        supplier,
        referenced=po,
        open_orders=[po],
        billed_ht={},
    )
    assert result.order is po
    assert result.anomalies == ()


def test_unknown_reference_is_po_not_found_like_inv_17() -> None:
    result = match_purchase_order(
        make_data(purchase_order_ref="PO-404"),
        make_supplier(),
        referenced=None,
        open_orders=[],
        billed_ht={},
    )
    assert result.order is None
    assert codes(result.anomalies) == [AnomalyCode.PO_NOT_FOUND]


def test_reference_to_another_suppliers_order_counts_as_not_found() -> None:
    supplier = make_supplier()
    foreign = order(uuid4(), "PO-9", "570.00")
    result = match_purchase_order(
        make_data(purchase_order_ref="PO-9"),
        supplier,
        referenced=foreign,
        open_orders=[],
        billed_ht={},
    )
    assert result.order is None
    assert codes(result.anomalies) == [AnomalyCode.PO_NOT_FOUND]


def test_amount_above_the_remaining_budget_like_inv_16() -> None:
    supplier = make_supplier()
    po = order(supplier.id, "PO-1", "500.00")
    result = match_purchase_order(
        make_data(purchase_order_ref="PO-1"),
        supplier,
        referenced=po,
        open_orders=[po],
        billed_ht={},
    )
    assert result.order is po  # still linked, so the reviewer sees which order
    assert codes(result.anomalies) == [AnomalyCode.AMOUNT_EXCEEDS_PO]


def test_second_invoice_uses_what_is_left() -> None:
    supplier = make_supplier()
    po = order(supplier.id, "PO-1", "1000.00")
    billed = {po.id: Decimal("500.00")}
    assert remaining_ht(po, billed) == Decimal("500.00")
    result = match_purchase_order(
        make_data(purchase_order_ref="PO-1"),  # 570.00 HT
        supplier,
        referenced=po,
        open_orders=[po],
        billed_ht=billed,
    )
    assert codes(result.anomalies) == [AnomalyCode.AMOUNT_EXCEEDS_PO]


def test_fully_billed_order_reports_only_already_invoiced() -> None:
    """INV-14 / INV-21: nothing left, so no additional 'exceeds' on top."""
    supplier = make_supplier()
    po = order(supplier.id, "PO-1", "570.00")
    result = match_purchase_order(
        make_data(purchase_order_ref="PO-1"),
        supplier,
        referenced=po,
        open_orders=[po],
        billed_ht={po.id: Decimal("570.00")},
    )
    assert result.order is po
    assert codes(result.anomalies) == [AnomalyCode.PO_ALREADY_INVOICED]


def test_closed_order_cannot_be_invoiced() -> None:
    from invoice_agent.domain.purchase_order import PurchaseOrderStatus

    supplier = make_supplier()
    po = order(supplier.id, "PO-1", "570.00", status=PurchaseOrderStatus.CLOSED)
    result = match_purchase_order(
        make_data(purchase_order_ref="PO-1"), supplier, referenced=po, open_orders=[], billed_ht={}
    )
    assert codes(result.anomalies) == [AnomalyCode.PO_ALREADY_INVOICED]


def test_no_reference_matches_the_single_order_with_the_same_amount_like_inv_08() -> None:
    supplier = make_supplier()
    target = order(supplier.id, "PO-A", "570.00")
    others = [order(supplier.id, "PO-B", "300.00"), order(supplier.id, "PO-C", "1250.00")]
    result = match_purchase_order(
        make_data(purchase_order_ref=None),
        supplier,
        referenced=None,
        open_orders=[*others, target],
        billed_ht={},
    )
    assert result.order is target
    assert result.anomalies == ()


def test_no_reference_and_no_matching_amount() -> None:
    supplier = make_supplier()
    result = match_purchase_order(
        make_data(purchase_order_ref=None),
        supplier,
        referenced=None,
        open_orders=[order(supplier.id, "PO-B", "300.00")],
        billed_ht={},
    )
    assert result.order is None
    assert codes(result.anomalies) == [AnomalyCode.NO_PO_MATCH]


def test_ambiguous_amount_is_not_guessed() -> None:
    supplier = make_supplier()
    twins = [order(supplier.id, "PO-A", "570.00"), order(supplier.id, "PO-B", "570.00")]
    result = match_purchase_order(
        make_data(purchase_order_ref=None),
        supplier,
        referenced=None,
        open_orders=twins,
        billed_ht={},
    )
    assert result.order is None
    assert codes(result.anomalies) == [AnomalyCode.NO_PO_MATCH]


def test_heuristic_skips_orders_already_billed() -> None:
    supplier = make_supplier()
    used = order(supplier.id, "PO-A", "570.00")
    free = order(supplier.id, "PO-B", "570.00")
    result = match_purchase_order(
        make_data(purchase_order_ref=None),
        supplier,
        referenced=None,
        open_orders=[used, free],
        billed_ht={used.id: Decimal("570.00")},
    )
    assert result.order is free


def test_unknown_supplier_without_reference_matches_nothing() -> None:
    result = match_purchase_order(
        make_data(purchase_order_ref=None), None, referenced=None, open_orders=[], billed_ht={}
    )
    assert codes(result.anomalies) == [AnomalyCode.NO_PO_MATCH]


def test_unknown_seller_cannot_attach_itself_to_a_real_order() -> None:
    """Quoting a genuine order number must not link an unrecognised seller to it."""
    real = order(make_supplier().id, "PO-1", "570.00")
    result = match_purchase_order(
        make_data(purchase_order_ref="PO-1"), None, referenced=real, open_orders=[], billed_ht={}
    )
    assert result.order is None
    assert codes(result.anomalies) == [AnomalyCode.PO_NOT_FOUND]
