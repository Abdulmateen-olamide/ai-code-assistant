"""Tests for the catalog-backed remote install endpoint (#389).

The endpoint is owner-only, installs only catalog-pinned metadata, enforces the
publisher trust policy before writing anything, and never creates capability
grants or executes plugin code.
"""

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.extensions import db
from app.models import (
    ActivityEvent,
    CapabilityGrant,
    Plugin,
    PluginInstallation,
    Workspace,
    WorkspaceMember,
)
from app.models.activity_event import EVENT_PLUGIN_DENIED
from app.services.plugin_catalog import (
    CatalogEntryNotFoundError,
    CatalogError,
    CatalogUnavailableError,
    load_catalog_file,
    load_plugin_catalog,
    parse_catalog,
)
from app.services.plugin_signatures import canonical_manifest_body

PUBLISHER = "acme-labs"
HASH = "a" * 64


def _manifest(plugin_id="catalog-plugin", version="0.1.0", **overrides):
    manifest = {
        "id": plugin_id,
        "name": "Catalog Plugin",
        "version": version,
        "description": "A catalogued test plugin",
        "author": "Tester",
        "entry_point": "plugins.test:TestPlugin",
        "capabilities": ["PROJECT_READ"],
    }
    manifest.update(overrides)
    return manifest


def _entry(manifest=None, *, signature=None, **overrides):
    manifest = manifest or _manifest()
    entry = {
        "id": manifest["id"],
        "version": manifest["version"],
        "source": "https://plugins.example.com/catalog-plugin-0.1.0.zip",
        "integrity": f"sha256:{HASH}",
        "manifest": manifest,
    }
    entry.update(overrides)
    if signature is not None:
        entry["signature"] = signature
    return entry


def _publisher():
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private_key, base64.b64encode(raw).decode()


def _signature(private_key, manifest, publisher=PUBLISHER):
    return {
        "publisher": publisher,
        "algorithm": "ed25519",
        "value": base64.b64encode(private_key.sign(canonical_manifest_body(manifest))).decode(),
    }


def _signed_entry(private_key, manifest=None, publisher=PUBLISHER, **overrides):
    manifest = manifest or _manifest()
    return _entry(
        manifest=manifest,
        signature=_signature(private_key, manifest, publisher),
        **overrides,
    )


def _write_catalog(tmp_path, entries, *, version=1):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps({"version": version, "plugins": entries}),
        encoding="utf-8",
    )
    return path


def _workspace(owner, name="WS"):
    ws = Workspace(user_id=owner.id, name=name)
    db.session.add(ws)
    db.session.commit()
    return ws


def _member(ws, user, role="contributor"):
    membership = WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=role)
    db.session.add(membership)
    db.session.commit()
    return membership


def _install(client, ws_id, payload):
    return client.post(
        f"/plugins/api/workspaces/{ws_id}/plugins/install-from-catalog",
        json=payload,
    )


