"""
Tests for pipeline API endpoints.

Covers branch cleanup during pipeline deletion.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from gateway_client import PushResult
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


def _make_pipeline_with_containers(pipeline_id="test-pipeline"):
    """Create a Pipeline with container history across phases."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=42,
        repo="owner/repo",
        branch="egg/test",
    )
    pipeline.phases = {
        "plan": PhaseExecution(
            phase=PipelinePhase.PLAN,
            containers=[
                ContainerInfo(
                    container_id="container-aaa",
                    container_name="egg-test-aaa-coder",
                ),
            ],
        ),
        "implement": PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            containers=[
                ContainerInfo(
                    container_id="container-bbb",
                    container_name="egg-test-bbb-coder",
                ),
                ContainerInfo(
                    container_id="container-ccc",
                    container_name="egg-test-ccc-tester",
                ),
            ],
        ),
    }
    return pipeline


class TestDeletePipelineBranchCleanup:
    """Tests for remote branch cleanup during pipeline deletion."""

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_pipeline_cleans_up_remote_branches(
        self, mock_resolve, mock_spawner_fn, mock_gw_fn, client
    ):
        """Verify delete_remote_branch is called for the pipeline branch
        and each container's branch."""
        pipeline = _make_pipeline_with_containers("test-pipeline")

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw = MagicMock()
        mock_gw.delete_remote_branch.return_value = PushResult(ok=True)
        mock_gw_fn.return_value = mock_gw

        response = client.delete("/api/v1/pipelines/test-pipeline")
        assert response.status_code == 200

        # Should have been called for each unique container plus the pipeline branch
        assert mock_gw.delete_remote_branch.call_count == 4

        called_branches = {
            call.kwargs.get("branch") or call.args[2]
            for call in mock_gw.delete_remote_branch.call_args_list
        }
        assert called_branches == {
            "egg/test",
            "egg/container-aaa/work",
            "egg/container-bbb/work",
            "egg/container-ccc/work",
        }

        # Pipeline should still be deleted even after branch cleanup
        mock_store.delete_pipeline.assert_called_once_with("test-pipeline")

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_pipeline_cleans_up_shared_pipeline_branch(
        self, mock_resolve, mock_spawner_fn, mock_gw_fn, client
    ):
        """Pins invariant from #2014: cleanup must delete the pipeline's
        shared branch (``pipeline.branch``), not just per-container worktree
        branches. Without this, resubmitting a cancelled task leaves stale
        remote commits that poison the next run's history."""
        pipeline = Pipeline(
            id="issue-1973",
            issue_number=1973,
            repo="owner/repo",
            branch="egg/issue-1973",
        )

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw = MagicMock()
        mock_gw.delete_remote_branch.return_value = PushResult(ok=True)
        mock_gw_fn.return_value = mock_gw

        response = client.delete("/api/v1/pipelines/issue-1973")
        assert response.status_code == 200

        mock_gw.delete_remote_branch.assert_called_once()
        call_args = mock_gw.delete_remote_branch.call_args
        assert (call_args.kwargs.get("branch") or call_args.args[2]) == "egg/issue-1973"

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_pipeline_succeeds_when_branch_cleanup_fails(
        self, mock_resolve, mock_spawner_fn, mock_gw_fn, client
    ):
        """Pipeline deletion succeeds even if branch cleanup fails."""
        pipeline = _make_pipeline_with_containers("test-pipeline")

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw = MagicMock()
        mock_gw.delete_remote_branch.return_value = PushResult(
            ok=False, category="network", detail="connection refused"
        )
        mock_gw_fn.return_value = mock_gw

        response = client.delete("/api/v1/pipelines/test-pipeline")
        assert response.status_code == 200

        # Pipeline should still be deleted
        mock_store.delete_pipeline.assert_called_once_with("test-pipeline")

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_pipeline_no_branches_skips_cleanup(
        self, mock_resolve, mock_spawner_fn, mock_gw_fn, client
    ):
        """No branch cleanup when pipeline has neither a shared branch nor containers."""
        pipeline = Pipeline(
            id="test-pipeline",
            issue_number=42,
            repo="owner/repo",
            branch=None,
        )

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw = MagicMock()
        mock_gw_fn.return_value = mock_gw

        response = client.delete("/api/v1/pipelines/test-pipeline")
        assert response.status_code == 200

        # Should not have called delete_remote_branch
        mock_gw.delete_remote_branch.assert_not_called()

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_pipeline_deduplicates_container_branches(
        self, mock_resolve, mock_spawner_fn, mock_gw_fn, client
    ):
        """Same container in multiple phases should only trigger one deletion."""
        pipeline = Pipeline(
            id="test-pipeline",
            issue_number=42,
            repo="owner/repo",
            branch="egg/test",
        )
        shared_container = ContainerInfo(
            container_id="container-shared",
            container_name="egg-test-shared-coder",
        )
        pipeline.phases = {
            "plan": PhaseExecution(
                phase=PipelinePhase.PLAN,
                containers=[shared_container],
            ),
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                containers=[shared_container],
            ),
        }

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw = MagicMock()
        mock_gw.delete_remote_branch.return_value = PushResult(ok=True)
        mock_gw_fn.return_value = mock_gw

        response = client.delete("/api/v1/pipelines/test-pipeline")
        assert response.status_code == 200

        # Pipeline branch + one container branch (deduplicated across phases)
        called_branches = {
            call.kwargs.get("branch") or call.args[2]
            for call in mock_gw.delete_remote_branch.call_args_list
        }
        assert called_branches == {"egg/test", "egg/container-shared/work"}

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_pipeline_treats_already_deleted_as_success(
        self, mock_resolve, mock_spawner_fn, mock_gw_fn, client
    ):
        """``already_deleted`` is the desired state — cleanup must not warn
        or treat it as a failure when a branch was never pushed (#2055)."""
        pipeline = _make_pipeline_with_containers("test-pipeline")

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw = MagicMock()
        mock_gw.delete_remote_branch.return_value = PushResult(
            ok=False,
            category="already_deleted",
            detail="error: unable to delete: remote ref does not exist",
        )
        mock_gw_fn.return_value = mock_gw

        with patch("routes.pipelines.logger") as mock_logger:
            response = client.delete("/api/v1/pipelines/test-pipeline")
            assert response.status_code == 200

            # No "deletion failed" warnings — the desired state is satisfied.
            for call in mock_logger.warning.call_args_list:
                assert "Remote branch deletion failed" not in (call.args[0] if call.args else "")

            # And the success-summary log records all branches as deleted.
            info_messages = [call.args[0] for call in mock_logger.info.call_args_list if call.args]
            assert "Cleaned up remote branches" in info_messages

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_pipeline_succeeds_when_gateway_client_raises(
        self, mock_resolve, mock_spawner_fn, mock_gw_fn, client
    ):
        """Pipeline deletion succeeds even if get_gateway_client() raises."""
        pipeline = _make_pipeline_with_containers("test-pipeline")

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw_fn.side_effect = ConnectionError("gateway unreachable")

        response = client.delete("/api/v1/pipelines/test-pipeline")
        assert response.status_code == 200

        # Pipeline should still be deleted despite branch cleanup exception
        mock_store.delete_pipeline.assert_called_once_with("test-pipeline")


