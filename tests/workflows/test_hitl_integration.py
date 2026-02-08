"""Tests verifying CLI output format matches workflow regex patterns.

The sdlc-hitl.yml workflow uses specific regex patterns to detect HITL decisions
and phase approvals. These tests ensure the CLI output format matches what the
workflow expects.
"""

import re
import sys
from pathlib import Path

# Add sandbox to path for import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib.contract_cli import format_decision_markdown


class TestWorkflowRegexPatterns:
    """Tests that CLI output matches workflow regex patterns.

    These patterns are extracted from .github/workflows/sdlc-hitl.yml
    """

    # Pattern from sdlc-hitl.yml step "Parse decision changes":
    # grep -oP '<!-- egg-hitl-decision id=\K[a-z0-9-]+' | head -1
    # The regex requires a valid boundary (space or >) after the ID to match workflow behavior
    DECISION_ID_PATTERN = re.compile(r"<!-- egg-hitl-decision id=([a-z0-9-]+)(?=[ >])")

    # Pattern from sdlc-hitl.yml step "Parse decision changes":
    # grep -oP '^\s*-\s*\[x\]\s*\K.+'
    CHECKED_OPTION_PATTERN = re.compile(r"^\s*-\s*\[x\]\s*(.+)$", re.MULTILINE)
    UNCHECKED_OPTION_PATTERN = re.compile(r"^\s*-\s*\[ \]\s*(.+)$", re.MULTILINE)

    # Phase approval markers from sdlc-hitl.yml job "handle-approval":
    # contains(github.event.comment.body, '<!-- egg-phase-approval') ||
    # contains(github.event.comment.body, '[x] Approve')
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
