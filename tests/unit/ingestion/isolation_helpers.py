"""Top-level functions run in child processes by the isolation tests (they are pickled by name)."""

import os
import time

from invoice_agent.ingestion.document import ExtractionError


def add(a: int, b: int) -> int:
    return a + b


def sleep_forever() -> None:
    time.sleep(3600)


def fail_extraction() -> None:
    raise ExtractionError("the document is bad")


def fail_unexpectedly() -> None:
    raise KeyError("sk-secret-key-in-a-message")


def exit_hard() -> None:
    os._exit(7)


def allocate(megabytes: int) -> int:
    return len(bytearray(megabytes * 1024 * 1024))
