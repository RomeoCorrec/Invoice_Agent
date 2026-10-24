"""Background jobs: model, lifecycle and retry policy. Pure Python, no I/O.

Lifecycle::

    queued -> running -> succeeded
                      -> queued   (transient failure, retry later with backoff)
                      -> dead     (attempts exhausted, or a permanent failure)
    running -> queued             (the lease expired: its worker is presumed dead)
"""

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Self
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from invoice_agent.domain.base import DomainModel
from invoice_agent.domain.types import NonEmptyStr, UtcDatetime


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD = "dead"


J = JobStatus

ALLOWED_TRANSITIONS: Mapping[JobStatus, frozenset[JobStatus]] = MappingProxyType(
    {
        J.QUEUED: frozenset({J.RUNNING}),
        J.RUNNING: frozenset({J.SUCCEEDED, J.QUEUED, J.DEAD}),
        J.SUCCEEDED: frozenset(),
        J.DEAD: frozenset(),
    }
)


class InvalidJobTransition(ValueError):
    def __init__(self, current: JobStatus, target: JobStatus) -> None:
        super().__init__(f"job cannot go from {current.value!r} to {target.value!r}")
        self.current = current
        self.target = target


def check_job_transition(current: JobStatus, target: JobStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidJobTransition(current, target)


class TransientJobError(RuntimeError):
    """The job may succeed later (database or provider hiccup, timeout): retry with backoff."""


class PermanentJobError(RuntimeError):
    """Retrying cannot help (unknown job kind, malformed payload): the job goes to dead."""


class Job(DomainModel):
    id: UUID
    kind: NonEmptyStr
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    status: JobStatus = JobStatus.QUEUED
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1)
    run_after: UtcDatetime
    locked_by: NonEmptyStr | None = None
    locked_until: UtcDatetime | None = None
    last_error: str | None = None
    idempotency_key: NonEmptyStr | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        running = self.status is JobStatus.RUNNING
        if running and (self.locked_by is None or self.locked_until is None):
            raise ValueError("a running job needs a worker and a lease")
        if not running and (self.locked_by is not None or self.locked_until is not None):
            raise ValueError("only a running job can hold a lease")
        if self.attempts > self.max_attempts:
            raise ValueError("attempts cannot exceed max_attempts")
        return self


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with a cap and full jitter."""

    max_attempts: int = 5
    base: timedelta = timedelta(seconds=5)
    factor: float = 2.0
    cap: timedelta = timedelta(minutes=10)

    def ceiling(self, attempt: int) -> timedelta:
        """Longest possible delay after the ``attempt``-th failure (1-based)."""
        if attempt < 1:
            raise ValueError("attempt starts at 1")
        return min(self.cap, self.base * self.factor ** (attempt - 1))

    def delay(self, attempt: int, rng: Callable[[], float] = random.random) -> timedelta:
        """A random delay in [ceiling/2, ceiling]: spread out, but never an instant retry."""
        ceiling = self.ceiling(attempt)
        return ceiling / 2 + (ceiling / 2) * rng()
