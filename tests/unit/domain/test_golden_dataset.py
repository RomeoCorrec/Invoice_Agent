"""The domain models must be able to represent every case of the golden dataset."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.domain.journal import JournalEntry, JournalLine, Side

DATASET = Path(__file__).parents[2] / "fixtures" / "invoice-test-set"
CASES = sorted((DATASET / "ground_truth").glob("INV-*.json"))


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_dataset_is_present() -> None:
    assert len(CASES) == 21


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_extraction_is_representable(path: Path) -> None:
    extraction = dict(load(path)["expected_extraction"])
    iban = extraction.pop("iban", None)
    if iban:
        extraction["iban"] = IbanFingerprint.from_iban(iban, b"test-key")
    data = InvoiceData.model_validate(extraction)
    assert str(data.total_ttc) == extraction["total_ttc"]


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_expected_journal_entry_is_balanced(path: Path) -> None:
    expected = load(path)["expected_journal_entry"]
    if expected is None:
        pytest.skip("no entry expected for this case")
    lines = []
    for raw in expected["lines"]:
        side, amount = (
            (Side.DEBIT, raw["debit"]) if raw["credit"] == "0.00" else (Side.CREDIT, raw["credit"])
        )
        lines.append(
            JournalLine(account=raw["account"], label=raw["label"], side=side, amount=amount)
        )
    entry = JournalEntry(
        id=uuid4(),
        invoice_id=uuid4(),
        journal=expected["journal"],
        entry_date=expected["date"],
        reference=expected["reference"],
        idempotency_key=path.stem,
        lines=tuple(lines),
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert entry.total_debit == entry.total_credit
