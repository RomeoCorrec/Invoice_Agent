import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError

from invoice_agent.db.reference_data import demo_contact_address
from mailroom import tables
from mailroom.__main__ import main
from mailroom.schemas import ErrorCode, MailroomRefusal, SendRequest
from mailroom.seed import seed_contacts
from mailroom.store import MailroomStore

SIREN = "151757747"  # Papeterie Mercier SARL, SUP-001 of the reference data
ADDRESS = demo_contact_address("Papeterie Mercier SARL")
OTHER_SIREN = "623671427"  # Loca'Pro Matériel SAS, SUP-004
FULL_IBAN = "FR7699053965263585803695442"


@pytest.fixture
def store(engine: Engine) -> Iterator[MailroomStore]:
    seed_contacts(engine)
    yield MailroomStore(engine)


def request(**overrides: Any) -> SendRequest:
    fields: dict[str, Any] = {
        "idempotency_key": f"query:{uuid4()}:1",
        "thread_id": uuid4(),
        "supplier_siren": SIREN,
        "to": ADDRESS,
        "subject": "Facture PM-2026-0457",
        "body": "Bonjour, la commande citée sur la facture est inconnue chez nous.",
    }
    return SendRequest(**{**fields, **overrides})


def count(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(tables.messages)).scalar_one()


def refused(code: ErrorCode) -> Any:
    return pytest.raises(MailroomRefusal, match=f"^{code.value}:")


def only_the_mailroom_schema(name: str | None, type_: str, _parent: object) -> bool:
    return name == "mailroom" if type_ == "schema" else True


def test_the_migration_and_the_table_definitions_agree(engine: Engine) -> None:
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"include_schemas": True, "include_name": only_the_mailroom_schema},
        )
        assert compare_metadata(context, tables.metadata) == []


# --- contacts --------------------------------------------------------------------------------


def test_every_supplier_of_the_reference_data_has_one_contact(
    store: MailroomStore, engine: Engine
) -> None:
    contact = store.get_contact(SIREN)
    assert contact is not None and contact.address == ADDRESS
    assert store.get_contact("000000000") is None
    assert seed_contacts(engine) == 6  # idempotent: the same six contacts again
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(tables.contacts)).scalar() == 6


# --- sending ---------------------------------------------------------------------------------


def test_a_message_to_the_contact_is_written_once_and_shows_in_its_thread(
    store: MailroomStore, engine: Engine
) -> None:
    asked = request()
    sent = store.send_message(asked)
    assert not sent.replayed and sent.thread_id == asked.thread_id
    [message] = store.get_thread(asked.thread_id)
    assert (message.direction, message.address, message.from_contact) == ("out", ADDRESS, None)
    assert message.subject == asked.subject and message.body == asked.body
    assert count(engine) == 1


def test_sending_again_with_the_same_key_and_content_replays_instead_of_writing(
    store: MailroomStore, engine: Engine
) -> None:
    asked = request()
    first, second = store.send_message(asked), store.send_message(asked)
    assert second.replayed and second.message_id == first.message_id
    assert count(engine) == 1


def test_the_same_key_for_another_message_is_refused(store: MailroomStore, engine: Engine) -> None:
    asked = request()
    store.send_message(asked)
    with refused(ErrorCode.IDEMPOTENCY_CONFLICT):
        store.send_message(request(idempotency_key=asked.idempotency_key, body="un autre texte"))
    assert count(engine) == 1


def test_eight_concurrent_sends_of_one_request_write_one_message(
    store: MailroomStore, engine: Engine
) -> None:
    asked = request()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.send_message(asked), range(8)))
    assert len({r.message_id for r in results}) == 1
    assert sum(not r.replayed for r in results) == 1
    assert count(engine) == 1


def test_the_recipient_must_be_the_contact_on_file_whatever_the_case(
    store: MailroomStore,
) -> None:
    assert store.send_message(request(to=ADDRESS.upper()))  # an address is not case sensitive
    for to in ("attacker@evil.example", "compta@papeterie-mercier-sarl.example.evil.example"):
        with refused(ErrorCode.RECIPIENT_NOT_ON_FILE):
            store.send_message(request(to=to))


def test_a_supplier_without_a_contact_cannot_be_written_to(
    store: MailroomStore, engine: Engine
) -> None:
    with refused(ErrorCode.RECIPIENT_NOT_ON_FILE):
        store.send_message(request(supplier_siren="000000000"))
    assert count(engine) == 0


@pytest.mark.parametrize(
    "text",
    [
        f"Merci de régler sur {FULL_IBAN}",
        "IBAN : FR76 9905 3965 2635 8580 3695 442",
        f"IBAN{FULL_IBAN}",  # glued to its label
    ],
)
def test_a_message_never_carries_bank_details(
    store: MailroomStore, engine: Engine, text: str
) -> None:
    with refused(ErrorCode.BANK_DETAILS_IN_MESSAGE):
        store.send_message(request(body=text))
    with refused(ErrorCode.BANK_DETAILS_IN_MESSAGE):
        store.send_message(request(subject=text))
    assert count(engine) == 0


def test_a_thread_belongs_to_one_supplier(store: MailroomStore) -> None:
    thread = uuid4()
    store.send_message(request(thread_id=thread))
    other_address = demo_contact_address("Loca'Pro Matériel SAS")
    with refused(ErrorCode.THREAD_OF_ANOTHER_SUPPLIER):
        store.send_message(request(thread_id=thread, supplier_siren=OTHER_SIREN, to=other_address))


