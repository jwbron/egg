"""
Gap tests for coordinator feature — targets missing error handling,
boundary conditions, and uncovered branches identified during tester review.

Covers:
- Routes: cancel with None container_id, phase backward skip, empty inputs,
  state save failures, escalation edge cases
- Executor: crash without coordinator state, respawn boundary, error propagation
- MCP tools: HTTP errors, limit validation, missing fields
- MCP server: rate limiter edge cases, tool call with empty args
- Models: boundary values, None handling, serialization round-trips
"""

import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from coordinator_executor import CoordinatorExecutor
from mcp_server import MCPServer, RateLimiter
from mcp_tools import COORDINATOR_TOOLS, CoordinatorToolHandler
from models import (
    AgentRole,
    AgentSpawnRecord,
    CoordinatorState,
    Escalation,
    GuardrailCounters,
    PhaseDecision,
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from pydantic import ValidationError

# ── Helpers ───────────────────────────────────────────────────────────


def _make_pipeline(
    pipeline_id="test-pipeline",
    coordinator_enabled=True,
    coordinator_state=None,
    phase=PipelinePhase.IMPLEMENT,
    status=PipelineStatus.RUNNING,
    max_agents=10,
    max_retries=2,
    max_respawns=2,
):
    config = PipelineConfig(
        coordinator_enabled=coordinator_enabled,
        coordinator_max_agents=max_agents,
        coordinator_max_retries_per_role=max_retries,
        coordinator_max_respawns=max_respawns,
    )
    return Pipeline(
        id=pipeline_id,
        issue_number=42,
        repo="owner/repo",
        branch="egg/test",
        config=config,
        coordinator_state=coordinator_state,
        current_phase=phase,
        status=status,
    )


@pytest.fixture
def app():
    from flask import Flask
    from routes.coordinator import coordinator_bp

    app = Flask(__name__)
    app.register_blueprint(coordinator_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


# ══════════════════════════════════════════════════════════════════════
# GAP 1: Routes — cancel agent with None container_id
# ══════════════════════════════════════════════════════════════════════


class TestCancelAgentNoneContainerId:
    """Cancel an agent whose spawn record has container_id=None.

    Gap: The cancel endpoint accesses container_id[:12] in logging.
    With a None container_id, slicing would crash if not guarded.
    The code guards with `if container_id:` before stopping but
    we need to verify the full path works cleanly.
    """

    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_cancel_agent_with_none_container_id(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, client
    ):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        # Spawn record with container_id=None
        record = AgentSpawnRecord(
            role=AgentRole.CODER,
            status="running",
            container_id=None,
        )
        state = CoordinatorState(agents_spawned=[record])
        pipeline = _make_pipeline(coordinator_state=state)

        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        resp = client.delete("/api/v1/pipelines/test-pipeline/coordinator/agents/coder")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["data"]["container_id"] is None
        # Spawner should NOT have been called since container_id is None
        mock_spawner_fn.return_value.remove_agent_container.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# GAP 2: Routes — phase transitions edge cases
# ══════════════════════════════════════════════════════════════════════


class TestPhaseTransitionEdgeCases:
    """Phase transition gaps: backward skip, skip to current, skip to first."""

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_skip_backward_to_earlier_phase(
        self, mock_repo, mock_lock, mock_store_fn, mock_emit, client
    ):
        """Skip from implement back to refine — the API allows this."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.IMPLEMENT)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        resp = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"target_phase": "refine", "reason": "Need to re-analyze"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["data"]["action"] == "loopback"
        assert data["data"]["current_phase"] == "refine"
        assert data["data"]["previous_phase"] == "implement"

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_skip_to_current_phase(self, mock_repo, mock_lock, mock_store_fn, mock_emit, client):
        """Skip to the same phase we're already on — rejected as no-op."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.IMPLEMENT)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        resp = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"target_phase": "implement", "reason": "Re-entering implement"},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False
        assert "current phase" in data["message"]

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_advance_phase_records_multiple_decisions(
        self, mock_repo, mock_lock, mock_store_fn, mock_emit, client
    ):
        """Multiple phase advances accumulate in coordinator state."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        state = CoordinatorState(
            phase_decisions=[
                PhaseDecision(phase="plan", action="advance", reason="initial"),
            ]
        )
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN, coordinator_state=state)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        resp = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"reason": "Plan complete"},
        )
        assert resp.status_code == 200
        # The pipeline should now have 2 phase decisions
        saved = store.save_pipeline.call_args[0][0]
        assert len(saved.coordinator_state.phase_decisions) == 2

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_advance_phase_empty_reason_rejected(self, mock_repo, mock_lock, mock_store_fn, client):
        """Empty string reason should be rejected (falsy)."""
        mock_repo.return_value = Path("/tmp/repo")

        resp = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"reason": ""},
        )
        assert resp.status_code == 400
        assert "reason" in resp.get_json()["message"].lower()


# ══════════════════════════════════════════════════════════════════════
# GAP 3: Routes — spawn edge cases
# ══════════════════════════════════════════════════════════════════════


class TestSpawnEdgeCases:
    """Spawn endpoint edge cases: empty task_context, extra_env with
    non-string values, retry number calculation with mixed roles."""

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_retry_number_counts_only_matching_role(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        """Retry number for 'tester' should not count coder spawns."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        state = CoordinatorState(
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER, status="complete"),
                AgentSpawnRecord(role=AgentRole.CODER, status="failed"),
                AgentSpawnRecord(role=AgentRole.TESTER, status="complete"),
            ],
            guardrail_counters=GuardrailCounters(
                total_agents_spawned=3,
                retries_by_role={"coder": 2, "tester": 1},
            ),
        )
        pipeline = _make_pipeline(coordinator_state=state, max_agents=20)

        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawner.spawn_agent_container.return_value = MagicMock(
            container_info=MagicMock(container_id="abc123", container_name="test")
        )
        mock_spawner_fn.return_value = spawner

        resp = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "tester"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        # Tester was spawned once before, so retry_number should be 1
        assert data["data"]["spawn_record"]["retry_number"] == 1

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_with_empty_task_context(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        """Task context defaults to empty string when omitted."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawner.spawn_agent_container.return_value = MagicMock(
            container_info=MagicMock(container_id="abc123", container_name="test")
        )
        mock_spawner_fn.return_value = spawner

        resp = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["spawn_record"]["task_context"] == ""

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_with_non_json_body(self, mock_repo, mock_lock, mock_store_fn, client):
        """Non-JSON body returns 415 (Flask rejects non-JSON content type)."""
        mock_repo.return_value = Path("/tmp/repo")

        resp = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            data="not json",
            content_type="text/plain",
        )
        assert resp.status_code == 415

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_unexpected_exception_returns_500(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        """Generic exception during spawn returns 500."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        store = MagicMock()
        store.load_pipeline.side_effect = RuntimeError("disk full")
        mock_store_fn.return_value = store

        resp = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder"},
        )
        assert resp.status_code == 500
        assert "disk full" in resp.get_json()["message"]