class TestCreatePipelineMultiRepo:
    """Tests that create_pipeline resolves repo paths in multi-repo setups (#1323)."""

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_create_pipeline_uses_get_repo_path(
        self, mock_repo_path, mock_get_store, mock_gw, client
    ):
        """create_pipeline should call get_repo_path() for all pipeline types."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_gw.return_value.ls_remote_branch.return_value = False
        mock_store = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.id = "issue-42"
        mock_pipeline.model_dump.return_value = {"id": "issue-42"}
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 42,
                "repo": "Khan/webapp",
                "branch": "egg/issue-42",
            },
        )
        assert response.status_code == 200
        mock_repo_path.assert_called_once()
        mock_get_store.assert_called_once_with(Path("/home/egg/repos/webapp"))

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_create_prompt_pipeline_uses_get_repo_path(
        self, mock_repo_path, mock_get_store, mock_gw, client
    ):
        """Prompt-driven pipelines (no issue_number) also use get_repo_path()."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_store = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.id = "prompt-abc"
        mock_pipeline.model_dump.return_value = {"id": "prompt-abc"}
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "repo": "Khan/webapp",
                "prompt": "Fix the login bug",
            },
        )
        assert response.status_code == 200
        mock_repo_path.assert_called_once()
        mock_get_store.assert_called_once_with(Path("/home/egg/repos/webapp"))

    def test_get_repo_path_returns_400_when_repo_not_found(self, client, tmp_path):
        """get_repo_path() returns 400 when repo subdir is missing in multi-repo setup."""
        with patch.dict(
            os.environ,
            {"EGG_REPO_PATH": str(tmp_path), "EGG_GATEWAY_READY_TIMEOUT_SECONDS": "0"},
        ):
            response = client.post(
                "/api/v1/pipelines",
                json={
                    "issue_number": 42,
                    "repo": "Khan/webapp",
                    "branch": "egg/issue-42",
                },
            )
        assert response.status_code == 400
        body = response.get_json() or {}
        msg = body.get("message", "") or body.get("description", "")
        assert "webapp" in msg or "webapp" in response.get_data(as_text=True)


