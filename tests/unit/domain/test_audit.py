from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from invoice_agent.domain.audit import ActorType, AuditEvent


def make_event(payload: dict[str, object]) -> AuditEvent:
    return AuditEvent(
        id=uuid4(),
        run_id=uuid4(),
        step="extract",
        actor_type=ActorType.AGENT,
        actor_id="extractor-v1",
        payload=payload,  # type: ignore[arg-type]
        occurred_at=datetime(2026, 9, 17, 9, 0, tzinfo=UTC),
    )


def test_event_with_json_payload() -> None:
    event = make_event({"total_ttc": "639.60", "anomalies": ["PO_NOT_FOUND"], "attempt": 1})
    assert event.payload["anomalies"] == ["PO_NOT_FOUND"]


def test_payload_must_be_json_serializable() -> None:
    with pytest.raises(ValidationError):
        make_event({"total": Decimal("1.00")})


@pytest.mark.parametrize(
    "payload",
    [
        {"iban": "FR7699053965263585803695442"},
        {"note": "please pay FR76 9905 3965 2635 8580 3695 442 now"},
        {"nested": {"deep": ["x", {"iban": "FR7699053965263585803695442"}]}},
    ],
)
def test_full_iban_in_payload_is_refused(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="IBAN"):
        make_event(payload)


def test_masked_iban_is_fine() -> None:
    assert make_event({"iban": "FR76 **** 5442"}).payload["iban"] == "FR76 **** 5442"


def test_timestamp_is_utc() -> None:
    event = AuditEvent(
        id=uuid4(),
        run_id=uuid4(),
        step="approve",
        actor_type=ActorType.HUMAN,
        actor_id="user@example.com",
        payload={},
        occurred_at=datetime(2026, 9, 17, 11, 0, tzinfo=timezone(timedelta(hours=2))),
    )
    assert event.occurred_at == datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    assert event.occurred_at.utcoffset() == timedelta(0)


def test_redact_payload_cleans_every_string_so_the_event_can_be_built() -> None:
    from invoice_agent.domain.audit import redact_payload

    raw = {
        "message": "pay FR76 9905 3965 2635 8580 3695 442 today",
        "nested": [{"note": "FR76-9905-3965-2635-8580-3695-442"}, 3, None],
    }
    redacted = redact_payload(raw)
    assert "[IBAN redacted]" in str(redacted)
    assert make_event(redacted)  # type: ignore[arg-type]
