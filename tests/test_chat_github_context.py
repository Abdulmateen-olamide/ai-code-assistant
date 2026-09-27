"""Tests for GitHub context references in chat (issue #74).

A chat message can point the assistant at a repository (``owner/repo``) or at a
specific issue or pull request (``owner/repo#123``, or a bare ``#123`` next to a
repository). The referenced material is fetched through the GitHub client,
bounded by ``GITHUB_MAX_CONTEXT_CHARS``, and handed to the provider as a system
message. A user who has not connected GitHub gets a clear connection prompt
rather than a silent no-op.
"""

import json

import pytest

from app.chat import routes as chat_routes
from app.config import Config
from app.extensions import db
from app.models import GithubAccount, Message
from app.services.github import (
    CONTEXT_TRUNCATED_MARKER,
    GitHubNotConnectedError,
    build_github_context,
    parse_context_references,
)
from app.services.providers import ProviderResponse

README_TEXT = "# Rocket\n\nA test repository for orbiting things."
ISSUE_TITLE = "Crash when the tube is empty"
PULL_TITLE = "Add a fuel gauge"


class FakeResponse:
    """Minimal ``requests.Response`` stand-in covering what the client touches."""

    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text
        self.headers = {}

    @property
    def content(self):
        """Body bytes; the client uses truthiness to detect an empty response."""
        if self._data is not None:
            return json.dumps(self._data).encode("utf-8")
        return self.text.encode("utf-8")

    def json(self):
        return self._data


class RoutedSession:
    """A ``requests.Session`` stand-in that answers by URL substring."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, timeout=None):
        self.calls.append((method, url, params))
        return self._route(url)

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(("GET", url, params))
        return self._route(url)

    def _route(self, url):
        for needle, response in self.routes:
            if needle in url:
                return response
        raise AssertionError("unrouted URL: " + url)


def _repo_payload(full_name="octo/rocket"):
    return {
        "full_name": full_name,
        "name": full_name.split("/")[1],
        "description": "A test repository",
        "language": "Python",
        "default_branch": "main",
        "html_url": "https://github.com/" + full_name,
    }


def _issue_payload(number=12):
    return {
        "number": number,
        "title": ISSUE_TITLE,
        "state": "open",
        "user": {"login": "octocat"},
        "labels": [{"name": "bug"}],
        "body": "Steps to reproduce: start the engine.",
        "html_url": "https://github.com/octo/rocket/issues/" + str(number),
    }


def _pull_payload(number=12):
    return {
        "number": number,
        "title": PULL_TITLE,
        "state": "open",
        "user": {"login": "octocat"},
        "head": {"ref": "feature/gauge"},
        "base": {"ref": "main"},
        "changed_files": 3,
        "additions": 40,
        "deletions": 2,
        "body": "Adds a gauge.",
        "html_url": "https://github.com/octo/rocket/pull/" + str(number),
    }


def _issue_routes(*, readme=False):
    """Routes where ``octo/rocket#12`` is a plain issue, not a pull request."""
    routes = []
    if readme:
        routes.append(("/readme", FakeResponse(200, text=README_TEXT)))
    routes.append(("/pulls/12", FakeResponse(404, {"message": "Not Found"})))
    routes.append(("/issues/12", FakeResponse(200, _issue_payload())))
    routes.append(("/repos/octo/rocket", FakeResponse(200, _repo_payload())))
    return routes


def _session(monkeypatch, routes):
    session = RoutedSession(routes)
    monkeypatch.setattr("app.services.github.requests.Session", lambda: session)
    return session


def _connect(user):
    """Give ``user`` a connected GitHub account with a decryptable token."""
    account = GithubAccount(
        user_id=user.id,
        github_user_id=4242,
        github_username="octocat",
        scopes="repo",
    )
    account.set_access_token("gho_test_token")
    db.session.add(account)
    db.session.commit()
    return account


def _create_conversation(client):
    response = client.post("/chat/conversations", json={}, headers={"X-CSRFToken": "ignored"})
    assert response.status_code == 201
    return response.get_json()


