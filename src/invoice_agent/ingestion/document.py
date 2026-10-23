"""First look at an incoming document, before anything parses it."""

import hashlib
import io
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from pypdf import PdfReader, apply_configuration
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


@contextmanager
def inflate_cap(limit: int) -> Iterator[None]:
    """Cap the output of every pypdf stream decoder (not only Flate) at ``limit`` bytes.

    The setting is context-local (a ContextVar inside pypdf), so it is thread-safe and is
    restored on exit. LZW, run-length, JBIG2 and Brotli are covered as well as zlib.
    """
    with apply_configuration(
        zlib_maximum_output_length=limit,
        lzw_maximum_output_length=limit,
        run_length_maximum_output_length=limit,
        jbig2_maximum_output_length=limit,
        brotli_maximum_output_length=limit,
        array_based_stream_maximum_output_length=limit,
    ):
        yield


@contextmanager
def open_pdf(content: bytes, limits: IngestionLimits) -> Iterator[PdfReader]:
    """A PdfReader created and used under a cap on inflated stream sizes.

    Building the reader already inflates cross-reference and object streams, so the whole
    lifecycle runs under the cap, not only the later content decoding. Callers that read
    content narrow it further with ``apply_configuration``.
    """
    with inflate_cap(limits.max_document_bytes):
        yield PdfReader(io.BytesIO(content))


def inspect_pdf(content: bytes, limits: IngestionLimits) -> PdfInfo:
    """Check size, signature, encryption and page count; raise ExtractionError otherwise."""
    if len(content) > limits.max_document_bytes:
        raise ExtractionError(f"document is larger than {limits.max_document_bytes} bytes")
    if not content.startswith(PDF_MAGIC):
        raise ExtractionError("document is not a PDF")
    try:
        with open_pdf(content, limits) as reader:
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
