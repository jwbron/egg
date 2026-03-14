"""Tests for egg_contracts.hitl module."""

from datetime import UTC, datetime, timedelta

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


class TestHitlOption:
    """Tests for HitlOption dataclass."""

    def test_create_option(self):
        """Test creating an HITL option."""
        option = HitlOption(
            id=HitlOptionId.PROVIDE_CONTEXT,
            label="Provide additional context",
            category=HitlDecisionCategory.GUIDANCE,
        )
        assert option.id == HitlOptionId.PROVIDE_CONTEXT
        assert option.category == HitlDecisionCategory.GUIDANCE
        assert option.checked is False

    def test_checked_option(self):
        """Test creating a checked option."""
        option = HitlOption(
            id="custom_option",
            label="Custom option",
            category=HitlDecisionCategory.OVERRIDE,
            checked=True,
        )
        assert option.checked is True


class TestHitlCheckboxState:
    """Tests for HitlCheckboxState dataclass."""

    def test_get_checked_options(self):
        """Test getting checked options."""
        state = HitlCheckboxState(
            options=[
                HitlOption(
                    id="opt1",
                    label="Option 1",
                    category=HitlDecisionCategory.GUIDANCE,
                    checked=True,
                ),
                HitlOption(
                    id="opt2",
                    label="Option 2",
                    category=HitlDecisionCategory.GUIDANCE,
                    checked=False,
                ),
                HitlOption(
                    id="opt3",
                    label="Option 3",
                    category=HitlDecisionCategory.OVERRIDE,
                    checked=True,
                ),
            ],
            has_changes=True,
            debounce_until=None,
            raw_comment="test",
        )
        checked = state.get_checked_options()
        assert len(checked) == 2
        assert all(opt.checked for opt in checked)

    def test_get_checked_by_category(self):
        """Test getting checked options by category."""
        state = HitlCheckboxState(
            options=[
                HitlOption(
                    id="opt1",
                    label="Option 1",
                    category=HitlDecisionCategory.GUIDANCE,
                    checked=True,
                ),
                HitlOption(
                    id="opt2",
                    label="Option 2",
                    category=HitlDecisionCategory.OVERRIDE,
                    checked=True,
                ),
            ],
            has_changes=True,
            debounce_until=None,
            raw_comment="test",
        )
        guidance = state.get_checked_by_category(HitlDecisionCategory.GUIDANCE)
        assert len(guidance) == 1
        assert guidance[0].id == "opt1"

    def test_to_dict(self):
        """Test conversion to dictionary."""
        state = HitlCheckboxState(
            options=[
                HitlOption(
                    id="opt1", label="Test", category=HitlDecisionCategory.GUIDANCE, checked=True
                ),
            ],
            has_changes=True,
            debounce_until=datetime.now(UTC) + timedelta(seconds=30),
            raw_comment="test",
        )
        data = state.to_dict()
        assert "options" in data
        assert data["has_changes"] is True
        assert data["debounce_until"] is not None


class TestGenerateCheckboxBlock:
    """Tests for generate_checkbox_block."""

    def test_generates_markdown(self):
        """Test that valid markdown is generated."""
        options = [
            HitlOption(id="opt1", label="Option 1", category=HitlDecisionCategory.GUIDANCE),
            HitlOption(id="opt2", label="Option 2", category=HitlDecisionCategory.GUIDANCE),
        ]
        block = generate_checkbox_block(options, HitlDecisionCategory.GUIDANCE, "Test Title")

        assert "### Test Title" in block
        assert "<!-- HITL-DECISION: guidance -->" in block
        assert "- [ ] Option 1" in block
        assert "- [ ] Option 2" in block

    def test_checked_options(self):
        """Test that checked options have [x]."""
        options = [
            HitlOption(
                id="opt1", label="Checked", category=HitlDecisionCategory.GUIDANCE, checked=True
            ),
        ]
        block = generate_checkbox_block(options, HitlDecisionCategory.GUIDANCE)

        assert "- [x] Checked" in block

    def test_includes_description(self):
        """Test that description is included if present."""
        options = [
            HitlOption(
                id="opt1",
                label="Option",
                category=HitlDecisionCategory.GUIDANCE,
                description="Test description",
            ),
        ]
        block = generate_checkbox_block(options, HitlDecisionCategory.GUIDANCE)

        assert "_Test description_" in block


