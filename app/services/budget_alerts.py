"""Budget threshold alert evaluation (Phase 10, issue #471).

The ledger records usage; this module decides when a workspace's spend crosses
its budget limit. Alerts are deduplicated per workspace and period so a burst of
usage produces one notification instead of one per request. The in-process
deduplicator is intentionally small and resettable (tests and multi-process
workers each keep their own view); a persistent dedupe store can replace it
without changing the public :func:`check_budget_alerts` contract.
"""

from __future__ import annotations

from app.services.token_usage import BudgetAlertDeduplicator

#: Alert kind emitted when spend is above the configured limit.
ALERT_OVER_BUDGET = "over_budget"

_deduplicator = BudgetAlertDeduplicator()


def check_budget_alerts(workspace_id, *, spend, limit, period) -> list[dict]:
    """Return alerts for ``workspace_id`` for the given budget ``period``.

    ``spend`` and ``limit`` are compared as numbers. A missing, zero or negative
    limit means no budget is configured, so no alert is emitted. The same alert
    (workspace + period + kind) fires at most once per period; starting a new
    period resets deduplication through
    :class:`~app.services.token_usage.BudgetAlertDeduplicator`.
    """
    try:
        spend_value = float(spend or 0)
        limit_value = float(limit or 0)
    except (TypeError, ValueError):
        return []
    if limit_value <= 0 or spend_value <= limit_value:
        return []

    period_key = str(period or "")
    if not _deduplicator.check_and_mark((workspace_id, period_key), ALERT_OVER_BUDGET):
        return []
    return [
        {
            "workspace_id": workspace_id,
            "kind": ALERT_OVER_BUDGET,
            "period": period_key,
            "spend": spend_value,
            "limit": limit_value,
            "over_by": round(spend_value - limit_value, 6),
        }
    ]


def reset() -> None:
    """Forget deduplication state (used between tests/periods)."""
    global _deduplicator
    _deduplicator = BudgetAlertDeduplicator()


__all__ = ["ALERT_OVER_BUDGET", "check_budget_alerts", "reset"]
