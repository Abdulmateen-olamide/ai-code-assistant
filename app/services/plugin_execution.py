"""Bounded execution for plugin callbacks (issue #408).

A plugin handler or lifecycle hook is third-party code: it may block on I/O,
loop forever, or simply take far longer than a request can afford. This module
runs such callbacks in a short-lived daemon thread and stops waiting once the
configured budget expires, so one slow plugin cannot stall a request.

The budget is ``PLUGIN_EXECUTION_TIMEOUT`` (seconds, default
:data:`DEFAULT_TIMEOUT_SECONDS`). A value of ``0`` (or negative) disables the
bound for local debugging.

Limitations (Python cannot kill threads):

* A callback that exceeds the budget is **abandoned**, not interrupted. It is a
  daemon thread, so it never blocks interpreter shutdown, but it may still be
  running (and finish) in the background. Callbacks must therefore be
  idempotent and must not be relied on to have stopped.
* The current Flask application context is copied into the worker thread when
  one is active, so plugins can still use ``current_app``. The calling thread
  blocks until the callback finishes or the budget expires.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Fallback budget used when the application configuration is unavailable.
DEFAULT_TIMEOUT_SECONDS = 5.0

#: Upper bound applied to the operation label stored in error reports.
_MAX_OPERATION_CHARS = 160


class PluginTimeoutError(TimeoutError):
    """Raised when a plugin callback exceeds its execution budget."""

    def __init__(self, operation: str, timeout: float) -> None:
        self.operation = str(operation)[:_MAX_OPERATION_CHARS]
        self.timeout = timeout
        super().__init__(f"Plugin operation '{self.operation}' timed out after {timeout:g}s")


def configured_timeout() -> float:
    """Return the configured callback budget in seconds (``<= 0`` disables it)."""
    value: Any = DEFAULT_TIMEOUT_SECONDS
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            value = current_app.config.get("PLUGIN_EXECUTION_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    except Exception:  # pragma: no cover - Flask is always available in the app
        logger.debug("Could not read the plugin execution timeout", exc_info=True)
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return DEFAULT_TIMEOUT_SECONDS


def _preserve_app_context(func: Callable[[], Any]) -> Callable[[], Any]:
    """Return ``func`` wrapped so an active Flask app context is available."""
    try:
        from flask import copy_current_app_context, has_app_context

        if not has_app_context():
            return func
        return copy_current_app_context(func)
    except Exception:  # pragma: no cover - context copying is best effort
        return func


def run_with_timeout(
    func: Callable[[], Any],
    *,
    operation: str,
    timeout: float | None = None,
) -> Any:
    """Call ``func`` and raise :class:`PluginTimeoutError` if it overruns.

    Args:
        func: Zero-argument callable to execute.
        operation: Short label (for example ``dispatch:project.created``) used
            in the timeout error and the recorded error report.
        timeout: Budget in seconds; defaults to :func:`configured_timeout`.
            A value ``<= 0`` disables the bound and calls ``func`` directly.

    Returns:
        Whatever ``func`` returns.

    Raises:
        PluginTimeoutError: If ``func`` does not finish within the budget.
        Exception: Any exception raised by ``func`` is re-raised unchanged.
    """
    limit = configured_timeout() if timeout is None else float(timeout)
    if limit <= 0:
        return func()

    call = _preserve_app_context(func)
    outcome: dict[str, Any] = {}
    finished = threading.Event()

    def _target() -> None:
        try:
            outcome["result"] = call()
        except BaseException as exc:  # re-raised in the caller thread below
            outcome["error"] = exc
        finally:
            finished.set()

    worker = threading.Thread(
        target=_target,
        name=f"plugin-callback:{str(operation)[:_MAX_OPERATION_CHARS]}",
        daemon=True,
    )
    worker.start()

    if not finished.wait(limit):
        logger.warning(
            "Plugin operation %s exceeded its %.3fs budget; abandoning the callback",
            operation,
            limit,
        )
        raise PluginTimeoutError(operation, limit)

    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "PluginTimeoutError",
    "configured_timeout",
    "run_with_timeout",
]