class TestGenerateDebounceNotice:
    """Tests for generate_debounce_notice."""

    def test_active_countdown(self):
        """Test notice for active countdown."""
        notice = generate_debounce_notice(25)

        assert "25 seconds" in notice
        assert "⏳" in notice
        assert "Debounce active" in notice

    def test_finalized(self):
        """Test notice when countdown is complete."""
        notice = generate_debounce_notice(0)

        assert "✅" in notice
        assert "finalized" in notice

    def test_includes_summary(self):
        """Test that decision summary is included."""
        notice = generate_debounce_notice(10, "Provide additional context")

        assert "Provide additional context" in notice


class TestGenerateFullHitlBlock:
    """Tests for generate_full_hitl_block."""

    def test_includes_all_categories(self):
        """Test that all option categories are included."""
        block = generate_full_hitl_block(issue_number=123)

        assert "guidance" in block.lower()
        assert "override" in block.lower()
        assert "manual" in block.lower()
        assert "HITL-DECISION" in block

    def test_includes_task_context(self):
        """Test that task context is included."""
        block = generate_full_hitl_block(issue_number=123, stuck_task_id="task-1-1")

        assert "task-1-1" in block

    def test_includes_debounce_notice(self):
        """Test that debounce notice is included."""
        block = generate_full_hitl_block(issue_number=123, include_debounce_notice=True)

        assert "seconds" in block.lower()

    def test_no_debounce_notice(self):
        """Test that debounce notice can be excluded."""
        block = generate_full_hitl_block(issue_number=123, include_debounce_notice=False)

        assert "Debounce active" not in block


class TestParseCheckboxState:
    """Tests for parse_checkbox_state."""

    def test_parses_unchecked(self):
        """Test parsing unchecked checkboxes."""
        comment = """
<!-- HITL-DECISION: guidance -->
- [ ] Option 1
- [ ] Option 2
"""
        state = parse_checkbox_state(comment)

        assert len(state.options) == 2
        assert all(not opt.checked for opt in state.options)

    def test_parses_checked(self):
        """Test parsing checked checkboxes."""
        comment = """
<!-- HITL-DECISION: guidance -->
- [x] Option 1
- [ ] Option 2
"""
        state = parse_checkbox_state(comment)

        checked = [opt for opt in state.options if opt.checked]
        assert len(checked) == 1
        assert checked[0].label == "Option 1"

    def test_parses_multiple_categories(self):
        """Test parsing multiple categories."""
        comment = """
<!-- HITL-DECISION: guidance -->
- [x] Provide context

<!-- HITL-DECISION: override -->
- [ ] Skip tasks
"""
        state = parse_checkbox_state(comment)

        guidance = [opt for opt in state.options if opt.category == HitlDecisionCategory.GUIDANCE]
        override = [opt for opt in state.options if opt.category == HitlDecisionCategory.OVERRIDE]

        assert len(guidance) == 1
        assert len(override) == 1

    def test_detects_changes(self):
        """Test that changes from previous state are detected."""
        previous = HitlCheckboxState(
            options=[
                HitlOption(
                    id="opt1",
                    label="Option 1",
                    category=HitlDecisionCategory.GUIDANCE,
                    checked=False,
                ),
            ],
            has_changes=False,
            debounce_until=None,
            raw_comment="",
        )

        new_comment = """
<!-- HITL-DECISION: guidance -->
- [x] Option 1
"""
        state = parse_checkbox_state(new_comment, previous_state=previous)
        assert state.has_changes is True

    def test_maps_known_options(self):
        """Test that known option labels are mapped to IDs."""
        comment = """
<!-- HITL-DECISION: guidance -->
- [x] I will provide additional context or requirements below
"""
        state = parse_checkbox_state(comment)

        assert len(state.options) == 1
        assert state.options[0].id == HitlOptionId.PROVIDE_CONTEXT


