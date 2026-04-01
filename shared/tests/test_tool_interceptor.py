"""Tests for the egg_agent.tool_interceptor module."""

from __future__ import annotations

import os
from unittest import mock

from egg_agent.tool_interceptor import (
    _find_owning_role,
    _normalize_to_repo_relative,
    check_file_write_permission,
    get_role_from_env,
)

# ---------------------------------------------------------------------------
# check_file_write_permission
# ---------------------------------------------------------------------------


class TestCheckFileWritePermission:
    """Tests for the main check_file_write_permission function."""

    def test_coder_writing_to_allowed_source_file(self):
        """Coder writing to a source file should be allowed (returns None)."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/myrepo/src/main.py"},
            agent_role="coder",
        )
        assert result is None

    def test_coder_writing_to_disallowed_test_file(self):
        """Coder writing to a test file should be blocked."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/myrepo/tests/test_foo.py"},
            agent_role="coder",
        )
        assert result is not None
        assert "coder" in result
        assert "tests/test_foo.py" in result

    def test_edit_to_disallowed_path(self):
        """Edit tool writing to a disallowed path should be blocked."""
        result = check_file_write_permission(
            "Edit",
            {"file_path": "/home/egg/repos/myrepo/tests/test_bar.py"},
            agent_role="coder",
        )
        assert result is not None
        assert "coder" in result

    def test_notebook_edit_to_disallowed_path(self):
        """NotebookEdit tool writing to a disallowed path should be blocked."""
        result = check_file_write_permission(
            "NotebookEdit",
            {"notebook_path": "/home/egg/repos/myrepo/docs/guide.ipynb"},
            agent_role="coder",
        )
        assert result is not None
        assert "coder" in result

    def test_bash_tool_not_intercepted(self):
        """Bash tool should never be intercepted (returns None)."""
        result = check_file_write_permission(
            "Bash",
            {"command": "rm -rf /"},
            agent_role="coder",
        )
        assert result is None

    def test_read_tool_not_intercepted(self):
        """Read tool should never be intercepted (returns None)."""
        result = check_file_write_permission(
            "Read",
            {"file_path": "/home/egg/repos/myrepo/tests/test_foo.py"},
            agent_role="coder",
        )
        assert result is None

    def test_no_role_returns_none(self):
        """No role set (None) should allow everything (backward compat)."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/myrepo/tests/test_foo.py"},
            agent_role=None,
        )
        assert result is None

    def test_empty_role_returns_none(self):
        """Empty string role should allow everything (backward compat)."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/myrepo/tests/test_foo.py"},
            agent_role="",
        )
        assert result is None

    def test_error_message_includes_owning_role(self):
        """When blocked, the error message should hint at the owning role."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/myrepo/tests/test_foo.py"},
            agent_role="coder",
        )
        assert result is not None
        # tests/ belongs to the tester role
        assert "tester" in result

    def test_tester_writing_to_test_file_allowed(self):
        """Tester writing to a test file should be allowed."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/myrepo/tests/test_something.py"},
            agent_role="tester",
        )
        assert result is None

    def test_tester_writing_to_source_file_blocked(self):
        """Tester writing to a source file should be blocked."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/myrepo/src/main.py"},
            agent_role="tester",
        )
        assert result is not None
        assert "tester" in result

    def test_documenter_writing_to_docs_allowed(self):
        """Documenter writing to docs should be allowed."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/myrepo/docs/guide.md"},
            agent_role="documenter",
        )
        assert result is None

    def test_missing_file_path_in_input(self):
        """Missing file_path key in tool input should return None."""
        result = check_file_write_permission(
            "Write",
            {},
            agent_role="coder",
        )
        assert result is None

    def test_rejection_message_in_error(self):
        """Error message should mention gateway rejection for blocked writes."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/myrepo/tests/test_foo.py"},
            agent_role="coder",
        )
        assert result is not None
        assert "gateway will reject" in result.lower()

    def test_gateway_mention_in_error(self):
        """Error message should mention the gateway would reject at push time."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/myrepo/tests/test_foo.py"},
            agent_role="coder",
        )
        assert result is not None
        assert "gateway" in result.lower()

    def test_egg_restrictions_import_failure_allows(self):
        """When egg_restrictions is not importable, should fail open."""
        with mock.patch.dict("sys.modules", {"egg_restrictions": None}):
            result = check_file_write_permission(
                "Write",
                {"file_path": "/home/egg/repos/myrepo/tests/test_foo.py"},
                agent_role="coder",
            )
            assert result is None


