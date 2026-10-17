"""Tests for the central usage ledger and its source breakdown API (Phase 10).

Covers recording/normalisation/scoping (issues #429, #432), aggregation by
source including unknown historical values (#451), and the instrumentation of
project chat and project analysis.
"""

import json

import pytest

from app.extensions import db
from app.models import Conversation, Message, Project, ProjectFile, UsageRecord, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY
from app.services import usage_ledger


def _project(make_user, login, username="ledgeruser", email="ledgeruser@example.com"):
    user = make_user(username=username, email=email)
    login(email=email)
    workspace = Workspace(user_id=user.id, name="Ledger workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Ledger project",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()
    db.session.add(
        ProjectFile(
            project_id=project.id,
            path="app.py",
            size=12,
            is_binary=False,
            content="def main(): pass\n",
        )
    )
    db.session.commit()
    return user, workspace, project


class TestRecordUsage:
    def test_records_counts_scope_and_cost(self, db, make_user):
        user = make_user(username="ledger", email="ledger@example.com")
        workspace = Workspace(user_id=user.id, name="W")
        db.session.add(workspace)
        db.session.commit()
        project = Project(
            workspace_id=workspace.id,
            user_id=user.id,
            name="P",
            source=SOURCE_ARCHIVE,
            status=STATUS_READY,
        )
        db.session.add(project)
        db.session.commit()

        record = usage_ledger.record_usage(
            source=" Analysis ",
            prompt_tokens=100,
            completion_tokens=50,
            user=user,
            project=project,
            provider="openai",
            model="gpt-4o",
            tokens_estimated=True,
        )

        assert record.id is not None
        assert record.source == "analysis"
        assert record.workspace_id == workspace.id
        assert record.project_id == project.id
        assert record.user_id == user.id
        assert record.prompt_tokens == 100
        assert record.completion_tokens == 50
        assert record.total_tokens == 150
        assert record.tokens_estimated is True
        assert record.cost_estimate == pytest.approx(0.00075)

    def test_missing_counts_default_to_zero(self, db):
        record = usage_ledger.record_usage(source="tools", provider="mock", model="mock-1")
        assert record.prompt_tokens == 0
        assert record.completion_tokens == 0
        assert record.total_tokens == 0
        assert record.cost_estimate == 0.0

    def test_unknown_model_cost_stays_unknown(self, db):
        record = usage_ledger.record_usage(
            source="chat",
            prompt_tokens=10,
            provider="openai",
            model="not-a-real-model",
        )
        assert record.cost_estimate is None

    def test_negative_counts_are_rejected(self, db):
        with pytest.raises(ValueError):
            usage_ledger.record_usage(source="chat", prompt_tokens=-1)
        with pytest.raises(ValueError):
            usage_ledger.record_usage(source="chat", completion_tokens=-1)
        with pytest.raises(ValueError):
            usage_ledger.record_usage(source="chat", cost_estimate=-0.1)

    def test_records_request_latency_and_status(self, db):
        record = usage_ledger.record_usage(
            source="review",
            provider="openai",
            model="gpt-4o",
            request_id="  req-123  ",
            latency_ms=1250,
            status="SUCCESS",
        )

        assert record.request_id == "req-123"
        assert record.latency_ms == 1250
        assert record.status == "success"
        payload = record.to_dict()
        assert payload["request_id"] == "req-123"
        assert payload["latency_ms"] == 1250
        assert payload["status"] == "success"

    def test_correlation_fields_default_to_none(self, db):
        record = usage_ledger.record_usage(source="tools", provider="mock", model="mock-1")

        assert record.request_id is None
        assert record.latency_ms is None
        assert record.status is None
        assert record.to_dict()["request_id"] is None

    def test_request_id_is_bounded(self, db):
        record = usage_ledger.record_usage(source="chat", request_id="x" * 200)

        assert record.request_id is not None
        assert len(record.request_id) == 64

    def test_invalid_latency_is_rejected(self, db):
        with pytest.raises(ValueError):
            usage_ledger.record_usage(source="chat", latency_ms=-1)
        with pytest.raises(ValueError):
            usage_ledger.record_usage(source="chat", latency_ms="not-a-number")


class TestBreakdown:
    def test_groups_known_and_unknown_sources(self, db, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        other = make_user(username="other", email="other@example.com")

        rows = [
            ("chat", 10, 5),
            ("chat", 1, 1),
            ("analysis", 20, 10),
            ("project_chat", 3, 2),
            ("review", 0, 4),
            ("tools", 5, 0),
            ("legacy-source", 7, 7),
        ]
        for source, prompt, completion in rows:
            usage_ledger.record_usage(
                source=source,
                prompt_tokens=prompt,
                completion_tokens=completion,
                user=owner,
                provider="mock",
                model="mock-1",
            )
        usage_ledger.record_usage(
            source="chat",
            prompt_tokens=999,
            completion_tokens=999,
            user=other,
            provider="mock",
            model="mock-1",
        )

        breakdown = usage_ledger.breakdown_by_source(user_id=owner.id)

        assert breakdown["chat"]["count"] == 2
        assert breakdown["chat"]["prompt_tokens"] == 11
        assert breakdown["chat"]["total_tokens"] == 17
        assert breakdown["analysis"]["count"] == 1
        assert breakdown["analysis"]["total_tokens"] == 30
        assert breakdown["review"]["count"] == 1
        assert breakdown["tools"]["count"] == 1
        assert breakdown["project_chat"]["count"] == 1
        assert breakdown["unknown"]["count"] == 1
        assert breakdown["unknown"]["total_tokens"] == 14
        assert breakdown["chat"]["cost_estimate"] == 0.0
        assert sum(item["count"] for item in breakdown.values()) == 7

    def test_scope_filters_are_applied(self, db, make_user):
        user = make_user(username="scope", email="scope@example.com")
        workspace = Workspace(user_id=user.id, name="Scoped")
        db.session.add(workspace)
        db.session.commit()
        project = Project(
            workspace_id=workspace.id,
            user_id=user.id,
            name="Scoped project",
            source=SOURCE_ARCHIVE,
            status=STATUS_READY,
        )
        db.session.add(project)
        db.session.commit()

        usage_ledger.record_usage(source="chat", user=user, prompt_tokens=5)
        usage_ledger.record_usage(source="analysis", user=user, project=project, prompt_tokens=7)

        by_workspace = usage_ledger.breakdown_by_source(user_id=user.id, workspace_id=workspace.id)
        by_project = usage_ledger.breakdown_by_source(user_id=user.id, project_id=project.id)

        assert sum(item["count"] for item in by_workspace.values()) == 1
        assert by_workspace["analysis"]["total_tokens"] == 7
        assert sum(item["count"] for item in by_project.values()) == 1
        assert by_project["analysis"]["total_tokens"] == 7


class TestBreakdownApi:
    def test_requires_authentication(self, client):
        response = client.get("/api/usage/breakdown")
        assert response.status_code == 401
        assert response.mimetype == "application/problem+json"

    def test_returns_counts_totals_and_currency(self, client, db, make_user, login):
        user = make_user(username="breakdown", email="breakdown@example.com")
        login(email="breakdown@example.com")
        usage_ledger.record_usage(
            source="chat", user=user, provider="mock", model="mock-1", prompt_tokens=10
        )
        usage_ledger.record_usage(
            source="analysis",
            user=user,
            provider="mock",
            model="mock-1",
            prompt_tokens=20,
            completion_tokens=5,
        )
        usage_ledger.record_usage(source="mystery", user=user, prompt_tokens=1)

        response = client.get("/api/usage/breakdown")

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["currency"]["code"] == "USD"
        assert payload["cost_is_estimate"] is True
        assert payload["by_source"]["chat"]["count"] == 1
        assert payload["by_source"]["analysis"]["total_tokens"] == 25
        assert payload["by_source"]["unknown"]["count"] == 1
        assert payload["totals"]["count"] == 3
        assert payload["totals"]["total_tokens"] == 36
        assert {item["source"] for item in payload["sources"]} >= {
            "chat",
            "analysis",
            "unknown",
        }

    def test_other_users_usage_is_not_visible(self, client, db, make_user, login):
        owner = make_user(username="api-owner", email="api-owner@example.com")
        usage_ledger.record_usage(source="chat", user=owner, prompt_tokens=100)
        make_user(username="api-other", email="api-other@example.com")
        login(email="api-other@example.com")

        payload = client.get("/api/usage/breakdown").get_json()

        assert payload["totals"]["count"] == 0
        assert all(item["count"] == 0 for item in payload["sources"])


class TestProjectInstrumentation:
    def test_project_chat_records_a_project_chat_row(self, client, db, make_user, login):
        user, workspace, project = _project(make_user, login)

        response = client.post(
            f"/workspaces/api/projects/{project.id}/chat",
            json={"content": "What does app.py do?"},
        )

        assert response.status_code == 201
        rows = UsageRecord.query.filter_by(source="project_chat").all()
        assert len(rows) == 1
        row = rows[0]
        assert row.user_id == user.id
        assert row.workspace_id == workspace.id
        assert row.project_id == project.id
        assert row.total_tokens > 0
        assert row.tokens_estimated is True
        assert row.provider == "mock"
        assert row.model == "mock-1"

    def test_project_chat_stream_records_a_row(self, client, db, make_user, login):
        _, workspace, project = _project(
            make_user, login, username="streamuser", email="streamuser@example.com"
        )

        response = client.post(
            f"/workspaces/api/projects/{project.id}/chat/stream",
            json={"content": "Explain helper.py"},
        )

        assert response.status_code == 200
        assert '"type": "done"' in response.get_data(as_text=True)
        rows = UsageRecord.query.filter_by(source="project_chat").all()
        assert len(rows) == 1
        assert rows[0].workspace_id == workspace.id
        assert rows[0].project_id == project.id
        assert rows[0].total_tokens > 0

    def test_each_project_analysis_records_a_row(self, client, db, make_user, login):
        user, workspace, project = _project(
            make_user, login, username="analysisuser", email="analysisuser@example.com"
        )

        for kind in ("bugs", "architecture"):
            response = client.post(
                f"/workspaces/api/projects/{project.id}/analyze", json={"kind": kind}
            )
            assert response.status_code == 200

        rows = UsageRecord.query.filter_by(source="analysis").order_by(UsageRecord.id).all()
        assert len(rows) == 2
        for row in rows:
            assert row.user_id == user.id
            assert row.workspace_id == workspace.id
            assert row.project_id == project.id
            assert row.total_tokens > 0
            assert row.tokens_estimated is True
            assert row.provider == "mock"
            assert row.model == "mock-1"


class TestMessageBackfill:
    def _conversation(self, make_user, **kwargs):
        user = make_user(**kwargs)
        conversation = Conversation(user_id=user.id, title="Historical chat")
        db.session.add(conversation)
        db.session.commit()
        return user, conversation

    def _message(self, conversation, **fields):
        values = {"role": "assistant", "content": "historical reply", **fields}
        message = Message(conversation_id=conversation.id, **values)
        db.session.add(message)
        db.session.commit()
        return message

    def test_empty_database_reports_zero(self, app):
        result = app.test_cli_runner().invoke(args=["usage", "backfill", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["scanned"] == 0
        assert payload["created"] == 0
        assert payload["skipped_existing"] == 0
        assert payload["batches"] == 0

    def test_backfills_chat_messages_and_is_idempotent(self, app, db, make_user):
        user, conversation = self._conversation(make_user)
        first = self._message(
            conversation,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
        second = self._message(
            conversation,
            prompt_tokens=4,
            completion_tokens=6,
            total_tokens=10,
        )
        self._message(conversation, role="user", content="no usage")  # ignored

        runner = app.test_cli_runner()
        initial = runner.invoke(args=["usage", "backfill", "--json"])

        assert initial.exit_code == 0
        initial_payload = json.loads(initial.output)
        assert initial_payload["scanned"] == 2
        assert initial_payload["created"] == 2
        assert initial_payload["skipped_existing"] == 0

        rows = UsageRecord.query.filter_by(source="chat").order_by(UsageRecord.id).all()
        assert len(rows) == 2
        assert {(row.prompt_tokens, row.completion_tokens, row.total_tokens) for row in rows} == {
            (10, 5, 15),
            (4, 6, 10),
        }
        assert all(row.user_id == user.id for row in rows)
        assert all(row.conversation_id == conversation.id for row in rows)
        assert all(row.tokens_estimated is True for row in rows)

        # The source message rows are never modified.
        assert db.session.get(Message, first.id).prompt_tokens == 10
        assert db.session.get(Message, second.id).prompt_tokens == 4

        repeat = runner.invoke(args=["usage", "backfill", "--json"])
        assert repeat.exit_code == 0
        repeat_payload = json.loads(repeat.output)
        assert repeat_payload["scanned"] == 2
        assert repeat_payload["created"] == 0
        assert repeat_payload["skipped_existing"] == 2
        assert UsageRecord.query.filter_by(source="chat").count() == 2

    def test_batch_size_and_limit_bound_the_run(self, app, db, make_user):
        _, conversation = self._conversation(make_user)
        for _ in range(3):
            self._message(
                conversation,
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
            )

        result = app.test_cli_runner().invoke(
            args=["usage", "backfill", "--batch-size", "1", "--limit", "2", "--json"]
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["scanned"] == 2
        assert payload["created"] == 2
        assert payload["batches"] == 2
        assert UsageRecord.query.filter_by(source="chat").count() == 2

    def test_dry_run_writes_nothing(self, app, db, make_user):
        _, conversation = self._conversation(make_user)
        self._message(
            conversation,
            prompt_tokens=3,
            completion_tokens=2,
            total_tokens=5,
        )

        result = app.test_cli_runner().invoke(args=["usage", "backfill", "--dry-run", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert payload["created"] == 1
        assert UsageRecord.query.filter_by(source="chat").count() == 0


class TestReviewInstrumentation:
    def test_each_review_records_a_scoped_review_row(self, client, db, make_user, login):
        user, workspace, project = _project(
            make_user,
            login,
            username="reviewuser",
            email="reviewuser@example.com",
        )

        for kind in ("quality", "security"):
            response = client.post(
                "/reviews/api/reviews",
                json={"source": "project", "project_id": project.id, "kind": kind},
            )
            assert response.status_code == 201

        rows = UsageRecord.query.filter_by(source="review").order_by(UsageRecord.id).all()
        assert len(rows) == 2
        for row in rows:
            assert row.user_id == user.id
            assert row.workspace_id == workspace.id
            assert row.project_id == project.id
            assert row.total_tokens > 0
            assert row.tokens_estimated is True
            assert row.provider == "mock"
            assert row.model == "mock-1"
            assert row.status == usage_ledger.STATUS_SUCCESS
            assert row.request_id
            assert row.latency_ms is not None
            assert row.latency_ms >= 0

    def test_failed_review_records_an_error_row(self, client, db, make_user, login, monkeypatch):
        from app.services.llm import LLMProviderError

        _, workspace, project = _project(
            make_user,
            login,
            username="failedreview",
            email="failedreview@example.com",
        )

        def boom():
            raise LLMProviderError("provider unavailable")

        monkeypatch.setattr("app.services.reviews.get_provider", boom)
        response = client.post(
            "/reviews/api/reviews",
            json={"source": "project", "project_id": project.id, "kind": "quality"},
        )

        assert response.status_code == 201
        assert response.get_json()["status"] == "failed"
        rows = UsageRecord.query.filter_by(source="review").all()
        assert len(rows) == 1
        row = rows[0]
        assert row.workspace_id == workspace.id
        assert row.project_id == project.id
        assert row.total_tokens == 0
        assert row.tokens_estimated is True
        assert row.status == usage_ledger.STATUS_ERROR
        assert row.request_id
        assert row.latency_ms is not None
