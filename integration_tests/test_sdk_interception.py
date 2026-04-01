"""Integration tests for SDK tool interception.

Verifies that the tool interceptor correctly validates file paths
against role restrictions for Write, Edit, and NotebookEdit tools.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add shared/ to sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))

from egg_agent.tool_interceptor import check_file_write_permission


# ---------------------------------------------------------------------------
# Write tool interception
# ---------------------------------------------------------------------------


class TestWriteToDisallowedPath:
    """Coder writing to test files gets an error."""

    def test_write_to_disallowed_path_returns_error(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "tests/test_foo.py"},
            agent_role="coder",
        )
        assert result is not None, "Expected error but got None (allowed)"
        assert "coder" in result
        assert "cannot write" in result.lower() or "cannot" in result.lower()

    def test_write_to_disallowed_docs(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "docs/README.md"},
            agent_role="coder",
        )
        assert result is not None, "Expected error but got None (allowed)"


class TestWriteToAllowedPath:
    """Coder writing to source files gets None (allowed)."""

    def test_write_to_allowed_path_returns_none(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "src/main.py"},
            agent_role="coder",
        )
        assert result is None, f"Expected None (allowed) but got: {result}"

    def test_write_to_allowed_config(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "config/settings.yml"},
            agent_role="coder",
        )
        assert result is None, f"Expected None (allowed) but got: {result}"


# ---------------------------------------------------------------------------
# Edit tool interception
# ---------------------------------------------------------------------------


class TestEditToDisallowedPath:
    """Coder editing docs gets an error."""

    def test_edit_to_disallowed_path(self):
        result = check_file_write_permission(
            tool_name="Edit",
            tool_input={"file_path": "docs/README.md"},
            agent_role="coder",
        )
        assert result is not None, "Expected error but got None (allowed)"
        assert "coder" in result

    def test_edit_to_disallowed_test(self):
        result = check_file_write_permission(
            tool_name="Edit",
            tool_input={"file_path": "tests/test_main.py"},
            agent_role="coder",
        )
        assert result is not None, "Expected error but got None (allowed)"


# ---------------------------------------------------------------------------
# NotebookEdit tool interception
# ---------------------------------------------------------------------------


class TestNotebookEditToDisallowedPath:
    """Coder editing a notebook in tests/ gets an error."""

    def test_notebook_edit_to_disallowed_path(self):
        result = check_file_write_permission(
            tool_name="NotebookEdit",
            tool_input={"notebook_path": "tests/notebook.ipynb"},
            agent_role="coder",
        )
        assert result is not None, "Expected error but got None (allowed)"
        assert "coder" in result


# ---------------------------------------------------------------------------
# Non-write tools are not intercepted
# ---------------------------------------------------------------------------


class TestBashNotIntercepted:
    """Bash tool always returns None (not intercepted)."""

    def test_bash_not_intercepted(self):
        result = check_file_write_permission(
            tool_name="Bash",
            tool_input={"command": "rm -rf tests/"},
            agent_role="coder",
        )
        assert result is None, "Bash should not be intercepted"

    def test_read_not_intercepted(self):
        result = check_file_write_permission(
            tool_name="Read",
            tool_input={"file_path": "tests/test_main.py"},
            agent_role="coder",
        )
        assert result is None, "Read should not be intercepted"

    def test_glob_not_intercepted(self):
        result = check_file_write_permission(
            tool_name="Glob",
            tool_input={"pattern": "**/*.py"},
            agent_role="coder",
        )
        assert result is None, "Glob should not be intercepted"


# ---------------------------------------------------------------------------
# Tester role
# ---------------------------------------------------------------------------


class TestTesterWritingTestFile:
    """Tester can write test files."""

    def test_tester_writing_test_file(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "tests/test_new_feature.py"},
            agent_role="tester",
        )
        assert result is None, f"Tester should be allowed to write tests: {result}"

    def test_tester_writing_conftest(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "tests/conftest.py"},
            agent_role="tester",
        )
        assert result is None, f"Tester should be allowed to write conftest: {result}"


class TestTesterBlockedFromSource:
    """Tester cannot write source files."""

    def test_tester_blocked_from_source(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "src/main.py"},
            agent_role="tester",
        )
        assert result is not None, "Tester should NOT be allowed to write source"
        assert "tester" in result

    def test_tester_blocked_from_config(self):
        result = check_file_write_permission(
            tool_name="Edit",
            tool_input={"file_path": "lib/utils.ts"},
            agent_role="tester",
        )
        assert result is not None, "Tester should NOT be allowed to write lib/"


# ---------------------------------------------------------------------------
# Documenter role
# ---------------------------------------------------------------------------


class TestDocumenterWritingDocs:
    """Documenter can write documentation files."""

    def test_documenter_writing_docs(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "docs/guide.md"},
            agent_role="documenter",
        )
        assert result is None, f"Documenter should be allowed to write docs: {result}"

    def test_documenter_writing_readme(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "README.md"},
            agent_role="documenter",
        )
        assert result is None, f"Documenter should be allowed to write README: {result}"


# ---------------------------------------------------------------------------
# No role set (interactive mode)
# ---------------------------------------------------------------------------


class TestNoRoleAllowsEverything:
    """No role set means all operations are allowed (interactive mode)."""

    def test_no_role_allows_everything(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "tests/test_foo.py"},
            agent_role=None,
        )
        assert result is None, "No role should allow everything"

    def test_empty_role_allows_everything(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "tests/test_foo.py"},
            agent_role="",
        )
        assert result is None, "Empty role should allow everything"

    def test_no_role_allows_source(self):
        result = check_file_write_permission(
            tool_name="Edit",
            tool_input={"file_path": "src/main.py"},
            agent_role=None,
        )
        assert result is None

    def test_no_role_allows_docs(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "docs/README.md"},
            agent_role=None,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Error message quality
# ---------------------------------------------------------------------------


class TestErrorMessageIncludesOwnerRole:
    """Error messages should mention which role owns the target path."""

    def test_error_message_includes_owner_role_tester(self):
        """Error for coder writing tests should mention 'tester' role."""
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "tests/test_foo.py"},
            agent_role="coder",
        )
        assert result is not None
        assert "tester" in result, (
            f"Error should mention 'tester' as the owning role, got: {result}"
        )

    def test_error_message_includes_owner_role_documenter(self):
        """Error for coder writing docs should mention 'documenter' role."""
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "docs/guide.md"},
            agent_role="coder",
        )
        assert result is not None
        assert "documenter" in result, (
            f"Error should mention 'documenter' as the owning role, got: {result}"
        )

    def test_error_message_includes_owner_role_coder(self):
        """Error for tester writing source should mention 'coder' role."""
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "src/main.py"},
            agent_role="tester",
        )
        assert result is not None
        assert "coder" in result, (
            f"Error should mention 'coder' as the owning role, got: {result}"
        )

    def test_error_message_mentions_gateway(self):
        """Error should mention that the gateway would reject the operation."""
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "tests/test_foo.py"},
            agent_role="coder",
        )
        assert result is not None
        assert "gateway" in result.lower(), (
            f"Error should mention gateway rejection, got: {result}"
        )

    def test_error_message_suggests_delegation(self):
        """Error should suggest delegating to the appropriate agent."""
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "tests/test_foo.py"},
            agent_role="coder",
        )
        assert result is not None
        assert "delegat" in result.lower(), (
            f"Error should suggest delegation, got: {result}"
        )


# ---------------------------------------------------------------------------
# Absolute path normalization
# ---------------------------------------------------------------------------


class TestAbsolutePathNormalization:
    """Verify that absolute paths are normalized to repo-relative for checking."""

    def test_absolute_path_normalized(self):
        """Absolute paths like /home/egg/repos/myrepo/tests/... are normalized."""
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "/home/egg/repos/myrepo/tests/test_foo.py"},
            agent_role="coder",
        )
        assert result is not None, "Absolute path to tests/ should still be blocked"

    def test_absolute_path_source_allowed(self):
        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "/home/egg/repos/myrepo/src/main.py"},
            agent_role="coder",
        )
        assert result is None, "Absolute path to src/ should be allowed for coder"