def _make_cancellable_pipeline(pipeline_id="test-pipeline"):
    """Create a pipeline with running containers and agents for cancellation tests."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=42,
        repo="owner/repo",
        branch="egg/test",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.REFINE,
    )
    pipeline.phases = {
        "refine": PhaseExecution(
            phase=PipelinePhase.REFINE,
            status=PipelineStatus.RUNNING,
            containers=[
                ContainerInfo(
                    container_id="refiner-bbb",
                    container_name="egg-test-refiner",
                    agent_role=AgentRole.REFINER,
                    status=ContainerStatus.RUNNING,
                ),
            ],
            agents=[
                AgentExecution(
                    role=AgentRole.REFINER,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="refiner-bbb",
                ),
            ],
        ),
    }
    return pipeline


class TestTerminatedPipelineSyncsState:
    """Tests that terminating a pipeline marks running containers/agents as stopped."""

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_cancel_marks_running_containers_as_removed(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        # Simulate update_pipeline setting status to cancelled
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

        # All running containers should be marked REMOVED
        for container in pipeline.phases["refine"].containers:
            assert container.status == ContainerStatus.REMOVED
            assert container.exited_at is not None

        # All running agents should be marked FAILED with correct error
        for agent in pipeline.phases["refine"].agents:
            assert agent.status == AgentExecutionStatus.FAILED
            assert agent.completed_at is not None
            assert agent.error == "Pipeline cancelled"  # status-specific message

        # Pipeline state should have been saved
        mock_store.save_pipeline.assert_called_once_with(pipeline)

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_cancel_skips_already_completed_agents(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()
        # Mark one agent as already complete
        pipeline.phases["refine"].agents[0].status = AgentExecutionStatus.COMPLETE
        pipeline.phases["refine"].containers[0].status = ContainerStatus.EXITED

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "cancelled"},
        )
        assert response.status_code == 200

        # Already-complete agent/container should NOT be overwritten
        assert pipeline.phases["refine"].agents[0].status == AgentExecutionStatus.COMPLETE
        assert pipeline.phases["refine"].containers[0].status == ContainerStatus.EXITED

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_failed_pipeline_uses_correct_error_message(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """When a pipeline transitions to FAILED, agent errors say 'Pipeline failed'."""
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.FAILED
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 2
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "failed"},
        )
        assert response.status_code == 200

        # Agent errors should reflect the actual terminal status
        for agent in pipeline.phases["refine"].agents:
            assert agent.status == AgentExecutionStatus.FAILED
            assert agent.error == "Pipeline failed"  # not "Pipeline cancelled"


class TestCreatePipelineJiraAndQualifier:
    """Tests for JIRA-driven pipeline creation and qualifier support."""

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_create_pipeline_with_explicit_pipeline_id(
        self, mock_repo_path, mock_get_store, mock_gw_client, client
    ):
        """Pipeline ID from request body is passed through to create_pipeline."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_store = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.id = "KORE-1234"
        mock_pipeline.model_dump.return_value = {"id": "KORE-1234"}
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = False
        mock_gw_client.return_value = mock_gw

        response = client.post(
            "/api/v1/pipelines",
            json={
                "pipeline_id": "KORE-1234",
                "repo": "Khan/webapp",
                "branch": "egg/KORE-1234",
                "prompt": "Fix the login bug",
            },
        )
        assert response.status_code == 200
        call_kwargs = mock_store.create_pipeline.call_args[1]
        assert call_kwargs["pipeline_id"] == "KORE-1234"
        # #2399 — the pipeline tip is normalised to ``<branch>/work`` so slice
        # integration branches at ``<branch>/slice-N`` can coexist as siblings.
        assert call_kwargs["branch"] == "egg/KORE-1234/work"

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_create_pipeline_with_qualifier_suffix(
        self, mock_repo_path, mock_get_store, mock_gw_client, client
    ):
        """Pipeline ID with qualifier suffix works correctly."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_store = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.id = "KORE-1234-backend"
        mock_pipeline.model_dump.return_value = {"id": "KORE-1234-backend"}
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = False
        mock_gw_client.return_value = mock_gw

        response = client.post(
            "/api/v1/pipelines",
            json={
                "pipeline_id": "KORE-1234-backend",
                "repo": "Khan/webapp",
                "branch": "egg/KORE-1234-backend",
                "prompt": "Fix the backend login bug",
            },
        )
        assert response.status_code == 200
        call_kwargs = mock_store.create_pipeline.call_args[1]
        assert call_kwargs["pipeline_id"] == "KORE-1234-backend"
        # #2399 — pipeline tip normalised to ``<branch>/work``.
        assert call_kwargs["branch"] == "egg/KORE-1234-backend/work"

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_create_pipeline_rejects_existing_branch(
        self, mock_repo_path, mock_get_store, mock_gw_client, client
    ):
        """409 when the target branch already exists and pipeline is active."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = True
        mock_gw_client.return_value = mock_gw

        # Simulate an active pipeline — the branch-exists check now
        # only returns 409 when an active pipeline exists for that ID.
        mock_store = MagicMock()
        mock_store.pipeline_exists.return_value = True
        existing = Pipeline(
            id="KORE-1234",
            repo="Khan/webapp",
            status=PipelineStatus.RUNNING,
        )
        mock_store.load_pipeline.return_value = existing
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "pipeline_id": "KORE-1234",
                "repo": "Khan/webapp",
                "branch": "egg/KORE-1234",
                "prompt": "Fix the login bug",
            },
        )
        assert response.status_code == 409
        body = response.get_json()
        assert "already exists" in body["message"]
        assert "qualifier" in body["message"]
        assert body["details"]["reason"] == "branch_exists"
        # #2399 — pipeline tip normalised to ``<branch>/work`` before the
        # branch-existence check, so the error surfaces the /work shape.
        assert body["details"]["branch"] == "egg/KORE-1234/work"

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_create_pipeline_rejects_stale_branch_with_no_active_pipeline(
        self, mock_repo_path, mock_get_store, mock_gw_client, client
    ):
        """409 when the target branch exists, no active pipeline holds it,
        but the branch tip differs from origin/<base_branch> — i.e. it
        carries commits from a prior failed/cancelled pipeline run.
        Starting on top of those would inherit the stale history and
        reproduce the contamination shape of #2222.
        """
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = True
        # Branch SHA differs from base SHA — prior-pipeline commits sit on top.
        mock_gw.get_remote_branch_sha.side_effect = lambda **kwargs: (
            "deadbeefcafef00d" * 2 + "00000000"
            if "egg/" in kwargs["ref"]
            else "feedfacefeedface" * 2 + "00000000"
        )
        mock_gw_client.return_value = mock_gw

        # No active pipeline (terminal state).
        mock_store = MagicMock()
        mock_store.pipeline_exists.return_value = True
        existing = Pipeline(
            id="issue-2137",
            repo="Khan/webapp",
            status=PipelineStatus.CANCELLED,
        )
        mock_store.load_pipeline.return_value = existing
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "pipeline_id": "issue-2137",
                "repo": "Khan/webapp",
                "branch": "egg/issue-2137",
                "base_branch": "main",
                "issue_number": 2137,
            },
        )
        assert response.status_code == 409
        body = response.get_json()
        assert body["details"]["reason"] == "stale_branch"
        # #2399 — pipeline tip normalised to ``<branch>/work``.
        assert body["details"]["branch"] == "egg/issue-2137/work"
        assert "cancel_task" in body["details"]["hint"]
        assert "cleanup=true" in body["details"]["hint"]

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_create_pipeline_allows_reuse_when_branch_at_base(
        self, mock_repo_path, mock_get_store, mock_gw_client, client
    ):
        """When the target branch exists with no active pipeline AND its
        tip matches origin/<base_branch>, allow reuse — that's a fresh
        branch off base with no inherited state.
        """
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = True
        # Both refs resolve to the same SHA — fresh-from-base.
        same_sha = "abc123def456" * 3 + "abcd"
        mock_gw.get_remote_branch_sha.return_value = same_sha
        mock_gw_client.return_value = mock_gw

        mock_store = MagicMock()
        mock_store.pipeline_exists.return_value = True
        existing = Pipeline(
            id="issue-2137",
            repo="Khan/webapp",
            status=PipelineStatus.COMPLETE,
        )
        mock_store.load_pipeline.return_value = existing
        mock_pipeline = MagicMock()
        mock_pipeline.id = "issue-2137"
        mock_pipeline.model_dump.return_value = {"id": "issue-2137"}
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "pipeline_id": "issue-2137",
                "repo": "Khan/webapp",
                "branch": "egg/issue-2137",
                "base_branch": "main",
                "issue_number": 2137,
            },
        )
        assert response.status_code == 200
        # A 200 alone is consistent with several short-circuit paths
        # through ``create_pipeline``; assert the request actually
        # reached creation so a future regression that returns 200
        # without persisting the pipeline can't quietly pass this test.
        mock_store.create_pipeline.assert_called_once()

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_branch_check_failure_does_not_block_creation(
        self, mock_repo_path, mock_get_store, mock_gw_client, client
    ):
        """If the gateway is unreachable for branch check, creation proceeds."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_store = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.id = "KORE-1234"
        mock_pipeline.model_dump.return_value = {"id": "KORE-1234"}
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.side_effect = Exception("Gateway unreachable")
        mock_gw_client.return_value = mock_gw

        response = client.post(
            "/api/v1/pipelines",
            json={
                "pipeline_id": "KORE-1234",
                "repo": "Khan/webapp",
                "branch": "egg/KORE-1234",
                "prompt": "Fix the login bug",
            },
        )
        assert response.status_code == 200

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_pipeline_id_with_issue_number_and_qualifier(
        self, mock_repo_path, mock_get_store, mock_gw_client, client
    ):
        """Issue-driven pipeline with qualifier uses the right pipeline_id."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_store = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.id = "issue-42-frontend"
        mock_pipeline.model_dump.return_value = {"id": "issue-42-frontend"}
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = False
        mock_gw_client.return_value = mock_gw

        response = client.post(
            "/api/v1/pipelines",
            json={
                "pipeline_id": "issue-42-frontend",
                "issue_number": 42,
                "repo": "Khan/webapp",
                "branch": "egg/issue-42-frontend",
            },
        )
        assert response.status_code == 200
        call_kwargs = mock_store.create_pipeline.call_args[1]
        assert call_kwargs["pipeline_id"] == "issue-42-frontend"
        # #2399 — pipeline tip normalised to ``<branch>/work``.
        assert call_kwargs["branch"] == "egg/issue-42-frontend/work"
        assert call_kwargs["issue_number"] == 42

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_repo_path")
    def test_pipeline_id_without_branch_returns_400(self, mock_repo_path, mock_gw_client, client):
        """Explicit pipeline_id without branch should fail validation."""
        mock_repo_path.return_value = Path("/tmp/repo")
        response = client.post(
            "/api/v1/pipelines",
            json={
                "pipeline_id": "KORE-1234",
                "repo": "Khan/webapp",
                "prompt": "Fix the bug",
            },
        )
        assert response.status_code == 400
        body = response.get_json()
        assert "branch" in body["message"].lower()


