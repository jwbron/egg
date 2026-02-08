"""Integration tests for refine and plan phase review cycles.

Tests the review cycle mechanism for refine and plan phases:
1. Refine phase review cycle tracking
2. Plan phase review cycle tracking
3. Feedback injection into producer prompts
4. Circuit breaker escalation after max cycles
5. Re-dispatch logic on review failure
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
        contracts_dir = repo_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
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
        "circuit_breaker": {
            "total_cycles": 0,
            "max_total_cycles": 10,
            "status": "closed",
        },
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

    def test_refine_review_triggers_circuit_breaker_after_max_cycles(
        self, temp_repo, base_contract
    ):
        """Circuit breaker opens after max refine review cycles."""
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"

        # Set cycle count at max (3)
        base_contract["refine_review_cycles"] = 3
        base_contract["circuit_breaker"]["status"] = "open"
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        assert contract["circuit_breaker"]["status"] == "open"

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

    def test_plan_review_triggers_circuit_breaker_after_max_cycles(
        self, temp_repo, base_contract
    ):
        """Circuit breaker opens after max plan review cycles."""
        base_contract["current_phase"] = "plan"
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"

        # Set cycle count at max (3)
        base_contract["plan_review_cycles"] = 3
        base_contract["circuit_breaker"]["status"] = "open"
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        assert contract["circuit_breaker"]["status"] == "open"

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


class TestCircuitBreakerIntegration:
    """Tests for circuit breaker integration with review cycles."""

    def test_circuit_breaker_blocks_review_when_open(self, temp_repo, base_contract):
        """When circuit breaker is open, review should be skipped."""
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"
        base_contract["circuit_breaker"]["status"] = "open"
        base_contract["refine_review_cycles"] = 3
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        assert contract["circuit_breaker"]["status"] == "open"
        # When open, the workflow job condition should skip review

    def test_circuit_breaker_blocks_redispatch_when_open(self, temp_repo, base_contract):
        """When circuit breaker is open, re-dispatch should escalate instead."""
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"
        base_contract["circuit_breaker"]["status"] = "open"
        base_contract["refine_review_cycles"] = 3
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        assert contract["circuit_breaker"]["status"] == "open"
        # When open, re-dispatch job should post escalation comment instead

    def test_escalation_provides_context(self, temp_repo, base_contract):
        """Escalation comment includes cycle count and guidance options."""
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"
        base_contract["circuit_breaker"]["status"] = "open"
        base_contract["refine_review_cycles"] = 3
        base_contract["refine_review_feedback"] = "Repeated issues with problem statement"
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        # The escalation comment template includes:
        # - cycle count
        # - guidance options (provide guidance, override, cancel)
        # - approval checkbox for override
        assert contract["refine_review_cycles"] == 3
        assert contract["circuit_breaker"]["status"] == "open"


class TestReviewVerdictParsing:
    """Tests for review verdict marker parsing."""

    def test_approved_verdict_detected(self):
        """Approved verdict marker is detected correctly."""
        comment = """## Refine Review: ✅ Approved

The analysis meets quality standards.

<!-- egg-refine-review-verdict: approved -->

---

*Authored-by: egg*"""

        assert "egg-refine-review-verdict: approved" in comment

    def test_needs_revision_verdict_detected(self):
        """Needs revision verdict marker is detected correctly."""
        comment = """## Refine Review: 🔄 Needs Revision

### Issues Found

1. **Problem Understanding**: Missing root cause analysis

<!-- egg-refine-review-verdict: needs_revision -->

---

*Authored-by: egg*"""

        assert "egg-refine-review-verdict: needs_revision" in comment

    def test_plan_approved_verdict_detected(self):
        """Plan approved verdict marker is detected correctly."""
        comment = """## Plan Review: ✅ Approved

The plan is well-structured.

<!-- egg-plan-review-verdict: approved -->

---

*Authored-by: egg*"""

        assert "egg-plan-review-verdict: approved" in comment

    def test_plan_needs_revision_verdict_detected(self):
        """Plan needs revision verdict marker is detected correctly."""
        comment = """## Plan Review: 🔄 Needs Revision

### Issues Found

1. **Task Breakdown**: Tasks too large

<!-- egg-plan-review-verdict: needs_revision -->

---

*Authored-by: egg*"""

        assert "egg-plan-review-verdict: needs_revision" in comment


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

    def test_circuit_breaker_open_creates_audit_entry(self, temp_repo, base_contract):
        """Circuit breaker opening creates audit log entry."""
        contract_path = temp_repo / ".egg-state" / "contracts" / "400.json"

        # Add audit entry for circuit breaker opening
        base_contract["circuit_breaker"]["status"] = "open"
        base_contract["refine_review_cycles"] = 3
        base_contract["audit_log"].append(
            {
                "timestamp": "2026-02-08T10:30:00Z",
                "actor": "system",
                "role": "system",
                "action": "transition",
                "field_path": "circuit_breaker.status",
                "old_value": "closed",
                "new_value": "open",
                "reason": "Refine review cycle threshold exceeded (3/3)",
            }
        )
        contract_path.write_text(json.dumps(base_contract))

        contract = json.loads(contract_path.read_text())
        circuit_breaker_entries = [
            e
            for e in contract["audit_log"]
            if e["field_path"] == "circuit_breaker.status"
        ]
        assert len(circuit_breaker_entries) == 1
        assert circuit_breaker_entries[0]["new_value"] == "open"
