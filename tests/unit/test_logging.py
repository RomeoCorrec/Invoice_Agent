import json
import logging

from invoice_agent.observability.logging import JsonFormatter, log_event


def render(**fields: object) -> dict[str, object]:
    logger = logging.getLogger("test.logging")
    record = logger.makeRecord(
        "test.logging", logging.INFO, __file__, 1, "job_done", (), None, extra={"fields": fields}
    )
    return json.loads(JsonFormatter().format(record))  # type: ignore[no-any-return]


def test_a_record_is_one_json_object_with_its_fields() -> None:
    entry = render(job_id="j1", attempt=2, duration_ms=12.5)
    assert entry["event"] == "job_done"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "test.logging"
    assert (entry["job_id"], entry["attempt"], entry["duration_ms"]) == ("j1", 2, 12.5)
    assert "ts" in entry


def test_values_that_are_not_json_are_stringified_not_dropped() -> None:
    from decimal import Decimal
    from uuid import UUID

    entry = render(amount=Decimal("1.50"), id=UUID(int=1))
    assert entry["amount"] == "1.50"
    assert entry["id"] == "00000000-0000-0000-0000-000000000001"


def test_a_full_iban_never_reaches_the_logs() -> None:
    entry = render(error="rejected payment to FR76-9905-3965-2635-8580-3695-442 today")
    # The whole line also holds the timestamp, whose microseconds can spell "9905" (they did, on
    # CI): look for a fragment too long to be in a time, and for the short one only where the
    # message is.
    assert "99053965" not in json.dumps(entry)
    assert "9905" not in str(entry["error"])
    assert "[IBAN redacted]" in str(entry["error"])


def test_log_event_attaches_fields(caplog: object) -> None:
    import pytest

    assert isinstance(caplog, pytest.LogCaptureFixture)
    with caplog.at_level(logging.INFO, logger="test.event"):
        log_event(logging.getLogger("test.event"), "started", worker="w1")
    assert caplog.records[0].fields == {"worker": "w1"}  # type: ignore[attr-defined]
