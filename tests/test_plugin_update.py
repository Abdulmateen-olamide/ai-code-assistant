"""Tests for the compatibility gate on plugin updates (issue #405)."""

import pytest

from app.extensions import db
from app.models import Plugin, Workspace
from app.services.plugin_compat import (
    IncompatibleCompatibilityError,
    InvalidCompatibilityError,
    app_version,
    enforce_compatible,
)
from app.services.plugin_ops import (
    PluginEntryPointMismatchError,
    PluginUpdateError,
    update_plugin,
)
from app.services.plugins import ManifestValidationError, PluginManifest

PLUGIN_ID = "update-plugin"

BASE_MANIFEST = {
    "id": PLUGIN_ID,
    "name": "Update Plugin",
    "version": "1.0.0",
    "description": "first release",
    "author": "tester",
    "entry_point": "plugins.test:TestPlugin",
    "capabilities": ["PROJECT_READ"],
}


@pytest.fixture()
def workspace(app, make_user, login):
    user = make_user()
    login()
    ws = Workspace(user_id=user.id, name="Update workspace")
    db.session.add(ws)
    db.session.commit()
    return ws


def _install(client, workspace_id, manifest=None):
    resp = client.post(
        f"/plugins/api/workspaces/{workspace_id}/plugins/install",
        json={"manifest": manifest or dict(BASE_MANIFEST)},
    )
    assert resp.status_code == 201, resp.get_json()
    return resp


def _update(client, workspace_id, manifest):
    return client.post(
        f"/plugins/api/workspaces/{workspace_id}/plugins/{PLUGIN_ID}/update",
        json={"manifest": manifest},
    )


class TestCompatibilityGate:
    def test_incompatible_update_is_refused_with_clear_message(self, app, client, workspace):
        _install(client, workspace.id)

        resp = _update(
            client,
            workspace.id,
            dict(BASE_MANIFEST, version="2.0.0", compatibility=">=99.0.0"),
        )

        assert resp.status_code == 400
        error = resp.get_json()["error"]
        assert ">=99.0.0" in error
        assert app_version() in error
        plugin = db.session.get(Plugin, PLUGIN_ID)
        assert plugin.version == "1.0.0"  # nothing was written

    def test_compatible_update_is_applied(self, app, client, workspace):
        _install(client, workspace.id)
        manifest = dict(
            BASE_MANIFEST,
            version="1.1.0",
            description="second release",
            compatibility=f"<={app_version()}",
        )

        resp = _update(client, workspace.id, manifest)

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["version"] == "1.1.0"
        assert data["compatible_with_app"] is True
        plugin = db.session.get(Plugin, PLUGIN_ID)
        assert plugin.version == "1.1.0"
        assert plugin.description == "second release"

    def test_update_without_compatibility_is_unrestricted(self, app, client, workspace):
        _install(client, workspace.id)

        resp = _update(client, workspace.id, dict(BASE_MANIFEST, version="1.2.0"))

        assert resp.status_code == 200
        assert db.session.get(Plugin, PLUGIN_ID).version == "1.2.0"

    def test_invalid_specifier_is_rejected_before_update(self, app, client, workspace):
        _install(client, workspace.id)

        resp = _update(
            client,
            workspace.id,
            dict(BASE_MANIFEST, version="2.0.0", compatibility="not-a-specifier"),
        )

        assert resp.status_code == 400
        assert "Invalid compatibility specifier" in resp.get_json()["error"]
        assert db.session.get(Plugin, PLUGIN_ID).version == "1.0.0"