class TestCreatePipelineErrorHandling:
    """Tests that create_pipeline returns detailed errors for unexpected exceptions (#1396)."""

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_non_state_store_error_returns_500_with_detail(
        self, mock_repo_path, mock_get_store, mock_gw, client
    ):
        """Non-StateStoreError exceptions should return 500 with the error type and message."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_store = MagicMock()
        mock_store.create_pipeline.side_effect = ValueError("unexpected validation failure")
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "repo": "Khan/webapp",
                "prompt": "Fix the bug",
                "base_branch": "develop",
            },
        )
        assert response.status_code == 500
        body = response.get_json()
        assert "ValueError" in body["message"]
        assert "unexpected validation failure" in body["message"]

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_os_error_returns_500_with_detail(
        self, mock_repo_path, mock_get_store, mock_gw, client
    ):
        """OSError during pipeline creation should return 500 with detail, not generic error."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_gw.return_value.ls_remote_branch.return_value = False
        mock_store = MagicMock()
        mock_store.create_pipeline.side_effect = OSError("Permission denied")
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "pipeline_id": "KORE-1234",
                "repo": "Khan/webapp",
                "branch": "egg/KORE-1234",
                "prompt": "Fix the bug",
            },
        )
        assert response.status_code == 500
        body = response.get_json()
        assert "OSError" in body["message"]
        assert "Permission denied" in body["message"]


