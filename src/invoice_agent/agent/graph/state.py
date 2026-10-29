"""State of an agent run and the human decision that resumes it.

The state holds identifiers and small results, never the text of a document: the checkpointer
stores it in the database, and invoice content is untrusted input.
"""

from enum import StrEnum
from typing import Annotated, Any, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from invoice_agent.domain.iban import redact_ibans

Approver = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class PauseKind(StrEnum):
    """What a paused run is waiting for. Every interrupt payload carries one, as ``kind``.

    The human task inbox of the web interface lists the pauses by kind, and an answer is only
    accepted for the kind it was asked for (ADR-0014).
    """

    APPROVAL = "approval"
    """Approve or reject an invoice."""
    REVIEW_DRAFT = "review_draft"
    """Read, edit and send (or discard) a message to the supplier."""
    AWAIT_REPLY = "await_reply"
    """Nobody to answer: the supplier's reply, or the deadline, resumes the run."""
    REVIEW_REPLY = "review_reply"
    """Read the supplier's reply and decide what it settles."""


class Verdict(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REJECT_AND_QUERY = "reject_and_query"
    """Reject, and ask the supplier for a correction (the correspondence subgraph)."""


class ApprovalDecision(BaseModel):
    """What a human answers when the run asks for approval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Verdict
    approver: Approver
    comment: str | None = Field(default=None, max_length=1000)

    @field_validator("approver", "comment")
    @classmethod
    def _no_bank_details(cls, value: str | None) -> str | None:
        """A decision is stored in the checkpoint as well as in the audit log: redact bank
        details here, before it goes anywhere, not at each place that writes it."""
        return redact_ibans(value) if value is not None else None


class AgentState(TypedDict, total=False):
    document: str
    """Name of the document in the store; the idempotency key of the whole run."""
    run_id: str
    """Also the checkpoint thread id and the run id of every audit event."""
    trace_id: str | None
    invoice_id: str
    status: str
    """The invoice status after the last node, as a string (JSON-friendly)."""
    anomaly_codes: list[str]
    explanation: dict[str, Any] | None
    """What the model wrote about the anomalies (an ``Explanation`` as JSON), for the reviewer."""
    entry: dict[str, Any] | None
    """The proposed entry (an ``EntryProposal`` dumped to JSON), if one could be built."""
    entry_blocked: dict[str, Any] | None
    """Why no entry could be built (an ``EntryBlocked`` dumped to JSON)."""
    decision: dict[str, Any]
    """An ``ApprovalDecision`` dumped to JSON."""
    query: dict[str, Any]
    """Where the correspondence with the supplier stands: its status, round and thread."""


def run_id_of(state: AgentState) -> UUID:
    return UUID(state["run_id"])


def invoice_id_of(state: AgentState) -> UUID:
    return UUID(state["invoice_id"])
