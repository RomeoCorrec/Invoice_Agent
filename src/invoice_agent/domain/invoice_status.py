"""Invoice lifecycle: statuses and the transitions allowed between them."""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class InvoiceStatus(StrEnum):
    RECEIVED = "received"
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    POSTED = "posted"
    FAILED = "failed"


S = InvoiceStatus

ALLOWED_TRANSITIONS: Mapping[InvoiceStatus, frozenset[InvoiceStatus]] = MappingProxyType(
    {
        S.RECEIVED: frozenset({S.EXTRACTED, S.FAILED}),
        S.EXTRACTED: frozenset({S.VALIDATED, S.NEEDS_REVIEW, S.FAILED}),
        # A human always decides, even when every check passed.
        S.VALIDATED: frozenset({S.APPROVED, S.REJECTED, S.FAILED}),
        S.NEEDS_REVIEW: frozenset({S.APPROVED, S.REJECTED, S.FAILED}),
        S.APPROVED: frozenset({S.POSTED, S.FAILED}),
        # Retry means reprocessing from the start: re-posting is safe because the
        # journal entry carries an idempotency key. A direct failed -> approved
        # would let received -> failed -> approved skip validation.
        S.FAILED: frozenset({S.RECEIVED}),
        S.POSTED: frozenset(),
        S.REJECTED: frozenset(),
    }
)

TERMINAL_STATUSES: frozenset[InvoiceStatus] = frozenset(
    status for status, targets in ALLOWED_TRANSITIONS.items() if not targets
)


class InvalidStatusTransition(ValueError):
    def __init__(self, current: InvoiceStatus, target: InvoiceStatus) -> None:
        super().__init__(f"invoice cannot go from {current.value!r} to {target.value!r}")
        self.current = current
        self.target = target


def can_transition(current: InvoiceStatus, target: InvoiceStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def check_transition(current: InvoiceStatus, target: InvoiceStatus) -> None:
    """Raise InvalidStatusTransition unless ``current -> target`` is allowed."""
    if not can_transition(current, target):
        raise InvalidStatusTransition(current, target)
