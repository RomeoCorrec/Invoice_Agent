"""Phase 1 of ingestion: read what the PDF contains. All the pypdf work happens here.

This is the only part that parses an untrusted container with pure-Python code that has no
wall-clock limit, so it is also the part the worker can run in a child process and kill.
Everything it returns is plain data (bytes and text) that crosses a process boundary.
"""

from dataclasses import dataclass

from invoice_agent.config import IngestionLimits
from invoice_agent.ingestion.document import inspect_pdf
from invoice_agent.ingestion.facturx import extract_xml
from invoice_agent.ingestion.pdf_text import extract_text


@dataclass(frozen=True)
class RawDocument:
    sha256: str
    xml: bytes | None
    """The embedded Factur-X XML, or None for a plain PDF."""
    text: str | None
    """The text of a plain PDF; None when there is an XML or when text was not requested."""


def read_document(content: bytes, limits: IngestionLimits, *, want_text: bool) -> RawDocument:
    """Check the PDF and read its XML, or its text if there is no XML and ``want_text``."""
    info = inspect_pdf(content, limits)
    xml = extract_xml(content, limits)
    if xml is not None:
        return RawDocument(sha256=info.sha256, xml=xml, text=None)
    text = extract_text(content, limits) if want_text else None
    return RawDocument(sha256=info.sha256, xml=None, text=text)
