"""Tests for jib_logging signature utilities."""

from egg_logging import ContextScope, set_current_context
from egg_logging.signatures import (
    add_signature_to_comment,
    add_signature_to_pr_body,
    get_workflow_context_dict,
    get_workflow_signature,
)


class TestGetWorkflowSignature:
    """Tests for get_workflow_signature function."""

    def teardown_method(self):
        """Reset context after each test."""
        set_current_context(None)

    def test_returns_empty_string_when_no_context(self):
        """Test that signature is empty when no context is set."""
        set_current_context(None)
        sig = get_workflow_signature()
        assert sig == ""

    def test_signature_with_workflow_type_only(self):
        """Test signature generation with only workflow_type."""
        with ContextScope(workflow_type="check_failure"):
            sig = get_workflow_signature()
            assert "Workflow: Check Failure" in sig
            assert sig.startswith("_(")
            assert sig.endswith(")_")

    def test_signature_with_workflow_id_only(self):
        """Test signature generation with only workflow_id."""
        with ContextScope(workflow_id="gw-test-20251130-102305-a1b2c3d4"):
            sig = get_workflow_signature()
            assert "ID: `gw-test-20251130-102305-a1b2c3d4`" in sig
            assert sig.startswith("_(")
            assert sig.endswith(")_")

    def test_signature_with_both_workflow_fields(self):
        """Test signature generation with both workflow_type and workflow_id."""
        with ContextScope(
            workflow_type="check_failure",
            workflow_id="gw-check_failure-20251130-102305-a1b2c3d4",
        ):
            sig = get_workflow_signature()
            assert "Workflow: Check Failure" in sig
            assert "ID: `gw-check_failure-20251130-102305-a1b2c3d4`" in sig
            assert " | " in sig  # Check separator

    def test_workflow_type_formatting(self):
        """Test that workflow_type is formatted with title case and spaces."""
        test_cases = [
            ("check_failure", "Check Failure"),
            ("slack_task", "Slack Task"),
            ("pr_comment", "Pr Comment"),
            ("simple", "Simple"),
        ]

        for input_type, expected_display in test_cases:
            with ContextScope(workflow_type=input_type):
                sig = get_workflow_signature()
                assert f"Workflow: {expected_display}" in sig

    def test_signature_without_trace_id_by_default(self):
        """Test that trace_id is not included by default."""
        with ContextScope(
            workflow_type="test",
            workflow_id="test-123",
            trace_id="abc123def456",
        ):
            sig = get_workflow_signature()
            assert "Trace:" not in sig
            assert "abc123def456" not in sig

    def test_signature_with_trace_id_when_requested(self):
        """Test that trace_id is included when include_trace_id=True."""
        with ContextScope(
            workflow_type="test",
            workflow_id="test-123",
            trace_id="abc123def456",
        ):
            sig = get_workflow_signature(include_trace_id=True)
            assert "Trace: `abc123de...`" in sig

    def test_signature_returns_empty_when_no_workflow_fields(self):
        """Test that signature is empty when context has no workflow fields."""
        with ContextScope(task_id="bd-abc123", repository="owner/repo"):
            sig = get_workflow_signature()
            assert sig == ""


