"""Trusted publisher key management (#386).

Operators configure the publishers they trust through ``PLUGIN_TRUSTED_KEYS``,
a JSON object mapping a publisher id to an Ed25519 public key. Supporting
multiple key encodings makes the setting easy to get wrong, and a silently
ignored key is a security problem: an operator who pastes a malformed key would
believe signatures are verified while every artifact is simply marked invalid.

This module therefore owns one strict loader:

* publisher ids are canonical (lowercase, ``[a-z0-9._-]``, max 64 characters);
* every key must decode to a real 32-byte Ed25519 public key, supplied either
  as base64 (raw key), as a PEM SubjectPublicKeyInfo, or as raw bytes;
* invalid configuration raises :class:`PluginTrustConfigError` naming the
  publisher and the problem, and **never** echoing key material;
* the read-only surface (:func:`trusted_publisher_ids`) exposes publisher ids
  only, so an operator can audit what is configured without any secret leaking.

The loader is imported by the application factory so bad key material is
rejected at startup (see :func:`validate_trust_config`).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

#: Accepted plugin trust policies (mirrored by ``app.services.plugin_signatures``).
VERIFY_IF_PRESENT = "if-present"
VERIFY_REQUIRED = "required"
TRUST_POLICIES = (VERIFY_IF_PRESENT, VERIFY_REQUIRED)

#: Canonical publisher id: lowercase, starts alphanumeric, ``[a-z0-9._-]``.
PUBLISHER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: Ed25519 raw public keys are exactly 32 bytes.
ED25519_KEY_BYTES = 32


class PluginTrustConfigError(ValueError):
    """Raised when ``PLUGIN_TRUSTED_KEYS`` / ``PLUGIN_TRUST_POLICY`` is invalid."""


def validate_publisher_id(publisher: Any) -> str:
    """Return ``publisher`` when it is a canonical id, else raise.

    Args:
        publisher: Configured publisher identifier.

    Raises:
        PluginTrustConfigError: If the id is not a lowercase ``[a-z0-9._-]``
            string of at most 64 characters.
    """
    if not isinstance(publisher, str) or not PUBLISHER_ID_RE.match(publisher):
        raise PluginTrustConfigError(
            "publisher ids must be lowercase [a-z0-9._-], start with a letter or "
            "digit, and be at most 64 characters"
        )
    return publisher


def decode_ed25519_public_key(value: str | bytes) -> bytes:
    """Decode a configured Ed25519 public key to its 32 raw bytes.

    Accepts base64-encoded raw keys, PEM ``SubjectPublicKeyInfo`` text, and raw
    ``bytes``. Error messages describe the encoding problem only; they never
    include the supplied value.

    Raises:
        PluginTrustConfigError: If the value is not a valid Ed25519 public key.
    """
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise PluginTrustConfigError("the key is empty")
        if text.startswith("-----BEGIN"):
            try:
                loaded = serialization.load_pem_public_key(text.encode("utf-8"))
            except (TypeError, ValueError) as exc:
                raise PluginTrustConfigError("the PEM value is not a valid public key") from exc
            if not isinstance(loaded, Ed25519PublicKey):
                raise PluginTrustConfigError("the PEM value is not an Ed25519 public key")
            raw = loaded.public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        else:
            try:
                raw = base64.b64decode(text, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise PluginTrustConfigError(
                    "the key is not valid base64 or a PEM public key"
                ) from exc
    else:
        raise PluginTrustConfigError("the key must be a base64/PEM string or raw bytes")

    if len(raw) != ED25519_KEY_BYTES:
        raise PluginTrustConfigError(f"an Ed25519 public key must be {ED25519_KEY_BYTES} raw bytes")
    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:  # pragma: no cover - length is checked above
        raise PluginTrustConfigError("the value is not a valid Ed25519 public key") from exc
    return raw


def load_trusted_publishers(
    raw: Mapping[str, str | bytes] | str | None,
) -> dict[str, bytes]:
    """Validate ``raw`` and return ``{publisher: raw_public_key_bytes}``.

    ``raw`` may be the parsed mapping held in ``PLUGIN_TRUSTED_KEYS`` or the
    original JSON string (which is what an operator sets), so the loader is
    usable from configuration, the CLI, and tests alike.

    Raises:
        PluginTrustConfigError: For malformed JSON, a non-object value, an
            invalid publisher id, or invalid key material.
    """
    if raw is None or raw == "":
        return {}

    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PluginTrustConfigError(
                "PLUGIN_TRUSTED_KEYS must be a JSON object of publisher keys"
            ) from exc
        raw = parsed

    if not isinstance(raw, Mapping):
        raise PluginTrustConfigError("PLUGIN_TRUSTED_KEYS must be a JSON object of publisher keys")

    publishers: dict[str, bytes] = {}
    for publisher, value in raw.items():
        try:
            publisher_id = validate_publisher_id(publisher)
            publishers[publisher_id] = decode_ed25519_public_key(value)
        except PluginTrustConfigError as exc:
            raise PluginTrustConfigError(
                f"trusted publisher {publisher!r} is invalid: {exc}"
            ) from exc
    return publishers


def validate_trust_policy(policy: Any) -> str:
    """Return ``policy`` when it is one of :data:`TRUST_POLICIES`.

    Raises:
        PluginTrustConfigError: For any other value.
    """
    if policy not in TRUST_POLICIES:
        allowed = ", ".join(TRUST_POLICIES)
        raise PluginTrustConfigError(
            f"PLUGIN_TRUST_POLICY must be one of: {allowed} (got {policy!r})"
        )
    return policy


def trusted_publisher_ids(
    raw: Mapping[str, str | bytes] | str | None,
) -> list[str]:
    """Return the configured publisher ids, sorted, without any key material.

    This is the only publisher information the API/CLI expose: the configured
    keys themselves (and even their fingerprints) never leave the process.
    """
    return sorted(load_trusted_publishers(raw))


def validate_trust_config(config: Mapping[str, Any]) -> dict[str, bytes]:
    """Validate the trust settings of an application config mapping.

    Called by the application factory so invalid key material fails at startup
    with a clear message instead of silently weakening verification.

    Raises:
        PluginTrustConfigError: If the keys or the policy are invalid.
    """
    try:
        publishers = load_trusted_publishers(config.get("PLUGIN_TRUSTED_KEYS"))
    except PluginTrustConfigError as exc:
        raise PluginTrustConfigError(f"invalid PLUGIN_TRUSTED_KEYS: {exc}") from exc

    try:
        validate_trust_policy(config.get("PLUGIN_TRUST_POLICY", VERIFY_IF_PRESENT))
    except PluginTrustConfigError as exc:
        raise PluginTrustConfigError(f"invalid PLUGIN_TRUST_POLICY: {exc}") from exc

    return publishers


__all__ = [
    "ED25519_KEY_BYTES",
    "PUBLISHER_ID_RE",
    "TRUST_POLICIES",
    "VERIFY_IF_PRESENT",
    "VERIFY_REQUIRED",
    "PluginTrustConfigError",
    "decode_ed25519_public_key",
    "load_trusted_publishers",
    "trusted_publisher_ids",
    "validate_publisher_id",
    "validate_trust_config",
    "validate_trust_policy",
]
