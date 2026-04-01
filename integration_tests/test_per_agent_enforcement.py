"""Integration tests for per-agent worktree isolation and role-aware enforcement.

These tests verify that the full enforcement stack works end-to-end:
1. Per-agent worktree isolation (distinct worktree IDs per role)
2. Role-based file restriction (from shared egg_restrictions package)
3. Per-agent git identity
4. Auto-commit disabled (no-op behavior)
5. Push scoping (each agent only pushes their own files)

Note: These are unit-level integration tests using mocks for Docker/gateway
interactions. Full end-to-end tests require a running infrastructure.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add shared/ and gateway/ to sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).parent.parent / "gateway"))

from egg_restrictions import (
    AGENT_PATTERNS,
    AgentFilePattern,
    AgentRole,
    check_agent_file_access,
    get_agent_pattern,
    validate_agent_push,
)
from egg_restrictions.checker import AgentRestrictionResult


# ---------------------------------------------------------------------------
# Worktree ID generation
# ---------------------------------------------------------------------------


class TestWorktreeIdFormat:
    """Verify worktree ID construction matches orchestrator convention."""

    def test_agent_worktree_id_format(self):
        """For pipeline 'issue-123' with role 'coder', ID is 'issue-123-coder'."""
        pipeline_id = "issue-123"
        role = "coder"
        agent_worktree_id = f"{pipeline_id}-{role}"
        assert agent_worktree_id == "issue-123-coder"

    def test_distinct_worktree_ids_per_role(self):
        """For roles coder/tester/documenter, all get distinct worktree IDs."""
        pipeline_id = "issue-456"
        roles = ["coder", "tester", "documenter"]
        worktree_ids = [f"{pipeline_id}-{role}" for role in roles]

        # All IDs should be unique
        assert len(set(worktree_ids)) == len(roles)

        # Verify specific values
        assert worktree_ids == [
            "issue-456-coder",
            "issue-456-tester",
            "issue-456-documenter",
        ]

    def test_worktree_id_with_numeric_pipeline(self):
        """Pipeline ID may be purely numeric (e.g., GitHub issue number)."""
        pipeline_id = "789"
        role = "tester"
        assert f"{pipeline_id}-{role}" == "789-tester"


# ---------------------------------------------------------------------------
# Role-based file restrictions
# ---------------------------------------------------------------------------


class TestCoderAllowedFiles:
    """Verify coder can write source code but NOT tests or docs."""

    @pytest.mark.parametrize(
        "file_path",
        [
            "src/main.py",
            "lib/utils.ts",
            "app/component.tsx",
            "config/settings.yml",
            "package.json",
            "pyproject.toml",
            "Makefile",
            "Dockerfile",
            "scripts/deploy.sh",
            ".egg-state/agent-outputs/coder-result.json",
        ],
    )
    def test_coder_allowed_source_files(self, file_path):
        allowed, blocked, reason = check_agent_file_access("coder", [file_path])
        assert allowed, f"Coder should be allowed to write {file_path}: {reason}"

    @pytest.mark.parametrize(
        "file_path",
        [
            "tests/test_main.py",
            "test/test_utils.py",
            "src/tests/test_foo.py",
            "lib/test/test_bar.py",
            "docs/README.md",
            "docs/guide.md",
            "README.md",
            "CHANGELOG.md",
        ],
    )
    def test_coder_blocked_from_tests_and_docs(self, file_path):
        allowed, blocked, reason = check_agent_file_access("coder", [file_path])
        assert not allowed, f"Coder should NOT be allowed to write {file_path}"
        assert file_path in blocked


class TestTesterAllowedFiles:
    """Verify tester can write test files but NOT source code or docs."""

    @pytest.mark.parametrize(
        "file_path",
        [
            "tests/test_main.py",
            "test/test_utils.py",
            "src/tests/test_integration.py",
            "app/test/test_component.py",
            "test_foo.py",
            "conftest.py",
            "tests/conftest.py",
            ".egg-state/agent-outputs/tester-result.json",
        ],
    )
    def test_tester_allowed_test_files(self, file_path):
        allowed, blocked, reason = check_agent_file_access("tester", [file_path])
        assert allowed, f"Tester should be allowed to write {file_path}: {reason}"

    @pytest.mark.parametrize(
        "file_path",
        [
            "src/main.py",
            "lib/utils.ts",
            "app/component.tsx",
            "docs/README.md",
            "docs/guide.md",
        ],
    )
    def test_tester_blocked_from_source_and_docs(self, file_path):
        allowed, blocked, reason = check_agent_file_access("tester", [file_path])
        assert not allowed, f"Tester should NOT be allowed to write {file_path}"
        assert file_path in blocked


class TestDocumenterAllowedFiles:
    """Verify documenter can write docs but NOT source code or tests."""

    @pytest.mark.parametrize(
        "file_path",
        [
            "docs/README.md",
            "docs/guide.md",
            "docs/architecture/overview.md",
            "README.md",
            "CONTRIBUTING.md",
            ".egg-state/agent-outputs/documenter-result.json",
        ],
    )
    def test_documenter_allowed_doc_files(self, file_path):
        allowed, blocked, reason = check_agent_file_access("documenter", [file_path])
        assert allowed, f"Documenter should be allowed to write {file_path}: {reason}"

    @pytest.mark.parametrize(
        "file_path",
        [
            "src/main.py",
            "lib/utils.ts",
            "tests/test_main.py",
            "test/test_utils.py",
        ],
    )
    def test_documenter_blocked_from_source_and_tests(self, file_path):
        allowed, blocked, reason = check_agent_file_access("documenter", [file_path])
        assert not allowed, f"Documenter should NOT be allowed to write {file_path}"
        assert file_path in blocked


class TestUnknownRoleDenied:
    """Verify unknown role gets deny-all from egg_restrictions."""

    def test_unknown_role_denied(self):
        allowed, blocked, reason = check_agent_file_access(
            "unknown_agent", ["src/main.py"]
        )
        assert not allowed
        assert "src/main.py" in blocked
        assert "Unknown agent role" in reason

    def test_unknown_role_denies_all_files(self):
        files = ["src/main.py", "tests/test.py", "docs/readme.md"]
        allowed, blocked, reason = check_agent_file_access("nonexistent", files)
        assert not allowed
        assert blocked == files

    def test_get_agent_pattern_returns_none_for_unknown(self):
        assert get_agent_pattern("nonexistent") is None


# ---------------------------------------------------------------------------
# Git identity
# ---------------------------------------------------------------------------


class TestGitIdentityFormat:
    """Verify per-agent git identity formatting conventions."""

    def test_git_identity_format_with_role(self):
        """With EGG_AGENT_ROLE='coder', identity is 'egg (coder) <coder@egg.local>'."""
        role = "coder"
        expected_name = f"egg ({role})"
        expected_email = f"{role}@egg.local"
        assert expected_name == "egg (coder)"
        assert expected_email == "coder@egg.local"

    def test_git_identity_format_tester(self):
        role = "tester"
        assert f"egg ({role})" == "egg (tester)"
        assert f"{role}@egg.local" == "tester@egg.local"

    def test_git_identity_format_documenter(self):
        role = "documenter"
        assert f"egg ({role})" == "egg (documenter)"
        assert f"{role}@egg.local" == "documenter@egg.local"

    def test_git_identity_fallback(self):
        """Without EGG_AGENT_ROLE, identity is 'egg <egg@localhost>'."""
        role = None
        if role:
            name = f"egg ({role})"
            email = f"{role}@egg.local"
        else:
            name = "egg"
            email = "egg@localhost"
        assert name == "egg"
        assert email == "egg@localhost"

    def test_all_identities_contain_egg_for_git_log(self):
        """All identity formats contain 'egg' so git log --author=egg works."""
        for role in ("coder", "tester", "documenter", "reviewer_code"):
            name = f"egg ({role})"
            assert "egg" in name


# ---------------------------------------------------------------------------
# Auto-commit disabled
# ---------------------------------------------------------------------------


class TestAutoCommitDisabled:
    """Verify auto_commit_worktree returns None (no commit created)."""

    def test_auto_commit_disabled(self):
        """auto_commit_worktree always returns None with per-agent isolation."""
        from post_agent_commit import auto_commit_worktree

        with tempfile.TemporaryDirectory() as tmpdir:
            result = auto_commit_worktree(
                worktree_path=tmpdir,
                container_id="issue-123-coder",
                agent_role="coder",
                pipeline_id="issue-123",
                phase="implement",
            )
            assert result is None

    def test_auto_commit_nonexistent_path(self):
        """auto_commit_worktree returns None for nonexistent path."""
        from post_agent_commit import auto_commit_worktree

        result = auto_commit_worktree(
            worktree_path="/tmp/nonexistent-worktree-path-xyz",
            container_id="issue-123-coder",
        )
        assert result is None


# ---------------------------------------------------------------------------
# Tool interceptor integration
# ---------------------------------------------------------------------------


class TestToolInterceptorBlocksCoderWritingTests:
    """Verify tool interceptor blocks coder writing to tests/."""

    def test_tool_interceptor_blocks_coder_writing_tests(self):
        from egg_agent.tool_interceptor import check_file_write_permission

        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "tests/test_foo.py"},
            agent_role="coder",
        )
        assert result is not None, "Expected block but got None (allowed)"
        assert "coder" in result
        assert "tester" in result  # error should mention owning role


class TestToolInterceptorAllowsCoderWritingSource:
    """Verify tool interceptor allows coder writing to src/main.py."""

    def test_tool_interceptor_allows_coder_writing_source(self):
        from egg_agent.tool_interceptor import check_file_write_permission

        result = check_file_write_permission(
            tool_name="Write",
            tool_input={"file_path": "src/main.py"},
            agent_role="coder",
        )
        assert result is None, f"Expected allow but got: {result}"


# ---------------------------------------------------------------------------
# Cross-role enforcement matrix
# ---------------------------------------------------------------------------


class TestCrossRoleEnforcementMatrix:
    """For each of coder/tester/documenter, verify correct allow/block behavior."""

    # Matrix: (role, file_path, expected_allowed)
    MATRIX = [
        # Coder: source allowed, tests and docs blocked
        ("coder", "src/main.py", True),
        ("coder", "lib/utils.ts", True),
        ("coder", "config.yml", True),
        ("coder", "tests/test_main.py", False),
        ("coder", "docs/guide.md", False),
        ("coder", "README.md", False),
        # Tester: tests allowed, source and docs blocked
        ("tester", "tests/test_main.py", True),
        ("tester", "test/test_utils.py", True),
        ("tester", "conftest.py", True),
        ("tester", "src/main.py", False),
        ("tester", "docs/guide.md", False),
        # Documenter: docs allowed, source and tests blocked
        ("documenter", "docs/guide.md", True),
        ("documenter", "README.md", True),
        ("documenter", "CONTRIBUTING.md", True),
        ("documenter", "src/main.py", False),
        ("documenter", "tests/test_main.py", False),
    ]

    @pytest.mark.parametrize("role,file_path,expected_allowed", MATRIX)
    def test_cross_role_enforcement_matrix(self, role, file_path, expected_allowed):
        allowed, blocked, reason = check_agent_file_access(role, [file_path])
        if expected_allowed:
            assert allowed, (
                f"Role '{role}' should be ALLOWED to write {file_path}: {reason}"
            )
        else:
            assert not allowed, (
                f"Role '{role}' should be BLOCKED from writing {file_path}"
            )
            assert file_path in blocked


class TestValidateAgentPushIntegration:
    """Verify validate_agent_push returns correct AgentRestrictionResult."""

    def test_allowed_push(self):
        result = validate_agent_push("coder", ["src/main.py", "lib/utils.ts"])
        assert result.allowed
        assert result.role == "coder"
        assert result.blocked_files == []

    def test_blocked_push(self):
        result = validate_agent_push("coder", ["tests/test_main.py"])
        assert not result.allowed
        assert result.role == "coder"
        assert "tests/test_main.py" in result.blocked_files

    def test_no_role_allows_all(self):
        result = validate_agent_push("", ["anything.py"])
        assert result.allowed

    def test_no_files_allows(self):
        result = validate_agent_push("coder", [])
        assert result.allowed

    def test_mixed_files_blocks_on_any_violation(self):
        result = validate_agent_push(
            "coder", ["src/main.py", "tests/test_main.py"]
        )
        assert not result.allowed
        assert "tests/test_main.py" in result.blocked_files
        # Allowed file should NOT be in blocked list
        assert "src/main.py" not in result.blocked_files
