from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from invoice_agent.db.models import JournalEntryRow
from invoice_agent.db.repositories import (
    AuditEventRepository,
    IdempotencyConflictError,
    InvoiceNotFoundError,
    InvoiceRepository,
    JournalEntryRepository,
    PurchaseOrderRepository,
    StaleInvoiceError,
    SupplierRepository,
)
from invoice_agent.db.session import make_engine, make_session_factory, session_scope
from invoice_agent.domain.invoice import Invoice, Party, SourceDocument, SourceFormat
from invoice_agent.domain.invoice_status import InvalidStatusTransition, InvoiceStatus
from invoice_agent.domain.purchase_order import PurchaseOrderStatus
from tests.factories import (
    GROUND_TRUTH,
    T0,
    golden_extraction,
    make_data,
    make_entry,
    make_event,
    make_invoice,
    make_purchase_order,
    make_supplier,
)

# Every test reloads through `session.expunge_all()` so reads really hit the database
# instead of returning objects cached in the session.


# --- suppliers ---------------------------------------------------------------------------


def test_supplier_round_trip(session: Session) -> None:
    repo = SupplierRepository(session)
    supplier = make_supplier()
    repo.add(supplier)
    session.expunge_all()
    assert repo.get(supplier.id) == supplier


def test_supplier_without_iban(session: Session) -> None:
    repo = SupplierRepository(session)
    supplier = make_supplier(iban=None, vat_number=None)
    repo.add(supplier)
    session.expunge_all()
    assert repo.get(supplier.id) == supplier


def test_the_contact_address_of_a_supplier_is_stored_and_optional(session: Session) -> None:
    repo = SupplierRepository(session)
    with_contact = make_supplier(contact_email="compta@papeterie-mercier-sarl.example")
    without = make_supplier(siren="623671427", siret="62367142700011")
    repo.add(with_contact)
    repo.add(without)
    session.expunge_all()
    assert repo.get(with_contact.id) == with_contact
    found = repo.get(without.id)
    assert found is not None and found.contact_email is None


def test_the_demo_suppliers_all_have_a_distinct_fictitious_contact(session: Session) -> None:
    from tests.integration.reference import load_reference

    ids = load_reference(session)
    contacts = [SupplierRepository(session).get(supplier_id) for supplier_id in ids.values()]
    addresses = [c.contact_email for c in contacts if c is not None]
    assert len(addresses) == len(ids) == len(set(addresses))
    assert all(
        a is not None and a.startswith("compta@") and a.endswith(".example") for a in addresses
    )


def test_supplier_lookups(session: Session) -> None:
    repo = SupplierRepository(session)
    supplier = make_supplier(code="SUP-042", siren="419058185")
    repo.add(supplier)
    assert repo.get_by_code("SUP-042") == supplier
    assert repo.get_by_siren("419058185") == supplier
    assert repo.get_by_code("nope") is None
    assert repo.get(uuid4()) is None


# --- purchase orders ---------------------------------------------------------------------


def test_purchase_order_round_trip(session: Session) -> None:
    supplier = make_supplier()
    SupplierRepository(session).add(supplier)
    repo = PurchaseOrderRepository(session)
    order = make_purchase_order(supplier.id)
    repo.add(order)
    session.expunge_all()
    loaded = repo.get(order.id)
    assert loaded == order
    assert loaded is not None
    assert str(loaded.total_ht) == "300.00"
    assert repo.get_by_number(order.number) == order


def test_only_open_orders_of_the_supplier_are_listed(session: Session) -> None:
    mine, other = make_supplier(), make_supplier()
    for supplier in (mine, other):
        SupplierRepository(session).add(supplier)
    repo = PurchaseOrderRepository(session)
    open_order = make_purchase_order(mine.id)
    repo.add(open_order)
    repo.add(make_purchase_order(mine.id, status=PurchaseOrderStatus.CLOSED))
    repo.add(make_purchase_order(other.id))
    assert repo.list_open_for_supplier(mine.id) == [open_order]


# --- invoices ----------------------------------------------------------------------------


