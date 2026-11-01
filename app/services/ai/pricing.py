"""
Per-model prices (USD per million tokens) used to estimate the cost of each
call. Source: the Claude API skill model table (claude-api/shared/models.md and
shared/prompt-caching.md), October 2026.

Cache writes use the 5-minute TTL rate (1.25x input). Cache reads are 0.05x
input on Claude Opus 5.5 and 0.1x elsewhere. Claude Haiku 5.5 has a higher
tier when the prompt exceeds 100K tokens.

These are estimates for caps and dashboards; the Anthropic console is the
billing source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20, cache_write=5.00),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125),
    # Optional seller-copilot tier (AI_COPILOT_MODEL); cache reads $0.20/MTok.
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.20, cache_write=2.50),
    # Possible server-side fallback targets for Opus 5.5 refusals.
    "claude-opus-5": Price(input=5.00, output=25.00, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-8": Price(input=5.00, output=25.00, cache_read=0.50, cache_write=6.25),
}

# Claude Haiku 5.5 above 100K prompt tokens: $0.50 / $2.50.
_HAIKU_LONG = Price(input=0.50, output=2.50, cache_read=0.05, cache_write=0.625)
_HAIKU_LONG_THRESHOLD = 100_000

# Unknown model ids are priced at the most expensive known rate so caps stay safe.
_FALLBACK_PRICE = max(PRICES.values(), key=lambda p: p.output)


def price_for(model: str | None, prompt_tokens: int = 0) -> Price:
    if model == "claude-haiku-5-5" and prompt_tokens > _HAIKU_LONG_THRESHOLD:
        return _HAIKU_LONG
    return PRICES.get(model or "", _FALLBACK_PRICE)


def estimate_microusd(
    model: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> int:
    """Estimated cost in micro-USD (1e-6 USD). Tokens x $/MTok = micro-USD directly."""
    prompt_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
    p = price_for(model, prompt_tokens)
    total = (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_creation_input_tokens * p.cache_write
        + cache_read_input_tokens * p.cache_read
    )
    return int(round(total))
