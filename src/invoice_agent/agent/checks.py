"""Run every deterministic check on one extracted invoice.

Pure: all the data the checks need is handed in through ``ValidationContext``, loaded
beforehand by ``agent.validation``. Same inputs always give the same anomalies.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from invoice_agent.domain.invoice import Invoice, InvoiceData
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.purchase_order import PurchaseOrder
from invoice_agent.domain.rules.anomaly import (
    Anomaly,
    AnomalyCode,
    recommended_status,
    sort_anomalies,
)
from invoice_agent.domain.rules.arithmetic import check_totals, check_vat
from invoice_agent.domain.rules.identifiers import check_iban, check_siren
from invoice_agent.domain.rules.injection import check_injection
from invoice_agent.domain.supplier import Supplier
from invoice_agent.matching.purchase_order import match_purchase_order
from invoice_agent.matching.supplier import resolve_supplier


@dataclass(frozen=True)
class ValidationContext:
    supplier_candidates: tuple[Supplier, ...] = ()
    """Suppliers whose SIREN or VAT number appears on the invoice."""
    referenced_order: PurchaseOrder | None = None
    """The order whose number the invoice quotes, if it exists."""
    open_orders: tuple[PurchaseOrder, ...] = ()
    """Open orders of the resolved supplier (for matching without a reference)."""
    billed_ht: Mapping[UUID, Decimal] = field(default_factory=dict)
    """HT already invoiced per order by other invoices."""
    earlier_duplicates: tuple[Invoice, ...] = ()
    """Earlier invoices with the same supplier and number, or the same source file."""


@dataclass(frozen=True)
class ValidationResult:
    anomalies: tuple[Anomaly, ...]
    supplier_id: UUID | None
    purchase_order_id: UUID | None

    @property
    def status(self) -> InvoiceStatus:
        return recommended_status(self.anomalies)


def run_checks(data: InvoiceData, context: ValidationContext) -> ValidationResult:
    supplier = resolve_supplier(data.seller, context.supplier_candidates)
    anomalies: list[Anomaly] = []

    if supplier is None:
        anomalies.append(
            Anomaly(
                code=AnomalyCode.UNKNOWN_SUPPLIER,
                message=f"seller {data.seller.name!r} is not in the supplier reference data",
                evidence={"seller_siren": data.seller.siren, "seller_vat": data.seller.vat_number},
            )
        )
    anomalies += check_siren(data, supplier)
    anomalies += check_iban(data, supplier)
    anomalies += check_vat(data)
    anomalies += check_totals(data)
    anomalies += check_injection(data)

    if context.earlier_duplicates:
        anomalies.append(
            Anomaly(
                code=AnomalyCode.DUPLICATE_INVOICE,
                message=f"invoice {data.invoice_number} was already received",
                evidence={"earlier_invoice_ids": [str(i.id) for i in context.earlier_duplicates]},
            )
        )

    match = match_purchase_order(
        data,
        supplier,
        referenced=context.referenced_order,
        open_orders=context.open_orders,
        billed_ht=context.billed_ht,
    )
    anomalies += match.anomalies

    return ValidationResult(
        anomalies=sort_anomalies(anomalies),
        supplier_id=supplier.id if supplier else None,
        purchase_order_id=match.order.id if match.order else None,
    )
