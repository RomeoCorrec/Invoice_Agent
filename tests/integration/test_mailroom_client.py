from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select

from invoice_agent.db.reference_data import demo_contact_address
from invoice_agent.tools.errors import ToolRefusedError
from invoice_agent.tools.mailroom import (
    MailroomConnection,
    MailroomReader,
    MailroomWriter,
    mailroom_stdio_server,
)
from mailroom import tables
from mailroom.schemas import SendRequest
from mailroom.seed import seed_contacts
from mailroom.server import build_server
from mailroom.store import MailroomStore

SIREN = "151757747"
ADDRESS = demo_contact_address("Papeterie Mercier SARL")
FULL_IBAN = "FR7699053965263585803695442"


@pytest.fixture
def seeded(engine: Engine) -> Iterator[Engine]:
    seed_contacts(engine)
    yield engine


def message(**overrides: Any) -> SendRequest:
    fields: dict[str, Any] = {
        "idempotency_key": f"query:{uuid4()}:1",
        "thread_id": uuid4(),
        "supplier_siren": SIREN,
        "to": ADDRESS,
        "subject": "Facture PM-2026-0457 : demande de correction",
        "body": "Bonjour, la commande citée est inconnue chez nous. Cordialement.",
    }
    return SendRequest(**{**fields, **overrides})


def stored(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(tables.messages)).scalar_one()


async def test_the_writer_sends_once_and_refusals_keep_their_code(seeded: Engine) -> None:
    async with MailroomConnection(build_server(MailroomStore(seeded))) as conn:
        writer, reader = MailroomWriter(conn), MailroomReader(conn)
        asked = message()
        first = await writer.send_message(asked)
        again = await writer.send_message(asked)
        assert (first.replayed, again.replayed) == (False, True)
        assert again.message_id == first.message_id and stored(seeded) == 1
        [sent] = await reader.get_thread(asked.thread_id)
        assert sent.direction == "out" and sent.address == ADDRESS

        for refused, code in (
            (
                message(idempotency_key=asked.idempotency_key, body="un autre texte"),
                "idempotency_conflict",
            ),
            (message(to="attacker@evil.example"), "recipient_not_on_file"),
            (message(body=f"Payez sur {FULL_IBAN}"), "bank_details_in_message"),
            (message(supplier_siren="000000000"), "recipient_not_on_file"),
        ):
            with pytest.raises(ToolRefusedError) as refusal:
                await writer.send_message(refused)
            assert refusal.value.code == code
        assert stored(seeded) == 1


async def test_the_reader_returns_the_contact_the_thread_and_the_replies(seeded: Engine) -> None:
    store = MailroomStore(seeded)
    async with MailroomConnection(build_server(store)) as conn:
        reader, writer = MailroomReader(conn), MailroomWriter(conn)
        contact = await reader.get_contact(SIREN)
        assert contact is not None and contact.address == ADDRESS
        assert await reader.get_contact("000000000") is None

        asked = message()
        await writer.send_message(asked)
        before = datetime.now(UTC) - timedelta(seconds=1)
        reply = store.receive_message(asked.thread_id, ADDRESS, "Re:", "Voici la facture corrigée")
        inbound = await reader.list_inbound(since=before)
        assert [m.id for m in inbound] == [reply.id] and inbound[0].from_contact is True
        assert await reader.list_inbound(since=reply.created_at) == []
        assert [m.direction for m in await reader.get_thread(asked.thread_id)] == ["out", "in"]


def public_methods(cls: type) -> set[str]:
    return {name for name in vars(cls) if not name.startswith("_")}


def test_each_facade_offers_only_its_own_tools() -> None:
    """Whoever is given a reader cannot write to anybody, and a writer cannot read."""
    assert public_methods(MailroomReader) == {"get_contact", "get_thread", "list_inbound"}
    assert public_methods(MailroomWriter) == {"send_message"}


async def test_a_read_only_server_refuses_the_writer(seeded: Engine) -> None:
    async with MailroomConnection(build_server(MailroomStore(seeded), writable=False)) as conn:
        with pytest.raises(ToolRefusedError) as refusal:
            await MailroomWriter(conn).send_message(message())
    assert refusal.value.code == "unknown_tool" and stored(seeded) == 0


async def test_a_real_mailroom_subprocess_over_stdio(seeded: Engine, postgres_url: str) -> None:
    async with MailroomConnection(mailroom_stdio_server(postgres_url)) as conn:
        reader, writer = MailroomReader(conn), MailroomWriter(conn)
        assert (await reader.get_contact(SIREN)) is not None
        asked = message()
        first = await writer.send_message(asked)
        again = await writer.send_message(asked)
        assert first.message_id == again.message_id and again.replayed
    assert stored(seeded) == 1


async def test_a_read_only_subprocess_cannot_send(seeded: Engine, postgres_url: str) -> None:
    async with MailroomConnection(mailroom_stdio_server(postgres_url, read_only=True)) as conn:
        assert await MailroomReader(conn).get_contact(SIREN) is not None
        with pytest.raises(ToolRefusedError) as refusal:
            await MailroomWriter(conn).send_message(message())
    assert refusal.value.code == "unknown_tool" and stored(seeded) == 0
