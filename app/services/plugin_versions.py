"""PEP 440 plugin version comparison helpers (#399).

Plugin versions are PEP 440 versions (the manifest already validates them as
semantic versions, which are a subset of PEP 440). This module wraps
:mod:`packaging.version` behind a small, uniform API so update checks,
dependency checks, and lockfile ordering compare versions the same way and
report invalid values consistently instead of raising bare ``packaging``
errors.

The helpers are intentionally side-effect free and never coerce a bad value
into a "comparable" one: an invalid version raises
:class:`InvalidPluginVersionError` (or is reported as invalid by
:func:`is_valid_version`). Callers on a security-sensitive path should fail
closed rather than treating an unparseable version as newest.

Examples:
    >>> from app.services.plugin_versions import compare_versions, sort_versions
    >>> compare_versions("1.2.0", "1.10.0")
    -1
    >>> sort_versions(["2.0.0", "1.0.0", "1.5.0"])
    ['1.0.0', '1.5.0', '2.0.0']
"""

from __future__ import annotations

from collections.abc import Iterable

from packaging.version import InvalidVersion, Version

#: Value returned by :func:`compare_versions` when the versions are equal under
#: PEP 440 ordering (e.g. ``1.0`` and ``1.0.0``).
EQUAL = 0
#: ``left`` sorts before (is older than) ``right``.
OLDER = -1
#: ``left`` sorts after (is newer than) ``right``.
NEWER = 1


class InvalidPluginVersionError(ValueError):
    """Raised when a value is not a valid PEP 440 version."""


def parse_version(value: str | Version) -> Version:
    """Parse ``value`` into a :class:`packaging.version.Version`.

    Strings are stripped before parsing. Passing an already-parsed
    :class:`~packaging.version.Version` is a no-op, which makes the helper safe
    to reuse in code that already works with ``Version`` objects.

    Raises:
        InvalidPluginVersionError: If ``value`` is not a valid PEP 440 version
            (including empty strings and non-string values).
    """
    if isinstance(value, Version):
        return value
    if not isinstance(value, str):
        raise InvalidPluginVersionError(f"Invalid plugin version: {value!r}")

    text = value.strip()
    if not text:
        raise InvalidPluginVersionError("Invalid plugin version: version is empty")

    try:
        return Version(text)
    except InvalidVersion as exc:
        raise InvalidPluginVersionError(f"Invalid plugin version: {value!r}") from exc


def is_valid_version(value: object) -> bool:
    """Return ``True`` when ``value`` is a valid PEP 440 version string."""
    if isinstance(value, Version):
        return True
    try:
        parse_version(value)  # type: ignore[arg-type]
    except InvalidPluginVersionError:
        return False
    return True


def normalize_version(value: str | Version) -> str:
    """Return the canonical string form of a PEP 440 version.

    ``"1.0.0"`` stays ``"1.0.0"``; whitespace is removed and equivalent
    spellings are normalized by ``packaging``. This keeps hashing, diffing, and
    display stable without changing ordering.
    """
    return str(parse_version(value))


def compare_versions(left: str | Version, right: str | Version) -> int:
    """Compare two PEP 440 versions.

    Returns:
        :data:`OLDER` (``-1``) when ``left < right``, :data:`EQUAL` (``0``)
        when they compare equal, and :data:`NEWER` (``1``) when
        ``left > right``.

    Raises:
        InvalidPluginVersionError: If either value is not a valid version.
    """
    left_version = parse_version(left)
    right_version = parse_version(right)
    return (left_version > right_version) - (left_version < right_version)


def is_newer(candidate: str | Version, current: str | Version) -> bool:
    """Return ``True`` when ``candidate`` is strictly newer than ``current``."""
    return compare_versions(candidate, current) == NEWER


def sort_versions(values: Iterable[str | Version], *, reverse: bool = False) -> list[str]:
    """Sort versions in ascending PEP 440 order (oldest first).

    The original values are returned (as strings) so callers can preserve the
    representation they were given. Sorting is stable: versions that compare
    equal (``1.0`` and ``1.0.0``) keep their input order.

    Raises:
        InvalidPluginVersionError: If any value is not a valid version.
    """
    parsed = [(parse_version(value), _original(value)) for value in values]
    parsed.sort(key=lambda item: item[0], reverse=reverse)
    return [original for _, original in parsed]


def _original(value: str | Version) -> str:
    """Return the caller's original string representation for ``value``."""
    return value if isinstance(value, str) else str(value)


def latest_version(values: Iterable[str | Version]) -> str | None:
    """Return the newest version in ``values``, or ``None`` for an empty input.

    Ties are resolved by input order (the first of the equal versions wins).
    """
    parsed = [(parse_version(value), _original(value)) for value in values]
    if not parsed:
        return None
    newest = parsed[0]
    for candidate in parsed[1:]:
        if candidate[0] > newest[0]:
            newest = candidate
    return newest[1]


__all__ = [
    "EQUAL",
    "NEWER",
    "OLDER",
    "InvalidPluginVersionError",
    "compare_versions",
    "is_newer",
    "is_valid_version",
    "latest_version",
    "normalize_version",
    "parse_version",
    "sort_versions",
]
