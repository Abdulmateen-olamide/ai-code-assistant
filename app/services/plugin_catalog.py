"""Validated plugin catalog entries used by remote installation (#389).

The remote-install endpoint never trusts a client-supplied manifest: it resolves
``(plugin_id, version)`` against the operator-controlled catalog and installs
only what that catalog entry describes. This module is the smallest catalog
surface that makes that contract testable while the full marketplace catalog
issues (#373-#381) land:

``PLUGIN_CATALOG_FILE`` points at a JSON document::

    {
      "version": 1,
      "plugins": [
        {
          "id": "hello-world",
          "version": "0.1.0",
          "source": "https://plugins.example.com/hello-world-0.1.0.zip",
          "integrity": "sha256:...",
          "signature": {"publisher": "acme", "algorithm": "ed25519", "value": "..."},
          "manifest": { ...a full manifest.json object... }
        }
      ]
    }

Every entry is validated at load time: ids and versions are canonical, the
source is https (or an opaque local identifier), ``integrity`` is a sha256
digest, the signature shape is well formed, and the embedded manifest describes
the same plugin id and version. Loading performs **no network access** and the
install path executes no plugin code.

Nothing here is a secret: the catalog carries public metadata only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from app.services.plugin_lockfile import LockfileError, normalize_integrity
from app.services.plugin_signatures import validate_signature_metadata

#: Catalog document version understood by this loader.
CATALOG_VERSION = 1

#: Refuse absurdly large catalog files instead of reading them into memory.
MAX_CATALOG_BYTES = 1024 * 1024

#: Refuse catalogs with more entries than the installer can reasonably serve.
MAX_CATALOG_ENTRIES = 500

#: Longest accepted ``source`` reference.
MAX_SOURCE_LENGTH = 512

#: Manifest/plugin id format, matching ``plugins/plugin.schema.json``.
_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class CatalogError(ValueError):
    """The plugin catalog or one of its entries is invalid."""


class CatalogUnavailableError(CatalogError):
    """No catalog is configured, or the configured file cannot be read."""


class CatalogEntryNotFoundError(CatalogError):
    """The requested plugin id/version is not present in the catalog."""


@dataclass(frozen=True)
class CatalogEntry:
    """One validated catalog entry.

    ``manifest`` is a plain mapping so it can be validated with
    :meth:`~app.services.plugins.PluginManifest.from_dict` exactly like a
    manifest uploaded by a client; ``signature`` is the *detached* publisher
    signature for that manifest (it may be ``None``).
    """

    plugin_id: str
    version: str
    source: str
    integrity: str
    manifest: Mapping[str, Any]
    signature: Mapping[str, str] | None = None

    @property
    def publisher(self) -> str | None:
        """Return the detached-signature publisher, when present."""
        if self.signature is None:
            return None
        return self.signature.get("publisher")

    def to_dict(self) -> dict[str, Any]:
        """Return the entry as JSON-serializable data."""
        data: dict[str, Any] = {
            "id": self.plugin_id,
            "version": self.version,
            "source": self.source,
            "integrity": self.integrity,
            "manifest": dict(self.manifest),
        }
        if self.signature is not None:
            data["signature"] = dict(self.signature)
        return data


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogError(f"{label} must be an object")
    return value


def _parse_version(value: Any, label: str) -> Version:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{label} is required")
    try:
        return Version(value.strip())
    except InvalidVersion as exc:
        raise CatalogError(f"{label} is not a valid version: {value!r}") from exc


def _parse_source(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError("catalog entry source is required")
    source = value.strip()
    if len(source) > MAX_SOURCE_LENGTH:
        raise CatalogError(f"catalog entry source must be at most {MAX_SOURCE_LENGTH} characters")
    if "://" in source:
        scheme = source.split("://", 1)[0].lower()
        if scheme != "https":
            raise CatalogError("catalog entry source URLs must use https")
    return source


def _parse_manifest(entry_id: str, entry_version: Version, value: Any) -> Mapping[str, Any]:
    manifest = _require_mapping(value, "catalog entry manifest")
    manifest_id = manifest.get("id")
    if manifest_id != entry_id:
        raise CatalogError(
            f"catalog entry id {entry_id!r} does not match the manifest id {manifest_id!r}"
        )
    manifest_version = _parse_version(manifest.get("version"), "catalog entry manifest version")
    if manifest_version != entry_version:
        raise CatalogError(
            f"catalog entry version {entry_version} does not match the manifest "
            f"version {manifest_version}"
        )
    return dict(manifest)


def _parse_signature(value: Any) -> Mapping[str, str] | None:
    if value is None:
        return None
    error = validate_signature_metadata(value)
    if error:
        raise CatalogError(error)
    return {key: value[key] for key in ("publisher", "algorithm", "value")}


def parse_catalog_entry(value: Any) -> CatalogEntry:
    """Validate one catalog entry mapping and return it.

    Raises:
        CatalogError: If any required field is missing or malformed.
    """
    entry = _require_mapping(value, "catalog entry")

    entry_id = entry.get("id")
    if not isinstance(entry_id, str) or not _PLUGIN_ID_RE.match(entry_id):
        raise CatalogError(
            "catalog entry id must start with a lowercase letter and contain only "
            "lowercase letters, numbers, hyphens, and underscores"
        )

    version = _parse_version(entry.get("version"), "catalog entry version")
    source = _parse_source(entry.get("source"))

    try:
        integrity = normalize_integrity(entry.get("integrity"))
    except LockfileError as exc:
        raise CatalogError(f"catalog entry integrity is invalid: {exc}") from exc

    signature = _parse_signature(entry.get("signature"))
    manifest = _parse_manifest(entry_id, version, entry.get("manifest"))

    return CatalogEntry(
        plugin_id=entry_id,
        version=version.public,
        source=source,
        integrity=integrity,
        manifest=manifest,
        signature=signature,
    )


class PluginCatalog:
    """An immutable, indexed collection of validated :class:`CatalogEntry` rows."""

    def __init__(self, entries: list[CatalogEntry]):
        ordered = sorted(entries, key=lambda item: (item.plugin_id, Version(item.version)))
        self._entries = tuple(ordered)
        self._by_id: dict[str, list[CatalogEntry]] = {}
        for entry in ordered:
            self._by_id.setdefault(entry.plugin_id, []).append(entry)

    def __len__(self) -> int:
        """Number of catalog entries."""
        return len(self._entries)

    def list_entries(self) -> tuple[CatalogEntry, ...]:
        """Return every entry, sorted by plugin id and version."""
        return self._entries

    def get(self, plugin_id: str, version: str | None = None) -> CatalogEntry:
        """Return the entry for ``plugin_id`` (and ``version`` when given).

        Without a version the newest compatible entry for the plugin is
        returned, using PEP 440 ordering.

        Raises:
            CatalogEntryNotFoundError: If the id or version is not catalogued.
        """
        matches = self._by_id.get(plugin_id, [])
        if version is None:
            if not matches:
                raise CatalogEntryNotFoundError(
                    f"Plugin {plugin_id!r} is not in the plugin catalog."
                )
            return matches[-1]

        requested = _parse_version(version, "version")
        for entry in matches:
            if Version(entry.version) == requested:
                return entry
        raise CatalogEntryNotFoundError(
            f"Plugin {plugin_id!r} version {requested} is not in the plugin catalog."
        )


def parse_catalog(document: Any) -> PluginCatalog:
    """Validate a catalog document and return a :class:`PluginCatalog`.

    Raises:
        CatalogError: For a malformed document or invalid/duplicate entries.
    """
    data = _require_mapping(document, "plugin catalog")
    document_version = data.get("version", CATALOG_VERSION)
    if document_version != CATALOG_VERSION:
        raise CatalogError(
            f"unsupported plugin catalog version {document_version!r} "
            f"(expected {CATALOG_VERSION})"
        )

    raw_entries = data.get("plugins")
    if not isinstance(raw_entries, list):
        raise CatalogError("plugin catalog 'plugins' must be a list")
    if len(raw_entries) > MAX_CATALOG_ENTRIES:
        raise CatalogError(f"plugin catalog must contain at most {MAX_CATALOG_ENTRIES} entries")

    entries: list[CatalogEntry] = []
    seen: set[tuple[str, Version]] = set()
    for index, raw_entry in enumerate(raw_entries):
        try:
            entry = parse_catalog_entry(raw_entry)
        except CatalogError as exc:
            raise CatalogError(f"catalog entry {index}: {exc}") from exc

        key = (entry.plugin_id, Version(entry.version))
        if key in seen:
            raise CatalogError(
                f"duplicate catalog entry for plugin {entry.plugin_id!r} "
                f"version {entry.version!r}"
            )
        seen.add(key)
        entries.append(entry)

    return PluginCatalog(entries)


def load_catalog_file(path: str | Path) -> PluginCatalog:
    """Read and validate the catalog JSON at ``path``.

    Raises:
        CatalogUnavailableError: If the file is missing, unreadable, or too big.
        CatalogError: If the contents are not a valid catalog.
    """
    catalog_path = Path(path)
    try:
        size = catalog_path.stat().st_size
    except OSError as exc:
        raise CatalogUnavailableError(
            f"Plugin catalog file could not be read: {catalog_path}"
        ) from exc
    if size > MAX_CATALOG_BYTES:
        raise CatalogUnavailableError(
            f"Plugin catalog file exceeds {MAX_CATALOG_BYTES} bytes: {catalog_path}"
        )

    try:
        with catalog_path.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Plugin catalog is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise CatalogUnavailableError(
            f"Plugin catalog file could not be read: {catalog_path}"
        ) from exc

    return parse_catalog(document)


def load_plugin_catalog(config: Mapping[str, Any]) -> PluginCatalog:
    """Load the catalog configured for ``config``.

    ``PLUGIN_CATALOG_FILE`` is empty by default, which deliberately disables
    remote installation rather than silently reading an unrelated file.

    Raises:
        CatalogUnavailableError: If no catalog is configured or it cannot be read.
        CatalogError: If the configured catalog is invalid.
    """
    configured = config.get("PLUGIN_CATALOG_FILE")
    if not configured:
        raise CatalogUnavailableError(
            "Remote plugin installation is not configured (set PLUGIN_CATALOG_FILE)."
        )
    return load_catalog_file(configured)


__all__ = [
    "CATALOG_VERSION",
    "MAX_CATALOG_BYTES",
    "MAX_CATALOG_ENTRIES",
    "CatalogEntry",
    "CatalogEntryNotFoundError",
    "CatalogError",
    "CatalogUnavailableError",
    "PluginCatalog",
    "load_catalog_file",
    "load_plugin_catalog",
    "parse_catalog",
    "parse_catalog_entry",
]
