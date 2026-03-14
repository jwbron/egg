"""
Functional tests for coordinator API routes.

Tests the REST endpoints for agent spawning, cancellation, phase management,
state retrieval, and HITL escalation using Flask test client with mocked
state store and container spawner.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from models import (
    AgentRole,
    AgentSpawnRecord,
    ContainerInfo,
    ContainerStatus,
    CoordinatorState,
    GuardrailCounters,
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)


def _make_pipeline(
    pipeline_id="test-pipeline",
    coordinator_enabled=True,
    coordinator_state=None,
    phase=PipelinePhase.IMPLEMENT,
    status=PipelineStatus.RUNNING,
    max_agents=10,
    max_retries=2,
):
    """Create a test Pipeline with coordinator config."""
    config = PipelineConfig(
        coordinator_enabled=coordinator_enabled,
        coordinator_max_agents=max_agents,
        coordinator_max_retries_per_role=max_retries,
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
    """Create a test Flask app with the coordinator blueprint."""
    from flask import Flask
    from routes.coordinator import coordinator_bp

    app = Flask(__name__)
    app.register_blueprint(coordinator_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


# ── Spawn endpoint tests ────────────────────────────────────────────


class TestSpawnEndpoint:
    """POST /api/v1/pipelines/{id}/coordinator/spawn"""

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_success(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawned = MagicMock()
        spawned.container_info = ContainerInfo(
            container_id="abc123def456",
            container_name="egg-test-coder",
            status=ContainerStatus.RUNNING,
        )
        spawner.spawn_agent_container.return_value = spawned
        mock_spawner_fn.return_value = spawner

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder", "task_context": "Fix the bug"},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["data"]["role"] == "coder"
        assert data["data"]["container_id"] == "abc123def456"
        store.save_pipeline.assert_called_once()

    @patch("routes.coordinator.get_repo_path")
    def test_spawn_missing_body(self, mock_repo, client):
        mock_repo.return_value = Path("/tmp/repo")
        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            content_type="application/json",
        )
        assert response.status_code == 400

    @patch("routes.coordinator.get_repo_path")
    def test_spawn_missing_role(self, mock_repo, client):
        mock_repo.return_value = Path("/tmp/repo")
        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"task_context": "Fix something"},
        )
        assert response.status_code == 400
        assert "role" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_repo_path")
    def test_spawn_invalid_role(self, mock_repo, client):
        mock_repo.return_value = Path("/tmp/repo")

        store = MagicMock()
        store.load_pipeline.return_value = _make_pipeline()

        with (
            patch("routes.coordinator.get_state_store", return_value=store),
            patch("routes.coordinator.get_pipeline_state_lock") as mock_lock,
        ):
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)

            response = client.post(
                "/api/v1/pipelines/test-pipeline/coordinator/spawn",
                json={"role": "nonexistent_role"},
            )
        assert response.status_code == 400
        assert "invalid role" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_repo_path")
    def test_spawn_invalid_extra_env_type(self, mock_repo, client):
        mock_repo.return_value = Path("/tmp/repo")
        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder", "extra_env": "not_a_dict"},
        )
        assert response.status_code == 400
        assert "extra_env" in response.get_json()["message"]

    @patch("routes.coordinator.get_repo_path")
    def test_spawn_coordinator_role_rejected(self, mock_repo, client):
        """Coordinator cannot spawn another coordinator (privilege escalation)."""
        mock_repo.return_value = Path("/tmp/repo")
        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coordinator"},
        )
        assert response.status_code == 403
        assert "cannot spawn another coordinator" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_coordinator_disabled(self, mock_repo, mock_lock, mock_store_fn, client):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(coordinator_enabled=False)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder"},
        )
        assert response.status_code == 403
        assert "not enabled" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_guardrail_max_agents(self, mock_repo, mock_lock, mock_store_fn, client):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            max_agents=2,
            coordinator_state=CoordinatorState(
                guardrail_counters=GuardrailCounters(total_agents_spawned=2),
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder"},
        )
        assert response.status_code == 429
        assert "max agents" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_guardrail_max_retries_per_role(
        self, mock_repo, mock_lock, mock_store_fn, client
    ):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            max_retries=1,
            coordinator_state=CoordinatorState(
                guardrail_counters=GuardrailCounters(
                    total_agents_spawned=1,
                    retries_by_role={"coder": 1},
                ),
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder"},
        )
        assert response.status_code == 429
        assert "retries" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_pipeline_not_found(self, mock_repo, mock_store_fn, client):
        from state_store import PipelineNotFoundError

        mock_repo.return_value = Path("/tmp/repo")
        store = MagicMock()
        store.load_pipeline.side_effect = PipelineNotFoundError("test-pipeline")
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder"},
        )
        assert response.status_code == 404

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_invalid_pipeline_id(self, mock_repo, mock_store_fn, client):
        from state_store import InvalidPipelineIdError

        mock_repo.return_value = Path("/tmp/repo")
        store = MagicMock()
        store.load_pipeline.side_effect = InvalidPipelineIdError("bad!id")
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/bad!id/coordinator/spawn",
            json={"role": "coder"},
        )
        assert response.status_code == 400

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_increments_guardrail_counters(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        # tester depends on coder — provide a completed coder record
        pipeline = _make_pipeline(
            coordinator_state=CoordinatorState(
                guardrail_counters=GuardrailCounters(total_agents_spawned=1),
                agents_spawned=[
                    AgentSpawnRecord(role=AgentRole.CODER, status="complete"),
                ],
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawned = MagicMock()
        spawned.container_info = ContainerInfo(
            container_id="xyz789", container_name="egg-test-tester"
        )
        spawner.spawn_agent_container.return_value = spawned
        mock_spawner_fn.return_value = spawner

        client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "tester"},
        )

        # Verify counters were updated
        assert pipeline.coordinator_state.guardrail_counters.total_agents_spawned == 2
        assert pipeline.coordinator_state.guardrail_counters.retries_by_role.get("tester") == 1

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_container_error_returns_500(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        from container_spawner import ContainerSpawnError

        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawner.spawn_agent_container.side_effect = ContainerSpawnError("Docker failed")
        mock_spawner_fn.return_value = spawner

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder"},
        )
        assert response.status_code == 500
        assert "docker failed" in response.get_json()["message"].lower()

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_initializes_coordinator_state_if_none(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(coordinator_state=None)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawned = MagicMock()
        spawned.container_info = ContainerInfo(
            container_id="abc123", container_name="egg-test-coder"
        )
        spawner.spawn_agent_container.return_value = spawned
        mock_spawner_fn.return_value = spawner

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder"},
        )
        assert response.status_code == 200
        assert pipeline.coordinator_state is not None
        assert len(pipeline.coordinator_state.agents_spawned) == 1


# ── Cancel endpoint tests ───────────────────────────────────────────


class TestCancelEndpoint:
    """DELETE /api/v1/pipelines/{id}/coordinator/agents/{role}"""

    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_cancel_success(self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, client):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            coordinator_state=CoordinatorState(
                agents_spawned=[
                    AgentSpawnRecord(
                        role=AgentRole.CODER,
                        status="running",
                        container_id="container-abc",
                    ),
                ],
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        mock_spawner_fn.return_value = spawner

        response = client.delete("/api/v1/pipelines/test-pipeline/coordinator/agents/coder")

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["data"]["role"] == "coder"
        # Verify record was updated
        assert pipeline.coordinator_state.agents_spawned[0].status == "cancelled"
        assert pipeline.coordinator_state.agents_spawned[0].completed_at is not None

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_cancel_no_running_agent(self, mock_repo, mock_lock, mock_store_fn, client):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            coordinator_state=CoordinatorState(
                agents_spawned=[
                    AgentSpawnRecord(role=AgentRole.CODER, status="complete"),
                ],
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.delete("/api/v1/pipelines/test-pipeline/coordinator/agents/coder")
        assert response.status_code == 404
        assert "no running agent" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_repo_path")
    def test_cancel_invalid_role(self, mock_repo, client):
        mock_repo.return_value = Path("/tmp/repo")

        store = MagicMock()
        store.load_pipeline.return_value = _make_pipeline()

        with (
            patch("routes.coordinator.get_state_store", return_value=store),
            patch("routes.coordinator.get_pipeline_state_lock") as mock_lock,
        ):
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)

            response = client.delete(
                "/api/v1/pipelines/test-pipeline/coordinator/agents/bogus_role"
            )
        assert response.status_code == 400

    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_cancel_container_stop_failure_still_marks_cancelled(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, client
    ):
        """Container stop failure should not prevent record from being marked cancelled."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            coordinator_state=CoordinatorState(
                agents_spawned=[
                    AgentSpawnRecord(
                        role=AgentRole.CODER,
                        status="running",
                        container_id="container-abc",
                    ),
                ],
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawner.remove_agent_container.side_effect = Exception("Docker not reachable")
        mock_spawner_fn.return_value = spawner

        response = client.delete("/api/v1/pipelines/test-pipeline/coordinator/agents/coder")
        assert response.status_code == 200
        assert pipeline.coordinator_state.agents_spawned[0].status == "cancelled"

    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_cancel_most_recent_running(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, client
    ):
        """When multiple spawns exist for a role, cancel the most recent running one."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            coordinator_state=CoordinatorState(
                agents_spawned=[
                    AgentSpawnRecord(
                        role=AgentRole.CODER,
                        status="complete",
                        container_id="old-container",
                        retry_number=0,
                    ),
                    AgentSpawnRecord(
                        role=AgentRole.CODER,
                        status="running",
                        container_id="new-container",
                        retry_number=1,
                    ),
                ],
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        mock_spawner_fn.return_value = spawner

        response = client.delete("/api/v1/pipelines/test-pipeline/coordinator/agents/coder")
        assert response.status_code == 200
        # First record (complete) should be unchanged
        assert pipeline.coordinator_state.agents_spawned[0].status == "complete"
        # Second record (running) should be cancelled
        assert pipeline.coordinator_state.agents_spawned[1].status == "cancelled"

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_cancel_empty_coordinator_state(self, mock_repo, mock_lock, mock_store_fn, client):
        """Cancel with no coordinator state should return 404."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(coordinator_state=None)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.delete("/api/v1/pipelines/test-pipeline/coordinator/agents/coder")
        assert response.status_code == 404


