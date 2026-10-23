"""From the bytes of a PDF to ``InvoiceData``: Factur-X first, extractor as fallback."""

from dataclasses import dataclass

from invoice_agent.config import IngestionLimits
from invoice_agent.domain.invoice import InvoiceData, SourceFormat
from invoice_agent.domain.rules.injection import squash_text, suspicious_passages
from invoice_agent.ingestion.document import ExtractionError, inspect_pdf
from invoice_agent.ingestion.extractor import InvoiceExtractor
from invoice_agent.ingestion.facturx import extract_xml, parse_cii
from invoice_agent.ingestion.grounding import verify_grounded
from invoice_agent.ingestion.pdf_text import extract_text


@dataclass(frozen=True)
class ExtractedDocument:
    source_format: SourceFormat
    sha256: str
    data: InvoiceData


def extract_invoice(
    content: bytes,
    *,
    extractor: InvoiceExtractor | None,
    iban_key: bytes,
    limits: IngestionLimits,
) -> ExtractedDocument:
    """Extract the invoice data, or raise ExtractionError (never a half-filled result).

    A PDF with an embedded Factur-X XML is parsed deterministically. If that XML is broken
    the document is refused: falling back to the visible text would let a tampered XML hide
    behind a readable page. Only a PDF *without* XML goes to the extractor, whose output is
    then checked against the text it came from.
    """
    info = inspect_pdf(content, limits)
    xml = extract_xml(content, limits)
    if xml is not None:
        return ExtractedDocument(SourceFormat.FACTURX, info.sha256, parse_cii(xml, iban_key))

    if extractor is None:
        raise ExtractionError("no extractor is configured for PDFs without an embedded XML")
    text = extract_text(content, limits)
    try:
        data = extractor.extract(text)
    except ExtractionError:
        raise
    except Exception as exc:  # a provider, network or parsing failure: never leak its details
        raise ExtractionError(f"the extractor failed ({type(exc).__name__})") from exc
    data = _with_detected_injections(data, text)
    verify_grounded(data, text, iban_key)
    return ExtractedDocument(SourceFormat.PDF, info.sha256, data)


def _with_detected_injections(data: InvoiceData, text: str) -> InvoiceData:
    """Add to the notes every injected passage found in the raw text.

    The extractor may have dropped, paraphrased or hidden the note that tries to steer it.
    Declaring the passage ourselves guarantees that the injection check sees it (so the
    invoice goes to review) and that the grounding check does not count its figures.
    """
    known = [squash_text(note) for note in data.notes]
    added = [
        passage
        for passage in suspicious_passages(text)
        if not any(squash_text(passage) in note for note in known)
    ]
    if not added:
        return data
    return data.model_copy(update={"notes": (*data.notes, *added)})
