"""
Tests for gateway phase filter anchor enforcement.

Covers:
- .egg-state/agent-anchors/* allowed in all phases (refine, plan, implement)
- Session-scoped write validation via check_anchor_write_permission
- Cross-agent write rejection
- Path normalization edge cases
"""

import sys
from pathlib import Path

import pytest

_gateway_path = Path(__file__).parent.parent
if str(_gateway_path) not in sys.path:
    sys.path.insert(0, str(_gateway_path))


class TestAnchorPhasePermissions:
    """Tests that anchor files are allowed in all phases."""

    def test_anchor_allowed_in_implement_phase(self):
        """Anchor file writes are allowed in implement phase."""
        from phase_filter import PhaseFilter, reset_phase_filter

        reset_phase_filter()
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions(
            "implement",
            [".egg-state/agent-anchors/coder-abc12345.json"],
        )
        assert result.allowed is True, f"Anchor writes should be allowed in implement: {result}"

    def test_anchor_allowed_in_plan_phase(self):
        """Anchor file writes are allowed in plan phase."""
        from phase_filter import PhaseFilter, reset_phase_filter

        reset_phase_filter()
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions(
            "plan",
            [".egg-state/agent-anchors/planner-xyz.json"],
        )
        assert result.allowed is True, f"Anchor writes should be allowed in plan: {result}"

    def test_anchor_allowed_in_refine_phase(self):
        """Anchor file writes are allowed in refine phase."""
        from phase_filter import PhaseFilter, reset_phase_filter

        reset_phase_filter()
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions(
            "refine",
            [".egg-state/agent-anchors/refiner-xyz.json"],
        )
        assert result.allowed is True, f"Anchor writes should be allowed in refine: {result}"


class TestAnchorWritePermission:
    """Tests for check_anchor_write_permission method."""

    def test_agent_can_write_own_anchor(self):
        """Agent can write to its own anchor file."""
        from phase_filter import PhaseFilter, reset_phase_filter

        reset_phase_filter()
        pf = PhaseFilter()
        result = pf.check_anchor_write_permission(
            ".egg-state/agent-anchors/coder-abc12345.json",
            "coder-abc12345",
        )
        assert result.allowed is True

    def test_agent_cannot_write_other_anchor(self):
        """Agent cannot write to another agent's anchor file."""
        from phase_filter import PhaseFilter, reset_phase_filter

        reset_phase_filter()
        pf = PhaseFilter()
        result = pf.check_anchor_write_permission(
            ".egg-state/agent-anchors/tester-def67890.json",
            "coder-abc12345",
        )
        assert result.allowed is False

    def test_no_anchor_id_blocks_write(self):
        """Write without AGENT_ANCHOR_ID is blocked."""
        from phase_filter import PhaseFilter, reset_phase_filter

        reset_phase_filter()
        pf = PhaseFilter()
        result = pf.check_anchor_write_permission(
            ".egg-state/agent-anchors/coder-abc12345.json",
            None,
        )
        assert result.allowed is False

    def test_non_anchor_file_always_allowed(self):
        """Non-anchor files are not restricted by this check."""
        from phase_filter import PhaseFilter, reset_phase_filter

        reset_phase_filter()
        pf = PhaseFilter()
        result = pf.check_anchor_write_permission(
            "src/main.py",
            "coder-abc12345",
        )
        assert result.allowed is True

    def test_path_normalization(self):
        """Path with ./ prefix is normalized correctly."""
        from phase_filter import PhaseFilter, reset_phase_filter

        reset_phase_filter()
        pf = PhaseFilter()
        result = pf.check_anchor_write_permission(
            "./.egg-state/agent-anchors/coder-abc12345.json",
            "coder-abc12345",
        )
        assert result.allowed is True

    def test_empty_anchor_id_blocks_write(self):
        """Empty string anchor ID blocks write."""
        from phase_filter import PhaseFilter, reset_phase_filter

        reset_phase_filter()
        pf = PhaseFilter()
        result = pf.check_anchor_write_permission(
            ".egg-state/agent-anchors/test.json",
            "",
        )
        assert result.allowed is False


class TestModuleLevelAnchorFunction:
    """Tests for the module-level check_anchor_write_permission function."""

    def test_module_function_exists(self):
        """Module-level convenience function exists."""
        from phase_filter import check_anchor_write_permission

        assert callable(check_anchor_write_permission)

    def test_module_function_allows_own_anchor(self):
        """Module-level function allows writing own anchor."""
        from phase_filter import check_anchor_write_permission, reset_phase_filter

        reset_phase_filter()
        result = check_anchor_write_permission(
            ".egg-state/agent-anchors/coder-abc.json",
            "coder-abc",
        )
        assert result.allowed is True

    def test_module_function_blocks_other_anchor(self):
        """Module-level function blocks writing other agent's anchor."""
        from phase_filter import check_anchor_write_permission, reset_phase_filter

        reset_phase_filter()
        result = check_anchor_write_permission(
            ".egg-state/agent-anchors/tester-xyz.json",
            "coder-abc",
        )
        assert result.allowed is False
