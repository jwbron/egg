"""Tests for egg_harness.permissions — permission callback utilities."""

from __future__ import annotations

from unittest.mock import MagicMock

from egg_harness.permissions import compose_permissions, create_disallow_list_callback


class TestCreateDisallowListCallback:
    """Tests for create_disallow_list_callback()."""

    def test_blocked_tool_returns_error_string(self):
        cb = create_disallow_list_callback(["WebFetch"])
        result = cb("WebFetch", {"url": "http://example.com"})
        assert result is not None
        assert isinstance(result, str)

    def test_allowed_tool_returns_none(self):
        cb = create_disallow_list_callback(["WebFetch"])
        assert cb("Bash", {"command": "ls"}) is None

    def test_empty_disallow_list_allows_everything(self):
        cb = create_disallow_list_callback([])
        assert cb("WebFetch", {}) is None
        assert cb("Bash", {}) is None

    def test_error_message_includes_tool_name(self):
        cb = create_disallow_list_callback(["WebSearch"])
        result = cb("WebSearch", {})
        assert "WebSearch" in result

    def test_case_sensitive_matching(self):
        cb = create_disallow_list_callback(["WebFetch"])
        assert cb("webfetch", {}) is None  # lowercase not blocked
        assert cb("WebFetch", {}) is not None

    def test_multiple_blocked_tools(self):
        cb = create_disallow_list_callback(["WebFetch", "WebSearch", "Bash"])
        assert cb("WebFetch", {}) is not None
        assert cb("WebSearch", {}) is not None
        assert cb("Bash", {}) is not None
        assert cb("Read", {}) is None


class TestComposePermissions:
    """Tests for compose_permissions()."""

    def test_first_blocker_wins(self):
        cb1 = create_disallow_list_callback(["Bash"])
        cb2 = create_disallow_list_callback(["Read"])
        composed = compose_permissions(cb1, cb2)
        result = composed("Bash", {})
        assert result is not None
        assert "Bash" in result

    def test_second_callback_not_called_when_first_blocks(self):
        cb1 = create_disallow_list_callback(["Bash"])
        cb2 = MagicMock(return_value=None)
        composed = compose_permissions(cb1, cb2)
        composed("Bash", {})
        cb2.assert_not_called()

    def test_all_allow_returns_none(self):
        cb1 = create_disallow_list_callback(["WebFetch"])
        cb2 = create_disallow_list_callback(["WebSearch"])
        composed = compose_permissions(cb1, cb2)
        assert composed("Bash", {}) is None

    def test_empty_composition_allows_everything(self):
        composed = compose_permissions()
        assert composed("Bash", {}) is None
        assert composed("WebFetch", {}) is None

    def test_single_callback_passthrough(self):
        cb = create_disallow_list_callback(["WebFetch"])
        composed = compose_permissions(cb)
        assert composed("WebFetch", {}) is not None
        assert composed("Bash", {}) is None

    def test_later_callback_blocks_when_first_allows(self):
        cb1 = create_disallow_list_callback(["WebFetch"])
        cb2 = create_disallow_list_callback(["Bash"])
        composed = compose_permissions(cb1, cb2)
        result = composed("Bash", {})
        assert result is not None
        assert "Bash" in result

    def test_tool_input_passed_through(self):
        spy = MagicMock(return_value=None)
        composed = compose_permissions(spy)
        input_dict = {"command": "ls", "timeout": 30}
        composed("Bash", input_dict)
        spy.assert_called_once_with("Bash", input_dict)
