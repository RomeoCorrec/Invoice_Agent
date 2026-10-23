"""Purchase-order matching and the billing checks that depend on it."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.domain.purchase_order import PurchaseOrder, PurchaseOrderStatus
from invoice_agent.domain.rules.anomaly import Anomaly, AnomalyCode
from invoice_agent.domain.supplier import Supplier

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class PurchaseOrderMatch:
    order: PurchaseOrder | None
    anomalies: tuple[Anomaly, ...]


def remaining_ht(order: PurchaseOrder, billed_ht: Mapping[UUID, Decimal]) -> Decimal:
    return order.total_ht - billed_ht.get(order.id, ZERO)


def match_purchase_order(
    data: InvoiceData,
    supplier: Supplier | None,
    *,
    referenced: PurchaseOrder | None,
    open_orders: Sequence[PurchaseOrder],
    billed_ht: Mapping[UUID, Decimal],
) -> PurchaseOrderMatch:
    """Find the order the invoice belongs to, then check it can still absorb the invoice.

    ``referenced`` is the order whose number the invoice quotes (None if it does not exist),
    ``open_orders`` the open orders of the resolved supplier, ``billed_ht`` the HT already
    invoiced per order by other invoices.
    """
    if data.purchase_order_ref:
        order = _accept_referenced(referenced, supplier)
        if order is None:
            return PurchaseOrderMatch(
                None,
                (
                    Anomaly(
                        code=AnomalyCode.PO_NOT_FOUND,
                        message=(
                            f"purchase order {data.purchase_order_ref} does not exist "
                            "for this supplier"
                        ),
                        evidence={"purchase_order_ref": data.purchase_order_ref},
                    ),
                ),
            )
    else:
        order = _guess_from_amount(data, open_orders, billed_ht) if supplier else None
        if order is None:
            return PurchaseOrderMatch(
                None,
                (
                    Anomaly(
                        code=AnomalyCode.NO_PO_MATCH,
                        message="no purchase order reference and no single order with this amount",
                        evidence={"total_ht": str(data.total_ht)},
                    ),
                ),
            )
    return PurchaseOrderMatch(order, tuple(_billing_anomalies(data, order, billed_ht)))


def _accept_referenced(
    order: PurchaseOrder | None, supplier: Supplier | None
) -> PurchaseOrder | None:
    """Link a quoted order only if it belongs to the recognised supplier.

    Fails closed: an unknown seller cannot attach itself to a real supplier's order just
    by quoting its number, and an order of another supplier is treated as missing.
    """
    if order is None or supplier is None or order.supplier_id != supplier.id:
        return None
    return order


def _guess_from_amount(
    data: InvoiceData, open_orders: Sequence[PurchaseOrder], billed_ht: Mapping[UUID, Decimal]
) -> PurchaseOrder | None:
    """Exactly one open, not yet billed order of the supplier with the same HT total.

    Zero or several candidates give None: we never pick one at random.
    """
    candidates = [
        order
        for order in open_orders
        if order.total_ht == data.total_ht and remaining_ht(order, billed_ht) > 0
    ]
    return candidates[0] if len(candidates) == 1 else None


def _billing_anomalies(
    data: InvoiceData, order: PurchaseOrder, billed_ht: Mapping[UUID, Decimal]
) -> list[Anomaly]:
    """Already invoiced and exceeds are exclusive: nothing left is reported once, not twice."""
    remaining = remaining_ht(order, billed_ht)
    evidence = {
        "purchase_order": order.number,
        "order_total_ht": str(order.total_ht),
        "already_billed_ht": str(billed_ht.get(order.id, ZERO)),
        "invoice_total_ht": str(data.total_ht),
    }
    if order.status is not PurchaseOrderStatus.OPEN or remaining <= 0:
        return [
            Anomaly(
                code=AnomalyCode.PO_ALREADY_INVOICED,
                message=f"purchase order {order.number} has nothing left to invoice",
                evidence={**evidence, "status": order.status.value},
            )
        ]
    if data.total_ht > remaining:
        return [
            Anomaly(
                code=AnomalyCode.AMOUNT_EXCEEDS_PO,
                message=(
                    f"invoice HT {data.total_ht} exceeds what is left on "
                    f"{order.number} ({remaining})"
                ),
                evidence={**evidence, "remaining_ht": str(remaining)},
            )
        ]
    return []
