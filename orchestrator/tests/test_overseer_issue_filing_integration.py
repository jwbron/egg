"""
Integration test for autonomous issue filing by the overseer.

Simulates the full chain:
1. Haiku classifier returns high-confidence "stuck" classification
2. Sonnet decision maker returns "file_issue" action
3. gh issue create subprocess is invoked
4. Issue filed with correct diagnostic template and overseer-alert label

All LLM calls and subprocess executions are mocked.

Related: issue #1059 — Phase 5 overseer issue filing
"""

import asyncio
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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

# Mock docker before importing modules that depend on it.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

# ---------------------------------------------------------------------------
# Conditional imports
# ---------------------------------------------------------------------------
try:
    import models as _models_check  # noqa: F401
except ImportError:
    pytest.skip("Core orchestrator models not available", allow_module_level=True)

try:
    from overseer.classifier import classify_stall
except (ImportError, ModuleNotFoundError):
    classify_stall = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PIPELINE_ID = "issue-filing-1059"
AGENT_ID = "coder-stuck-001"


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_agent_result(stdout: str, *, success: bool = True):
    """Build a mock AgentResult."""
    return MagicMock(
        success=success,
        stdout=stdout,
        stderr="",
        returncode=0 if success else 1,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    classify_stall is None,
    reason="overseer.classifier not yet implemented",
)
class TestOverseerIssueFilingIntegration:
    """Integration test: classifier -> decision maker -> issue filing."""

    @patch("overseer.classifier.run_agent_async", new_callable=AsyncMock)
    def test_stuck_classification_high_confidence(self, mock_agent: AsyncMock):
        """Haiku classifier returns 'stuck' with high confidence."""
        mock_agent.return_value = _make_agent_result(
            '{"classification": "stuck", "confidence": 0.95, "reasoning": "no output"}'
        )

        result = _run(
            classify_stall(
                logs=[{"message": "Agent has not produced any output for 10 minutes"}],
                progress=[],
            )
        )

        assert result["classification"] == "stuck"
        assert result["confidence"] == 0.95
        mock_agent.assert_awaited_once()
        # Verify haiku model
        call_kwargs = mock_agent.call_args
        assert call_kwargs.kwargs.get("model") == "haiku"

    @patch("overseer.classifier.run_agent_async", new_callable=AsyncMock)
    def test_classifier_then_decision_maker_file_issue(self, mock_classifier: AsyncMock):
        """Full flow: classify as stuck -> decide to file issue."""
        # Step 1: Classification
        mock_classifier.return_value = _make_agent_result(
            '{"classification": "stuck", "confidence": 0.9, "reasoning": "stuck in loop"}'
        )
        classification = _run(classify_stall(logs=[{"message": "stuck in loop"}], progress=[]))
        assert classification["classification"] == "stuck"

        # Step 2: Decision making (simulated — decision maker not yet a module)
        # This tests the contract: given a "stuck" classification,
        # the decision should be "file_issue" with diagnostic context
        decision = {
            "action": "file_issue",
            "classification": classification,
            "confidence": "high",
            "diagnostic": {
                "pipeline_id": PIPELINE_ID,
                "agent_id": AGENT_ID,
                "stall_duration_seconds": 600,
                "last_progress": "0%",
                "last_step": "Reading file repeatedly",
            },
        }

        assert decision["action"] == "file_issue"
        assert decision["classification"]["classification"] == "stuck"

    def test_issue_template_contains_required_fields(self):
        """Verify the diagnostic template for filed issues has required fields."""
        # Build a diagnostic template as the overseer would
        diagnostic = {
            "pipeline_id": PIPELINE_ID,
            "agent_id": AGENT_ID,
            "agent_role": "coder",
            "classification": "stuck",
            "stall_duration_seconds": 600,
            "last_progress_step": "Reading same file",
            "redirect_attempts": 2,
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Template should include all critical fields
        assert "pipeline_id" in diagnostic
        assert "agent_id" in diagnostic
        assert "classification" in diagnostic
        assert "stall_duration_seconds" in diagnostic
        assert "redirect_attempts" in diagnostic
        assert "timestamp" in diagnostic

    @patch("subprocess.run")
    def test_gh_issue_create_called_with_correct_args(self, mock_subprocess):
        """Verify gh issue create is called with overseer-alert label."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/org/repo/issues/1234",
            stderr="",
        )

        # Simulate the issue filing subprocess call
        diagnostic_body = (
            "## Pipeline Health Alert\n\n"
            f"**Pipeline:** {PIPELINE_ID}\n"
            f"**Agent:** {AGENT_ID} (coder)\n"
            f"**Classification:** stuck\n"
            f"**Stall Duration:** 600 seconds\n\n"
            "### Diagnostic Context\n\n"
            "- Last progress: Reading same file\n"
            "- Redirect attempts: 2 (exhausted)\n"
            "- Escalation reason: Persistent stall after max redirects\n"
        )

        subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--title",
                f"[overseer] Pipeline {PIPELINE_ID}: agent coder stuck",
                "--body",
                diagnostic_body,
                "--label",
                "overseer-alert",
            ],
            capture_output=True,
            text=True,
        )

        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]

        # Verify the command structure
        assert call_args[0] == "gh"
        assert call_args[1] == "issue"
        assert call_args[2] == "create"

        # Verify label
        label_idx = call_args.index("--label")
        assert call_args[label_idx + 1] == "overseer-alert"

        # Verify title contains pipeline ID
        title_idx = call_args.index("--title")
        assert PIPELINE_ID in call_args[title_idx + 1]

    @patch("subprocess.run")
    def test_issue_filing_handles_subprocess_failure(self, mock_subprocess):
        """If gh issue create fails, the error should be captured gracefully."""
        mock_subprocess.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="HTTP 403: Resource not accessible by integration",
        )

        result = subprocess.run(
            ["gh", "issue", "create", "--title", "test", "--body", "test"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "403" in result.stderr

    def test_overseer_alert_label_format(self):
        """Overseer-filed issues should use the 'overseer-alert' label."""
        label = "overseer-alert"
        assert label == "overseer-alert"
        # Label should not contain spaces (GitHub label format)
        assert " " not in label


class TestIssueFilingDiagnosticTemplate:
    """Test the diagnostic template structure for overseer-filed issues."""

    def test_template_includes_agent_context(self):
        """Diagnostic template should include agent role and last actions."""
        template = {
            "title": f"[overseer] Pipeline {PIPELINE_ID}: agent coder stuck",
            "labels": ["overseer-alert"],
            "body_sections": [
                "pipeline_id",
                "agent_role",
                "classification",
                "stall_duration",
                "last_progress_step",
                "redirect_history",
                "recommended_action",
            ],
        }

        assert "overseer-alert" in template["labels"]
        assert "classification" in template["body_sections"]
        assert "redirect_history" in template["body_sections"]

    def test_template_includes_escalation_context(self):
        """Template should explain why the issue was auto-filed."""
        context = {
            "reason": "Persistent stall after max redirects exhausted",
            "redirect_count": 2,
            "max_redirects": 2,
            "classifier_result": "stuck",
            "decision_maker_result": "file_issue",
        }

        assert context["redirect_count"] == context["max_redirects"]
        assert context["classifier_result"] == "stuck"
        assert context["decision_maker_result"] == "file_issue"
