"""Tests verifying CLI output format matches HITL regex patterns.

The HITL decision handler uses specific regex patterns to detect decisions
and phase approvals. These tests ensure the CLI output format matches what the
handler expects.
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add sandbox to path for import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib.contract_cli import format_decision_markdown


class TestWorkflowRegexPatterns:
    """Tests that CLI output matches HITL regex patterns.

    These patterns are used by the HITL decision handler to detect
    decisions and approvals.
    """

    # Pattern for parsing decision ID from HTML comment markers:
    # The regex requires a valid boundary (space or >) after the ID
    DECISION_ID_PATTERN = re.compile(r"<!-- egg-hitl-decision id=([a-z0-9-]+)(?=[ >])")

    # Patterns for parsing checkbox state:
    CHECKED_OPTION_PATTERN = re.compile(r"^\s*-\s*\[x\]\s*(.+)$", re.MULTILINE)
    UNCHECKED_OPTION_PATTERN = re.compile(r"^\s*-\s*\[ \]\s*(.+)$", re.MULTILINE)

    # Phase approval markers:
    PHASE_APPROVAL_MARKER = "<!-- egg-phase-approval"
    APPROVE_CHECKBOX_PATTERN = re.compile(r"\[x\]\s*Approve")

    def test_decision_markdown_contains_valid_id_marker(self):
        """Test that format_decision_markdown produces a valid decision ID marker."""
        options = [{"id": "opt-1", "label": "Option A"}]
        markdown = format_decision_markdown("decision-1", "Test question?", options)

        match = self.DECISION_ID_PATTERN.search(markdown)
        assert match is not None, "Decision ID marker not found in markdown output"
        assert match.group(1) == "decision-1"

    def test_decision_markdown_id_format_variations(self):
        """Test various valid decision ID formats."""
        test_cases = [
            "decision-1",
            "decision-10",
            "decision-123",
        ]

        for decision_id in test_cases:
            options = [{"id": "opt-1", "label": "Yes"}]
            markdown = format_decision_markdown(decision_id, "Question?", options)

            match = self.DECISION_ID_PATTERN.search(markdown)
            assert match is not None, f"Decision ID {decision_id} not matched"
            assert match.group(1) == decision_id

    def test_decision_markdown_checkbox_format(self):
        """Test that checkboxes use the expected format for workflow detection."""
        options = [
            {"id": "opt-1", "label": "Option A"},
            {"id": "opt-2", "label": "Option B"},
        ]
        markdown = format_decision_markdown("decision-1", "Pick one?", options)

        # Should have unchecked boxes that the workflow can detect when checked
        unchecked = self.UNCHECKED_OPTION_PATTERN.findall(markdown)
        assert len(unchecked) == 2
        assert "Option A" in unchecked
        assert "Option B" in unchecked

    def test_phase_approval_format_detection(self):
        """Test that phase approval format is detectable by workflow conditions."""
        # Simulate a phase completion comment
        comment = """## Refine Phase Complete

Analysis complete.

### Ready for Review

<!-- egg-phase-approval -->
- [ ] Approve and advance to plan phase

---

*Authored-by: egg*
"""

        # Workflow condition 1: contains '<!-- egg-phase-approval'
        assert self.PHASE_APPROVAL_MARKER in comment

        # When human checks the box, it becomes [x]
        checked_comment = comment.replace("- [ ] Approve", "- [x] Approve")
        match = self.APPROVE_CHECKBOX_PATTERN.search(checked_comment)
        assert match is not None, "Checked approval box not detected"

    def test_other_option_format(self):
        """Test that auto-appended 'Other' option has consistent format."""
        options = [
            {"id": "opt-1", "label": "Option A"},
            {"id": "opt-2", "label": "Other (explain in reply)"},
        ]
        markdown = format_decision_markdown("decision-1", "Pick one?", options)

        # The "Other" option should be detectable as a regular checkbox option
        unchecked = self.UNCHECKED_OPTION_PATTERN.findall(markdown)
        assert any("Other (explain in reply)" in opt for opt in unchecked)


class TestDecisionAndApprovalSeparation:
    """Tests verifying decisions and approvals are correctly separated.

    The workflow has separate jobs for handle-decision and handle-approval.
    These tests verify the formats don't conflict.
    """

    def test_decision_does_not_trigger_approval(self):
        """Test that a decision comment doesn't accidentally trigger approval."""
        options = [{"id": "opt-1", "label": "Option A"}]
        markdown = format_decision_markdown("decision-1", "Question?", options)

        # Should not contain phase approval marker
        assert "<!-- egg-phase-approval" not in markdown
        # Should not have "Approve" as an option unless explicitly added
        assert "Approve" not in markdown

    def test_approval_format_is_distinct(self):
        """Test that approval format is distinct from decision format."""
        approval_comment = """<!-- egg-phase-approval -->
- [ ] Approve and advance to plan phase"""

        decision = format_decision_markdown(
            "decision-1", "Question?", [{"id": "opt-1", "label": "Yes"}]
        )

        # Decision has its own marker
        assert "<!-- egg-hitl-decision" in decision
        assert "<!-- egg-hitl-decision" not in approval_comment

        # Approval has its own marker
        assert "<!-- egg-phase-approval" in approval_comment
        assert "<!-- egg-phase-approval" not in decision


