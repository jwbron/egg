"""Tests for gateway/error_messages.py."""

import os
from unittest.mock import patch

from gateway.error_messages import (
    GENERIC_ERROR_MESSAGES,
    PRIVATE_MODE_HINTS,
    PRIVATE_REPO_ERROR_MESSAGES,
    _is_verbose_errors,
    format_policy_blocked_response,
    get_error_message,
    get_hints_for_error,
)


class TestIsVerboseErrors:
    """Tests for _is_verbose_errors function."""

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "true"})
    def test_verbose_true(self):
        """Test returns True when VERBOSE_ERRORS=true."""
        assert _is_verbose_errors() is True

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "TRUE"})
    def test_verbose_true_uppercase(self):
        """Test returns True when VERBOSE_ERRORS=TRUE."""
        assert _is_verbose_errors() is True

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "1"})
    def test_verbose_one(self):
        """Test returns True when VERBOSE_ERRORS=1."""
        assert _is_verbose_errors() is True

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "yes"})
    def test_verbose_yes(self):
        """Test returns True when VERBOSE_ERRORS=yes."""
        assert _is_verbose_errors() is True

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "false"})
    def test_verbose_false(self):
        """Test returns False when VERBOSE_ERRORS=false."""
        assert _is_verbose_errors() is False

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "0"})
    def test_verbose_zero(self):
        """Test returns False when VERBOSE_ERRORS=0."""
        assert _is_verbose_errors() is False

    @patch.dict(os.environ, {}, clear=False)
    def test_verbose_default_true(self):
        """Test defaults to True when not set."""
        # Remove if exists
        env_backup = os.environ.get("VERBOSE_ERRORS")
        if "VERBOSE_ERRORS" in os.environ:
            del os.environ["VERBOSE_ERRORS"]
        try:
            assert _is_verbose_errors() is True
        finally:
            if env_backup is not None:
                os.environ["VERBOSE_ERRORS"] = env_backup

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "  true  "})
    def test_verbose_whitespace(self):
        """Test handles whitespace."""
        assert _is_verbose_errors() is True


class TestGetErrorMessage:
    """Tests for get_error_message function."""

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "true"})
    def test_verbose_message_with_repo(self):
        """Test verbose message includes repo name."""
        msg = get_error_message("push_public", repo="owner/repo")
        assert "owner/repo" in msg
        assert "public repository" in msg or "public" in msg.lower()

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "false"})
    def test_generic_message_hides_repo(self):
        """Test generic message does not include repo name."""
        msg = get_error_message("push_public", repo="owner/repo")
        assert "owner/repo" not in msg
        assert "blocked by policy" in msg.lower()

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "true"})
    def test_visibility_unknown_message(self):
        """Test visibility unknown error message."""
        msg = get_error_message(
            "visibility_unknown",
            repo="owner/repo",
            hint="Check your token permissions",
        )
        assert "owner/repo" in msg
        assert "visibility" in msg.lower()
        assert "Check your token permissions" in msg

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "true"})
    def test_fork_from_public_message(self):
        """Test fork from public error message."""
        msg = get_error_message("fork_from_public", repo="public/repo")
        assert "public/repo" in msg
        assert "fork" in msg.lower()

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "true"})
    def test_fork_to_public_message(self):
        """Test fork to public error message."""
        msg = get_error_message("fork_to_public")
        assert "public fork" in msg.lower() or "private" in msg.lower()

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "true"})
    def test_default_message_for_unknown_type(self):
        """Test default message for unknown error type."""
        msg = get_error_message("unknown_error_type")
        assert "blocked" in msg.lower() and "policy" in msg.lower()

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "false"})
    def test_generic_default_message(self):
        """Test generic default for unknown type in non-verbose mode."""
        msg = get_error_message("unknown_error_type")
        assert msg == GENERIC_ERROR_MESSAGES["default"]

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "true"})
    def test_message_with_kwargs(self):
        """Test that additional kwargs are substituted."""
        # Use visibility_unknown which has {hint} placeholder
        msg = get_error_message(
            "visibility_unknown",
            repo="test/repo",
            hint="Custom hint",
        )
        assert "Custom hint" in msg

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "true"})
    def test_message_with_missing_placeholder(self):
        """Test graceful handling of missing placeholder."""
        # If template has placeholder but kwarg not provided
        msg = get_error_message("visibility_unknown", repo="test/repo")
        # Should not crash, just have empty hint
        assert "test/repo" in msg

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "true"})
    @patch(
        "gateway.error_messages.PRIVATE_REPO_ERROR_MESSAGES",
        {
            "test_error": "Test {unknown_placeholder} message",
            "default": "Default message",
        },
    )
    def test_message_with_unknown_placeholder_returns_default(self):
        """Test that unknown placeholder in template returns default message."""
        msg = get_error_message("test_error", repo="test/repo")
        # Should return default message when KeyError occurs
        assert msg == "Default message"

    @patch.dict(os.environ, {"VERBOSE_ERRORS": "true"})
    def test_all_error_types_have_messages(self):
        """Test that all generic error types have verbose equivalents."""
        for error_type in GENERIC_ERROR_MESSAGES:
            if error_type != "default":
                msg = get_error_message(error_type, repo="owner/repo")
                assert len(msg) > 0


