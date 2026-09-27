"""Tests for structured chat audit logging and request-id correlation (#17)."""

import json
import logging

import pytest

from app.services import chat_audit
from app.services.providers.base import ProviderResponse, ProviderUnavailableError
from app.services.providers.retry import RetryingProvider


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self) -> list[dict]:
        formatter = chat_audit.JsonFormatter()
        return [json.loads(formatter.format(record)) for record in self.records]

    def raw(self) -> str:
        return " ".join(chat_audit.JsonFormatter().format(record) for record in self.records)


@pytest.fixture()
def capture():
    handler = _Capture()
    chat_audit.LOGGER.addHandler(handler)
    chat_audit.LOGGER.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        chat_audit.LOGGER.removeHandler(handler)


def _register_and_login(client, email="audit@example.com", password="supersecret123"):
    client.post(
        "/auth/register",
        data={
            "username": "auditor",
            "email": email,
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
    )
    # Registration logs the user in; start the login flow from a clean session.
    client.post("/auth/logout")
    client.post("/auth/login", data={"email": email, "password": password})


def _events_by_name(capture, name):
    return [event for event in capture.events() if event.get("event") == name]


class TestChatAuditLogging:
    def test_login_success_is_logged_structured(self, client, capture):
        _register_and_login(client)

        login = _events_by_name(capture, "chat.login")
        assert login
        success = [event for event in login if event.get("status") == "success"]
        assert success
        assert success[0]["user_id"] is not None
        # Correlation id is attached from the request context.
        assert success[0]["request_id"]

    def test_login_failure_is_logged_without_pii(self, client, capture):
        _register_and_login(client, password="wrong-password")

        failures = [
            event
            for event in _events_by_name(capture, "chat.login")
            if event.get("status") == "failure"
        ]
        assert failures
        assert failures[0]["reason"] == "invalid_credentials"
        raw = capture.raw()
        assert "audit@example.com" not in raw
        assert "wrong" not in raw.replace("invalid_credentials", "")
        assert "supersecret123" not in raw

    def test_key_lifecycle_events(self, client, capture):
        _register_and_login(client)
        created = client.post(
            "/keys/api/keys", json={"provider": "openai", "key": "sk-secret-abcdef"}
        ).get_json()

        assert _events_by_name(capture, "chat.key_created")
        assert _events_by_name(capture, "chat.key_verified")

        client.delete(f"/keys/api/keys/{created['id']}")
        deleted = _events_by_name(capture, "chat.key_deleted")
        assert deleted and deleted[0]["key_id"] == created["id"]

        # The secret material is never written to the audit log.
        assert "sk-secret-abcdef" not in capture.raw()


class TestRequestId:
    def test_inbound_request_id_is_echoed(self, client):
        response = client.get("/auth/login", headers={"X-Request-ID": "trace-123"})
        assert response.headers[chat_audit.REQUEST_ID_HEADER] == "trace-123"

    def test_missing_request_id_is_generated(self, client):
        response = client.get("/auth/login")
        request_id = response.headers.get(chat_audit.REQUEST_ID_HEADER)
        assert request_id and len(request_id) == 32

    def test_unsafe_request_id_is_sanitized(self, client):
        response = client.get("/auth/login", headers={"X-Request-ID": "bad value!!"})
        assert response.headers[chat_audit.REQUEST_ID_HEADER] == "badvalue"


class TestProviderCallLogging:
    class _FakeProvider:
        name = "fake"
        models = ("fake-1",)

        def __init__(self, *, response=None, error=None):
            self.response = response
            self.error = error

        def chat(self, messages, *, model=None, params=None):
            if self.error is not None:
                raise self.error
            return self.response

        def stream(self, messages, *, model=None, params=None):
            if self.error is not None:
                raise self.error
            yield "chunk"

    def _retrying(self, provider):
        return RetryingProvider(provider, max_retries=0, sleep=lambda _d: None, jitter=lambda: 0.0)

    def test_success_logs_provider_status_tokens(self, capture):
        provider = self._FakeProvider(
            response=ProviderResponse(content="hi", prompt_tokens=3, completion_tokens=2)
        )

        self._retrying(provider).chat([])

        events = _events_by_name(capture, "chat.provider_call")
        assert events
        assert events[-1]["provider"] == "fake"
        assert events[-1]["status"] == "success"
        assert events[-1]["retries"] == 0
        assert events[-1]["token_count"] == 5

    def test_failure_logs_error_class_without_retrying(self, capture):
        provider = self._FakeProvider(error=ProviderUnavailableError("down", provider="fake"))

        with pytest.raises(ProviderUnavailableError):
            self._retrying(provider).chat([])

        events = _events_by_name(capture, "chat.provider_call")
        assert events
        assert events[-1]["status"] == "error"
        assert events[-1]["error"] == "ProviderUnavailableError"
        assert events[-1]["retries"] == 0

    def test_stream_success_is_logged(self, capture):
        provider = self._FakeProvider()

        list(self._retrying(provider).stream([]))

        events = _events_by_name(capture, "chat.provider_call")
        assert events and events[-1]["status"] == "success"
        assert events[-1]["stream"] is True
