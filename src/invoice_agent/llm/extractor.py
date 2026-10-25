"""The LLM-backed ``InvoiceExtractor``.

It builds the prompt, calls the provider and converts the reply. Whatever it returns is still
untrusted: the pipeline checks it against the document text afterwards.
"""

import re

from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.domain.job import TransientJobError
from invoice_agent.ingestion.document import ExtractionError
from invoice_agent.llm.extraction_schema import ExtractedInvoice, to_invoice_data
from invoice_agent.llm.prompt import Prompt, load_prompt
from invoice_agent.llm.provider import LlmPermanentError, LlmProvider, LlmTransientError

DOCUMENT_TAG = re.compile(r"<\s*/?\s*document\b[^>]*>", re.IGNORECASE)
DEFAULT_MAX_TOKENS = 3000
EXTRACT_NAME = "extract_invoice"


def wrap_document(text: str) -> str:
    """Put the text between <document> tags, after neutralising any tag it contains.

    Otherwise a document could close the tag itself and make its next lines look like
    instructions placed outside the data.
    """
    return f"<document>\n{DOCUMENT_TAG.sub('[document-tag]', text)}\n</document>"


class LlmInvoiceExtractor:
    def __init__(
        self,
        provider: LlmProvider,
        iban_key: bytes,
        *,
        prompt: Prompt | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._provider = provider
        self._iban_key = iban_key
        self._prompt = prompt or load_prompt("extract_invoice", 1)
        self._max_tokens = max_tokens

    @property
    def prompt(self) -> Prompt:
        return self._prompt

    def extract(self, text: str) -> InvoiceData:
        try:
            result = self._provider.generate(
                system=self._prompt.text,
                user=wrap_document(text),
                schema=ExtractedInvoice,
                max_tokens=self._max_tokens,
                name=EXTRACT_NAME,
            )
        except LlmTransientError as exc:
            raise TransientJobError(str(exc)) from exc  # the worker retries later
        except LlmPermanentError as exc:
            raise ExtractionError(f"the language model could not read the document: {exc}") from exc
        return to_invoice_data(result.parsed, self._iban_key)
