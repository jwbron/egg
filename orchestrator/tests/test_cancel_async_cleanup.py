"""
Tests for async container cleanup during pipeline cancellation (issue #1515).

Verifies that:
- PATCH with status=cancelled returns before spawner.cleanup_pipeline() completes
- Response includes cleanup_pending: true for cancelled/failed pipelines
- Background cleanup still runs to completion after the response
- _mark_pipeline_records_terminated() runs synchronously before the response
- Error handling in the background thread doesn't affect the response
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    PhaseExecution,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import pipelines_bp


@pytest.fixture
def app():
    """Create a test Flask app with the pipelines blueprint."""
    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


def _make_cancellable_pipeline(pipeline_id="test-pipeline"):
    """Create a pipeline with running containers and agents for cancellation tests."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=42,
        repo="owner/repo",
        branch="egg/test",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )
    pipeline.phases = {
        "implement": PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            containers=[
                ContainerInfo(
                    container_id="container-aaa",
                    container_name="egg-test-aaa-coder",
                    agent_role=AgentRole.CODER,
                    status=ContainerStatus.RUNNING,
                ),
                ContainerInfo(
                    container_id="container-bbb",
                    container_name="egg-test-bbb-tester",
                    agent_role=AgentRole.TESTER,
                    status=ContainerStatus.RUNNING,
                ),
                ContainerInfo(
                    container_id="container-ccc",
                    container_name="egg-test-ccc-reviewer",
                    agent_role=AgentRole.REVIEWER_CODE,
                    status=ContainerStatus.RUNNING,
                ),
                ContainerInfo(
                    container_id="container-ddd",
                    container_name="egg-test-ddd-documenter",
                    agent_role=AgentRole.DOCUMENTER,
                    status=ContainerStatus.RUNNING,
                ),
            ],
            agents=[
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="container-aaa",
                ),
                AgentExecution(
                    role=AgentRole.TESTER,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="container-bbb",
                ),
                AgentExecution(
                    role=AgentRole.REVIEWER_CODE,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="container-ccc",
                ),
                AgentExecution(
                    role=AgentRole.DOCUMENTER,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="container-ddd",
                ),
            ],
        ),
    }
    return pipeline


class TestAsyncCleanupOnCancel:
    """Tests that PATCH cancel returns immediately with async container cleanup."""

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_cancel_returns_before_cleanup_completes(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """PATCH with status=cancelled should return before cleanup_pipeline finishes."""
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        # Track when cleanup starts and when response is received
        cleanup_started = threading.Event()
        cleanup_can_finish = threading.Event()

        def slow_cleanup(pipeline_id, force=False, preserve_worktrees=False, **kwargs):
            cleanup_started.set()
            # Block until we allow it to finish (simulates slow Docker cleanup)
            cleanup_can_finish.wait(timeout=10)
            return 4

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.side_effect = slow_cleanup
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        # The PATCH should return without waiting for cleanup_pipeline
        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "cancelled"},
        )
        assert response.status_code == 200

        # Allow cleanup to finish so the background thread can complete
        cleanup_can_finish.set()

        # Wait for cleanup to actually start (proves it ran in the background)
        assert cleanup_started.wait(timeout=5), "Cleanup was never started"

        # Verify cleanup was actually called (in the background)
        mock_spawner.cleanup_pipeline.assert_called_once_with(
            "test-pipeline",
            force=True,
            preserve_worktrees=True,
            salvage_mode="public",
            salvage_base_branch=None,
        )

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_cancel_response_includes_cleanup_pending(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """Response should include cleanup_pending: true when pipeline is cancelled."""
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 2
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "cancelled"},
        )
        assert response.status_code == 200

        body = response.get_json()
        assert body["data"]["cleanup_pending"] is True

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_failed_response_includes_cleanup_pending(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """Response should include cleanup_pending: true when pipeline is failed."""
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.FAILED
        mock_resolve.return_value = (mock_store, pipeline)

        cleanup_done = threading.Event()

        mock_spawner = MagicMock()

        def cleanup_with_signal(pipeline_id, force=False, preserve_worktrees=False, **kwargs):
            cleanup_done.set()
            return 2

        mock_spawner.cleanup_pipeline.side_effect = cleanup_with_signal
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "failed"},
        )
        assert response.status_code == 200

        body = response.get_json()
        assert body["data"]["cleanup_pending"] is True

        # Verify FAILED pipelines do NOT preserve worktrees
        assert cleanup_done.wait(timeout=5), "Background cleanup was never called"
        mock_spawner.cleanup_pipeline.assert_called_once_with(
            "test-pipeline",
            force=True,
            preserve_worktrees=False,
            salvage_mode="public",
            salvage_base_branch=None,
        )

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_non_terminal_status_has_no_cleanup_pending(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """Response should NOT include cleanup_pending when pipeline is not terminal."""
        mock_repo.return_value = "/repo"
        pipeline = Pipeline(
            id="test-pipeline",
            issue_number=42,
            repo="owner/repo",
            branch="egg/test",
            status=PipelineStatus.RUNNING,
        )

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"current_phase": "implement"},
        )
        assert response.status_code == 200

        body = response.get_json()
        # cleanup_pending should not be in the response for non-terminal status
        assert "cleanup_pending" not in body.get("data", {})

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_background_cleanup_runs_to_completion(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """Container cleanup should complete in the background after response."""
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        cleanup_completed = threading.Event()

        def cleanup_with_signal(pipeline_id, force=False, preserve_worktrees=False, **kwargs):
            time.sleep(0.1)  # Simulate some work
            cleanup_completed.set()
            return 4

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.side_effect = cleanup_with_signal
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "cancelled"},
        )
        assert response.status_code == 200

        # Wait for background cleanup to complete
        assert cleanup_completed.wait(timeout=5), "Background cleanup did not run to completion"
        mock_spawner.cleanup_pipeline.assert_called_once_with(
            "test-pipeline",
            force=True,
            preserve_worktrees=True,
            salvage_mode="public",
            salvage_base_branch=None,
        )

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_state_sync_runs_before_response(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """_mark_pipeline_records_terminated should run synchronously before response."""
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 2
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "cancelled"},
        )
        assert response.status_code == 200

        # Verify state was saved (by _mark_pipeline_records_terminated)
        mock_store.save_pipeline.assert_called_once()

        # All running containers should be marked REMOVED in the response
        for container in pipeline.phases["implement"].containers:
            assert container.status == ContainerStatus.REMOVED
            assert container.exited_at is not None

        # All running agents should be marked FAILED
        for agent in pipeline.phases["implement"].agents:
            assert agent.status == AgentExecutionStatus.FAILED
            assert agent.completed_at is not None
            assert agent.error == "Pipeline cancelled"

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_cleanup_error_does_not_affect_response(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """Errors during background cleanup should not affect the cancel response."""
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        error_raised = threading.Event()

        def cleanup_that_raises(pipeline_id, force=False, preserve_worktrees=False, **kwargs):
            error_raised.set()
            raise RuntimeError("Docker daemon unavailable")

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.side_effect = cleanup_that_raises
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "cancelled"},
        )
        # Response should succeed regardless of cleanup errors
        assert response.status_code == 200

        body = response.get_json()
        assert body["data"]["cleanup_pending"] is True

        # Wait for background thread to have run
        assert error_raised.wait(timeout=5), "Cleanup was never called"

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_decisions_cancelled_before_response(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """Pending decisions should be cancelled synchronously before the response."""
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_decision1 = MagicMock()
        mock_decision1.id = "decision-1"
        mock_decision2 = MagicMock()
        mock_decision2.id = "decision-2"

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = [mock_decision1, mock_decision2]
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "cancelled"},
        )
        assert response.status_code == 200

        # Both decisions should have been cancelled
        assert mock_dq.cancel_decision.call_count == 2
        mock_dq.cancel_decision.assert_any_call("decision-1")
        mock_dq.cancel_decision.assert_any_call("decision-2")