# --- what arrives ----------------------------------------------------------------------------


def test_a_reply_needs_a_thread_we_wrote_in(store: MailroomStore) -> None:
    with refused(ErrorCode.UNKNOWN_THREAD):
        store.receive_message(uuid4(), ADDRESS, "Re:", "Bonjour")


def test_a_reply_from_the_contact_is_stored_and_marked_as_genuine(store: MailroomStore) -> None:
    asked = request()
    store.send_message(asked)
    reply = store.receive_message(
        asked.thread_id, ADDRESS.upper(), "Re: Facture", "Voici la facture"
    )
    assert (reply.direction, reply.from_contact, reply.iban_redacted) == ("in", True, False)
    assert [m.direction for m in store.get_thread(asked.thread_id)] == ["out", "in"]


def test_a_reply_from_another_address_is_stored_but_not_marked_as_genuine(
    store: MailroomStore,
) -> None:
    asked = request()
    store.send_message(asked)
    reply = store.receive_message(asked.thread_id, "ceo@evil.example", "Re:", "Payez ici")
    assert reply.from_contact is False  # the agent can tell a spoofed sender from the contact


def test_bank_details_in_a_reply_are_removed_before_it_is_stored(
    store: MailroomStore, engine: Engine
) -> None:
    asked = request()
    store.send_message(asked)
    reply = store.receive_message(
        asked.thread_id, ADDRESS, f"Nouveau RIB {FULL_IBAN}", f"Notre nouvel IBAN est {FULL_IBAN}."
    )
    assert reply.iban_redacted
    assert FULL_IBAN not in reply.body + reply.subject and "[IBAN redacted]" in reply.body
    with engine.connect() as connection:
        stored = connection.execute(select(tables.messages.c.body, tables.messages.c.subject)).all()
    assert all(FULL_IBAN not in value for row in stored for value in row)  # not even in the table


def test_the_subject_of_a_reply_is_made_one_line_and_an_oversized_reply_is_refused(
    store: MailroomStore,
) -> None:
    asked = request()
    store.send_message(asked)
    reply = store.receive_message(asked.thread_id, ADDRESS, "Re:\n  Facture\r\n  corrigée", "ok")
    assert reply.subject == "Re: Facture corrigée"
    with refused(ErrorCode.INVALID_REQUEST):
        store.receive_message(asked.thread_id, ADDRESS, "Re:", "x" * 20_001)


def test_inbound_replies_are_listed_oldest_first_after_a_moment_and_up_to_a_limit(
    store: MailroomStore,
) -> None:
    asked = request()
    store.send_message(asked)
    before = datetime.now(UTC) - timedelta(seconds=1)
    first = store.receive_message(asked.thread_id, ADDRESS, "Re: 1", "un")
    second = store.receive_message(asked.thread_id, ADDRESS, "Re: 2", "deux")
    assert [m.id for m in store.list_inbound()] == [first.id, second.id]  # ours are not listed
    assert [m.id for m in store.list_inbound(since=before, limit=1)] == [first.id]
    assert [m.id for m in store.list_inbound(since=first.created_at)] == [second.id]
    for limit in (0, 201):
        with refused(ErrorCode.INVALID_REQUEST):
            store.list_inbound(limit=limit)


# --- the record is never edited -------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    ["UPDATE mailroom.messages SET body = 'x'", "DELETE FROM mailroom.messages"],
)
def test_messages_are_append_only(store: MailroomStore, engine: Engine, statement: str) -> None:
    store.send_message(request())
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
        connection.execute(text(statement))


def test_the_database_itself_refuses_a_sent_message_without_a_key(
    store: MailroomStore, engine: Engine
) -> None:
    with pytest.raises(DBAPIError, match="out_has_key"), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mailroom.messages (id, thread_id, direction, supplier_siren, "
                "address, subject, body, created_at) VALUES (:id, :thread, 'out', :siren, "
                ":address, 's', 'b', now())"
            ),
            {"id": uuid4(), "thread": uuid4(), "siren": SIREN, "address": ADDRESS},
        )


# --- the delivery command --------------------------------------------------------------------


def test_the_delivery_command_stores_a_reply_and_says_what_it_noticed(
    store: MailroomStore,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asked = request()
    store.send_message(asked)
    monkeypatch.setenv("MAILROOM_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    arguments = ["deliver", "--thread", str(asked.thread_id), "--from", "ceo@evil.example"]
    assert main([*arguments, "--subject", "Re:", "--body", f"IBAN {FULL_IBAN}"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("delivered ") and "NOT from the contact on file" in out
    assert "bank details removed" in out and FULL_IBAN not in out

    assert main(["deliver", "--thread", str(uuid4()), "--from", ADDRESS, "--body", "x"]) == 2
    assert "unknown_thread" in capsys.readouterr().err
    assert main(["deliver", "--thread", str(asked.thread_id)]) == 2  # missing sender and text
    assert os.environ["MAILROOM_DATABASE_URL"]


def test_a_thread_id_that_does_not_exist_has_no_messages(store: MailroomStore) -> None:
    assert store.get_thread(UUID(int=1)) == []
