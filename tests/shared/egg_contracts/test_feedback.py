"""Tests for egg_contracts.feedback module."""

from datetime import UTC, datetime, timedelta

from egg_contracts.feedback import (
    DEFAULT_DEBOUNCE_SECONDS,
    FeedbackQuestionInput,
    ParsedFeedbackResponse,
    calculate_feedback_debounce_remaining,
    generate_feedback_comment,
    generate_feedback_id,
    parse_feedback_comment,
    should_process_feedback,
    start_feedback_debounce,
    update_feedback_with_countdown,
)


class TestGenerateFeedbackId:
    """Tests for generate_feedback_id."""

    def test_first_id(self):
        """Test generating first feedback ID."""
        fid = generate_feedback_id()
        assert fid == "feedback-1"

    def test_with_no_existing(self):
        """Test with empty existing IDs list."""
        fid = generate_feedback_id([])
        assert fid == "feedback-1"

    def test_increments_from_existing(self):
        """Test that it increments from existing IDs."""
        fid = generate_feedback_id(["feedback-1", "feedback-2"])
        assert fid == "feedback-3"

    def test_handles_gaps(self):
        """Test handling gaps in numbering."""
        fid = generate_feedback_id(["feedback-1", "feedback-5"])
        assert fid == "feedback-6"


class TestGenerateFeedbackComment:
    """Tests for generate_feedback_comment."""

    def test_includes_marker(self):
        """Test that the feedback marker is included."""
        questions = [FeedbackQuestionInput(id="Q1", question="What is the expected volume?")]
        comment = generate_feedback_comment("feedback-1", questions)

        assert "<!-- egg-feedback id=feedback-1 -->" in comment

    def test_includes_questions(self):
        """Test that all questions are included."""
        questions = [
            FeedbackQuestionInput(id="Q1", question="What is the expected volume?"),
            FeedbackQuestionInput(id="Q2", question="Should we support legacy browsers?"),
        ]
        comment = generate_feedback_comment("feedback-1", questions)

        assert "**Q1: What is the expected volume?**" in comment
        assert "**Q2: Should we support legacy browsers?**" in comment

    def test_includes_placeholder(self):
        """Test that answer placeholders are included."""
        questions = [FeedbackQuestionInput(id="Q1", question="Test question?")]
        comment = generate_feedback_comment("feedback-1", questions)

        assert "> _Your answer here_" in comment

    def test_includes_submit_checkbox(self):
        """Test that submit checkbox is included."""
        questions = [FeedbackQuestionInput(id="Q1", question="Test?")]
        comment = generate_feedback_comment("feedback-1", questions)

        assert "- [ ] Submit feedback" in comment

    def test_includes_instructions(self):
        """Test that instructions are included."""
        questions = [FeedbackQuestionInput(id="Q1", question="Test?")]
        comment = generate_feedback_comment("feedback-1", questions)

        assert "edit this comment" in comment.lower()
        assert "Questions & Feedback" in comment

    def test_includes_additional_feedback_section(self):
        """Test that additional feedback section is included."""
        questions = [FeedbackQuestionInput(id="Q1", question="Test?")]
        comment = generate_feedback_comment("feedback-1", questions)

        assert "Additional Feedback" in comment

    def test_debounce_notice(self):
        """Test including debounce notice."""
        questions = [FeedbackQuestionInput(id="Q1", question="Test?")]
        comment = generate_feedback_comment("feedback-1", questions, include_debounce_notice=True)

        assert "Debounce active" in comment
        assert f"{DEFAULT_DEBOUNCE_SECONDS} seconds" in comment

    def test_no_debounce_notice_by_default(self):
        """Test that debounce notice is not included by default."""
        questions = [FeedbackQuestionInput(id="Q1", question="Test?")]
        comment = generate_feedback_comment("feedback-1", questions)

        assert "Debounce active" not in comment


