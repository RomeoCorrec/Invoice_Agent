"""Text of a PDF without embedded XML, bounded so it can be handed to an extractor."""

from pypdf.generic import ArrayObject

from invoice_agent.config import IngestionLimits
from invoice_agent.ingestion.document import ExtractionError, inflate_cap, open_pdf

MAX_CONTENT_STREAMS_PER_PAGE = 10
INFLATE_FACTOR = 8
MIN_INFLATE_CAP = 1024 * 1024
"""A page's content stream (text operators) may inflate to INFLATE_FACTOR times the text
budget, at least MIN_INFLATE_CAP: generous for real invoices, far below a decompression bomb."""


def extract_text(content: bytes, limits: IngestionLimits) -> str:
    """All pages joined by newlines.

    Bounded at every step: at most MAX_CONTENT_STREAMS_PER_PAGE content streams per page,
    each inflated under INFLATE_FACTOR x the text budget, and extraction stops as soon as the
    text exceeds the budget. Fails, rather than silently truncating, if the text is longer
    than the limit or if there is no text at all, which is what a scanned image looks like.
    """
    pages: list[str] = []
    total = 0
    try:
        with open_pdf(content, limits) as reader:
            if len(reader.pages) > limits.max_pages:
                raise ExtractionError(f"PDF has more than {limits.max_pages} pages")
            cap = max(limits.max_text_chars * INFLATE_FACTOR, MIN_INFLATE_CAP)
            with inflate_cap(cap):
                for page in reader.pages:
                    streams = page.get("/Contents")
                    streams = streams.get_object() if streams is not None else None
                    if (
                        isinstance(streams, ArrayObject)
                        and len(streams) > MAX_CONTENT_STREAMS_PER_PAGE
                    ):
                        raise ExtractionError("a PDF page has too many content streams")
                    text = (page.extract_text() or "").strip()
                    total += len(text)
                    if total > limits.max_text_chars:
                        raise ExtractionError(
                            f"PDF text is longer than {limits.max_text_chars} characters"
                        )
                    pages.append(text)
    except ExtractionError:
        raise
    except Exception as exc:  # pypdf raises many types on malformed PDFs or oversized streams
        raise ExtractionError("could not read the text of the PDF") from exc
    text = "\n".join(pages).strip()
    if not text:
        raise ExtractionError("PDF contains no text (scanned documents are not supported)")
    return text
