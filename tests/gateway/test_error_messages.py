"""Tests for gateway error_messages module."""

import sys
from pathlib import Path

import pytest

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from error_messages import (
    GENERIC_ERROR_MESSAGES,
    PRIVATE_MODE_HINTS,
    PRIVATE_REPO_ERROR_MESSAGES,
    format_policy_blocked_response,
    get_error_message,
    get_hints_for_error,
)


class TestIsVerboseErrors:
    """Tests for _is_verbose_errors helper."""

    def test_default_is_verbose(self, monkeypatch):
        """Default is verbose (true)."""
        monkeypatch.delenv("VERBOSE_ERRORS", raising=False)
        from error_messages import _is_verbose_errors

        assert _is_verbose_errors() is True

    def test_explicit_true(self, monkeypatch):
        """Explicit true."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        from error_messages import _is_verbose_errors

        assert _is_verbose_errors() is True

    def test_explicit_false(self, monkeypatch):
        """Explicit false disables verbose."""
        monkeypatch.setenv("VERBOSE_ERRORS", "false")
        from error_messages import _is_verbose_errors

        assert _is_verbose_errors() is False

    def test_value_one(self, monkeypatch):
        """Value '1' enables verbose."""
        monkeypatch.setenv("VERBOSE_ERRORS", "1")
        from error_messages import _is_verbose_errors

        assert _is_verbose_errors() is True

    def test_value_yes(self, monkeypatch):
        """Value 'yes' enables verbose."""
        monkeypatch.setenv("VERBOSE_ERRORS", "yes")
        from error_messages import _is_verbose_errors

        assert _is_verbose_errors() is True

    def test_value_no(self, monkeypatch):
        """Value 'no' disables verbose."""
        monkeypatch.setenv("VERBOSE_ERRORS", "no")
        from error_messages import _is_verbose_errors

        assert _is_verbose_errors() is False


class TestGetErrorMessage:
    """Tests for get_error_message function."""

    def test_verbose_push_public(self, monkeypatch):
        """Verbose message includes repo name."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        msg = get_error_message("push_public", repo="owner/repo")
        assert "owner/repo" in msg
        assert "public repository" in msg

    def test_verbose_fetch_public(self, monkeypatch):
        """Verbose fetch error includes repo."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        msg = get_error_message("fetch_public", repo="owner/repo")
        assert "owner/repo" in msg

    def test_verbose_clone_public(self, monkeypatch):
        """Verbose clone error includes repo."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        msg = get_error_message("clone_public", repo="myorg/myrepo")
        assert "myorg/myrepo" in msg

    def test_verbose_pr_create_public(self, monkeypatch):
        """Verbose PR create error includes repo."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        msg = get_error_message("pr_create_public", repo="owner/repo")
        assert "PR" in msg or "pr" in msg.lower()

    def test_verbose_visibility_unknown(self, monkeypatch):
        """Verbose visibility unknown includes hint."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        msg = get_error_message(
            "visibility_unknown", repo="owner/repo", hint="Check permissions"
        )
        assert "owner/repo" in msg
        assert "Check permissions" in msg

    def test_verbose_default_fallback(self, monkeypatch):
        """Unknown error type uses default message."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        msg = get_error_message("unknown_type_xyz")
        assert "Private Repo Mode" in msg

    def test_non_verbose_returns_generic(self, monkeypatch):
        """Non-verbose mode returns generic messages."""
        monkeypatch.setenv("VERBOSE_ERRORS", "false")
        msg = get_error_message("push_public", repo="secret/repo")
        assert "secret/repo" not in msg
        assert "blocked by policy" in msg.lower()

    def test_non_verbose_default(self, monkeypatch):
        """Non-verbose unknown type uses generic default."""
        monkeypatch.setenv("VERBOSE_ERRORS", "false")
        msg = get_error_message("unknown_type")
        assert "Private Repo Mode" in msg

    def test_verbose_fork_from_public(self, monkeypatch):
        """Fork from public error message."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        msg = get_error_message("fork_from_public", repo="public/repo")
        assert "public/repo" in msg
        assert "fork" in msg.lower()

    def test_verbose_fork_to_public(self, monkeypatch):
        """Fork to public error message."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        msg = get_error_message("fork_to_public")
        assert "private" in msg.lower()

    def test_verbose_gh_execute_public(self, monkeypatch):
        """GH execute on public repo error."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        msg = get_error_message("gh_execute_public", repo="owner/pub")
        assert "owner/pub" in msg

    def test_missing_repo_uses_unknown(self, monkeypatch):
        """Missing repo kwarg uses 'unknown' placeholder."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        msg = get_error_message("push_public")
        assert "unknown" in msg

    def test_verbose_issue_public(self, monkeypatch):
        """Issue operation on public repo."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        msg = get_error_message("issue_public", repo="org/repo")
        assert "org/repo" in msg

    def test_verbose_pr_comment_public(self, monkeypatch):
        """PR comment on public repo."""
        monkeypatch.setenv("VERBOSE_ERRORS", "true")
        msg = get_error_message("pr_comment_public", repo="org/repo")
        assert "org/repo" in msg


