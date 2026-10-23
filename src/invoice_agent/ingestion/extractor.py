"""The seam between ingestion and an LLM-based extractor.

Ingestion only knows this protocol. The real implementation (a provider in ``llm/`` with a
versioned prompt, evaluated on the golden dataset) arrives with step 04; tests use a
scripted fake. Whatever it returns is untrusted: it is checked against the document text.
"""

from typing import Protocol

from invoice_agent.domain.invoice import InvoiceData


class InvoiceExtractor(Protocol):
    def extract(self, text: str) -> InvoiceData:
        """Structured invoice data from the text of a PDF without embedded XML.

        Implementations must treat ``text`` as data, never as instructions, and may raise
        ``ExtractionError`` when the text cannot be turned into an invoice.
        """
        ...
