"""Prompt template version history model.

Every save of a :class:`~app.models.prompt.Prompt` is recorded as an immutable
``PromptVersion`` row so the prompt detail view can show a version timeline with
diffs and users can revert to an earlier version.

``prompt_id`` is intentionally a plain indexed integer rather than a foreign key
with ``ON DELETE CASCADE``: when a prompt is deleted its version history must
survive for the configured retention period (``PROMPT_VERSION_RETENTION_DAYS``),
so the rows cannot be cascaded away. ``prompt_deleted_at`` records when the
parent prompt was deleted so the history can be purged once the retention
window elapses.
"""

from datetime import UTC, datetime

from app.extensions import db


class PromptVersion(db.Model):
    """An immutable snapshot of a prompt template at one point in time."""

    __tablename__ = "prompt_versions"
    __table_args__ = (
        db.UniqueConstraint("prompt_id", "version", name="uq_prompt_versions_prompt_version"),
    )

    id = db.Column(db.Integer, primary_key=True)
    # Not a FK on purpose (see module docstring): history outlives the prompt.
    prompt_id = db.Column(db.Integer, nullable=False, index=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(80), nullable=False, default="General")
    changed_by = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    # Set when the owning prompt is deleted; used for retention-based purging.
    prompt_deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        """Serialize the version for JSON API responses."""
        return {
            "id": self.id,
            "prompt_id": self.prompt_id,
            "version": self.version,
            "title": self.title,
            "content": self.content,
            "category": self.category,
            "changed_by": self.changed_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "prompt_deleted_at": (
                self.prompt_deleted_at.isoformat() if self.prompt_deleted_at else None
            ),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PromptVersion id={self.id} prompt_id={self.prompt_id} version={self.version}>"
