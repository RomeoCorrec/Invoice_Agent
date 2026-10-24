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


class LookupExtractor:
    """Plays the LLM for several documents: picks the prepared result whose invoice number is
    printed in the text it receives."""

    def __init__(self, by_number: dict[str, InvoiceData]) -> None:
        self._by_number = by_number

    def extract(self, text: str) -> InvoiceData:
        from invoice_agent.domain.rules.injection import squash_text
        from invoice_agent.ingestion.document import ExtractionError

        printed = f" {squash_text(text)} "
        for number, data in self._by_number.items():
            if f" {squash_text(number)} " in printed:
                return data
        raise ExtractionError("unknown document")
