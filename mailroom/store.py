"""The simulated mail provider's storage and rules.

``send_message`` is idempotent: the same key with the same request returns the message already
written, the same key with another request is refused. What it refuses besides:

* a recipient that is not the address on file for the supplier (a document or a model cannot
  choose where a message goes, and neither can a mistake in the caller);
* bank details in the text: nothing that leaves the company carries an IBAN;
* a thread that already belongs to another supplier.

``receive_message`` is the provider's side, used by the delivery command, never by the agent:
what arrives is untrusted, so bank details are removed before it is stored and the sender is
compared with the address on file.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Column, Connection, Engine, Select, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping

from invoice_agent.domain.iban import contains_iban, redact_ibans
from mailroom import tables
from mailroom.schemas import (
    MAX_INBOUND_BODY,
    MAX_LISTED,
    ContactInfo,
    ErrorCode,
    MailroomRefusal,
    MessageInfo,
    SendRequest,
    SentMessage,
)

SUBJECT_LIMIT = 200


def request_fingerprint(request: SendRequest) -> str:
    """Hash of what the request says, without the key, to tell a replay from a different request."""
    body = request.model_dump(mode="json", exclude={"idempotency_key"})
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


class MailroomStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- reads ---------------------------------------------------------------------------

    def get_contact(self, supplier_siren: str) -> ContactInfo | None:
        with self._engine.connect() as connection:
            return self._contact(connection, supplier_siren)

    def get_thread(self, thread_id: UUID) -> list[MessageInfo]:
        query = self._messages().where(tables.messages.c.thread_id == thread_id)
        with self._engine.connect() as connection:
            rows = connection.execute(query.order_by(*self._order())).mappings().all()
        return [self._info(row) for row in rows]

    def list_inbound(self, since: datetime | None = None, limit: int = 50) -> list[MessageInfo]:
        """Replies received after ``since`` (all if None), oldest first."""
        if not 1 <= limit <= MAX_LISTED:
            raise MailroomRefusal(ErrorCode.INVALID_REQUEST, f"limit must be 1 to {MAX_LISTED}")
        query = self._messages().where(tables.messages.c.direction == "in")
        if since is not None:
            query = query.where(tables.messages.c.created_at > since)
        with self._engine.connect() as connection:
            rows = connection.execute(query.order_by(*self._order()).limit(limit)).mappings().all()
        return [self._info(row) for row in rows]

    # --- the write the agent may do -------------------------------------------------------

    def send_message(self, request: SendRequest) -> SentMessage:
        fingerprint = request_fingerprint(request)
        with self._engine.begin() as connection:
            existing = self._by_key(connection, request.idempotency_key)
            if existing is not None:
                return self._replay(request, existing, fingerprint)
            contact = self._contact(connection, request.supplier_siren)
            if contact is None or contact.address.lower() != request.to.lower():
                raise MailroomRefusal(
                    ErrorCode.RECIPIENT_NOT_ON_FILE,
                    "the recipient is not the contact on file for this supplier",
                )
            if contains_iban(f"{request.subject}\n{request.body}"):
                raise MailroomRefusal(
                    ErrorCode.BANK_DETAILS_IN_MESSAGE,
                    "a message must not carry bank details",
                )
            owner = self._thread_supplier(connection, request.thread_id)
            if owner is not None and owner != request.supplier_siren:
                raise MailroomRefusal(
                    ErrorCode.THREAD_OF_ANOTHER_SUPPLIER,
                    "this thread already belongs to another supplier",
                )
            message_id = uuid4()
            inserted = connection.execute(
                pg_insert(tables.messages)
                .values(
                    id=message_id,
                    thread_id=request.thread_id,
                    direction="out",
                    supplier_siren=request.supplier_siren,
                    address=contact.address,
                    subject=request.subject,
                    body=request.body,
                    idempotency_key=request.idempotency_key,
                    request_hash=fingerprint,
                    iban_redacted=False,
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(tables.messages.c.id)
            ).scalar_one_or_none()
            if inserted is None:  # another request used the key between our read and write
                raced = self._by_key(connection, request.idempotency_key)
                assert raced is not None
                return self._replay(request, raced, fingerprint)
        return SentMessage(
            message_id=message_id,
            thread_id=request.thread_id,
            idempotency_key=request.idempotency_key,
            replayed=False,
        )

    # --- the provider's side: not exposed to the agent --------------------------------------

    def receive_message(
        self, thread_id: UUID, from_address: str, subject: str, body: str
    ) -> MessageInfo:
        """Store a reply to a thread we wrote in. Bank details are removed first."""
        if len(body) > MAX_INBOUND_BODY:
            raise MailroomRefusal(
                ErrorCode.INVALID_REQUEST, f"a reply is limited to {MAX_INBOUND_BODY} characters"
            )
        clean_subject = re.sub(r"\s+", " ", subject).strip()[:SUBJECT_LIMIT] or "(no subject)"
        redacted_subject, redacted_body = redact_ibans(clean_subject), redact_ibans(body)
        iban_redacted = (redacted_subject, redacted_body) != (clean_subject, body)
        with self._engine.begin() as connection:
            supplier = self._thread_supplier(connection, thread_id)
            if supplier is None:
                raise MailroomRefusal(
                    ErrorCode.UNKNOWN_THREAD, "no message was sent in this thread"
                )
            message_id = uuid4()
            connection.execute(
                pg_insert(tables.messages).values(
                    id=message_id,
                    thread_id=thread_id,
                    direction="in",
                    supplier_siren=supplier,
                    address=from_address[:254],
                    subject=redacted_subject[:SUBJECT_LIMIT],
                    body=redacted_body,
                    iban_redacted=iban_redacted,
                    created_at=datetime.now(UTC),
                )
            )
        stored = [m for m in self.get_thread(thread_id) if m.id == message_id]
        return stored[0]

    # --- helpers ---------------------------------------------------------------------------

    @staticmethod
    def _order() -> tuple[Column[datetime], Column[UUID]]:
        return tables.messages.c.created_at, tables.messages.c.id

    @staticmethod
    def _messages() -> Select[tuple[object, ...]]:
        """Messages with the address on file of their supplier, to tell a genuine sender."""
        contact = tables.contacts.c.address.label("contact_address")
        return select(tables.messages, contact).join(
            tables.contacts,
            tables.contacts.c.supplier_siren == tables.messages.c.supplier_siren,
        )

    @staticmethod
    def _info(row: RowMapping) -> MessageInfo:
        inbound = row["direction"] == "in"
        return MessageInfo(
            id=row["id"],
            thread_id=row["thread_id"],
            direction=row["direction"],
            supplier_siren=row["supplier_siren"],
            address=row["address"],
            subject=row["subject"],
            body=row["body"],
            created_at=row["created_at"],
            from_contact=(row["address"].lower() == row["contact_address"].lower())
            if inbound
            else None,
            iban_redacted=row["iban_redacted"],
        )

    @staticmethod
    def _contact(connection: Connection, supplier_siren: str) -> ContactInfo | None:
        row = (
            connection.execute(
                select(tables.contacts).where(tables.contacts.c.supplier_siren == supplier_siren)
            )
            .mappings()
            .first()
        )
        return ContactInfo(**row) if row else None

    @staticmethod
    def _by_key(connection: Connection, key: str) -> tuple[UUID, UUID, str] | None:
        row = connection.execute(
            select(
                tables.messages.c.id, tables.messages.c.thread_id, tables.messages.c.request_hash
            ).where(tables.messages.c.idempotency_key == key)
        ).first()
        return (row[0], row[1], row[2]) if row else None

    @staticmethod
    def _thread_supplier(connection: Connection, thread_id: UUID) -> str | None:
        """The supplier a thread belongs to: the one of its first message, None for a new thread."""
        return connection.execute(
            select(tables.messages.c.supplier_siren)
            .where(tables.messages.c.thread_id == thread_id)
            .order_by(tables.messages.c.created_at, tables.messages.c.id)
            .limit(1)
        ).scalar_one_or_none()

    @staticmethod
    def _replay(
        request: SendRequest, existing: tuple[UUID, UUID, str], fingerprint: str
    ) -> SentMessage:
        message_id, thread_id, stored_hash = existing
        if stored_hash != fingerprint:
            raise MailroomRefusal(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "this idempotency key was already used for a different message",
            )
        return SentMessage(
            message_id=message_id,
            thread_id=thread_id,
            idempotency_key=request.idempotency_key,
            replayed=True,
        )
