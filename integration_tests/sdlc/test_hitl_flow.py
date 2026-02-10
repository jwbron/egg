"""Integration tests for SDLC pipeline HITL (Human-in-the-Loop) flow.

Tests the human decision checkpoint mechanism where:
1. HITL checkboxes are generated for stuck tasks
2. Checkbox state is parsed from comments
3. Debounce timing prevents accidental triggers
4. Decisions are processed after debounce expires
5. Pipeline resumes after human guidance
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from egg_contracts import (
    Contract,
    Decision,
    DecisionType,
    IssueInfo,
    Phase,
    PhaseStatus,
    PipelinePhase,
    Role,
    Task,
    TaskStatus,
    apply_mutation,
    load_contract,
    save_contract,
)
from egg_contracts.hitl import (
    DEFAULT_DEBOUNCE_SECONDS,
    HitlCheckboxState,
    HitlDecisionCategory,
    HitlOption,
    HitlOptionId,
    calculate_debounce_remaining,
    generate_checkbox_block,
    generate_debounce_notice,
    generate_full_hitl_block,
    parse_checkbox_state,
    should_process_decision,
    start_debounce,
    update_comment_with_countdown,
)


@pytest.fixture
def temp_repo():
    """Create a temporary repository directory for testing."""
    with TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        contracts_dir = repo_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        yield repo_path


@pytest.fixture
def sample_issue_info():
    """Create sample issue info for testing."""
    return IssueInfo(
        number=400,
        title="Feature requiring HITL",
        url="https://github.com/test-owner/test-repo/issues/400",
    )


@pytest.fixture
def escalated_contract(sample_issue_info):
    """Create a contract that has escalated and needs HITL."""
    return Contract(
        schemaVersion="1.0",
        issue=sample_issue_info,
        current_phase=PipelinePhase.IMPLEMENT,
        phases=[
            Phase(
                id="phase-1",
                name="Stuck Phase",
                status=PhaseStatus.IN_PROGRESS,
                escalated=True,
                escalation_reason="Task threshold exceeded",
                tasks=[
                    Task(
                        id="task-1-1",
                        description="Stuck task",
                        status=TaskStatus.IN_PROGRESS,
                        review_cycles=3,
                        escalated=True,
                    ),
                ],
            ),
        ],
    )


class TestHitlCheckboxGeneration:
    """Tests for HITL checkbox block generation."""

    def test_generate_checkbox_block_basic(self):
        """Generate a basic checkbox block."""
        options = [
            HitlOption(
                id=HitlOptionId.PROVIDE_CONTEXT,
                label="Provide additional context",
                category=HitlDecisionCategory.GUIDANCE,
            ),
            HitlOption(
                id=HitlOptionId.ADJUST_CRITERIA,
                label="Adjust acceptance criteria",
                category=HitlDecisionCategory.GUIDANCE,
            ),
        ]

        block = generate_checkbox_block(
            options,
            HitlDecisionCategory.GUIDANCE,
            title="Guidance Options",
        )

        assert "### Guidance Options" in block
        assert "[ ] Provide additional context" in block
        assert "[ ] Adjust acceptance criteria" in block
        assert "HITL-DECISION: guidance" in block

    def test_generate_checkbox_block_with_checked(self):
        """Generate checkbox block with pre-checked option."""
        options = [
            HitlOption(
                id=HitlOptionId.MARK_COMPLETE,
                label="Mark as complete",
                category=HitlDecisionCategory.OVERRIDE,
                checked=True,
            ),
        ]

        block = generate_checkbox_block(options, HitlDecisionCategory.OVERRIDE)
        assert "[x] Mark as complete" in block

    def test_generate_full_hitl_block(self):
        """Generate complete HITL decision block."""
        block = generate_full_hitl_block(
            issue_number=400,
            stuck_task_id="task-1-1",
            include_debounce_notice=True,
        )

        # Check structure
        assert "Human Decision Required for task `task-1-1`" in block
        assert "Option 1: Provide Guidance" in block
        assert "Option 2: Override" in block
        assert "Option 3: Manual Intervention" in block

        # Check debounce notice
        assert "Debounce active" in block
        assert f"{DEFAULT_DEBOUNCE_SECONDS} seconds" in block

    def test_generate_debounce_notice_active(self):
        """Generate debounce notice when timer is active."""
        notice = generate_debounce_notice(25, "Marked task as complete")

        assert "25 seconds" in notice
        assert "Debounce active" in notice
        assert "Making additional changes will reset the timer" in notice

    def test_generate_debounce_notice_expired(self):
        """Generate debounce notice when timer has expired."""
        notice = generate_debounce_notice(0)

        assert "Decision finalized" in notice
        assert "Processing now" in notice


class TestHitlCheckboxParsing:
    """Tests for parsing checkbox state from comments."""

    def test_parse_unchecked_options(self):
        """Parse comment with unchecked options."""
        comment = """
