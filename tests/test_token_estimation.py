"""Tests for pre-flight token/cost estimation (issue #104)."""

import pathlib
from contextlib import contextmanager

from flask_login import login_user

from app.extensions import db
from app.models import Project, ProjectFile, User, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_INDEXING, STATUS_READY
from app.services import project_analysis


@contextmanager
def _authorized_context(app, project):
    with app.test_request_context("/"):
        login_user(db.session.get(User, project.user_id))
        yield


def _ready_project(files, *, status=STATUS_READY, username="estuser", email="estuser@example.com"):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    workspace = Workspace(user_id=user.id, name="Estimate workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Estimate project",
        source=SOURCE_ARCHIVE,
        status=status,
    )
    db.session.add(project)
    db.session.commit()
    for path, content in files:
        db.session.add(
            ProjectFile(
                project_id=project.id,
                path=path,
                size=len(content),
                is_binary=False,
                content=content,
            )
        )
    db.session.commit()
    project.file_count = len(files)
    project.total_size_bytes = sum(len(content) for _, content in files)
    db.session.commit()
    return user, project


class TestEstimateTokens:
    def test_empty_text_is_zero(self):
        assert project_analysis.estimate_tokens("") == 0
        assert project_analysis.estimate_tokens(None) == 0

    def test_rounds_up_to_whole_tokens(self):
        assert project_analysis.estimate_tokens("abcd") == 1
        assert project_analysis.estimate_tokens("a" * 400) == 100
        assert project_analysis.estimate_tokens("a" * 401) == 101


class TestEstimateChat:
    def test_matches_build_messages_and_never_calls_a_provider(self, app, monkeypatch):
        def boom():
            raise AssertionError("estimation must not contact a provider")

        monkeypatch.setattr(project_analysis, "get_provider", boom)
        _user, project = _ready_project([("app.py", "def main():\n    return 42\n")])

        with _authorized_context(app, project):
            messages = project_analysis.build_messages(project, "what does main return?", [])
            expected = sum(project_analysis.estimate_tokens(m["content"]) for m in messages)
            estimate = project_analysis.estimate_chat(project, "what does main return?")

        assert estimate["mode"] == "chat"
        assert estimate["prompt_tokens"] == expected
        assert estimate["prompt_tokens"] > 0

    def test_grows_with_the_retrieved_context(self, app):
        _user, small = _ready_project([("app.py", "x = 1\n")])
        _user2, large = _ready_project(
            [("app.py", "y = 2\n"), ("big.py", "print('z')\n" * 500)],
            username="estuser2",
            email="estuser2@example.com",
        )

        with _authorized_context(app, small):
            small_tokens = project_analysis.estimate_chat(small, "explain big.py")["prompt_tokens"]
        with _authorized_context(app, large):
            large_tokens = project_analysis.estimate_chat(large, "explain big.py")["prompt_tokens"]

        assert large_tokens > small_tokens


class TestEstimateAnalysis:
    def test_reports_context_tokens_for_each_kind(self, app):
        _user, project = _ready_project(
            [("app.py", "def f():\n    return 1\n"), ("README.md", "# Docs\n")]
        )

        with _authorized_context(app, project):
            for kind in project_analysis.ANALYSIS_KINDS:
                estimate = project_analysis.estimate_analysis(project, kind)
                assert estimate["mode"] == "analysis"
                assert estimate["prompt_tokens"] > 0

    def test_unknown_kind_defaults_to_architecture(self, app):
        _user, project = _ready_project([("app.py", "x = 1\n")])
        with _authorized_context(app, project):
            assert project_analysis.estimate_analysis(project, "bogus")["kind"] == "architecture"

    def test_cost_uses_the_configured_rate(self, app):
        _user, project = _ready_project([("app.py", "x = 1\n")])
        app.config["LLM_ESTIMATE_USD_PER_1K_TOKENS"] = 2.0
        with _authorized_context(app, project):
            estimate = project_analysis.estimate_analysis(project, "bugs")
        expected = round(estimate["prompt_tokens"] / 1000 * 2.0, 6)
        assert estimate["estimated_cost_usd"] == expected


class TestEstimateEndpoint:
    def _login_and_project(self, client, make_user, login, *, status=STATUS_READY):
        user = make_user(username="apiuser", email="apiuser@example.com")
        login(email="apiuser@example.com")
        workspace = Workspace(user_id=user.id, name="W")
        db.session.add(workspace)
        db.session.commit()
        project = Project(
            workspace_id=workspace.id,
            user_id=user.id,
            name="P",
            source=SOURCE_ARCHIVE,
            status=status,
        )
        db.session.add(project)
        db.session.commit()
        db.session.add(
            ProjectFile(
                project_id=project.id,
                path="app.py",
                size=10,
                is_binary=False,
                content="x = 1\n",
            )
        )
        db.session.commit()
        return user, project

    def test_estimate_chat_endpoint(self, client, make_user, login):
        _user, project = self._login_and_project(client, make_user, login)

        response = client.post(
            f"/workspaces/api/projects/{project.id}/estimate",
            json={"mode": "chat", "content": "explain app.py"},
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["mode"] == "chat"
        assert body["prompt_tokens"] > 0

    def test_estimate_analysis_endpoint(self, client, make_user, login):
        _user, project = self._login_and_project(client, make_user, login)

        response = client.post(
            f"/workspaces/api/projects/{project.id}/estimate",
            json={"mode": "analysis", "kind": "bugs"},
        )

        assert response.status_code == 200
        assert response.get_json()["kind"] == "bugs"

    def test_invalid_mode_and_kind_are_rejected(self, client, make_user, login):
        _user, project = self._login_and_project(client, make_user, login)

        bad_mode = client.post(
            f"/workspaces/api/projects/{project.id}/estimate", json={"mode": "nope"}
        )
        assert bad_mode.status_code == 400

        bad_kind = client.post(
            f"/workspaces/api/projects/{project.id}/estimate",
            json={"mode": "analysis", "kind": "nope"},
        )
        assert bad_kind.status_code == 400

    def test_empty_chat_message_is_rejected(self, client, make_user, login):
        _user, project = self._login_and_project(client, make_user, login)

        response = client.post(
            f"/workspaces/api/projects/{project.id}/estimate",
            json={"mode": "chat", "content": "   "},
        )

        assert response.status_code == 400

    def test_indexing_project_conflicts(self, client, make_user, login):
        _user, project = self._login_and_project(client, make_user, login, status=STATUS_INDEXING)

        response = client.post(
            f"/workspaces/api/projects/{project.id}/estimate",
            json={"mode": "chat", "content": "hi"},
        )

        assert response.status_code == 409

    def test_owner_scoped(self, client, make_user, login, db):
        _owner, project = self._login_and_project(client, make_user, login)
        make_user(username="intruder", email="intruder@example.com")
        login(email="intruder@example.com")

        response = client.post(
            f"/workspaces/api/projects/{project.id}/estimate",
            json={"mode": "chat", "content": "hi"},
        )

        assert response.status_code == 404


class TestEstimateUiWiring:
    def test_template_and_script_expose_the_estimate(self, app):
        root = pathlib.Path(app.root_path)
        template = (root / "templates" / "workspaces" / "project.html").read_text(encoding="utf-8")
        assert 'id="project-chat-estimate"' in template

        script = (root / "static" / "js" / "project.js").read_text(encoding="utf-8")
        assert "/estimate" in script
        assert "project-chat-estimate" in script
        assert "formatEstimate" in script