class RecordingProvider:
    """Captures the message lists it is asked to complete."""

    name = "recording"
    models = ("recording-1",)

    def __init__(self):
        self.calls = []

    def chat(self, messages, *, model=None, params=None):
        self.calls.append(list(messages))
        return ProviderResponse(content="ok", model=model or "recording-1")

    def stream(self, messages, *, model=None, params=None):
        self.calls.append(list(messages))
        yield "ok"


@pytest.fixture()
def recording_provider(monkeypatch):
    provider = RecordingProvider()
    monkeypatch.setattr(chat_routes, "RetryingProvider", lambda inner, **kwargs: inner)
    monkeypatch.setattr(chat_routes, "build_provider", lambda user, name=None: provider)

    from app.chat import api as chat_api

    monkeypatch.setattr(chat_api, "RetryingProvider", lambda inner, **kwargs: inner)
    monkeypatch.setattr(chat_api, "build_provider", lambda user, name=None: provider)
    return provider


def _system_text(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "system")


class TestParseContextReferences:
    def test_repository_with_number(self):
        assert parse_context_references("Summarise octo/rocket#12") == [
            {"kind": "thread", "full_name": "octo/rocket", "number": 12}
        ]

    def test_bare_repository(self):
        assert parse_context_references("What does octo/rocket do?") == [
            {"kind": "repo", "full_name": "octo/rocket", "number": None}
        ]

    def test_bare_number_resolves_against_the_named_repository(self):
        refs = parse_context_references("In octo/rocket, what is #12 about?")
        kinds = {(r["kind"], r["full_name"], r["number"]) for r in refs}
        assert ("repo", "octo/rocket", None) in kinds
        assert ("thread", "octo/rocket", 12) in kinds

    def test_bare_number_without_a_repository_is_ignored(self):
        assert parse_context_references("What is #12 about?") == []

    def test_duplicate_references_collapse(self):
        refs = parse_context_references("octo/rocket#12 then again octo/rocket#12")
        assert len(refs) == 1

    def test_plain_message_has_no_references(self):
        assert parse_context_references("hello there") == []

    def test_markdown_heading_is_not_a_reference(self):
        assert parse_context_references("# Summary of the changes") == []


class TestBuildGithubContext:
    def test_repository_context_includes_description_and_readme(self, app, make_user, monkeypatch):
        _session(
            monkeypatch,
            [
                ("/readme", FakeResponse(200, text=README_TEXT)),
                ("/repos/octo/rocket", FakeResponse(200, _repo_payload())),
            ],
        )
        user = make_user()
        _connect(user)

        result = build_github_context(user, "Explain octo/rocket")

        assert "A test repository" in result["context"]
        assert "README" in result["context"]
        assert "orbiting things" in result["context"]
        assert result["notices"] == []
        assert result["references"] == [
            {"kind": "repo", "full_name": "octo/rocket", "number": None}
        ]

    def test_issue_context_includes_title_labels_and_body(self, app, make_user, monkeypatch):
        _session(monkeypatch, _issue_routes())
        user = make_user()
        _connect(user)

        result = build_github_context(user, "Summarise octo/rocket#12")

        assert ISSUE_TITLE in result["context"]
        assert "bug" in result["context"]
        assert "start the engine" in result["context"]

    def test_pull_request_is_preferred_over_issue(self, app, make_user, monkeypatch):
        _session(
            monkeypatch,
            [
                ("/pulls/12", FakeResponse(200, _pull_payload())),
                ("/repos/octo/rocket", FakeResponse(200, _repo_payload())),
            ],
        )
        user = make_user()
        _connect(user)

        result = build_github_context(user, "Review octo/rocket#12")

        assert PULL_TITLE in result["context"]
        assert "feature/gauge" in result["context"]

    def test_missing_reference_becomes_a_notice(self, app, make_user, monkeypatch):
        _session(
            monkeypatch,
            [
                ("/pulls/12", FakeResponse(404, {"message": "Not Found"})),
                ("/issues/12", FakeResponse(404, {"message": "Not Found"})),
            ],
        )
        user = make_user()
        _connect(user)

        result = build_github_context(user, "Summarise octo/rocket#12")

        assert result["context"] == ""
        assert result["notices"]
        assert "octo/rocket#12" in result["notices"][0]

    def test_context_is_bounded_by_the_configured_cap(self, app, make_user, monkeypatch):
        monkeypatch.setattr(Config, "GITHUB_MAX_CONTEXT_CHARS", 120)
        long_readme = "x" * 4000
        _session(
            monkeypatch,
            [
                ("/readme", FakeResponse(200, text=long_readme)),
                ("/repos/octo/rocket", FakeResponse(200, _repo_payload())),
            ],
        )
        user = make_user()
        _connect(user)

        result = build_github_context(user, "Explain octo/rocket")

        assert CONTEXT_TRUNCATED_MARKER in result["context"]
        assert len(result["context"]) <= 120 + len(CONTEXT_TRUNCATED_MARKER)
        assert len(result["context"]) < len(long_readme)

    def test_unconnected_user_raises_so_the_caller_can_prompt(self, app, make_user):
        user = make_user()
        with pytest.raises(GitHubNotConnectedError):
            build_github_context(user, "Explain octo/rocket")

    def test_message_without_references_never_touches_github(self, app, make_user, monkeypatch):
        def _explode(*args, **kwargs):  # pragma: no cover - only hit on regression
            raise AssertionError("GitHub should not be contacted without a reference")

        monkeypatch.setattr("app.services.github.get_github_client", _explode)
        user = make_user()

        result = build_github_context(user, "hello there")

        assert result == {"context": "", "references": [], "notices": []}


