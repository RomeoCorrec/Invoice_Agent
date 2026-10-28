from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, Table, func, select, text
from sqlalchemy.exc import DBAPIError

from ledger import tables
from ledger.schemas import ErrorCode, LedgerRefusal
from ledger.seed import seed_reference
from ledger.store import LedgerStore
from tests.unit.ledger.test_schemas import lines, request

FULL_IBAN = "FR7699053965263585803695442"


@pytest.fixture
def store(engine: Engine) -> Iterator[LedgerStore]:
    seed_reference(engine)
    yield LedgerStore(engine)


def count(engine: Engine, table: Table) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def only_the_ledger_schema(name: str | None, type_: str, _parent: object) -> bool:
    return name == "ledger" if type_ == "schema" else True


def test_the_migration_and_the_table_definitions_agree(engine: Engine) -> None:
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"include_schemas": True, "include_name": only_the_ledger_schema},
        )
        assert compare_metadata(context, tables.metadata) == []


# --- reads ---------------------------------------------------------------------------------


def test_a_supplier_is_found_by_siren_or_vat_number(store: LedgerStore) -> None:
    by_siren = store.get_supplier(siren="151757747")
    assert by_siren is not None and by_siren.code == "SUP-001"
    assert by_siren.supplier_account == "401MERC"
    assert store.get_supplier(vat_number=by_siren.vat_number) == by_siren
    assert store.get_supplier(siren="000000000") is None


def test_a_lookup_needs_a_key(store: LedgerStore) -> None:
    with pytest.raises(LedgerRefusal) as refusal:
        store.get_supplier()
    assert refusal.value.code is ErrorCode.INVALID_REQUEST


def test_the_full_iban_never_leaves_the_ledger(store: LedgerStore) -> None:
    supplier = store.get_supplier(siren="151757747")
    assert supplier is not None
    assert supplier.iban_masked and FULL_IBAN not in supplier.model_dump_json()


def test_open_purchase_orders_come_with_their_lines(store: LedgerStore) -> None:
    orders = store.list_open_purchase_orders("SUP-001")
    assert orders and all(order.status == "open" for order in orders)
    first = orders[0]
    assert first.number == "PO-2026-0101"
    assert sum((line.amount_ht for line in first.lines), Decimal(0)) == first.total_ht
    assert store.list_open_purchase_orders("SUP-999") == []


def test_one_purchase_order_by_number(store: LedgerStore) -> None:
    assert store.get_purchase_order("PO-2026-0101") is not None
    assert store.get_purchase_order("PO-NOPE") is None


def test_the_chart_can_be_filtered_by_prefix(store: LedgerStore) -> None:
    assert {a.number for a in store.get_chart_of_accounts("60")} == {"6064"}
    assert len(store.get_chart_of_accounts()) >= 8
    assert store.get_chart_of_accounts("%") == []  # a wildcard is data, not a pattern


# --- the write -----------------------------------------------------------------------------


def test_posting_writes_one_balanced_entry_that_can_then_be_found(
    store: LedgerStore, engine: Engine
) -> None:
    posted = store.post_entry(request())
    assert posted.replayed is False and posted.total == Decimal("298.80")
    assert count(engine, tables.entries) == 1 and count(engine, tables.entry_lines) == 3
    [found] = store.find_invoices("151757747", "PM-2026-0457")
    assert found.entry_id == posted.entry_id and found.total == Decimal("298.80")
    assert store.find_invoices("151757747", "OTHER") == []
    assert len(store.find_invoices("151757747")) == 1


def test_posting_twice_with_the_same_key_writes_once(store: LedgerStore, engine: Engine) -> None:
    first = store.post_entry(request())
    again = store.post_entry(request())
    assert again.replayed is True and again.entry_id == first.entry_id
    assert count(engine, tables.entries) == 1 and count(engine, tables.entry_lines) == 3


def test_the_same_key_for_another_request_is_refused(store: LedgerStore, engine: Engine) -> None:
    store.post_entry(request())
    other = request(lines=lines("250.00", "50.00", "300.00"))
    with pytest.raises(LedgerRefusal) as refusal:
        store.post_entry(other)
    assert refusal.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert count(engine, tables.entries) == 1