class TestUpdateIdentityAndScope:
    def test_entry_point_change_is_refused(self, app, client, workspace):
        _install(client, workspace.id)

        resp = _update(
            client,
            workspace.id,
            dict(BASE_MANIFEST, version="2.0.0", entry_point="plugins.other:OtherPlugin"),
        )

        assert resp.status_code == 409
        assert db.session.get(Plugin, PLUGIN_ID).version == "1.0.0"

    def test_manifest_id_must_match_the_plugin(self, app, client, workspace):
        _install(client, workspace.id)

        resp = _update(
            client,
            workspace.id,
            dict(BASE_MANIFEST, id="other-plugin", version="2.0.0"),
        )

        assert resp.status_code == 400
        assert db.session.get(Plugin, PLUGIN_ID).version == "1.0.0"

    def test_plugin_must_be_installed_in_the_workspace(self, app, client, workspace):
        owner_id = db.session.get(Workspace, workspace.id).user_id
        other_ws = Workspace(user_id=owner_id, name="Other workspace")
        db.session.add(other_ws)
        db.session.commit()

        resp = _update(client, other_ws.id, dict(BASE_MANIFEST, version="2.0.0"))

        assert resp.status_code == 404

    def test_manifest_object_is_required(self, client, workspace):
        _install(client, workspace.id)

        resp = client.post(
            f"/plugins/api/workspaces/{workspace.id}/plugins/{PLUGIN_ID}/update",
            json={},
        )

        assert resp.status_code == 400
        assert "manifest" in resp.get_json()["error"].lower()


class TestUpdateService:
    def test_service_refuses_incompatible_manifest(self, app, workspace):
        plugin = Plugin(
            id=PLUGIN_ID,
            name="Update Plugin",
            version="1.0.0",
            description="first release",
            author="tester",
            entry_point="plugins.test:TestPlugin",
            capabilities=["PROJECT_READ"],
        )
        db.session.add(plugin)
        db.session.commit()

        with pytest.raises(IncompatibleCompatibilityError) as excinfo:
            update_plugin(
                PLUGIN_ID,
                PluginManifest.from_dict(dict(BASE_MANIFEST, compatibility=">=99.0.0")),
            )

        message = str(excinfo.value)
        assert ">=99.0.0" in message
        assert app_version() in message
        assert db.session.get(Plugin, PLUGIN_ID).version == "1.0.0"

    def test_service_refuses_entry_point_change(self, app, workspace):
        db.session.add(
            Plugin(
                id=PLUGIN_ID,
                name="Update Plugin",
                version="1.0.0",
                description="first release",
                author="tester",
                entry_point="plugins.test:TestPlugin",
                capabilities=["PROJECT_READ"],
            )
        )
        db.session.commit()

        with pytest.raises(PluginEntryPointMismatchError):
            update_plugin(
                PLUGIN_ID,
                PluginManifest.from_dict(
                    dict(BASE_MANIFEST, entry_point="plugins.other:OtherPlugin")
                ),
            )

    def test_service_refuses_id_mismatch(self, app, workspace):
        db.session.add(
            Plugin(
                id=PLUGIN_ID,
                name="Update Plugin",
                version="1.0.0",
                description="first release",
                author="tester",
                entry_point="plugins.test:TestPlugin",
                capabilities=["PROJECT_READ"],
            )
        )
        db.session.commit()

        with pytest.raises(PluginUpdateError):
            update_plugin(
                PLUGIN_ID,
                PluginManifest.from_dict(dict(BASE_MANIFEST, id="other-plugin")),
            )


class TestEnforceCompatible:
    def test_absent_and_empty_ranges_are_unrestricted(self):
        enforce_compatible(None)
        enforce_compatible("")

    def test_incompatible_range_reports_range_and_version(self):
        with pytest.raises(IncompatibleCompatibilityError) as excinfo:
            enforce_compatible(">=99.0.0", subject="Plugin demo 1.0.0")
        assert "Plugin demo 1.0.0" in str(excinfo.value)
        assert ">=99.0.0" in str(excinfo.value)
        assert app_version() in str(excinfo.value)

    def test_invalid_range_raises_validation_error(self):
        with pytest.raises(InvalidCompatibilityError):
            enforce_compatible("bogus")

    def test_manifest_validation_still_rejects_invalid_range(self):
        with pytest.raises(ManifestValidationError):
            PluginManifest.from_dict(dict(BASE_MANIFEST, compatibility="bogus"))
