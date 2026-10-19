"""Deterministic plugin lockfile serialization (#398).

A plugin lockfile records the exact versions, sources, and artifact hashes a
workspace resolved so an install can be reproduced. That only helps if the file
is stable: resolving the same set of plugins twice must produce byte-identical
output, otherwise every run creates a spurious diff.

This module therefore defines one canonical serialization:

* entries are sorted by plugin id (the order they were resolved in is ignored);
* versions are normalized to their canonical PEP 440 spelling;
* sha256 integrity hashes are normalized to ``sha256:<lowercase hex>``;
* dependency requirement strings are sorted;
* JSON object keys are sorted and the document ends with exactly one ``\\n``;
* volatile fields (such as a per-resolution ``resolved_at`` timestamp) are not
  written, because they would defeat reproducibility.

Callers can pass :class:`LockfileEntry` objects or mappings/objects with the
matching fields, so the serializer works with the persisted lock model and with
plain dictionaries from a resolver::

    dumps_lockfile([
        {"plugin_id": "b", "version": "1.0", "source": "catalog", "integrity": "..."},
    ])

Use :func:`write_lockfile` to write the canonical bytes atomically.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

#: Format version written at the top of every lockfile.
LOCKFILE_VERSION = 1

#: Accepted sha256 spellings: ``sha256:<hex>``, ``sha256-<hex>``, and bare hex.
_SHA256_RE = re.compile(r"^(?:sha256[:=-])?([0-9a-fA-F]{64})$", re.IGNORECASE)

#: Mapping field aliases accepted for the integrity hash.
_INTEGRITY_ALIASES = ("integrity", "integrity_hash", "artifact_hash", "hash")


class LockfileError(ValueError):
    """Raised when lockfile entries cannot be serialized deterministically."""


@dataclass(frozen=True)
class LockfileEntry:
    """One resolved plugin entry in a lockfile.

    ``resolved_at`` is intentionally absent: a timestamp changes between runs
    and would make the lockfile non-deterministic. Callers that persist it in a
    database row should keep it out of the serialized output.
    """

    plugin_id: str
    version: str
    source: str
    integrity: str
    dependencies: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical mapping written to the lockfile."""
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "source": self.source,
            "integrity": self.integrity,
            "dependencies": list(self.dependencies),
        }


def normalize_integrity(value: str) -> str:
    """Normalize a sha256 integrity hash to ``sha256:<lowercase hex>``.

    Accepts ``sha256:`` / ``sha256-`` prefixes and bare uppercase hex so hashes
    copied from a catalog, a checksum file, or a previous lockfile all converge
    on the same output.

    Raises:
        LockfileError: If the value is not a 64-character sha256 hex digest.
    """
    if not isinstance(value, str):
        raise LockfileError(f"Invalid integrity hash: {value!r}")

    text = value.strip()
    match = _SHA256_RE.match(text)
    if match is None:
        algorithm = text.split(":", 1)[0].lower() if ":" in text else ""
        if algorithm and algorithm != "sha256":
            raise LockfileError(f"Unsupported integrity algorithm: {algorithm}")
        raise LockfileError(f"Invalid sha256 integrity hash: {value!r}")
    return f"sha256:{match.group(1).lower()}"


def _normalize_version(value: str) -> str:
    """Return the canonical PEP 440 spelling of a plugin version."""
    if not isinstance(value, str) or not value.strip():
        raise LockfileError(f"Invalid plugin version: {value!r}")
    try:
        return str(Version(value.strip()))
    except InvalidVersion as exc:
        raise LockfileError(f"Invalid plugin version: {value!r}") from exc


def _required_text(entry: Mapping[str, Any] | object, field_name: str) -> str:
    """Return a required, non-empty string field from a mapping or object."""
    if isinstance(entry, Mapping):
        value = entry.get(field_name)
    else:
        value = getattr(entry, field_name, None)
    if not isinstance(value, str) or not value.strip():
        raise LockfileError(f"Invalid lockfile entry: {field_name} is required")
    return value.strip()