class TestAddSignatureToPrBody:
    """Tests for add_signature_to_pr_body function."""

    def teardown_method(self):
        """Reset context after each test."""
        set_current_context(None)

    def test_returns_unchanged_body_when_no_context(self):
        """Test that body is unchanged when no context."""
        set_current_context(None)
        body = "Original PR body"
        result = add_signature_to_pr_body(body)
        assert result == body

    def test_adds_signature_to_pr_body(self):
        """Test that signature is appended to PR body."""
        with ContextScope(
            workflow_type="check_failure",
            workflow_id="gw-check_failure-20251130-102305-a1b2c3d4",
        ):
            body = "Fixed linting errors"
            result = add_signature_to_pr_body(body)

            assert "Fixed linting errors" in result
            assert "Workflow: Check Failure" in result
            assert "ID: `gw-check_failure-20251130-102305-a1b2c3d4`" in result
            assert "\n\n---\n\n" in result  # Separator

    def test_signature_at_end_of_pr_body(self):
        """Test that signature appears at the end after separator."""
        with ContextScope(workflow_type="test", workflow_id="test-123"):
            body = "PR description here"
            result = add_signature_to_pr_body(body)

            lines = result.split("\n")
            assert lines[0] == "PR description here"
            assert "---" in result
            assert result.endswith(")_")

    def test_includes_trace_id_when_requested(self):
        """Test that trace_id is included when parameter is True."""
        with ContextScope(
            workflow_type="test",
            workflow_id="test-123",
            trace_id="abc123def456",
        ):
            result = add_signature_to_pr_body("Body", include_trace_id=True)
            assert "Trace: `abc123de...`" in result


class TestAddSignatureToComment:
    """Tests for add_signature_to_comment function."""

    def teardown_method(self):
        """Reset context after each test."""
        set_current_context(None)

    def test_returns_unchanged_comment_when_no_context(self):
        """Test that comment is unchanged when no context."""
        set_current_context(None)
        comment = "Original comment"
        result = add_signature_to_comment(comment)
        assert result == comment

    def test_adds_signature_to_comment(self):
        """Test that signature is appended to comment."""
        with ContextScope(
            workflow_type="check_failure",
            workflow_id="gw-check_failure-20251130-102305-a1b2c3d4",
        ):
            comment = "Fixed the issue"
            result = add_signature_to_comment(comment)

            assert "Fixed the issue" in result
            assert "Workflow: Check Failure" in result
            assert "ID: `gw-check_failure-20251130-102305-a1b2c3d4`" in result

    def test_signature_at_end_of_comment(self):
        """Test that signature appears at the end of comment."""
        with ContextScope(workflow_type="test", workflow_id="test-123"):
            comment = "Comment text"
            result = add_signature_to_comment(comment)

            lines = result.split("\n")
            assert lines[0] == "Comment text"
            assert result.endswith(")_")

    def test_includes_trace_id_when_requested(self):
        """Test that trace_id is included when parameter is True."""
        with ContextScope(
            workflow_type="test",
            workflow_id="test-123",
            trace_id="abc123def456",
        ):
            result = add_signature_to_comment("Comment", include_trace_id=True)
            assert "Trace: `abc123de...`" in result


class TestGetWorkflowContextDict:
    """Tests for get_workflow_context_dict function."""

    def teardown_method(self):
        """Reset context after each test."""
        set_current_context(None)

    def test_returns_empty_dict_when_no_context(self):
        """Test that empty dict is returned when no context."""
        set_current_context(None)
        result = get_workflow_context_dict()
        assert result == {}

    def test_returns_workflow_fields_when_present(self):
        """Test that workflow fields are included when present."""
        with ContextScope(
            workflow_id="gw-test-123",
            workflow_type="test_type",
            trace_id="abc123",
        ):
            result = get_workflow_context_dict()
            assert result["workflow_id"] == "gw-test-123"
            assert result["workflow_type"] == "test_type"
            assert result["trace_id"] == "abc123"

    def test_excludes_missing_workflow_fields(self):
        """Test that missing workflow fields are not included."""
        with ContextScope(workflow_id="gw-test-123"):
            result = get_workflow_context_dict()
            assert "workflow_id" in result
            assert "workflow_type" not in result
            # Note: trace_id is auto-generated by ContextScope, so it will be present

    def test_excludes_non_workflow_fields(self):
        """Test that non-workflow context fields are excluded."""
        with ContextScope(
            workflow_id="gw-test-123",
            task_id="bd-abc",
            repository="owner/repo",
            pr_number=42,
        ):
            result = get_workflow_context_dict()
            assert "workflow_id" in result
            assert "task_id" not in result
            assert "repository" not in result
            assert "pr_number" not in result
