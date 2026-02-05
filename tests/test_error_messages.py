"""Tests for gateway/error_messages.py."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "gateway"))
from error_messages import (
    GENERIC_ERROR_MESSAGES,
    PRIVATE_MODE_HINTS,
    PRIVATE_REPO_ERROR_MESSAGES,
    format_policy_blocked_response,
    get_error_message,
    get_hints_for_error,
)


class TestGetErrorMessage:
    """Tests for get_error_message()."""

    def test_verbose_push_public(self):
        with patch.dict(os.environ, {"VERBOSE_ERRORS": "true"}):
            msg = get_error_message("push_public", repo="owner/repo")
            assert "owner/repo" in msg
            assert "public" in msg.lower()

    def test_verbose_fork_from_public(self):
        with patch.dict(os.environ, {"VERBOSE_ERRORS": "true"}):
            msg = get_error_message("fork_from_public", repo="owner/repo")
            assert "owner/repo" in msg

    def test_verbose_fork_to_public(self):
        with patch.dict(os.environ, {"VERBOSE_ERRORS": "true"}):
            msg = get_error_message("fork_to_public")
            assert "private" in msg.lower()

    def test_verbose_unknown_type_uses_default(self):
        with patch.dict(os.environ, {"VERBOSE_ERRORS": "true"}):
            msg = get_error_message("nonexistent_type")
            assert msg == PRIVATE_REPO_ERROR_MESSAGES["default"]

    def test_non_verbose_returns_generic(self):
        with patch.dict(os.environ, {"VERBOSE_ERRORS": "false"}):
            msg = get_error_message("push_public", repo="owner/repo")
            assert "owner/repo" not in msg
            assert msg == GENERIC_ERROR_MESSAGES["push_public"]

    def test_non_verbose_unknown_type(self):
        with patch.dict(os.environ, {"VERBOSE_ERRORS": "false"}):
            msg = get_error_message("nonexistent_type")
            assert msg == GENERIC_ERROR_MESSAGES["default"]

    def test_visibility_unknown_with_hint(self):
        with patch.dict(os.environ, {"VERBOSE_ERRORS": "true"}):
            msg = get_error_message(
                "visibility_unknown",
                repo="test/repo",
                operation="push",
                hint="Check token permissions",
            )
            assert "test/repo" in msg
            assert "Check token permissions" in msg

    def test_default_repo_when_none(self):
        with patch.dict(os.environ, {"VERBOSE_ERRORS": "true"}):
            msg = get_error_message("push_public")
            assert "unknown" in msg


class TestFormatPolicyBlockedResponse:
    """Tests for format_policy_blocked_response()."""

    def test_basic_response(self):
        resp = format_policy_blocked_response(
            operation="push",
            reason="Not allowed",
        )
        assert resp["success"] is False
        assert resp["error"] == "PolicyViolation"
        assert resp["operation"] == "push"
        assert resp["reason"] == "Not allowed"
        assert resp["policy"] == "private_mode"

    def test_with_repository(self):
        resp = format_policy_blocked_response(
            operation="push",
            reason="Not allowed",
            repository="owner/repo",
        )
        assert resp["repository"] == "owner/repo"

    def test_with_visibility(self):
        resp = format_policy_blocked_response(
            operation="push",
            reason="Not allowed",
            visibility="public",
        )
        assert resp["visibility"] == "public"

    def test_with_hints(self):
        hints = ["Try a private repo", "Contact admin"]
        resp = format_policy_blocked_response(
            operation="push",
            reason="Not allowed",
            hints=hints,
        )
        assert resp["hints"] == hints

    def test_without_optional_fields(self):
        resp = format_policy_blocked_response(
            operation="fetch",
            reason="Blocked",
        )
        assert "repository" not in resp
        assert "visibility" not in resp
        assert "hints" not in resp


class TestGetHintsForError:
    """Tests for get_hints_for_error()."""

    def test_fork_error(self):
        hints = get_hints_for_error("fork_from_public")
        assert hints == PRIVATE_MODE_HINTS["fork_blocked"]

    def test_unknown_error(self):
        hints = get_hints_for_error("visibility_unknown")
        assert hints == PRIVATE_MODE_HINTS["visibility_unknown"]

    def test_public_error(self):
        hints = get_hints_for_error("push_public")
        assert hints == PRIVATE_MODE_HINTS["public_repo"]

    def test_no_matching_hints(self):
        hints = get_hints_for_error("some_other_error")
        assert hints == []
