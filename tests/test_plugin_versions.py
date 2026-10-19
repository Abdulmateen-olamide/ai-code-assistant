"""Tests for the PEP 440 version comparison helpers (#399).

The helpers back update and compatibility checks, so the tests cover the
ordering rules that are easy to get wrong (release padding, prereleases,
post/dev releases, epochs, and local versions), invalid input handling, and
stable sorting.
"""

import pytest
from packaging.version import Version

from app.services.plugin_versions import (
    EQUAL,
    NEWER,
    OLDER,
    InvalidPluginVersionError,
    compare_versions,
    is_newer,
    is_valid_version,
    latest_version,
    normalize_version,
    parse_version,
    sort_versions,
)


class TestParseVersion:
    @pytest.mark.parametrize(
        "value",
        ["1", "1.0", "v1.0", "1.2.3", " 2.1.0 ", "1.0.0rc1", "1.0.post1", "1!2.0", "1.0+local"],
    )
    def test_accepts_valid_versions(self, value):
        assert isinstance(parse_version(value), Version)

    def test_accepts_an_existing_version(self):
        version = Version("1.2.3")
        assert parse_version(version) is version

    @pytest.mark.parametrize(
        "value",
        ["", "   ", "not-a-version", "1.0.0-", None, 123, object()],
    )
    def test_rejects_invalid_versions(self, value):
        with pytest.raises(InvalidPluginVersionError):
            parse_version(value)

    def test_invalid_error_has_a_clear_message(self):
        with pytest.raises(InvalidPluginVersionError, match="not-a-version"):
            parse_version("not-a-version")


class TestValidation:
    @pytest.mark.parametrize("value", ["1.0", "1.0.0rc1", Version("2.0")])
    def test_valid(self, value):
        assert is_valid_version(value) is True

    @pytest.mark.parametrize("value", ["", "nope", None, 1.0, []])
    def test_invalid(self, value):
        assert is_valid_version(value) is False


class TestNormalizeVersion:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1.0", "1.0"),
            ("v1.0", "1.0"),
            (" 1.0.0 ", "1.0.0"),
            ("01.002.0003", "1.2.3"),
            ("1.0.0RC1", "1.0.0rc1"),
            ("1.0.0+ABC", "1.0.0+abc"),
        ],
    )
    def test_normalizes(self, value, expected):
        assert normalize_version(value) == expected


class TestCompareVersions:
    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            ("1.0.0", "1.0.0", EQUAL),
            ("1.0", "1.0.0", EQUAL),  # missing release segments are zero
            ("1.0.1", "1.0.0", NEWER),
            ("0.9.9", "1.0.0", OLDER),
            ("1.0.0rc1", "1.0.0", OLDER),  # prerelease sorts before release
            ("1.0.0rc2", "1.0.0rc1", NEWER),
            ("1.0.0.post1", "1.0.0", NEWER),  # postrelease sorts after release
            ("1.0.0.dev1", "1.0.0a1", OLDER),  # dev sorts before pre-release
            ("1!1.0", "2.0", NEWER),  # epoch dominates
            ("1.0+abc", "1.0", NEWER),  # local version sorts after plain
            ("1.0+abc", "1.0+abd", OLDER),
        ],
    )
    def test_ordering(self, left, right, expected):
        assert compare_versions(left, right) == expected

    def test_compare_is_antisymmetric(self):
        left, right = "1.2.0", "1.10.0"
        assert compare_versions(left, right) == -compare_versions(right, left)

    def test_invalid_operand_raises(self):
        with pytest.raises(InvalidPluginVersionError):
            compare_versions("1.0", "bogus")


class TestIsNewer:
    def test_newer(self):
        assert is_newer("1.0.1", "1.0.0") is True

    def test_equal_is_not_newer(self):
        assert is_newer("1.0", "1.0.0") is False

    def test_older_is_not_newer(self):
        assert is_newer("0.9.0", "1.0.0") is False


class TestSortVersions:
    def test_sorts_ascending(self):
        assert sort_versions(["2.0.0", "1.0.0", "1.5.0"]) == ["1.0.0", "1.5.0", "2.0.0"]

    def test_sorts_with_prereleases(self):
        assert sort_versions(["1.0.0", "1.0.0rc1", "0.9.0"]) == [
            "0.9.0",
            "1.0.0rc1",
            "1.0.0",
        ]

    def test_reverse(self):
        assert sort_versions(["1.0.0", "2.0.0"], reverse=True) == ["2.0.0", "1.0.0"]

    def test_stable_for_equal_versions(self):
        assert sort_versions(["1.0.0", "1.0"]) == ["1.0.0", "1.0"]

    def test_preserves_input_representation(self):
        assert sort_versions([" 2.0 ", "1.0"]) == ["1.0", " 2.0 "]

    def test_empty(self):
        assert sort_versions([]) == []

    def test_invalid_value_raises(self):
        with pytest.raises(InvalidPluginVersionError):
            sort_versions(["1.0", "nope"])


class TestLatestVersion:
    def test_latest(self):
        assert latest_version(["0.9.0", "1.2.0", "1.10.0"]) == "1.10.0"

    def test_empty_returns_none(self):
        assert latest_version([]) is None

    def test_tie_keeps_first(self):
        assert latest_version(["1.0.0", "1.0"]) == "1.0.0"

    def test_invalid_value_raises(self):
        with pytest.raises(InvalidPluginVersionError):
            latest_version(["1.0", "bad"])


class TestCompatibilityUsesTheHelpers:
    def test_invalid_app_version_fails_closed(self):
        from app.services.plugin_compat import plugin_compatible

        assert plugin_compatible(">=1.0.0", "not-a-version") is False

    def test_version_comparison_is_pep440(self):
        from app.services.plugin_compat import plugin_compatible

        assert plugin_compatible(">=1.10.0", "1.9.9") is False
        assert plugin_compatible(">=1.10.0", "1.11.0") is True
