"""Invoice aggregate.

These models check *structure* (types, bounds, internal consistency of the lifecycle),
not business correctness: an invoice with a wrong VAT amount or a total that does not
add up must be representable, because detecting it is the job of the rules in step 2.
"""

from datetime import date
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from invoice_agent.domain.base import DomainModel
from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.invoice_status import InvoiceStatus, check_transition
from invoice_agent.domain.types import (
    CurrencyCode,
    Money,
    NonEmptyStr,
    Quantity,
    Siren,
    UtcDatetime,
    VatRate,
)


class SourceFormat(StrEnum):
    FACTURX = "facturx"
    PDF = "pdf"


class SourceDocument(DomainModel):
    uri: NonEmptyStr
    format: SourceFormat
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Party(DomainModel):
    """Seller or buyer exactly as written on the invoice."""

    name: NonEmptyStr
    siren: Siren | None = None
    vat_number: str | None = None


class InvoiceLine(DomainModel):
    description: NonEmptyStr
    quantity: Quantity
    unit_price: Money
    vat_rate: VatRate
    amount_ht: Money


class VatBreakdownLine(DomainModel):
    rate: VatRate
    basis: Money
    vat: Money


class InvoiceData(DomainModel):
    """Everything extracted from the document (filled once the invoice is extracted)."""

    invoice_number: NonEmptyStr
    issue_date: date
    due_date: date | None = None
    currency: CurrencyCode = "EUR"
    seller: Party
    buyer: Party | None = None
    purchase_order_ref: str | None = None
    iban: IbanFingerprint | None = None
    lines: tuple[InvoiceLine, ...] = Field(min_length=1)
    vat_breakdown: tuple[VatBreakdownLine, ...] = ()
    total_ht: Money
    total_vat: Money
    total_ttc: Money


_REQUIRES_DATA = frozenset(
    {
        InvoiceStatus.EXTRACTED,
        InvoiceStatus.VALIDATED,
        InvoiceStatus.NEEDS_REVIEW,
        InvoiceStatus.APPROVED,
        InvoiceStatus.REJECTED,
        InvoiceStatus.POSTED,
    }
)


class Invoice(DomainModel):
    id: UUID
    status: InvoiceStatus = InvoiceStatus.RECEIVED
    source: SourceDocument
    data: InvoiceData | None = None
    supplier_id: UUID | None = None
    """Known supplier matched to the seller; None while unknown (INV-15)."""
    purchase_order_id: UUID | None = None
    failure_reason: NonEmptyStr | None = None
    received_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _check_lifecycle_consistency(self) -> Self:
        if self.status in _REQUIRES_DATA and self.data is None:
            raise ValueError(f"an invoice in status {self.status.value!r} must have extracted data")
        if self.status is InvoiceStatus.FAILED and self.failure_reason is None:
            raise ValueError("a failed invoice must carry a failure_reason")
        if self.status is not InvoiceStatus.FAILED and self.failure_reason is not None:
            raise ValueError("failure_reason is only allowed on failed invoices")
        if self.updated_at < self.received_at:
            raise ValueError("updated_at cannot be before received_at")
        return self

    def _evolve(self, at: UtcDatetime, **changes: object) -> Self:
        # Re-validates, so the result obeys every invariant above.
        return type(self).model_validate({**dict(self), **changes, "updated_at": at})

    def with_data(self, data: InvoiceData, *, at: UtcDatetime) -> Self:
        return self._evolve(at, data=data)

    def with_links(
        self, *, supplier_id: UUID | None, purchase_order_id: UUID | None, at: UtcDatetime
    ) -> Self:
        return self._evolve(at, supplier_id=supplier_id, purchase_order_id=purchase_order_id)

    def transition_to(
        self, target: InvoiceStatus, *, at: UtcDatetime, failure_reason: str | None = None
    ) -> Self:
        """Return the invoice in ``target`` status, or raise InvalidStatusTransition."""
        check_transition(self.status, target)
        return self._evolve(at, status=target, failure_reason=failure_reason)
