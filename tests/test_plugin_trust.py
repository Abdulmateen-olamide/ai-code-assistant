"""Tests for trusted publisher key management (#386).

The interesting properties are: valid key material in every supported encoding
is accepted, malformed configuration fails loudly (never silently untrusted),
and the read-only surfaces expose publisher ids only — never key bytes.
"""

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import TestingConfig
from app.services.plugin_trust import (
    PluginTrustConfigError,
    decode_ed25519_public_key,
    load_trusted_publishers,
    trusted_publisher_ids,
    validate_trust_config,
    validate_trust_policy,
)

PUBLISHER = "acme-labs"


def _keypair():
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private_key, raw


def _base64_key():
    _, raw = _keypair()
    return base64.b64encode(raw).decode()


class TestDecodeEd25519PublicKey:
    def test_accepts_base64_raw_key(self):
        _, raw = _keypair()
        assert decode_ed25519_public_key(base64.b64encode(raw).decode()) == raw

    def test_accepts_raw_bytes(self):
        _, raw = _keypair()
        assert decode_ed25519_public_key(raw) == raw

    def test_accepts_pem_subject_public_key_info(self):
        private_key, raw = _keypair()
        pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        assert decode_ed25519_public_key(pem.decode()) == raw

    @pytest.mark.parametrize("value", ["", "   ", "not-base64!!", "a" * 10])
    def test_rejects_malformed_keys(self, value):
        with pytest.raises(PluginTrustConfigError):
            decode_ed25519_public_key(value)

    def test_rejects_non_ed25519_pem(self):
        other = ec.generate_private_key(ec.SECP256R1())
        pem = other.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        with pytest.raises(PluginTrustConfigError, match="Ed25519"):
            decode_ed25519_public_key(pem.decode())

    def test_rejects_no_bytes(self):
        with pytest.raises(PluginTrustConfigError):
            decode_ed25519_public_key(123)

    def test_error_never_echoes_key_material(self):
        secret = base64.b64encode(b"x" * 31).decode()
        with pytest.raises(PluginTrustConfigError) as exc_info:
            decode_ed25519_public_key(secret)
        assert secret not in str(exc_info.value)


class TestLoadTrustedPublishers:
    def test_empty_configuration(self):
        assert load_trusted_publishers(None) == {}
        assert load_trusted_publishers({}) == {}
        assert load_trusted_publishers("") == {}

    def test_parses_json_string(self):
        key = _base64_key()
        loaded = load_trusted_publishers(json.dumps({PUBLISHER: key}))
        assert list(loaded) == [PUBLISHER]
        assert len(loaded[PUBLISHER]) == 32

    def test_parses_mapping(self):
        key = _base64_key()
        assert list(load_trusted_publishers({PUBLISHER: key})) == [PUBLISHER]

    def test_rejects_invalid_json(self):
        with pytest.raises(PluginTrustConfigError, match="JSON object"):
            load_trusted_publishers("{not json")

    def test_rejects_non_object_json(self):
        with pytest.raises(PluginTrustConfigError, match="JSON object"):
            load_trusted_publishers("[1, 2, 3]")

    @pytest.mark.parametrize("publisher", ["Acme", "acme labs", "", "-acme", "a" * 65])
    def test_rejects_non_canonical_publisher_ids(self, publisher):
        with pytest.raises(PluginTrustConfigError, match="publisher"):
            load_trusted_publishers({publisher: _base64_key()})

    def test_invalid_key_names_the_publisher_but_not_the_key(self):
        bad = base64.b64encode(b"short").decode()
        with pytest.raises(PluginTrustConfigError) as exc_info:
            load_trusted_publishers({PUBLISHER: bad})
        message = str(exc_info.value)
        assert PUBLISHER in message
        assert bad not in message


class TestTrustPolicy:
    @pytest.mark.parametrize("policy", ["if-present", "required"])
    def test_accepts_known_policies(self, policy):
        assert validate_trust_policy(policy) == policy

    @pytest.mark.parametrize("policy", ["", "lenient", None, "REQUIRED"])
    def test_rejects_unknown_policies(self, policy):
        with pytest.raises(PluginTrustConfigError, match="PLUGIN_TRUST_POLICY"):
            validate_trust_policy(policy)


