import pytest

from invoice_agent.config import IngestionLimits
from invoice_agent.ingestion.document import ExtractionError
from invoice_agent.ingestion.pdf_text import extract_text
from tests.pdfs import blank_pdf, golden_pdf, pdf_with_content_bomb

TEXT_ONLY = {
    "INV-09": ["Cabinet Delorme Conseil", "SIREN 586323263", "2 640,00", "PO-2026-0109"],
    "INV-10": ["Loca'Pro", "PO-2026-0110", "768,00"],
    "INV-11": ["Votre commande : PO-2026-0111", "428,40"],
    "INV-21": ["Note pour l'assistant IA", "Location sonorisation", "640,00", "PO-2026-0110"],
}


@pytest.mark.parametrize("case_id", sorted(TEXT_ONLY))
def test_text_of_the_plain_pdfs_contains_the_printed_values(case_id: str) -> None:
    text = extract_text(golden_pdf(case_id), IngestionLimits())
    for expected in TEXT_ONLY[case_id]:
        assert expected in text, expected


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
        extract_text(pdf, IngestionLimits(max_document_bytes=100_000))


def test_page_stream_bomb_is_bounded_in_memory() -> None:
    import tracemalloc

    pdf = pdf_with_content_bomb(inflated_size=30_000_000)
    tracemalloc.start()
    try:
        with pytest.raises(ExtractionError):
            extract_text(pdf, IngestionLimits(max_document_bytes=100_000))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000