class TestCatalogParsing:
    def test_parses_and_normalizes_entries(self):
        catalog = parse_catalog(
            {
                "version": 1,
                "plugins": [
                    _entry(id="zeta", manifest=_manifest("zeta")),
                    _entry(
                        id="alpha",
                        manifest=_manifest("alpha"),
                        integrity=HASH.upper(),
                    ),
                ],
            }
        )

        assert len(catalog) == 2
        assert [entry.plugin_id for entry in catalog.list_entries()] == ["alpha", "zeta"]
        alpha = catalog.get("alpha")
        assert alpha.integrity == f"sha256:{HASH}"
        assert alpha.publisher is None

    def test_selects_the_newest_version_by_default(self):
        catalog = parse_catalog(
            {
                "plugins": [
                    _entry(manifest=_manifest(version="0.1.0")),
                    _entry(manifest=_manifest(version="0.10.0")),
                    _entry(manifest=_manifest(version="0.9.0")),
                ]
            }
        )

        assert catalog.get("catalog-plugin").version == "0.10.0"
        assert catalog.get("catalog-plugin", "0.9.0").version == "0.9.0"
        assert catalog.get("catalog-plugin", "0.10").version == "0.10.0"

    def test_unknown_id_and_version_raise(self):
        catalog = parse_catalog({"plugins": [_entry()]})

        with pytest.raises(CatalogEntryNotFoundError, match="ghost"):
            catalog.get("ghost")
        with pytest.raises(CatalogEntryNotFoundError, match=r"9\.9\.9"):
            catalog.get("catalog-plugin", "9.9.9")

    @pytest.mark.parametrize(
        "document",
        [
            [],
            {"version": 2, "plugins": []},
            {"plugins": {}},
            {"version": 1},
        ],
    )
    def test_rejects_malformed_documents(self, document):
        with pytest.raises(CatalogError):
            parse_catalog(document)

    def test_rejects_duplicate_entries(self):
        with pytest.raises(CatalogError, match="duplicate"):
            parse_catalog({"plugins": [_entry(), _entry()]})

    @pytest.mark.parametrize(
        "entry",
        [
            _entry(id="BadId"),
            _entry(version="not-a-version"),
            _entry(source="http://plugins.example.com/p.zip"),
            _entry(integrity="sha512:" + "a" * 128),
            _entry(signature={"publisher": PUBLISHER, "algorithm": "rsa", "value": "x"}),
            _entry(id="different-id"),
            _entry(version="2.0.0", manifest=_manifest(version="0.1.0")),
            {
                "id": "catalog-plugin",
                "version": "0.1.0",
                "source": "https://plugins.example.com/p.zip",
                "integrity": f"sha256:{HASH}",
                "manifest": "not-an-object",
            },
            {"id": "catalog-plugin"},
        ],
    )
    def test_rejects_invalid_entries_with_the_index(self, entry):
        with pytest.raises(CatalogError, match="catalog entry 0"):
            parse_catalog({"plugins": [entry]})

    def test_load_catalog_file_errors(self, tmp_path):
        with pytest.raises(CatalogUnavailableError):
            load_catalog_file(tmp_path / "missing.json")

        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with pytest.raises(CatalogError, match="JSON"):
            load_catalog_file(broken)

        oversized = tmp_path / "big.json"
        oversized.write_bytes(b"{" + b" " * (1024 * 1024))
        with pytest.raises(CatalogUnavailableError):
            load_catalog_file(oversized)

    def test_unconfigured_catalog_is_unavailable(self):
        with pytest.raises(CatalogUnavailableError, match="PLUGIN_CATALOG_FILE"):
            load_plugin_catalog({"PLUGIN_CATALOG_FILE": ""})


class TestCatalogInstallAuthorization:
    def test_owner_can_install(self, app, client, make_user, login, tmp_path):
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [_entry()]))
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 201

    def test_member_is_forbidden(self, app, client, make_user, login, tmp_path):
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [_entry()]))
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        ws = _workspace(owner)
        _member(ws, member)
        login(email="member@example.com")

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 403
        assert db.session.get(Plugin, "catalog-plugin") is None

    def test_outsider_gets_404(self, app, client, make_user, login, tmp_path):
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [_entry()]))
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="outsider", email="outsider@example.com")
        ws = _workspace(owner)
        login(email="outsider@example.com")

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 404

    def test_anonymous_is_not_allowed(self, app, client, make_user, tmp_path):
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [_entry()]))
        owner = make_user()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code in (302, 401)