# ── State endpoint tests ────────────────────────────────────────────


class TestStateEndpoint:
    """GET /api/v1/pipelines/{id}/coordinator/state"""

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_repo_path")
    def test_state_success(self, mock_repo, mock_store_fn, client):
        mock_repo.return_value = Path("/tmp/repo")

        pipeline = _make_pipeline(
            coordinator_state=CoordinatorState(
                workflow_type="bug_fix",
                agents_spawned=[
                    AgentSpawnRecord(role=AgentRole.CODER, status="running"),
                    AgentSpawnRecord(role=AgentRole.TESTER, status="complete"),
                ],
                guardrail_counters=GuardrailCounters(total_agents_spawned=2),
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.get("/api/v1/pipelines/test-pipeline/coordinator/state")

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["current_phase"] == "implement"
        assert data["status"] == "running"
        assert len(data["running_agents"]) == 1
        assert len(data["completed_agents"]) == 1
        assert data["coordinator_state"]["workflow_type"] == "bug_fix"

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_repo_path")
    def test_state_no_coordinator_state(self, mock_repo, mock_store_fn, client):
        """Pipeline without coordinator_state should return empty defaults."""
        mock_repo.return_value = Path("/tmp/repo")

        pipeline = _make_pipeline(coordinator_state=None)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.get("/api/v1/pipelines/test-pipeline/coordinator/state")

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert len(data["running_agents"]) == 0
        assert len(data["completed_agents"]) == 0

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_repo_path")
    def test_state_categorizes_agents_correctly(self, mock_repo, mock_store_fn, client):
        """Agents should be categorized by status: running vs completed/failed/cancelled."""
        mock_repo.return_value = Path("/tmp/repo")

        pipeline = _make_pipeline(
            coordinator_state=CoordinatorState(
                agents_spawned=[
                    AgentSpawnRecord(role=AgentRole.CODER, status="running"),
                    AgentSpawnRecord(role=AgentRole.TESTER, status="complete"),
                    AgentSpawnRecord(role=AgentRole.DOCUMENTER, status="failed"),
                    AgentSpawnRecord(role=AgentRole.CODER, status="cancelled"),
                ],
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.get("/api/v1/pipelines/test-pipeline/coordinator/state")

        data = response.get_json()["data"]
        assert len(data["running_agents"]) == 1
        assert len(data["completed_agents"]) == 3  # complete + failed + cancelled

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_repo_path")
    def test_state_pipeline_not_found(self, mock_repo, mock_store_fn, client):
        from state_store import PipelineNotFoundError

        mock_repo.return_value = Path("/tmp/repo")
        store = MagicMock()
        store.load_pipeline.side_effect = PipelineNotFoundError("nope")
        mock_store_fn.return_value = store

        response = client.get("/api/v1/pipelines/nope/coordinator/state")
        assert response.status_code == 404


# ── Phase advance endpoint tests ────────────────────────────────────


class TestPhaseEndpoint:
    """POST /api/v1/pipelines/{id}/coordinator/phase"""

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_advance_phase_success(self, mock_repo, mock_lock, mock_store_fn, mock_emit, client):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"reason": "Plan approved by coordinator"},
        )

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["previous_phase"] == "plan"
        assert data["current_phase"] == "implement"
        assert data["action"] == "advance"
        assert pipeline.current_phase == PipelinePhase.IMPLEMENT

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_skip_to_target_phase(self, mock_repo, mock_lock, mock_store_fn, mock_emit, client):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"target_phase": "implement", "reason": "Simple bug fix, skip planning"},
        )

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["previous_phase"] == "refine"
        assert data["current_phase"] == "implement"
        assert data["action"] == "skip"

    @patch("routes.coordinator.get_repo_path")
    def test_advance_phase_missing_reason(self, mock_repo, client):
        mock_repo.return_value = Path("/tmp/repo")
        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"target_phase": "implement"},
        )
        assert response.status_code == 400
        assert "reason" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_advance_beyond_final_phase(self, mock_repo, mock_lock, mock_store_fn, client):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.PR)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"reason": "Try to advance past final"},
        )
        assert response.status_code == 400
        assert "final phase" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_skip_to_invalid_phase(self, mock_repo, mock_lock, mock_store_fn, client):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"target_phase": "nonexistent", "reason": "test"},
        )
        assert response.status_code == 400
        assert "invalid target_phase" in response.get_json()["message"].lower()

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_phase_advance_records_decision(
        self, mock_repo, mock_lock, mock_store_fn, mock_emit, client
    ):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"reason": "All good"},
        )

        assert pipeline.coordinator_state is not None
        assert len(pipeline.coordinator_state.phase_decisions) == 1
        assert pipeline.coordinator_state.phase_decisions[0].action == "advance"
        assert pipeline.coordinator_state.phase_decisions[0].reason == "All good"

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_phase_advance_coordinator_disabled(self, mock_repo, mock_lock, mock_store_fn, client):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(coordinator_enabled=False)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"reason": "test"},
        )
        assert response.status_code == 403


