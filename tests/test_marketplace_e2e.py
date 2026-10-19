"""Offline end-to-end tests for the Phase 9 marketplace flow (#423).

The tests exercise the real catalog loader, install endpoint, enable/disable
endpoint, capability grants, and event dispatcher. They use a local temp catalog
and a synthetic uploaded project, so no network access or real plugin code
execution is involved.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.extensions import db
from app.models import (
    ActivityEvent,
    CapabilityGrant,
    Plugin,
    PluginInstallation,
    Workspace,
)
from app.models.activity_event import EVENT_PLUGIN_DENIED
from app.services.events import get_dispatcher

INTEGRITY = "sha256:" + "a" * 64


def _manifest(plugin_id: str, *, dependencies=None, capabilities=None):
    return {
        "id": plugin_id,
        "name": f"Marketplace {plugin_id}",
        "version": "0.1.0",
        "description": "Offline marketplace end-to-end fixture",
        "author": "tester",
        "entry_point": "plugins.test:TestPlugin",
        "capabilities": capabilities or ["PROJECT_READ"],
        "dependencies": dependencies or [],
    }


def _entry(plugin_id: str, *, dependencies=None, capabilities=None):
    manifest = _manifest(
        plugin_id,
        dependencies=dependencies,
        capabilities=capabilities,
    )
    return {
        "id": plugin_id,
        "version": manifest["version"],
        # An opaque local identifier: the loader accepts it and the install
        # path never downloads it, which keeps this test fully offline.
        "source": f"catalog/{plugin_id}-{manifest['version']}.zip",
        "integrity": INTEGRITY,
        "manifest": manifest,
    }


def _write_catalog(tmp_path, entries):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"version": 1, "plugins": entries}), encoding="utf-8")
    return path


def _install(client, workspace_id: int, plugin_id: str):
    return client.post(
        f"/plugins/api/workspaces/{workspace_id}/plugins/install-from-catalog",
        json={"plugin_id": plugin_id},
    )


def _enable(client, workspace_id: int, plugin_id: str):
    return client.post(f"/plugins/api/workspaces/{workspace_id}/plugins/{plugin_id}/enable")


def _disable(client, workspace_id: int, plugin_id: str):
    return client.post(f"/plugins/api/workspaces/{workspace_id}/plugins/{plugin_id}/disable")


def _grant(client, workspace_id: int, plugin_id: str):
    return client.post(
        f"/plugins/api/workspaces/{workspace_id}/plugins/{plugin_id}/capabilities",
        json={"grant": ["PROJECT_READ"], "revoke": []},
    )


def _zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def _upload_project(client, workspace_id: int):
    return client.post(
        f"/workspaces/api/workspaces/{workspace_id}/projects",
        data={
            "file": (
                io.BytesIO(_zip_bytes([("main.py", "print('marketplace')\n")])),
                "marketplace.zip",
            )
        },
        content_type="multipart/form-data",
    )


@pytest.fixture()
def workspace(app, make_user, login):
    user = make_user()
    login()
    workspace = Workspace(user_id=user.id, name="Marketplace E2E")
    db.session.add(workspace)
    db.session.commit()
    return workspace


class TestMarketplaceCatalogToDispatch:
    def test_install_enable_dependency_order_and_dispatch(self, app, client, workspace, tmp_path):
        app.config["PLUGIN_CATALOG_FILE"] = str(
            _write_catalog(
                tmp_path,
                [
                    _entry("market-base"),
                    _entry("market-addon", dependencies=["market-base>=0.1.0"]),
                ],
            )
        )

        # The dependent plugin can be installed first (install records metadata
        # only), but it cannot be enabled until its dependency is registered.
        assert _install(client, workspace.id, "market-addon").status_code == 201
        assert _disable(client, workspace.id, "market-addon").status_code == 200
        denied = _enable(client, workspace.id, "market-addon")
        assert denied.status_code == 400
        installation = PluginInstallation.query.filter_by(
            plugin_id="market-addon", workspace_id=workspace.id
        ).one()
        assert installation.enabled is False

        # Install and enable the dependency, then the addon enable succeeds.
        assert _install(client, workspace.id, "market-base").status_code == 201
        assert _enable(client, workspace.id, "market-base").status_code == 200
        assert _enable(client, workspace.id, "market-addon").status_code == 200

        # Grants stay explicit, even after a catalog install.
        assert CapabilityGrant.query.filter_by(plugin_id="market-addon").count() == 0
        assert _grant(client, workspace.id, "market-addon").status_code == 200

        dispatcher = get_dispatcher()
        received = []

        def handler(event):
            received.append(event)

        dispatcher.subscribe("project.created", handler, plugin_id="market-addon")
        try:
            response = _upload_project(client, workspace.id)
        finally:
            dispatcher.unsubscribe("project.created", handler)

        assert response.status_code == 201
        assert len(received) == 1
        assert received[0].event_type == "project.created"
        assert received[0].workspace_id == workspace.id
        assert received[0].data["project_id"] == response.get_json()["id"]


class TestMarketplaceTrustRefusal:
    def test_required_policy_refuses_unsigned_entry_without_partial_writes(
        self, app, client, workspace, tmp_path
    ):
        app.config["PLUGIN_CATALOG_FILE"] = str(
            _write_catalog(tmp_path, [_entry("market-unsigned")])
        )
        app.config["PLUGIN_TRUST_POLICY"] = "required"

        response = _install(client, workspace.id, "market-unsigned")

        assert response.status_code == 403
        assert Plugin.query.filter_by(id="market-unsigned").count() == 0
        assert (
            PluginInstallation.query.filter_by(
                plugin_id="market-unsigned", workspace_id=workspace.id
            ).count()
            == 0
        )
        assert CapabilityGrant.query.filter_by(plugin_id="market-unsigned").count() == 0
        denied = ActivityEvent.query.filter_by(
            workspace_id=workspace.id, event_type=EVENT_PLUGIN_DENIED
        ).one()
        assert denied.event_metadata["plugin_id"] == "market-unsigned"
        assert denied.event_metadata["reason"] == "publisher signature refused"


class TestMarketplaceRollback:
    def test_duplicate_install_rolls_back_without_extra_rows(
        self, app, client, workspace, tmp_path
    ):
        app.config["PLUGIN_CATALOG_FILE"] = str(
            _write_catalog(tmp_path, [_entry("market-duplicate")])
        )

        assert _install(client, workspace.id, "market-duplicate").status_code == 201
        duplicate = _install(client, workspace.id, "market-duplicate")

        assert duplicate.status_code == 409
        assert Plugin.query.filter_by(id="market-duplicate").count() == 1
        assert (
            PluginInstallation.query.filter_by(
                plugin_id="market-duplicate", workspace_id=workspace.id
            ).count()
            == 1
        )
        assert CapabilityGrant.query.filter_by(plugin_id="market-duplicate").count() == 0

    def test_entry_point_conflict_leaves_no_workspace_installation(
        self, app, client, workspace, tmp_path
    ):
        db.session.add(
            Plugin(
                id="market-conflict",
                name="Original Plugin",
                version="0.1.0",
                entry_point="other.module:OtherPlugin",
                capabilities=["PROJECT_READ"],
            )
        )
        db.session.commit()
        app.config["PLUGIN_CATALOG_FILE"] = str(
            _write_catalog(tmp_path, [_entry("market-conflict")])
        )

        response = _install(client, workspace.id, "market-conflict")

        assert response.status_code == 409
        assert (
            PluginInstallation.query.filter_by(
                plugin_id="market-conflict", workspace_id=workspace.id
            ).count()
            == 0
        )
        plugin = db.session.get(Plugin, "market-conflict")
        assert plugin.entry_point == "other.module:OtherPlugin"
