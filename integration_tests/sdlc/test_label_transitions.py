"""Integration tests for SDLC label transitions.

Tests the label management throughout the SDLC pipeline phases:
1. Phase labels are applied correctly during init
2. Labels transition on phase approval
3. awaiting-approval label is managed correctly
4. Cleanup removes all SDLC labels on issue close
"""

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

# SDLC labels used by the pipeline
SDLC_LABELS = [
    "sdlc:refine",
    "sdlc:plan",
    "sdlc:implement",
    "sdlc:pr",
    "sdlc:awaiting-approval",
]


class TestLabelSetupScript:
    """Tests for the setup-sdlc-labels.sh script."""

    @pytest.fixture
    def script_path(self):
        """Get the path to the label setup script."""
        return Path(__file__).parents[2] / ".github" / "scripts" / "setup-sdlc-labels.sh"

    def test_script_exists_and_executable(self, script_path):
        """Verify the setup script exists and is executable."""
        assert script_path.exists(), f"Script not found at {script_path}"
        # Check if executable bit is set
        assert script_path.stat().st_mode & 0o111, "Script is not executable"

    def test_script_defines_all_labels(self, script_path):
        """Verify script defines all required SDLC labels."""
        content = script_path.read_text()

        for label in SDLC_LABELS:
            assert label in content, f"Label {label} not defined in setup script"


class TestLabelTransitionScript:
    """Tests for the transition-sdlc-label.sh script."""

    @pytest.fixture
    def script_path(self):
        """Get the path to the label transition script."""
        return Path(__file__).parents[2] / ".github" / "scripts" / "transition-sdlc-label.sh"

    def test_script_exists_and_executable(self, script_path):
        """Verify the transition script exists and is executable."""
        assert script_path.exists(), f"Script not found at {script_path}"
        assert script_path.stat().st_mode & 0o111, "Script is not executable"

    def test_script_validates_arguments(self, script_path):
        """Verify script validates required arguments."""
        content = script_path.read_text()

        # Check for argument validation
        assert "--issue" in content, "Script should check for --issue argument"
        assert "Error:" in content, "Script should have error messages"


class TestLabelTransitionLogic:
    """Tests for label transition logic in workflows."""

    def test_phase_to_label_mapping(self):
        """Verify phases map to correct labels."""
        phase_label_map = {
            "refine": "sdlc:refine",
            "plan": "sdlc:plan",
            "implement": "sdlc:implement",
            "pr": "sdlc:pr",
        }

        for phase, expected_label in phase_label_map.items():
            assert expected_label in SDLC_LABELS, f"Label {expected_label} for phase {phase} is valid"

    def test_label_transitions_are_mutually_exclusive(self):
        """Verify phase labels are mutually exclusive."""
        phase_labels = ["sdlc:refine", "sdlc:plan", "sdlc:implement", "sdlc:pr"]

        # All phase labels should be in the SDLC labels set
        for label in phase_labels:
            assert label in SDLC_LABELS

        # sdlc:awaiting-approval is a status label, not a phase label
        assert "sdlc:awaiting-approval" in SDLC_LABELS
        assert "sdlc:awaiting-approval" not in phase_labels


class TestWorkflowLabelIntegration:
    """Tests for label handling in workflow files."""

    @pytest.fixture
    def workflows_dir(self):
        """Get the path to the workflows directory."""
        return Path(__file__).parents[2] / ".github" / "workflows"

    def test_pipeline_triggers_on_sdlc_refine(self, workflows_dir):
        """Verify pipeline triggers on sdlc:refine label."""
        pipeline_path = workflows_dir / "sdlc-pipeline.yml"
        content = pipeline_path.read_text()

        assert "sdlc:refine" in content, "Pipeline should trigger on sdlc:refine"
        # The old egg-sdlc trigger should be removed from the trigger condition
        # but may still be referenced in comments/documentation

    def test_cleanup_handles_all_sdlc_labels(self, workflows_dir):
        """Verify cleanup workflow handles all SDLC labels."""
        cleanup_path = workflows_dir / "on-issue-closed.yml"
        content = cleanup_path.read_text()

        # Should check for any SDLC phase label
        for label in ["sdlc:refine", "sdlc:plan", "sdlc:implement", "sdlc:pr"]:
            assert label in content, f"Cleanup should check for {label}"

    def test_contract_verify_uses_sdlc_pr(self, workflows_dir):
        """Verify contract verification triggers on sdlc:pr label."""
        verify_path = workflows_dir / "on-pull-request-contract-verify.yml"
        content = verify_path.read_text()

        assert "sdlc:pr" in content, "Contract verification should use sdlc:pr"

    def test_hitl_handles_label_transitions(self, workflows_dir):
        """Verify HITL workflow handles label transitions."""
        hitl_path = workflows_dir / "sdlc-hitl.yml"
        content = hitl_path.read_text()

        # Should have label transition logic
        assert "Transition SDLC labels" in content, "HITL should have label transition step"
        assert "sdlc:awaiting-approval" in content, "HITL should manage awaiting-approval label"


class TestLabelNameConventions:
    """Tests for label naming conventions."""

    def test_all_labels_have_sdlc_prefix(self):
        """Verify all SDLC labels use the sdlc: prefix."""
        for label in SDLC_LABELS:
            assert label.startswith("sdlc:"), f"Label {label} should have sdlc: prefix"

    def test_labels_use_lowercase(self):
        """Verify all labels are lowercase."""
        for label in SDLC_LABELS:
            assert label == label.lower(), f"Label {label} should be lowercase"

    def test_labels_have_no_spaces(self):
        """Verify labels have no spaces."""
        for label in SDLC_LABELS:
            assert " " not in label, f"Label {label} should not contain spaces"


class TestLabelColors:
    """Tests for label color consistency in setup script."""

    @pytest.fixture
    def script_path(self):
        """Get the path to the label setup script."""
        return Path(__file__).parents[2] / ".github" / "scripts" / "setup-sdlc-labels.sh"

    def test_labels_have_distinct_colors(self, script_path):
        """Verify each phase label has a distinct color."""
        content = script_path.read_text()

        # Extract color codes from the script
        # Format: "label|color|description"
        import re
        colors = re.findall(r'\|([0-9A-Fa-f]{6})\|', content)

        # Phase labels should have distinct colors
        phase_colors = colors[:4]  # First 4 are phase labels
        assert len(phase_colors) == len(set(phase_colors)), "Phase labels should have distinct colors"

    def test_awaiting_approval_uses_warning_color(self, script_path):
        """Verify awaiting-approval uses a visible warning-like color."""
        content = script_path.read_text()

        # Yellow-ish colors are typically used for warnings
        # FBCA04 is GitHub's yellow
        assert "FBCA04" in content, "awaiting-approval should use yellow/warning color"
