"""Prompt template version history service.

Records an immutable snapshot on every prompt save, builds a version timeline
with unified diffs, performs reverts (which themselves create a new version),
and purges the history of deleted prompts once the configured retention window
has elapsed.
"""

from __future__ import annotations

import difflib
from datetime import UTC, datetime, timedelta

from flask import current_app

from app.extensions import db
from app.models import PromptVersion


def _next_version_number(prompt_id: int) -> int:
    last = (
        PromptVersion.query.filter_by(prompt_id=prompt_id)
        .order_by(PromptVersion.version.desc())
        .first()
    )
    return (last.version + 1) if last else 1


def record_version(prompt, *, changed_by: int | None = None) -> PromptVersion:
    """Snapshot the current state of ``prompt`` as a new version row."""
    version = PromptVersion(
        prompt_id=prompt.id,
        version=_next_version_number(prompt.id),
        title=prompt.title,
        content=prompt.content,
        category=prompt.category,
        changed_by=changed_by,
    )
    db.session.add(version)
    db.session.flush()
    return version


def list_versions(prompt_id: int) -> list[PromptVersion]:
    """Return all versions of a prompt, oldest first."""
    return (
        PromptVersion.query.filter_by(prompt_id=prompt_id)
        .order_by(PromptVersion.version.asc())
        .all()
    )


def get_version(prompt_id: int, version_id: int) -> PromptVersion | None:
    """Return a single version by id, scoped to ``prompt_id``."""
    return PromptVersion.query.filter_by(id=version_id, prompt_id=prompt_id).first()


def unified_diff(previous: PromptVersion | None, current: PromptVersion) -> str:
    """Return a unified diff from ``previous`` (or empty) to ``current``."""
    old_lines = previous.content.splitlines() if previous is not None else []
    fromfile = f"v{previous.version}" if previous is not None else "(new)"
    diff = difflib.unified_diff(
        old_lines,
        current.content.splitlines(),
        fromfile=fromfile,
        tofile=f"v{current.version}",
        lineterm="",
    )
    return "\n".join(diff)


def _previous(version: PromptVersion) -> PromptVersion | None:
    return (
        PromptVersion.query.filter_by(prompt_id=version.prompt_id)
        .filter(PromptVersion.version < version.version)
        .order_by(PromptVersion.version.desc())
        .first()
    )


def version_payload(version: PromptVersion) -> dict:
    """Serialize a version with a diff against the version before it."""
    payload = version.to_dict()
    payload["diff"] = unified_diff(_previous(version), version)
    return payload


def version_timeline(prompt_id: int) -> list[dict]:
    """Return every version of a prompt (oldest first) with per-version diffs."""
    return [version_payload(version) for version in list_versions(prompt_id)]


def resolve_version(prompt_id: int, selector) -> PromptVersion | None:
    """Resolve a version selector (row id or version number) to a version row."""
    try:
        selector = int(selector)
    except (TypeError, ValueError):
        return None
    return (
        get_version(prompt_id, selector)
        or PromptVersion.query.filter_by(prompt_id=prompt_id, version=selector).first()
    )


def revert_to_version(prompt, version: PromptVersion, *, changed_by: int | None = None):
    """Apply ``version``'s snapshot to ``prompt`` and record it as a new version."""
    prompt.title = version.title
    prompt.content = version.content
    prompt.category = version.category
    return record_version(prompt, changed_by=changed_by)


def mark_prompt_deleted(prompt_id: int) -> None:
    """Mark a prompt's versions as orphaned so retention purging can find them."""
    PromptVersion.query.filter_by(prompt_id=prompt_id, prompt_deleted_at=None).update(
        {"prompt_deleted_at": datetime.now(UTC)}, synchronize_session=False
    )


def purge_expired_versions(*, retention_days: int | None = None) -> int:
    """Delete version rows whose prompt was deleted beyond the retention window.

    Returns the number of rows removed.
    """
    if retention_days is None:
        retention_days = current_app.config.get("PROMPT_VERSION_RETENTION_DAYS", 30)
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    return PromptVersion.query.filter(
        PromptVersion.prompt_deleted_at.isnot(None),
        PromptVersion.prompt_deleted_at < cutoff,
    ).delete(synchronize_session=False)