class TestCatalogInstallSuccess:
    def test_installs_metadata_only_without_grants_or_code_execution(
        self, app, client, make_user, login, tmp_path, monkeypatch
    ):
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [_entry()]))
        owner = make_user()
        login()
        ws = _workspace(owner)

        def _fail_load(*args, **kwargs):  # pragma: no cover - only runs on regression
            raise AssertionError("catalog install must never load or execute plugin code")

        monkeypatch.setattr("app.services.plugins.Plugin.load", _fail_load)
        monkeypatch.setattr("app.services.plugins.Plugin.get_instance", _fail_load)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 201
        data = resp.get_json()
        assert data["id"] == "catalog-plugin"
        assert data["installed"] is True
        assert data["trust_state"] == "Unverified"
        assert data["source"].startswith("https://")
        assert data["integrity"] == f"sha256:{HASH}"

        plugin = db.session.get(Plugin, "catalog-plugin")
        assert plugin is not None
        assert plugin.entry_point == "plugins.test:TestPlugin"
        installation = PluginInstallation.query.filter_by(
            plugin_id="catalog-plugin", workspace_id=ws.id
        ).one()
        assert installation.enabled is True
        assert installation.granted_capabilities == []
        assert CapabilityGrant.query.filter_by(plugin_id="catalog-plugin").count() == 0

    def test_pins_the_requested_version(self, app, client, make_user, login, tmp_path):
        catalog_path = _write_catalog(
            tmp_path,
            [
                _entry(manifest=_manifest(version="0.1.0")),
                _entry(manifest=_manifest(version="0.2.0")),
            ],
        )
        app.config["PLUGIN_CATALOG_FILE"] = str(catalog_path)
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin", "version": "0.1.0"})

        assert resp.status_code == 201
        assert db.session.get(Plugin, "catalog-plugin").version == "0.1.0"


class TestCatalogInstallTrustPolicy:
    def test_required_policy_refuses_unsigned_entries(
        self, app, client, make_user, login, tmp_path
    ):
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [_entry()]))
        app.config["PLUGIN_TRUST_POLICY"] = "required"
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 403
        assert "required" in resp.get_json()["error"]
        assert db.session.get(Plugin, "catalog-plugin") is None
        denied = ActivityEvent.query.filter_by(
            workspace_id=ws.id, event_type=EVENT_PLUGIN_DENIED
        ).one()
        assert denied.event_metadata["plugin_id"] == "catalog-plugin"
        assert denied.event_metadata["reason"] == "publisher signature refused"
        assert HASH not in json.dumps(denied.event_metadata)

    def test_valid_signature_installs_under_required_policy(
        self, app, client, make_user, login, tmp_path
    ):
        private_key, public_key = _publisher()
        manifest = _manifest()
        app.config["PLUGIN_CATALOG_FILE"] = str(
            _write_catalog(tmp_path, [_signed_entry(private_key, manifest)])
        )
        app.config["PLUGIN_TRUSTED_KEYS"] = {PUBLISHER: public_key}
        app.config["PLUGIN_TRUST_POLICY"] = "required"
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 201
        data = resp.get_json()
        assert data["trust_state"] == "Trusted"
        assert data["trust_publisher"] == PUBLISHER
        assert db.session.get(Plugin, "catalog-plugin").trust_state == "Trusted"

    def test_forged_signature_is_refused_even_under_if_present(
        self, app, client, make_user, login, tmp_path
    ):
        private_key, public_key = _publisher()
        manifest = _manifest()
        entry = _signed_entry(private_key, manifest)
        entry["manifest"]["name"] = "Tampered Plugin"
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [entry]))
        app.config["PLUGIN_TRUSTED_KEYS"] = {PUBLISHER: public_key}
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 403
        assert db.session.get(Plugin, "catalog-plugin") is None
        assert (
            ActivityEvent.query.filter_by(
                workspace_id=ws.id, event_type=EVENT_PLUGIN_DENIED
            ).count()
            == 1
        )

    def test_signature_from_an_unconfigured_publisher_is_refused(
        self, app, client, make_user, login, tmp_path
    ):
        private_key, _ = _publisher()
        app.config["PLUGIN_CATALOG_FILE"] = str(
            _write_catalog(tmp_path, [_signed_entry(private_key, _manifest())])
        )
        app.config["PLUGIN_TRUSTED_KEYS"] = {}
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 403
        assert PUBLISHER in resp.get_json()["error"]

    def test_embedded_manifest_signature_satisfies_required_policy(
        self, app, client, make_user, login, tmp_path
    ):
        private_key, public_key = _publisher()
        manifest = _manifest()
        signed_manifest = {**manifest, "signature": _signature(private_key, manifest)}
        app.config["PLUGIN_CATALOG_FILE"] = str(
            _write_catalog(tmp_path, [_entry(manifest=signed_manifest)])
        )
        app.config["PLUGIN_TRUSTED_KEYS"] = {PUBLISHER: public_key}
        app.config["PLUGIN_TRUST_POLICY"] = "required"
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 201
        assert resp.get_json()["trust_state"] == "Trusted"