def test_received_invoice_round_trip(session: Session) -> None:
    repo = InvoiceRepository(session)
    invoice = make_invoice()
    repo.add(invoice)
    session.expunge_all()
    loaded = repo.get(invoice.id)
    assert loaded == invoice
    assert loaded is not None
    assert loaded.data is None


def test_invoice_with_data_and_links_round_trip(session: Session) -> None:
    supplier = make_supplier()
    SupplierRepository(session).add(supplier)
    order = make_purchase_order(supplier.id)
    PurchaseOrderRepository(session).add(order)
    invoice = (
        make_invoice()
        .with_data(make_data(), at=T0)
        .with_links(supplier_id=supplier.id, purchase_order_id=order.id, at=T0)
        .transition_to(InvoiceStatus.EXTRACTED, at=T0)
    )
    repo = InvoiceRepository(session)
    repo.add(invoice)
    session.expunge_all()
    assert repo.get(invoice.id) == invoice


@pytest.mark.parametrize("path", sorted(GROUND_TRUTH.glob("INV-*.json")), ids=lambda p: p.stem)
def test_every_golden_invoice_survives_a_round_trip(session: Session, path: Path) -> None:
    invoice = (
        make_invoice()
        .with_data(golden_extraction(path), at=T0)
        .transition_to(InvoiceStatus.EXTRACTED, at=T0)
    )
    repo = InvoiceRepository(session)
    repo.add(invoice)
    session.expunge_all()
    assert repo.get(invoice.id) == invoice


def test_save_applies_a_transition(session: Session) -> None:
    repo = InvoiceRepository(session)
    received = make_invoice().with_data(make_data(), at=T0)
    repo.add(received)
    extracted = received.transition_to(InvoiceStatus.EXTRACTED, at=T0 + timedelta(seconds=1))
    repo.save(extracted, expected_status=InvoiceStatus.RECEIVED)
    session.expunge_all()
    assert repo.get(received.id) == extracted


def test_save_replaces_lines_when_data_changes(session: Session) -> None:
    repo = InvoiceRepository(session)
    invoice = make_invoice().with_data(make_data(), at=T0)
    repo.add(invoice)
    one_line = make_data(lines=make_data().lines[:1], vat_breakdown=make_data().vat_breakdown[:1])
    changed = invoice.with_data(one_line, at=T0 + timedelta(seconds=1))
    repo.save(changed, expected_status=InvoiceStatus.RECEIVED)
    session.expunge_all()
    loaded = repo.get(invoice.id)
    assert loaded == changed
    assert loaded is not None
    assert loaded.data is not None
    assert len(loaded.data.lines) == 1


def test_save_refuses_a_stale_invoice(engine: Engine) -> None:
    """Worker B moved the invoice on; worker A must not overwrite that with old state."""
    factory = make_session_factory(engine)
    invoice = make_invoice().with_data(make_data(), at=T0)
    with session_scope(factory) as setup:
        InvoiceRepository(setup).add(invoice)

    with session_scope(factory) as worker_b:
        InvoiceRepository(worker_b).save(
            invoice.transition_to(InvoiceStatus.EXTRACTED, at=T0),
            expected_status=InvoiceStatus.RECEIVED,
        )

    failed = invoice.transition_to(InvoiceStatus.FAILED, at=T0, failure_reason="boom")
    with pytest.raises(StaleInvoiceError) as error, session_scope(factory) as worker_a:
        InvoiceRepository(worker_a).save(failed, expected_status=InvoiceStatus.RECEIVED)
    assert error.value.actual is InvoiceStatus.EXTRACTED

    with session_scope(factory) as check:
        loaded = InvoiceRepository(check).get(invoice.id)
    assert loaded is not None
    assert loaded.status is InvoiceStatus.EXTRACTED


def test_save_rejects_a_forbidden_transition_before_touching_the_database(
    session: Session,
) -> None:
    repo = InvoiceRepository(session)
    invoice = make_invoice().with_data(make_data(), at=T0)
    repo.add(invoice)
    # The domain forbids received -> posted, so build the target without going through it.
    posted = invoice.model_copy(update={"status": InvoiceStatus.POSTED})
    with pytest.raises(InvalidStatusTransition):
        repo.save(posted, expected_status=InvoiceStatus.RECEIVED)