# ── Contract enforcement tests ─────────────────────────────────────


class TestContractEnforcement:
    """Phase advancement must be blocked when no contract exists."""

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_advance_to_implement_blocked_without_contract(
        self, mock_repo, mock_lock, mock_store_fn, client
    ):
        """Advancing from plan to implement must fail if contract_synced is False."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        pipeline.contract_synced = False
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"reason": "Plan approved"},
        )

        assert response.status_code == 409
        assert "contract" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_skip_to_implement_blocked_without_contract(
        self, mock_repo, mock_lock, mock_store_fn, client
    ):
        """Skipping directly to implement must fail if contract_synced is False."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        pipeline.contract_synced = False
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"target_phase": "implement", "reason": "Skip to implement"},
        )

        assert response.status_code == 409
        assert "contract" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_skip_to_pr_blocked_without_contract(self, mock_repo, mock_lock, mock_store_fn, client):
        """Skipping to PR phase must also fail if contract_synced is False."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        pipeline.contract_synced = False
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"target_phase": "pr", "reason": "Skip everything"},
        )

        assert response.status_code == 409
        assert "contract" in response.get_json()["message"].lower()

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_advance_to_implement_allowed_with_contract(
        self, mock_repo, mock_lock, mock_store_fn, mock_emit, client
    ):
        """Advancing to implement succeeds when contract_synced is True."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        pipeline.contract_synced = True
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"reason": "Plan approved"},
        )

        assert response.status_code == 200
        assert response.get_json()["data"]["current_phase"] == "implement"

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_advance_to_plan_allowed_without_contract(
        self, mock_repo, mock_lock, mock_store_fn, mock_emit, client
    ):
        """Advancing to plan (before implement) is allowed without a contract."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        pipeline.contract_synced = False
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"reason": "Refine complete"},
        )

        assert response.status_code == 200
        assert response.get_json()["data"]["current_phase"] == "plan"

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_loopback_to_implement_blocked_without_contract(
        self, mock_repo, mock_lock, mock_store_fn, client
    ):
        """Looping back from PR to implement must fail if contract_synced is False."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.PR)
        pipeline.contract_synced = False
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/phase",
            json={"target_phase": "implement", "reason": "Need more implementation"},
        )

        assert response.status_code == 409
        assert "contract" in response.get_json()["message"].lower()


