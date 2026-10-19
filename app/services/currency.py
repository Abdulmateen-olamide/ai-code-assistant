"""Currency configuration and cost formatting (Phase 10, issue #441).

The ledger and price table work in one configured display currency, defaulting
to USD. Cost figures are estimates (see :mod:`app.services.pricing`), so every
formatted value includes both the currency code and an ``estimate`` marker made
available to callers.

Formatting is centralised here instead of being duplicated in templates and
JavaScript so every cost figure uses the same symbol, precision and code.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

DEFAULT_CURRENCY_CODE = "USD"
DEFAULT_CURRENCY_SYMBOL = "$"
DEFAULT_DECIMAL_PLACES = 4

#: Suffix that makes it unambiguous that a figure is not an invoice amount.
ESTIMATE_SUFFIX = "estimate"


def _clean_code(value) -> str:
    code = str(value or "").strip().upper()
    return code[:8] or DEFAULT_CURRENCY_CODE


def _clean_symbol(value) -> str:
    symbol = str(value or "").strip()
    return symbol[:8]


def _clean_places(value) -> int:
    try:
        places = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DECIMAL_PLACES
    return min(max(places, 0), 8)


def normalize_currency(config: dict | None = None) -> dict:
    """Return a complete, validated currency configuration.

    Missing or malformed values fall back to USD rather than failing a page
    render or API request. Unknown keys are ignored.
    """
    config = config or {}
    code = _clean_code(config.get("code"))
    symbol = _clean_symbol(config.get("symbol", DEFAULT_CURRENCY_SYMBOL))
    places = _clean_places(config.get("decimal_places", DEFAULT_DECIMAL_PLACES))
    return {"code": code, "symbol": symbol, "decimal_places": places}


def get_currency_config(app=None) -> dict:
    """Read the configured display currency from the Flask application.

    Falls back to the USD defaults when called outside an application context
    (for example from a unit test or a formatting helper), so formatting never
    fails merely because no app is bound.
    """
    if app is None:
        from flask import current_app

        try:
            config = current_app.config
        except RuntimeError:
            config = {}
    else:
        config = app.config
    return normalize_currency(
        {
            "code": config.get("CURRENCY_CODE", DEFAULT_CURRENCY_CODE),
            "symbol": config.get("CURRENCY_SYMBOL", DEFAULT_CURRENCY_SYMBOL),
            "decimal_places": config.get("CURRENCY_DECIMAL_PLACES", DEFAULT_DECIMAL_PLACES),
        }
    )


def format_cost(amount, config: dict | None = None) -> str:
    """Format ``amount`` with the configured currency symbol, code and precision.

    ``amount`` must be a finite, non-negative number. Values are rounded
    half-up to the configured number of decimal places, then the code is shown
    explicitly (for example ``$0.0025 USD``) so the display is never ambiguous.
    """
    currency = normalize_currency(config) if config is not None else get_currency_config()
    try:
        value = Decimal(str(amount if amount is not None else 0))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Cost amount must be numeric.") from exc
    if not value.is_finite():
        raise ValueError("Cost amount must be finite.")
    if value < 0:
        raise ValueError("Cost amount must be non-negative.")

    quantizer = Decimal(1).scaleb(-currency["decimal_places"])
    rounded = value.quantize(quantizer, rounding=ROUND_HALF_UP)
    number = f"{rounded:,.{currency['decimal_places']}f}"
    symbol = currency["symbol"]
    if symbol:
        return f"{symbol}{number} {currency['code']}"
    return f"{number} {currency['code']}"


def format_estimate(tokens, amount, config: dict | None = None) -> str:
    """Format a pre-flight estimate with tokens and an explicit cost label."""
    currency = normalize_currency(config) if config is not None else get_currency_config()
    try:
        token_count = max(0, int(tokens or 0))
    except (TypeError, ValueError):
        token_count = 0
    return (
        f"Estimated: ~{token_count:,} tokens "
        f"({format_cost(amount, currency)}; {ESTIMATE_SUFFIX})"
    )


__all__ = [
    "DEFAULT_CURRENCY_CODE",
    "DEFAULT_CURRENCY_SYMBOL",
    "DEFAULT_DECIMAL_PLACES",
    "ESTIMATE_SUFFIX",
    "format_cost",
    "format_estimate",
    "get_currency_config",
    "normalize_currency",
]
