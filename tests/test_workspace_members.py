"""Tests for workspace membership: roles, owner-only management, and isolation."""

from app.extensions import db
from app.models import User, Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_CONTRIBUTOR, ROLE_OWNER, ROLE_VIEWER


def _create_user(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _workspace_for(user, name="Team workspace"):
    workspace = Workspace(user_id=user.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


class TestMemberManagement:
    def test_add_list_update_remove(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        login(email="owner@example.com")
        workspace = _workspace_for(User.query.filter_by(username="owner").first())

        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "member", "role": "viewer"},
        )
        assert response.status_code == 201
        assert response.get_json()["role"] == ROLE_VIEWER

        response = client.get(f"/workspaces/api/workspaces/{workspace.id}/members")
        assert response.status_code == 200
        members = response.get_json()
        assert len(members) == 1
        assert members[0]["username"] == "member"

        response = client.patch(
            f"/workspaces/api/workspaces/{workspace.id}/members/{member.id}",
            json={"role": ROLE_CONTRIBUTOR},
        )
        assert response.status_code == 200
        assert response.get_json()["role"] == ROLE_CONTRIBUTOR

        response = client.delete(f"/workspaces/api/workspaces/{workspace.id}/members/{member.id}")
        assert response.status_code == 200
        # Removal is a soft-delete that preserves membership history (#135):
        # the row is retained with status "removed" and excluded from listings.
        row = WorkspaceMember.query.filter_by(workspace_id=workspace.id, user_id=member.id).one()
        assert row.status == "removed"
        assert row.removed_at is not None
        response = client.get(f"/workspaces/api/workspaces/{workspace.id}/members")
        assert response.get_json() == []

    def test_add_unknown_user(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        owner = User.query.filter_by(username="owner").first()
        workspace = _workspace_for(owner)
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "ghost", "role": "viewer"},
        )
        assert response.status_code == 404

    def test_add_duplicate_member(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        login(email="owner@example.com")
        owner = User.query.filter_by(username="owner").first()
        workspace = _workspace_for(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role=ROLE_VIEWER)
        )
        db.session.commit()
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "member", "role": "viewer"},
        )
        assert response.status_code == 409

    def test_add_self_rejected(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        owner = User.query.filter_by(username="owner").first()
        workspace = _workspace_for(owner)
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "owner", "role": "viewer"},
        )
        assert response.status_code == 400

    def test_invalid_role_rejected(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        owner = User.query.filter_by(username="owner").first()
        workspace = _workspace_for(owner)
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "ghost", "role": "admin"},
        )
        assert response.status_code == 400

    def test_owner_role_not_assignable(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        owner = User.query.filter_by(username="owner").first()
        workspace = _workspace_for(owner)
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "ghost", "role": ROLE_OWNER},
        )
        assert response.status_code == 400

    def test_requires_login(self, client):
        assert client.get("/workspaces/api/workspaces/1/members").status_code == 302


