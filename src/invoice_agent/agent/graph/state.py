"""State of an agent run and the human decision that resumes it.

The state holds identifiers and small results, never the text of a document: the checkpointer
stores it in the database, and invoice content is untrusted input.
"""

from enum import StrEnum
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Approver = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class Verdict(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalDecision(BaseModel):
    """What a human answers when the run asks for approval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Verdict
    approver: Approver
    comment: str | None = Field(default=None, max_length=1000)


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
    decision: dict[str, Any]
    """An ``ApprovalDecision`` dumped to JSON."""