class TestValidateTrustConfig:
    def test_valid_configuration(self):
        publishers = validate_trust_config(
            {
                "PLUGIN_TRUSTED_KEYS": {PUBLISHER: _base64_key()},
                "PLUGIN_TRUST_POLICY": "required",
            }
        )
        assert list(publishers) == [PUBLISHER]

    def test_invalid_key_is_reported_with_the_setting_name(self):
        with pytest.raises(PluginTrustConfigError, match="PLUGIN_TRUSTED_KEYS"):
            validate_trust_config({"PLUGIN_TRUSTED_KEYS": {PUBLISHER: "bogus"}})

    def test_invalid_policy_is_reported_with_the_setting_name(self):
        with pytest.raises(PluginTrustConfigError, match="PLUGIN_TRUST_POLICY"):
            validate_trust_config({"PLUGIN_TRUSTED_KEYS": {}, "PLUGIN_TRUST_POLICY": "maybe"})


class TestReadOnlySurfaces:
    def test_ids_are_sorted_and_secret_free(self):
        key_one = _base64_key()
        key_two = _base64_key()
        raw = {"zeta": key_one, "alpha": key_two}

        ids = trusted_publisher_ids(raw)

        assert ids == ["alpha", "zeta"]
        assert key_one not in json.dumps(ids)
        assert key_two not in json.dumps(ids)

    def test_api_lists_publisher_ids_only(self, app, client, make_user, login):
        key = _base64_key()
        app.config["PLUGIN_TRUSTED_KEYS"] = {PUBLISHER: key}
        app.config["PLUGIN_TRUST_POLICY"] = "required"
        make_user()
        login()

        resp = client.get("/plugins/api/trusted-publishers")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"publishers": [PUBLISHER], "count": 1, "policy": "required"}
        assert key not in resp.get_data(as_text=True)

    def test_api_requires_login(self, app, client):
        app.config["PLUGIN_TRUSTED_KEYS"] = {PUBLISHER: _base64_key()}
        resp = client.get("/plugins/api/trusted-publishers")
        assert resp.status_code in (302, 401)

    def test_cli_json_and_human_output(self, app):
        key = _base64_key()
        app.config["PLUGIN_TRUSTED_KEYS"] = {PUBLISHER: key}
        runner = app.test_cli_runner()

        as_json = runner.invoke(args=["plugins", "publishers", "--json"])
        assert as_json.exit_code == 0
        assert json.loads(as_json.output) == {
            "publishers": [PUBLISHER],
            "count": 1,
            "policy": "if-present",
        }

        human = runner.invoke(args=["plugins", "publishers"])
        assert human.exit_code == 0
        assert PUBLISHER in human.output
        assert key not in human.output

    def test_cli_empty_configuration(self, app):
        app.config["PLUGIN_TRUSTED_KEYS"] = {}
        result = app.test_cli_runner().invoke(args=["plugins", "publishers"])
        assert result.exit_code == 0
        assert "No trusted publishers configured." in result.output


class TestStartupValidation:
    def test_create_app_rejects_invalid_key_material(self, monkeypatch):
        from app import create_app

        monkeypatch.setattr(TestingConfig, "PLUGIN_TRUSTED_KEYS", {PUBLISHER: "bogus"})

        with pytest.raises(RuntimeError, match="Invalid plugin trust configuration"):
            create_app("testing")

    def test_create_app_rejects_invalid_policy(self, monkeypatch):
        from app import create_app

        monkeypatch.setattr(TestingConfig, "PLUGIN_TRUSTED_KEYS", {})
        monkeypatch.setattr(TestingConfig, "PLUGIN_TRUST_POLICY", "lenient")

        with pytest.raises(RuntimeError, match="Invalid plugin trust configuration"):
            create_app("testing")