class TestCatalogInstallFailures:
    def test_unknown_plugin_is_404(self, app, client, make_user, login, tmp_path):
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [_entry()]))
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "ghost"})

        assert resp.status_code == 404
        assert "ghost" in resp.get_json()["error"]

    def test_unknown_version_is_404(self, app, client, make_user, login, tmp_path):
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [_entry()]))
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin", "version": "9.9.9"})

        assert resp.status_code == 404

    def test_invalid_version_is_400(self, app, client, make_user, login, tmp_path):
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [_entry()]))
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin", "version": "not-a-version"})

        assert resp.status_code == 400
        assert "Invalid plugin version" in resp.get_json()["error"]

    def test_missing_plugin_id_is_400(self, app, client, make_user, login, tmp_path):
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [_entry()]))
        owner = make_user()
        login()
        ws = _workspace(owner)

        assert _install(client, ws.id, {}).status_code == 400
        assert _install(client, ws.id, {"plugin_id": "  "}).status_code == 400
        assert (
            _install(client, ws.id, {"plugin_id": "catalog-plugin", "version": 1}).status_code
            == 400
        )

    def test_unconfigured_catalog_is_404(self, app, client, make_user, login):
        app.config["PLUGIN_CATALOG_FILE"] = ""
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 404
        assert "PLUGIN_CATALOG_FILE" in resp.get_json()["error"]

    def test_invalid_manifest_in_entry_is_400(self, app, client, make_user, login, tmp_path):
        manifest = _manifest(capabilities=[])
        app.config["PLUGIN_CATALOG_FILE"] = str(
            _write_catalog(tmp_path, [_entry(manifest=manifest)])
        )
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 400
        assert "Invalid plugin manifest" in resp.get_json()["error"]

    def test_incompatible_range_is_400(self, app, client, make_user, login, tmp_path):
        manifest = _manifest(compatibility=">=99.0.0")
        app.config["PLUGIN_CATALOG_FILE"] = str(
            _write_catalog(tmp_path, [_entry(manifest=manifest)])
        )
        owner = make_user()
        login()
        ws = _workspace(owner)

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 400
        assert "99.0.0" in resp.get_json()["error"]

    def test_duplicate_install_is_409(self, app, client, make_user, login, tmp_path):
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [_entry()]))
        owner = make_user()
        login()
        ws = _workspace(owner)
        assert _install(client, ws.id, {"plugin_id": "catalog-plugin"}).status_code == 201

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 409
        assert (
            PluginInstallation.query.filter_by(
                plugin_id="catalog-plugin", workspace_id=ws.id
            ).count()
            == 1
        )

    def test_entry_point_mismatch_is_409(self, app, client, make_user, login, tmp_path):
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(tmp_path, [_entry()]))
        owner = make_user()
        login()
        ws = _workspace(owner)
        db.session.add(
            Plugin(
                id="catalog-plugin",
                name="Catalog Plugin",
                version="0.1.0",
                entry_point="other.module:OtherPlugin",
                capabilities=["PROJECT_READ"],
            )
        )
        db.session.commit()

        resp = _install(client, ws.id, {"plugin_id": "catalog-plugin"})

        assert resp.status_code == 409
        assert (
            PluginInstallation.query.filter_by(
                plugin_id="catalog-plugin", workspace_id=ws.id
            ).count()
            == 0
        )
