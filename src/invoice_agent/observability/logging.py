"""Structured JSON logs. One object per line, so any log platform can index the fields."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from invoice_agent.domain.audit import redact_payload


class JsonFormatter(logging.Formatter):
    """Format a record as a JSON line: time, level, logger, event and the extra fields.

    Strings go through the IBAN redaction first: invoice text can end up in an error message,
    and bank details must never reach the logs.
    """

    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] = getattr(record, "fields", {})
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **fields,
        }
        if record.exc_info:
            entry["exception"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        plain = json.loads(json.dumps(entry, default=str))
        return json.dumps(redact_payload(plain), ensure_ascii=False)


def log_event(
    logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any
) -> None:
    logger.log(level, event, extra={"fields": fields})


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
