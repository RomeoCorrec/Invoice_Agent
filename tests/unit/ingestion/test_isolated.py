import sys
import time

import pytest

from invoice_agent.config import IngestionLimits
from invoice_agent.ingestion.document import ExtractionError
from invoice_agent.ingestion.isolated import read_document_isolated, run_isolated
from invoice_agent.ingestion.reading import read_document
from tests.pdfs import golden_pdf
from tests.unit.ingestion import isolation_helpers as helpers

LIMITS = IngestionLimits()
TIMEOUT = 60  # generous: starting a spawned interpreter is slow on a busy machine


def test_the_result_comes_back_from_the_child() -> None:
    assert run_isolated(helpers.add, 2, 3, timeout_s=TIMEOUT) == 5


def test_a_function_that_never_returns_is_killed_at_the_deadline() -> None:
    started = time.monotonic()
    with pytest.raises(ExtractionError, match="took longer than"):
        run_isolated(helpers.sleep_forever, timeout_s=3, memory_limit=None)
    assert time.monotonic() - started < 40  # it did not wait for the hour-long sleep


def test_an_extraction_error_keeps_its_safe_message() -> None:
    with pytest.raises(ExtractionError, match="the document is bad"):
        run_isolated(helpers.fail_extraction, timeout_s=TIMEOUT)


def test_unexpected_errors_are_reported_by_type_only() -> None:
    with pytest.raises(ExtractionError) as error:
        run_isolated(helpers.fail_unexpectedly, timeout_s=TIMEOUT)
    assert "KeyError" in str(error.value)
    assert "secret" not in str(error.value)


def test_a_child_that_dies_is_reported_not_waited_for() -> None:
    with pytest.raises(ExtractionError, match="stopped unexpectedly"):
        run_isolated(helpers.exit_hard, timeout_s=TIMEOUT)


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS does not exist on Windows")
def test_the_child_cannot_allocate_beyond_its_memory_limit() -> None:
    with pytest.raises(ExtractionError, match="MemoryError"):
        run_isolated(helpers.allocate, 800, timeout_s=TIMEOUT, memory_limit=300 * 1024 * 1024)


def test_the_parent_is_unaffected_by_what_the_child_did() -> None:
    with pytest.raises(ExtractionError):
        run_isolated(helpers.exit_hard, timeout_s=TIMEOUT)
    assert run_isolated(helpers.add, 1, 1, timeout_s=TIMEOUT) == 2


@pytest.mark.parametrize(("case_id", "want_text"), [("INV-01", False), ("INV-09", True)])
def test_isolated_reading_equals_in_process_reading(case_id: str, want_text: bool) -> None:
    content = golden_pdf(case_id)
    isolated = read_document_isolated(content, LIMITS, want_text=want_text, timeout_s=TIMEOUT)
    assert isolated == read_document(content, LIMITS, want_text=want_text)


def test_a_hostile_document_is_refused_through_the_child_too() -> None:
    with pytest.raises(ExtractionError, match="not a PDF"):
        read_document_isolated(b"hello", LIMITS, want_text=True, timeout_s=TIMEOUT)
