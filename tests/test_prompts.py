"""Tests for the prompts blueprint: CRUD, favorites, categories, search,
and version history (create/list/revert/retention)."""

from datetime import UTC, datetime, timedelta

from app.models import Prompt, PromptVersion
from app.services import prompt_versions


def _register(client, username="tester", email="tester@example.com"):
    client.post(
        "/auth/register",
        data={
            "username": username,
            "email": email,
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
    )


def _create_prompt(client, title="Explain code", content="Explain this code:", category="Explain"):
    return client.post(
        "/prompts/api/prompts",
        json={"title": title, "content": content, "category": category},
        headers={"X-CSRFToken": "ignored"},
    )


class TestPromptsPage:
    def test_prompts_page_requires_login(self, client):
        response = client.get("/prompts/")
        assert response.status_code == 302

    def test_prompts_page_renders(self, client):
        _register(client)
        response = client.get("/prompts/")
        assert response.status_code == 200
        assert b"Prompt Library" in response.data


class TestPromptCrud:
    def test_create_prompt(self, client, db):
        _register(client)
        response = _create_prompt(client)
        assert response.status_code == 201
        assert response.get_json()["category"] == "Explain"
        assert Prompt.query.count() == 1

    def test_create_requires_title_and_content(self, client, db):
        _register(client)
        response = client.post(
            "/prompts/api/prompts",
            json={"title": "", "content": ""},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 400

    def test_update_prompt(self, client, db):
        _register(client)
        created = _create_prompt(client).get_json()
        response = client.patch(
            f"/prompts/api/prompts/{created['id']}",
            json={"title": "Renamed prompt"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.get_json()["title"] == "Renamed prompt"

    def test_delete_prompt(self, client, db):
        _register(client)
        created = _create_prompt(client).get_json()
        response = client.delete(
            f"/prompts/api/prompts/{created['id']}",
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 200
        assert Prompt.query.count() == 0

    def test_other_users_prompt_is_404(self, client, db):
        _register(client, username="owner", email="owner@example.com")
        created = _create_prompt(client).get_json()
        client.post("/auth/logout")
        _register(client, username="intruder", email="intruder@example.com")
        response = client.get(f"/prompts/api/prompts/{created['id']}")
        assert response.status_code == 404


class TestPromptFilters:
    def test_favorite_filter(self, client, db):
        _register(client)
        created = _create_prompt(client).get_json()
        client.post(
            f"/prompts/api/prompts/{created['id']}/favorite",
            headers={"X-CSRFToken": "ignored"},
        )
        response = client.get("/prompts/api/prompts?favorites=1")
        assert [p["id"] for p in response.get_json()] == [created["id"]]

    def test_toggle_favorite_flips_flag(self, client, db):
        _register(client)
        created = _create_prompt(client).get_json()
        response = client.post(
            f"/prompts/api/prompts/{created['id']}/favorite",
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.get_json()["is_favorite"] is True
        response = client.post(
            f"/prompts/api/prompts/{created['id']}/favorite",
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.get_json()["is_favorite"] is False

    def test_category_filter(self, client, db):
        _register(client)
        _create_prompt(client, title="Explainer", category="Explain")
        _create_prompt(client, title="Generator", category="Generate")
        response = client.get("/prompts/api/prompts?category=Generate")
        assert [p["title"] for p in response.get_json()] == ["Generator"]

    def test_search_matches_title_content_category(self, client, db):
        _register(client)
        _create_prompt(client, title="Refactor helper", content="Refactor this snippet")
        _create_prompt(client, title="Unrelated", content="Nothing to see")
        response = client.get("/prompts/api/prompts?q=refactor")
        assert len(response.get_json()) == 1
        assert response.get_json()[0]["title"] == "Refactor helper"

    def test_categories_endpoint(self, client, db):
        _register(client)
        _create_prompt(client, category="Explain")
        _create_prompt(client, category="Generate")
        _create_prompt(client, category="Explain")
        response = client.get("/prompts/api/categories")
        categories = response.get_json()
        assert set(categories) == {"Explain", "Generate"}


def _patch(client, prompt_id, payload):
    return client.patch(
        f"/prompts/api/prompts/{prompt_id}",
        json=payload,
        headers={"X-CSRFToken": "ignored"},
    )


class TestPromptVersioning:
    def test_create_records_initial_version(self, client, db):
        _register(client)
        created = _create_prompt(client, content="Version one").get_json()

        response = client.get(f"/prompts/api/prompts/{created['id']}/versions")
        assert response.status_code == 200
        versions = response.get_json()
        assert len(versions) == 1
        assert versions[0]["version"] == 1
        assert versions[0]["content"] == "Version one"
        assert versions[0]["changed_by"] is not None

    def test_update_records_new_version_with_diff(self, client, db):
        _register(client)
        created = _create_prompt(client, content="line one").get_json()
        _patch(client, created["id"], {"content": "line two"})

        versions = client.get(f"/prompts/api/prompts/{created['id']}/versions").get_json()
        assert [v["version"] for v in versions] == [1, 2]
        assert versions[1]["content"] == "line two"
        assert "-line one" in versions[1]["diff"]
        assert "+line two" in versions[1]["diff"]

    def test_favorite_change_does_not_create_version(self, client, db):
        _register(client)
        created = _create_prompt(client).get_json()
        _patch(client, created["id"], {"is_favorite": True})

        versions = client.get(f"/prompts/api/prompts/{created['id']}/versions").get_json()
        assert [v["version"] for v in versions] == [1]

    def test_revert_restores_content_and_creates_new_version(self, client, db):
        _register(client)
        created = _create_prompt(client, content="original").get_json()
        _patch(client, created["id"], {"content": "changed"})
        first = client.get(f"/prompts/api/prompts/{created['id']}/versions").get_json()[0]

        response = _patch(client, created["id"], {"revert_to_version": first["id"]})
        assert response.status_code == 200
        assert response.get_json()["content"] == "original"

        versions = client.get(f"/prompts/api/prompts/{created['id']}/versions").get_json()
        assert [v["version"] for v in versions] == [1, 2, 3]
        assert versions[-1]["content"] == "original"

    def test_revert_endpoint_accepts_version_number(self, client, db):
        _register(client)
        created = _create_prompt(client, content="v1").get_json()
        _patch(client, created["id"], {"content": "v2"})

        response = client.post(
            f"/prompts/api/prompts/{created['id']}/revert",
            json={"version": 1},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 200
        assert response.get_json()["content"] == "v1"

    def test_revert_unknown_version_is_404(self, client, db):
        _register(client)
        created = _create_prompt(client).get_json()
        response = _patch(client, created["id"], {"revert_to_version": 999999})
        assert response.status_code == 404

    def test_single_version_payload_includes_diff(self, client, db):
        _register(client)
        created = _create_prompt(client, content="hello").get_json()
        version_id = client.get(f"/prompts/api/prompts/{created['id']}/versions").get_json()[0][
            "id"
        ]
        response = client.get(f"/prompts/api/prompts/{created['id']}/versions/{version_id}")
        assert response.status_code == 200
        assert response.get_json()["content"] == "hello"

    def test_delete_retains_version_history(self, client, db):
        _register(client)
        created = _create_prompt(client).get_json()
        client.delete(
            f"/prompts/api/prompts/{created['id']}",
            headers={"X-CSRFToken": "ignored"},
        )
        assert Prompt.query.count() == 0
        retained = PromptVersion.query.filter_by(prompt_id=created["id"]).all()
        assert len(retained) == 1
        assert retained[0].prompt_deleted_at is not None

    def test_purge_removes_versions_past_retention(self, client, db, app):
        _register(client)
        created = _create_prompt(client).get_json()
        client.delete(
            f"/prompts/api/prompts/{created['id']}",
            headers={"X-CSRFToken": "ignored"},
        )
        # Within the retention window nothing is purged.
        app.config["PROMPT_VERSION_RETENTION_DAYS"] = 30
        assert prompt_versions.purge_expired_versions() == 0

        version = PromptVersion.query.filter_by(prompt_id=created["id"]).first()
        version.prompt_deleted_at = datetime.now(UTC) - timedelta(days=90)
        db.session.commit()
        assert prompt_versions.purge_expired_versions() == 1
        assert PromptVersion.query.count() == 0

    def test_resolve_version_prefers_id_then_number(self, client, db):
        _register(client)
        _create_prompt(client, title="A", content="a1")
        second = _create_prompt(client, title="B", content="b1").get_json()
        version = PromptVersion.query.filter_by(prompt_id=second["id"]).first()
        assert version.version == 1
        # The row id is globally unique, so it differs from the per-prompt number.
        assert version.id != version.version
        assert prompt_versions.resolve_version(second["id"], version.id).id == version.id
        assert prompt_versions.resolve_version(second["id"], version.version).id == version.id

    def test_versions_are_owner_scoped(self, client, db):
        _register(client, username="owner", email="owner@example.com")
        created = _create_prompt(client).get_json()
        client.post("/auth/logout")
        _register(client, username="intruder", email="intruder@example.com")

        assert client.get(f"/prompts/api/prompts/{created['id']}/versions").status_code == 404
        assert client.get(f"/prompts/api/prompts/{created['id']}/versions/1").status_code == 404
        assert (
            client.post(
                f"/prompts/api/prompts/{created['id']}/revert",
                json={"version": 1},
                headers={"X-CSRFToken": "ignored"},
            ).status_code
            == 404
        )