### Guidance Options
<!-- HITL-DECISION: guidance -->
- [ ] Provide additional context
- [ ] Adjust acceptance criteria
"""
        state = parse_checkbox_state(comment)

        assert len(state.options) == 2
        assert all(not opt.checked for opt in state.options)

    def test_parse_checked_option(self):
        """Parse comment with checked option."""
        comment = """
### Override Options
<!-- HITL-DECISION: override -->
- [x] Mark current tasks as complete
- [ ] Skip remaining tasks
"""
        state = parse_checkbox_state(comment)

        assert len(state.options) == 2
        checked = state.get_checked_options()
        assert len(checked) == 1
        assert "complete" in checked[0].label.lower()

    def test_parse_multiple_checked(self):
        """Parse comment with multiple checked options."""
        comment = """
<!-- HITL-DECISION: guidance -->
- [x] I will provide additional context
- [x] Break this task into smaller sub-tasks
- [ ] The acceptance criteria should be adjusted
"""
        state = parse_checkbox_state(comment)

        checked = state.get_checked_options()
        assert len(checked) == 2

    def test_parse_mixed_categories(self):
        """Parse comment with options from multiple categories."""
        comment = """
### Guidance
<!-- HITL-DECISION: guidance -->
- [x] Provide context

### Override
<!-- HITL-DECISION: override -->
- [ ] Mark complete
"""
        state = parse_checkbox_state(comment)

        guidance = state.get_checked_by_category(HitlDecisionCategory.GUIDANCE)
        override = state.get_checked_by_category(HitlDecisionCategory.OVERRIDE)

        assert len(guidance) == 1
        assert len(override) == 0

    def test_detect_changes_from_previous_state(self):
        """Detect changes between previous and current state."""
        previous_comment = """
<!-- HITL-DECISION: guidance -->
- [ ] Provide context
"""
        previous_state = parse_checkbox_state(previous_comment)

        current_comment = """
<!-- HITL-DECISION: guidance -->
- [x] Provide context
"""
        current_state = parse_checkbox_state(current_comment, previous_state)

        assert current_state.has_changes is True


class TestDebounceHandling:
    """Tests for debounce timing logic."""

    def test_calculate_debounce_remaining_active(self):
        """Calculate remaining seconds when debounce is active."""
        future = datetime.now(UTC) + timedelta(seconds=20)
        remaining = calculate_debounce_remaining(future)

        assert 19 <= remaining <= 20

    def test_calculate_debounce_remaining_expired(self):
        """Calculate remaining returns 0 when debounce expired."""
        past = datetime.now(UTC) - timedelta(seconds=10)
        remaining = calculate_debounce_remaining(past)

        assert remaining == 0

    def test_calculate_debounce_remaining_none(self):
        """Calculate remaining returns 0 when no debounce set."""
        remaining = calculate_debounce_remaining(None)
        assert remaining == 0

    def test_start_debounce(self):
        """Start debounce sets future expiration time."""
        options = [
            HitlOption(
                id=HitlOptionId.PROVIDE_CONTEXT,
                label="Test",
                category=HitlDecisionCategory.GUIDANCE,
                checked=True,
            )
        ]
        state = HitlCheckboxState(
            options=options,
            has_changes=True,
            debounce_until=None,
            raw_comment="test",
        )

        new_state = start_debounce(state, debounce_seconds=30)

        assert new_state.debounce_until is not None
        remaining = calculate_debounce_remaining(new_state.debounce_until)
        assert 29 <= remaining <= 30

    def test_should_process_no_checked_options(self):
        """Should not process if no options are checked."""
        options = [
            HitlOption(
                id=HitlOptionId.PROVIDE_CONTEXT,
                label="Test",
                category=HitlDecisionCategory.GUIDANCE,
                checked=False,
            )
        ]
        state = HitlCheckboxState(
            options=options,
            has_changes=False,
            debounce_until=datetime.now(UTC) - timedelta(seconds=10),
            raw_comment="test",
        )

        assert should_process_decision(state) is False

    def test_should_process_debounce_not_expired(self):
        """Should not process if debounce hasn't expired."""
        options = [
            HitlOption(
                id=HitlOptionId.PROVIDE_CONTEXT,
                label="Test",
                category=HitlDecisionCategory.GUIDANCE,
                checked=True,
            )
        ]
        state = HitlCheckboxState(
            options=options,
            has_changes=True,
            debounce_until=datetime.now(UTC) + timedelta(seconds=20),
            raw_comment="test",
        )

        assert should_process_decision(state) is False

    def test_should_process_ready(self):
        """Should process when checked and debounce expired."""
        options = [
            HitlOption(
                id=HitlOptionId.PROVIDE_CONTEXT,
                label="Test",
                category=HitlDecisionCategory.GUIDANCE,
                checked=True,
            )
        ]
        state = HitlCheckboxState(
            options=options,
            has_changes=True,
            debounce_until=datetime.now(UTC) - timedelta(seconds=10),
            raw_comment="test",
        )

        assert should_process_decision(state) is True


