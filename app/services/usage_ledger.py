"""Central usage ledger service (Phase 10).

The ledger is append-only: callers add :class:`~app.models.UsageRecord` rows and
readers aggregate them. This module keeps the normalisation and aggregation
rules in one place so project chat, project analysis, dashboards and future
features (reviews, tools, budgets) all report the same numbers.

Only metadata is stored. Prompts and completions are never written here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_

from app.extensions import db
from app.models import Message, UsageRecord
from app.services import pricing

#: Operation families understood by the ledger. Unknown values are grouped
#: under :data:`UNKNOWN_SOURCE` instead of crashing a dashboard.
SOURCE_CHAT = "chat"
SOURCE_ANALYSIS = "analysis"
SOURCE_REVIEW = "review"
SOURCE_TOOLS = "tools"
SOURCE_PROJECT_CHAT = "project_chat"
SOURCES = (
    SOURCE_CHAT,
    SOURCE_ANALYSIS,
    SOURCE_REVIEW,
    SOURCE_TOOLS,
    SOURCE_PROJECT_CHAT,
)
UNKNOWN_SOURCE = "unknown"

#: Default/upper bounds for the historical chat-message backfill. The work is
#: paginated by message id so a large conversation history is never loaded at
#: once.
BACKFILL_DEFAULT_BATCH_SIZE = 500
BACKFILL_MAX_BATCH_SIZE = 5000

#: Operation outcomes understood by the ledger (see docs/observability.md).
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_DENIED = "denied"


def normalize_source(source: Any) -> str | None:
    """Return the lowercase source label, or ``None`` when it is missing."""
    value = str(source or "").strip().lower()
    return value[:40] or None


def source_bucket(source: Any) -> str:
    """Group a raw source into a known bucket, falling back to ``unknown``."""
    value = normalize_source(source)
    if value in SOURCES:
        return value
    return UNKNOWN_SOURCE


def _non_negative_int(value, name: str) -> int:
    """Coerce a count to a non-negative integer, rejecting invalid input."""
    if value in (None, ""):
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if count < 0:
        raise ValueError(f"{name} must be non-negative.")
    return count


def _optional_non_negative_int(value, name: str) -> int | None:
    """Coerce an optional measurement to a non-negative integer, or ``None``."""
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if number < 0:
        raise ValueError(f"{name} must be non-negative.")
    return number


def _optional_text(value, name: str, limit: int) -> str | None:
    """Normalize an optional short string field, or return ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def normalize_usage(prompt_tokens=0, completion_tokens=0, total_tokens=None) -> dict[str, int]:
    """Return validated prompt/completion/total token counts.

    ``total_tokens`` is derived from the two parts when it is omitted. Explicit
    values are validated as well, so a malformed provider payload can never
    write a negative count into the ledger.
    """
    prompt = _non_negative_int(prompt_tokens, "prompt_tokens")
    completion = _non_negative_int(completion_tokens, "completion_tokens")
    total = (
        prompt + completion
        if total_tokens is None
        else _non_negative_int(total_tokens, "total_tokens")
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _object_id(value, attribute: str = "id"):
    if value is None:
        return None
    return getattr(value, attribute, None)


def current_provider_metadata(user=None) -> tuple[str | None, str | None]:
    """Return ``(provider_name, model)`` for the active provider, if resolvable.

    Instrumentation must never break the operation it is recording, so any
    configuration error simply yields ``(None, None)`` and cost stays unknown.
    """
    try:
        from app.services.providers import get_provider

        provider = get_provider(user=user)
        name = getattr(provider, "name", None)
        model = getattr(provider, "model", None)
        if not model:
            models = getattr(provider, "models", ()) or ()
            model = models[0] if models else None
        return name, model
    except Exception:  # pragma: no cover - defensive: never break instrumentation
        return None, None


def estimate_cost(provider, model, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Estimate cost with the default price table, returning ``None`` if unknown."""
    if not provider or not model:
        return None
    return pricing.estimate_cost_usd(
        provider,
        model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def record_usage(
    *,
    source: str,
    prompt_tokens=0,
    completion_tokens=0,
    total_tokens=None,
    user=None,
    user_id=None,
    workspace=None,
    workspace_id=None,
    project=None,
    project_id=None,
    conversation=None,
    conversation_id=None,
    provider: str | None = None,
    model: str | None = None,
    tokens_estimated: bool = True,
    cost_estimate=None,
    request_id: str | None = None,
    latency_ms=None,
    status: str | None = None,
    commit: bool = True,
) -> UsageRecord:
    """Append one usage record and return it.

    Scope objects take precedence over their explicit id counterparts. Cost is
    estimated from the configured price table when the caller does not supply a
    value; an unknown provider/model leaves it as ``None`` rather than zero.

    ``request_id``, ``latency_ms`` and ``status`` are optional correlation and
    performance fields. They default to ``None`` (the operation did not report
    them) and are validated so a malformed provider payload cannot write a
    negative latency or an over-long id into the ledger.
    """
    counts = normalize_usage(prompt_tokens, completion_tokens, total_tokens)
    provider = (str(provider).strip()[:50] or None) if provider else None
    model = (str(model).strip()[:100] or None) if model else None
    request_id = _optional_text(request_id, "request_id", 64)
    latency_ms = _optional_non_negative_int(latency_ms, "latency_ms")
    status = _optional_text(status, "status", 20)
    if status:
        status = status.lower()

    if cost_estimate is None:
        cost_estimate = estimate_cost(
            provider, model, counts["prompt_tokens"], counts["completion_tokens"]
        )
    else:
        try:
            cost_estimate = float(cost_estimate)
        except (TypeError, ValueError) as exc:
            raise ValueError("cost_estimate must be numeric.") from exc
        if cost_estimate < 0:
            raise ValueError("cost_estimate must be non-negative.")

    record = UsageRecord(
        user_id=_object_id(user) or user_id,
        workspace_id=_object_id(workspace) or workspace_id or _object_id(project, "workspace_id"),
        project_id=_object_id(project) or project_id,
        conversation_id=_object_id(conversation) or conversation_id,
        source=normalize_source(source) or UNKNOWN_SOURCE,
        provider=provider,
        model=model,
        prompt_tokens=counts["prompt_tokens"],
        completion_tokens=counts["completion_tokens"],
        total_tokens=counts["total_tokens"],
        tokens_estimated=bool(tokens_estimated),
        cost_estimate=cost_estimate,
        request_id=request_id,
        latency_ms=latency_ms,
        status=status,
    )
    db.session.add(record)
    if commit:
        db.session.commit()
    return record


def summarize_records(records) -> dict:
    """Aggregate counts and costs for an iterable of records."""
    summary = {
        "count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_estimate": 0.0,
        "priced_records": 0,
        "estimated_records": 0,
    }
    for record in records:
        summary["count"] += 1
        summary["prompt_tokens"] += int(getattr(record, "prompt_tokens", 0) or 0)
        summary["completion_tokens"] += int(getattr(record, "completion_tokens", 0) or 0)
        summary["total_tokens"] += int(getattr(record, "total_tokens", 0) or 0)
        if getattr(record, "tokens_estimated", False):
            summary["estimated_records"] += 1
        cost = getattr(record, "cost_estimate", None)
        if cost is not None:
            summary["cost_estimate"] += float(cost)
            summary["priced_records"] += 1
    summary["cost_estimate"] = round(summary["cost_estimate"], 6)
    return summary


def query_records(
    *,
    user_id=None,
    workspace_id=None,
    project_id=None,
    source=None,
) -> list[UsageRecord]:
    """Fetch ledger rows with optional owner/scope filters."""
    query = UsageRecord.query
    if user_id is not None:
        query = query.filter(UsageRecord.user_id == user_id)
    if workspace_id is not None:
        query = query.filter(UsageRecord.workspace_id == workspace_id)
    if project_id is not None:
        query = query.filter(UsageRecord.project_id == project_id)
    if source is not None:
        query = query.filter(UsageRecord.source == normalize_source(source))
    return query.order_by(UsageRecord.created_at, UsageRecord.id).all()


def breakdown_by_source(
    records=None,
    *,
    user_id=None,
    workspace_id=None,
    project_id=None,
) -> dict[str, dict]:
    """Aggregate usage and cost by source.

    All known sources are returned (zeroed when unused) so consumers get a stable
    shape. Rows with a missing or unrecognised ``source`` are grouped under
    ``unknown`` only when such rows exist, which keeps malformed historical data
    visible without breaking the response.
    """
    if records is None:
        records = query_records(user_id=user_id, workspace_id=workspace_id, project_id=project_id)

    buckets: dict[str, list] = {source: [] for source in SOURCES}
    unknown = []
    for record in records:
        bucket = source_bucket(getattr(record, "source", None))
        if bucket == UNKNOWN_SOURCE:
            unknown.append(record)
        else:
            buckets.setdefault(bucket, []).append(record)

    result = {source: summarize_records(buckets[source]) for source in SOURCES}
    if unknown:
        result[UNKNOWN_SOURCE] = summarize_records(unknown)
    return result


def breakdown_payload(
    *,
    user_id=None,
    workspace_id=None,
    project_id=None,
) -> dict:
    """Build the JSON-friendly payload used by the breakdown API."""
    records = query_records(user_id=user_id, workspace_id=workspace_id, project_id=project_id)
    by_source = breakdown_by_source(records)
    totals = summarize_records(records)
    return {
        "by_source": by_source,
        "sources": [{"source": source, **stats} for source, stats in by_source.items()],
        "totals": totals,
        "known_sources": list(SOURCES),
    }


def _message_usage(message: Message) -> dict[str, int] | None:
    """Return validated token counts for a message, or ``None`` when absent."""
    values = (
        getattr(message, "prompt_tokens", None),
        getattr(message, "completion_tokens", None),
        getattr(message, "total_tokens", None),
    )
    if all(value is None for value in values):
        return None
    try:
        return normalize_usage(*values)
    except ValueError:
        return None


def _backfill_record_exists(
    message: Message,
    conversation_id: int,
    user_id: int | None,
    counts: dict[str, int],
) -> bool:
    """Return ``True`` when this message was already copied into the ledger.

    The ledger predates a durable ``message_id`` column, so a backfilled row is
    identified by its source, conversation, token counts, and the original
    message timestamp. That tuple is stable across runs, which makes the
    command idempotent without changing or annotating historical message rows.
    """
    if message.created_at is None:
        return False
    return (
        UsageRecord.query.filter_by(
            source=SOURCE_CHAT,
            conversation_id=conversation_id,
            user_id=user_id,
            prompt_tokens=counts["prompt_tokens"],
            completion_tokens=counts["completion_tokens"],
            total_tokens=counts["total_tokens"],
        )
        .filter(UsageRecord.created_at == message.created_at)
        .first()
        is not None
    )


def backfill_from_messages(
    *,
    batch_size: int = BACKFILL_DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Copy historical chat-message token counts into the usage ledger.

    The operation is keyset-paginated and idempotent: existing message rows
    are never modified, and re-running the command only skips rows that were
    already backfilled. ``limit`` bounds the number of messages considered in a
    single invocation, which is useful for a staged rollout. ``dry_run`` reports
    what would be written without committing anything.

    Raises:
        ValueError: If ``batch_size``/``limit`` is outside the supported range.
    """
    if not isinstance(batch_size, int) or not 1 <= batch_size <= BACKFILL_MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be an integer between 1 and {BACKFILL_MAX_BATCH_SIZE}.")
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a non-negative integer.")

    stats = {
        "source": SOURCE_CHAT,
        "scanned": 0,
        "created": 0,
        "skipped_existing": 0,
        "skipped_incomplete": 0,
        "skipped_orphan": 0,
        "batches": 0,
        "batch_size": batch_size,
        "limit": limit,
        "dry_run": bool(dry_run),
    }
    last_id = 0
    processed = 0

    try:
        while limit is None or processed < limit:
            size = batch_size if limit is None else min(batch_size, limit - processed)
            if size <= 0:
                break
            messages = (
                Message.query.filter(Message.id > last_id)
                .filter(
                    or_(
                        Message.prompt_tokens.isnot(None),
                        Message.completion_tokens.isnot(None),
                        Message.total_tokens.isnot(None),
                    )
                )
                .order_by(Message.id)
                .limit(size)
                .all()
            )
            if not messages:
                break

            stats["batches"] += 1
            for message in messages:
                if limit is not None and processed >= limit:
                    break
                last_id = message.id
                processed += 1
                stats["scanned"] += 1

                counts = _message_usage(message)
                if counts is None:
                    stats["skipped_incomplete"] += 1
                    continue
                conversation = message.conversation
                if conversation is None:
                    stats["skipped_orphan"] += 1
                    continue
                if _backfill_record_exists(
                    message,
                    message.conversation_id,
                    conversation.user_id,
                    counts,
                ):
                    stats["skipped_existing"] += 1
                    continue
                if dry_run:
                    stats["created"] += 1
                    continue

                record = record_usage(
                    source=SOURCE_CHAT,
                    prompt_tokens=counts["prompt_tokens"],
                    completion_tokens=counts["completion_tokens"],
                    total_tokens=counts["total_tokens"],
                    # Historical columns do not record whether the provider
                    # reported the counts or they were estimated.
                    tokens_estimated=True,
                    user_id=conversation.user_id,
                    conversation_id=message.conversation_id,
                    commit=False,
                )
                record.created_at = message.created_at
                stats["created"] += 1

            if not dry_run:
                db.session.commit()
            if len(messages) < size:
                break
    except Exception:
        db.session.rollback()
        raise

    return stats


__all__ = [
    "BACKFILL_DEFAULT_BATCH_SIZE",
    "BACKFILL_MAX_BATCH_SIZE",
    "SOURCES",
    "SOURCE_ANALYSIS",
    "SOURCE_CHAT",
    "SOURCE_PROJECT_CHAT",
    "SOURCE_REVIEW",
    "SOURCE_TOOLS",
    "STATUS_DENIED",
    "STATUS_ERROR",
    "STATUS_SUCCESS",
    "UNKNOWN_SOURCE",
    "backfill_from_messages",
    "breakdown_by_source",
    "breakdown_payload",
    "current_provider_metadata",
    "estimate_cost",
    "normalize_source",
    "normalize_usage",
    "query_records",
    "record_usage",
    "source_bucket",
    "summarize_records",
]