def _entry_integrity(entry: Mapping[str, Any] | object) -> str:
    """Return the first integrity alias present on a mapping or object."""
    for alias in _INTEGRITY_ALIASES:
        value = entry.get(alias) if isinstance(entry, Mapping) else getattr(entry, alias, None)
        if value is not None:
            return normalize_integrity(value)
    raise LockfileError("Invalid lockfile entry: integrity is required")


def _entry_dependencies(entry: Mapping[str, Any] | object) -> tuple[str, ...]:
    """Return the sorted dependency requirement strings for an entry."""
    if isinstance(entry, Mapping):
        raw = entry.get("dependencies", ())
    else:
        raw = getattr(entry, "dependencies", ())
    if raw is None:
        return ()
    if isinstance(raw, str):
        raise LockfileError("Invalid lockfile entry: dependencies must be a list")
    try:
        items = list(raw)
    except TypeError as exc:
        raise LockfileError("Invalid lockfile entry: dependencies must be a list") from exc
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise LockfileError("Invalid lockfile entry: dependencies must be non-empty strings")
    return tuple(sorted(item.strip() for item in items))


def _coerce_entry(entry: LockfileEntry | Mapping[str, Any] | object) -> LockfileEntry:
    """Normalize an entry object/mapping into a :class:`LockfileEntry`."""
    if isinstance(entry, LockfileEntry):
        plugin_id = _required_text(entry, "plugin_id")
        version = _normalize_version(entry.version)
        source = _required_text(entry, "source")
        integrity = normalize_integrity(entry.integrity)
        dependencies = _entry_dependencies(entry)
    else:
        plugin_id = _required_text(entry, "plugin_id")
        version = _normalize_version(_required_text(entry, "version"))
        source = _required_text(entry, "source")
        integrity = _entry_integrity(entry)
        dependencies = _entry_dependencies(entry)

    return LockfileEntry(
        plugin_id=plugin_id,
        version=version,
        source=source,
        integrity=integrity,
        dependencies=dependencies,
    )


def build_lockfile(
    entries: Iterable[LockfileEntry | Mapping[str, Any] | object],
) -> dict[str, Any]:
    """Build the canonical lockfile document from ``entries``.

    Entries are normalized, checked for duplicate plugin ids, and sorted by
    plugin id. The returned mapping is plain JSON-serializable data (no model
    objects or timestamps).

    Raises:
        LockfileError: For duplicate ids or invalid entry fields.
    """
    normalized = [_coerce_entry(entry) for entry in entries]
    normalized.sort(key=lambda item: item.plugin_id)

    seen: set[str] = set()
    for entry in normalized:
        if entry.plugin_id in seen:
            raise LockfileError(f"Duplicate lockfile entry for plugin: {entry.plugin_id}")
        seen.add(entry.plugin_id)

    return {
        "lockfile_version": LOCKFILE_VERSION,
        "plugins": [entry.to_dict() for entry in normalized],
    }


def dumps_lockfile(entries: Iterable[LockfileEntry | Mapping[str, Any] | object]) -> str:
    """Serialize ``entries`` to the canonical, byte-stable lockfile string.

    The output uses sorted keys, two-space indentation, UTF-8 text, and a single
    trailing newline. Sorting and normalization happen before serialization, so
    the same entries always produce the same bytes regardless of input order.
    """
    document = build_lockfile(entries)
    return (
        json.dumps(
            document,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )


def write_lockfile(
    path: str | Path,
    entries: Iterable[LockfileEntry | Mapping[str, Any] | object],
) -> Path:
    """Write the canonical lockfile to ``path`` atomically.

    The file is written to a temporary file in the destination directory and
    moved into place with :func:`os.replace`, so readers never observe a
    partially written lockfile. Newlines are forced to ``\\n`` on every
    platform to avoid line-ending churn in version control.

    Returns:
        The destination path.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    contents = dumps_lockfile(entries)

    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(contents)
        os.replace(temporary, destination)
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    return destination


__all__ = [
    "LOCKFILE_VERSION",
    "LockfileEntry",
    "LockfileError",
    "build_lockfile",
    "dumps_lockfile",
    "normalize_integrity",
    "write_lockfile",
]
