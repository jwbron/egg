"""
Tests for agent file restriction enforcement.

Tests cover:
- AgentRole enum consistency between gateway and shared library
- Path traversal prevention in file access checks
- Blocked/allowed pattern precedence
"""

import sys
from pathlib import Path

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

# Import both role definitions to verify consistency
from agent_restrictions import (
    AgentFilePattern,
    AgentRole,
    get_agent_pattern,
    validate_agent_push,
)


class TestAgentRoleConsistency:
    """Verify gateway AgentRole matches shared library AgentRole."""

    def test_role_values_match_shared_library(self):
        """Gateway AgentRole values must match egg_contracts.agent_roles.AgentRole.

        This test prevents subtle bugs from enum value drift between the two modules.
        """
        from egg_contracts.agent_roles import AgentRole as SharedAgentRole

        # Verify all shared library roles exist in gateway with same values
        for shared_role in SharedAgentRole:
            assert hasattr(AgentRole, shared_role.name), (
                f"Gateway AgentRole missing role: {shared_role.name}"
            )
            gateway_value = getattr(AgentRole, shared_role.name)
            assert gateway_value == shared_role.value, (
                f"Role value mismatch for {shared_role.name}: "
                f"gateway={gateway_value}, shared={shared_role.value}"
            )

    def test_all_gateway_roles_in_shared_library(self):
        """All gateway roles must exist in the shared library."""
        from egg_contracts.agent_roles import AgentRole as SharedAgentRole

        gateway_roles = {
            AgentRole.CODER,
            AgentRole.TESTER,
            AgentRole.DOCUMENTER,
            AgentRole.INTEGRATOR,
        }
        shared_values = {r.value for r in SharedAgentRole}

        for role in gateway_roles:
            assert role in shared_values, f"Gateway role '{role}' not found in shared library"


class TestPathTraversalPrevention:
    """Verify path traversal attacks are blocked."""

    def test_path_traversal_blocked(self):
        """Paths with '..' components should be rejected."""
        pattern = AgentFilePattern(
            role=AgentRole.CODER,
            allowed_patterns=[".egg-state/agent-outputs/"],
            blocked_patterns=[],
        )

        # Direct path traversal attempts
        assert not pattern.can_write(".egg-state/agent-outputs/../contracts/evil.json")
        assert not pattern.can_write("../../../etc/passwd")
        assert not pattern.can_write("foo/../bar/../../../secrets")

    def test_normalized_valid_paths_allowed(self):
        """Valid normalized paths should be allowed."""
        pattern = AgentFilePattern(
            role=AgentRole.CODER,
            allowed_patterns=[".egg-state/agent-outputs/"],
            blocked_patterns=[],
        )

        assert pattern.can_write(".egg-state/agent-outputs/coder-output.json")
        assert pattern.can_write(".egg-state/agent-outputs/handoff.json")

    def test_path_traversal_via_double_encoding(self):
        """Paths that normalize to traversal should be blocked."""
        pattern = AgentFilePattern(
            role=AgentRole.CODER,
            allowed_patterns=["**/*.py"],
            blocked_patterns=[],
        )

        # Even if someone tries sneaky encoding, normpath handles it
        assert not pattern.can_write("./foo/../../bar.py")


class TestPatternPrecedence:
    """Verify blocked patterns take precedence over allowed patterns."""

    def test_blocked_takes_precedence_over_directory_allow(self):
        """Blocked patterns must be checked before allowed directory patterns.

        This prevents bypass via allowed directory patterns like:
        .egg-state/agent-outputs/ (allowed) -> .egg-state/agent-outputs/../contracts/ (blocked)
        """
        pattern = AgentFilePattern(
            role=AgentRole.CODER,
            allowed_patterns=[".egg-state/agent-outputs/", "**/*.json"],
            blocked_patterns=[".egg-state/contracts/"],
        )

        # Contract directory must be blocked even though *.json is allowed
        assert not pattern.can_write(".egg-state/contracts/123.json")

        # Agent outputs should still work
        assert pattern.can_write(".egg-state/agent-outputs/output.json")

    def test_explicit_block_overrides_wildcard_allow(self):
        """Explicit blocks must override wildcard allows."""
        pattern = AgentFilePattern(
            role=AgentRole.CODER,
            allowed_patterns=["**/*.py"],
            blocked_patterns=["tests/"],
        )

        # Source files allowed
        assert pattern.can_write("src/app.py")
        # Test files blocked (tester role handles these)
        assert not pattern.can_write("tests/test_app.py")