class TestPipelineIdValidation:
    """Tests that PIPELINE_ID_PATTERN accepts new JIRA and qualifier formats."""

    def test_validate_jira_ticket_id(self):
        """JIRA ticket IDs like KORE-1234 are accepted."""
        from state_store import _validate_pipeline_id

        _validate_pipeline_id("KORE-1234")

    def test_validate_jira_ticket_with_qualifier(self):
        """JIRA ticket IDs with qualifier like KORE-1234-backend are accepted."""
        from state_store import _validate_pipeline_id

        _validate_pipeline_id("KORE-1234-backend")

    def test_validate_jira_ticket_with_hyphenated_qualifier(self):
        """JIRA ticket IDs with multi-segment qualifier like KORE-1234-v2-hotfix are accepted."""
        from state_store import _validate_pipeline_id

        _validate_pipeline_id("KORE-1234-v2-hotfix")

    def test_validate_issue_with_hyphenated_qualifier(self):
        """Issue IDs with multi-segment qualifier like issue-42-v2-hotfix are accepted."""
        from state_store import _validate_pipeline_id

        _validate_pipeline_id("issue-42-v2-hotfix")

    def test_validate_issue_with_qualifier(self):
        """Issue IDs with qualifier like issue-42-frontend are accepted."""
        from state_store import _validate_pipeline_id

        _validate_pipeline_id("issue-42-frontend")

    def test_validate_traditional_issue_id(self):
        """Traditional issue-{number} IDs still work."""
        from state_store import _validate_pipeline_id

        _validate_pipeline_id("issue-42")

    def test_validate_traditional_pipeline_id(self):
        """Traditional pipeline-{hex} IDs still work."""
        from state_store import _validate_pipeline_id

        _validate_pipeline_id("pipeline-abcd1234")

    def test_validate_traditional_local_id(self):
        """Traditional local-{hex} IDs still work."""
        from state_store import _validate_pipeline_id

        _validate_pipeline_id("local-abcd1234")

    def test_validate_pr_id(self):
        """PR IDs still work."""
        from state_store import _validate_pipeline_id

        _validate_pipeline_id("pr-123")

    def test_reject_trailing_hyphen_qualifier(self):
        """Qualifiers with trailing hyphens are rejected."""
        from state_store import InvalidPipelineIdError, _validate_pipeline_id

        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("KORE-1234-backend-")

    def test_reject_path_traversal(self):
        """Path traversal attempts are rejected."""
        from state_store import InvalidPipelineIdError, _validate_pipeline_id

        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("../../../etc")

    def test_reject_empty_id(self):
        """Empty pipeline IDs are rejected."""
        from state_store import InvalidPipelineIdError, _validate_pipeline_id

        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("")

    def test_reject_invalid_format(self):
        """Random strings are rejected."""
        from state_store import InvalidPipelineIdError, _validate_pipeline_id

        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("not-a-valid-id")