# ── Escalation endpoint tests ───────────────────────────────────────


class TestEscalateEndpoint:
    """POST /api/v1/pipelines/{id}/coordinator/escalate"""

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_decision_queue")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_escalate_choice_success(
        self, mock_repo, mock_lock, mock_store_fn, mock_queue_fn, mock_emit, client
    ):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        mock_decision = MagicMock()
        mock_decision.id = "d-123"
        mock_queue = MagicMock()
        mock_queue.queue_decision.return_value = mock_decision
        mock_queue_fn.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/escalate",
            json={
                "question": "REST or GraphQL?",
                "escalation_type": "choice",
                "options": ["REST", "GraphQL"],
            },
        )

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["question"] == "REST or GraphQL?"
        assert data["escalation_type"] == "choice"
        assert data["decision_id"] == "d-123"

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_decision_queue")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_escalate_feedback_success(
        self, mock_repo, mock_lock, mock_store_fn, mock_queue_fn, mock_emit, client
    ):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        mock_decision = MagicMock()
        mock_decision.id = "d-456"
        mock_queue = MagicMock()
        mock_queue.queue_decision.return_value = mock_decision
        mock_queue_fn.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/escalate",
            json={
                "question": "What traffic volume expected?",
                "escalation_type": "feedback",
            },
        )

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["escalation_type"] == "feedback"
        # For feedback type, options should not be passed to queue
        call_kwargs = mock_queue.queue_decision.call_args
        assert call_kwargs.kwargs.get("options") is None or call_kwargs[1].get("options") is None

    @patch("routes.coordinator.get_repo_path")
    def test_escalate_missing_question(self, mock_repo, client):
        mock_repo.return_value = Path("/tmp/repo")
        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/escalate",
            json={"escalation_type": "choice", "options": ["A", "B"]},
        )
        assert response.status_code == 400
        assert "question" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_repo_path")
    def test_escalate_missing_type(self, mock_repo, client):
        mock_repo.return_value = Path("/tmp/repo")
        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/escalate",
            json={"question": "What approach?"},
        )
        assert response.status_code == 400
        assert "escalation_type" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_repo_path")
    def test_escalate_invalid_type(self, mock_repo, client):
        mock_repo.return_value = Path("/tmp/repo")
        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/escalate",
            json={
                "question": "What approach?",
                "escalation_type": "invalid_type",
            },
        )
        assert response.status_code == 400
        assert "invalid escalation_type" in response.get_json()["message"].lower()

    @patch("routes.coordinator.get_repo_path")
    def test_escalate_choice_missing_options(self, mock_repo, client):
        mock_repo.return_value = Path("/tmp/repo")
        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/escalate",
            json={
                "question": "Which approach?",
                "escalation_type": "choice",
            },
        )
        assert response.status_code == 400
        assert "options" in response.get_json()["message"].lower()

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_decision_queue")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_escalate_records_in_coordinator_state(
        self, mock_repo, mock_lock, mock_store_fn, mock_queue_fn, mock_emit, client
    ):
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(coordinator_state=None)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        mock_decision = MagicMock()
        mock_decision.id = "d-789"
        mock_queue = MagicMock()
        mock_queue.queue_decision.return_value = mock_decision
        mock_queue_fn.return_value = mock_queue

        client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/escalate",
            json={
                "question": "REST or GraphQL?",
                "escalation_type": "choice",
                "options": ["REST", "GraphQL"],
            },
        )

        assert pipeline.coordinator_state is not None
        assert len(pipeline.coordinator_state.escalations) == 1
        assert pipeline.coordinator_state.escalations[0].question == "REST or GraphQL?"