class TestRealAgentPatterns:
    """Test actual agent patterns from AGENT_PATTERNS registry."""

    def test_coder_cannot_write_contracts(self):
        """Coder should not be able to write to contracts directory."""
        pattern = get_agent_pattern("coder")
        assert pattern is not None
        assert not pattern.can_write(".egg-state/contracts/123.json")

    def test_coder_can_write_source_code(self):
        """Coder should be able to write source code."""
        pattern = get_agent_pattern("coder")
        assert pattern is not None
        assert pattern.can_write("src/module.py")
        assert pattern.can_write("lib/utils.ts")

    def test_tester_can_write_test_files(self):
        """Tester should be able to write test files in test directories."""
        pattern = get_agent_pattern("tester")
        assert pattern is not None
        assert pattern.can_write("tests/test_module.py")
        # Test files in src/ are blocked because src/ is blocked for tester
        # (tester shouldn't modify source code directories)
        assert not pattern.can_write("src/module_test.py")

    def test_integrator_cannot_write_source_or_tests(self):
        """Integrator should not be able to write source or test files."""
        pattern = get_agent_pattern("integrator")
        assert pattern is not None
        assert not pattern.can_write("src/module.py")
        assert not pattern.can_write("tests/test.py")
        assert not pattern.can_write("lib/utils.js")

    def test_integrator_agent_outputs_directory(self):
        """Integrator should be able to write JSON handoff files to agent outputs."""
        pattern = get_agent_pattern("integrator")
        assert pattern is not None
        assert pattern.can_write(".egg-state/agent-outputs/report.json")
        assert pattern.can_write(".egg-state/agent-outputs/integrator-output.json")


class TestValidateAgentPush:
    """Test the validate_agent_push function used by the gateway."""

    def test_empty_role_allows_all(self):
        """Empty role should allow all files (backwards compatibility)."""
        result = validate_agent_push("", ["any/file.py"])
        assert result.allowed

    def test_blocked_files_returned(self):
        """Blocked files should be listed in result."""
        result = validate_agent_push("coder", [".egg-state/contracts/123.json"])
        assert not result.allowed
        assert ".egg-state/contracts/123.json" in result.blocked_files

    def test_mixed_files_reports_blocked(self):
        """Mix of allowed and blocked should report only blocked."""
        result = validate_agent_push(
            "coder", ["src/app.py", ".egg-state/contracts/123.json", "lib/utils.ts"]
        )
        assert not result.allowed
        assert len(result.blocked_files) == 1
        assert ".egg-state/contracts/123.json" in result.blocked_files


class TestRefinerPatterns:
    """Test REFINER_PATTERNS can_write enforcement at the gateway level.

    The refiner uses extension-based blocks (e.g. **/*.py) rather than
    directory-based blocks, so these tests verify pattern matching works
    for various source code extensions and edge cases.
    """

    def test_refiner_can_write_drafts(self):
        """Refiner should be able to write to drafts directory."""
        pattern = get_agent_pattern("refiner")
        assert pattern is not None
        assert pattern.can_write(".egg-state/drafts/analysis.md")
        assert pattern.can_write(".egg-state/drafts/refine-output.json")

    def test_refiner_can_write_agent_outputs(self):
        """Refiner should be able to write to agent-outputs directory."""
        pattern = get_agent_pattern("refiner")
        assert pattern is not None
        assert pattern.can_write(".egg-state/agent-outputs/refiner-output.json")

    def test_refiner_blocked_from_source_code_extensions(self):
        """Refiner must not write files with source code extensions."""
        pattern = get_agent_pattern("refiner")
        assert pattern is not None
        assert not pattern.can_write("src/module.py")
        assert not pattern.can_write("lib/component.ts")
        assert not pattern.can_write("lib/component.tsx")
        assert not pattern.can_write("src/app.js")
        assert not pattern.can_write("src/app.jsx")
        assert not pattern.can_write("cmd/main.go")
        assert not pattern.can_write("src/Main.java")

    def test_refiner_blocked_from_nested_source_files(self):
        """Extension-based blocks should match at any directory depth."""
        pattern = get_agent_pattern("refiner")
        assert pattern is not None
        assert not pattern.can_write("deeply/nested/dir/module.py")
        assert not pattern.can_write("a/b/c/d/file.ts")

    def test_refiner_blocked_from_contracts(self):
        """Refiner should not be able to write to contracts directory."""
        pattern = get_agent_pattern("refiner")
        assert pattern is not None
        assert not pattern.can_write(".egg-state/contracts/123.json")

    def test_refiner_blocked_outside_allowed_directories(self):
        """Refiner should not write to arbitrary directories."""
        pattern = get_agent_pattern("refiner")
        assert pattern is not None
        assert not pattern.can_write("README.md")
        assert not pattern.can_write("docs/guide.md")