# ══════════════════════════════════════════════════════════════════════
# GAP 4: Routes — escalation edge cases
# ══════════════════════════════════════════════════════════════════════


class TestEscalationEdgeCases:
    """Escalation gaps: empty options list for choice type, whitespace-only
    question, feedback type with options (should still succeed)."""

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_decision_queue")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_escalate_choice_with_empty_options_list(
        self, mock_repo, mock_lock, mock_store_fn, mock_queue_fn, mock_emit, client
    ):
        """Choice type with empty options list should fail (falsy check)."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        resp = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/escalate",
            json={
                "question": "Which approach?",
                "escalation_type": "choice",
                "options": [],
            },
        )
        # Empty list is falsy, so this should fail validation
        assert resp.status_code == 400
        assert "options" in resp.get_json()["message"].lower()

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_decision_queue")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_escalate_feedback_type_ignores_options(
        self, mock_repo, mock_lock, mock_store_fn, mock_queue_fn, mock_emit, client
    ):
        """Feedback type should succeed even if options are passed."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        queue = MagicMock()
        queue.queue_decision.return_value = MagicMock(id="d-123")
        mock_queue_fn.return_value = queue

        resp = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/escalate",
            json={
                "question": "What volume?",
                "escalation_type": "feedback",
                "options": ["not used"],
            },
        )
        assert resp.status_code == 200
        # queue_decision should receive options=None for feedback type
        call_kwargs = queue.queue_decision.call_args
        assert call_kwargs.kwargs.get("options") is None or call_kwargs[1].get("options") is None

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_decision_queue")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_escalate_records_multiple_escalations(
        self, mock_repo, mock_lock, mock_store_fn, mock_queue_fn, mock_emit, client
    ):
        """Multiple escalations accumulate in coordinator state."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        state = CoordinatorState(
            escalations=[
                Escalation(question="First question", escalation_type="choice"),
            ]
        )
        pipeline = _make_pipeline(coordinator_state=state)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        queue = MagicMock()
        queue.queue_decision.return_value = MagicMock(id="d-456")
        mock_queue_fn.return_value = queue

        resp = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/escalate",
            json={
                "question": "Second question",
                "escalation_type": "feedback",
            },
        )
        assert resp.status_code == 200
        saved = store.save_pipeline.call_args[0][0]
        assert len(saved.coordinator_state.escalations) == 2

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_escalate_non_json_body(self, mock_repo, mock_lock, mock_store_fn, client):
        """Non-JSON body returns 415 (Flask rejects non-JSON content type)."""
        mock_repo.return_value = Path("/tmp/repo")
        resp = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/escalate",
            data="not json",
            content_type="text/plain",
        )
        assert resp.status_code == 415


# ══════════════════════════════════════════════════════════════════════
# GAP 5: Routes — state endpoint edge cases
# ══════════════════════════════════════════════════════════════════════


class TestStateEndpointEdgeCases:
    """State endpoint gaps: agents with unexpected status, empty decisions."""

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_repo_path")
    def test_state_with_unknown_agent_status(self, mock_repo, mock_store_fn, client):
        """Agents with unexpected status values are excluded from both
        running_agents and completed_agents."""
        mock_repo.return_value = Path("/tmp/repo")

        state = CoordinatorState(
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER, status="running"),
                AgentSpawnRecord(role=AgentRole.TESTER, status="unknown_status"),
                AgentSpawnRecord(role=AgentRole.DOCUMENTER, status="complete"),
            ]
        )
        pipeline = _make_pipeline(coordinator_state=state)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        resp = client.get("/api/v1/pipelines/test-pipeline/coordinator/state")
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert len(data["running_agents"]) == 1
        assert len(data["completed_agents"]) == 1
        # "unknown_status" agent not in either category

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_repo_path")
    def test_state_generic_exception_returns_500(self, mock_repo, mock_store_fn, client):
        """Generic exception returns 500."""
        mock_repo.return_value = Path("/tmp/repo")
        store = MagicMock()
        store.load_pipeline.side_effect = RuntimeError("unexpected")
        mock_store_fn.return_value = store

        resp = client.get("/api/v1/pipelines/test-pipeline/coordinator/state")
        assert resp.status_code == 500


# ══════════════════════════════════════════════════════════════════════
# GAP 6: Executor — crash recovery edge cases
# ══════════════════════════════════════════════════════════════════════


class TestExecutorCrashRecovery:
    """Executor gaps: crash without coordinator_state, respawn exact boundary,
    exit code 0 without coordinator state."""

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_state_store")
    @patch("coordinator_executor.get_pipeline_state_lock")
    def test_crash_without_coordinator_state_creates_new_state(
        self, mock_lock, mock_store_fn, mock_emit, tmp_path
    ):
        """Crash with exit_code=1 when coordinator_state is None should
        create a new CoordinatorState and increment respawn counter."""
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(coordinator_state=None)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.handle_coordinator_completion("test-pipeline", exit_code=1)

        assert result == "respawn"
        saved = store.save_pipeline.call_args[0][0]
        assert saved.coordinator_state is not None
        assert saved.coordinator_state.guardrail_counters.coordinator_respawns == 1

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_state_store")
    @patch("coordinator_executor.get_pipeline_state_lock")
    def test_crash_at_exact_max_respawns_fails(self, mock_lock, mock_store_fn, mock_emit, tmp_path):
        """When respawns == max_respawns, no more respawns allowed."""
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        state = CoordinatorState(
            guardrail_counters=GuardrailCounters(coordinator_respawns=2),
        )
        pipeline = _make_pipeline(coordinator_state=state, max_respawns=2)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.handle_coordinator_completion("test-pipeline", exit_code=1)

        assert result == "failed"
        saved = store.save_pipeline.call_args[0][0]
        assert saved.status == PipelineStatus.FAILED

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_state_store")
    @patch("coordinator_executor.get_pipeline_state_lock")
    def test_crash_one_below_max_respawns_succeeds(
        self, mock_lock, mock_store_fn, mock_emit, tmp_path
    ):
        """When respawns == max_respawns - 1, respawn is allowed."""
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        state = CoordinatorState(
            guardrail_counters=GuardrailCounters(coordinator_respawns=1),
        )
        pipeline = _make_pipeline(coordinator_state=state, max_respawns=2)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.handle_coordinator_completion("test-pipeline", exit_code=1)

        assert result == "respawn"
        saved = store.save_pipeline.call_args[0][0]
        assert saved.coordinator_state.guardrail_counters.coordinator_respawns == 2

    @patch("coordinator_executor.get_state_store")
    @patch("coordinator_executor.get_pipeline_state_lock")
    def test_success_exit_without_coordinator_state(self, mock_lock, mock_store_fn, tmp_path):
        """Exit code 0 without coordinator state sets COMPLETE."""
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(coordinator_state=None)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.handle_coordinator_completion("test-pipeline", exit_code=0)

        assert result == "complete"
        saved = store.save_pipeline.call_args[0][0]
        assert saved.status == PipelineStatus.COMPLETE

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_state_store")
    @patch("coordinator_executor.get_pipeline_state_lock")
    def test_init_coordinator_state_raises_when_disabled(
        self, mock_lock, mock_store_fn, mock_emit, tmp_path
    ):
        """Initialising coordinator state on disabled pipeline raises ValueError."""
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(coordinator_enabled=False)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        with pytest.raises(ValueError, match="does not have coordinator enabled"):
            executor.init_coordinator_state("test-pipeline")

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_state_store")
    @patch("coordinator_executor.get_pipeline_state_lock")
    def test_max_respawns_zero_disables_respawn(
        self, mock_lock, mock_store_fn, mock_emit, tmp_path
    ):
        """max_respawns=0 means coordinator crash immediately fails."""
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        state = CoordinatorState(
            guardrail_counters=GuardrailCounters(coordinator_respawns=0),
        )
        pipeline = _make_pipeline(coordinator_state=state, max_respawns=0)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.handle_coordinator_completion("test-pipeline", exit_code=1)

        assert result == "failed"
        saved = store.save_pipeline.call_args[0][0]
        assert saved.status == PipelineStatus.FAILED
        assert "max respawns reached" in saved.error


# ══════════════════════════════════════════════════════════════════════
# GAP 7: MCP Tools — handler edge cases
# ══════════════════════════════════════════════════════════════════════


class TestMCPToolHandlerGaps:
    """MCP tool handler gaps: HTTP errors, missing fields, limit boundaries."""

    def test_unknown_tool_returns_error(self):
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call("nonexistent_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_handler_exception_returns_error(self):
        """Exception in handler is caught and returned as error dict."""
        handler = CoordinatorToolHandler()
        with patch.object(handler, "_handle_submit_task", side_effect=ValueError("bad data")):
            result = handler.handle_tool_call("submit_task", {})
            assert "error" in result
            assert "bad data" in result["error"]

    def test_list_tasks_with_zero_limit(self):
        """Limit=0 should return empty list."""
        handler = CoordinatorToolHandler()
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "data": {
                    "pipelines": [
                        {
                            "config": {"coordinator_enabled": True},
                            "status": "running",
                        }
                    ]
                }
            }
            result = handler._handle_list_tasks({"limit": 0})
            assert result["tasks"] == []
            assert result["total"] == 1

    def test_list_tasks_default_filter_is_active(self):
        """Default filter is 'active' — only pending/running/awaiting_human."""
        handler = CoordinatorToolHandler()
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "data": {
                    "pipelines": [
                        {"config": {"coordinator_enabled": True}, "status": "running"},
                        {"config": {"coordinator_enabled": True}, "status": "complete"},
                        {"config": {"coordinator_enabled": True}, "status": "failed"},
                        {"config": {"coordinator_enabled": True}, "status": "pending"},
                    ]
                }
            }
            result = handler._handle_list_tasks({})
            assert result["total"] == 2  # running + pending

    def test_list_tasks_all_filter_returns_everything(self):
        """'all' filter returns everything."""
        handler = CoordinatorToolHandler()
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "data": {
                    "pipelines": [
                        {"config": {"coordinator_enabled": True}, "status": "running"},
                        {"config": {"coordinator_enabled": True}, "status": "complete"},
                        {"config": {"coordinator_enabled": False}, "status": "running"},
                    ]
                }
            }
            result = handler._handle_list_tasks({"status_filter": "all"})
            # Only coordinator-enabled pipelines
            assert result["total"] == 2

    def test_list_tasks_excludes_non_coordinator_pipelines(self):
        """Non-coordinator pipelines are never returned."""
        handler = CoordinatorToolHandler()
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "data": {
                    "pipelines": [
                        {"config": {"coordinator_enabled": False}, "status": "running"},
                        {"config": {}, "status": "running"},
                    ]
                }
            }
            result = handler._handle_list_tasks({"status_filter": "all"})
            assert result["total"] == 0

    def test_submit_task_with_issue_number(self):
        """submit_task with issue sets issue_number."""
        handler = CoordinatorToolHandler()
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"data": {"pipeline": {"id": "issue-42"}}}
            result = handler._handle_submit_task({"description": "Fix bug", "issue_number": 42})
            # First call is the pipeline create; second is /start
            assert mock_req.call_count == 2
            call_data = mock_req.call_args_list[0][1]["data"]
            assert "mode" not in call_data
            assert call_data["issue_number"] == 42
            assert call_data["branch"] == "egg/issue-42"
            assert result["task_id"] == "issue-42"
            start_call = mock_req.call_args_list[1]
            assert "/start" in start_call[0][0]
            assert start_call[1]["method"] == "POST"

    def test_submit_task_with_issue_and_branch_override(self):
        """submit_task with issue_number and explicit branch uses the override."""
        handler = CoordinatorToolHandler()
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"data": {"pipeline": {"id": "issue-42"}}}
            handler._handle_submit_task(
                {"description": "Fix bug", "issue_number": 42, "branch": "egg/my-branch"}
            )
            call_data = mock_req.call_args_list[0][1]["data"]
            assert call_data["mode"] == "issue"
            assert call_data["issue_number"] == 42
            assert call_data["branch"] == "egg/my-branch"

    def test_submit_task_without_issue_number(self):
        """submit_task without issue includes prompt."""
        handler = CoordinatorToolHandler()
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"data": {"pipeline": {"id": "local-abc123"}}}
            handler._handle_submit_task({"description": "Refactor auth"})
            # First call is the pipeline create; second is /start
            assert mock_req.call_count == 2
            call_data = mock_req.call_args_list[0][1]["data"]
            assert "mode" not in call_data
            assert call_data["prompt"] == "Refactor auth"
            start_call = mock_req.call_args_list[1]
            assert "/start" in start_call[0][0]
            assert start_call[1]["method"] == "POST"

    def test_cancel_task_passes_reason(self):
        """cancel_task uses PATCH with status and reason."""
        handler = CoordinatorToolHandler()
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True}
            handler._handle_cancel_task({"task_id": "issue-42", "reason": "No longer needed"})
            mock_req.assert_called_once_with(
                "/api/v1/pipelines/issue-42",
                method="PATCH",
                data={"status": "cancelled", "reason": "No longer needed"},
            )

    def test_provide_input_calls_correct_endpoint(self):
        """provide_input posts to the correct decision endpoint."""
        handler = CoordinatorToolHandler()
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True}
            handler._handle_provide_input(
                {"task_id": "issue-42", "decision_id": "d-1", "response": "Option A"}
            )
            endpoint = mock_req.call_args[0][0]
            assert "issue-42" in endpoint
            assert "d-1" in endpoint


# ══════════════════════════════════════════════════════════════════════
# GAP 8: MCP Server — rate limiter and endpoint edge cases
# ══════════════════════════════════════════════════════════════════════


class TestMCPServerGaps:
    """MCP server gaps: rate limiter window reset, health check, tool
    registration, and rate limiting via Streamable HTTP transport."""

    def test_rate_limiter_allows_after_window_expires(self):
        """After window expires, requests should be allowed again."""
        limiter = RateLimiter(max_requests=1, window_seconds=1)
        assert limiter.allow() is True
        assert limiter.allow() is False
        time.sleep(1.1)
        assert limiter.allow() is True

    def test_rate_limiter_zero_max_blocks_all(self):
        """max_requests=0 blocks everything."""
        limiter = RateLimiter(max_requests=0)
        assert limiter.allow() is False

    def test_mcp_server_health_endpoint(self):
        from starlette.testclient import TestClient

        server = MCPServer()
        mcp = server.create_app()
        app = mcp.streamable_http_app()
        with TestClient(app) as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "healthy"

    def test_mcp_server_registers_all_five_tools(self):
        from starlette.testclient import TestClient

        server = MCPServer()
        mcp = server.create_app()
        app = mcp.streamable_http_app()
        with TestClient(app) as client:
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            init_resp = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"},
                    },
                },
                headers=headers,
            )
            session_id = init_resp.headers.get("mcp-session-id")
            if session_id:
                headers["mcp-session-id"] = session_id
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            )
            resp = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers=headers,
            )
            assert resp.status_code == 200
            tools = resp.json()["result"]["tools"]
            assert len(tools) == 5
            names = {t["name"] for t in tools}
            assert names == {
                "submit_task",
                "get_status",
                "provide_input",
                "list_tasks",
                "cancel_task",
            }

    def test_mcp_server_mcp_endpoint_responds(self):
        from starlette.testclient import TestClient

        server = MCPServer()
        mcp = server.create_app()
        app = mcp.streamable_http_app()
        with TestClient(app) as client:
            # Send an MCP initialize request
            resp = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"},
                    },
                },
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("jsonrpc") == "2.0"
            assert "result" in data

    def test_mcp_server_rate_limit_in_tool_response(self):
        import json
        from unittest.mock import patch as _patch

        from starlette.testclient import TestClient

        server = MCPServer(rate_limit=1)
        mcp = server.create_app()
        app = mcp.streamable_http_app()

        with (
            _patch.object(CoordinatorToolHandler, "handle_tool_call", return_value={"ok": True}),
            TestClient(app) as client,
        ):
            # Initialize
            init_resp = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"},
                    },
                },
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            session_id = init_resp.headers.get("mcp-session-id")
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if session_id:
                headers["mcp-session-id"] = session_id

            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            )

            # First call succeeds
            client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {"name": "get_status", "arguments": {"task_id": "test"}},
                },
                headers=headers,
            )
            # Second call hits rate limit
            resp2 = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {"name": "get_status", "arguments": {"task_id": "test"}},
                },
                headers=headers,
            )
            assert resp2.status_code == 200
            data = resp2.json()
            result_text = data["result"]["content"][0]["text"]
            result = json.loads(result_text)
            assert "Rate limit exceeded" in result.get("error", "")


# ══════════════════════════════════════════════════════════════════════
# GAP 9: Models — boundary values and serialization
# ══════════════════════════════════════════════════════════════════════


class TestModelBoundaryGaps:
    """Model gaps: None container_id serialization, empty lists,
    guardrail counter boundaries."""

    def test_spawn_record_none_container_id_serializes(self):
        """AgentSpawnRecord with None container_id serializes cleanly."""
        record = AgentSpawnRecord(
            role=AgentRole.CODER,
            container_id=None,
        )
        d = record.model_dump(mode="json")
        assert d["container_id"] is None
        # Round-trip
        restored = AgentSpawnRecord.model_validate(d)
        assert restored.container_id is None

    def test_coordinator_state_empty_lists_serialize(self):
        """Empty CoordinatorState serializes and deserializes cleanly."""
        state = CoordinatorState()
        d = state.model_dump(mode="json")
        assert d["agents_spawned"] == []
        assert d["phase_decisions"] == []
        assert d["escalations"] == []
        restored = CoordinatorState.model_validate(d)
        assert restored.agents_spawned == []

    def test_guardrail_counters_default_values(self):
        """Default guardrail counters are all zero."""
        counters = GuardrailCounters()
        assert counters.total_agents_spawned == 0
        assert counters.coordinator_respawns == 0
        assert counters.retries_by_role == {}

    def test_guardrail_counters_reject_negative(self):
        """Negative values should be rejected by ge=0 constraint."""
        with pytest.raises(ValidationError):
            GuardrailCounters(total_agents_spawned=-1)

    def test_escalation_resolved_fields(self):
        """Escalation with resolved_at and resolution."""
        now = datetime.utcnow()
        esc = Escalation(
            question="Which DB?",
            escalation_type="choice",
            resolved_at=now,
            resolution="PostgreSQL",
        )
        d = esc.model_dump(mode="json")
        assert d["resolution"] == "PostgreSQL"
        restored = Escalation.model_validate(d)
        assert restored.resolution == "PostgreSQL"

    def test_phase_decision_loopback_action(self):
        """PhaseDecision with loopback action."""
        pd = PhaseDecision(
            phase="implement",
            action="loopback",
            reason="Tester found edge case",
        )
        assert pd.action == "loopback"
        d = pd.model_dump(mode="json")
        restored = PhaseDecision.model_validate(d)
        assert restored.action == "loopback"

    def test_pipeline_config_coordinator_fields_defaults(self):
        """PipelineConfig coordinator fields have correct defaults."""
        config = PipelineConfig()
        assert config.coordinator_enabled is False
        assert config.coordinator_max_agents == 10
        assert config.coordinator_max_retries_per_role == 2
        assert config.coordinator_max_respawns == 2

    def test_pipeline_config_coordinator_max_agents_min_1(self):
        """coordinator_max_agents must be >= 1."""
        with pytest.raises(ValidationError):
            PipelineConfig(coordinator_max_agents=0)

    def test_pipeline_with_coordinator_state_round_trip(self):
        """Full Pipeline with coordinator state survives serialization."""
        state = CoordinatorState(
            workflow_type="bug_fix",
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER, status="complete"),
            ],
            phase_decisions=[
                PhaseDecision(phase="implement", action="advance", reason="done"),
            ],
            escalations=[
                Escalation(question="Approach?", escalation_type="choice"),
            ],
            guardrail_counters=GuardrailCounters(
                total_agents_spawned=1,
                retries_by_role={"coder": 1},
                coordinator_respawns=0,
            ),
        )
        pipeline = _make_pipeline(coordinator_state=state)
        d = pipeline.model_dump(mode="json")
        restored = Pipeline.model_validate(d)
        assert restored.coordinator_state.workflow_type == "bug_fix"
        assert len(restored.coordinator_state.agents_spawned) == 1
        assert len(restored.coordinator_state.phase_decisions) == 1
        assert len(restored.coordinator_state.escalations) == 1
        assert restored.coordinator_state.guardrail_counters.total_agents_spawned == 1


# ══════════════════════════════════════════════════════════════════════
# GAP 10: Tool definitions validation
# ══════════════════════════════════════════════════════════════════════


class TestToolDefinitionGaps:
    """Validate MCP tool definition schemas are well-formed."""

    def test_all_tools_have_required_fields(self):
        """Every tool has name, description, inputSchema."""
        for tool in COORDINATOR_TOOLS:
            assert "name" in tool, f"Tool missing name: {tool}"
            assert "description" in tool, f"Tool {tool['name']} missing description"
            assert "inputSchema" in tool, f"Tool {tool['name']} missing inputSchema"

    def test_submit_task_requires_description_and_repo(self):
        """submit_task schema has 'description' and 'repo' as required."""
        tool = next(t for t in COORDINATOR_TOOLS if t["name"] == "submit_task")
        assert "description" in tool["inputSchema"]["required"]
        assert "repo" in tool["inputSchema"]["required"]

    def test_provide_input_requires_all_fields(self):
        """provide_input requires task_id, decision_id, response."""
        tool = next(t for t in COORDINATOR_TOOLS if t["name"] == "provide_input")
        required = set(tool["inputSchema"]["required"])
        assert required == {"task_id", "decision_id", "response"}

    def test_cancel_task_requires_task_id(self):
        """cancel_task requires task_id."""
        tool = next(t for t in COORDINATOR_TOOLS if t["name"] == "cancel_task")
        assert "task_id" in tool["inputSchema"]["required"]

    def test_list_tasks_has_no_required_fields(self):
        """list_tasks has no required fields — all optional."""
        tool = next(t for t in COORDINATOR_TOOLS if t["name"] == "list_tasks")
        assert "required" not in tool["inputSchema"] or tool["inputSchema"]["required"] == []