# ── Guardrail helper tests ──────────────────────────────────────────


class TestGuardrailHelper:
    """Tests for _check_spawn_guardrails helper function."""

    def test_guardrails_allow_within_limits(self):
        from routes.coordinator import _check_spawn_guardrails

        pipeline = _make_pipeline(
            max_agents=10,
            max_retries=2,
            coordinator_state=CoordinatorState(
                guardrail_counters=GuardrailCounters(
                    total_agents_spawned=5,
                    retries_by_role={"coder": 1},
                ),
            ),
        )
        allowed, reason = _check_spawn_guardrails(pipeline, "coder")
        assert allowed is True
        assert reason == ""

    def test_guardrails_block_max_agents(self):
        from routes.coordinator import _check_spawn_guardrails

        pipeline = _make_pipeline(
            max_agents=3,
            coordinator_state=CoordinatorState(
                guardrail_counters=GuardrailCounters(total_agents_spawned=3),
            ),
        )
        allowed, reason = _check_spawn_guardrails(pipeline, "coder")
        assert allowed is False
        assert "max agents" in reason.lower()

    def test_guardrails_block_max_retries(self):
        from routes.coordinator import _check_spawn_guardrails

        pipeline = _make_pipeline(
            max_retries=2,
            coordinator_state=CoordinatorState(
                guardrail_counters=GuardrailCounters(
                    total_agents_spawned=3,
                    retries_by_role={"coder": 2},
                ),
            ),
        )
        allowed, reason = _check_spawn_guardrails(pipeline, "coder")
        assert allowed is False
        assert "retries" in reason.lower()

    def test_guardrails_allow_new_role(self):
        """A role with no retries should be allowed even if others are at limit."""
        from routes.coordinator import _check_spawn_guardrails

        pipeline = _make_pipeline(
            max_retries=2,
            coordinator_state=CoordinatorState(
                guardrail_counters=GuardrailCounters(
                    total_agents_spawned=3,
                    retries_by_role={"coder": 2},
                ),
            ),
        )
        allowed, reason = _check_spawn_guardrails(pipeline, "tester")
        assert allowed is True

    def test_guardrails_no_coordinator_state(self):
        """Pipeline without coordinator_state should allow spawning."""
        from routes.coordinator import _check_spawn_guardrails

        pipeline = _make_pipeline(coordinator_state=None)
        allowed, reason = _check_spawn_guardrails(pipeline, "coder")
        assert allowed is True


