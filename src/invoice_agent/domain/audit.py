"""Audit trail: an append-only record of every step of an agent run."""

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import JsonValue, model_validator

from invoice_agent.domain.base import DomainModel
from invoice_agent.domain.iban import contains_iban
from invoice_agent.domain.types import NonEmptyStr, UtcDatetime


class ActorType(StrEnum):
    SYSTEM = "system"
    AGENT = "agent"
    TOOL = "tool"
    HUMAN = "human"


class AuditEvent(DomainModel):
    id: UUID
    run_id: UUID
    step: NonEmptyStr
    actor_type: ActorType
    actor_id: NonEmptyStr
    invoice_id: UUID | None = None
    payload: dict[str, JsonValue]
    """JSON-serialisable. Decimals go in as strings; full IBANs are refused."""
    occurred_at: UtcDatetime

    @model_validator(mode="after")
    def _payload_has_no_full_iban(self) -> Self:
        if _contains_iban(self.payload):
            raise ValueError("payload must not contain a full IBAN, store the masked form")
        return self


def _contains_iban(value: JsonValue) -> bool:
    match value:
        case str():
            return contains_iban(value)
        case list():
            return any(_contains_iban(item) for item in value)
        case dict():
            return any(_contains_iban(key) or _contains_iban(item) for key, item in value.items())
        case _:
            return False
