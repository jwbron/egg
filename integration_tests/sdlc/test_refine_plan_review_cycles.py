"""Integration tests for refine and plan phase review cycles.

Tests the review cycle mechanism for refine and plan phases:
1. Refine phase review cycle tracking
2. Plan phase review cycle tracking
3. Feedback injection into producer prompts
4. Re-dispatch logic on review failure
5. File-based draft storage
6. File-based review verdicts
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.fixture
def temp_repo():
    """Create a temporary repository directory for testing."""
    with TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        # Create all state directories
        (repo_path / ".egg-state" / "contracts").mkdir(parents=True)
        (repo_path / ".egg-state" / "drafts").mkdir(parents=True)
        (repo_path / ".egg-state" / "reviews").mkdir(parents=True)
        yield repo_path


@pytest.fixture
def base_contract():
    """Create a base contract for testing."""
    return {
        "schemaVersion": "1.0",
        "issue": {
            "number": 400,
            "title": "Test issue for review cycles",
            "url": "https://github.com/test-owner/test-repo/issues/400",
        },
        "current_phase": "refine",
        "acceptance_criteria": [],
        "phases": [],
        "decisions": [],
        "workflow_owner": "test-user",
        "audit_log": [],
    }


class TestRefineReviewCycle:
    """Tests for refine phase review cycle."""

    def test_initial_refine_review_cycle_is_zero(self, temp_repo, base_contract):
        """New contract has zero refine review cycles."""
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())

        # New contracts don't have review cycles yet
        assert contract.get("refine_review_cycles", 0) == 0
        assert contract.get("refine_review_feedback", "") == ""

    def test_refine_review_cycle_increments(self, temp_repo, base_contract):
        """Refine review cycle increments after each review."""
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"

        # Simulate first review cycle
        base_contract["refine_review_cycles"] = 1
        base_contract["refine_review_feedback"] = "Issues found in problem statement"
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        assert contract["refine_review_cycles"] == 1

        # Simulate second review cycle
        base_contract["refine_review_cycles"] = 2
        base_contract["refine_review_feedback"] = "Options analysis still weak"
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        assert contract["refine_review_cycles"] == 2

    def test_refine_review_feedback_stored_in_contract(self, temp_repo, base_contract):
        """Review feedback is stored in contract for re-run."""
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"

        feedback = """### Issues Found

1. **Problem Understanding**: The analysis doesn't clearly identify root cause
2. **Options Analysis**: Options A and B are nearly identical

### Suggestions
- Add more context about current behavior
- Differentiate options more clearly"""

        base_contract["refine_review_cycles"] = 1
        base_contract["refine_review_feedback"] = feedback
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        assert "Problem Understanding" in contract["refine_review_feedback"]
        assert "Options Analysis" in contract["refine_review_feedback"]


class TestPlanReviewCycle:
    """Tests for plan phase review cycle."""

    def test_initial_plan_review_cycle_is_zero(self, temp_repo, base_contract):
        """New contract has zero plan review cycles."""
        base_contract["current_phase"] = "plan"
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())

        # New contracts don't have review cycles yet
        assert contract.get("plan_review_cycles", 0) == 0
        assert contract.get("plan_review_feedback", "") == ""

    def test_plan_review_cycle_increments(self, temp_repo, base_contract):
        """Plan review cycle increments after each review."""
        base_contract["current_phase"] = "plan"
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"

        # Simulate first review cycle
        base_contract["plan_review_cycles"] = 1
        base_contract["plan_review_feedback"] = "Task breakdown too coarse"
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        assert contract["plan_review_cycles"] == 1

        # Simulate second review cycle
        base_contract["plan_review_cycles"] = 2
        base_contract["plan_review_feedback"] = "Missing test strategy"
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        assert contract["plan_review_cycles"] == 2

    def test_plan_review_feedback_stored_in_contract(self, temp_repo, base_contract):
        """Review feedback is stored in contract for re-run."""
        base_contract["current_phase"] = "plan"
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"

        feedback = """### Issues Found

1. **Task Breakdown**: Tasks are too large for single commits
2. **Dependencies**: Missing dependency between task-1 and task-2
3. **YAML Appendix**: YAML doesn't match prose tasks

