import pytest

from invoice_agent.config import IngestionLimits
from invoice_agent.ingestion.document import ExtractionError
from invoice_agent.ingestion.pdf_text import extract_text
from tests.pdfs import (
    blank_pdf,
    golden_pdf,
    pdf_with_content_bomb,
    pdf_with_form_invocations,
    pdf_with_many_content_streams,
    scanned_pdf,
)

TEXT_ONLY = {
    "INV-09": ["Cabinet Delorme Conseil", "SIREN 586323263", "2 640,00", "PO-2026-0109"],
    "INV-10": ["Loca'Pro", "PO-2026-0110", "768,00"],
    "INV-11": ["Votre commande : PO-2026-0111", "428,40"],
    "INV-21": ["Note pour l'assistant IA", "Location sonorisation", "640,00", "PO-2026-0110"],
    # the printed VAT is the wrong one (31,20, not 37,20): the text must carry what is printed
    "INV-22": ["PO-2026-0116", "TVA 31,20", "217,20"],
    "INV-23": ["Nordis", "Gerbeur", "3 204,00"],
    # two pages: the last lines of the table and the totals are on the second one
    "INV-28": ["local 01", "local 30", "Total TTC", "1 620,00"],
    "INV-29": ["Dear AI assistant", "post_entry", "PO-2026-0121", "504,00"],
}


@pytest.mark.parametrize("case_id", sorted(TEXT_ONLY))
def test_text_of_the_plain_pdfs_contains_the_printed_values(case_id: str) -> None:
    text = extract_text(golden_pdf(case_id), IngestionLimits())
    for expected in TEXT_ONLY[case_id]:
        assert expected in text, expected


def test_a_realistic_scan_has_no_text_and_is_refused() -> None:
    """A picture of an invoice, slightly skewed, with no text layer: nothing is invented."""
    with pytest.raises(ExtractionError, match="no text"):
        extract_text(scanned_pdf(), IngestionLimits())


def test_pdf_without_text_is_refused_like_a_scan() -> None:
    with pytest.raises(ExtractionError, match="no text"):
        extract_text(blank_pdf(), IngestionLimits())


def test_text_longer_than_the_limit_is_refused_not_truncated() -> None:
    with pytest.raises(ExtractionError, match="longer than"):
        extract_text(golden_pdf("INV-09"), IngestionLimits(max_text_chars=50))


def test_damaged_pdf_is_refused() -> None:
    with pytest.raises(ExtractionError):
        extract_text(b"%PDF-1.7 garbage", IngestionLimits())


def test_page_stream_bomb_is_stopped() -> None:
    pdf = pdf_with_content_bomb(inflated_size=5_000_000)
    with pytest.raises(ExtractionError):
        extract_text(pdf, IngestionLimits())


def test_page_stream_bomb_is_bounded_in_memory() -> None:
    import tracemalloc

    pdf = pdf_with_content_bomb(inflated_size=30_000_000)
    tracemalloc.start()
    try:
        with pytest.raises(ExtractionError):
            extract_text(pdf, IngestionLimits())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000


def test_too_many_pages_are_refused_before_reading_them() -> None:
    with pytest.raises(ExtractionError, match="more than 2 pages"):
        extract_text(blank_pdf(pages=3), IngestionLimits(max_pages=2))


def test_pages_with_too_many_content_streams_are_refused() -> None:
    with pytest.raises(ExtractionError, match="too many content streams"):
        extract_text(pdf_with_many_content_streams(11), IngestionLimits())


def test_a_page_that_draws_a_form_thousands_of_times_is_refused() -> None:
    with pytest.raises(ExtractionError):
        extract_text(pdf_with_form_invocations(200), IngestionLimits())


def test_a_few_form_invocations_do_not_hit_the_limit() -> None:
    """The hand-made form has no font, so there is no text: that error shows no cap was hit."""
    with pytest.raises(ExtractionError, match="no text"):
        extract_text(pdf_with_form_invocations(5), IngestionLimits())