class TestFormatPolicyBlockedResponse:
    """Tests for format_policy_blocked_response function."""

    def test_minimal_response(self):
        """Minimal blocked response."""
        resp = format_policy_blocked_response(
            operation="push",
            reason="Not allowed",
        )
        assert resp["success"] is False
        assert resp["error"] == "PolicyViolation"
        assert resp["operation"] == "push"
        assert resp["reason"] == "Not allowed"
        assert resp["policy"] == "private_mode"
        assert "repository" not in resp

    def test_with_repository(self):
        """Response with repository."""
        resp = format_policy_blocked_response(
            operation="push",
            reason="Public repo",
            repository="owner/repo",
        )
        assert resp["repository"] == "owner/repo"

    def test_with_visibility(self):
        """Response with visibility."""
        resp = format_policy_blocked_response(
            operation="fetch",
            reason="blocked",
            visibility="public",
        )
        assert resp["visibility"] == "public"

    def test_with_hints(self):
        """Response with hints list."""
        hints = ["Try a private repo", "Contact admin"]
        resp = format_policy_blocked_response(
            operation="clone",
            reason="blocked",
            hints=hints,
        )
        assert resp["hints"] == hints

    def test_full_response(self):
        """Full response with all fields."""
        resp = format_policy_blocked_response(
            operation="push",
            reason="Public repo blocked",
            repository="org/public-repo",
            visibility="public",
            hints=["Use private repo"],
        )
        assert resp["success"] is False
        assert resp["repository"] == "org/public-repo"
        assert resp["visibility"] == "public"
        assert len(resp["hints"]) == 1


class TestGetHintsForError:
    """Tests for get_hints_for_error function."""

    def test_fork_hints(self):
        """Fork errors get fork hints."""
        hints = get_hints_for_error("fork_from_public")
        assert len(hints) > 0
        assert any("fork" in h.lower() for h in hints)

    def test_unknown_hints(self):
        """Unknown visibility errors get appropriate hints."""
        hints = get_hints_for_error("visibility_unknown")
        assert len(hints) > 0
        assert any("visibility" in h.lower() for h in hints)

    def test_public_hints(self):
        """Public repo errors get public hints."""
        hints = get_hints_for_error("push_public")
        assert len(hints) > 0
        assert any("private" in h.lower() for h in hints)

    def test_no_matching_hints(self):
        """Unrecognized error type returns empty list."""
        hints = get_hints_for_error("completely_unrelated")
        assert hints == []

    def test_fork_to_public_hints(self):
        """Fork-to-public gets fork hints."""
        hints = get_hints_for_error("fork_to_public")
        assert len(hints) > 0


class TestErrorMessageConstants:
    """Tests for error message constant dictionaries."""

    def test_generic_has_default(self):
        """Generic messages have a default key."""
        assert "default" in GENERIC_ERROR_MESSAGES

    def test_verbose_has_default(self):
        """Verbose messages have a default key."""
        assert "default" in PRIVATE_REPO_ERROR_MESSAGES

    def test_all_generic_keys_in_verbose(self):
        """All generic keys have verbose counterparts."""
        for key in GENERIC_ERROR_MESSAGES:
            assert key in PRIVATE_REPO_ERROR_MESSAGES, f"Missing verbose template for '{key}'"

    def test_hints_have_expected_categories(self):
        """PRIVATE_MODE_HINTS has expected categories."""
        assert "public_repo" in PRIVATE_MODE_HINTS
        assert "visibility_unknown" in PRIVATE_MODE_HINTS
        assert "fork_blocked" in PRIVATE_MODE_HINTS