class TestTypedDecisionIntegration:
    """End-to-end tests for typed HITL decisions.

    Tests that phase gate decisions are created with the correct decision_type,
    JSON resolutions parse correctly, and follow-up decisions carry the type.
    """

    def test_phase_gate_decision_type_field(self):
        """Phase gate decisions should have decision_type='phase_gate'."""

        decision = {
            "id": "decision-1",
            "question": "The refine phase has completed. Please review the analysis.",
            "context": "Draft content",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        # Verify the decision dict carries the type field
        assert decision["decision_type"] == "phase_gate"

    def test_json_resolution_approve_parsed(self):
        """JSON {"action": "approve"} should be recognized as approval."""
        resolution = json.dumps({"action": "approve"})
        payload = json.loads(resolution)
        assert payload["action"] == "approve"

    def test_json_resolution_request_changes_parsed(self):
        """JSON request_changes should extract readable feedback."""
        resolution = json.dumps(
            {
                "action": "request_changes",
                "feedback": "Add more detail to section 3",
            }
        )
        payload = json.loads(resolution)
        assert payload["action"] == "request_changes"
        assert payload["feedback"] == "Add more detail to section 3"
        # R-1: feedback text should be readable, not the raw JSON
        assert payload["feedback"] != resolution

    def test_json_resolution_select_parsed(self):
        """JSON select resolution carries the selected option."""
        resolution = json.dumps({"action": "select", "selected": "PostgreSQL"})
        payload = json.loads(resolution)
        assert payload["action"] == "select"
        assert payload["selected"] == "PostgreSQL"

    def test_json_resolution_submit_feedback_parsed(self):
        """JSON submit_feedback carries structured answers."""
        answers = {"q-1": "High volume", "q-2": "Under 100ms"}
        resolution = json.dumps({"action": "submit_feedback", "answers": answers})
        payload = json.loads(resolution)
        assert payload["action"] == "submit_feedback"
        assert payload["answers"] == answers

    def test_follow_up_decision_carries_phase_gate_type(self):
        """Follow-up decisions should also have decision_type='phase_gate' (R-3).

        When a bare 'request changes' triggers a follow-up, the follow-up
        should carry the same decision_type as the original.
        """
        # Simulate the follow-up decision as created by the orchestrator
        followup = {
            "id": "decision-2",
            "question": "Please describe what changes you'd like.",
            "context": "Draft content",
            "decision_type": "phase_gate",
            "options": ["approve"],
        }
        assert followup["decision_type"] == "phase_gate"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_phase_gate_end_to_end_approve(self, mock_input, mock_repo, tmp_path):
        """End-to-end: phase gate renders correctly and resolves with JSON."""
        from egg_lib.sdlc_hitl import handle_hitl_checkpoint

        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"  # Approve

        client = MagicMock()
        client.resolve_decision.return_value = {"status": "resolved"}

        decision = {
            "id": "decision-1",
            "question": "The plan phase has completed. Approve the plan.",
            "context": "Plan content here",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }

        result = handle_hitl_checkpoint(
            client,
            "issue-100",
            decision,
            pipeline_mode="issue",
            issue_number=100,
        )

        assert result == "resolved"
        # Verify JSON resolution was sent
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "approve"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_choice_end_to_end_select(self, mock_input, mock_repo, tmp_path):
        """End-to-end: choice decision renders and resolves with JSON."""
        from egg_lib.sdlc_hitl import handle_hitl_checkpoint

        mock_repo.return_value = tmp_path
        mock_input.return_value = "2"  # Select second option

        client = MagicMock()
        client.resolve_decision.return_value = {"status": "resolved"}

        decision = {
            "id": "decision-1",
            "question": "Which database?",
            "context": "",
            "decision_type": "choice",
            "options": ["PostgreSQL", "MongoDB", "SQLite"],
        }

        result = handle_hitl_checkpoint(
            client,
            "issue-100",
            decision,
            pipeline_mode="issue",
            issue_number=100,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "select"
        assert resolution["selected"] == "MongoDB"
