"""Tests for ``egg_overseer.infra_error`` (issue #1962)."""

from __future__ import annotations

import pytest
from egg_overseer.infra_error import classify_infra_error, is_infra_error


class TestIsInfraError:
    @pytest.mark.parametrize(
        "text",
        [
            "API rate limit exceeded for installation X",
            "secondary rate limit reached",
            "abuse detection triggered",
            "OOMKilled by kernel",
            "out of memory: kill process 1234",
            "fork: cannot allocate memory",
            "Temporary failure in name resolution",
            "could not resolve host: example.com",
            "Connection refused",
            "Connection reset by peer",
            "i/o timeout while contacting api.github.com",
        ],
    )
    def test_known_infra_patterns_classified(self, text: str) -> None:
        assert is_infra_error(text) is True

    def test_empty_input_is_not_infra(self) -> None:
        assert is_infra_error("") is False

    def test_none_safe(self) -> None:
        assert is_infra_error(None) is False  # type: ignore[arg-type]

    def test_non_matching_text(self) -> None:
        assert is_infra_error("Implementation has a real bug") is False


class TestClassifyInfraError:
    @pytest.mark.parametrize(
        "text, expected_kind",
        [
            ("API rate limit exceeded", "gh-rate-limit"),
            ("secondary rate limit", "gh-secondary-rate-limit"),
            ("abuse detection", "gh-abuse-detection"),
            ("OOMKilled", "container-oom"),
            ("Out Of Memory", "container-oom"),
            ("cannot allocate memory", "container-oom"),
            ("Temporary failure in name resolution", "network-dns"),
            ("could not resolve host", "network-dns"),
            ("connection refused", "network-conn"),
            ("connection reset by peer", "network-conn"),
            ("i/o timeout", "network-timeout"),
        ],
    )
    def test_known_pattern_classification(self, text: str, expected_kind: str) -> None:
        assert classify_infra_error(text) == expected_kind

    def test_no_match_returns_none(self) -> None:
        assert classify_infra_error("Nothing infra-flavoured here.") is None

    def test_empty_returns_none(self) -> None:
        assert classify_infra_error("") is None

    def test_none_returns_none(self) -> None:
        assert classify_infra_error(None) is None  # type: ignore[arg-type]

    def test_first_match_wins_when_multiple_present(self) -> None:
        # Patterns are checked in order. With "API rate limit exceeded"
        # AND "OOMKilled" both present, the gh-rate-limit pattern
        # matches first.
        text = "OOMKilled because API rate limit exceeded"
        assert classify_infra_error(text) == "gh-rate-limit"
