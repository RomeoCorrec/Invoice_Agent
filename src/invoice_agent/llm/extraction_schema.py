"""What the model is asked to return, and its conversion to the domain model.

The model fills a flat schema made of strings: amounts and dates are written the way the
prompt asks for, then converted here with the same strict types as everywhere else. The model
never produces a ``Decimal`` or a fingerprint; a value that does not convert is an extraction
error, not something to repair.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, ValidationError

from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.invoice import (
    InvoiceData,
    InvoiceLine,
    Party,
    VatBreakdownLine,
)
from invoice_agent.ingestion.document import ExtractionError


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PartyOut(_Out):
    name: str
    siren: str | None = None
    vat_number: str | None = None


class LineOut(_Out):
    description: str
    quantity: str
    unit_price: str
    vat_rate: str
    amount_ht: str


class VatOut(_Out):
    rate: str
    basis: str
    vat: str


class ExtractedInvoice(_Out):
    invoice_number: str
    issue_date: str
    due_date: str | None = None
    currency: str = "EUR"
    seller: PartyOut
    buyer: PartyOut | None = None
    purchase_order_ref: str | None = None
    iban: str | None = None
    notes: list[str] = []
    lines: list[LineOut]
    vat_breakdown: list[VatOut] = []
    total_ht: str
    total_vat: str
    total_ttc: str


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        raise ExtractionError("an extracted amount is not a valid number") from None


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ExtractionError("an extracted date is not a valid ISO date") from None


def _party(out: PartyOut) -> Party:
    return Party(name=out.name, siren=out.siren, vat_number=out.vat_number)


def to_invoice_data(extracted: ExtractedInvoice, iban_key: bytes) -> InvoiceData:
    """Convert the model's strings to strict domain values, or raise ExtractionError."""
    try:
        return InvoiceData(
            invoice_number=extracted.invoice_number,
            issue_date=_date(extracted.issue_date),
            due_date=_date(extracted.due_date) if extracted.due_date else None,
            currency=extracted.currency,
            seller=_party(extracted.seller),
            buyer=_party(extracted.buyer) if extracted.buyer else None,
            purchase_order_ref=extracted.purchase_order_ref or None,
            iban=IbanFingerprint.from_iban(extracted.iban, iban_key) if extracted.iban else None,
            notes=tuple(note for note in (n.strip() for n in extracted.notes) if note),
            lines=tuple(
                InvoiceLine(
                    description=line.description,
                    quantity=_decimal(line.quantity),
                    unit_price=_decimal(line.unit_price),
                    vat_rate=_decimal(line.vat_rate),
                    amount_ht=_decimal(line.amount_ht),
                )
                for line in extracted.lines
            ),
            vat_breakdown=tuple(
                VatBreakdownLine(
                    rate=_decimal(vat.rate), basis=_decimal(vat.basis), vat=_decimal(vat.vat)
                )
                for vat in extracted.vat_breakdown
            ),
            total_ht=_decimal(extracted.total_ht),
            total_vat=_decimal(extracted.total_vat),
            total_ttc=_decimal(extracted.total_ttc),
        )
    except ValidationError:
        raise ExtractionError("the extracted invoice does not satisfy the domain rules") from None
    except ValueError as exc:  # IbanFingerprint.from_iban: not an IBAN shape
        if isinstance(exc, ExtractionError):
            raise
        raise ExtractionError("an extracted bank detail is malformed") from None