class TestFormatPolicyBlockedResponse:
    """Tests for format_policy_blocked_response function."""

    def test_minimal_response(self):
        """Test response with only required fields."""
        response = format_policy_blocked_response(
            operation="push",
            reason="Repository is public",
        )
        assert response == {
            "success": False,
            "error": "PolicyViolation",
            "operation": "push",
            "reason": "Repository is public",
            "policy": "private_mode",
        }

    def test_response_with_repository(self):
        """Test response includes repository when provided."""
        response = format_policy_blocked_response(
            operation="push",
            reason="Repository is public",
            repository="owner/repo",
        )
        assert response["repository"] == "owner/repo"

    def test_response_with_visibility(self):
        """Test response includes visibility when provided."""
        response = format_policy_blocked_response(
            operation="push",
            reason="Repository is public",
            visibility="public",
        )
        assert response["visibility"] == "public"

    def test_response_with_hints(self):
        """Test response includes hints when provided."""
        hints = ["Hint 1", "Hint 2"]
        response = format_policy_blocked_response(
            operation="push",
            reason="Repository is public",
            hints=hints,
        )
        assert response["hints"] == hints

    def test_response_with_all_fields(self):
        """Test response with all optional fields."""
        response = format_policy_blocked_response(
            operation="fork",
            reason="Cannot fork public repo",
            repository="public/repo",
            visibility="public",
            hints=["Use private repo", "Create private fork"],
        )
        assert response["success"] is False
        assert response["error"] == "PolicyViolation"
        assert response["operation"] == "fork"
        assert response["reason"] == "Cannot fork public repo"
        assert response["policy"] == "private_mode"
        assert response["repository"] == "public/repo"
        assert response["visibility"] == "public"
        assert len(response["hints"]) == 2


class TestGetHintsForError:
    """Tests for get_hints_for_error function."""

    def test_fork_error_hints(self):
        """Test hints for fork-related errors."""
        hints = get_hints_for_error("fork_from_public")
        assert hints == PRIVATE_MODE_HINTS["fork_blocked"]
        assert len(hints) > 0

    def test_fork_to_public_hints(self):
        """Test hints for fork_to_public."""
        hints = get_hints_for_error("fork_to_public")
        assert hints == PRIVATE_MODE_HINTS["fork_blocked"]

    def test_unknown_visibility_hints(self):
        """Test hints for unknown visibility errors."""
        hints = get_hints_for_error("visibility_unknown")
        assert hints == PRIVATE_MODE_HINTS["visibility_unknown"]

    def test_public_repo_hints(self):
        """Test hints for public repo errors."""
        hints = get_hints_for_error("push_public")
        assert hints == PRIVATE_MODE_HINTS["public_repo"]

    def test_fetch_public_hints(self):
        """Test hints for fetch_public errors."""
        hints = get_hints_for_error("fetch_public")
        assert hints == PRIVATE_MODE_HINTS["public_repo"]

    def test_unrecognized_error_no_hints(self):
        """Test that unrecognized errors return empty hints."""
        hints = get_hints_for_error("some_other_error")
        assert hints == []


class TestErrorMessageTemplates:
    """Tests for error message template structure."""

    def test_all_verbose_messages_are_strings(self):
        """Test that all verbose messages are strings."""
        for key, msg in PRIVATE_REPO_ERROR_MESSAGES.items():
            assert isinstance(msg, str), f"{key} is not a string"

    def test_all_generic_messages_are_strings(self):
        """Test that all generic messages are strings."""
        for key, msg in GENERIC_ERROR_MESSAGES.items():
            assert isinstance(msg, str), f"{key} is not a string"

    def test_verbose_messages_have_content(self):
        """Test that verbose messages are not empty."""
        for key, msg in PRIVATE_REPO_ERROR_MESSAGES.items():
            assert len(msg) > 10, f"{key} is too short"

    def test_generic_messages_dont_reveal_info(self):
        """Test that generic messages don't reveal sensitive info."""
        for key, msg in GENERIC_ERROR_MESSAGES.items():
            # Generic messages should not contain placeholders
            assert "{" not in msg, f"{key} contains placeholder"
            assert "}" not in msg, f"{key} contains placeholder"
