from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, select

from invoice_agent.tools.errors import ToolRefusedError
from invoice_agent.tools.ledger import (
    LedgerConnection,
    LedgerReader,
    LedgerWriter,
    ledger_stdio_server,
)
from ledger import tables
from ledger.seed import seed_reference
from ledger.server import build_server
from ledger.store import LedgerStore
from tests.unit.ledger.test_schemas import lines, request


@pytest.fixture
def seeded(engine: Engine) -> Iterator[Engine]:
    seed_reference(engine)
    yield engine


def entries(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(tables.entries)).scalar_one()


async def test_the_reader_returns_typed_models(seeded: Engine) -> None:
    async with LedgerConnection(build_server(LedgerStore(seeded))) as conn:
        reader = LedgerReader(conn)
        supplier = await reader.get_supplier(siren="151757747")
        assert supplier is not None and supplier.supplier_account == "401MERC"
        assert await reader.get_supplier(siren="000000000") is None
        [first, *_] = await reader.list_open_purchase_orders("SUP-001")
        assert first.number == "PO-2026-0101" and first.total_ht == Decimal("300.00")
        assert await reader.get_purchase_order("PO-2026-0101") == first
        assert await reader.get_purchase_order("PO-NOPE") is None
        assert [a.number for a in await reader.get_chart_of_accounts("44")] == ["44566"]
        assert await reader.find_invoices("151757747") == []


async def test_the_writer_posts_once_and_refusals_keep_their_code(seeded: Engine) -> None:
    async with LedgerConnection(build_server(LedgerStore(seeded))) as conn:
        writer, reader = LedgerWriter(conn), LedgerReader(conn)
        first = await writer.post_entry(request())
        again = await writer.post_entry(request())
        assert (first.replayed, again.replayed) == (False, True)
        assert again.entry_id == first.entry_id and entries(seeded) == 1
        [booked] = await reader.find_invoices("151757747", "PM-2026-0457")
        assert booked.entry_id == first.entry_id and booked.total == Decimal("298.80")

        for entry, code in (
            (request(lines=lines("250.00", "50.00", "300.00")), "idempotency_conflict"),
            (request("post:2"), "invoice_already_booked"),
            (request("post:3", supplier_siren="000000000"), "unknown_supplier"),
        ):
            with pytest.raises(ToolRefusedError) as refusal:
                await writer.post_entry(entry)
            assert refusal.value.code == code
        assert entries(seeded) == 1


def public_methods(cls: type) -> set[str]:
    return {name for name in vars(cls) if not name.startswith("_")}


def test_each_facade_offers_only_its_own_tools() -> None:
    """Whoever is given a reader cannot post, and a writer cannot be used to look things up."""
    assert public_methods(LedgerReader) == {
        "get_supplier",
        "list_open_purchase_orders",
        "get_purchase_order",
        "find_invoices",
        "get_chart_of_accounts",
    }
    assert public_methods(LedgerWriter) == {"post_entry"}


async def test_a_read_only_server_refuses_the_writer(seeded: Engine) -> None:
    async with LedgerConnection(build_server(LedgerStore(seeded), writable=False)) as conn:
        with pytest.raises(ToolRefusedError) as refusal:
            await LedgerWriter(conn).post_entry(request())
    assert refusal.value.code == "unknown_tool" and entries(seeded) == 0


async def test_a_real_ledger_subprocess_over_stdio(seeded: Engine, postgres_url: str) -> None:
    async with LedgerConnection(ledger_stdio_server(postgres_url)) as conn:
        reader, writer = LedgerReader(conn), LedgerWriter(conn)
        supplier = await reader.get_supplier(vat_number="FR67151757747")
        assert supplier is not None and supplier.code == "SUP-001"
        first = await writer.post_entry(request())
        again = await writer.post_entry(request())
        assert first.entry_id == again.entry_id and again.replayed
    assert entries(seeded) == 1


async def test_a_read_only_subprocess_cannot_post(seeded: Engine, postgres_url: str) -> None:
    async with LedgerConnection(ledger_stdio_server(postgres_url, read_only=True)) as conn:
        assert await LedgerReader(conn).get_supplier(siren="151757747") is not None
        with pytest.raises(ToolRefusedError) as refusal:
            await LedgerWriter(conn).post_entry(request())
    assert refusal.value.code == "unknown_tool" and entries(seeded) == 0
