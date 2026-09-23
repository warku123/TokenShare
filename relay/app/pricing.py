"""Tiered per-token pricing for the seller relay.

Prices come from the Registry listing and are native USDC units (6 dp),
expressed per 1M tokens: priceInput = USDC-native price per 1,000,000 prompt
tokens, etc.

PIN formula (verbatim):
    actual = (cached*priceCachedIn + (prompt-cached)*priceInput + completion*priceOutput) // 1e6
Settle amount is clamped to the payment's maxAmount: settle = min(actual, maxAmount).
"""

from __future__ import annotations

from dataclasses import dataclass

# 1e6: USDC decimals / price-per-1M-tokens scaling divisor (PIN).
_SCALE = 1_000_000


@dataclass(frozen=True)
class Usage:
    """Token usage reported by OpenAI (native integers)."""

    prompt_tokens: int
    cached_tokens: int  # prompt_tokens_details.cached_tokens, default 0
    completion_tokens: int


@dataclass(frozen=True)
class Prices:
    """Registry listing prices, all in USDC native units per 1M tokens."""

    price_cached_in: int
    price_input: int
    price_output: int


def compute_actual(usage: Usage, prices: Prices) -> int:
    """Tiered actual cost, floor-divided to USDC native units.

    actual = (cached*priceCachedIn + (prompt-cached)*priceInput + completion*priceOutput) // 1e6
    """
    non_cached_prompt = max(0, usage.prompt_tokens - usage.cached_tokens)
    return (
        usage.cached_tokens * prices.price_cached_in
        + non_cached_prompt * prices.price_input
        + usage.completion_tokens * prices.price_output
    ) // _SCALE


def clamp_settle_amount(actual: int, max_amount: int) -> int:
    """Settle amount = min(actual, maxAmount) per the PIN."""
    return min(actual, max_amount)


def estimate_min_amount(prices: Prices, prompt_token_cap: int, completion_token_cap: int) -> int:
    """Per-request minAmount upper-bound estimate (PIN):

    (priceInput*PROMPT_TOKEN_CAP + priceOutput*COMPLETION_TOKEN_CAP) // 1e6
    """
    return (
        prices.price_input * prompt_token_cap + prices.price_output * completion_token_cap
    ) // _SCALE
