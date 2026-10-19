"""Tests for plugin artifact sha256 verification (#384).

Covers hashing bytes/files/streams, accepting the catalog digest spellings,
constant-time match/mismatch handling, refusing registration on a mismatch, and
recording the refused install in the plugin audit trail.
"""

import hashlib
import io

import pytest

from app.extensions import db
from app.models import ActivityEvent, Workspace
from app.models.activity_event import EVENT_PLUGIN_DENIED
from app.services.plugin_artifacts import (
    ArtifactError,
    ArtifactIntegrityError,
    compute_sha256,
    normalize_sha256,
    verify_and_register_artifact,
    verify_artifact_integrity,
)

PAYLOAD = b"the plugin artifact bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
OTHER_DIGEST = hashlib.sha256(b"tampered").hexdigest()


class TestNormalizeSha256:
    @pytest.mark.parametrize(
        "value",
        [DIGEST, DIGEST.upper(), f"sha256:{DIGEST}", f"sha256-{DIGEST}", f"SHA256:{DIGEST}"],
    )
    def test_accepts_catalog_spellings(self, value):
        assert normalize_sha256(value) == DIGEST

    def test_strips_whitespace(self):
        assert normalize_sha256(f"  sha256:{DIGEST}  ") == DIGEST

    @pytest.mark.parametrize(
        "value",
        ["", "a" * 63, "a" * 65, "g" * 64, "sha512:" + "a" * 128, None, 123],
    )
    def test_rejects_invalid_digests(self, value):
        with pytest.raises(ArtifactError):
            normalize_sha256(value)

    def test_names_an_unsupported_algorithm(self):
        with pytest.raises(ArtifactError, match="sha512"):
            normalize_sha256("sha512:" + "a" * 128)


class TestComputeSha256:
    def test_hashes_bytes(self):
        assert compute_sha256(PAYLOAD) == DIGEST

    def test_hashes_empty_bytes(self):
        assert compute_sha256(b"") == hashlib.sha256(b"").hexdigest()

    def test_hashes_a_file_path(self, tmp_path):
        artifact = tmp_path / "plugin.zip"
        artifact.write_bytes(PAYLOAD)
        assert compute_sha256(artifact) == DIGEST
        assert compute_sha256(str(artifact)) == DIGEST

    def test_hashes_a_binary_stream(self):
        assert compute_sha256(io.BytesIO(PAYLOAD)) == DIGEST

    def test_streaming_chunk_size_does_not_change_the_digest(self):
        assert compute_sha256(PAYLOAD, chunk_size=1) == DIGEST
        assert compute_sha256(io.BytesIO(PAYLOAD), chunk_size=2) == DIGEST

    def test_missing_file_is_an_artifact_error(self, tmp_path):
        with pytest.raises(ArtifactError, match="Cannot read artifact file"):
            compute_sha256(tmp_path / "missing.zip")

    def test_rejects_invalid_chunk_size(self):
        with pytest.raises(ArtifactError, match="chunk size"):
            compute_sha256(PAYLOAD, chunk_size=0)

    def test_rejects_text_streams(self):
        with pytest.raises(ArtifactError, match="binary"):
            compute_sha256(io.StringIO("not bytes"))

    def test_rejects_unsupported_sources(self):
        with pytest.raises(ArtifactError, match="Unsupported artifact source"):
            compute_sha256(12345)


class TestVerifyArtifactIntegrity:
    def test_match_returns_the_digest(self):
        assert verify_artifact_integrity(PAYLOAD, DIGEST) == DIGEST
        assert verify_artifact_integrity(PAYLOAD, f"sha256:{DIGEST.upper()}") == DIGEST

    def test_mismatch_raises_with_both_digests(self):
        with pytest.raises(ArtifactIntegrityError) as excinfo:
            verify_artifact_integrity(PAYLOAD, OTHER_DIGEST, plugin_id="evil-plugin")

        error = excinfo.value
        assert error.expected == OTHER_DIGEST
        assert error.actual == DIGEST
        assert error.plugin_id == "evil-plugin"
        assert OTHER_DIGEST in str(error)
        assert DIGEST in str(error)
        assert "evil-plugin" in str(error)

    def test_mismatch_without_workspace_does_not_need_an_app_context(self):
        with pytest.raises(ArtifactIntegrityError):
            verify_artifact_integrity(PAYLOAD, OTHER_DIGEST, workspace_id=None)

    def test_match_does_not_record_an_audit_event(self, app, make_user):
        user = make_user()
        workspace = Workspace(user_id=user.id, name="Artifacts")
        db.session.add(workspace)
        db.session.commit()

        verify_artifact_integrity(
            PAYLOAD,
            DIGEST,
            plugin_id="safe-plugin",
            workspace_id=workspace.id,
            actor=user,
        )
        db.session.flush()

        assert (
            ActivityEvent.query.filter_by(
                workspace_id=workspace.id, event_type=EVENT_PLUGIN_DENIED
            ).count()
            == 0
        )


class TestVerifyAndRegisterArtifact:
    def test_registers_only_after_a_match(self):
        calls = []

        def register(digest):
            calls.append(digest)
            return "registered"

        assert (
            verify_and_register_artifact(PAYLOAD, DIGEST, register, plugin_id="safe-plugin")
            == "registered"
        )
        assert calls == [DIGEST]

    def test_does_not_register_a_tampered_artifact(self):
        calls = []

        def register(digest):  # pragma: no cover - must never run
            calls.append(digest)
            return "registered"

        with pytest.raises(ArtifactIntegrityError):
            verify_and_register_artifact(PAYLOAD, OTHER_DIGEST, register, plugin_id="evil-plugin")

        assert calls == []

    def test_propagates_registration_errors_after_verification(self):
        def register(digest):
            raise RuntimeError("registration failed")

        with pytest.raises(RuntimeError, match="registration failed"):
            verify_and_register_artifact(PAYLOAD, DIGEST, register)


class TestMismatchAudit:
    def test_mismatch_records_a_bounded_audit_event(self, app, make_user):
        user = make_user()
        workspace = Workspace(user_id=user.id, name="Artifacts")
        db.session.add(workspace)
        db.session.commit()

        with pytest.raises(ArtifactIntegrityError):
            verify_artifact_integrity(
                PAYLOAD,
                OTHER_DIGEST,
                plugin_id="evil-plugin",
                workspace_id=workspace.id,
                actor=user,
            )

        db.session.flush()
        event = ActivityEvent.query.filter_by(
            workspace_id=workspace.id, event_type=EVENT_PLUGIN_DENIED
        ).one()
        assert event.actor_id == user.id
        assert event.event_metadata["plugin_id"] == "evil-plugin"
        assert event.event_metadata["action"] == "denied"
        assert event.event_metadata["outcome"] == "denied"
        assert event.event_metadata["reason"] == "artifact integrity mismatch"
        # No artifact contents or digests are written to the audit trail.
        serialized = str(event.event_metadata)
        assert DIGEST not in serialized
        assert OTHER_DIGEST not in serialized