class TestGithubContextInChat:
    def test_reference_context_reaches_the_provider(
        self, client, app, make_user, login, monkeypatch, recording_provider
    ):
        user = make_user()
        login()
        _connect(user)
        _session(monkeypatch, _issue_routes())
        conversation = _create_conversation(client)

        response = client.post(
            "/chat/conversations/{}/messages".format(conversation["id"]),
            json={"content": "Summarise octo/rocket#12"},
            headers={"X-CSRFToken": "ignored"},
        )

        assert response.status_code == 201
        sent = recording_provider.calls[-1]
        assert ISSUE_TITLE in _system_text(sent)
        # The user's own message is still delivered verbatim.
        assert any(m["role"] == "user" and "octo/rocket#12" in m["content"] for m in sent)

    def test_streaming_endpoint_injects_context(
        self, client, app, make_user, login, monkeypatch, recording_provider
    ):
        user = make_user()
        login()
        _connect(user)
        _session(monkeypatch, _issue_routes())
        conversation = _create_conversation(client)

        response = client.post(
            "/chat/conversations/{}/stream".format(conversation["id"]),
            json={"content": "Summarise octo/rocket#12"},
            headers={"X-CSRFToken": "ignored"},
        )

        assert response.status_code == 200
        assert "done" in response.get_data(as_text=True)
        assert ISSUE_TITLE in _system_text(recording_provider.calls[-1])

    def test_unconnected_user_sees_a_connection_prompt(
        self, client, app, make_user, login, recording_provider
    ):
        make_user()
        login()
        conversation = _create_conversation(client)

        response = client.post(
            "/chat/conversations/{}/messages".format(conversation["id"]),
            json={"content": "Summarise octo/rocket#12"},
            headers={"X-CSRFToken": "ignored"},
        )

        assert response.status_code == 409
        payload = response.get_json()
        assert payload["code"] == "github_not_connected"
        assert "Connect your GitHub account" in payload["error"]
        assert payload["connect_url"] == "/github/connect"
        # The message is not stored and no provider call is made.
        assert Message.query.count() == 0
        assert recording_provider.calls == []

    def test_plain_message_still_works_without_a_github_connection(
        self, client, app, make_user, login, recording_provider
    ):
        make_user()
        login()
        conversation = _create_conversation(client)

        response = client.post(
            "/chat/conversations/{}/messages".format(conversation["id"]),
            json={"content": "hello there"},
            headers={"X-CSRFToken": "ignored"},
        )

        assert response.status_code == 201
        assert recording_provider.calls

    def test_json_api_reports_the_connection_prompt(
        self, client, app, make_user, login, recording_provider
    ):
        make_user()
        login()
        conversation = _create_conversation(client)

        response = client.post(
            "/api/conversations/{}/messages".format(conversation["id"]),
            json={"content": "Summarise octo/rocket#12"},
        )

        assert response.status_code == 409
        payload = response.get_json()
        assert payload["code"] == "github_not_connected"
        assert payload["connect_url"] == "/github/connect"
        assert payload["status"] == 409