class TestCommentUpdates:
    """Tests for updating comments with countdown."""

    def test_update_comment_countdown(self):
        """Update comment to show current countdown."""
        original = """
## Decision Required

<!-- HITL-DECISION: guidance -->
- [x] Provide context

---

> ⏳ **Debounce active:** Decision will be processed in **30 seconds**.
>
> _Making additional changes will reset the timer._
"""
        updated = update_comment_with_countdown(original, 15)

        assert "15 seconds" in updated
        assert "30 seconds" not in updated

    def test_update_comment_finalized(self):
        """Update comment when decision is finalized."""
        original = """
> ⏳ **Debounce active:** Decision will be processed in **5 seconds**.
"""
        updated = update_comment_with_countdown(original, 0)

        assert "Decision finalized" in updated
        assert "Processing now" in updated


class TestHitlContractIntegration:
    """Integration tests for HITL with contract state."""

    def test_add_hitl_decision_to_contract(self, temp_repo, escalated_contract):
        """HITL decision can be added to contract."""
        escalated_contract.decisions.append(
            Decision(
                id="decision-1",
                question="How should we proceed with stuck task?",
                type=DecisionType.HITL,
                resolved=False,
            )
        )
        save_contract(escalated_contract, temp_repo)

        loaded = load_contract(400, temp_repo)
        assert len(loaded.decisions) == 1
        assert loaded.decisions[0].resolved is False

    def test_human_resolves_decision(self, temp_repo, escalated_contract):
        """Human can resolve a HITL decision."""
        escalated_contract.decisions.append(
            Decision(
                id="decision-1",
                question="How should we proceed?",
                type=DecisionType.HITL,
                resolved=False,
            )
        )
        save_contract(escalated_contract, temp_repo)

        # Human resolves the decision
        contract = load_contract(400, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.HUMAN,
            actor="reviewer",
            field_path="decisions.0.resolved",
            new_value=True,
            reason="Provided additional context",
        )
        assert result.success

        result = apply_mutation(
            contract=result.contract,
            role=Role.HUMAN,
            actor="reviewer",
            field_path="decisions.0.resolution",
            new_value="Adjusted acceptance criteria and provided examples",
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Verify
        loaded = load_contract(400, temp_repo)
        assert loaded.decisions[0].resolved is True
        assert "examples" in loaded.decisions[0].resolution

class TestHitlEndToEnd:
    """End-to-end tests for HITL workflow."""

    def test_full_hitl_cycle(self, temp_repo, escalated_contract):
        """Complete HITL cycle from escalation to resolution."""
        # Step 1: Contract is escalated (already in fixture)
        save_contract(escalated_contract, temp_repo)

        # Step 2: Generate HITL block for the stuck task
        block = generate_full_hitl_block(
            issue_number=400,
            stuck_task_id="task-1-1",
        )
        assert "task-1-1" in block

        # Step 3: Simulate human checking a box
        comment_with_selection = """
<!-- HITL-DECISION: guidance -->
- [x] I will provide additional context or requirements below
- [ ] The acceptance criteria should be adjusted
"""
        state = parse_checkbox_state(comment_with_selection)
        assert len(state.get_checked_options()) == 1

        # Step 4: Start debounce
        state = start_debounce(state, debounce_seconds=30)
        assert calculate_debounce_remaining(state.debounce_until) > 0

        # Step 5: Simulate debounce expiring (in real system, time passes)
        state.debounce_until = datetime.now(UTC) - timedelta(seconds=10)
        assert should_process_decision(state) is True

        # Step 6: Decision is ready to be processed - pipeline can resume
        # The human's decision has been captured and debounce has expired
