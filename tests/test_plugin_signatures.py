"""Tests for detached publisher signatures (#385).

A valid signature verifies; a forged signature always fails (even in lenient
mode); a missing signature is tolerated only when the trust policy allows it.
"""

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services.plugin_signatures import (
    INVALID,
    TRUSTED,
    UNVERIFIED,
    InvalidPublisherSignatureError,
    MissingPublisherSignatureError,
    canonical_manifest_body,
    enforce_publisher_trust,
    evaluate_signature,
    validate_signature_metadata,
)
from app.services.plugin_trust import PluginTrustConfigError
from app.services.plugins import ManifestValidationError, PluginManifest

PUBLISHER = "acme-labs"


def _manifest():
    return {
        "id": "signed-plugin",
        "name": "Signed Plugin",
        "version": "0.1.0",
        "description": "A signed test plugin",
        "author": "Tester",
        "entry_point": "plugins.test:TestPlugin",
        "capabilities": ["PROJECT_READ"],
    }


def _publisher():
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private_key, base64.b64encode(raw).decode()


def _sign(private_key, manifest, publisher=PUBLISHER):
    return {
        "publisher": publisher,
        "algorithm": "ed25519",
        "value": base64.b64encode(private_key.sign(canonical_manifest_body(manifest))).decode(),
    }


class TestCanonicalBody:
    def test_is_stable_regardless_of_key_order(self):
        manifest = _manifest()
        reordered = dict(reversed(list(manifest.items())))
        assert canonical_manifest_body(manifest) == canonical_manifest_body(reordered)

    def test_excludes_signature_and_runtime_trust_fields(self):
        manifest = _manifest()
        decorated = {
            **manifest,
            "signature": {"publisher": PUBLISHER, "algorithm": "ed25519", "value": "x"},
            "trust_state": TRUSTED,
            "trust_publisher": PUBLISHER,
        }
        assert canonical_manifest_body(decorated) == canonical_manifest_body(manifest)

    def test_rejects_non_mapping(self):
        with pytest.raises(TypeError):
            canonical_manifest_body(["not", "a", "manifest"])


class TestMetadataValidation:
    def test_accepts_well_formed_signature(self):
        private_key, _ = _publisher()
        assert validate_signature_metadata(_sign(private_key, _manifest())) is None

    @pytest.mark.parametrize(
        "signature",
        [
            ["not", "an", "object"],
            {"publisher": PUBLISHER},
            {"publisher": PUBLISHER, "algorithm": "rsa", "value": "x"},
            {"publisher": PUBLISHER, "algorithm": "ed25519", "value": ""},
            {"publisher": "", "algorithm": "ed25519", "value": "x"},
            {
                "publisher": PUBLISHER,
                "algorithm": "ed25519",
                "value": "x",
                "extra": "nope",
            },
        ],
    )
    def test_rejects_malformed_metadata(self, signature):
        assert validate_signature_metadata(signature) is not None


class TestEvaluateSignature:
    def test_valid_detached_signature_is_trusted(self):
        private_key, public_key = _publisher()
        manifest = _manifest()

        trust = evaluate_signature(manifest, _sign(private_key, manifest), {PUBLISHER: public_key})

        assert trust.state == TRUSTED
        assert trust.publisher == PUBLISHER
        assert trust.signature_present is True
        assert trust.to_dict() == {"trust_state": TRUSTED, "trust_publisher": PUBLISHER}

    def test_forged_signature_is_invalid(self):
        private_key, public_key = _publisher()
        manifest = _manifest()
        signature = _sign(private_key, manifest)
        manifest["name"] = "Tampered Plugin"

        trust = evaluate_signature(manifest, signature, {PUBLISHER: public_key})

        assert trust.state == INVALID
        assert trust.publisher == PUBLISHER

    def test_unknown_publisher_is_invalid(self):
        private_key, _ = _publisher()
        manifest = _manifest()

        trust = evaluate_signature(manifest, _sign(private_key, manifest), {})

        assert trust.state == INVALID
        assert trust.publisher == PUBLISHER

    def test_missing_signature_is_unverified(self):
        trust = evaluate_signature(_manifest(), None, {})
        assert trust.state == UNVERIFIED
        assert trust.publisher is None
        assert trust.signature_present is False

    def test_pem_key_is_accepted(self):
        private_key, _ = _publisher()
        pem = (
            private_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        manifest = _manifest()

        trust = evaluate_signature(manifest, _sign(private_key, manifest), {PUBLISHER: pem})

        assert trust.state == TRUSTED


class TestEnforcePublisherTrust:
    def test_valid_signature_passes_both_policies(self):
        private_key, public_key = _publisher()
        manifest = _manifest()
        signature = _sign(private_key, manifest)
        keys = {PUBLISHER: public_key}

        for policy in ("if-present", "required"):
            trust = enforce_publisher_trust(
                manifest,
                signature,
                trusted_publishers=keys,
                policy=policy,
            )
            assert trust.state == TRUSTED

    def test_missing_signature_fails_under_required_policy(self):
        with pytest.raises(MissingPublisherSignatureError, match="unsigned"):
            enforce_publisher_trust(
                _manifest(),
                None,
                trusted_publishers={},
                policy="required",
            )

    def test_missing_signature_is_allowed_under_if_present(self):
        trust = enforce_publisher_trust(
            _manifest(),
            None,
            trusted_publishers={},
            policy="if-present",
        )
        assert trust.state == UNVERIFIED

    def test_forged_signature_fails_even_if_present(self):
        private_key, public_key = _publisher()
        manifest = _manifest()
        signature = _sign(private_key, manifest)
        manifest["name"] = "Tampered Plugin"

        with pytest.raises(InvalidPublisherSignatureError, match="Invalid"):
            enforce_publisher_trust(
                manifest,
                signature,
                trusted_publishers={PUBLISHER: public_key},
                policy="if-present",
            )

    def test_untrusted_publisher_fails_even_if_present(self):
        private_key, _ = _publisher()
        manifest = _manifest()

        with pytest.raises(InvalidPublisherSignatureError, match=PUBLISHER):
            enforce_publisher_trust(
                manifest,
                _sign(private_key, manifest),
                trusted_publishers={},
                policy="if-present",
            )

    def test_unknown_policy_is_rejected(self):
        with pytest.raises(PluginTrustConfigError):
            enforce_publisher_trust(
                _manifest(),
                None,
                trusted_publishers={},
                policy="sometimes",
            )


class TestEmbeddedManifestSignatureRegression:
    """The shared verifier must keep the existing embedded-signature behaviour."""

    def test_embedded_signature_is_trusted_after_refactor(self):
        private_key, public_key = _publisher()
        manifest = _manifest()
        signed = {**manifest, "signature": _sign(private_key, manifest)}

        parsed = PluginManifest.from_dict(signed, trusted_publishers={PUBLISHER: public_key})

        assert parsed.trust_state == TRUSTED
        assert parsed.trust_publisher == PUBLISHER

    def test_embedded_forgery_is_invalid_in_lenient_mode(self):
        private_key, public_key = _publisher()
        manifest = _manifest()
        signed = {**manifest, "signature": _sign(private_key, manifest)}
        signed["name"] = "Tampered Plugin"

        parsed = PluginManifest.from_dict(signed, trusted_publishers={PUBLISHER: public_key})

        assert parsed.trust_state == INVALID

    def test_strict_policy_still_rejects_unsigned_manifest(self):
        with pytest.raises(ManifestValidationError, match="Unverified"):
            PluginManifest.from_dict(_manifest(), trust_policy="required")
