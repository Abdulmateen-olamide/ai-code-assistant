"""Tests for bounded plugin execution (issue #408).

A deliberately slow handler/hook must not hold a request hostage: dispatch
abandons it after the configured budget, records one bounded error report, and
continues with the remaining handlers.
"""

import threading
import time
import types

import pytest

from app.extensions import db
from app.models import Plugin as PluginRow
from app.models import PluginErrorReport
from app.models.plugin_error_report import MAX_ERROR_MESSAGE_CHARS
from app.services.events import Event, EventDispatcher, get_dispatcher
from app.services.plugin_execution import (
    DEFAULT_TIMEOUT_SECONDS,
    PluginTimeoutError,
    configured_timeout,
    run_with_timeout,
)
from app.services.plugins import Plugin, PluginManifest, PluginRegistry


class TestRunWithTimeout:
    def test_returns_the_callback_result(self):
        assert run_with_timeout(lambda: 42, operation="test", timeout=1) == 42

    def test_reraises_callback_errors_unchanged(self):
        error = ValueError("boom")

        def fail():
            raise error

        with pytest.raises(ValueError) as excinfo:
            run_with_timeout(fail, operation="test", timeout=1)
        assert excinfo.value is error

    def test_slow_callback_times_out(self):
        release = threading.Event()
        try:
            with pytest.raises(PluginTimeoutError) as excinfo:
                run_with_timeout(
                    lambda: release.wait(5),
                    operation="dispatch:project.created",
                    timeout=0.05,
                )
        finally:
            release.set()

        assert excinfo.value.operation == "dispatch:project.created"
        assert excinfo.value.timeout == pytest.approx(0.05)
        assert "timed out" in str(excinfo.value)

    def test_non_positive_timeout_disables_the_bound(self):
        assert run_with_timeout(lambda: time.sleep(0.05), operation="test", timeout=0) is None
        assert run_with_timeout(lambda: 7, operation="test", timeout=-1) == 7

    def test_configured_timeout_reads_the_app_config(self, app):
        app.config["PLUGIN_EXECUTION_TIMEOUT"] = 0.25
        assert configured_timeout() == pytest.approx(0.25)

    def test_configured_timeout_falls_back_when_unset(self, app):
        app.config.pop("PLUGIN_EXECUTION_TIMEOUT", None)
        assert configured_timeout() == DEFAULT_TIMEOUT_SECONDS


class TestDispatchTimeout:
    def _slow(self, release):
        def handler(event):
            release.wait(5)

        return handler

    def test_slow_handler_does_not_block_other_handlers(self, app):
        app.config["PLUGIN_EXECUTION_TIMEOUT"] = 0.05
        release = threading.Event()
        calls = []
        dispatcher = get_dispatcher()

        def fast(event):
            calls.append("fast")

        dispatcher.subscribe("project.created", self._slow(release))
        dispatcher.subscribe("project.created", fast)
        try:
            started = time.perf_counter()
            result = dispatcher.dispatch(Event(event_type="project.created", data={}))
            elapsed = time.perf_counter() - started
        finally:
            release.set()

        assert result["failed"] == 1
        assert result["successful"] == 1
        assert calls == ["fast"]
        assert isinstance(result["errors"][0][1], PluginTimeoutError)
        assert elapsed < 1.0

    def test_disabled_timeout_allows_slow_handler(self, app):
        app.config["PLUGIN_EXECUTION_TIMEOUT"] = 0
        dispatcher = EventDispatcher()

        def slow(event):
            time.sleep(0.05)

        dispatcher.subscribe("project.created", slow)
        result = dispatcher.dispatch(Event(event_type="project.created", data={}))

        assert result["successful"] == 1
        assert result["failed"] == 0

    def test_timed_out_plugin_handler_records_a_bounded_error_report(self, app):
        app.config["PLUGIN_EXECUTION_TIMEOUT"] = 0.05
        db.session.add(
            PluginRow(
                id="slow-plugin",
                name="Slow plugin",
                version="0.1.0",
                entry_point="slow_plugin:SlowPlugin",
                capabilities=["GITHUB_READ"],
                enabled=True,
            )
        )
        db.session.commit()

        release = threading.Event()
        dispatcher = get_dispatcher()
        dispatcher.subscribe("github.connected", self._slow(release), plugin_id="slow-plugin")
        try:
            result = dispatcher.dispatch(Event(event_type="github.connected", data={}))
        finally:
            release.set()

        assert result["failed"] == 1
        report = (
            PluginErrorReport.query.filter_by(plugin_id="slow-plugin")
            .order_by(PluginErrorReport.id.desc())
            .first()
        )
        assert report is not None
        assert report.operation == "dispatch:github.connected"
        assert "timed out" in report.message
        assert len(report.message) <= MAX_ERROR_MESSAGE_CHARS


class TestLifecycleHookTimeout:
    def _registry_with_slow_hook(self, release):
        class SlowHookPlugin:
            def __init__(self, app=None, manifest=None):
                pass

            def on_enable(self):
                release.wait(5)

        module = types.ModuleType("slow_hook_mod")
        module.SlowHookPlugin = SlowHookPlugin
        manifest = PluginManifest(
            id="slow-hook",
            name="Slow hook",
            version="0.1.0",
            description="test",
            author="tester",
            entry_point="slow_hook_mod:SlowHookPlugin",
            capabilities=["PROJECT_READ"],
        )
        plugin = Plugin(manifest)
        plugin.module = module
        registry = PluginRegistry()
        registry.register(plugin)
        return registry, plugin

    def test_slow_lifecycle_hook_times_out_but_state_changes(self, app):
        app.config["PLUGIN_EXECUTION_TIMEOUT"] = 0.05
        release = threading.Event()
        registry, plugin = self._registry_with_slow_hook(release)
        # Plugins start enabled; disable first so enable() actually runs the hook.
        plugin.enabled = False

        try:
            started = time.perf_counter()
            registry.enable("slow-hook")
            elapsed = time.perf_counter() - started
        finally:
            release.set()

        assert plugin.enabled is True
        assert elapsed < 1.0
        report = (
            PluginErrorReport.query.filter_by(plugin_id="slow-hook")
            .order_by(PluginErrorReport.id.desc())
            .first()
        )
        assert report is not None
        assert report.operation == "hook:on_enable"
        assert "timed out" in report.message