class TestUpdateCommentWithCountdown:
    """Tests for update_comment_with_countdown."""

    def test_updates_countdown(self):
        """Test updating countdown time."""
        original = """
Some content

> ⏳ **Debounce active:** Decision will be processed in **30 seconds**.
>
> _Making additional changes will reset the timer._
"""
        updated = update_comment_with_countdown(original, 15)

        assert "15 seconds" in updated
        assert "30 seconds" not in updated

    def test_handles_finalized(self):
        """Test updating to finalized state."""
        original = """
> ⏳ **Debounce active:** Decision will be processed in **5 seconds**.
"""
        updated = update_comment_with_countdown(original, 0)

        assert "finalized" in updated.lower()


class TestCalculateDebounceRemaining:
    """Tests for calculate_debounce_remaining."""

    def test_no_debounce(self):
        """Test when no debounce is set."""
        remaining = calculate_debounce_remaining(None)
        assert remaining == 0

    def test_expired(self):
        """Test when debounce has expired."""
        past = datetime.now(UTC) - timedelta(seconds=10)
        remaining = calculate_debounce_remaining(past)
        assert remaining == 0

    def test_active(self):
        """Test when debounce is active."""
        future = datetime.now(UTC) + timedelta(seconds=20)
        remaining = calculate_debounce_remaining(future)
        assert 18 <= remaining <= 21  # Allow small timing variance


class TestShouldProcessDecision:
    """Tests for should_process_decision."""

    def test_no_checked_options(self):
        """Test when no options are checked."""
        state = HitlCheckboxState(
            options=[
                HitlOption(
                    id="opt1", label="Test", category=HitlDecisionCategory.GUIDANCE, checked=False
                ),
            ],
            has_changes=False,
            debounce_until=None,
            raw_comment="",
        )
        assert should_process_decision(state) is False

    def test_debounce_active(self):
        """Test when debounce is still active."""
        state = HitlCheckboxState(
            options=[
                HitlOption(
                    id="opt1", label="Test", category=HitlDecisionCategory.GUIDANCE, checked=True
                ),
            ],
            has_changes=True,
            debounce_until=datetime.now(UTC) + timedelta(seconds=30),
            raw_comment="",
        )
        assert should_process_decision(state) is False

    def test_ready_to_process(self):
        """Test when ready to process (checked options, debounce expired)."""
        state = HitlCheckboxState(
            options=[
                HitlOption(
                    id="opt1", label="Test", category=HitlDecisionCategory.GUIDANCE, checked=True
                ),
            ],
            has_changes=True,
            debounce_until=datetime.now(UTC) - timedelta(seconds=5),  # Expired
            raw_comment="",
        )
        assert should_process_decision(state) is True


class TestStartDebounce:
    """Tests for start_debounce."""

    def test_sets_debounce(self):
        """Test that debounce is set correctly."""
        state = HitlCheckboxState(
            options=[],
            has_changes=False,
            debounce_until=None,
            raw_comment="",
        )
        new_state = start_debounce(state)

        assert new_state.debounce_until is not None
        remaining = calculate_debounce_remaining(new_state.debounce_until)
        assert remaining > 0

    def test_custom_debounce_time(self):
        """Test custom debounce time."""
        state = HitlCheckboxState(
            options=[],
            has_changes=False,
            debounce_until=None,
            raw_comment="",
        )
        new_state = start_debounce(state, debounce_seconds=60)

        remaining = calculate_debounce_remaining(new_state.debounce_until)
        assert 55 <= remaining <= 61


class TestDefaultDebounceSeconds:
    """Tests for DEFAULT_DEBOUNCE_SECONDS."""

    def test_default_value(self):
        """Test default debounce is 30 seconds."""
        assert DEFAULT_DEBOUNCE_SECONDS == 30