class TestParseFeedbackComment:
    """Tests for parse_feedback_comment."""

    def test_returns_none_without_marker(self):
        """Test that None is returned when no marker is present."""
        comment = "Just a regular comment"
        result = parse_feedback_comment(comment)

        assert result is None

    def test_extracts_feedback_id(self):
        """Test extracting the feedback ID from marker."""
        comment = """
<!-- egg-feedback id=feedback-1 -->

**Q1: Test question?**

> Test answer
"""
        result = parse_feedback_comment(comment)

        assert result is not None
        assert result.feedback_id == "feedback-1"

    def test_extracts_single_answer(self):
        """Test extracting a single answer."""
        comment = """
<!-- egg-feedback id=feedback-1 -->

**Q1: What is the expected volume?**

> About 10k requests per day
"""
        result = parse_feedback_comment(comment)

        assert result is not None
        assert result.questions["Q1"] == "About 10k requests per day"

    def test_extracts_multiple_answers(self):
        """Test extracting multiple answers."""
        comment = """
<!-- egg-feedback id=feedback-1 -->

**Q1: What is the expected volume?**

> 10k requests/day

**Q2: Should we support legacy browsers?**

> No, we can drop IE11

---
"""
        result = parse_feedback_comment(comment)

        assert result is not None
        assert result.questions["Q1"] == "10k requests/day"
        assert result.questions["Q2"] == "No, we can drop IE11"

    def test_placeholder_returns_none(self):
        """Test that placeholder text returns None."""
        comment = """
<!-- egg-feedback id=feedback-1 -->

**Q1: What is the expected volume?**

> _Your answer here_
"""
        result = parse_feedback_comment(comment)

        assert result is not None
        assert result.questions["Q1"] is None

    def test_detects_submit_unchecked(self):
        """Test detecting unchecked submit checkbox."""
        comment = """
<!-- egg-feedback id=feedback-1 -->

**Q1: Test?**

> Answer

- [ ] Submit feedback (I'm done editing)
"""
        result = parse_feedback_comment(comment)

        assert result is not None
        assert result.submitted is False

    def test_detects_submit_checked(self):
        """Test detecting checked submit checkbox."""
        comment = """
<!-- egg-feedback id=feedback-1 -->

**Q1: Test?**

> Answer

- [x] Submit feedback (I'm done editing)
"""
        result = parse_feedback_comment(comment)

        assert result is not None
        assert result.submitted is True

    def test_detects_submit_checked_uppercase(self):
        """Test detecting uppercase X in checkbox."""
        comment = """
<!-- egg-feedback id=feedback-1 -->

- [X] Submit feedback
"""
        result = parse_feedback_comment(comment)

        assert result is not None
        assert result.submitted is True

    def test_empty_answer(self):
        """Test that empty blockquote returns None."""
        comment = """
<!-- egg-feedback id=feedback-1 -->

**Q1: Test?**

>

---
"""
        result = parse_feedback_comment(comment)

        assert result is not None
        assert result.questions["Q1"] is None


class TestParsedFeedbackResponse:
    """Tests for ParsedFeedbackResponse."""

    def test_get_answer(self):
        """Test getting an answer by question ID."""
        response = ParsedFeedbackResponse(
            feedback_id="feedback-1",
            questions={"Q1": "Answer 1", "Q2": "Answer 2"},
            submitted=False,
            raw_comment="",
        )

        assert response.get_answer("Q1") == "Answer 1"
        assert response.get_answer("Q3") is None

    def test_has_all_answers_true(self):
        """Test has_all_answers when all questions answered."""
        response = ParsedFeedbackResponse(
            feedback_id="feedback-1",
            questions={"Q1": "Answer 1", "Q2": "Answer 2"},
            submitted=False,
            raw_comment="",
        )

        assert response.has_all_answers() is True

    def test_has_all_answers_false(self):
        """Test has_all_answers when some questions unanswered."""
        response = ParsedFeedbackResponse(
            feedback_id="feedback-1",
            questions={"Q1": "Answer 1", "Q2": None},
            submitted=False,
            raw_comment="",
        )

        assert response.has_all_answers() is False

    def test_has_all_answers_empty_string(self):
        """Test has_all_answers with empty string answer."""
        response = ParsedFeedbackResponse(
            feedback_id="feedback-1",
            questions={"Q1": "Answer 1", "Q2": "   "},
            submitted=False,
            raw_comment="",
        )

        assert response.has_all_answers() is False

    def test_to_dict(self):
        """Test conversion to dictionary."""
        response = ParsedFeedbackResponse(
            feedback_id="feedback-1",
            questions={"Q1": "Answer"},
            submitted=True,
            raw_comment="test",
        )
        data = response.to_dict()

        assert data["feedback_id"] == "feedback-1"
        assert data["questions"] == {"Q1": "Answer"}
        assert data["submitted"] is True


