"""Text of a PDF without embedded XML, bounded so it can be handed to an extractor."""

import io

from pypdf import PdfReader, apply_configuration

from invoice_agent.config import IngestionLimits
from invoice_agent.ingestion.document import ExtractionError


def extract_text(content: bytes, limits: IngestionLimits) -> str:
    """All pages joined by newlines.

    Page streams are inflated under a size cap (a small compressed stream can otherwise
    expand to tens of megabytes). Fails, rather than silently truncating, if the text is
    longer than the limit or if there is no text at all, which is what a scanned image
    looks like.
    """
    try:
        with apply_configuration(zlib_maximum_output_length=limits.max_document_bytes):
            reader = PdfReader(io.BytesIO(content))
            pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises many types on malformed PDFs or oversized streams
        raise ExtractionError("could not read the text of the PDF") from exc
    text = "\n".join(page.strip() for page in pages).strip()
    if not text:
        raise ExtractionError("PDF contains no text (scanned documents are not supported)")
    if len(text) > limits.max_text_chars:
        raise ExtractionError(f"PDF text is longer than {limits.max_text_chars} characters")
    return text
