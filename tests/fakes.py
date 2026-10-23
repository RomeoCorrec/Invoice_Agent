"""Test doubles for the LLM-backed extractor."""

from invoice_agent.domain.invoice import InvoiceData


class ScriptedExtractor:
    """Returns a prepared result (or raises a prepared error) and records what it was given."""

    def __init__(self, result: InvoiceData | Exception) -> None:
        self._result = result
        self.received: list[str] = []

    def extract(self, text: str) -> InvoiceData:
        self.received.append(text)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result
