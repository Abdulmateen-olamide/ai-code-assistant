"""Default LLM price table for common providers and models (issue #439).

The prices here are **estimates**, expressed in USD per 1,000 tokens, for
turning token counts into rough cost figures (pre-flight estimates, dashboards,
budgets). They are not a billing source of truth: vendors change prices and
introduce dated model snapshots, so operators must verify the numbers against
their provider contract before relying on them.

The offline ``mock`` provider is the one entry that is not an estimate: it
never calls a vendor and therefore always costs zero.

Lookups are provider/model exact matches after normalizing case and
surrounding whitespace. A ``*`` wildcard may be used for the provider and/or
model so an operator table can define a fallback rate; an exact entry always
takes precedence over a wildcard. An unknown provider or model returns ``None``
rather than guessing, so callers can distinguish "free" from "we do not know".

Token counts passed to :func:`estimate_cost_usd` are clamped to
:data:`MAX_TOKEN_COUNT` so an implausibly large (or malicious) count cannot
produce an infinite estimate. Negative counts are still rejected.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

#: Wildcard key understood by :func:`get_model_price`.
WILDCARD = "*"

#: Upper bound applied to each token count before costing. A completion with
#: more tokens than this is almost certainly a bug or hostile input; clamping
#: keeps the estimate finite without silently accepting an unbounded value.
MAX_TOKEN_COUNT = 1_000_000_000

#: Shown next to the table in docs/UI wherever costs are displayed.
ESTIMATE_DISCLAIMER = (
    "Default prices are estimates in USD per 1,000 tokens. "
    "Verify them against your provider's current pricing before relying on them."
)


@dataclass(frozen=True)
class ModelPrice:
    """Estimated input/output price for one provider model.

    Attributes:
        provider: Registered provider name (e.g. ``openai``).
        model: Model identifier as accepted by the provider.
        input_per_1k_usd: Estimated USD cost per 1,000 prompt tokens.
        output_per_1k_usd: Estimated USD cost per 1,000 completion tokens.
        estimated: ``False`` only for models with a known, guaranteed price
            (the offline mock provider). Everything else is a default estimate.
    """

    provider: str
    model: str
    input_per_1k_usd: float
    output_per_1k_usd: float
    estimated: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation, including the disclaimer."""
        data = asdict(self)
        data["disclaimer"] = ESTIMATE_DISCLAIMER
        return data


def _price(
    provider: str,
    model: str,
    input_per_1k_usd: float,
    output_per_1k_usd: float,
    *,
    estimated: bool = True,
) -> ModelPrice:
    return ModelPrice(
        provider=provider,
        model=model,
        input_per_1k_usd=input_per_1k_usd,
        output_per_1k_usd=output_per_1k_usd,
        estimated=estimated,
    )


#: Default, operator-verifiable estimates for the models registered by the
#: built-in providers. Keys are ``(provider, model)`` normalized to lowercase.
DEFAULT_PRICE_TABLE: dict[tuple[str, str], ModelPrice] = {
    # The offline provider is deterministic and free (not an estimate).
    ("mock", "mock-1"): _price("mock", "mock-1", 0.0, 0.0, estimated=False),
    # OpenAI defaults (see app/services/providers/openai.py).
    ("openai", "gpt-4o-mini"): _price("openai", "gpt-4o-mini", 0.00015, 0.0006),
    ("openai", "gpt-4o"): _price("openai", "gpt-4o", 0.0025, 0.01),
    ("openai", "gpt-4.1-mini"): _price("openai", "gpt-4.1-mini", 0.0004, 0.0016),
    ("openai", "gpt-4.1"): _price("openai", "gpt-4.1", 0.002, 0.008),
    # Anthropic defaults (see app/services/providers/anthropic.py).
    ("anthropic", "claude-3-5-sonnet-latest"): _price(
        "anthropic", "claude-3-5-sonnet-latest", 0.003, 0.015
    ),
    ("anthropic", "claude-3-5-haiku-latest"): _price(
        "anthropic", "claude-3-5-haiku-latest", 0.0008, 0.004
    ),
}


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def _normalized_table(table=None) -> dict[tuple[str, str], ModelPrice]:
    """Return ``table`` (default: the shipped table) with normalized keys."""
    source = DEFAULT_PRICE_TABLE if table is None else table
    return {
        (_normalize(provider), _normalize(model)): price
        for (provider, model), price in source.items()
    }


def get_model_price(
    provider: str | None,
    model: str | None,
    *,
    table=None,
) -> ModelPrice | None:
    """Return the price entry for ``provider``/``model``, or ``None``.

    Matching is case-insensitive and ignores surrounding whitespace. An exact
    provider/model entry always wins over a fallback, then the following
    wildcard order is tried: ``(provider, "*")``, ``("*", model)`` and finally
    ``("*", "*")``. Unknown combinations return ``None`` (cost unknown) rather
    than assuming zero.
    """
    normalized = _normalized_table(table)
    provider_key = _normalize(provider)
    model_key = _normalize(model)
    for key in (
        (provider_key, model_key),
        (provider_key, WILDCARD),
        (WILDCARD, model_key),
        (WILDCARD, WILDCARD),
    ):
        price = normalized.get(key)
        if price is not None:
            return price
    return None


def list_model_prices(provider: str | None = None, *, table=None) -> list[ModelPrice]:
    """Return the price entries, optionally filtered by ``provider``.

    Results are sorted by provider then model so the table (and any API built
    on it) is stable across runs.
    """
    wanted = _normalize(provider)
    normalized = _normalized_table(table)
    entries = [
        price
        for (entry_provider, _), price in normalized.items()
        if not wanted or entry_provider == wanted
    ]
    return sorted(entries, key=lambda price: (price.provider, price.model))


def price_table(*, table=None) -> dict[str, list[dict[str, Any]]]:
    """Return the whole table grouped by provider (JSON-friendly)."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for price in list_model_prices(table=table):
        grouped.setdefault(price.provider, []).append(price.to_dict())
    return grouped


def estimate_cost_usd(
    provider: str | None,
    model: str | None,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    table=None,
) -> float | None:
    """Estimate USD cost for a completion, or ``None`` when the price is unknown.

    Token counts must be non-negative integers; anything else raises
    :class:`ValueError` so callers cannot silently report a negative cost.
    Each count is clamped to :data:`MAX_TOKEN_COUNT` before the arithmetic so
    an implausible value cannot overflow the estimate.
    """
    prompt = int(prompt_tokens)
    completion = int(completion_tokens)
    if prompt < 0 or completion < 0:
        raise ValueError("Token counts must be non-negative.")
    prompt = min(prompt, MAX_TOKEN_COUNT)
    completion = min(completion, MAX_TOKEN_COUNT)

    price = get_model_price(provider, model, table=table)
    if price is None:
        return None
    return prompt / 1000 * price.input_per_1k_usd + completion / 1000 * price.output_per_1k_usd


__all__ = [
    "DEFAULT_PRICE_TABLE",
    "ESTIMATE_DISCLAIMER",
    "MAX_TOKEN_COUNT",
    "WILDCARD",
    "ModelPrice",
    "estimate_cost_usd",
    "get_model_price",
    "list_model_prices",
    "price_table",
]
