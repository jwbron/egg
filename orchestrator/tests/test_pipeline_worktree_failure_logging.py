"""Regression tests for #2186 — worktree-creation failure logging.

When the gateway returns 500 from POST /api/v1/worktree/create with a
populated `details.errors` payload, the orchestrator's retry loop in
`routes/pipelines.py` must surface those per-repo errors in its own
logs.  Prior to #2186 the loop logged only `str(gw_err)` (the bare
message) and the wrapped `RuntimeError` discarded `gw_err.details`
entirely, leaving operators with no diagnostic when pipelines failed
at worktree creation.
"""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from gateway_client import GatewayError
from models import Pipeline, PipelinePhase, PipelineStatus


def _make_running_pipeline():
    pipeline = Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch="egg/issue-42",
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.REFINE,
        network_mode="public",
    )
    pipeline.contract_synced = True
    execution = pipeline.get_phase_execution(PipelinePhase.REFINE)
    execution.status = PipelineStatus.RUNNING
    execution.started_at = datetime.now(UTC)
    return pipeline


_COMMON_PATCHES = [
    "routes.pipelines._emit_pipeline_event",
    "routes.pipelines.get_container_spawner",
    "routes.pipelines.get_state_store",
    "routes.pipelines._spawn_and_wait",
    "routes.pipelines.get_pipeline_state_lock",
    "routes.pipelines._build_phase_prompt",
    "routes.pipelines._read_phase_draft",
    "routes.pipelines.report_pipeline_status",
]


def _setup(
    mock_report,
    mock_read_draft,
    mock_build_prompt,
    mock_state_lock,
    mock_spawn_wait,
    mock_get_store,
    mock_get_spawner,
    mock_emit,
    pipeline,
    create_worktrees_side_effect,
):
    mock_store = MagicMock()
    mock_store.load_pipeline.return_value = pipeline
    mock_store.repo_path = Path("/repo")
    mock_get_store.return_value = mock_store

    mock_gateway = MagicMock()
    mock_gateway.create_worktrees.side_effect = create_worktrees_side_effect

    mock_spawner = MagicMock()
    mock_spawner.gateway = mock_gateway
    mock_get_spawner.return_value = mock_spawner

    mock_spawn_wait.return_value = (1, "error log")

    mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
    mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

    mock_build_prompt.return_value = "test prompt"
    mock_read_draft.return_value = None

    return mock_store, mock_gateway