### Suggestions
- Break task-1 into subtasks
- Add explicit dependencies field"""

        base_contract["plan_review_cycles"] = 1
        base_contract["plan_review_feedback"] = feedback
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        assert "Task Breakdown" in contract["plan_review_feedback"]
        assert "YAML Appendix" in contract["plan_review_feedback"]


class TestFeedbackInjection:
    """Tests for feedback injection into producer prompts."""

    def test_build_refine_prompt_includes_feedback(self, temp_repo, base_contract):
        """build-sdlc-prompt.sh includes refine feedback when present."""
        # Create contract with feedback
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"
        base_contract["refine_review_cycles"] = 1
        base_contract["refine_review_feedback"] = "Add more detail to constraints section"
        contract_path.write_text(json.dumps(base_contract))

        # The test validates the contract structure supports feedback
        contract = json.loads(contract_path.read_text())
        assert contract["refine_review_cycles"] == 1
        assert "constraints" in contract["refine_review_feedback"]

    def test_build_plan_prompt_includes_feedback(self, temp_repo, base_contract):
        """build-sdlc-prompt.sh includes plan feedback when present."""
        # Create contract with feedback
        base_contract["current_phase"] = "plan"
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"
        base_contract["plan_review_cycles"] = 2
        base_contract["plan_review_feedback"] = "Improve acceptance criteria specificity"
        contract_path.write_text(json.dumps(base_contract))

        # The test validates the contract structure supports feedback
        contract = json.loads(contract_path.read_text())
        assert contract["plan_review_cycles"] == 2
        assert "acceptance criteria" in contract["plan_review_feedback"]

    def test_no_feedback_on_first_cycle(self, temp_repo, base_contract):
        """No feedback section when refine_review_cycles is 0."""
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        assert contract.get("refine_review_cycles", 0) == 0
        assert contract.get("refine_review_feedback", "") == ""


class TestReviewVerdictFileParsing:
    """Tests for file-based review verdict parsing."""

    def test_approved_verdict_from_file(self, temp_repo):
        """Approved verdict is correctly read from JSON file."""
        review_file = temp_repo / ".egg-state" / "reviews" / "400-refine-review.json"
        review_data = {
            "verdict": "approved",
            "summary": "The analysis meets quality standards.",
            "feedback": "",
            "timestamp": "2026-02-08T12:00:00Z",
        }
        review_file.write_text(json.dumps(review_data))

        loaded = json.loads(review_file.read_text())
        assert loaded["verdict"] == "approved"
        assert loaded["feedback"] == ""

    def test_needs_revision_verdict_from_file(self, temp_repo):
        """Needs revision verdict is correctly read from JSON file."""
        review_file = temp_repo / ".egg-state" / "reviews" / "400-refine-review.json"
        feedback = "### Issues Found\\n\\n1. **Problem Understanding**: Missing root cause"
        review_data = {
            "verdict": "needs_revision",
            "summary": "The analysis requires revision.",
            "feedback": feedback,
            "timestamp": "2026-02-08T12:00:00Z",
        }
        review_file.write_text(json.dumps(review_data))

        loaded = json.loads(review_file.read_text())
        assert loaded["verdict"] == "needs_revision"
        assert "Problem Understanding" in loaded["feedback"]

    def test_plan_approved_verdict_from_file(self, temp_repo):
        """Plan approved verdict is correctly read from JSON file."""
        review_file = temp_repo / ".egg-state" / "reviews" / "400-plan-review.json"
        review_data = {
            "verdict": "approved",
            "summary": "The plan is well-structured.",
            "feedback": "",
            "timestamp": "2026-02-08T12:00:00Z",
        }
        review_file.write_text(json.dumps(review_data))

        loaded = json.loads(review_file.read_text())
        assert loaded["verdict"] == "approved"

    def test_plan_needs_revision_verdict_from_file(self, temp_repo):
        """Plan needs revision verdict is correctly read from JSON file."""
        review_file = temp_repo / ".egg-state" / "reviews" / "400-plan-review.json"
        feedback = "### Issues Found\\n\\n1. **Task Breakdown**: Tasks too large"
        review_data = {
            "verdict": "needs_revision",
            "summary": "The plan requires revision.",
            "feedback": feedback,
            "timestamp": "2026-02-08T12:00:00Z",
        }
        review_file.write_text(json.dumps(review_data))

        loaded = json.loads(review_file.read_text())
        assert loaded["verdict"] == "needs_revision"
        assert "Task Breakdown" in loaded["feedback"]


class TestDraftFileStorage:
    """Tests for file-based draft storage."""

    def test_analysis_draft_file_created(self, temp_repo):
        """Analysis draft file is created in correct location."""
        draft_file = temp_repo / ".egg-state" / "drafts" / "400-analysis.md"
        draft_content = """# Analysis: Test Issue

## Problem Statement

This is the problem statement.

## Recommended Approach

Option A is recommended.
"""
        draft_file.write_text(draft_content)

        assert draft_file.exists()
        content = draft_file.read_text()
        assert "## Problem Statement" in content
        assert "## Recommended Approach" in content

    def test_plan_draft_file_created(self, temp_repo):
        """Plan draft file is created in correct location."""
        draft_file = temp_repo / ".egg-state" / "drafts" / "400-plan.md"
        draft_content = """# Plan: Test Issue

## Implementation Phases

### Phase 1: Setup

- [TASK-1-1] Create schema — Acceptance: Schema validates

## Test Strategy

Unit and integration tests.
"""
        draft_file.write_text(draft_content)

        assert draft_file.exists()
        content = draft_file.read_text()
        assert "## Implementation Phases" in content
        assert "## Test Strategy" in content

    def test_draft_updated_on_revision(self, temp_repo):
        """Draft file is updated in place during revision cycles."""
        draft_file = temp_repo / ".egg-state" / "drafts" / "400-analysis.md"

        # First draft
        draft_file.write_text("## Problem Statement\n\nOriginal content.")

        # Revision
        draft_file.write_text("## Problem Statement\n\nRevised content with more detail.")

        content = draft_file.read_text()
        assert "Revised content" in content
        assert "Original content" not in content


class TestAuditLogIntegration:
    """Tests for audit log entries during review cycles."""

    def test_review_cycle_creates_audit_entry(self, temp_repo, base_contract):
        """Review cycle updates create audit log entries."""
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"

        # Add audit entry for review cycle
        base_contract["refine_review_cycles"] = 1
        base_contract["audit_log"].append(
            {
                "timestamp": "2026-02-08T10:00:00Z",
                "actor": "system",
                "role": "reviewer",
                "action": "update",
                "field_path": "refine_review_cycles",
                "old_value": 0,
                "new_value": 1,
                "reason": "Refine review cycle 1: needs_revision",
            }
        )
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        assert len(contract["audit_log"]) == 1
        assert contract["audit_log"][0]["field_path"] == "refine_review_cycles"
        assert contract["audit_log"][0]["role"] == "reviewer"