def test_an_invoice_is_booked_once_whatever_the_key(store: LedgerStore, engine: Engine) -> None:
    store.post_entry(request("post:1"))
    with pytest.raises(LedgerRefusal) as refusal:
        store.post_entry(request("post:2"))
    assert refusal.value.code is ErrorCode.INVOICE_ALREADY_BOOKED
    assert count(engine, tables.entries) == 1 and count(engine, tables.entry_lines) == 3


def test_an_unknown_supplier_or_account_is_refused_and_nothing_is_written(
    store: LedgerStore, engine: Engine
) -> None:
    with pytest.raises(LedgerRefusal) as refusal:
        store.post_entry(request(supplier_siren="000000000"))
    assert refusal.value.code is ErrorCode.UNKNOWN_SUPPLIER
    first, *rest = lines()
    bad = request(lines=(first.model_copy(update={"account": "9999"}), *rest))
    with pytest.raises(LedgerRefusal) as refusal:
        store.post_entry(bad)
    assert refusal.value.code is ErrorCode.UNKNOWN_ACCOUNT and "9999" in str(refusal.value)
    assert count(engine, tables.entries) == 0


def test_another_suppliers_auxiliary_account_is_not_accepted(store: LedgerStore) -> None:
    """401MERC belongs to Papeterie Mercier: booking it against ClimaTech is refused."""
    with pytest.raises(LedgerRefusal) as refusal:
        store.post_entry(request(supplier_siren="419058185"))
    assert refusal.value.code is ErrorCode.UNKNOWN_ACCOUNT


def test_posted_entries_cannot_be_changed_or_deleted(store: LedgerStore, engine: Engine) -> None:
    store.post_entry(request())
    for statement in (
        "UPDATE ledger.entry_lines SET amount = 1",
        "DELETE FROM ledger.entry_lines",
        "UPDATE ledger.entries SET journal = 'XX'",
        "DELETE FROM ledger.entries",
    ):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
            connection.execute(text(statement))
    assert count(engine, tables.entries) == 1


def test_concurrent_posts_with_one_key_write_once(store: LedgerStore, engine: Engine) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.post_entry(request()), range(8)))
    assert len({r.entry_id for r in results}) == 1
    assert sum(not r.replayed for r in results) == 1
    assert count(engine, tables.entries) == 1 and count(engine, tables.entry_lines) == 3


def test_concurrent_posts_never_refuse_the_same_request_as_already_booked(
    store: LedgerStore, engine: Engine
) -> None:
    """Twins of one request race on two unique indexes (key, invoice): all must replay.

    Which index trips first depends on timing, so the race is repeated with a new invoice each
    round. A retried post whose twin is still in flight is the same request, not a second
    entry for the same invoice.
    """
    rounds = 40
    for number in range(rounds):
        entry = request(f"post:race-{number}", invoice_number=f"RACE-{number:03d}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _, e=entry: store.post_entry(e), range(8)))
        assert len({r.entry_id for r in results}) == 1
        assert sum(not r.replayed for r in results) == 1
    assert count(engine, tables.entries) == rounds


def test_a_different_request_for_a_booked_invoice_is_still_refused(store: LedgerStore) -> None:
    store.post_entry(request("post:first"))
    with pytest.raises(LedgerRefusal) as refusal:
        store.post_entry(request("post:second"))  # another key, same invoice: a second booking
    assert refusal.value.code is ErrorCode.INVOICE_ALREADY_BOOKED


def test_a_twin_that_won_the_invoice_index_first_is_replayed_not_refused(
    store: LedgerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The losing side of the race, made deterministic.

    The entry of the invoice exists (the winner committed) but the loser's first read of its key
    saw nothing; its insert then trips the invoice index. It must read the key again and replay.
    """
    from uuid import uuid4

    from ledger.store import request_fingerprint

    winner = store.post_entry(request("post:twin"))
    twin_request = request("post:twin")
    reads = {"n": 0}

    def late_reader(_connection: object, _key: str) -> tuple[object, str] | None:
        reads["n"] += 1
        if reads["n"] == 1:
            return None  # the read before the winner's commit was visible
        return winner.entry_id, request_fingerprint(twin_request)

    monkeypatch.setattr(LedgerStore, "_find_by_key", staticmethod(late_reader))
    # A key the table has not seen, so that only the invoice index can object to the insert.
    posted = store.post_entry(request(f"post:{uuid4()}"))
    assert posted.replayed and posted.entry_id == winner.entry_id and reads["n"] == 2
