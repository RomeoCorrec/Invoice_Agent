"""Price of a model call, from the token counts the API reports.

Prices are USD per million tokens, copied from Anthropic's pricing page (October 2026) and
kept in one table so they can be audited against it. An unknown model is an error, never a
silent zero: a cost that is missing is worse than one that is approximate.
"""

from dataclasses import dataclass
from decimal import Decimal

from invoice_agent.llm.provider import LlmUsage

MILLION = Decimal(1_000_000)
LONG_PROMPT_THRESHOLD = 100_000


class UnknownModelError(LookupError):
    pass


@dataclass(frozen=True)
class ModelPrice:
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write_5m: Decimal
    # Haiku 5.5 charges more for prompts over 100k tokens; other models have one price.
    long_prompt: "ModelPrice | None" = None


def _price(input_: str, output: str, cache_read: str, cache_write: str) -> ModelPrice:
    return ModelPrice(Decimal(input_), Decimal(output), Decimal(cache_read), Decimal(cache_write))


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(
        Decimal("0.10"),
        Decimal("0.50"),
        Decimal("0.01"),
        Decimal("0.125"),
        long_prompt=_price("0.50", "2.50", "0.05", "0.625"),
    ),
    "claude-sonnet-5-5": _price("2.00", "10.00", "0.10", "2.50"),
    "claude-opus-5-5": _price("4.00", "20.00", "0.20", "5.00"),
}


def price_for(model: str) -> ModelPrice:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModelError(f"no price known for model {model!r}") from None


def cost_usd(model: str, usage: LlmUsage) -> Decimal:
    """Cost of one call. Cached and written tokens are counted apart from fresh input."""
    price = price_for(model)
    prompt_tokens = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    if price.long_prompt is not None and prompt_tokens > LONG_PROMPT_THRESHOLD:
        price = price.long_prompt
    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write_5m
    )
    return total / MILLION