class TestShouldProcessFeedback:
    """Tests for should_process_feedback."""

    def test_not_submitted(self):
        """Test when feedback is not submitted."""
        response = ParsedFeedbackResponse(
            feedback_id="feedback-1",
            questions={"Q1": "Answer"},
            submitted=False,
            raw_comment="",
        )

        assert should_process_feedback(response) is False

    def test_submitted(self):
        """Test when feedback is submitted."""
        response = ParsedFeedbackResponse(
            feedback_id="feedback-1",
            questions={"Q1": "Answer"},
            submitted=True,
            raw_comment="",
        )

        assert should_process_feedback(response) is True


class TestCalculateFeedbackDebounceRemaining:
    """Tests for calculate_feedback_debounce_remaining."""

    def test_no_debounce(self):
        """Test when no debounce is set."""
        remaining = calculate_feedback_debounce_remaining(None)
        assert remaining == 0

    def test_expired(self):
        """Test when debounce has expired."""
        past = datetime.now(UTC) - timedelta(seconds=10)
        remaining = calculate_feedback_debounce_remaining(past)
        assert remaining == 0

    def test_active(self):
        """Test when debounce is active."""
        future = datetime.now(UTC) + timedelta(seconds=20)
        remaining = calculate_feedback_debounce_remaining(future)
        assert 18 <= remaining <= 21  # Allow small timing variance


class TestStartFeedbackDebounce:
    """Tests for start_feedback_debounce."""

    def test_returns_future_datetime(self):
        """Test that debounce returns a future datetime."""
        debounce_until = start_feedback_debounce()
        now = datetime.now(UTC)

        assert debounce_until > now

    def test_custom_debounce_time(self):
        """Test custom debounce time."""
        debounce_until = start_feedback_debounce(debounce_seconds=60)
        remaining = calculate_feedback_debounce_remaining(debounce_until)

        assert 55 <= remaining <= 61


class TestUpdateFeedbackWithCountdown:
    """Tests for update_feedback_with_countdown."""

    def test_updates_existing_debounce(self):
        """Test updating existing debounce notice."""
        original = """
<!-- egg-feedback id=feedback-1 -->

**Q1: Test?**

> Answer

---

> **Debounce active:** Feedback will be processed in **30 seconds**.
>
> _Making additional changes will reset the timer._

---

*Authored-by: egg*
"""
        updated = update_feedback_with_countdown(original, 15)

        assert "15 seconds" in updated
        assert "30 seconds" not in updated

    def test_updates_to_processing(self):
        """Test updating to processing state."""
        original = """
<!-- egg-feedback id=feedback-1 -->

> **Debounce active:** Feedback will be processed in **5 seconds**.
"""
        updated = update_feedback_with_countdown(original, 0)

        assert "Processing feedback" in updated


class TestDefaultDebounceSeconds:
    """Tests for DEFAULT_DEBOUNCE_SECONDS import."""

    def test_default_value(self):
        """Test default debounce is 30 seconds."""
        assert DEFAULT_DEBOUNCE_SECONDS == 30