class TestReviewerRefinePatterns:
    """Test REVIEWER_REFINE_PATTERNS can_write enforcement at the gateway level."""

    def test_reviewer_refine_can_write_reviews(self):
        """Reviewer refine should be able to write to reviews directory."""
        pattern = get_agent_pattern("reviewer_refine")
        assert pattern is not None
        assert pattern.can_write(".egg-state/reviews/refine-review.md")

    def test_reviewer_refine_can_write_agent_outputs(self):
        """Reviewer refine should be able to write to agent-outputs."""
        pattern = get_agent_pattern("reviewer_refine")
        assert pattern is not None
        assert pattern.can_write(".egg-state/agent-outputs/review-output.json")

    def test_reviewer_refine_blocked_from_source(self):
        """Reviewer refine should not write to source directories."""
        pattern = get_agent_pattern("reviewer_refine")
        assert pattern is not None
        assert not pattern.can_write("src/module.py")
        assert not pattern.can_write("lib/utils.ts")

    def test_reviewer_refine_blocked_from_contracts(self):
        """Reviewer refine should not write to contracts."""
        pattern = get_agent_pattern("reviewer_refine")
        assert pattern is not None
        assert not pattern.can_write(".egg-state/contracts/123.json")

    def test_reviewer_refine_blocked_from_drafts(self):
        """Reviewer refine should not write to drafts (only refiner can)."""
        pattern = get_agent_pattern("reviewer_refine")
        assert pattern is not None
        assert not pattern.can_write(".egg-state/drafts/analysis.md")


class TestReviewerPlanPatterns:
    """Test REVIEWER_PLAN_PATTERNS can_write enforcement at the gateway level."""

    def test_reviewer_plan_can_write_reviews(self):
        """Reviewer plan should be able to write to reviews directory."""
        pattern = get_agent_pattern("reviewer_plan")
        assert pattern is not None
        assert pattern.can_write(".egg-state/reviews/plan-review.md")

    def test_reviewer_plan_can_write_agent_outputs(self):
        """Reviewer plan should be able to write to agent-outputs."""
        pattern = get_agent_pattern("reviewer_plan")
        assert pattern is not None
        assert pattern.can_write(".egg-state/agent-outputs/review-output.json")

    def test_reviewer_plan_blocked_from_source(self):
        """Reviewer plan should not write to source directories."""
        pattern = get_agent_pattern("reviewer_plan")
        assert pattern is not None
        assert not pattern.can_write("src/module.py")
        assert not pattern.can_write("lib/utils.ts")

    def test_reviewer_plan_blocked_from_contracts(self):
        """Reviewer plan should not write to contracts."""
        pattern = get_agent_pattern("reviewer_plan")
        assert pattern is not None
        assert not pattern.can_write(".egg-state/contracts/123.json")

    def test_reviewer_plan_blocked_from_drafts(self):
        """Reviewer plan should not write to drafts."""
        pattern = get_agent_pattern("reviewer_plan")
        assert pattern is not None
        assert not pattern.can_write(".egg-state/drafts/plan.md")