class TestRuntimeStateLeakageOnBranchReuse:
    """Regression tests for #2053.

    A new pipeline that reuses an id from a prior terminal run (same
    branch, e.g. ``issue-1965``) must not inherit the prior run's
    consensus tracker, legacy consensus state, or message-store
    history. Without isolation, the ``/status/wait`` route reports
    ``concurrent.consensus.is_complete: true`` for a pipeline that has
    not spawned any agents.
    """

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_cancel_clears_runtime_state(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """PATCH to cancelled status evicts consensus + message-store state."""
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline("issue-1965")

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        with patch("routes.pipelines._clear_pipeline_runtime_state") as mock_clear:
            response = client.patch(
                "/api/v1/pipelines/issue-1965",
                json={"status": "cancelled"},
            )
            assert response.status_code == 200
            mock_clear.assert_called_once()
            assert mock_clear.call_args.args[0] == "issue-1965"
            assert "cancelled" in mock_clear.call_args.kwargs["reason"]

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_clears_runtime_state(self, mock_resolve, mock_spawner_fn, mock_gw_fn, client):
        """DELETE evicts consensus + message-store state alongside the JSON file."""
        pipeline = _make_pipeline_with_containers("issue-1965")

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw = MagicMock()
        mock_gw.delete_remote_branch.return_value = PushResult(ok=True)
        mock_gw_fn.return_value = mock_gw

        with patch("routes.pipelines._clear_pipeline_runtime_state") as mock_clear:
            response = client.delete("/api/v1/pipelines/issue-1965")
            assert response.status_code == 200
            mock_clear.assert_called_once()
            assert mock_clear.call_args.args[0] == "issue-1965"

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_create_clears_runtime_state(
        self, mock_repo_path, mock_get_store, mock_gw_client, client
    ):
        """POST evicts any lingering state for the created pipeline id.

        Defends against paths that bypass PATCH/DELETE — auto-FAILED
        pipelines, and Redis-backed message-store entries that survived
        an orchestrator restart between cancel and resubmit.
        """
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_store = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.id = "issue-1965"
        mock_pipeline.model_dump.return_value = {"id": "issue-1965"}
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = False
        mock_gw_client.return_value = mock_gw

        with patch("routes.pipelines._clear_pipeline_runtime_state") as mock_clear:
            response = client.post(
                "/api/v1/pipelines",
                json={
                    "issue_number": 1965,
                    "repo": "Khan/webapp",
                    "branch": "egg/issue-1965",
                },
            )
            assert response.status_code == 200
            mock_clear.assert_called_once()
            assert mock_clear.call_args.args[0] == "issue-1965"
            assert mock_clear.call_args.kwargs["reason"] == "pipeline_create"

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_post_clears_real_runtime_state(
        self, mock_repo_path, mock_get_store, mock_gw_client, client
    ):
        """POST evicts real Redis/in-memory state at the route level.

        Integration variant of ``test_create_clears_runtime_state`` that
        exercises the real ``_clear_pipeline_runtime_state`` helper (no
        mock) through the route handler. Seeds the three backends
        (``PeerConsensusTracker``, the legacy evaluator, the message
        store), POSTs a fresh pipeline with the same id, and asserts
        every backend is empty afterwards.

        This is the route-level safety net for the POST-site clear's
        primary motivation: auto-FAILED paths (restart_agent spawn
        failure, _handle_pr_creation_failure) write status=FAILED
        directly via ``store.update_pipeline`` / ``store.save_pipeline``,
        bypassing PATCH and therefore bypassing the PATCH-site clear.
        The seeding here represents the residual state such a path would
        leave behind, not a literal auto-FAILED prior pipeline.
        """
        from consensus import ReadinessState, get_consensus_evaluator
        from message_store import Message, get_message_store
        from peer_consensus import (
            create_peer_consensus_tracker,
            get_peer_consensus_tracker,
            remove_peer_consensus_tracker,
        )
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        pipeline_id = "issue-1965"

        # Defensive: clear any leftover state from a prior test run
        remove_peer_consensus_tracker(pipeline_id)
        get_consensus_evaluator().clear(pipeline_id)
        get_message_store().clear(pipeline_id)

        # Seed state as if a prior run had reached CONFIRMED and then
        # been auto-FAILED via store.update_pipeline (bypassing PATCH).
        graph = ReviewGraph(
            edges=[
                ReviewEdge(
                    reviewer_role="reviewer_refine",
                    producer_role="refiner",
                    criticality=ReviewCriticality.CRITICAL,
                )
            ]
        )
        create_peer_consensus_tracker(pipeline_id, graph)
        evaluator = get_consensus_evaluator()
        evaluator.register_agent(pipeline_id, "refiner")
        evaluator.update_readiness(pipeline_id, "refiner", ReadinessState.READY)
        msg_store = get_message_store()
        msg_store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="refiner",
                to_role="all",
                message_type="PROGRESS",
                body="prior-run-leak",
            )
        )

        # Sanity: prior-run state is present
        assert get_peer_consensus_tracker(pipeline_id) is not None
        assert evaluator.get_state(pipeline_id)["agents"]
        assert msg_store.get_status(pipeline_id)["total"] == 1

        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_store = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.id = pipeline_id
        mock_pipeline.model_dump.return_value = {"id": pipeline_id}
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = False
        mock_gw_client.return_value = mock_gw

        # No patch of _clear_pipeline_runtime_state — exercise the real helper
        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 1965,
                "repo": "Khan/webapp",
                "branch": "egg/issue-1965",
            },
        )
        assert response.status_code == 200

        # All three backends must be evicted by the POST-site clear
        assert get_peer_consensus_tracker(pipeline_id) is None
        assert evaluator.get_state(pipeline_id)["agents"] == {}
        assert msg_store.get_status(pipeline_id)["total"] == 0

    def test_clear_runtime_state_evicts_real_consensus_and_messages(self):
        """End-to-end: helper actually clears tracker, evaluator, and messages.

        Seeds a real ``PeerConsensusTracker``, the legacy consensus
        evaluator, and the message store under the same pipeline id,
        then invokes ``_clear_pipeline_runtime_state`` and asserts every
        backend lookup returns empty/None — matching what a fresh
        pipeline with the same id would observe.
        """
        from consensus import ReadinessState, get_consensus_evaluator
        from message_store import Message, get_message_store
        from peer_consensus import (
            create_peer_consensus_tracker,
            get_peer_consensus_tracker,
            remove_peer_consensus_tracker,
        )
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph
        from routes.pipelines import _clear_pipeline_runtime_state

        pipeline_id = "issue-1965-test"
        # Defensive: clear any leftover state from a prior test run
        remove_peer_consensus_tracker(pipeline_id)
        get_consensus_evaluator().clear(pipeline_id)
        get_message_store().clear(pipeline_id)

        # Seed peer-consensus tracker (BRC). Presence of the tracker in
        # the global ``_trackers`` map is the symptom; what's inside it
        # doesn't matter for the leak.
        graph = ReviewGraph(
            edges=[
                ReviewEdge(
                    reviewer_role="reviewer_refine",
                    producer_role="refiner",
                    criticality=ReviewCriticality.CRITICAL,
                )
            ]
        )
        tracker = create_peer_consensus_tracker(pipeline_id, graph)
        assert get_peer_consensus_tracker(pipeline_id) is tracker

        # Seed legacy consensus evaluator
        evaluator = get_consensus_evaluator()
        evaluator.register_agent(pipeline_id, "refiner")
        evaluator.update_readiness(pipeline_id, "refiner", ReadinessState.READY)
        assert evaluator.get_state(pipeline_id)["agents"]

        # Seed message store
        store = get_message_store()
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="refiner",
                to_role="all",
                message_type="PROGRESS",
                body="seed",
            )
        )
        assert store.get_status(pipeline_id)["total"] == 1

        _clear_pipeline_runtime_state(pipeline_id, reason="test")

        assert get_peer_consensus_tracker(pipeline_id) is None
        assert evaluator.get_state(pipeline_id)["agents"] == {}
        assert store.get_status(pipeline_id)["total"] == 0

    def test_clear_runtime_state_evicts_context_pr_dedupe(self):
        """#2599 review 2 item 1 — dedupe set keyed on pipeline_id alone
        is per-lifecycle, not per-id.  Without eviction at the same
        terminal-state hook the other backends use, a pipeline id reused
        across runs inherits the prior run's emitted-event set and the
        new run's failure goes unreported on the message bus.
        """
        from routes.pipelines import (
            _clear_pipeline_runtime_state,
            _context_pr_events_emitted,
            _context_pr_events_emitted_lock,
        )

        pipeline_id = "issue-2599-test"
        # Defensive: clear any leftover state from a prior test run
        with _context_pr_events_emitted_lock:
            _context_pr_events_emitted.pop(pipeline_id, None)

        # Seed dedupe state — simulate a prior failed context-PR open
        with _context_pr_events_emitted_lock:
            _context_pr_events_emitted[pipeline_id] = {"context_pr.failed"}
        assert pipeline_id in _context_pr_events_emitted

        _clear_pipeline_runtime_state(pipeline_id, reason="test")

        # Stale set would otherwise silently suppress a fresh pipeline's
        # ``context_pr.failed`` emission.
        assert pipeline_id not in _context_pr_events_emitted


