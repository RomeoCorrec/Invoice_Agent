from decimal import Decimal

import pytest

from invoice_agent.llm.pricing import PRICES, UnknownModelError, cost_usd, price_for
from invoice_agent.llm.provider import LlmUsage


def test_haiku_cost_of_a_typical_extraction() -> None:
    # 1,800 tokens in at $0.10/M plus 300 out at $0.50/M
    usage = LlmUsage(input_tokens=1800, output_tokens=300)
    assert cost_usd("claude-haiku-5-5", usage) == Decimal("0.00033")


def test_sonnet_and_opus_are_priced_per_million_tokens() -> None:
    usage = LlmUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("claude-sonnet-5-5", usage) == Decimal("12")
    assert cost_usd("claude-opus-5-5", usage) == Decimal("24")


def test_cache_reads_and_writes_have_their_own_price() -> None:
    small = LlmUsage(cache_read_tokens=40_000, cache_write_tokens=40_000)
    assert cost_usd("claude-haiku-5-5", small) == Decimal("0.0054")  # 0.0004 read + 0.005 write
    big = LlmUsage(cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert cost_usd("claude-sonnet-5-5", big) == Decimal("2.60")  # 0.10 + 2.50
    # 2M tokens is a long prompt for Haiku: 0.05 + 0.625
    assert cost_usd("claude-haiku-5-5", big) == Decimal("0.675")


def test_haiku_prompts_over_100k_tokens_use_the_higher_price() -> None:
    short = LlmUsage(input_tokens=100_000)
    long = LlmUsage(input_tokens=100_001)
    assert cost_usd("claude-haiku-5-5", short) == Decimal("0.01")
    assert cost_usd("claude-haiku-5-5", long) == Decimal("0.0500005")


def test_cached_tokens_count_towards_the_long_prompt_threshold() -> None:
    usage = LlmUsage(input_tokens=10, cache_read_tokens=100_000)
    assert cost_usd("claude-haiku-5-5", usage) == Decimal("0.005005")  # 0.05/M read, 0.50/M input


def test_no_usage_costs_nothing() -> None:
    assert cost_usd("claude-opus-5-5", LlmUsage()) == Decimal(0)


def test_an_unknown_model_is_an_error_not_a_free_call() -> None:
    with pytest.raises(UnknownModelError, match="claude-future-9"):
        cost_usd("claude-future-9", LlmUsage(input_tokens=1))
    with pytest.raises(UnknownModelError):
        price_for("gpt-4")


def test_every_listed_price_is_positive_and_ordered() -> None:
    for model, price in PRICES.items():
        assert price.input > 0 and price.output > price.input, model
        assert price.cache_read < price.input < price.cache_write_5m, model
