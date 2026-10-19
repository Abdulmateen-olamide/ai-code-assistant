"""Stellar notifier example plugin (issue #415).

Subscribes to ``stellar.analysis.completed`` and turns each event into a short,
bounded, human-readable summary. The plugin is deliberately:

* **read-only** - it copies the event payload and never mutates it, and never
  writes to the database;
* **offline** - it performs no network requests, so the whole example runs
  against the mock network (and in tests) without credentials;
* **bounded** - every field and the final summary are truncated, and the
  in-memory history keeps at most :data:`MAX_RECENT_SUMMARIES` entries, so a
  noisy or hostile event cannot grow memory without limit.

Importing this module has no side effects: nothing is subscribed until
``register()`` (called from ``on_enable``) runs.
"""

from __future__ import annotations

import logging

from app.services.events import get_dispatcher

logger = logging.getLogger(__name__)

PLUGIN_ID = "stellar-notifier"
EVENT_TYPE = "stellar.analysis.completed"

#: Maximum characters kept for the formatted one-line summary.
MAX_SUMMARY_CHARS = 280
#: Maximum characters kept for an individual payload field in the summary.
MAX_FIELD_CHARS = 80
#: Maximum number of summaries retained in memory.
MAX_RECENT_SUMMARIES = 20
#: Field names copied from the event payload (everything else is ignored).
SUMMARY_FIELDS = ("project_id", "detected", "confidence", "network")


def _bounded_with_flag(value: object, limit: int) -> tuple[str, bool]:
    """Return ``value`` as a bounded single-line string and whether it was cut."""
    if value is None:
        return "", False
    text = " ".join(str(value).split())
    if len(text) > limit:
        return text[: max(limit - 1, 0)].rstrip() + "…", True
    return text, False


def _bounded(value: object, limit: int) -> str:
    """Return ``value`` as a single-line string of at most ``limit`` chars."""
    return _bounded_with_flag(value, limit)[0]


class StellarNotifierPlugin:
    """Format bounded summaries for ``stellar.analysis.completed`` events."""

    def __init__(self, app=None, manifest=None):
        self.app = app
        self.manifest = manifest
        self.recent_summaries: list[dict] = []

    @staticmethod
    def format_summary(event) -> dict:
        """Return a bounded, read-only summary of ``event``.

        Only :data:`SUMMARY_FIELDS` are read from the payload; free-form or
        unrelated keys are never copied, so the summary cannot leak arbitrary
        event content. The payload itself is not modified.
        """
        data = dict(event.data or {})
        project_id = data.get("project_id")
        detected = bool(data.get("detected"))
        confidence = data.get("confidence")
        project_text, project_cut = _bounded_with_flag(project_id, MAX_FIELD_CHARS)
        confidence_text, confidence_cut = _bounded_with_flag(confidence, 32)
        network, network_cut = _bounded_with_flag(data.get("network"), MAX_FIELD_CHARS)

        parts = [f"Project {project_text or 'unknown'}"]
        parts.append("Stellar detected" if detected else "No Stellar detected")
        if confidence is not None:
            parts.append(f"confidence {confidence_text}")
        if network:
            parts.append(f"network {network}")
        raw_summary = f"{parts[0]} — " + ", ".join(parts[1:])
        summary = _bounded(raw_summary, MAX_SUMMARY_CHARS)

        return {
            "plugin": PLUGIN_ID,
            "event": event.event_type,
            "project_id": project_id,
            "detected": detected,
            "confidence": confidence,
            "network": network or None,
            "summary": summary,
            "truncated": project_cut or confidence_cut or network_cut or summary != raw_summary,
        }

    def register(self) -> None:
        """Subscribe to ``stellar.analysis.completed`` (idempotent)."""
        dispatcher = get_dispatcher()
        dispatcher.unsubscribe(EVENT_TYPE, self.on_event)
        dispatcher.subscribe(EVENT_TYPE, self.on_event, plugin_id=PLUGIN_ID)

    def on_event(self, event):
        """Handle one event and keep a bounded in-memory history."""
        summary = self.format_summary(event)
        self.recent_summaries.append(summary)
        # Keep only the most recent entries; never let the history grow.
        del self.recent_summaries[:-MAX_RECENT_SUMMARIES]
        logger.info("stellar-notifier: %s", summary["summary"])
        return summary

    def on_enable(self) -> None:
        """Lifecycle hook: subscribe once the plugin is enabled."""
        self.register()

    def on_disable(self) -> None:
        """Lifecycle hook: stop receiving events while disabled."""
        get_dispatcher().unsubscribe(EVENT_TYPE, self.on_event)


def main() -> None:
    """Offline demo: register, handle one synthetic event, unsubscribe."""
    from app.services.events import Event

    instance = StellarNotifierPlugin()
    instance.register()
    try:
        summary = instance.on_event(
            Event(
                event_type=EVENT_TYPE,
                data={"project_id": 1, "detected": True, "confidence": 0.98},
            )
        )
        print(f"{PLUGIN_ID}: {summary['summary']}")
    finally:
        instance.on_disable()


if __name__ == "__main__":  # pragma: no cover - manual demo entry point
    main()