class TestNonObjectJsonBodyReturns400:
    """Fix for #2673: non-object JSON bodies must 400, not 500.

    Mirrors the #2656 fix on the decisions route. Without the guard,
    ``data.get(...)`` raises ``AttributeError`` for a list/scalar body
    and the generic handler returns 500.
    """

    @pytest.mark.parametrize(
        "raw_body",
        ["[1, 2, 3]", '"a string body"', "42", "true", "[]", "0", "false", '""'],
        ids=[
            "array",
            "string",
            "number",
            "bool",
            "empty-array",
            "zero",
            "false",
            "empty-string",
        ],
    )
    def test_create_pipeline_non_object_body_returns_400(self, client, raw_body):
        response = client.post(
            "/api/v1/pipelines",
            data=raw_body,
            content_type="application/json",
        )
        assert response.status_code == 400, response.data
        body = response.get_json()
        assert body["success"] is False
        assert "json object" in body["message"].lower(), body

    @pytest.mark.parametrize(
        "raw_body",
        ["[1, 2, 3]", '"a string body"', "42", "true", "[]", "0", "false", '""'],
        ids=[
            "array",
            "string",
            "number",
            "bool",
            "empty-array",
            "zero",
            "false",
            "empty-string",
        ],
    )
    def test_update_pipeline_non_object_body_returns_400(self, client, raw_body):
        response = client.patch(
            "/api/v1/pipelines/issue-42",
            data=raw_body,
            content_type="application/json",
        )
        assert response.status_code == 400, response.data
        body = response.get_json()
        assert body["success"] is False
        assert "json object" in body["message"].lower(), body


