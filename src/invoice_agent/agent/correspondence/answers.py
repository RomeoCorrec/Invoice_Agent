"""What a person, or an event, answers when the correspondence pauses (ADR-0014).

Each pause of the subgraph has its own answer, validated when it is built: a draft that is
edited is still one line of subject and a bounded body, a note never carries bank details (it
is stored in the checkpoint and the audit log), and a reply that ends the wait names the
message it is about.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from invoice_agent.agent.graph.state import Approver
from invoice_agent.domain.iban import redact_ibans
from mailroom.schemas import Body, Subject


class _Answer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DraftReview(_Answer):
    """The answer to ``review_draft``: send the message (as it is, or as edited) or drop it."""

    action: Literal["send", "discard"]
    approver: Approver
    subject: Subject | None = None
    body: Body | None = None
    """An edited text replaces the draft's: both together, or neither."""

    @model_validator(mode="after")
    def _edits_come_together_and_only_with_a_send(self) -> Self:
        if (self.subject is None) != (self.body is None):
            raise ValueError("an edited draft needs both a subject and a body")
        if self.action == "discard" and self.subject is not None:
            raise ValueError("a draft that is discarded cannot be edited")
        return self


class WaitEnded(_Answer):
    """The answer to ``await_reply``: a reply arrived, or the deadline passed."""

    event: Literal["reply", "timeout"]
    message_id: UUID | None = None

    @model_validator(mode="after")
    def _a_reply_names_its_message(self) -> Self:
        if (self.event == "reply") != (self.message_id is not None):
            raise ValueError("a reply names its message, a timeout does not")
        return self


class ReplyReview(_Answer):
    """The answer to ``review_reply``: what the reply settles."""

    action: Literal["resolved", "ask_again", "give_up"]
    approver: Approver
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("note")
    @classmethod
    def _no_bank_details(cls, value: str | None) -> str | None:
        return redact_ibans(value) if value is not None else None
