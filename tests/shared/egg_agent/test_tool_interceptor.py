"""
Tests for SDK tool interception (#1481).

Validates the pre-execution hook that checks Write/Edit/NotebookEdit
file paths against role-based file restrictions, giving agents early
feedback instead of discovering violations at push time.

Key behaviors tested:
- check_file_write_permission blocks writes outside role boundaries
- check_file_write_permission allows writes within role boundaries
- No role (interactive mode) allows all operations
- Non-write tools are not intercepted
- _normalize_to_repo_relative strips /home/egg/repos/<repo>/ prefix
- _find_owning_role returns the correct owner
- get_role_from_env reads EGG_AGENT_ROLE
"""

import sys
from pathlib import Path

# Add shared to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "shared"))

from egg_agent.tool_interceptor import (
    _normalize_to_repo_relative,
    check_file_write_permission,
    get_role_from_env,
)


class TestCheckFileWritePermission:
    """check_file_write_permission validates tool invocations against role."""

    def test_coder_can_write_source_code(self):
        """Coder role allowed to write src/ files."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/egg/src/main.py"},
            agent_role="coder",
        )
        assert result is None, f"Coder should be allowed to write source: {result}"

    def test_tester_can_write_test_files(self):
        """Tester role allowed to write test files."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/egg/tests/test_main.py"},
            agent_role="tester",
        )
        assert result is None, f"Tester should be allowed to write tests: {result}"

    def test_documenter_can_write_docs(self):
        """Documenter role allowed to write docs."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/egg/docs/guide.md"},
            agent_role="documenter",
        )
        assert result is None, f"Documenter should be allowed to write docs: {result}"

    def test_tester_blocked_from_source_code(self):
        """Tester role blocked from writing source code (non-test files)."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/egg/src/main.py"},
            agent_role="tester",
        )
        # Should return an error string
        assert result is not None, "Tester should be blocked from source code"
        assert "tester" in result.lower()

    def test_documenter_blocked_from_source_code(self):
        """Documenter role blocked from writing source code."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/egg/gateway/git_client.py"},
            agent_role="documenter",
        )
        assert result is not None, "Documenter should be blocked from source code"
        assert "documenter" in result.lower()

    def test_no_role_allows_all(self):
        """No role (interactive mode) allows all operations."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/egg/anything.py"},
            agent_role=None,
        )
        assert result is None

    def test_empty_role_allows_all(self):
        """Empty role string allows all operations."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/egg/anything.py"},
            agent_role="",
        )
        assert result is None

    def test_non_write_tool_not_intercepted(self):
        """Read, Bash, etc. are not intercepted."""
        result = check_file_write_permission(
            "Read",
            {"file_path": "/home/egg/repos/egg/src/main.py"},
            agent_role="tester",
        )
        assert result is None

    def test_edit_tool_intercepted(self):
        """Edit tool is also intercepted."""
        result = check_file_write_permission(
            "Edit",
            {"file_path": "/home/egg/repos/egg/src/main.py"},
            agent_role="tester",
        )
        # Should be blocked for tester writing source
        assert result is not None

    def test_notebook_edit_intercepted(self):
        """NotebookEdit tool is also intercepted."""
        result = check_file_write_permission(
            "NotebookEdit",
            {"notebook_path": "/home/egg/repos/egg/src/notebook.ipynb"},
            agent_role="tester",
        )
        # Tester shouldn't write source notebooks
        assert result is not None

    def test_missing_file_path_allows(self):
        """Missing file_path in tool input allows (no path to check)."""
        result = check_file_write_permission(
            "Write",
            {},
            agent_role="tester",
        )
        assert result is None

    def test_error_message_mentions_delegation(self):
        """Error message suggests delegating to appropriate role."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/egg/src/main.py"},
            agent_role="tester",
        )
        assert result is not None
        assert "delegat" in result.lower() or "role" in result.lower()

    def test_error_message_mentions_gateway(self):
        """Error message warns about gateway rejection at push time."""
        result = check_file_write_permission(
            "Write",
            {"file_path": "/home/egg/repos/egg/src/main.py"},
            agent_role="tester",
        )
        assert result is not None
        assert "gateway" in result.lower() or "push" in result.lower()


class TestNormalizeToRepoRelative:
    """_normalize_to_repo_relative strips common prefixes."""

    def test_strips_home_egg_repos_prefix(self):
        """Strips /home/egg/repos/<repo>/ prefix."""
        result = _normalize_to_repo_relative("/home/egg/repos/egg/src/main.py")
        assert result == "src/main.py"

    def test_strips_leading_slash(self):
        """Strips leading slash from paths without the known prefix."""
        result = _normalize_to_repo_relative("/some/other/path.py")
        assert result == "some/other/path.py"

    def test_relative_path_unchanged(self):
        """Relative paths pass through unchanged."""
        result = _normalize_to_repo_relative("src/main.py")
        assert result == "src/main.py"

    def test_deep_repo_path(self):
        """Deeply nested file under repos prefix."""
        result = _normalize_to_repo_relative("/home/egg/repos/myrepo/deep/nested/file.py")
        assert result == "deep/nested/file.py"


class TestGetRoleFromEnv:
    """get_role_from_env reads EGG_AGENT_ROLE."""

    def test_returns_role_when_set(self, monkeypatch):
        monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
        assert get_role_from_env() == "coder"

    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
        assert get_role_from_env() is None

    def test_returns_none_for_empty(self, monkeypatch):
        monkeypatch.setenv("EGG_AGENT_ROLE", "")
        assert get_role_from_env() is None

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("EGG_AGENT_ROLE", "  tester  ")
        assert get_role_from_env() == "tester"