class TestEpicModeNonEpicRejection:
    """Regression tests for issue #1557 review feedback (N5).

    ``epic_mode='reassess'`` and ``epic_mode='fresh'`` against a Jira
    ticket whose ``issuetype`` is not ``Epic`` are operator errors —
    the operator specifically asked for epic-mode treatment but the
    ticket doesn't qualify. Both must surface as HTTP 400 with the
    ``<mode>_not_epic`` reason rather than the silent demotion that
    ``resolve_epic_mode`` performs internally. ``epic_mode='auto'``
    intentionally still demotes silently — that's the point of auto.

    Pinning the behavior with these tests prevents a future refactor
    of the ``resolve_epic_mode`` call site from quietly flipping
    either of the two explicit overrides back to warning-only.
    """

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_reassess_against_non_epic_returns_400(
        self, mock_repo_path, mock_get_store, mock_gw_client, client
    ):
        """``epic_mode='reassess'`` + non-epic ticket → HTTP 400."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_get_store.return_value = MagicMock()
        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = False
        mock_gw_client.return_value = mock_gw
        with patch(
            "jira_epic.resolve_epic_mode",
            return_value=(False, None, ["ticket KORE-1234 is not an Epic"]),
        ):
            response = client.post(
                "/api/v1/pipelines",
                json={
                    "pipeline_id": "KORE-1234",
                    "repo": "Khan/webapp",
                    "branch": "egg/KORE-1234",
                    "prompt": "Drive the epic",
                    "jira_ticket": "KORE-1234",
                    "epic_mode": "reassess",
                },
            )
        assert response.status_code == 400
        body = response.get_json()
        assert body["details"]["reason"] == "reassess_not_epic"
        assert "is not an Epic" in body["details"]["warnings"][0]

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_fresh_against_non_epic_returns_400(
        self, mock_repo_path, mock_get_store, mock_gw_client, client
    ):
        """``epic_mode='fresh'`` + non-epic ticket → HTTP 400.

        Symmetric with ``reassess``; closes the silent-demotion gap
        flagged in the prior review (#14).
        """
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_get_store.return_value = MagicMock()
        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = False
        mock_gw_client.return_value = mock_gw
        with patch(
            "jira_epic.resolve_epic_mode",
            return_value=(False, None, ["ticket KORE-1234 is not an Epic"]),
        ):
            response = client.post(
                "/api/v1/pipelines",
                json={
                    "pipeline_id": "KORE-1234",
                    "repo": "Khan/webapp",
                    "branch": "egg/KORE-1234",
                    "prompt": "Drive the epic",
                    "jira_ticket": "KORE-1234",
                    "epic_mode": "fresh",
                },
            )
        assert response.status_code == 400
        body = response.get_json()
        assert body["details"]["reason"] == "fresh_not_epic"
        assert "is not an Epic" in body["details"]["warnings"][0]

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_auto_against_non_epic_demotes_silently(
        self, mock_repo_path, mock_get_store, mock_gw_client, client
    ):
        """``epic_mode='auto'`` + non-epic ticket → 200 (silent demote)."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_store = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.id = "KORE-1234"
        mock_pipeline.model_dump.return_value = {"id": "KORE-1234"}
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = False
        mock_gw_client.return_value = mock_gw
        with patch(
            "jira_epic.resolve_epic_mode",
            return_value=(False, None, []),
        ):
            response = client.post(
                "/api/v1/pipelines",
                json={
                    "pipeline_id": "KORE-1234",
                    "repo": "Khan/webapp",
                    "branch": "egg/KORE-1234",
                    "prompt": "Drive the epic",
                    "jira_ticket": "KORE-1234",
                    "epic_mode": "auto",
                },
            )
        assert response.status_code == 200
        # Pipeline created with is_epic=False — auto demoted silently.
        call_kwargs = mock_store.create_pipeline.call_args[1]
        assert call_kwargs["is_epic"] is False
        assert call_kwargs["pipeline_mode"] is None
