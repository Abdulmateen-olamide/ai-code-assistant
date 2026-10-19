"""PEP 440 plugin/application compatibility checks.

A plugin manifest declares the application versions it supports in its
``compatibility`` field (e.g. ``>=0.8.0``). This module compares that field
against the running application version using the standard ``packaging``
specifier engine (PEP 440: ``==``, ``!=``, ``<=``, ``>=``, ``<``, ``>``,
``~=``, ``===``, wildcards, and comma/space-separated clauses).

The application version is read from installed package metadata first and from
``pyproject.toml`` as a fallback so the check also works when running from a
source checkout.

Rules:

* An **invalid** specifier never matches and is reported as invalid — callers
  reject such a manifest with a clear message instead of silently accepting it.
* An empty specifier set matches every version (PEP 440 semantics), so a plugin
  that declares no restriction is always compatible.
* Prerelease application versions are allowed to satisfy a specifier.
"""

from __future__ import annotations

import logging
import tomllib
from functools import lru_cache
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from app.services.plugin_versions import InvalidPluginVersionError, parse_version

logger = logging.getLogger(__name__)

#: Value used when the application version cannot be determined. Kept as a
#: valid PEP 440 version so comparison never crashes; operators should notice
#: this only in broken/misconfigured environments.
_FALLBACK_APP_VERSION = "0.0.0"


class InvalidCompatibilityError(ValueError):
    """Raised when a compatibility field is not a valid PEP 440 specifier."""


class IncompatibleCompatibilityError(ValueError):
    """Raised when a valid compatibility range excludes the running app."""


@lru_cache(maxsize=1)
def _pyproject_version() -> str | None:
    """Read the ``project.version`` from the repository ``pyproject.toml``."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        logger.debug("Could not read application version from %s", pyproject, exc_info=True)
        return None
    return data.get("project", {}).get("version")


@lru_cache(maxsize=1)
def app_version() -> str:
    """Return the running application version (installed metadata or pyproject)."""
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _version

        return _version("ai-code-assistant")
    except (PackageNotFoundError, Exception):
        pass
    return _pyproject_version() or _FALLBACK_APP_VERSION


def parse_compatibility(value: str | None) -> SpecifierSet:
    """Parse a ``compatibility`` value into a :class:`SpecifierSet`.

    ``None``/empty means "no restriction". Raises :class:`InvalidCompatibilityError`
    when the value is not a valid PEP 440 specifier.
    """
    if value is None:
        return SpecifierSet("")
    if not isinstance(value, str):
        raise InvalidCompatibilityError(f"Invalid compatibility specifier: {value!r}")
    try:
        return SpecifierSet(value.strip())
    except (InvalidSpecifier, ValueError) as exc:
        raise InvalidCompatibilityError(f"Invalid compatibility specifier: {value!r}") from exc


def is_valid_compatibility(value: str | None) -> bool:
    """Return ``True`` when ``value`` is a valid (possibly empty) PEP 440 specifier."""
    if value is None:
        return True
    try:
        parse_compatibility(value)
    except InvalidCompatibilityError:
        return False
    return True


def plugin_compatible(compatibility: str | None, version: str | None = None) -> bool:
    """Return ``True`` when ``compatibility`` is satisfied by ``version``.

    ``version`` defaults to :func:`app_version`. An invalid specifier or an
    unparseable application version never matches (fail closed).
    """
    try:
        specifier = parse_compatibility(compatibility)
        target = parse_version((version or "").strip() or app_version())
        return specifier.contains(target, prereleases=True)
    except (InvalidCompatibilityError, InvalidPluginVersionError):
        return False
    except ValueError:  # additional defensive fallback -> not compatible
        return False


def compatibility_status(compatibility: str | None) -> dict[str, object]:
    """Return a metadata-friendly compatibility summary.

    Keys: ``compatibility`` (normalized string, empty when unrestricted),
    ``app_version``, ``valid``, and ``compatible``. Used for the plugin metadata
    badge/field and for the registration-time rejection message.
    """
    valid = is_valid_compatibility(compatibility)
    return {
        "compatibility": (compatibility or "").strip(),
        "app_version": app_version(),
        "valid": valid,
        "compatible": plugin_compatible(compatibility) if valid else False,
    }


def enforce_compatible(compatibility: str | None, *, subject: str = "Plugin") -> None:
    """Refuse a plugin whose declared range excludes the running app version.

    ``subject`` names the thing being checked (for example
    ``"Plugin stellar-tools v1.2.0"``) and appears verbatim in the error, which
    always reports both the declared range and the running version so the
    operator can act on it. An omitted/empty range is unrestricted and never
    raises.

    Raises:
        InvalidCompatibilityError: If ``compatibility`` is not valid PEP 440.
        IncompatibleCompatibilityError: If the range excludes this app version.
    """
    status = compatibility_status(compatibility)
    if not status["valid"]:
        raise InvalidCompatibilityError(f"Invalid compatibility specifier: {compatibility!r}")
    if status["compatible"]:
        return
    raise IncompatibleCompatibilityError(
        f"{subject} requires application {status['compatibility']}, "
        f"but this app is {status['app_version']}."
    )


__all__ = [
    "IncompatibleCompatibilityError",
    "InvalidCompatibilityError",
    "app_version",
    "compatibility_status",
    "enforce_compatible",
    "is_valid_compatibility",
    "parse_compatibility",
    "plugin_compatible",
]
