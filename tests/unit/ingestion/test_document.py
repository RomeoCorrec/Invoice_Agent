import pytest

from invoice_agent.config import IngestionLimits
from invoice_agent.ingestion.document import ExtractionError, inspect_pdf, sha256_hex
from tests.pdfs import blank_pdf, encrypted_pdf, golden_pdf

LIMITS = IngestionLimits(max_document_bytes=50_000, max_pages=3)


def test_valid_pdf_is_described() -> None:
    content = blank_pdf(pages=2)
    info = inspect_pdf(content, LIMITS)
    assert info.pages == 2
    assert info.size == len(content)
    assert info.sha256 == sha256_hex(content)


def test_hash_is_stable_and_content_dependent() -> None:
    assert sha256_hex(b"a") == sha256_hex(b"a")
    assert sha256_hex(b"a") != sha256_hex(b"b")
    assert len(sha256_hex(b"a")) == 64


def test_real_invoices_are_accepted() -> None:
    assert inspect_pdf(golden_pdf("INV-01"), IngestionLimits()).pages == 1


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "not a PDF"),
        (b"PK\x03\x04 zip file", "not a PDF"),
        (b"<html>hello</html>", "not a PDF"),
        (b"%PDF-1.7\nthis is not really a pdf", "damaged"),
        (blank_pdf()[:60], "damaged"),
    ],
)
def test_non_pdf_or_damaged_content_is_rejected(content: bytes, message: str) -> None:
    with pytest.raises(ExtractionError, match=message):
        inspect_pdf(content, LIMITS)


def test_too_large_document_is_rejected() -> None:
    with pytest.raises(ExtractionError, match="larger than"):
        inspect_pdf(b"%PDF-" + b"0" * 60_000, LIMITS)


def test_too_many_pages_is_rejected() -> None:
    with pytest.raises(ExtractionError, match="4 pages"):
        inspect_pdf(blank_pdf(pages=4), LIMITS)


def test_encrypted_pdf_is_rejected() -> None:
    with pytest.raises(ExtractionError, match="encrypted"):
        inspect_pdf(encrypted_pdf(), LIMITS)


def test_error_messages_never_contain_document_content() -> None:
    secret = b"%PDF-1.7 TOP-SECRET-CONTENT"
    with pytest.raises(ExtractionError) as error:
        inspect_pdf(secret, LIMITS)
    assert "SECRET" not in str(error.value)


def test_inflate_cap_covers_every_decoder_and_is_restored() -> None:
    from pypdf import get_configuration

    from invoice_agent.ingestion.document import inflate_cap

    before = get_configuration()
    with inflate_cap(1234):
        inside = get_configuration()
        assert inside.zlib_maximum_output_length == 1234
        assert inside.lzw_maximum_output_length == 1234
        assert inside.run_length_maximum_output_length == 1234
        assert inside.jbig2_maximum_output_length == 1234
        assert inside.brotli_maximum_output_length == 1234
        assert inside.array_based_stream_maximum_output_length == 1234
    assert get_configuration() == before


def test_the_reader_is_built_under_the_document_cap() -> None:
    from pypdf import get_configuration

    from invoice_agent.ingestion.document import open_pdf

    with open_pdf(blank_pdf(), LIMITS):
        assert get_configuration().zlib_maximum_output_length == LIMITS.max_document_bytes
