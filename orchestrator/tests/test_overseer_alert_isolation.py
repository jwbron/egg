"""Tests for overseer alert pipeline isolation (issue #1523).

Validates that all overseer CLI wrapper methods (_broadcast_alert,
_send_message, _resolve_alert, _create_hitl_decision) pass the
pipeline_id explicitly so alerts cannot leak across pipelines.
Also verifies _broadcast_alert and _send_message pass --role overseer
so that the from_role is correct.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirrors test_overseer_monitor.py)
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

try:
    from overseer.monitor import OverseerMonitor
except (ImportError, ModuleNotFoundError) as exc:
    pytest.skip(
        f"overseer.monitor not available yet: {exc}",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scope_egg_repo_path(monkeypatch, tmp_path):
    """Scope EGG_REPO_PATH to tmp_path for every test in this file.

    See test_overseer_monitor.py for the full rationale (#2244): without
    this, OverseerMonitor writes oversight JSONLs into the real repo.
    """
    monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


class _MockConfig:
    """Minimal config for testing."""

    overseer_poll_interval_seconds = 1
    overseer_max_redirects_before_escalation = 2
    overseer_decision_maker_model = "sonnet"


def _make_monitor(pipeline_id: str = "test-isolation-001") -> OverseerMonitor:
    """Create a monitor with mocked CLI."""
    monitor = OverseerMonitor(pipeline_id=pipeline_id, config=_MockConfig())
    monitor._run_cli = AsyncMock(return_value=(0, "", ""))
    return monitor


def _get_cli_args(monitor: OverseerMonitor) -> tuple:
    """Extract positional args from the last _run_cli call."""
    monitor._run_cli.assert_awaited_once()
    return monitor._run_cli.call_args.args


# ===================================================================
# _broadcast_alert: pipeline isolation
# ===================================================================


class TestBroadcastAlertIsolation:
    """Verify _broadcast_alert passes pipeline_id and --role overseer."""

    def test_pipeline_id_passed_as_positional_arg(self) -> None:
        """_broadcast_alert includes self.pipeline_id in CLI args."""
        monitor = _make_monitor("pipeline-abc-123")
        _run(
            monitor._broadcast_alert(
                anomaly_type="stall",
                agent_role="coder",
                message="test message",
            )
        )
        args = _get_cli_args(monitor)
        # pipeline_id should appear after "send"
        send_idx = args.index("send")
        assert args[send_idx + 1] == "pipeline-abc-123"

    def test_role_overseer_passed(self) -> None:
        """_broadcast_alert passes --role overseer so from_role is correct."""
        monitor = _make_monitor("test-role-001")
        _run(
            monitor._broadcast_alert(
                anomaly_type="test",
                agent_role="tester",
                message="check role",
                priority="low",
            )
        )
        args = _get_cli_args(monitor)
        idx_role = args.index("--role")
        assert args[idx_role + 1] == "overseer"

    def test_message_type_is_overseer_alert(self) -> None:
        """_broadcast_alert still sends OVERSEER_ALERT type."""
        monitor = _make_monitor()
        _run(
            monitor._broadcast_alert(
                anomaly_type="test",
                agent_role="coder",
                message="msg",
            )
        )
        args = _get_cli_args(monitor)
        idx_type = args.index("--type")
        assert args[idx_type + 1] == "OVERSEER_ALERT"

    def test_subject_format_includes_anomaly_role_priority(self) -> None:
        """Subject line includes anomaly_type, agent_role, and priority."""
        monitor = _make_monitor()
        _run(
            monitor._broadcast_alert(
                anomaly_type="post_consensus_stall",
                agent_role="orchestrator",
                message="test",
                priority="critical",
            )
        )
        args = _get_cli_args(monitor)
        idx_subject = args.index("--subject")
        subject = args[idx_subject + 1]
        assert "post_consensus_stall" in subject
        assert "orchestrator" in subject
        assert "critical" in subject

    def test_broadcast_to_all(self) -> None:
        """_broadcast_alert sends to 'all' target."""
        monitor = _make_monitor()
        _run(
            monitor._broadcast_alert(
                anomaly_type="test",
                agent_role="coder",
                message="test",
            )
        )
        args = _get_cli_args(monitor)
        idx_to = args.index("--to")
        assert args[idx_to + 1] == "all"

    def test_default_priority_is_medium(self) -> None:
        """Default priority is 'medium' when not specified."""
        monitor = _make_monitor()
        _run(
            monitor._broadcast_alert(
                anomaly_type="test",
                agent_role="coder",
                message="test",
            )
        )
        args = _get_cli_args(monitor)
        idx_subject = args.index("--subject")
        assert "medium" in args[idx_subject + 1]

    def test_cli_failure_does_not_propagate(self) -> None:
        """CLI failures are silently caught."""
        monitor = _make_monitor()
        monitor._run_cli = AsyncMock(side_effect=RuntimeError("CLI crashed"))
        # Should not raise
        _run(
            monitor._broadcast_alert(
                anomaly_type="test",
                agent_role="coder",
                message="test",
            )
        )


# ===================================================================
# _send_message: pipeline isolation
# ===================================================================


class TestSendMessageIsolation:
    """Verify _send_message passes pipeline_id, --role overseer, and --type."""

    def test_pipeline_id_passed(self) -> None:
        """_send_message includes self.pipeline_id in CLI args."""
        monitor = _make_monitor("pipeline-msg-001")
        _run(monitor._send_message("coder", "check your progress"))
        args = _get_cli_args(monitor)
        send_idx = args.index("send")
        assert args[send_idx + 1] == "pipeline-msg-001"

    def test_role_overseer_passed(self) -> None:
        """_send_message passes --role overseer."""
        monitor = _make_monitor()
        _run(monitor._send_message("tester", "health check"))
        args = _get_cli_args(monitor)
        idx_role = args.index("--role")
        assert args[idx_role + 1] == "overseer"

    def test_message_type_is_status(self) -> None:
        """_send_message now sends --type STATUS."""
        monitor = _make_monitor()
        _run(monitor._send_message("coder", "test"))
        args = _get_cli_args(monitor)
        idx_type = args.index("--type")
        assert args[idx_type + 1] == "STATUS"

    def test_to_field_is_agent_role(self) -> None:
        """_send_message targets the specified agent role."""
        monitor = _make_monitor()
        _run(monitor._send_message("documenter", "update docs"))
        args = _get_cli_args(monitor)
        idx_to = args.index("--to")
        assert args[idx_to + 1] == "documenter"

    def test_subject_is_health_check(self) -> None:
        """_send_message uses 'Overseer health check' as subject."""
        monitor = _make_monitor()
        _run(monitor._send_message("coder", "test"))
        args = _get_cli_args(monitor)
        idx_subject = args.index("--subject")
        assert args[idx_subject + 1] == "Overseer health check"

    def test_body_is_message(self) -> None:
        """_send_message passes the message as the body."""
        monitor = _make_monitor()
        _run(monitor._send_message("coder", "Agent appears stuck for 5 minutes"))
        args = _get_cli_args(monitor)
        idx_body = args.index("--body")
        assert args[idx_body + 1] == "Agent appears stuck for 5 minutes"

    def test_cli_failure_does_not_propagate(self) -> None:
        """CLI failures are silently caught."""
        monitor = _make_monitor()
        monitor._run_cli = AsyncMock(side_effect=OSError("CLI not found"))
        _run(monitor._send_message("coder", "test"))


# ===================================================================
# _resolve_alert: pipeline isolation
# ===================================================================


class TestResolveAlertIsolation:
    """Verify _resolve_alert passes pipeline_id explicitly."""

    def test_pipeline_id_passed(self) -> None:
        """_resolve_alert includes self.pipeline_id in CLI args."""
        monitor = _make_monitor("pipeline-resolve-001")
        _run(monitor._resolve_alert("agent-coder-1", "post_consensus_stall"))
        args = _get_cli_args(monitor)
        resolve_idx = args.index("resolve")
        assert args[resolve_idx + 1] == "pipeline-resolve-001"

    def test_agent_id_passed(self) -> None:
        """_resolve_alert passes --agent-id correctly."""
        monitor = _make_monitor()
        _run(monitor._resolve_alert("agent-tester-42", "orchestrator_unreachable"))
        args = _get_cli_args(monitor)
        idx = args.index("--agent-id")
        assert args[idx + 1] == "agent-tester-42"

    def test_alert_type_passed(self) -> None:
        """_resolve_alert passes --alert-type correctly."""
        monitor = _make_monitor()
        _run(monitor._resolve_alert("agent-1", "status_inconsistency"))
        args = _get_cli_args(monitor)
        idx = args.index("--alert-type")
        assert args[idx + 1] == "status_inconsistency"

    def test_uses_health_resolve_subcommand(self) -> None:
        """_resolve_alert calls egg-orch health resolve."""
        monitor = _make_monitor()
        _run(monitor._resolve_alert("agent-1", "test"))
        args = _get_cli_args(monitor)
        assert "egg-orch" in args
        assert "health" in args
        assert "resolve" in args

    def test_cli_failure_does_not_propagate(self) -> None:
        """CLI failures are silently caught."""
        monitor = _make_monitor()
        monitor._run_cli = AsyncMock(side_effect=ConnectionError("no connection"))
        _run(monitor._resolve_alert("agent-1", "test"))


# ===================================================================
# _create_hitl_decision: pipeline isolation
# ===================================================================


class TestCreateHitlDecisionIsolation:
    """Verify _create_hitl_decision passes pipeline_id explicitly."""

    def test_pipeline_id_passed(self) -> None:
        """_create_hitl_decision includes self.pipeline_id in CLI args."""
        monitor = _make_monitor("pipeline-hitl-001")
        _run(monitor._create_hitl_decision("coder", "Agent stuck in loop"))
        args = _get_cli_args(monitor)
        create_idx = args.index("create")
        assert args[create_idx + 1] == "pipeline-hitl-001"

    def test_question_includes_agent_and_message(self) -> None:
        """_create_hitl_decision includes agent role and message in question."""
        monitor = _make_monitor()
        _run(monitor._create_hitl_decision("tester", "Tests failing repeatedly"))
        args = _get_cli_args(monitor)
        idx = args.index("--question")
        question = args[idx + 1]
        assert "tester" in question
        assert "Tests failing repeatedly" in question

    def test_options_provided(self) -> None:
        """_create_hitl_decision provides expected resolution options."""
        monitor = _make_monitor()
        _run(monitor._create_hitl_decision("coder", "issue"))
        args = _get_cli_args(monitor)
        idx = args.index("--options")
        # Options should follow --options
        remaining = args[idx + 1 :]
        assert "Restart agent" in remaining
        assert "Continue monitoring" in remaining
        assert "Cancel pipeline" in remaining

    def test_uses_decision_create_subcommand(self) -> None:
        """_create_hitl_decision calls egg-orch decision create."""
        monitor = _make_monitor()
        _run(monitor._create_hitl_decision("coder", "test"))
        args = _get_cli_args(monitor)
        assert "egg-orch" in args
        assert "decision" in args
        assert "create" in args

    def test_cli_failure_does_not_propagate(self) -> None:
        """CLI failures are silently caught."""
        monitor = _make_monitor()
        monitor._run_cli = AsyncMock(side_effect=TimeoutError("timed out"))
        _run(monitor._create_hitl_decision("coder", "test"))


# ===================================================================
# Cross-method: pipeline_id consistency
# ===================================================================


class TestPipelineIdConsistency:
    """Verify that ALL CLI wrapper methods use the monitor's pipeline_id."""

    @pytest.mark.parametrize(
        "pipeline_id",
        [
            "issue-1523-v2",
            "test-postconsensus-001",
            "pipeline-with-special-chars_v3",
            "short",
        ],
    )
    def test_broadcast_alert_uses_monitor_pipeline_id(self, pipeline_id: str) -> None:
        """_broadcast_alert routes to the correct pipeline across various IDs."""
        monitor = _make_monitor(pipeline_id)
        _run(
            monitor._broadcast_alert(
                anomaly_type="test",
                agent_role="coder",
                message="msg",
            )
        )
        args = _get_cli_args(monitor)
        assert pipeline_id in args

    @pytest.mark.parametrize(
        "pipeline_id",
        [
            "issue-1523-v2",
            "test-postconsensus-001",
            "pipeline-with-special-chars_v3",
        ],
    )
    def test_send_message_uses_monitor_pipeline_id(self, pipeline_id: str) -> None:
        """_send_message routes to the correct pipeline across various IDs."""
        monitor = _make_monitor(pipeline_id)
        _run(monitor._send_message("coder", "test"))
        args = _get_cli_args(monitor)
        assert pipeline_id in args

    @pytest.mark.parametrize(
        "pipeline_id",
        [
            "issue-1523-v2",
            "test-postconsensus-001",
        ],
    )
    def test_resolve_alert_uses_monitor_pipeline_id(self, pipeline_id: str) -> None:
        """_resolve_alert routes to the correct pipeline across various IDs."""
        monitor = _make_monitor(pipeline_id)
        _run(monitor._resolve_alert("agent-1", "test_alert"))
        args = _get_cli_args(monitor)
        assert pipeline_id in args

    @pytest.mark.parametrize(
        "pipeline_id",
        [
            "issue-1523-v2",
            "test-postconsensus-001",
        ],
    )
    def test_create_hitl_decision_uses_monitor_pipeline_id(self, pipeline_id: str) -> None:
        """_create_hitl_decision routes to the correct pipeline."""
        monitor = _make_monitor(pipeline_id)
        _run(monitor._create_hitl_decision("coder", "test"))
        args = _get_cli_args(monitor)
        assert pipeline_id in args


# ===================================================================
# Regression: self-test pipeline IDs must not leak
# ===================================================================


class TestSelfTestAlertIsolation:
    """Regression tests: overseer self-test alerts must stay isolated.

    When the overseer runs health checks using test pipeline IDs
    (e.g. test-postconsensus-001), the alerts must be sent to that
    test pipeline — not to whatever EGG_PIPELINE_ID is set in env.
    """

    def test_self_test_alert_stays_in_test_pipeline(self) -> None:
        """Alert from test pipeline stays in test pipeline, not real one."""
        # Simulate: overseer monitoring a test pipeline
        monitor = _make_monitor("test-postconsensus-001")
        _run(
            monitor._broadcast_alert(
                anomaly_type="post_consensus_stall",
                agent_role="orchestrator",
                message="Consensus complete but phase not transitioning",
                priority="high",
            )
        )
        args = _get_cli_args(monitor)
        send_idx = args.index("send")
        # The pipeline_id in the CLI args must be the test pipeline,
        # NOT whatever is in the environment
        assert args[send_idx + 1] == "test-postconsensus-001"

    def test_real_pipeline_alert_stays_in_real_pipeline(self) -> None:
        """Alert from real pipeline stays in real pipeline."""
        monitor = _make_monitor("issue-1515-v2")
        _run(
            monitor._broadcast_alert(
                anomaly_type="orchestrator_unreachable",
                agent_role="orchestrator",
                message="Cannot reach orchestrator",
                priority="critical",
            )
        )
        args = _get_cli_args(monitor)
        send_idx = args.index("send")
        assert args[send_idx + 1] == "issue-1515-v2"

    def test_from_role_is_overseer_not_coder(self) -> None:
        """Alerts must come from 'overseer', not 'coder' or other roles.

        The original bug had alerts appearing with from_role: 'coder'
        because EGG_AGENT_ROLE was used instead of --role overseer.
        """
        monitor = _make_monitor("test-dedup-001")
        _run(
            monitor._broadcast_alert(
                anomaly_type="test",
                agent_role="coder",
                message="test",
            )
        )
        args = _get_cli_args(monitor)
        idx_role = args.index("--role")
        assert args[idx_role + 1] == "overseer", (
            "Alert from_role must be 'overseer', not derived from EGG_AGENT_ROLE"
        )


# ===================================================================
# Health check integration: broadcast still invoked
# ===================================================================


class TestHealthChecksBroadcastWithIsolation:
    """Verify health checks still broadcast alerts after the isolation fix.

    These tests confirm the integration between health check methods
    and _broadcast_alert is preserved with the new pipeline_id routing.
    """

    @staticmethod
    def _make_health_check_monitor(pipeline_id: str = "test-hc-iso-001"):
        monitor = OverseerMonitor(pipeline_id=pipeline_id, config=_MockConfig())
        monitor._run_cli = AsyncMock(return_value=(0, "", ""))
        monitor._broadcast_alert = AsyncMock()
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._log_oversight_event = MagicMock()
        return monitor

    def test_post_consensus_stall_triggers_broadcast(self) -> None:
        """Post-consensus stall check calls _broadcast_alert."""
        import time as _time

        monitor = self._make_health_check_monitor()
        consensus = {"is_complete": True}

        # First call sets first-seen timestamp
        _run(monitor._check_post_consensus_stall(consensus, "running"))
        assert monitor._broadcast_alert.await_count == 0

        # Force grace period expiry
        monitor._post_consensus_stall_first_seen = _time.time() - 200
        _run(monitor._check_post_consensus_stall(consensus, "running"))
        monitor._broadcast_alert.assert_awaited_once()
        assert monitor._broadcast_alert.call_args.args[0] == "post_consensus_stall"

    def test_orchestrator_unreachable_triggers_broadcast(self) -> None:
        """Orchestrator unreachable check calls _broadcast_alert at threshold."""
        monitor = self._make_health_check_monitor()
        monitor._consecutive_orch_failures = 2  # one below threshold of 3

        _run(monitor._check_orchestrator_reachability({}, {}))
        monitor._broadcast_alert.assert_awaited_once()
        assert monitor._broadcast_alert.call_args.args[0] == "orchestrator_unreachable"