class TestCleanupBackgroundThreadBehavior:
    """Tests for the behavior of the background cleanup thread."""

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_cleanup_thread_is_daemon(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """The cleanup thread should be a daemon thread so it doesn't block shutdown."""
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        thread_captured = {}
        original_thread_init = threading.Thread.__init__

        def capture_thread(self_thread, *args, **kwargs):
            original_thread_init(self_thread, *args, **kwargs)
            # Capture threads that appear to be cleanup threads
            target = kwargs.get("target") or (args[0] if args else None)
            if target is not None:
                thread_captured["thread"] = self_thread

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 2
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        with patch.object(threading.Thread, "__init__", capture_thread):
            response = client.patch(
                "/api/v1/pipelines/test-pipeline",
                json={"status": "cancelled"},
            )

        assert response.status_code == 200

        assert "thread" in thread_captured, "Expected a cleanup thread to be created"
        assert thread_captured["thread"].daemon, "Cleanup thread should be a daemon thread"
        assert thread_captured["thread"].name == "cleanup-test-pipeline"

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_cleanup_uses_correct_pipeline_id(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """Background cleanup should use the correct pipeline_id."""
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline("specific-pipeline-123")

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        cleanup_done = threading.Event()

        def cleanup_with_signal(pipeline_id, force=False, preserve_worktrees=False, **kwargs):
            cleanup_done.set()
            return 2

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.side_effect = cleanup_with_signal
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/specific-pipeline-123",
            json={"status": "cancelled"},
        )
        assert response.status_code == 200

        # Wait for background thread to complete
        assert cleanup_done.wait(timeout=5), "Cleanup was never called"

        # Verify the correct pipeline_id was passed
        mock_spawner.cleanup_pipeline.assert_called_once_with(
            "specific-pipeline-123",
            force=True,
            preserve_worktrees=True,
            salvage_mode="public",
            salvage_base_branch=None,
        )

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_docker_exception_in_background_handled_gracefully(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """DockerException during background cleanup should be handled."""
        from docker.errors import DockerException

        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        cleanup_called = threading.Event()

        def cleanup_raises_docker_error(pipeline_id, force=False, preserve_worktrees=False, **kwargs):
            cleanup_called.set()
            raise DockerException("Container not found")

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.side_effect = cleanup_raises_docker_error
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "cancelled"},
        )
        assert response.status_code == 200

        # Verify cleanup ran and raised (didn't crash the thread unhandled)
        assert cleanup_called.wait(timeout=5)
