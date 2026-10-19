"""Tests for the Stellar notifier example plugin (issue #415).

The example must stay valid against the published manifest schema, load through
the real plugin services, and handle the ``stellar.analysis.completed`` event
path offline with a bounded, read-only summary.
"""

from __future__ import annotations

import copy
import importlib
import json
import re
import socket
import sys
from pathlib import Path

import pytest

from app.services.events import Event, get_dispatcher
from app.services.plugins import Plugin, PluginManifest, PluginRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "plugins" / "stellar_notifier"
PLUGINS_DIR = REPO_ROOT / "examples" / "plugins"
MANIFEST_PATH = EXAMPLE_DIR / "manifest.json"
SCHEMA_PATH = REPO_ROOT / "plugins" / "plugin.schema.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def example_module(monkeypatch):
    """Import ``stellar_notifier.plugin`` from the examples directory."""
    monkeypatch.syspath_prepend(str(PLUGINS_DIR))
    sys.modules.pop("stellar_notifier.plugin", None)
    yield importlib.import_module("stellar_notifier.plugin")
    sys.modules.pop("stellar_notifier.plugin", None)


class TestManifest:
    def test_manifest_matches_published_schema(self):
        manifest = _load_json(MANIFEST_PATH)
        schema = _load_json(SCHEMA_PATH)
        properties = schema["properties"]

        assert set(schema["required"]) <= set(manifest)
        assert schema["additionalProperties"] is False
        assert set(manifest) <= set(properties)

        assert re.match(properties["id"]["pattern"], manifest["id"])
        assert re.match(properties["version"]["pattern"], manifest["version"])
        assert re.match(properties["entry_point"]["pattern"], manifest["entry_point"])

        allowed = set(properties["capabilities"]["items"]["enum"])
        assert manifest["capabilities"] == ["STELLAR_READ"]
        assert set(manifest["capabilities"]) <= allowed

    def test_manifest_parses_with_app_validator(self):
        manifest = PluginManifest.from_file(MANIFEST_PATH)

        assert manifest.id == "stellar-notifier"
        assert manifest.entry_point == "stellar_notifier.plugin:StellarNotifierPlugin"
        assert manifest.capabilities == ["STELLAR_READ"]
        assert manifest.compatibility == ">=0.1.0"


class TestLoadAndRegister:
    def test_plugin_loads_and_registers(self, app, example_module):
        manifest = PluginManifest.from_file(MANIFEST_PATH)
        registry = PluginRegistry()
        plugin = Plugin(manifest)

        plugin.load()
        registry.register(plugin)

        assert registry.get("stellar-notifier") is plugin
        assert plugin.module is not None
        assert getattr(plugin.module, "StellarNotifierPlugin", None) is not None

    def test_plugin_subscribes_to_the_stellar_event(self, app, example_module):
        plugin = Plugin(PluginManifest.from_file(MANIFEST_PATH))
        plugin.load()
        instance = plugin.get_instance()

        instance.register()
        try:
            subscribers = get_dispatcher().list_subscribers("stellar.analysis.completed")
            assert "stellar-notifier" in subscribers["stellar.analysis.completed"]
        finally:
            instance.on_disable()

        assert (
            "stellar-notifier"
            not in get_dispatcher().list_subscribers("stellar.analysis.completed")[
                "stellar.analysis.completed"
            ]
        )


class TestEventPath:
    def test_event_produces_a_summary(self, app, example_module):
        instance = example_module.StellarNotifierPlugin()
        event = Event(
            event_type="stellar.analysis.completed",
            data={"project_id": 7, "detected": True, "confidence": 0.98},
        )

        summary = instance.on_event(event)

        assert summary["plugin"] == "stellar-notifier"
        assert summary["event"] == "stellar.analysis.completed"
        assert summary["project_id"] == 7
        assert summary["detected"] is True
        assert "Project 7" in summary["summary"]
        assert "Stellar detected" in summary["summary"]
        assert len(summary["summary"]) <= example_module.MAX_SUMMARY_CHARS
        assert instance.recent_summaries == [summary]

    def test_summary_is_bounded_for_large_payloads(self, app, example_module):
        instance = example_module.StellarNotifierPlugin()
        event = Event(
            event_type="stellar.analysis.completed",
            data={
                "project_id": "p" * 500,
                "detected": False,
                "confidence": "9" * 500,
                "network": "n" * 500,
                "summary": "s" * 10_000,
            },
        )

        summary = instance.on_event(event)

        assert len(summary["summary"]) <= example_module.MAX_SUMMARY_CHARS
        assert len(summary["network"]) <= example_module.MAX_FIELD_CHARS
        assert summary["truncated"] is True

    def test_handler_does_not_mutate_the_event_payload(self, app, example_module):
        instance = example_module.StellarNotifierPlugin()
        payload = {"project_id": 11, "detected": True, "confidence": 0.5}
        event = Event(event_type="stellar.analysis.completed", data=payload)
        before = copy.deepcopy(event.data)

        instance.on_event(event)

        assert event.data == before

    def test_history_stays_bounded(self, app, example_module):
        instance = example_module.StellarNotifierPlugin()
        for project_id in range(example_module.MAX_RECENT_SUMMARIES + 15):
            instance.on_event(
                Event(
                    event_type="stellar.analysis.completed",
                    data={"project_id": project_id, "detected": True},
                )
            )

        assert len(instance.recent_summaries) == example_module.MAX_RECENT_SUMMARIES
        assert instance.recent_summaries[0]["project_id"] == 15

    def test_event_handling_uses_no_network(self, app, example_module, monkeypatch):
        def forbidden(*args, **kwargs):
            raise AssertionError("the example plugin must stay offline")

        monkeypatch.setattr(socket.socket, "connect", forbidden)
        monkeypatch.setattr(socket, "create_connection", forbidden)

        instance = example_module.StellarNotifierPlugin()
        summary = instance.on_event(
            Event(
                event_type="stellar.analysis.completed",
                data={"project_id": 3, "detected": True, "confidence": 0.4},
            )
        )

        assert summary["project_id"] == 3

    def test_demo_main_runs_offline(self, example_module, capsys):
        example_module.main()

        assert "stellar-notifier: Project 1" in capsys.readouterr().out