class TestWorktreeFailureLogsGatewayDetails:
    """The orchestrator's retry loop must log gw_err.details on the
    final-attempt failure so per-repo error text from the gateway
    survives in the orchestrator log stream.  See #2186."""

    @patch(_COMMON_PATCHES[7])
    @patch(_COMMON_PATCHES[6])
    @patch(_COMMON_PATCHES[5])
    @patch(_COMMON_PATCHES[4])
    @patch(_COMMON_PATCHES[3])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[0])
    def test_final_failure_logs_error_with_details(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_spawn_wait,
        mock_state_lock,
        mock_build_prompt,
        mock_read_draft,
        mock_report,
    ):
        from routes.pipelines import _run_pipeline

        pipeline = _make_running_pipeline()
        # Every attempt fails with a transient 500 carrying per-repo errors.
        gw_err = GatewayError(
            "Failed to create any worktrees",
            status_code=500,
            details={"errors": ["egg: branch already exists"]},
        )
        _setup(
            mock_report,
            mock_read_draft,
            mock_build_prompt,
            mock_state_lock,
            mock_spawn_wait,
            mock_get_store,
            mock_get_spawner,
            mock_emit,
            pipeline,
            create_worktrees_side_effect=gw_err,
        )

        with (
            patch.dict(os.environ, {"EGG_HOST_REPO_MAP": '{"repo": "/host/repo"}'}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("routes.pipelines.logger") as mock_logger,
            # Skip the retry sleep to keep the test fast.
            patch("routes.pipelines.time.sleep"),
        ):
            _run_pipeline("issue-42", Path("/repo"))

        # The final-failure logger.error must include gw_err.details.
        permanent_calls = [
            c
            for c in mock_logger.error.call_args_list
            if c.args and c.args[0] == "Worktree creation failed permanently"
        ]
        assert permanent_calls, (
            "Expected logger.error('Worktree creation failed permanently', ...), "
            f"got: {mock_logger.error.call_args_list}"
        )
        kwargs = permanent_calls[0].kwargs
        assert kwargs["details"] == {"errors": ["egg: branch already exists"]}
        assert kwargs["status_code"] == 500
        assert kwargs["pipeline_id"] == "issue-42"
        # The logger renames the GatewayError's message field to
        # 'error_message' because 'message' collides with LogRecord's
        # reserved key.
        assert kwargs["error_message"] == "Failed to create any worktrees"

    @patch(_COMMON_PATCHES[7])
    @patch(_COMMON_PATCHES[6])
    @patch(_COMMON_PATCHES[5])
    @patch(_COMMON_PATCHES[4])
    @patch(_COMMON_PATCHES[3])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[0])
    def test_retry_warning_includes_details(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_spawn_wait,
        mock_state_lock,
        mock_build_prompt,
        mock_read_draft,
        mock_report,
    ):
        """Transient retry warnings also carry gw_err.details so
        intermediate failures are diagnosable, not just the final
        raise."""
        from routes.pipelines import _run_pipeline

        pipeline = _make_running_pipeline()
        gw_err = GatewayError(
            "Failed to create any worktrees",
            status_code=500,
            details={"errors": ["egg: transient lock contention"]},
        )
        _setup(
            mock_report,
            mock_read_draft,
            mock_build_prompt,
            mock_state_lock,
            mock_spawn_wait,
            mock_get_store,
            mock_get_spawner,
            mock_emit,
            pipeline,
            create_worktrees_side_effect=gw_err,
        )

        with (
            patch.dict(os.environ, {"EGG_HOST_REPO_MAP": '{"repo": "/host/repo"}'}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("routes.pipelines.logger") as mock_logger,
            patch("routes.pipelines.time.sleep"),
        ):
            _run_pipeline("issue-42", Path("/repo"))

        retry_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "Worktree creation failed, retrying"
        ]
        assert retry_calls, (
            f"Expected at least one retry warning, got: {mock_logger.warning.call_args_list}"
        )
        # Every retry warning must carry the per-repo details.
        for c in retry_calls:
            assert c.kwargs.get("details") == {"errors": ["egg: transient lock contention"]}

    @patch(_COMMON_PATCHES[7])
    @patch(_COMMON_PATCHES[6])
    @patch(_COMMON_PATCHES[5])
    @patch(_COMMON_PATCHES[4])
    @patch(_COMMON_PATCHES[3])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[0])
    def test_runtime_error_message_includes_details(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_spawn_wait,
        mock_state_lock,
        mock_build_prompt,
        mock_read_draft,
        mock_report,
    ):
        """The wrapped RuntimeError message must include gw_err.details
        so any traceback consumer (Sentry, structured stderr) sees the
        per-repo errors, not just the bare gateway message."""
        from routes.pipelines import _run_pipeline

        pipeline = _make_running_pipeline()
        gw_err = GatewayError(
            "Failed to create any worktrees",
            status_code=500,
            details={"errors": ["egg: ENOENT /repos/egg"]},
        )
        # Patch get_state_store to capture the failure_message that's
        # written when the worktree-creation RuntimeError propagates.
        captured_errors: list[str] = []
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")

        def _save_pipeline(p, **kwargs):
            if getattr(p, "error", None):
                captured_errors.append(p.error)

        mock_store.save_pipeline.side_effect = _save_pipeline
        mock_get_store.return_value = mock_store

        mock_gateway = MagicMock()
        mock_gateway.create_worktrees.side_effect = gw_err
        mock_spawner = MagicMock()
        mock_spawner.gateway = mock_gateway
        mock_get_spawner.return_value = mock_spawner

        mock_spawn_wait.return_value = (1, "error log")
        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_build_prompt.return_value = "test prompt"
        mock_read_draft.return_value = None

        with (
            patch.dict(os.environ, {"EGG_HOST_REPO_MAP": '{"repo": "/host/repo"}'}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("routes.pipelines.time.sleep"),
        ):
            _run_pipeline("issue-42", Path("/repo"))

        # At least one captured error string includes both the gateway
        # message and the per-repo details payload.
        assert any(
            "Failed to create any worktrees" in e and "egg: ENOENT /repos/egg" in e
            for e in captured_errors
        ), f"Expected pipeline.error to embed gw_err.details; got: {captured_errors}"
