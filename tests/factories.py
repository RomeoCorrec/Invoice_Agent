"""Builders for domain objects used by the repository tests."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from invoice_agent.domain.audit import ActorType, AuditEvent
from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.invoice import (
    Invoice,
    InvoiceData,
    InvoiceLine,
    Party,
    SourceDocument,
    SourceFormat,
    VatBreakdownLine,
)
from invoice_agent.domain.journal import JournalEntry, JournalLine, Side
from invoice_agent.domain.purchase_order import PurchaseOrder, PurchaseOrderLine
from invoice_agent.domain.supplier import Supplier

IBAN_KEY = b"test-key"
T0 = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
GROUND_TRUTH = Path(__file__).parent / "fixtures" / "invoice-test-set" / "ground_truth"


def make_supplier(**overrides: Any) -> Supplier:
    values: dict[str, Any] = {
        "id": uuid4(),
        "code": f"SUP-{uuid4().hex[:8]}",
        "name": "Papeterie Mercier SARL",
        "siren": "151757747",
        "siret": "15175774726270",
        "vat_number": "FR67151757747",
        "iban": IbanFingerprint.from_iban("FR7699053965263585803695442", IBAN_KEY),
        "default_expense_account": "6064",
        "supplier_account": "401MERC",
    }
    return Supplier(**{**values, **overrides})


def make_purchase_order(supplier_id: UUID, **overrides: Any) -> PurchaseOrder:
    values: dict[str, Any] = {
        "id": uuid4(),
        "number": f"PO-{uuid4().hex[:8]}",
        "supplier_id": supplier_id,
        "order_date": date(2026, 9, 1),
        "lines": (
            PurchaseOrderLine(
                description="Ramettes", quantity="10", unit_price="24.90", amount_ht="249.00"
            ),
            PurchaseOrderLine(
                description="Stylos", quantity="6", unit_price="8.50", amount_ht="51.00"
            ),
        ),
    }
    return PurchaseOrder(**{**values, **overrides})


def make_data(number: str = "F-1", **overrides: Any) -> InvoiceData:
    values: dict[str, Any] = {
        "invoice_number": number,
        "issue_date": date(2026, 9, 17),
        "due_date": date(2026, 10, 17),
        "seller": Party(
            name="Café des Ateliers SARL", siren="850223454", vat_number="FR84850223454"
        ),
        "buyer": Party(name="Atelier Lumen SAS", siren="213033558"),
        "purchase_order_ref": "PO-2026-0105",
        "iban": IbanFingerprint.from_iban("FR7699061508882043304951471", IBAN_KEY),
        "lines": (
            InvoiceLine(
                description="Plateaux repas",
                quantity="24",
                unit_price="18.50",
                vat_rate="10.00",
                amount_ht="444.00",
            ),
            InvoiceLine(
                description="Boissons",
                quantity="3",
                unit_price="42.00",
                vat_rate="20.00",
                amount_ht="126.00",
            ),
        ),
        "vat_breakdown": (
            VatBreakdownLine(rate="20.00", basis="126.00", vat="25.20"),
            VatBreakdownLine(rate="10.00", basis="444.00", vat="44.40"),
        ),
        "total_ht": "570.00",
        "total_vat": "69.60",
        "total_ttc": "639.60",
    }
    return InvoiceData(**{**values, **overrides})


def make_invoice(**overrides: Any) -> Invoice:
    values: dict[str, Any] = {
        "id": uuid4(),
        "source": SourceDocument(
            uri="s3://bucket/a.pdf", format=SourceFormat.FACTURX, sha256=uuid4().hex * 2
        ),
        "received_at": T0,
        "updated_at": T0,
    }
    return Invoice(**{**values, **overrides})


def make_entry(invoice_id: UUID, key: str | None = None) -> JournalEntry:
    """The entry expected for INV-05 in the golden dataset."""
    return JournalEntry(
        id=uuid4(),
        invoice_id=invoice_id,
        entry_date=date(2026, 9, 17),
        reference="F-1",
        idempotency_key=key or f"invoice-{invoice_id}",
        lines=(
            JournalLine(account="6257", label="Réceptions", side=Side.DEBIT, amount="570.00"),
            JournalLine(account="44566", label="TVA", side=Side.DEBIT, amount="69.60"),
            JournalLine(account="401CAFE", label="Fournisseur", side=Side.CREDIT, amount="639.60"),
        ),
        created_at=T0,
    )


def make_event(invoice_id: UUID | None = None, **overrides: Any) -> AuditEvent:
    values: dict[str, Any] = {
        "id": uuid4(),
        "run_id": uuid4(),
        "step": "extract",
        "actor_type": ActorType.AGENT,
        "actor_id": "extractor-v1",
        "invoice_id": invoice_id,
        "payload": {"total_ttc": "639.60", "anomalies": ["PO_NOT_FOUND"], "attempt": 1},
        "occurred_at": T0,
    }
    return AuditEvent(**{**values, **overrides})


def golden_extraction(path: Path) -> InvoiceData:
    extraction: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["expected_extraction"]
    iban = extraction.pop("iban", None)
    if iban:
        extraction["iban"] = IbanFingerprint.from_iban(iban, IBAN_KEY)
    return InvoiceData.model_validate(extraction)
