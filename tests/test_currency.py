"""Tests for currency configuration and consistent cost formatting (issue #441)."""

import pytest

from app.services.currency import (
    DEFAULT_CURRENCY_CODE,
    format_cost,
    format_estimate,
    get_currency_config,
    normalize_currency,
)


class TestNormalizeCurrency:
    def test_defaults_to_usd(self):
        assert normalize_currency() == {
            "code": DEFAULT_CURRENCY_CODE,
            "symbol": "$",
            "decimal_places": 4,
        }

    def test_missing_and_malformed_values_fall_back(self):
        assert normalize_currency({"code": "", "symbol": None, "decimal_places": "nope"}) == {
            "code": "USD",
            "symbol": "",
            "decimal_places": 4,
        }

    def test_precision_is_bounded(self):
        assert normalize_currency({"decimal_places": -2})["decimal_places"] == 0
        assert normalize_currency({"decimal_places": 99})["decimal_places"] == 8


class TestFormatCost:
    def test_usd_is_explicit(self):
        assert format_cost(0.0025) == "$0.0025 USD"

    def test_configured_currency_is_used(self):
        currency = {"code": "eur", "symbol": "€", "decimal_places": 2}
        assert format_cost(12.345, currency) == "€12.35 EUR"

    def test_rounds_half_up(self):
        currency = {"code": "USD", "symbol": "$", "decimal_places": 4}
        assert format_cost(0.00005, currency) == "$0.0001 USD"

    def test_thousands_are_grouped(self):
        currency = {"code": "USD", "symbol": "$", "decimal_places": 2}
        assert format_cost(1234567.891, currency) == "$1,234,567.89 USD"

    def test_negative_and_non_finite_are_rejected(self):
        with pytest.raises(ValueError):
            format_cost(-0.01)
        with pytest.raises(ValueError):
            format_cost(float("nan"))

    def test_format_estimate_labels_the_estimate(self):
        assert format_estimate(1234, 0.0024) == ("Estimated: ~1,234 tokens ($0.0024 USD; estimate)")


class TestCurrencyConfig:
    def test_reads_application_config(self, app):
        app.config["CURRENCY_CODE"] = "GBP"
        app.config["CURRENCY_SYMBOL"] = "£"
        app.config["CURRENCY_DECIMAL_PLACES"] = 3
        assert get_currency_config(app) == {
            "code": "GBP",
            "symbol": "£",
            "decimal_places": 3,
        }

    def test_estimate_endpoint_returns_currency_and_display(self, client, make_user, login):
        user = make_user(username="currency", email="currency@example.com")
        login(email="currency@example.com")
        from app.extensions import db
        from app.models import Project, ProjectFile, Workspace
        from app.models.project import SOURCE_ARCHIVE, STATUS_READY

        workspace = Workspace(user_id=user.id, name="Currency workspace")
        db.session.add(workspace)
        db.session.commit()
        project = Project(
            workspace_id=workspace.id,
            user_id=user.id,
            name="Currency project",
            source=SOURCE_ARCHIVE,
            status=STATUS_READY,
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

        response = client.post(
            f"/workspaces/api/projects/{project.id}/estimate",
            json={"mode": "chat", "content": "explain app.py"},
        )

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["currency"]["code"] == "USD"
        assert payload["cost_is_estimate"] is True
        assert payload["estimated_cost_display"].endswith(" USD")
        assert payload["estimated_cost_display"].startswith("$")
        assert payload["estimated_cost"] == payload["estimated_cost_usd"]
