"""What the mailroom's tools accept and return.

These models are the contract between the MCP server and its clients, and the only thing the
two share. A request that cannot be an honest one (a subject with a line break, several
recipients, a body without text) is refused when it is built.
"""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from invoice_agent.domain.types import EmailAddress, NonEmptyStr, Siren, UtcDatetime

# One line, so nothing can be smuggled into a header through it.
Subject = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\r\n]+$"),
]
Body = Annotated[str, StringConstraints(min_length=1, max_length=5000)]

MAX_INBOUND_BODY = 20_000
MAX_LISTED = 200


class MailroomModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ErrorCode(StrEnum):
    """Why the mailroom refused a request. It is the prefix of the error message."""

    RECIPIENT_NOT_ON_FILE = "recipient_not_on_file"
    BANK_DETAILS_IN_MESSAGE = "bank_details_in_message"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    THREAD_OF_ANOTHER_SUPPLIER = "thread_of_another_supplier"
    UNKNOWN_THREAD = "unknown_thread"
    INVALID_REQUEST = "invalid_request"


class MailroomRefusal(Exception):
    """A request the mailroom understood and refused. The code is stable; the text is for people."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code


class ContactInfo(MailroomModel):
    supplier_siren: Siren
    address: EmailAddress


class SendRequest(MailroomModel):
    """The only write the mailroom accepts: one message to the contact of one supplier."""

    idempotency_key: NonEmptyStr = Field(max_length=200)
    thread_id: UUID
    supplier_siren: Siren
    to: EmailAddress
    subject: Subject
    body: Body


class SentMessage(MailroomModel):
    message_id: UUID
    thread_id: UUID
    idempotency_key: NonEmptyStr
    replayed: bool
    """True when the same request had already been sent: nothing was written this time."""


class MessageInfo(MailroomModel):
    id: UUID
    thread_id: UUID
    direction: Literal["out", "in"]
    supplier_siren: Siren
    address: str
    """The recipient of a message we sent, the sender of a reply."""
    subject: str
    body: str
    created_at: UtcDatetime
    from_contact: bool | None
    """For a reply: whether it comes from the address on file for the supplier. Otherwise None."""
    iban_redacted: bool
    """A reply carried bank details: they were removed before it was stored."""