class TestOwnerOnlyIsolation:
    def test_non_owner_cannot_manage(self, client, make_user, login):
        owner = _create_user("owner", "owner@example.com")
        member = _create_user("member", "member@example.com")
        make_user(username="viewer", email="viewer@example.com")
        login(email="viewer@example.com")
        workspace = _workspace_for(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role=ROLE_VIEWER)
        )
        db.session.commit()
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/members").status_code == 404
        assert (
            client.post(
                f"/workspaces/api/workspaces/{workspace.id}/members",
                json={"username": "someone", "role": "viewer"},
            ).status_code
            == 404
        )

    def test_owner_workspace_visibility(self, client, make_user, login):
        owner = _create_user("owner", "owner@example.com")
        member = _create_user("member", "member@example.com")
        make_user(username="viewer", email="viewer@example.com")
        login(email="viewer@example.com")
        workspace = _workspace_for(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role=ROLE_VIEWER)
        )
        db.session.commit()

        # Workspace owner actions stay owner-scoped: a member (or anyone else)
        # cannot access the workspace through the owner-scoped routes, and a
        # user who is neither owner nor member is equally locked out.
        assert client.get(f"/workspaces/{workspace.id}").status_code == 404

        # A member cannot see workspace list entries owned by others.
        response = client.get("/workspaces/api/workspaces")
        assert response.get_json()["items"] == []

        # A member cannot access the workspace via the owner-scoped API.
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/projects").status_code == 404

    def test_member_does_not_break_other_users(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        first_workspace = _workspace_for(User.query.filter_by(username="owner").first())
        other = _create_user("other", "other@example.com")
        second_workspace = _workspace_for(other)

        assert client.get(f"/workspaces/{first_workspace.id}").status_code == 200
        assert client.get(f"/workspaces/{second_workspace.id}").status_code == 404


class TestMemberManagementAuthorization:
    """#126: management is owner-only with correct 403/404 semantics."""

    def _workspace_with_viewer_and_target(self):
        owner = _create_user("owner", "owner@example.com")
        viewer = _create_user("viewer", "viewer@example.com")
        target = _create_user("target", "target@example.com")
        workspace = _workspace_for(owner)
        db.session.add_all(
            [
                WorkspaceMember(workspace_id=workspace.id, user_id=viewer.id, role=ROLE_VIEWER),
                WorkspaceMember(workspace_id=workspace.id, user_id=target.id, role=ROLE_VIEWER),
            ]
        )
        db.session.commit()
        return workspace, viewer, target

    def test_known_member_gets_403_on_management(self, client, make_user, login):
        # A logged-in *member* without ``manage_members`` gets 403 (not the
        # uniform 404 reserved for non-members, which would be misleading).
        workspace, _viewer, target = self._workspace_with_viewer_and_target()
        login(email="viewer@example.com")
        assert (
            client.post(
                f"/workspaces/api/workspaces/{workspace.id}/members",
                json={"username": "target", "role": "viewer"},
            ).status_code
            == 403
        )
        assert (
            client.patch(
                f"/workspaces/api/workspaces/{workspace.id}/members/{target.id}",
                json={"role": "contributor"},
            ).status_code
            == 403
        )
        assert (
            client.delete(
                f"/workspaces/api/workspaces/{workspace.id}/members/{target.id}"
            ).status_code
            == 403
        )
        # ...but the read capability still works for the member.
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/members").status_code == 200

    def test_non_member_gets_404_and_no_leak(self, client, make_user, login):
        # A user who is not in the workspace must not learn whether it exists.
        workspace, _viewer, target = self._workspace_with_viewer_and_target()
        make_user(username="outsider", email="outsider@example.com")
        login(email="outsider@example.com")
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/members").status_code == 404
        assert (
            client.delete(
                f"/workspaces/api/workspaces/{workspace.id}/members/{target.id}"
            ).status_code
            == 404
        )

    def test_owner_row_cannot_be_changed_or_removed(self, client, make_user, login):
        # After an ownership transfer the new owner owns a membership row whose
        # role is ``owner``; it must never be demoted or removed via the API.
        owner = _create_user("owner", "owner@example.com")
        workspace = _workspace_for(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=owner.id, role=ROLE_OWNER)
        )
        db.session.commit()
        login(email="owner@example.com")
        assert (
            client.patch(
                f"/workspaces/api/workspaces/{workspace.id}/members/{owner.id}",
                json={"role": "viewer"},
            ).status_code
            == 400
        )
        assert (
            client.delete(
                f"/workspaces/api/workspaces/{workspace.id}/members/{owner.id}"
            ).status_code
            == 400
        )

    def test_cannot_update_removed_member(self, client, make_user, login):
        owner = _create_user("owner", "owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _workspace_for(owner)
        membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=member.id,
            role=ROLE_VIEWER,
        )
        db.session.add(membership)
        db.session.commit()
        membership.mark_removed()
        db.session.commit()
        login(email="owner@example.com")
        assert (
            client.patch(
                f"/workspaces/api/workspaces/{workspace.id}/members/{member.id}",
                json={"role": "contributor"},
            ).status_code
            == 409
        )
