"""Assemble the extractor from the settings: provider, tracing wrapper, prompt."""

from invoice_agent.config import Settings
from invoice_agent.llm.anthropic_provider import AnthropicProvider
from invoice_agent.llm.extractor import LlmInvoiceExtractor
from invoice_agent.llm.prompt import load_prompt
from invoice_agent.llm.traced import TracedProvider
from invoice_agent.observability.tracing import Tracing

EXTRACT_PROMPT_VERSION = 1


def build_extractor(settings: Settings, tracing: Tracing) -> LlmInvoiceExtractor | None:
    """The LLM extractor, or None when no API key is configured."""
    llm = settings.llm
    if llm.api_key is None:
        return None
    prompt = load_prompt("extract_invoice", EXTRACT_PROMPT_VERSION)
    provider = AnthropicProvider(
        api_key=llm.api_key,
        model=llm.model,
        timeout_s=llm.timeout_s,
        max_retries=llm.max_retries,
        effort=llm.effort,
    )
    traced = TracedProvider(provider, tracing, prompt_label=prompt.label)
    return LlmInvoiceExtractor(traced, settings.iban_hash_key, prompt=prompt)
