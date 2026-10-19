"""Tests for deterministic plugin lockfile serialization (#398).

The lockfile is meant to be diffed and committed, so the important properties
are byte stability across runs/input order, sorted entries, normalized hashes,
and the absence of volatile fields such as per-resolution timestamps.
"""

import json
from dataclasses import dataclass

import pytest

from app.services.plugin_lockfile import (
    LOCKFILE_VERSION,
    LockfileEntry,
    LockfileError,
    build_lockfile,
    dumps_lockfile,
    normalize_integrity,
    write_lockfile,
)

HASH_A = "a" * 64
HASH_B = "B" * 64


def _entry(plugin_id, version="1.0.0", source="catalog", integrity=HASH_A, dependencies=()):
    return LockfileEntry(
        plugin_id=plugin_id,
        version=version,
        source=source,
        integrity=integrity,
        dependencies=tuple(dependencies or ()),
    )


class TestNormalizeIntegrity:
    @pytest.mark.parametrize("prefix", ["", "sha256:", "sha256-", "SHA256:"])
    def test_accepts_common_spellings(self, prefix):
        assert normalize_integrity(prefix + HASH_A.upper()) == f"sha256:{HASH_A}"

    def test_strips_whitespace(self):
        assert normalize_integrity(f"  sha256:{HASH_A}  ") == f"sha256:{HASH_A}"

    @pytest.mark.parametrize(
        "value",
        ["", "a" * 63, "a" * 65, "g" * 64, "sha512:" + "a" * 128, None, 123],
    )
    def test_rejects_invalid_hashes(self, value):
        with pytest.raises(LockfileError):
            normalize_integrity(value)

    def test_names_an_unsupported_algorithm(self):
        with pytest.raises(LockfileError, match="sha512"):
            normalize_integrity("sha512:" + "a" * 128)


class TestBuildLockfile:
    def test_sorts_entries_by_plugin_id(self):
        document = build_lockfile([_entry("zebra"), _entry("alpha"), _entry("middle")])
        assert [entry["plugin_id"] for entry in document["plugins"]] == [
            "alpha",
            "middle",
            "zebra",
        ]
        assert document["lockfile_version"] == LOCKFILE_VERSION

    def test_normalizes_versions_and_hashes(self):
        document = build_lockfile(
            [_entry("plugin", version=" 1.0.0RC1 ", integrity=HASH_B.lower())]
        )
        entry = document["plugins"][0]
        assert entry["version"] == "1.0.0rc1"
        assert entry["integrity"] == f"sha256:{HASH_B.lower()}"

    def test_sorts_and_trims_dependencies(self):
        document = build_lockfile([_entry("plugin", dependencies=(" z>=1 ", "a>=2"))])
        assert document["plugins"][0]["dependencies"] == ["a>=2", "z>=1"]

    def test_tolerates_none_dependencies(self):
        document = build_lockfile([_entry("plugin", dependencies=None)])
        assert document["plugins"][0]["dependencies"] == []

    def test_accepts_mappings_and_aliases(self):
        document = build_lockfile(
            [
                {
                    "plugin_id": "plugin",
                    "version": "2.0",
                    "source": "https://example.test/catalog",
                    "integrity_hash": HASH_B,
                    "dependencies": ("dep>=1",),
                    "resolved_at": "2026-10-17T00:00:00Z",
                }
            ]
        )
        entry = document["plugins"][0]
        assert entry["integrity"] == f"sha256:{HASH_B.lower()}"
        assert "resolved_at" not in entry  # volatile data must not be serialized

    def test_accepts_objects_with_attributes(self):
        @dataclass
        class Row:
            plugin_id: str
            version: str
            source: str
            integrity: str
            dependencies: tuple[str, ...]

        document = build_lockfile([Row("plugin", "1.0", "catalog", HASH_A, ("dep>=1",))])
        assert document["plugins"][0]["plugin_id"] == "plugin"

    def test_ignores_volatile_resolved_at_on_entries(self):
        entry = LockfileEntry(
            plugin_id="plugin",
            version="1.0.0",
            source="catalog",
            integrity=HASH_A,
            dependencies=(),
        )
        assert "resolved_at" not in entry.to_dict()

    def test_duplicate_plugin_ids_are_refused(self):
        with pytest.raises(LockfileError, match="Duplicate"):
            build_lockfile([_entry("plugin"), _entry("plugin", version="2.0.0")])

    @pytest.mark.parametrize(
        "entry",
        [
            {"version": "1.0", "source": "catalog", "integrity": HASH_A},
            {"plugin_id": "p", "source": "catalog", "integrity": HASH_A},
            {"plugin_id": "p", "version": "1.0", "integrity": HASH_A},
            {"plugin_id": "p", "version": "1.0", "source": "catalog"},
        ],
    )
    def test_missing_required_fields_are_refused(self, entry):
        with pytest.raises(LockfileError):
            build_lockfile([entry])

    def test_invalid_version_is_refused(self):
        with pytest.raises(LockfileError, match="version"):
            build_lockfile([_entry("plugin", version="not-a-version")])

    def test_empty_input(self):
        assert build_lockfile([]) == {"lockfile_version": LOCKFILE_VERSION, "plugins": []}


class TestDumpsLockfile:
    def test_output_is_stable_across_input_order(self):
        entries = [_entry("b"), _entry("a", version="2.0.0")]
        first = dumps_lockfile(entries)
        second = dumps_lockfile(list(reversed(entries)))
        assert first == second

    def test_output_is_stable_across_repeated_runs(self):
        entries = [_entry("a"), _entry("b")]
        assert len({dumps_lockfile(entries) for _ in range(5)}) == 1

    def test_ends_with_exactly_one_newline(self):
        output = dumps_lockfile([_entry("a")])
        assert output.endswith("\n")
        assert not output.endswith("\n\n")

    def test_object_keys_are_sorted(self):
        output = dumps_lockfile([_entry("a")])
        assert output.index('"lockfile_version"') < output.index('"plugins"')
        entry = json.loads(output)["plugins"][0]
        assert list(entry) == ["dependencies", "integrity", "plugin_id", "source", "version"]

    def test_keeps_unicode(self):
        output = dumps_lockfile([_entry("plugin", source="catalogue-é")])
        assert "catalogue-é" in output
        assert "\\u00e9" not in output


class TestWriteLockfile:
    def test_writes_canonical_bytes_and_newlines(self, tmp_path):
        destination = tmp_path / "plugins.lock.json"
        write_lockfile(destination, [_entry("b"), _entry("a")])

        contents = destination.read_bytes()
        assert contents == dumps_lockfile([_entry("a"), _entry("b")]).encode("utf-8")
        assert b"\r\n" not in contents

    def test_creates_parent_directories(self, tmp_path):
        destination = tmp_path / "nested" / "dir" / "plugins.lock.json"
        write_lockfile(destination, [_entry("a")])
        assert destination.is_file()

    def test_leaves_no_temporary_files(self, tmp_path):
        destination = tmp_path / "plugins.lock.json"
        write_lockfile(destination, [_entry("a")])
        assert list(tmp_path.glob(".plugins.lock.json.*")) == []