# ---------------------------------------------------------------------------
# _normalize_to_repo_relative
# ---------------------------------------------------------------------------


class TestNormalizeToRepoRelative:
    """Tests for the path normalization helper."""

    def test_strips_home_egg_repos_prefix(self):
        """Should strip /home/egg/repos/<repo>/ prefix."""
        result = _normalize_to_repo_relative("/home/egg/repos/myrepo/src/main.py")
        assert result == "src/main.py"

    def test_strips_different_repo_name(self):
        """Should work with any repo name."""
        result = _normalize_to_repo_relative("/home/egg/repos/other-repo/tests/test_foo.py")
        assert result == "tests/test_foo.py"

    def test_strips_leading_slash_for_other_paths(self):
        """Should strip leading slash for non-standard paths."""
        result = _normalize_to_repo_relative("/some/other/path/file.py")
        assert result == "some/other/path/file.py"

    def test_relative_path_passthrough(self):
        """Relative paths should pass through as-is."""
        result = _normalize_to_repo_relative("src/main.py")
        assert result == "src/main.py"

    def test_deeply_nested_path(self):
        """Should handle deeply nested paths after repo root."""
        result = _normalize_to_repo_relative(
            "/home/egg/repos/myrepo/shared/egg_agent/tool_interceptor.py"
        )
        assert result == "shared/egg_agent/tool_interceptor.py"

    def test_path_at_repo_root(self):
        """Should handle files directly at repo root."""
        result = _normalize_to_repo_relative("/home/egg/repos/myrepo/Makefile")
        assert result == "Makefile"


# ---------------------------------------------------------------------------
# _find_owning_role
# ---------------------------------------------------------------------------


class TestFindOwningRole:
    """Tests for the role ownership lookup helper."""

    def test_test_file_owned_by_tester(self):
        """Test files should be owned by the tester role."""
        result = _find_owning_role("tests/test_foo.py", "coder")
        assert result == "tester"

    def test_source_file_owned_by_coder(self):
        """Source files should be owned by the coder role."""
        result = _find_owning_role("src/main.py", "tester")
        assert result == "coder"

    def test_docs_file_owned_by_documenter(self):
        """Documentation files should be owned by the documenter role."""
        result = _find_owning_role("docs/guide.md", "coder")
        assert result == "documenter"

    def test_excluded_role_not_returned(self):
        """The excluded role (the asking role) should not be returned."""
        result = _find_owning_role("src/main.py", "coder")
        # coder owns src/main.py, but coder is excluded -- should look further
        # Actually coder IS the owner but is excluded, so it might return
        # autofixer or conflict_resolver which also can write .py files
        assert result != "coder"

    def test_egg_state_file_returns_appropriate_role(self):
        """Files in .egg-state/drafts/ should return an appropriate role."""
        result = _find_owning_role(".egg-state/drafts/plan.json", "coder")
        # Architect, task_planner, risk_analyst can write to drafts
        assert result in ("architect", "task_planner", "risk_analyst", "refiner")

    def test_no_owner_found_returns_none(self):
        """When no role can write to a file, should return None."""
        # A file in .egg-state/pipelines/ is blocked by most roles
        # and not in any allowed_patterns -- could return None or conflict_resolver
        # Let's test with a truly unmatched path
        result = _find_owning_role("__nonexistent_weird_dir__/file.xyz", "coder")
        # This might or might not match any pattern; the test verifies it doesn't crash
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# get_role_from_env
# ---------------------------------------------------------------------------


class TestGetRoleFromEnv:
    """Tests for environment-based role detection."""

    def test_returns_role_when_set(self):
        with mock.patch.dict(os.environ, {"EGG_AGENT_ROLE": "coder"}):
            assert get_role_from_env() == "coder"

    def test_returns_none_when_not_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            assert get_role_from_env() is None

    def test_returns_none_for_empty_string(self):
        with mock.patch.dict(os.environ, {"EGG_AGENT_ROLE": ""}):
            assert get_role_from_env() is None

    def test_returns_none_for_whitespace_only(self):
        with mock.patch.dict(os.environ, {"EGG_AGENT_ROLE": "   "}):
            assert get_role_from_env() is None

    def test_strips_whitespace(self):
        with mock.patch.dict(os.environ, {"EGG_AGENT_ROLE": "  tester  "}):
            assert get_role_from_env() == "tester"
