"""Tests for the ``PluginLockEntry`` model (#394).

The model is the persistence half of the deterministic lockfile: rows are
owner/workspace scoped, serialize to the canonical lockfile shape (no volatile
timestamps), and never imply capability grants.
"""

import json

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import CapabilityGrant, PluginLockEntry, Workspace
from app.services.plugin_lockfile import LockfileEntry, dumps_lockfile, normalize_integrity

HASH_A = "A" * 64
HASH_B = "b" * 64


def _workspace(owner, name="WS"):
    ws = Workspace(user_id=owner.id, name=name)
    db.session.add(ws)
    db.session.commit()
    return ws


def _lock(
    workspace,
    owner,
    plugin_id="catalog-plugin",
    *,
    version="0.1.0",
    source="https://plugins.example.com/p.zip",
    integrity=HASH_A,
    dependencies=None,
):
    row = PluginLockEntry(
        workspace_id=workspace.id,
        owner_id=owner.id,
        plugin_id=plugin_id,
        version=version,
        source=source,
        integrity=integrity,
        dependencies=dependencies if dependencies is not None else ["dep>=1.0"],
    )
    db.session.add(row)
    db.session.commit()
    return row


class TestSerialization:
    def test_to_dict_contains_scoping_and_audit_fields(self, db, make_user):
        owner = make_user()
        ws = _workspace(owner)
        row = _lock(ws, owner)

        data = row.to_dict()

        assert data["id"] == row.id
        assert data["workspace_id"] == ws.id
        assert data["owner_id"] == owner.id
        assert data["plugin_id"] == "catalog-plugin"
        assert data["version"] == "0.1.0"
        assert data["source"] == "https://plugins.example.com/p.zip"
        assert data["integrity"] == HASH_A
        assert data["dependencies"] == ["dep>=1.0"]
        assert data["resolved_at"] == row.resolved_at.isoformat()

    def test_to_lockfile_entry_normalizes_integrity(self, db, make_user):
        owner = make_user()
        ws = _workspace(owner)
        row = _lock(ws, owner)

        entry = row.to_lockfile_entry()

        assert isinstance(entry, LockfileEntry)
        assert entry.integrity == normalize_integrity(HASH_A)
        assert entry.dependencies == ("dep>=1.0",)

    def test_lockfile_is_byte_stable_and_hides_volatile_fields(self, db, make_user):
        owner = make_user()
        ws = _workspace(owner)
        first = _lock(ws, owner, "zeta")
        second = _lock(ws, owner, "alpha", integrity=HASH_B)

        output = dumps_lockfile([first, second])
        document = json.loads(output)

        assert [entry["plugin_id"] for entry in document["plugins"]] == ["alpha", "zeta"]
        assert document["plugins"][1]["integrity"] == normalize_integrity(HASH_A)
        assert "resolved_at" not in output
        assert "workspace_id" not in output
        assert "owner_id" not in output
        assert output == dumps_lockfile([second, first])
        assert output.endswith("\n")


class TestScoping:
    def test_unique_per_workspace(self, db, make_user):
        owner = make_user()
        ws = _workspace(owner)
        _lock(ws, owner)

        db.session.add(
            PluginLockEntry(
                workspace_id=ws.id,
                owner_id=owner.id,
                plugin_id="catalog-plugin",
                version="0.2.0",
                source="https://plugins.example.com/p.zip",
                integrity=HASH_B,
            )
        )
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_same_plugin_allowed_in_another_workspace(self, db, make_user):
        owner = make_user()
        first = _workspace(owner, "One")
        second = _workspace(owner, "Two")
        _lock(first, owner)
        _lock(second, owner)

        assert PluginLockEntry.query.filter_by(plugin_id="catalog-plugin").count() == 2

    def test_rows_are_owner_scoped(self, db, make_user):
        owner = make_user()
        other = make_user(username="other", email="other@example.com")
        ws = _workspace(owner)
        row = _lock(ws, owner)

        assert PluginLockEntry.query.filter_by(owner_id=other.id).count() == 0
        assert PluginLockEntry.query.filter_by(owner_id=owner.id).one().id == row.id


class TestNoImplicitCapabilities:
    def test_lock_entries_never_grant_capabilities(self, db, make_user):
        owner = make_user()
        ws = _workspace(owner)
        _lock(ws, owner, dependencies=[])

        assert CapabilityGrant.query.filter_by(plugin_id="catalog-plugin").count() == 0
        assert CapabilityGrant.query.count() == 0