# ── Phase-role validation tests ────────────────────────────────────


class TestPhaseRoleValidation:
    """Tests for phase-role validation in spawn endpoint."""

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_wrong_role_for_phase_rejected(self, mock_repo, mock_lock, mock_store_fn, client):
        """Spawning coder in refine phase should be rejected."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder"},
        )
        assert response.status_code == 400
        body = response.get_json()
        assert "not valid for phase" in body["message"]
        assert body["details"]["phase"] == "refine"
        assert "refiner" in body["details"]["valid_roles"]

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_correct_role_for_refine_phase(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        """Spawning refiner in refine phase should succeed."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawned = MagicMock()
        spawned.container_info = ContainerInfo(
            container_id="ref123", container_name="egg-test-refiner"
        )
        spawner.spawn_agent_container.return_value = spawned
        mock_spawner_fn.return_value = spawner

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "refiner"},
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["role"] == "refiner"

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_reviewer_role_allowed_for_phase(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        """Reviewer roles should be allowed for their corresponding phase
        when all dependencies have completed."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        # reviewer_code depends on integrator, task_planner, risk_analyst
        completed_deps = [
            AgentSpawnRecord(role=AgentRole.INTEGRATOR, status="complete"),
            AgentSpawnRecord(role=AgentRole.TASK_PLANNER, status="complete"),
            AgentSpawnRecord(role=AgentRole.RISK_ANALYST, status="complete"),
        ]
        pipeline = _make_pipeline(
            phase=PipelinePhase.IMPLEMENT,
            coordinator_state=CoordinatorState(agents_spawned=completed_deps),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawned = MagicMock()
        spawned.container_info = ContainerInfo(
            container_id="rev123", container_name="egg-test-reviewer"
        )
        spawner.spawn_agent_container.return_value = spawned
        mock_spawner_fn.return_value = spawner

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "reviewer_code"},
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["role"] == "reviewer_code"

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_refiner_in_implement_phase_rejected(
        self, mock_repo, mock_lock, mock_store_fn, client
    ):
        """Spawning refiner in implement phase should be rejected."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.IMPLEMENT)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "refiner"},
        )
        assert response.status_code == 400
        assert "not valid for phase" in response.get_json()["message"]

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_wrong_reviewer_for_phase_rejected(
        self, mock_repo, mock_lock, mock_store_fn, client
    ):
        """Spawning reviewer_plan in implement phase should be rejected."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        # reviewer_plan depends on task_planner and risk_analyst — satisfy
        # those so the dependency check passes and phase-role check fires
        pipeline = _make_pipeline(
            phase=PipelinePhase.IMPLEMENT,
            coordinator_state=CoordinatorState(
                agents_spawned=[
                    AgentSpawnRecord(role=AgentRole.TASK_PLANNER, status="complete"),
                    AgentSpawnRecord(role=AgentRole.RISK_ANALYST, status="complete"),
                ],
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "reviewer_plan"},
        )
        assert response.status_code == 400
        assert "not valid for phase" in response.get_json()["message"]

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_in_phase_without_role_mapping_allowed(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        """Spawning in a phase without defined role mappings (e.g., PR) should be allowed."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.PR)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawned = MagicMock()
        spawned.container_info = ContainerInfo(
            container_id="pr123", container_name="egg-test-coder"
        )
        spawner.spawn_agent_container.return_value = spawned
        mock_spawner_fn.return_value = spawner

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder"},
        )
        assert response.status_code == 200

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_in_coordinator_phase_without_role_mapping_allowed(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        """Spawning in coordinator phase (no role mappings) should be allowed."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.COORDINATOR)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawned = MagicMock()
        spawned.container_info = ContainerInfo(
            container_id="coord123", container_name="egg-test-coder"
        )
        spawner.spawn_agent_container.return_value = spawned
        mock_spawner_fn.return_value = spawner

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder"},
        )
        assert response.status_code == 200


# ── Dependency validation tests ────────────────────────────────────


class TestSpawnDependencyValidation:
    """Spawn must be blocked when role dependencies are not complete."""

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_blocked_when_dependency_not_complete(
        self, mock_repo, mock_lock, mock_store_fn, client
    ):
        """Spawning reviewer_code without its dependencies complete returns 409."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        # Only integrator completed — task_planner and risk_analyst missing
        pipeline = _make_pipeline(
            phase=PipelinePhase.IMPLEMENT,
            coordinator_state=CoordinatorState(
                agents_spawned=[
                    AgentSpawnRecord(role=AgentRole.INTEGRATOR, status="complete"),
                    AgentSpawnRecord(role=AgentRole.TASK_PLANNER, status="running"),
                ],
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "reviewer_code"},
        )
        assert response.status_code == 409
        body = response.get_json()
        assert "dependencies not yet complete" in body["message"]
        assert "missing_dependencies" in body["details"]

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_allowed_when_dependencies_complete(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        """Spawning tester succeeds when coder has completed."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        # tester depends on coder — coder is complete
        pipeline = _make_pipeline(
            phase=PipelinePhase.IMPLEMENT,
            coordinator_state=CoordinatorState(
                agents_spawned=[
                    AgentSpawnRecord(role=AgentRole.CODER, status="complete"),
                ],
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawned = MagicMock()
        spawned.container_info = ContainerInfo(
            container_id="tst123", container_name="egg-test-tester"
        )
        spawner.spawn_agent_container.return_value = spawned
        mock_spawner_fn.return_value = spawner

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "tester"},
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["role"] == "tester"


# ── Spawn contract enforcement tests ──────────────────────────────


class TestSpawnContractEnforcement:
    """Spawn must be blocked in implement/PR phases without a contract."""

    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_blocked_without_contract_in_implement_phase(
        self, mock_repo, mock_lock, mock_store_fn, client
    ):
        """Spawning in implement phase without contract_synced returns 409."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.IMPLEMENT)
        pipeline.contract_synced = False
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "coder"},
        )
        assert response.status_code == 409
        assert "contract" in response.get_json()["message"].lower()

    @patch("routes.coordinator.emit_event")
    @patch("routes.coordinator.get_container_spawner")
    @patch("routes.coordinator.get_state_store")
    @patch("routes.coordinator.get_pipeline_state_lock")
    @patch("routes.coordinator.get_repo_path")
    def test_spawn_allowed_without_contract_in_refine_phase(
        self, mock_repo, mock_lock, mock_store_fn, mock_spawner_fn, mock_emit, client
    ):
        """Spawning in refine phase is allowed even without a contract."""
        mock_repo.return_value = Path("/tmp/repo")
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        pipeline.contract_synced = False
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawned = MagicMock()
        spawned.container_info = ContainerInfo(
            container_id="ref123", container_name="egg-test-refiner"
        )
        spawner.spawn_agent_container.return_value = spawned
        mock_spawner_fn.return_value = spawner

        response = client.post(
            "/api/v1/pipelines/test-pipeline/coordinator/spawn",
            json={"role": "refiner"},
        )
        assert response.status_code == 200
