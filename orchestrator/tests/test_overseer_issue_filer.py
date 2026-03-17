"""Tests for overseer issue filer (Phase 4).

Validates that diagnostic issues are constructed correctly with the
expected format and labels.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

# ---------------------------------------------------------------------------
# Conditional import
# ---------------------------------------------------------------------------

try:
    from overseer.issue_filer import (
        DIAGNOSTIC_LABELS,
        _build_issue_body,
        file_diagnostic_issue,
    )
except (ImportError, ModuleNotFoundError) as exc:
    pytest.skip(
        f"overseer.issue_filer not available yet: {exc}",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


# ===================================================================
# test_file_diagnostic_issue_template
# ===================================================================


class TestFileDiagnosticIssueTemplate:
    """Test the issue body template generation."""

    def test_file_diagnostic_issue_template(self) -> None:
        """The issue body should contain all required sections."""
        body = _build_issue_body(
            pipeline_id="issue-42",
            agent_role="coder",
            anomaly={
                "type": "stall",
                "description": "Agent has not made progress for 15 minutes",
                "classification": {
                    "type": "stall",
                    "confidence": 0.92,
                    "reasoning": "No tool calls or file changes detected",
                },
            },
            context={
                "phase": "implement",
                "detected_at": "2026-03-16T10:30:00Z",
                "timeline": [
                    {"timestamp": "10:15:00", "event": "Last progress event"},
                    {"timestamp": "10:20:00", "event": "Heartbeat timeout alert"},
                    {"timestamp": "10:25:00", "event": "Nudge sent"},
                    {"timestamp": "10:30:00", "event": "Stall classified as stuck"},
                ],
                "actions_taken": [
                    "Auto-nudge sent at 10:20",
                    "Redirect sent at 10:25",
                ],
                "suggested_remediation": "Check agent logs for error loops",
            },
        )

        # Required sections
        assert "## Pipeline Diagnostic: stall" in body
        assert "**Pipeline**: `issue-42`" in body
        assert "**Phase**: `implement`" in body
        assert "**Agent**: `coder`" in body
        assert "### Anomaly" in body
        assert "### Timeline" in body
        assert "### Classification" in body
        assert "### Actions Taken" in body
        assert "### Suggested Remediation" in body

        # Content checks
        assert "Agent has not made progress" in body
        assert "Last progress event" in body
        assert "0.92" in body
        assert "Auto-nudge sent" in body
        assert "Check agent logs" in body

    @patch("overseer.issue_filer.subprocess")
    def test_file_diagnostic_issue_gh_failure(self, mock_subprocess) -> None:
        """When gh CLI fails, filed=False but template is still returned."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "not authenticated"
        mock_subprocess.run.return_value = mock_result

        result = _run(
            file_diagnostic_issue(
                pipeline_id="issue-42",
                agent_role="coder",
                anomaly={"type": "stall", "description": "stuck"},
                context={"phase": "implement"},
            )
        )

        assert result["filed"] is False
        assert result["issue_number"] is None
        assert "## Pipeline Diagnostic" in result["template"]

    @patch("overseer.issue_filer.subprocess")
    def test_file_diagnostic_issue_success(self, mock_subprocess) -> None:
        """When gh CLI succeeds, filed=True and issue_number is parsed."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/org/repo/issues/456\n"
        mock_subprocess.run.return_value = mock_result

        result = _run(
            file_diagnostic_issue(
                pipeline_id="issue-42",
                agent_role="coder",
                anomaly={"type": "stall", "description": "stuck"},
                context={"phase": "implement"},
            )
        )

        assert result["filed"] is True
        assert result["issue_number"] == 456
        assert "## Pipeline Diagnostic" in result["template"]


# ===================================================================
# test_issue_labels
# ===================================================================


class TestIssueLabels:
    """Test that diagnostic issues use the correct labels."""

    def test_issue_labels(self) -> None:
        """DIAGNOSTIC_LABELS should include expected labels."""
        assert "egg:diagnostic" in DIAGNOSTIC_LABELS
        assert "pipeline-health" in DIAGNOSTIC_LABELS

    @patch("overseer.issue_filer.subprocess")
    def test_labels_passed_to_gh(self, mock_subprocess) -> None:
        """Labels should be passed to the gh CLI command."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/org/repo/issues/789\n"
        mock_subprocess.run.return_value = mock_result

        _run(
            file_diagnostic_issue(
                pipeline_id="test-1",
                agent_role="tester",
                anomaly={"type": "error", "description": "test"},
                context={},
            )
        )

        call_args = mock_subprocess.run.call_args[0][0]
        # Verify --label flags are in the command
        assert "--label" in call_args
        label_indices = [i for i, a in enumerate(call_args) if a == "--label"]
        labels_used = [call_args[i + 1] for i in label_indices]
        assert "egg:diagnostic" in labels_used
        assert "pipeline-health" in labels_used