def test_save_unknown_invoice(session: Session) -> None:
    with pytest.raises(InvoiceNotFoundError):
        InvoiceRepository(session).save(make_invoice(), expected_status=InvoiceStatus.RECEIVED)


def test_resent_invoice_is_found_as_a_duplicate(session: Session) -> None:
    """INV-14 re-sends INV-01: same supplier and number, stored twice, found together."""
    supplier = make_supplier()
    SupplierRepository(session).add(supplier)
    repo = InvoiceRepository(session)
    first = (
        make_invoice(received_at=T0, updated_at=T0)
        .with_data(make_data("PM-2026-0457"), at=T0)
        .with_links(supplier_id=supplier.id, purchase_order_id=None, at=T0)
    )
    resent = (
        make_invoice(received_at=T0 + timedelta(days=1), updated_at=T0 + timedelta(days=1))
        .with_data(make_data("PM-2026-0457"), at=T0 + timedelta(days=1))
        .with_links(supplier_id=supplier.id, purchase_order_id=None, at=T0 + timedelta(days=1))
    )
    repo.add(resent)
    repo.add(first)
    found = repo.find_by_supplier_and_number(supplier.id, "PM-2026-0457")
    assert [invoice.id for invoice in found] == [first.id, resent.id]  # oldest first
    assert repo.find_by_supplier_and_number(supplier.id, "other") == []


def test_find_by_source_hash_and_status(session: Session) -> None:
    repo = InvoiceRepository(session)
    invoice = make_invoice()
    repo.add(invoice)
    assert repo.find_by_source_sha256(invoice.source.sha256) == [invoice]
    assert repo.list_by_status(InvoiceStatus.RECEIVED) == [invoice]
    assert repo.list_by_status(InvoiceStatus.POSTED) == []


# --- journal entries ---------------------------------------------------------------------


def test_journal_entry_round_trip_and_commit(engine: Engine) -> None:
    """Committing proves the database's balance trigger accepts what the mapper writes."""
    factory = make_session_factory(engine)
    invoice = make_invoice()
    entry = make_entry(invoice.id)
    with session_scope(factory) as session:
        InvoiceRepository(session).add(invoice)
        JournalEntryRepository(session).add(entry)
    with session_scope(factory) as session:
        repo = JournalEntryRepository(session)
        assert repo.get_by_invoice(invoice.id) == entry
        assert repo.get_by_idempotency_key(entry.idempotency_key) == entry


def test_posting_twice_creates_a_single_entry(engine: Engine) -> None:
    factory = make_session_factory(engine)
    invoice = make_invoice()
    first = make_entry(invoice.id, key="invoice-1")
    retry = make_entry(invoice.id, key="invoice-1")  # same key, fresh id: a retried call
    with session_scope(factory) as session:
        InvoiceRepository(session).add(invoice)
        assert JournalEntryRepository(session).add(first) == first
    with session_scope(factory) as session:
        assert JournalEntryRepository(session).add(retry) == first
    with session_scope(factory) as session:
        count = session.scalar(select(func.count()).select_from(JournalEntryRow))
    assert count == 1


def test_idempotency_key_reused_for_another_invoice_is_refused(session: Session) -> None:
    one, two = make_invoice(), make_invoice()
    for invoice in (one, two):
        InvoiceRepository(session).add(invoice)
    repo = JournalEntryRepository(session)
    repo.add(make_entry(one.id, key="shared"))
    with pytest.raises(IdempotencyConflictError):
        repo.add(make_entry(two.id, key="shared"))


# --- audit events ------------------------------------------------------------------------


