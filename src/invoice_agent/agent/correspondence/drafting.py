"""The message to the supplier: a template first, a model's text only if it passes the checks.

The template is built before the model is asked and is always valid, so the conversation never
waits on, or depends on, a model. The model may write a better text from the facts (never from
the document); the code then decides whether it is used. A text that fails, a model that is
down, or no model at all, all end the same way: the template stands, and the result says why.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from pydantic import ValidationError

from invoice_agent.agent.correspondence.checks import check_draft
from invoice_agent.agent.correspondence.facts import QueryFacts
from invoice_agent.agent.correspondence.templates import QueryDraft, template_draft
from invoice_agent.agent.graph.deps import QueryConfig
from invoice_agent.config import Settings
from invoice_agent.llm.anthropic_provider import AnthropicProvider
from invoice_agent.llm.prompt import Prompt, load_prompt
from invoice_agent.llm.provider import LlmError, LlmProvider
from invoice_agent.llm.traced import TracedProvider
from invoice_agent.observability.tracing import Tracing

CALL_NAME = "draft_supplier_query"
MAX_TOKENS = 1000


@dataclass(frozen=True)
class DraftResult:
    draft: QueryDraft
    source: Literal["model", "template"]
    fallback_reason: str | None = None
    """Why the template was used, when it was: no model, model unavailable, draft refused."""
    problems: tuple[str, ...] = ()
    """The checks the model's text failed, when it was refused."""
    cost_usd: Decimal = Decimal(0)
    model: str | None = None


def build_query_config(settings: Settings, tracing: Tracing) -> QueryConfig:
    """The production drafting configuration. Without an API key the template is the draft."""
    deadline = timedelta(days=settings.mail.reply_deadline_days)
    if settings.llm.api_key is None:
        return QueryConfig(reply_deadline=deadline)
    prompt = load_prompt("draft_supplier_query", 1)
    provider = TracedProvider(
        AnthropicProvider(
            api_key=settings.llm.api_key,
            model=settings.llm.model,
            timeout_s=settings.llm.timeout_s,
            max_retries=settings.llm.max_retries,
            effort=settings.llm.effort,
        ),
        tracing,
        prompt_label=prompt.label,
    )
    return QueryConfig(provider=provider, prompt=prompt, reply_deadline=deadline)


def make_draft(
    facts: QueryFacts, *, provider: LlmProvider | None = None, prompt: Prompt | None = None
) -> DraftResult:
    """The draft to show a person: the model's if it passes every check, else the template."""
    baseline = template_draft(facts)
    if provider is None or prompt is None:
        return DraftResult(baseline, "template", fallback_reason="no model configured")
    try:
        result = provider.generate(
            system=prompt.text,
            user=facts.case_text(),
            schema=QueryDraft,
            max_tokens=MAX_TOKENS,
            name=CALL_NAME,
        )
    except (LlmError, ValidationError):
        return DraftResult(baseline, "template", fallback_reason="model unavailable")
    problems = check_draft(result.parsed, facts)
    if problems:
        return DraftResult(
            baseline,
            "template",
            fallback_reason="draft refused",
            problems=tuple(problems),
            cost_usd=result.cost_usd,
            model=result.model,
        )
    return DraftResult(result.parsed, "model", cost_usd=result.cost_usd, model=result.model)
