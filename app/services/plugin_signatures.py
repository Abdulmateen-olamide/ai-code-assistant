"""Detached publisher signature verification (#385).

A publisher signs the canonical JSON body of a plugin manifest with an Ed25519
private key. The signature may travel inside the manifest (``signature``) or
detached from it (a ``.sig`` file or a catalog field) — both forms sign exactly
the same bytes, so a detached signature can never be used to authenticate
metadata the embedded form would reject.

The signed payload is :func:`canonical_manifest_body`: the manifest as compact,
key-sorted JSON with ``signature`` and the runtime-managed ``trust_state`` /
``trust_publisher`` fields removed. Those fields are produced by the host, so
including them would make a signature impossible to verify after a round-trip.

Verification returns a :class:`PublisherTrust` fact and never leaks key
material. Two entry points cover the two call sites:

* :func:`evaluate_signature` — the historical manifest behaviour, which reports
  ``Unverified``/``Invalid`` instead of raising;
* :func:`enforce_publisher_trust` — the strict path used for installations,
  where a *forged* signature always fails and a *missing* signature fails when
  ``PLUGIN_TRUST_POLICY=required`` (``if-present`` only tolerates absence).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.services.plugin_trust import (
    VERIFY_IF_PRESENT,
    VERIFY_REQUIRED,
    PluginTrustConfigError,
    decode_ed25519_public_key,
    validate_trust_policy,
)

__all__ = [
    "INVALID",
    "TRUSTED",
    "UNVERIFIED",
    "VERIFY_IF_PRESENT",
    "VERIFY_REQUIRED",
    "InvalidPublisherSignatureError",
    "MissingPublisherSignatureError",
    "PublisherSignatureError",
    "PublisherTrust",
    "canonical_manifest_body",
    "enforce_publisher_trust",
    "evaluate_signature",
    "validate_signature_metadata",
]

TRUSTED = "Trusted"
UNVERIFIED = "Unverified"
INVALID = "Invalid"

#: Fields the host owns and that are never part of the signed bytes.
_UNSIGNED_FIELDS = frozenset({"signature", "trust_state", "trust_publisher"})


class PublisherSignatureError(ValueError):
    """A publisher signature was required but could not be verified."""


class MissingPublisherSignatureError(PublisherSignatureError):
    """No signature was supplied while the trust policy requires one."""


class InvalidPublisherSignatureError(PublisherSignatureError):
    """A supplied signature is malformed, forged, or from an untrusted publisher."""


@dataclass(frozen=True)
class PublisherTrust:
    """The outcome of checking one publisher signature."""

    state: str
    publisher: str | None
    signature_present: bool

    @property
    def trusted(self) -> bool:
        """Whether the signature verified against a configured publisher key."""
        return self.state == TRUSTED

    def to_dict(self) -> dict[str, Any]:
        """Return the trust fields as stored on a plugin record."""
        return {
            "trust_state": self.state,
            "trust_publisher": self.publisher,
        }


def validate_signature_metadata(signature: Any) -> str | None:
    """Validate the shape of optional signature metadata.

    Returns a human-readable error string, or ``None`` when the metadata is a
    well-formed Ed25519 signature object.
    """
    if signature is None:
        return None
    if not isinstance(signature, Mapping):
        return "Invalid signature: must be an object"
    if set(signature) != {"publisher", "algorithm", "value"}:
        return "Invalid signature: expected publisher, algorithm, and value"
    if not all(isinstance(signature.get(key), str) and signature[key] for key in signature):
        return "Invalid signature: publisher, algorithm, and value must be non-empty strings"
    if signature["algorithm"] != "ed25519":
        return "Invalid signature: algorithm must be ed25519"
    return None


def canonical_manifest_body(data: Mapping[str, Any]) -> bytes:
    """Return the stable JSON bytes a publisher signs for ``data``.

    ``signature`` and the runtime-managed trust fields are excluded so the same
    payload verifies whether the signature is embedded or detached.
    """
    if not isinstance(data, Mapping):
        raise TypeError("Plugin manifest must be a mapping")
    body = {key: value for key, value in data.items() if key not in _UNSIGNED_FIELDS}
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def evaluate_signature(
    manifest: Mapping[str, Any],
    signature: Any,
    trusted_publishers: Mapping[str, str | bytes] | None,
) -> PublisherTrust:
    """Check ``signature`` and report trust without raising.

    Returns ``Trusted`` only when the signature verifies against the key
    configured for its ``publisher``. A missing signature is ``Unverified``;
    malformed metadata, an unknown publisher, or a forged signature is
    ``Invalid``. The caller decides whether the resulting state is acceptable
    (see :func:`enforce_publisher_trust`).
    """
    if signature is None:
        return PublisherTrust(UNVERIFIED, None, False)

    metadata_error = validate_signature_metadata(signature)
    if metadata_error:
        publisher = signature.get("publisher") if isinstance(signature, Mapping) else None
        return PublisherTrust(INVALID, publisher if isinstance(publisher, str) else None, True)

    publisher = signature["publisher"]
    key_value = (trusted_publishers or {}).get(publisher)
    if key_value is None:
        return PublisherTrust(INVALID, publisher, True)

    try:
        public_key = Ed25519PublicKey.from_public_bytes(decode_ed25519_public_key(key_value))
        public_key.verify(
            base64.b64decode(signature["value"], validate=True),
            canonical_manifest_body(manifest),
        )
    except (PluginTrustConfigError, ValueError, TypeError, binascii.Error, InvalidSignature):
        return PublisherTrust(INVALID, publisher, True)
    return PublisherTrust(TRUSTED, publisher, True)


def enforce_publisher_trust(
    manifest: Mapping[str, Any],
    signature: Any,
    *,
    trusted_publishers: Mapping[str, str | bytes] | None,
    policy: str = VERIFY_IF_PRESENT,
) -> PublisherTrust:
    """Enforce the trust policy for an installation and return the trust fact.

    * A valid signature from a configured publisher is always accepted.
    * A **forged or untrusted** signature is always refused, even under
      ``if-present``: tolerating absence must not mean tolerating forgeries.
    * A **missing** signature is refused when ``policy`` is ``required`` and
      accepted (as ``Unverified``) when it is ``if-present``.

    Args:
        manifest: The manifest mapping as published by the catalog.
        signature: Detached signature metadata, or ``None``.
        trusted_publishers: Mapping of publisher id to configured key.
        policy: ``if-present`` or ``required``.

    Raises:
        PluginTrustConfigError: If ``policy`` is not a known policy.
        MissingPublisherSignatureError: If the policy requires a signature and
            none was supplied.
        InvalidPublisherSignatureError: If the signature is malformed, forged,
            or from a publisher without a configured key.
    """
    policy = validate_trust_policy(policy)
    trust = evaluate_signature(manifest, signature, trusted_publishers)

    if trust.trusted:
        return trust

    if trust.signature_present:
        publisher_label = f" from {trust.publisher!r}" if trust.publisher else ""
        raise InvalidPublisherSignatureError(
            f"Publisher signature verification failed: {trust.state} signature"
            f"{publisher_label}. The manifest was modified or the publisher key is not trusted."
        )

    if policy == VERIFY_REQUIRED:
        raise MissingPublisherSignatureError(
            "A publisher signature is required by PLUGIN_TRUST_POLICY=required, "
            "but the catalog entry is unsigned."
        )

    return trust