def test_audit_events_round_trip_in_chronological_order(session: Session) -> None:
    invoice = make_invoice()
    InvoiceRepository(session).add(invoice)
    repo = AuditEventRepository(session)
    run_id = uuid4()
    later = make_event(invoice.id, run_id=run_id, step="validate", occurred_at=T0 + timedelta(1))
    earlier = make_event(invoice.id, run_id=run_id, step="extract", occurred_at=T0)
    unrelated = make_event(None)
    for event in (later, earlier, unrelated):
        repo.append(event)
    session.expunge_all()
    assert repo.list_for_invoice(invoice.id) == [earlier, later]
    assert repo.list_for_run(run_id) == [earlier, later]


def test_audit_repository_exposes_no_way_to_modify_events() -> None:
    public = {name for name in dir(AuditEventRepository) if not name.startswith("_")}
    assert public == {"append", "list_for_invoice", "list_for_run", "open_conversations"}


# --- sessions and transactions -----------------------------------------------------------


def test_work_in_one_scope_is_atomic(engine: Engine) -> None:
    """An invoice change and its audit event succeed or fail together."""
    factory = make_session_factory(engine)
    invoice = make_invoice().with_data(make_data(), at=T0)
    with session_scope(factory) as session:
        InvoiceRepository(session).add(invoice)

    with pytest.raises(RuntimeError, match="crash"), session_scope(factory) as session:
        InvoiceRepository(session).save(
            invoice.transition_to(InvoiceStatus.EXTRACTED, at=T0),
            expected_status=InvoiceStatus.RECEIVED,
        )
        AuditEventRepository(session).append(make_event(invoice.id))
        raise RuntimeError("crash")

    with session_scope(factory) as session:
        loaded = InvoiceRepository(session).get(invoice.id)
        events = AuditEventRepository(session).list_for_invoice(invoice.id)
    assert loaded is not None
    assert loaded.status is InvoiceStatus.RECEIVED
    assert events == []


def test_engine_enforces_the_statement_timeout(postgres_url: str, engine: Engine) -> None:
    fast = make_engine(postgres_url, statement_timeout_ms=200)
    with fast.connect() as connection, pytest.raises(DBAPIError, match="statement timeout"):
        connection.execute(text("SELECT pg_sleep(2)"))
    fast.dispose()


def test_invoice_notes_round_trip(session: Session) -> None:
    repo = InvoiceRepository(session)
    invoice = make_invoice().with_data(make_data(notes=("Paiement à 30 jours", "Merci")), at=T0)
    repo.add(invoice)
    session.expunge_all()
    loaded = repo.get(invoice.id)
    assert loaded == invoice
    assert loaded is not None
    assert loaded.data is not None
    assert loaded.data.notes == ("Paiement à 30 jours", "Merci")


# --- duplicate detection -----------------------------------------------------------------


def extracted(
    number: str, *, siren: str = "850223454", minutes: int = 0, **overrides: Any
) -> Invoice:
    when = T0 + timedelta(minutes=minutes)
    seller = Party(name="Café des Ateliers SARL", siren=siren, vat_number=f"FR00{siren}")
    return (
        make_invoice(received_at=when, updated_at=when, **overrides)
        .with_data(make_data(number, seller=seller), at=when)
        .transition_to(InvoiceStatus.EXTRACTED, at=when)
    )


def test_duplicate_found_when_the_earlier_copy_has_no_supplier_link_yet(session: Session) -> None:
    repo = InvoiceRepository(session)
    supplier = make_supplier()
    SupplierRepository(session).add(supplier)
    first = extracted("F-100", minutes=0)  # supplier_id still NULL: not validated yet
    second = extracted("F-100", minutes=5)
    repo.add(first)
    repo.add(second)
    found = repo.find_duplicates(second, supplier_id=supplier.id)
    assert [i.id for i in found] == [first.id]


def test_invoice_numbers_are_compared_ignoring_case_and_punctuation(session: Session) -> None:
    repo = InvoiceRepository(session)
    first, second = extracted("F-100", minutes=0), extracted("f 100", minutes=1)
    repo.add(first)
    repo.add(second)
    assert [i.id for i in repo.find_duplicates(second, supplier_id=None)] == [first.id]


