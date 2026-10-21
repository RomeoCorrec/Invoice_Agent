"""The database must reject invalid data even when the application layer is bypassed."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)


def insert(connection: Connection, table: str, **values: Any) -> None:
    columns = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    connection.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({params})"), values)


def new_invoice(connection: Connection, **overrides: Any) -> uuid.UUID:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "status": "received",
        "source_uri": "s3://bucket/a.pdf",
        "source_format": "pdf",
        "source_sha256": "a" * 64,
        "received_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    insert(connection, "invoices", **values)
    return values["id"]  # type: ignore[no-any-return]


def extracted_fields(number: str = "F-1") -> dict[str, Any]:
    return {
        "invoice_number": number,
        "issue_date": NOW.date(),
        "currency": "EUR",
        "seller_name": "ACME",
        "total_ht": "10.00",
        "total_vat": "2.00",
        "total_ttc": "12.00",
    }


def new_entry(connection: Connection, invoice_id: uuid.UUID, key: str = "k1") -> uuid.UUID:
    entry_id = uuid.uuid4()
    insert(
        connection,
        "journal_entries",
        id=entry_id,
        invoice_id=invoice_id,
        journal="ACH",
        entry_date=NOW.date(),
        reference="F-1",
        idempotency_key=key,
        created_at=NOW,
    )
    return entry_id


def add_line(connection: Connection, entry_id: uuid.UUID, no: int, debit: str, credit: str) -> None:
    insert(
        connection,
        "journal_lines",
        entry_id=entry_id,
        line_no=no,
        account="6064",
        label="x",
        debit=debit,
        credit=credit,
    )


# --- journal entries: balance ------------------------------------------------------------


def test_balanced_entry_commits(engine: Engine) -> None:
    with engine.begin() as connection:
        entry_id = new_entry(connection, new_invoice(connection))
        add_line(connection, entry_id, 1, "10.00", "0")
        add_line(connection, entry_id, 2, "0", "10.00")


def test_unbalanced_entry_is_rejected_at_commit(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="unbalanced"), engine.begin() as connection:
        entry_id = new_entry(connection, new_invoice(connection))
        add_line(connection, entry_id, 1, "10.00", "0")
        add_line(connection, entry_id, 2, "0", "9.99")


def test_entry_with_a_single_line_is_rejected(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="at least 2 lines"), engine.begin() as connection:
        entry_id = new_entry(connection, new_invoice(connection))
        add_line(connection, entry_id, 1, "10.00", "0")


def test_entry_without_lines_is_rejected(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="at least 2 lines"), engine.begin() as connection:
        new_entry(connection, new_invoice(connection))


def test_balance_is_checked_at_commit_not_per_row(engine: Engine) -> None:
    """Lines may be inserted one at a time; only the final state must balance."""
    with engine.begin() as connection:
        entry_id = new_entry(connection, new_invoice(connection))
        add_line(connection, entry_id, 1, "0.10", "0")
        add_line(connection, entry_id, 2, "0.20", "0")
        add_line(connection, entry_id, 3, "0", "0.30")


@pytest.mark.parametrize(
    ("debit", "credit", "constraint"),
    [
        ("5.00", "5.00", "one_side_only"),
        ("0", "0", "one_side_only"),
        ("-5.00", "0", "amounts_non_negative"),
    ],
)
def test_line_must_carry_exactly_one_positive_side(
    engine: Engine, debit: str, credit: str, constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint), engine.begin() as connection:
        entry_id = new_entry(connection, new_invoice(connection))
        add_line(connection, entry_id, 1, debit, credit)


def test_one_entry_per_invoice_and_unique_idempotency_key(engine: Engine) -> None:
    with engine.begin() as connection:
        invoice_id = new_invoice(connection)
        entry_id = new_entry(connection, invoice_id)
        add_line(connection, entry_id, 1, "1.00", "0")
        add_line(connection, entry_id, 2, "0", "1.00")
    with pytest.raises(IntegrityError, match="uq_journal_entries_invoice_id"), engine.begin() as c:
        new_entry(c, invoice_id, key="other-key")
    with pytest.raises(IntegrityError, match="idempotency_key"), engine.begin() as c:
        new_entry(c, new_invoice(c), key="k1")


# --- append-only tables ------------------------------------------------------------------


def test_posted_entries_cannot_be_modified_or_deleted(engine: Engine) -> None:
    with engine.begin() as connection:
        entry_id = new_entry(connection, new_invoice(connection))
        add_line(connection, entry_id, 1, "1.00", "0")
        add_line(connection, entry_id, 2, "0", "1.00")
    for statement in (
        "UPDATE journal_lines SET debit = 2.00 WHERE debit > 0",
        "DELETE FROM journal_lines",
        "UPDATE journal_entries SET reference = 'x'",
        "DELETE FROM journal_entries",
    ):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
            connection.execute(text(statement))


def test_audit_events_are_append_only(engine: Engine) -> None:
    event_id = uuid.uuid4()
    with engine.begin() as connection:
        insert(
            connection,
            "audit_events",
            id=event_id,
            run_id=uuid.uuid4(),
            step="extract",
            actor_type="agent",
            actor_id="extractor",
            payload="{}",
            occurred_at=NOW,
        )
    for statement in ("UPDATE audit_events SET step = 'x'", "DELETE FROM audit_events"):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
            connection.execute(text(statement))


def test_audit_actor_type_is_restricted(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="actor_type_valid"), engine.begin() as connection:
        insert(
            connection,
            "audit_events",
            id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            step="x",
            actor_type="robot",
            actor_id="x",
            payload="{}",
            occurred_at=NOW,
        )


# --- invoices ----------------------------------------------------------------------------


def test_unknown_status_is_rejected(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="status_valid"), engine.begin() as connection:
        new_invoice(connection, status="archived")


def test_extracted_status_requires_data(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="data_required_for_status"), engine.begin() as c:
        new_invoice(c, status="extracted")
    with engine.begin() as connection:
        new_invoice(connection, status="extracted", **extracted_fields())


def test_extracted_data_is_all_or_nothing(engine: Engine) -> None:
    partial = extracted_fields()
    del partial["total_ttc"]
    with pytest.raises(IntegrityError, match="data_all_or_nothing"), engine.begin() as c:
        new_invoice(c, status="received", **partial)


def test_failure_reason_matches_failed_status(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="failure_reason"), engine.begin() as c:
        new_invoice(c, status="failed")
    with pytest.raises(IntegrityError, match="failure_reason"), engine.begin() as c:
        new_invoice(c, status="received", failure_reason="boom")
    with engine.begin() as connection:
        new_invoice(connection, status="failed", failure_reason="boom")


def test_updated_at_cannot_precede_received_at(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="updated_after_received"), engine.begin() as c:
        new_invoice(c, updated_at=NOW - timedelta(seconds=1))


def test_duplicate_invoice_numbers_are_storable(engine: Engine) -> None:
    """INV-14 re-sends INV-01: it must be stored to be flagged, not blocked."""
    with engine.begin() as connection:
        supplier_id = uuid.uuid4()
        insert(
            connection,
            "suppliers",
            id=supplier_id,
            code="SUP-001",
            name="ACME",
            siren="151757747",
            siret="15175774726270",
            default_expense_account="6064",
            supplier_account="401ACME",
        )
        for _ in range(2):
            new_invoice(
                connection, supplier_id=supplier_id, status="extracted", **extracted_fields()
            )


def test_iban_mask_and_hash_go_together(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="iban_all_or_nothing"), engine.begin() as c:
        new_invoice(c, iban_masked="FR76 **** 5442")
