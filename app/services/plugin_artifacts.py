"""Plugin artifact integrity verification (#384).

A marketplace artifact is only trustworthy if the bytes downloaded match the
sha256 digest recorded in the catalog. This module provides the single
verification path used before an artifact is registered:

1. normalize the catalog digest (accepting ``sha256:`` / ``sha256-`` prefixes
   and bare hex);
2. hash the artifact (streaming for files, never loading a whole package into
   memory);
3. compare the two in constant time with :func:`hmac.compare_digest`;
4. on mismatch, record a bounded audit event (when a workspace context is
   supplied) and raise :class:`ArtifactIntegrityError`.

:func:`verify_and_register_artifact` makes the ordering explicit: the
registration callback is invoked **only after** verification succeeds, so a
tampered artifact can never reach the registry.

This module does not download anything and does not execute plugin code.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from app.services.plugins import PluginError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.user import User

logger = logging.getLogger(__name__)

#: Read size used when hashing a file or stream.
CHUNK_SIZE = 1024 * 1024

#: Accepted sha256 spellings: ``sha256:<hex>``, ``sha256-<hex>``, and bare hex.
_SHA256_RE = re.compile(r"^(?:sha256[:=-])?([0-9a-fA-F]{64})$", re.IGNORECASE)

#: A filesystem path, in-memory bytes, or a binary file-like object.
ArtifactSource = str | os.PathLike[str] | bytes | bytearray | memoryview | BinaryIO


class ArtifactError(PluginError):
    """A plugin artifact could not be read or validated."""


class ArtifactIntegrityError(ArtifactError):
    """An artifact's sha256 digest does not match the catalog value."""

    def __init__(self, expected: str, actual: str, *, plugin_id: str | None = None) -> None:
        label = f" for plugin {plugin_id}" if plugin_id else ""
        super().__init__(f"Artifact sha256 mismatch{label}: expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual
        self.plugin_id = plugin_id


def normalize_sha256(value: str) -> str:
    """Normalize a sha256 digest to lowercase 64-character hex.

    Raises:
        ArtifactError: If ``value`` is not a valid sha256 hex digest.
    """
    if not isinstance(value, str):
        raise ArtifactError(f"Invalid sha256 digest: {value!r}")

    text = value.strip()
    match = _SHA256_RE.match(text)
    if match is None:
        algorithm = text.split(":", 1)[0].lower() if ":" in text else ""
        if algorithm and algorithm != "sha256":
            raise ArtifactError(f"Unsupported artifact integrity algorithm: {algorithm}")
        raise ArtifactError(f"Invalid sha256 digest: {value!r}")
    return match.group(1).lower()


def _update_from_stream(digest: Any, stream: BinaryIO, chunk_size: int) -> None:
    """Feed a binary stream into ``digest`` without reading it all at once."""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ArtifactError("Artifact stream must be opened in binary mode")
        digest.update(chunk)


def compute_sha256(source: ArtifactSource, *, chunk_size: int = CHUNK_SIZE) -> str:
    """Return the lowercase hex sha256 digest of ``source``.

    ``source`` may be a path (``str``/``os.PathLike``), in-memory bytes, or a
    readable binary stream. Files are streamed in ``chunk_size`` blocks.

    Raises:
        ArtifactError: If the artifact cannot be read or ``chunk_size`` is not
            positive.
    """
    if not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ArtifactError("Artifact chunk size must be a positive integer")

    digest = hashlib.sha256()

    if isinstance(source, (bytes, bytearray, memoryview)):
        digest.update(source)
        return digest.hexdigest()

    if hasattr(source, "read"):
        try:
            _update_from_stream(digest, source, chunk_size)  # type: ignore[arg-type]
        except OSError as exc:
            raise ArtifactError(f"Cannot read artifact stream: {exc}") from exc
        return digest.hexdigest()

    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        try:
            with path.open("rb") as handle:
                _update_from_stream(digest, handle, chunk_size)
        except OSError as exc:
            raise ArtifactError(f"Cannot read artifact file {path}: {exc}") from exc
        return digest.hexdigest()

    raise ArtifactError(f"Unsupported artifact source: {type(source).__name__}")


def _record_mismatch(
    *,
    plugin_id: str | None,
    workspace_id: int | None,
    actor: User | None,
) -> None:
    """Best-effort bounded audit event for a refused artifact.

    The integrity error is the security-relevant outcome; a failure to write the
    audit row must not mask it, so recording failures are logged and swallowed.
    Outside an application context (for example a unit test or CLI helper with
    no workspace) there is nowhere to persist the event and recording is a
    no-op.
    """
    if workspace_id is None:
        return

    try:
        from flask import has_app_context

        if not has_app_context():
            return

        from app.services.plugin_audit import record_plugin_audit

        record_plugin_audit(
            workspace_id,
            "denied",
            actor=actor,
            plugin_id=plugin_id,
            outcome="denied",
            reason="artifact integrity mismatch",
        )
    except Exception:  # pragma: no cover - audit is best effort
        logger.warning("Could not record artifact integrity audit event", exc_info=True)


def verify_artifact_integrity(
    source: ArtifactSource,
    expected_sha256: str,
    *,
    plugin_id: str | None = None,
    workspace_id: int | None = None,
    actor: User | None = None,
) -> str:
    """Verify ``source`` against ``expected_sha256`` in constant time.

    Returns the artifact's computed (normalized) digest on success. On mismatch
    the catalog value is recorded as a plugin audit denial for ``workspace_id``
    (when provided) and :class:`ArtifactIntegrityError` is raised, so the caller
    must not register the artifact.

    Args:
        source: Artifact path, bytes, or binary stream.
        expected_sha256: Catalog digest (``sha256:<hex>``, ``sha256-<hex>``, or
            bare hex).
        plugin_id: Plugin being installed, for the error/audit facts.
        workspace_id: Workspace performing the install, for the audit event.
        actor: User performing the install, for the audit event.

    Raises:
        ArtifactIntegrityError: If the hashes do not match.
        ArtifactError: If the artifact or expected digest cannot be read/parsed.
    """
    expected = normalize_sha256(expected_sha256)
    actual = compute_sha256(source)

    if not hmac.compare_digest(actual, expected):
        _record_mismatch(plugin_id=plugin_id, workspace_id=workspace_id, actor=actor)
        raise ArtifactIntegrityError(expected, actual, plugin_id=plugin_id)

    return actual


def verify_and_register_artifact[T](
    source: ArtifactSource,
    expected_sha256: str,
    register: Callable[[str], T],
    *,
    plugin_id: str | None = None,
    workspace_id: int | None = None,
    actor: User | None = None,
) -> T:
    """Verify an artifact and only then invoke ``register``.

    ``register`` receives the verified digest and is never called for a hash
    mismatch, which keeps a tampered artifact out of the registry.

    Returns:
        The value returned by ``register``.
    """
    digest = verify_artifact_integrity(
        source,
        expected_sha256,
        plugin_id=plugin_id,
        workspace_id=workspace_id,
        actor=actor,
    )
    return register(digest)


__all__ = [
    "CHUNK_SIZE",
    "ArtifactError",
    "ArtifactIntegrityError",
    "ArtifactSource",
    "compute_sha256",
    "normalize_sha256",
    "verify_and_register_artifact",
    "verify_artifact_integrity",
]