def test_same_number_from_another_seller_is_not_a_duplicate(session: Session) -> None:
    repo = InvoiceRepository(session)
    first = extracted("F-100", siren="151757747", minutes=0)
    second = extracted("F-100", siren="419058185", minutes=1)
    repo.add(first)
    repo.add(second)
    assert repo.find_duplicates(second, supplier_id=None) == []


def test_same_file_is_found_even_while_the_other_copy_is_still_received(session: Session) -> None:
    repo = InvoiceRepository(session)
    sha = "b" * 64
    earlier = make_invoice(
        received_at=T0,
        updated_at=T0,
        source=SourceDocument(uri="s3://a", format=SourceFormat.PDF, sha256=sha),
    )
    later = extracted(
        "F-100",
        minutes=3,
        source=SourceDocument(uri="s3://b", format=SourceFormat.PDF, sha256=sha),
    )
    repo.add(earlier)
    repo.add(later)
    assert [i.id for i in repo.find_duplicates(later, supplier_id=None)] == [earlier.id]


def test_a_later_copy_counts_only_once_it_has_been_checked(session: Session) -> None:
    """Whatever the processing order, at least one of two copies ends up flagged."""
    repo = InvoiceRepository(session)
    first, second = extracted("F-100", minutes=0), extracted("F-100", minutes=5)
    repo.add(first)
    repo.add(second)
    # The later copy is only extracted: it will flag itself against `first` when checked.
    assert repo.find_duplicates(first, supplier_id=None) == []
    # Once the later copy was validated without seeing `first`, `first` must flag it.
    repo.save(
        second.transition_to(InvoiceStatus.VALIDATED, at=T0 + timedelta(minutes=6)),
        expected_status=InvoiceStatus.EXTRACTED,
    )
    assert [i.id for i in repo.find_duplicates(first, supplier_id=None)] == [second.id]


def test_failed_copies_are_ignored(session: Session) -> None:
    repo = InvoiceRepository(session)
    dead = make_invoice(received_at=T0, updated_at=T0).transition_to(
        InvoiceStatus.FAILED, at=T0, failure_reason="boom"
    )
    live = extracted("F-100", minutes=2, source=dead.source)
    repo.add(dead)
    repo.add(live)
    assert repo.find_duplicates(live, supplier_id=None) == []


def test_billed_amounts_ignore_dead_ends_and_the_invoice_itself(session: Session) -> None:
    supplier = make_supplier()
    SupplierRepository(session).add(supplier)
    order = make_purchase_order(supplier.id)
    PurchaseOrderRepository(session).add(order)
    repo = InvoiceRepository(session)

    def linked(status: InvoiceStatus, number: str) -> Invoice:
        invoice = extracted(number).with_links(
            supplier_id=supplier.id, purchase_order_id=order.id, at=T0
        )
        if status is InvoiceStatus.EXTRACTED:
            return invoice
        if status is InvoiceStatus.FAILED:
            return invoice.transition_to(status, at=T0, failure_reason="boom")
        return invoice.transition_to(InvoiceStatus.VALIDATED, at=T0)

    counted = linked(InvoiceStatus.EXTRACTED, "A")
    validated = linked(InvoiceStatus.VALIDATED, "B")
    failed = linked(InvoiceStatus.FAILED, "C")
    for invoice in (counted, validated, failed):
        repo.add(invoice)
    billed = repo.billed_ht_by_purchase_order([order.id], excluding=counted.id)
    assert billed == {order.id: Decimal("570.00")}  # only `validated`
    assert repo.billed_ht_by_purchase_order([], excluding=counted.id) == {}
    assert repo.billed_ht_by_purchase_order([uuid4()], excluding=counted.id) == {}


def test_events_with_the_same_timestamp_keep_their_insertion_order(session: Session) -> None:
    invoice = make_invoice()
    InvoiceRepository(session).add(invoice)
    repo = AuditEventRepository(session)
    steps = ["receive", "extract", "validate", "approve", "post"]
    for step in steps:
        repo.append(make_event(invoice.id, step=step, occurred_at=T0))  # all at the same instant
    session.expunge_all()
    assert [e.step for e in repo.list_for_invoice(invoice.id)] == steps
