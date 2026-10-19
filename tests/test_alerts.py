"""Offline tests for billing alerts and digests."""

from __future__ import annotations

import importlib
import inspect

import pytest


def _load_module():
    for name in ("app.services.alerts", "services.alerts", "alerts"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    pytest.skip("alert service module not found", allow_module_level=True)


alerts = _load_module()


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, recipient: str, body: str) -> None:
        self.sent.append((recipient, body))


def _call_alert(func, kwargs):
    sig = inspect.signature(func)
    accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return func(**accepted)


def test_threshold_fires_once_per_window():
    sender = _Recorder()
    service = alerts.AlertService(sender=sender.send)
    service.set_threshold(user_id="u1", amount=100)

    service.record_usage(user_id="u1", amount=60)
    assert sender.sent == []

    service.record_usage(user_id="u1", amount=50)
    assert len(sender.sent) == 1

    service.record_usage(user_id="u1", amount=50)
    assert len(sender.sent) == 1


def test_dedupe_key_identical_events():
    sender = _Recorder()
    service = alerts.AlertService(sender=sender.send)
    service.set_threshold(user_id="u2", amount=10)

    service.record_usage(user_id="u2", amount=20)
    service.record_usage(user_id="u2", amount=20)
    assert len(sender.sent) == 1


def test_preferences_disable_alerts():
    sender = _Recorder()
    service = alerts.AlertService(sender=sender.send)
    service.set_threshold(user_id="u3", amount=5)
    service.set_preferences(user_id="u3", enabled=False)

    service.record_usage(user_id="u3", amount=100)
    assert sender.sent == []


def test_email_fallback_when_no_recipient():
    sender = _Recorder()
    service = alerts.AlertService(sender=sender.send, default_recipient="billing@ops.local")
    service.set_threshold(user_id="u4", amount=1)

    service.record_usage(user_id="u4", amount=10)
    assert len(sender.sent) == 1
    assert sender.sent[0][0] == "billing@ops.local"


def test_digiest_summarizes_usage():
    sender = _Recorder()
    service = alerts.AlertService(sender=sender.send)
    service.record_usage(user_id="u5", amount=15)
    service.record_usage(user_id="u5", amount=25)

    digest = service.build_digest(user_id="u5")
    assert "40" in str(digest)

    service.send_digest(user_id="u5", recipient="u5@local")
    assert len(sender.sent) == 1
    assert sender.sent[0][0] == "u5@local"
