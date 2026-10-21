from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from invoice_agent.domain.invoice import (
    Invoice,
    InvoiceData,
    InvoiceLine,
    Party,
    SourceDocument,
    SourceFormat,
    VatBreakdownLine,
)
from invoice_agent.domain.invoice_status import InvalidStatusTransition, InvoiceStatus

T0 = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=1)


def make_data(**overrides: object) -> InvoiceData:
    data: dict[str, object] = {
        "invoice_number": "CDA-26-0412",
        "issue_date": date(2026, 9, 17),
        "seller": Party(name="Café des Ateliers SARL", siren="850223454"),
        "lines": (
            InvoiceLine(
                description="Plateaux repas",
                quantity="24",
                unit_price="18.50",
                vat_rate="10.00",
                amount_ht="444.00",
            ),
        ),
        "vat_breakdown": (VatBreakdownLine(rate="10.00", basis="444.00", vat="44.40"),),
        "total_ht": "444.00",
        "total_vat": "44.40",
        "total_ttc": "488.40",
    }
    return InvoiceData(**{**data, **overrides})  # type: ignore[arg-type]


def make_invoice(**overrides: object) -> Invoice:
    data: dict[str, object] = {
        "id": uuid4(),
        "source": SourceDocument(
            uri="s3://bucket/a.pdf", format=SourceFormat.FACTURX, sha256="a" * 64
        ),
        "received_at": T0,
        "updated_at": T0,
    }
    return Invoice(**{**data, **overrides})  # type: ignore[arg-type]


def test_new_invoice_is_received_without_data() -> None:
    invoice = make_invoice()
    assert invoice.status is InvoiceStatus.RECEIVED
    assert invoice.data is None


def test_inconsistent_totals_are_representable() -> None:
    """Detecting wrong totals/VAT is a rule (step 2), not a model invariant."""
    data = make_data(total_ttc="999.99", total_vat="1.00")
    assert data.total_ttc > data.total_ht


def test_happy_path_through_the_lifecycle() -> None:
    invoice = make_invoice().with_data(make_data(), at=T1)
    for target in (InvoiceStatus.EXTRACTED, InvoiceStatus.VALIDATED, InvoiceStatus.APPROVED):
        invoice = invoice.transition_to(target, at=T1)
    assert invoice.status is InvoiceStatus.APPROVED
    assert invoice.updated_at == T1


def test_transition_returns_a_new_invoice() -> None:
    original = make_invoice().with_data(make_data(), at=T1)
    moved = original.transition_to(InvoiceStatus.EXTRACTED, at=T1)
    assert original.status is InvoiceStatus.RECEIVED
    assert moved.status is InvoiceStatus.EXTRACTED


def test_forbidden_transition_is_rejected() -> None:
    with pytest.raises(InvalidStatusTransition):
        make_invoice().transition_to(InvoiceStatus.POSTED, at=T1)


def test_cannot_be_extracted_without_data() -> None:
    with pytest.raises(ValidationError, match="extracted data"):
        make_invoice().transition_to(InvoiceStatus.EXTRACTED, at=T1)


def test_failed_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="failure_reason"):
        make_invoice().transition_to(InvoiceStatus.FAILED, at=T1)
    failed = make_invoice().transition_to(InvoiceStatus.FAILED, at=T1, failure_reason="boom")
    assert failed.failure_reason == "boom"


def test_retry_clears_the_failure_reason() -> None:
    failed = make_invoice().transition_to(InvoiceStatus.FAILED, at=T1, failure_reason="boom")
    retried = failed.transition_to(InvoiceStatus.RECEIVED, at=T1)
    assert retried.failure_reason is None


def test_failure_reason_only_on_failed_invoices() -> None:
    with pytest.raises(ValidationError, match="only allowed"):
        make_invoice(failure_reason="boom")


def test_updated_at_cannot_precede_received_at() -> None:
    with pytest.raises(ValidationError, match="updated_at"):
        make_invoice(updated_at=T0 - timedelta(seconds=1))


def test_links_to_supplier_and_purchase_order() -> None:
    supplier_id, po_id = uuid4(), uuid4()
    linked = make_invoice().with_links(supplier_id=supplier_id, purchase_order_id=po_id, at=T1)
    assert (linked.supplier_id, linked.purchase_order_id) == (supplier_id, po_id)


def test_invoice_needs_at_least_one_line() -> None:
    with pytest.raises(ValidationError):
        make_data(lines=())


def test_source_hash_must_be_sha256_hex() -> None:
    with pytest.raises(ValidationError):
        SourceDocument(uri="x", format=SourceFormat.PDF, sha256="nope")
