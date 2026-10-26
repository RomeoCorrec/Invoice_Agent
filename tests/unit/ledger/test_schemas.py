from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from invoice_agent.domain.journal import JournalLine, Side
from ledger.schemas import EntryRequest, ErrorCode, LedgerRefusal
from ledger.store import request_fingerprint


def lines(ht: str = "249.00", vat: str = "49.80", ttc: str = "298.80") -> tuple[JournalLine, ...]:
    return (
        JournalLine(account="6064", label="Papier", side=Side.DEBIT, amount=Decimal(ht)),
        JournalLine(account="44566", label="TVA", side=Side.DEBIT, amount=Decimal(vat)),
        JournalLine(account="401MERC", label="Fournisseur", side=Side.CREDIT, amount=Decimal(ttc)),
    )


def request(key: str = "post:1", **overrides: Any) -> EntryRequest:
    fields: dict[str, Any] = {
        "idempotency_key": key,
        "supplier_siren": "151757747",
        "invoice_number": "PM-2026-0457",
        "entry_date": date(2026, 9, 30),
        "lines": lines(),
    }
    return EntryRequest(**{**fields, **overrides})


def test_a_balanced_entry_is_accepted_and_totals_add_up() -> None:
    entry = request()
    assert entry.total_debit == entry.total_credit == Decimal("298.80")
    assert entry.journal == "ACH"


def test_an_unbalanced_entry_cannot_be_built() -> None:
    with pytest.raises(ValidationError, match="unbalanced_entry"):
        request(lines=lines(ttc="298.79"))


def test_an_entry_needs_two_lines_and_accepts_no_unknown_field() -> None:
    with pytest.raises(ValidationError):
        request(lines=lines()[:1])
    with pytest.raises(ValidationError):
        request(surprise=1)


def test_the_fingerprint_ignores_the_key_but_not_the_content() -> None:
    assert request_fingerprint(request("a")) == request_fingerprint(request("b"))
    assert request_fingerprint(request()) != request_fingerprint(
        request(lines=lines("250.00", "50.00", "300.00"))
    )
    assert request_fingerprint(request()) != request_fingerprint(request(invoice_number="X-1"))


def test_a_refusal_carries_a_stable_code_in_its_message() -> None:
    refusal = LedgerRefusal(ErrorCode.UNKNOWN_ACCOUNT, "accounts not in the chart: 9999")
    assert str(refusal) == "unknown_account: accounts not in the chart: 9999"
    assert refusal.code is ErrorCode.UNKNOWN_ACCOUNT
