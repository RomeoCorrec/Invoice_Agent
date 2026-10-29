"""What an approver needs to see next to an invoice to judge it: the orders and the copies.

The page already shows the figures, the anomalies and the explanation. Two of the most common
questions are answered by data that is not in the invoice itself: *which orders does this
supplier have, and how much of each is left?* and *where is the other copy of this invoice?*
Both are read from the database, never from the document, so nothing a document says can
change them.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from invoice_agent.db.repositories import InvoiceRepository, PurchaseOrderRepository
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.rules.duplicates import normalize_invoice_number

# A copy in one of these statuses is on the approvers' queue: its own page can be opened.
DECIDABLE = frozenset({InvoiceStatus.VALIDATED, InvoiceStatus.NEEDS_REVIEW})


@dataclass(frozen=True)
class OrderView:
    number: str
    order_date: date
    total_ht: Decimal
    billed_ht: Decimal
    remaining_ht: Decimal
    cited: bool
    """The invoice quotes this number."""
    matched: bool
    """The checks matched the invoice to this order."""


@dataclass(frozen=True)
class CopyView:
    invoice_id: UUID
    invoice_number: str
    status: InvoiceStatus
    received_at: datetime
    same_file: bool
    """The very same file, not only the same invoice number."""

    @property
    def decidable(self) -> bool:
        return self.status in DECIDABLE


@dataclass(frozen=True)
class DecisionContext:
    supplier_known: bool
    cited_ref: str | None
    """The order number written on the invoice, as read."""
    cited_found: bool
    """That number is one of the supplier's open orders."""
    orders: tuple[OrderView, ...]
    copies: tuple[CopyView, ...]


def decision_context(session: Session, invoice_id: UUID) -> DecisionContext | None:
    invoices = InvoiceRepository(session)
    invoice = invoices.get(invoice_id)
    if invoice is None:
        return None
    cited_ref = invoice.data.purchase_order_ref if invoice.data else None
    cited = normalize_invoice_number(cited_ref) if cited_ref else None

    orders: tuple[OrderView, ...] = ()
    if invoice.supplier_id is not None:
        open_orders = PurchaseOrderRepository(session).list_open_for_supplier(invoice.supplier_id)
        billed = invoices.billed_ht_by_purchase_order(
            [order.id for order in open_orders], excluding=invoice.id
        )
        orders = tuple(
            OrderView(
                number=order.number,
                order_date=order.order_date,
                total_ht=order.total_ht,
                billed_ht=billed.get(order.id, Decimal("0.00")),
                remaining_ht=order.total_ht - billed.get(order.id, Decimal("0.00")),
                cited=cited is not None and normalize_invoice_number(order.number) == cited,
                matched=order.id == invoice.purchase_order_id,
            )
            for order in sorted(open_orders, key=lambda o: o.number)
        )

    copies = tuple(
        CopyView(
            invoice_id=other.id,
            invoice_number=other.data.invoice_number if other.data else "?",
            status=other.status,
            received_at=other.received_at,
            same_file=other.source.sha256 == invoice.source.sha256,
        )
        for other in invoices.find_duplicates(invoice, supplier_id=invoice.supplier_id)
    )
    return DecisionContext(
        supplier_known=invoice.supplier_id is not None,
        cited_ref=cited_ref,
        cited_found=any(order.cited for order in orders),
        orders=orders,
        copies=copies,
    )
