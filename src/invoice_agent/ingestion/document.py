"""First look at an incoming document, before anything parses it."""

import hashlib
import io
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from invoice_agent.config import IngestionLimits

PDF_MAGIC = b"%PDF-"


class ExtractionError(ValueError):
    """The document cannot be turned into reliable invoice data.

    The message is safe to store and show: it never contains document content.
    """


@dataclass(frozen=True)
class PdfInfo:
    sha256: str
    size: int
    pages: int


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def inspect_pdf(content: bytes, limits: IngestionLimits) -> PdfInfo:
    """Check size, signature, encryption and page count; raise ExtractionError otherwise."""
    if len(content) > limits.max_document_bytes:
        raise ExtractionError(f"document is larger than {limits.max_document_bytes} bytes")
    if not content.startswith(PDF_MAGIC):
        raise ExtractionError("document is not a PDF")
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ExtractionError("encrypted PDFs are not supported")
        pages = len(reader.pages)
    except ExtractionError:
        raise
    except (PyPdfError, ValueError, KeyError, OSError, RecursionError) as exc:
        raise ExtractionError("PDF is damaged or unreadable") from exc
    if pages == 0:
        raise ExtractionError("PDF has no pages")
    if pages > limits.max_pages:
        raise ExtractionError(f"PDF has {pages} pages, the limit is {limits.max_pages}")
    return PdfInfo(sha256=sha256_hex(content), size=len(content), pages=pages)
