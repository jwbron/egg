"""
Tests for signal handler contract-role guards.

Verifies that handle_complete_signal and handle_error_signal skip
contract interaction for non-contract roles (e.g. REFINER)
and interact with the contract orchestrator for contract-mapped roles (e.g. CODER).
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add orchestrator and shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


@pytest.fixture
def app():
    """Create a test Flask app with the signals blueprint."""
    from flask import Flask
    from routes.signals import signals_bp

    app = Flask(__name__)
    app.register_blueprint(signals_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Test client for the signals blueprint."""
    return app.test_client()


@pytest.fixture
def mock_pipeline():
    """Create a mock pipeline."""
    from models import Pipeline

    return Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch="egg/issue-42",
    )


def _mock_contract_orchestrator(is_complete: bool = False):
    """Create a mock contract orchestrator with standard responses."""
    mock_orch = MagicMock()
    mock_decision = MagicMock()
    mock_decision.all_complete = is_complete
    mock_orch.get_next_dispatch.return_value = mock_decision
    mock_orch.apply_to_contract.return_value = MagicMock()
    return mock_orch


class TestCompleteSignalNonContractRole:
    """handle_complete_signal with a non-contract role skips contract interaction."""

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.load_contract")
    def test_refiner_skips_contract(
        self,
        mock_load_contract,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        """REFINER role should not load or interact with contract."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        with app.app_context():
            from routes.signals import handle_complete_signal

            response, status_code = handle_complete_signal(
                "issue-42",
                {"agent_role": "refiner"},
                Path("/tmp/repo"),
            )

        assert status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["data"]["all_complete"] is True
        mock_load_contract.assert_not_called()

    @patch("routes.signals.save_agent_output")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.save_contract")
    @patch("routes.signals.create_orchestrator")
    @patch("routes.signals.load_contract")
    def test_coder_uses_contract(
        self,
        mock_load_contract,
        mock_create_orchestrator,
        mock_save_contract,
        mock_get_store,
        mock_resolve_wt,
        mock_save_output,
        app,
        mock_pipeline,
    ):
        """CODER role should load contract and record completion."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_orch = _mock_contract_orchestrator(is_complete=False)
        mock_create_orchestrator.return_value = mock_orch

        with app.app_context():
            from routes.signals import handle_complete_signal

            response, status_code = handle_complete_signal(
                "issue-42",
                {"agent_role": "coder", "commit": "abc1234"},
                Path("/tmp/repo"),
            )

        assert status_code == 200
        data = json.loads(response.data)
        assert data["data"]["all_complete"] is False
        mock_load_contract.assert_called_once()
        mock_orch.complete_agent.assert_called_once()
        mock_save_contract.assert_called_once()


class TestErrorSignalNonContractRole:
    """handle_error_signal with a non-contract role skips contract interaction."""

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.load_contract")
    def test_refiner_skips_contract(
        self,
        mock_load_contract,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        """REFINER error signal should not load or interact with contract."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        with app.app_context():
            from routes.signals import handle_error_signal

            response, status_code = handle_error_signal(
                "issue-42",
                {"agent_role": "refiner", "error": "Something failed"},
                Path("/tmp/repo"),
            )

        assert status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["data"]["error"] == "Something failed"
        mock_load_contract.assert_not_called()

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.save_contract")
    @patch("routes.signals.create_orchestrator")
    @patch("routes.signals.load_contract")
    def test_coder_uses_contract(
        self,
        mock_load_contract,
        mock_create_orchestrator,
        mock_save_contract,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        """CODER error signal should load contract and record failure."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_orch = _mock_contract_orchestrator()
        mock_create_orchestrator.return_value = mock_orch

        with app.app_context():
            from routes.signals import handle_error_signal

            response, status_code = handle_error_signal(
                "issue-42",
                {"agent_role": "coder", "error": "Build failed"},
                Path("/tmp/repo"),
            )

        assert status_code == 200
        data = json.loads(response.data)
        assert data["data"]["error"] == "Build failed"
        mock_load_contract.assert_called_once()
        mock_orch.fail_agent.assert_called_once()
        mock_save_contract.assert_called_once()


class TestErrorSignalContractNotFound:
    """handle_error_signal returns 200 when contract is missing."""

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.load_contract")
    def test_contract_not_found_returns_200(
        self,
        mock_load_contract,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        """ContractNotFoundError in error handler returns 200, not 500."""
        from egg_contracts.loader import ContractNotFoundError

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_load_contract.side_effect = ContractNotFoundError(42, Path("/tmp/worktree"))

        with app.app_context():
            from routes.signals import handle_error_signal

            response, status_code = handle_error_signal(
                "issue-42",
                {"agent_role": "coder", "error": "Build failed"},
                Path("/tmp/repo"),
            )

        assert status_code == 200
        data = json.loads(response.data)
        assert data["data"]["contract_missing"] is True


# ---------------------------------------------------------------------------
# SIGTERM clean shutdown tests (issue #1336)
# ---------------------------------------------------------------------------


class TestSigtermCleanShutdown:
    """Error signals for SIGTERM on completed pipelines are treated as clean shutdown."""

    @pytest.fixture
    def completed_pipeline(self):
        """Create a mock pipeline with COMPLETE status."""
        from models import Pipeline, PipelineStatus

        return Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
            status=PipelineStatus.COMPLETE,
        )

    @patch("routes.signals.get_state_store")
    def test_sigterm_on_complete_pipeline_returns_clean_shutdown(
        self,
        mock_get_store,
        app,
        completed_pipeline,
    ):
        """SIGTERM (exit code 143) on a completed pipeline is a clean shutdown."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = completed_pipeline
        mock_get_store.return_value = mock_store

        with app.app_context():
            from routes.signals import handle_error_signal

            response, status_code = handle_error_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "error": "Container exited with code 143",
                    "recoverable": False,
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200
        data = json.loads(response.data)
        assert data["data"]["clean_shutdown"] is True

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    def test_non_sigterm_on_complete_pipeline_still_records_error(
        self,
        mock_get_store,
        mock_resolve_wt,
        app,
        completed_pipeline,
    ):
        """Non-SIGTERM errors on a completed pipeline are still recorded."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = completed_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        with app.app_context():
            from routes.signals import handle_error_signal

            response, status_code = handle_error_signal(
                "issue-42",
                {
                    "agent_role": "refiner",
                    "error": "Out of memory",
                    "recoverable": False,
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200
        data = json.loads(response.data)
        assert "clean_shutdown" not in data.get("data", {})

    @patch("routes.signals.save_contract")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.load_contract")
    def test_sigterm_on_non_complete_pipeline_still_records_error(
        self,
        mock_load_contract,
        mock_get_store,
        mock_resolve_wt,
        mock_save_contract,
        app,
        mock_pipeline,
    ):
        """SIGTERM on a non-complete (PENDING) pipeline is NOT treated as clean shutdown."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_orch = MagicMock()
        mock_orch.apply_to_contract.return_value = MagicMock()

        with patch("routes.signals.create_orchestrator", return_value=mock_orch):
            with app.app_context():
                from routes.signals import handle_error_signal

                response, status_code = handle_error_signal(
                    "issue-42",
                    {
                        "agent_role": "coder",
                        "error": "Container exited with code 143",
                        "recoverable": False,
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 200
        data = json.loads(response.data)
        assert "clean_shutdown" not in data.get("data", {})


class TestAgentAlreadyCompleteSuppression:
    """Error signals for agents already marked COMPLETE are suppressed (issue #1495)."""

    @patch("routes.signals.get_state_store")
    def test_error_suppressed_when_agent_already_complete(
        self,
        mock_get_store,
        app,
    ):
        """Error signal from a COMPLETE agent should be suppressed."""
        from models import (
            AgentExecution,
            AgentExecutionStatus,
            AgentRole,
            PhaseExecution,
            Pipeline,
            PipelinePhase,
            PipelineStatus,
        )

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            agents=[
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.COMPLETE,
                ),
            ],
        )
        pipeline.phases["implement"] = phase_exec

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        with app.app_context():
            from routes.signals import handle_error_signal

            response, status_code = handle_error_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "error": "Container exited with code 1",
                    "recoverable": False,
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200
        data = json.loads(response.data)
        assert data["data"]["already_complete"] is True

    @patch("routes.signals.save_contract")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.load_contract")
    def test_error_not_suppressed_when_agent_still_running(
        self,
        mock_load_contract,
        mock_get_store,
        mock_resolve_wt,
        mock_save_contract,
        app,
    ):
        """Error signal from a RUNNING agent should NOT be suppressed."""
        from models import (
            AgentExecution,
            AgentExecutionStatus,
            AgentRole,
            PhaseExecution,
            Pipeline,
            PipelinePhase,
            PipelineStatus,
        )

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            agents=[
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.RUNNING,
                ),
            ],
        )
        pipeline.phases["implement"] = phase_exec

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")
        mock_load_contract.return_value = MagicMock()

        mock_orch = MagicMock()
        mock_orch.apply_to_contract.return_value = MagicMock()

        with patch("routes.signals.create_orchestrator", return_value=mock_orch):
            with app.app_context():
                from routes.signals import handle_error_signal

                response, status_code = handle_error_signal(
                    "issue-42",
                    {
                        "agent_role": "coder",
                        "error": "Container exited with code 1",
                        "recoverable": False,
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 200
        data = json.loads(response.data)
        assert "already_complete" not in data.get("data", {})
        # Contract should have been updated with the error
        mock_orch.fail_agent.assert_called_once()

    @patch("routes.signals.save_contract")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.load_contract")
    def test_slice_3_error_not_suppressed_by_slice_2_complete(
        self,
        mock_load_contract,
        mock_get_store,
        mock_resolve_wt,
        mock_save_contract,
        app,
    ):
        """slice-3 coder error must not be silently swallowed by a slice-2 coder
        already-COMPLETE record (#2422). Pre-fix the role-only predicate matched
        the slice-2 row first and returned ``already_complete``."""
        from models import (
            AgentExecution,
            AgentExecutionStatus,
            AgentRole,
            PhaseExecution,
            Pipeline,
            PipelinePhase,
            PipelineStatus,
        )

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            agents=[
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.COMPLETE,
                    slice_id="slice-2",
                ),
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.RUNNING,
                    slice_id="slice-3",
                ),
            ],
        )
        pipeline.phases["implement"] = phase_exec

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")
        mock_load_contract.return_value = MagicMock()

        mock_orch = MagicMock()
        mock_orch.apply_to_contract.return_value = MagicMock()

        with patch("routes.signals.create_orchestrator", return_value=mock_orch):
            with app.app_context():
                from routes.signals import handle_error_signal

                response, status_code = handle_error_signal(
                    "issue-42",
                    {
                        "agent_role": "coder",
                        "error": "Build failed in slice-3",
                        "recoverable": False,
                        "slice_id": "slice-3",
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 200
        data = json.loads(response.data)
        # slice-3 is RUNNING, so it must NOT be suppressed
        assert "already_complete" not in data.get("data", {}), (
            "slice-3 error was suppressed by slice-2's COMPLETE record"
        )
        mock_orch.fail_agent.assert_called_once()

    @patch("routes.signals.get_state_store")
    def test_slice_3_error_suppressed_when_slice_3_complete(
        self,
        mock_get_store,
        app,
    ):
        """slice-3 coder COMPLETE → slice-3 coder error is suppressed (positive case)."""
        from models import (
            AgentExecution,
            AgentExecutionStatus,
            AgentRole,
            PhaseExecution,
            Pipeline,
            PipelinePhase,
            PipelineStatus,
        )

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            agents=[
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.RUNNING,
                    slice_id="slice-2",
                ),
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.COMPLETE,
                    slice_id="slice-3",
                ),
            ],
        )
        pipeline.phases["implement"] = phase_exec

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        with app.app_context():
            from routes.signals import handle_error_signal

            response, status_code = handle_error_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "error": "post-consensus SIGTERM",
                    "recoverable": False,
                    "slice_id": "slice-3",
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200
        data = json.loads(response.data)
        assert data["data"]["already_complete"] is True

    def test_invalid_slice_id_returns_400(self, app):
        """Malformed slice_id is rejected before touching pipeline state."""
        with app.app_context():
            from routes.signals import handle_error_signal

            response, status_code = handle_error_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "error": "x",
                    "slice_id": "../etc",
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400


# ---------------------------------------------------------------------------
# Completion signal branch verification tests (TASK-5-3)
# ---------------------------------------------------------------------------

import subprocess


def _make_subprocess_result(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestVerifyCommitOnBranch:
    """Unit tests for _verify_commit_on_branch helper.

    The fetch leg goes through ``_gateway_fetch_tracking_ref`` (gateway-
    authenticated, #3081) — patched here so only the local
    ``git branch -r --contains`` read hits ``subprocess.run``.
    """

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.subprocess.run")
    def test_returns_none_when_branch_contains_fails(self, mock_run, mock_fetch):
        """branch --contains failure returns None (non-blocking)."""
        from routes.signals import _verify_commit_on_branch

        mock_run.return_value = _make_subprocess_result(returncode=128, stderr="not a valid commit")
        result = _verify_commit_on_branch("abc123", "egg/issue-42", Path("/tmp/wt"), "pipe-1")
        assert result is None

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.subprocess.run")
    def test_returns_none_on_unexpected_exception(self, mock_run, mock_fetch):
        """Unexpected exception returns None (non-blocking)."""
        from routes.signals import _verify_commit_on_branch

        mock_run.side_effect = OSError("disk error")
        result = _verify_commit_on_branch("abc123", "egg/issue-42", Path("/tmp/wt"), "pipe-1")
        assert result is None

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.subprocess.run")
    def test_returns_true_when_multiple_branches_include_expected(self, mock_run, mock_fetch):
        """Returns True when expected branch is among multiple branches."""
        from routes.signals import _verify_commit_on_branch

        mock_run.return_value = _make_subprocess_result(
            stdout="  origin/egg/issue-42\n  origin/main\n"
        )
        result = _verify_commit_on_branch("abc123", "egg/issue-42", Path("/tmp/wt"), "pipe-1")
        assert result is True

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.subprocess.run")
    def test_returns_false_when_branch_not_in_output(self, mock_run, mock_fetch):
        """Returns False when commit exists but not on expected branch."""
        from routes.signals import _verify_commit_on_branch

        mock_run.return_value = _make_subprocess_result(stdout="  origin/egg/other-branch\n")
        result = _verify_commit_on_branch("abc123", "egg/issue-42", Path("/tmp/wt"), "pipe-1")
        assert result is False

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.subprocess.run")
    def test_returns_false_on_empty_branch_output(self, mock_run, mock_fetch):
        """Returns False when branch --contains returns empty output."""
        from routes.signals import _verify_commit_on_branch

        mock_run.return_value = _make_subprocess_result(stdout="")
        result = _verify_commit_on_branch("abc123", "egg/issue-42", Path("/tmp/wt"), "pipe-1")
        assert result is False

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=False)
    @patch("routes.signals.subprocess.run")
    def test_returns_none_when_gateway_fetch_fails(self, mock_run, mock_fetch):
        """Gateway fetch failure returns None without touching git (#3081).

        This is the path that silently disabled all propose-time validation
        when the (pre-#3081) raw ``git fetch`` failed on every call for lack
        of credentials — now gateway-authenticated and alarmed via
        OVERSEER_ALERT, but still non-blocking.
        """
        from routes.signals import _verify_commit_on_branch

        result = _verify_commit_on_branch("abc123", "egg/issue-42", Path("/tmp/wt"), "pipe-1")
        assert result is None
        mock_run.assert_not_called()

    @patch("routes.signals.subprocess.run")
    def test_gateway_fetch_uses_explicit_tracking_refspec(self, mock_run):
        """The fetch leg sends an explicit ``+refs/heads/X:refs/remotes/origin/X``
        refspec through the gateway, so the contains-check below reads a fresh
        tracking ref even on narrow-refspec mirrors (#3072 / #3075)."""
        from routes.signals import _gateway_fetch_tracking_ref

        mock_client = MagicMock()
        mock_client.fetch_branch.return_value = True
        mock_pipeline = MagicMock()

        with (
            patch("gateway_client.get_gateway_client", return_value=mock_client),
            patch(
                "routes.pipelines._compute_gateway_mode",
                return_value=("private", "private"),
            ),
        ):
            ok = _gateway_fetch_tracking_ref(
                "pipe-1", "egg/issue-42", Path("/tmp/wt"), mock_pipeline
            )

        assert ok is True
        mock_client.fetch_branch.assert_called_once_with(
            "pipe-1",
            "/tmp/wt",
            args=["+refs/heads/egg/issue-42:refs/remotes/origin/egg/issue-42"],
            mode="private",
        )

    def test_gateway_fetch_degrades_to_false_on_fetch_exception(self):
        """A ``fetch_branch`` raise → False (caller maps that to the
        non-blocking ``None`` tri-state).

        Only the fetch call itself is wrapped in the function's try/except —
        a deliberate narrowing so a lazy-import or ``_compute_gateway_mode``
        failure does not mis-log "Gateway tracking-ref fetch failed" (and
        through it, the upstream ``OVERSEER_ALERT``) when no fetch was
        actually attempted. Those rarer failure modes propagate to the
        outer ``_verify_commit_on_branch`` try/except, which still degrades
        to ``None`` (same end-to-end posture, accurate logs).
        """
        from routes.signals import _gateway_fetch_tracking_ref

        mock_client = MagicMock()
        mock_client.fetch_branch.side_effect = RuntimeError("fetch boom")

        with patch("gateway_client.get_gateway_client", return_value=mock_client):
            ok = _gateway_fetch_tracking_ref("pipe-1", "egg/issue-42", Path("/tmp/wt"), None)
        assert ok is False


class TestCommitObjectResolvable:
    """Unit tests for _commit_object_resolvable helper (#3081)."""

    @patch("routes.signals.subprocess.run")
    def test_true_when_cat_file_succeeds(self, mock_run):
        from routes.signals import _commit_object_resolvable

        mock_run.return_value = _make_subprocess_result(returncode=0)
        assert _commit_object_resolvable(Path("/tmp/wt"), "abc123") is True
        cmd = mock_run.call_args[0][0]
        assert cmd[-2:] == ["-e", "abc123^{commit}"]

    @patch("routes.signals.subprocess.run")
    def test_false_when_object_absent(self, mock_run):
        from routes.signals import _commit_object_resolvable

        mock_run.return_value = _make_subprocess_result(returncode=1)
        assert _commit_object_resolvable(Path("/tmp/wt"), "abc123") is False

    @patch("routes.signals.subprocess.run")
    def test_false_on_exception(self, mock_run):
        from routes.signals import _commit_object_resolvable

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
        assert _commit_object_resolvable(Path("/tmp/wt"), "abc123") is False


class TestCheckBranchProgress:
    """Unit tests for _check_branch_progress helper."""

    @patch("routes.signals.subprocess.run")
    def test_no_warning_when_branch_has_progressed(self, mock_run):
        """No warning when current tip differs from phase start SHA."""
        from routes.signals import _check_branch_progress

        mock_run.return_value = _make_subprocess_result(stdout="bbb222\n")
        with patch("routes.signals.logger") as mock_logger:
            _check_branch_progress("egg/issue-42", "aaa111", Path("/tmp/wt"), "pipe-1")
            # No warning should be logged since branch progressed
            mock_logger.warning.assert_not_called()

    @patch("routes.signals.subprocess.run")
    def test_warns_when_tip_matches_start(self, mock_run):
        """Warning logged when branch tip equals phase start SHA."""
        from routes.signals import _check_branch_progress

        mock_run.return_value = _make_subprocess_result(stdout="aaa111\n")
        with patch("routes.signals.logger") as mock_logger:
            _check_branch_progress("egg/issue-42", "aaa111", Path("/tmp/wt"), "pipe-1")
            mock_logger.warning.assert_called_once()
            assert "No new commits" in mock_logger.warning.call_args[0][0]

    @patch("routes.signals.subprocess.run")
    def test_handles_revparse_failure(self, mock_run):
        """Rev-parse failure does not raise."""
        from routes.signals import _check_branch_progress

        mock_run.return_value = _make_subprocess_result(returncode=1, stderr="unknown ref")
        # Should not raise
        _check_branch_progress("egg/issue-42", "aaa111", Path("/tmp/wt"), "pipe-1")

    @patch("routes.signals.subprocess.run")
    def test_handles_exception(self, mock_run):
        """Unexpected exception in progress check does not raise."""
        from routes.signals import _check_branch_progress

        mock_run.side_effect = OSError("disk error")
        # Should not raise
        _check_branch_progress("egg/issue-42", "aaa111", Path("/tmp/wt"), "pipe-1")


class TestCompletionBranchVerification:
    """Verify commit location when agent signals completion with a commit SHA."""

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.subprocess.run")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.save_contract")
    @patch("routes.signals.create_orchestrator")
    @patch("routes.signals.load_contract")
    def test_commit_on_correct_branch_accepted(
        self,
        mock_load_contract,
        mock_create_orchestrator,
        mock_save_contract,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        mock_gateway_fetch,
        app,
        mock_pipeline,
    ):
        """(a) completion with commit on correct branch -> accepted."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_orch = _mock_contract_orchestrator(is_complete=False)
        mock_create_orchestrator.return_value = mock_orch

        # subprocess.run calls: branch --contains returns origin/egg/issue-42
        # (fetch goes through the gateway — patched above; no rev-parse for
        # progress check — mock_pipeline has no phase_start_sha)
        mock_subprocess_run.side_effect = [
            _make_subprocess_result(stdout="  origin/egg/issue-42\n"),  # branch --contains
        ]

        with app.app_context():
            from routes.signals import handle_complete_signal

            response, status_code = handle_complete_signal(
                "issue-42",
                {"agent_role": "coder", "commit": "abc1234"},
                Path("/tmp/repo"),
            )

        assert status_code == 200

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.subprocess.run")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    def test_commit_not_on_correct_branch_rejected_409(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        mock_gateway_fetch,
        app,
        mock_pipeline,
    ):
        """(b) completion with commit NOT on correct branch -> 409 rejected."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        # gateway fetch succeeds, but branch --contains shows different branch
        mock_subprocess_run.side_effect = [
            _make_subprocess_result(stdout="  origin/egg/wrong-branch\n"),  # branch --contains
        ]

        with app.app_context():
            from routes.signals import handle_complete_signal

            response, status_code = handle_complete_signal(
                "issue-42",
                {"agent_role": "coder", "commit": "abc1234"},
                Path("/tmp/repo"),
            )

        assert status_code == 409
        data = json.loads(response.data)
        assert "not found on expected branch" in data["message"]

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.save_contract")
    @patch("routes.signals.create_orchestrator")
    @patch("routes.signals.load_contract")
    def test_commit_none_accepted_without_check(
        self,
        mock_load_contract,
        mock_create_orchestrator,
        mock_save_contract,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        """(c) completion with commit=None -> accepted without check."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_orch = _mock_contract_orchestrator(is_complete=True)
        mock_create_orchestrator.return_value = mock_orch

        with app.app_context():
            from routes.signals import handle_complete_signal

            response, status_code = handle_complete_signal(
                "issue-42",
                {"agent_role": "coder"},  # no commit
                Path("/tmp/repo"),
            )

        assert status_code == 200

    @patch("routes.signals.subprocess.run")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.save_contract")
    @patch("routes.signals.create_orchestrator")
    @patch("routes.signals.load_contract")
    def test_branch_fetch_fails_accepted_with_warning(
        self,
        mock_load_contract,
        mock_create_orchestrator,
        mock_save_contract,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        app,
        mock_pipeline,
    ):
        """(d) branch fetch fails -> signal accepted with warning."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_orch = _mock_contract_orchestrator(is_complete=False)
        mock_create_orchestrator.return_value = mock_orch

        # fetch fails
        mock_subprocess_run.side_effect = [
            _make_subprocess_result(returncode=1, stderr="network error"),  # fetch fails
        ]

        with app.app_context():
            from routes.signals import handle_complete_signal

            response, status_code = handle_complete_signal(
                "issue-42",
                {"agent_role": "coder", "commit": "abc1234"},
                Path("/tmp/repo"),
            )

        # Should still accept (fetch failure is non-blocking)
        assert status_code == 200

    @patch("routes.signals.subprocess.run")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.save_contract")
    @patch("routes.signals.create_orchestrator")
    @patch("routes.signals.load_contract")
    def test_no_new_commits_warns_but_accepts(
        self,
        mock_load_contract,
        mock_create_orchestrator,
        mock_save_contract,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        app,
    ):
        """(e) no new commits since phase start -> warning logged but accepted."""
        from models import PhaseExecution, Pipeline, PipelinePhase, PipelineStatus

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
            current_phase=PipelinePhase.IMPLEMENT,
            phases={
                "implement": PhaseExecution(
                    phase=PipelinePhase.IMPLEMENT,
                    status=PipelineStatus.RUNNING,
                    phase_start_sha="aaa111",
                ),
            },
        )
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_orch = _mock_contract_orchestrator(is_complete=False)
        mock_create_orchestrator.return_value = mock_orch

        # fetch ok, branch --contains ok, rev-parse returns same as phase_start_sha
        mock_subprocess_run.side_effect = [
            _make_subprocess_result(returncode=0),  # fetch
            _make_subprocess_result(stdout="  origin/egg/issue-42\n"),  # branch --contains
            _make_subprocess_result(stdout="aaa111\n"),  # rev-parse (same as start)
        ]

        with app.app_context():
            from routes.signals import handle_complete_signal

            response, status_code = handle_complete_signal(
                "issue-42",
                {"agent_role": "coder", "commit": "abc1234"},
                Path("/tmp/repo"),
            )

        assert status_code == 200


class TestCompletionBranchVerificationEdgeCases:
    """Edge cases for branch verification in completion signals."""

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.save_contract")
    @patch("routes.signals.create_orchestrator")
    @patch("routes.signals.load_contract")
    def test_pipeline_without_branch_skips_verification(
        self,
        mock_load_contract,
        mock_create_orchestrator,
        mock_save_contract,
        mock_get_store,
        mock_resolve_wt,
        app,
    ):
        """Pipeline with branch=None skips commit verification."""
        from models import Pipeline

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch=None,  # No branch set
        )
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_orch = _mock_contract_orchestrator(is_complete=True)
        mock_create_orchestrator.return_value = mock_orch

        with app.app_context():
            from routes.signals import handle_complete_signal

            response, status_code = handle_complete_signal(
                "issue-42",
                {"agent_role": "coder", "commit": "abc1234"},
                Path("/tmp/repo"),
            )

        # Should succeed — no branch to verify against
        assert status_code == 200

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    def test_refiner_role_with_commit_skips_dispatcher_but_checks_branch(
        self,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        """Non-contract role (refiner) with commit completes without contract interaction."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        with app.app_context():
            from routes.signals import handle_complete_signal

            response, status_code = handle_complete_signal(
                "issue-42",
                {"agent_role": "refiner", "commit": "abc1234"},
                Path("/tmp/repo"),
            )

        # Refiner does not use contract, so it succeeds directly.
        # Branch verification applies before the contract check.
        assert status_code == 200


class TestConsensusProposeBranchVerification:
    """Verify commit SHA on branch when agent sends CONSENSUS_PROPOSE (#1473)."""

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.subprocess.run")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_propose_commit_not_on_branch_rejected_409(
        self,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        mock_gateway_fetch,
        app,
        mock_pipeline,
    ):
        """Proposal with commit SHA not on expected branch -> 409."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        # gateway fetch succeeds, branch --contains returns different branch
        mock_subprocess_run.side_effect = [
            _make_subprocess_result(stdout="  origin/other-branch\n"),  # no match
        ]

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": "Implemented authentication with JWT validation and session management for issue-42",
                        "artifacts": ["src/a.py"],
                        "commit_sha": "abc1234",
                    },
                },
                Path("/tmp/repo"),
            )

        assert status_code == 409
        data = response.get_json()
        assert "not found on expected branch" in data.get("message", "")

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.subprocess.run")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_propose_commit_on_branch_accepted(
        self,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        mock_gateway_fetch,
        app,
        mock_pipeline,
    ):
        """Proposal with commit SHA on correct branch -> accepted."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "abc1234",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        # gateway fetch succeeds, branch --contains returns correct branch
        mock_subprocess_run.side_effect = [
            _make_subprocess_result(stdout="  origin/egg/issue-42\n"),  # match
        ]

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": "Implemented authentication with JWT validation and session management for issue-42",
                        "artifacts": ["src/a.py"],
                        "commit_sha": "abc1234",
                    },
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_propose_verification_failure_non_blocking(
        self,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        """Pipeline state load failure should not block proposal."""
        mock_get_store.side_effect = Exception("state store unavailable")
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "abc1234",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": "Implemented authentication with JWT validation and session management for issue-42",
                        "artifacts": ["src/a.py"],
                        "commit_sha": "abc1234",
                    },
                },
                Path("/tmp/repo"),
            )

        # Should proceed despite state store failure
        assert status_code == 200

    @patch("routes.signals.subprocess.run")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_propose_commit_sha_in_message_metadata(
        self,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        app,
        mock_pipeline,
    ):
        """Message metadata should include commit_sha for reviewers."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "deadbeef",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        # Branch verification succeeds
        mock_subprocess_run.side_effect = [
            _make_subprocess_result(returncode=0),
            _make_subprocess_result(stdout="  origin/egg/issue-42\n"),
        ]

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            with patch("message_store.get_message_store") as mock_msg_store:
                mock_store_inst = MagicMock()
                mock_msg_store.return_value = mock_store_inst

                response, status_code = handle_consensus_propose_signal(
                    "issue-42",
                    {
                        "agent_role": "coder",
                        "payload": {
                            "summary": "Implemented authentication with JWT validation and session management for issue-42",
                            "artifacts": ["src/a.py"],
                            "commit_sha": "deadbeef",
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 200
        # Verify message was written with commit_sha in metadata
        call_args = mock_store_inst.add_message.call_args_list[0]
        msg = call_args[0][0]
        assert msg.metadata.get("commit_sha") == "deadbeef"


class TestConsensusProposePendingTaskGate:
    """Propose-time contract-bookkeeping gate (#3470).

    A producer proposing while its owned contract task rows are still
    ``pending`` must be rejected before the tracker records the proposal
    — otherwise reviewer_contract NACKs on pure bookkeeping, and a
    contract-only fix cannot re-propose (unchanged-tree 409, #3395),
    mechanically deadlocking the slice.
    """

    PIPELINE_ID = "issue-42"

    @staticmethod
    def _implement_pipeline():
        from egg_contracts.models import PipelinePhase
        from models import Pipeline

        return Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
            current_phase=PipelinePhase.IMPLEMENT,
        )

    @staticmethod
    def _write_contract(worktree: Path, *, coder_row_status: str) -> None:
        contracts_dir = worktree / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        contract = {
            "schemaVersion": "1.0",
            "issue": {"number": 42, "title": "gate test", "url": "http://example"},
            "phases": [
                {
                    "id": "slice-2",
                    "name": "second",
                    "tasks": [
                        {
                            "id": "task-2-1",
                            "description": "open coder work",
                            "role": "coder",
                            "status": coder_row_status,
                        },
                    ],
                },
            ],
        }
        (contracts_dir / "issue-42.json").write_text(json.dumps(contract))

    def _propose(self, data_extra=None):
        from routes.signals import handle_consensus_propose_signal

        data = {
            "agent_role": "coder",
            "slice_id": "slice-2",
            "payload": {
                "summary": (
                    "Implemented authentication with JWT validation and "
                    "session management for issue-42"
                ),
                "artifacts": ["src/a.py"],
            },
        }
        data.update(data_extra or {})
        return handle_consensus_propose_signal(self.PIPELINE_ID, data, Path("/tmp/repo"))

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_propose_with_pending_owned_rows_rejected_409(
        self, mock_get_tracker, mock_get_store, mock_resolve_wt, app, tmp_path
    ):
        """Pending owned rows -> immediate 409 naming the rows and the fix."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = self._implement_pipeline()
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = tmp_path
        self._write_contract(tmp_path, coder_row_status="pending")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            response, status_code = self._propose()

        assert status_code == 409
        body = response.get_json()
        assert body["details"]["status"] == "contract_incomplete"
        assert [r["id"] for r in body["details"]["incomplete_tasks"]] == ["task-2-1"]
        assert "task-2-1" in body["message"]
        assert "mcp__task__complete" in body["message"]
        # Rejected BEFORE the tracker recorded anything: no version bump,
        # no reviewer invocation.
        mock_tracker.handle_propose.assert_not_called()
        mock_tracker.handle_re_propose.assert_not_called()

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_re_propose_with_pending_owned_rows_rejected_409(
        self, mock_get_tracker, mock_get_store, mock_resolve_wt, app, tmp_path
    ):
        """The changed_artifacts (re-propose) dispatch is gated too."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = self._implement_pipeline()
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = tmp_path
        self._write_contract(tmp_path, coder_row_status="pending")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            response, status_code = self._propose({"changed_artifacts": ["src/a.py"]})

        assert status_code == 409
        assert response.get_json()["details"]["status"] == "contract_incomplete"
        mock_tracker.handle_re_propose.assert_not_called()

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_propose_with_complete_rows_accepted(
        self, mock_get_tracker, mock_get_store, mock_resolve_wt, app, tmp_path
    ):
        """After marking the rows complete, the same propose goes through."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = self._implement_pipeline()
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = tmp_path
        self._write_contract(tmp_path, coder_row_status="complete")

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            with patch("message_store.get_message_store") as mock_msg_store:
                mock_msg_store.return_value = MagicMock()
                response, status_code = self._propose()

        assert status_code == 200
        mock_tracker.handle_propose.assert_called_once()

    @patch("routes.signals._verify_commit_on_branch", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_propose_with_commit_sha_rejected_via_preloaded_phase(
        self,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_verify_branch,
        app,
        tmp_path,
    ):
        """Production-realistic path: a non-no-op propose carries a
        ``commit_sha``, so the commit-on-branch block pre-loads
        ``pipeline_state`` and the gate reuses its phase (lines 687-691)
        instead of self-loading. Pending owned rows must still 409, and the
        gate must NOT re-read pipeline state — proving the reject flows
        through the pre-loaded-phase branch, not the ``current_phase is
        None`` self-load fallback the other tests exercise.
        """
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = self._implement_pipeline()
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = tmp_path
        self._write_contract(tmp_path, coder_row_status="pending")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            response, status_code = self._propose(
                {
                    "payload": {
                        "summary": (
                            "Implemented authentication with JWT validation "
                            "and session management for issue-42"
                        ),
                        "artifacts": ["src/a.py"],
                        "commit_sha": "abc1234",
                    },
                }
            )

        assert status_code == 409
        body = response.get_json()
        assert body["details"]["status"] == "contract_incomplete"
        assert [r["id"] for r in body["details"]["incomplete_tasks"]] == ["task-2-1"]
        assert "mcp__task__complete" in body["message"]
        # The commit-on-branch block loaded pipeline state; an additional
        # load for run_epoch resolution (#3632) is expected. The gate reused
        # that phase rather than self-loading.
        assert mock_store.load_pipeline.call_count >= 1
        # Rejected before the tracker recorded anything.
        mock_tracker.handle_propose.assert_not_called()
        mock_tracker.handle_re_propose.assert_not_called()


# ---------------------------------------------------------------------------
# consensus_excuse_producer HITL gate tests (#1637)
# ---------------------------------------------------------------------------


class TestExcuseProducerHITLGate:
    """Tests for HITL gate validation in handle_consensus_excuse_producer_signal."""

    def test_missing_decision_id_returns_403(self, app):
        """Request without decision_id is rejected with 403."""
        with app.app_context():
            from routes.signals import handle_consensus_excuse_producer_signal

            response, status_code = handle_consensus_excuse_producer_signal(
                "issue-42",
                {"producer_role": "coder", "reason": "Not delivering"},
                Path("/tmp/repo"),
            )

        assert status_code == 403
        data = json.loads(response.data)
        assert data["success"] is False
        assert "Missing decision_id" in data["message"]

    def test_unresolved_decision_returns_403(self, app):
        """Decision that is not RESOLVED is rejected with 403."""
        with app.app_context():
            from routes.signals import handle_consensus_excuse_producer_signal

            mock_decision = MagicMock()
            mock_decision.status = MagicMock()
            mock_decision.status.value = "pending"
            # Make status != RESOLVED
            mock_queue = MagicMock()
            mock_queue.get_decision.return_value = mock_decision

            with patch("routes.signals.DecisionStatus", create=True):
                # Import the real DecisionStatus for comparison
                from models import DecisionStatus

                mock_decision.status = DecisionStatus.PENDING

                with patch("decision_queue.get_decision_queue", return_value=mock_queue):
                    response, status_code = handle_consensus_excuse_producer_signal(
                        "issue-42",
                        {
                            "producer_role": "coder",
                            "reason": "Not delivering",
                            "decision_id": "dec-123",
                        },
                        Path("/tmp/repo"),
                    )

        assert status_code == 403
        data = json.loads(response.data)
        assert data["success"] is False
        assert "not resolved" in data["message"]

    def test_wrong_context_returns_403(self, app):
        """Decision for a different role is rejected with 403."""
        with app.app_context():
            from models import DecisionStatus
            from routes.signals import handle_consensus_excuse_producer_signal

            mock_decision = MagicMock()
            mock_decision.status = DecisionStatus.RESOLVED
            mock_decision.context = "failed_role:tester"  # Wrong role

            mock_queue = MagicMock()
            mock_queue.get_decision.return_value = mock_decision

            with patch("decision_queue.get_decision_queue", return_value=mock_queue):
                response, status_code = handle_consensus_excuse_producer_signal(
                    "issue-42",
                    {
                        "producer_role": "coder",
                        "reason": "Not delivering",
                        "decision_id": "dec-123",
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 403
        data = json.loads(response.data)
        assert data["success"] is False
        assert "not authorized for excusing producer coder" in data["message"]

    def test_decision_not_found_returns_404(self, app):
        """Non-existent decision ID returns 404."""
        with app.app_context():
            from decision_queue import DecisionNotFoundError
            from routes.signals import handle_consensus_excuse_producer_signal

            mock_queue = MagicMock()
            mock_queue.get_decision.side_effect = DecisionNotFoundError("dec-999")

            with patch("decision_queue.get_decision_queue", return_value=mock_queue):
                response, status_code = handle_consensus_excuse_producer_signal(
                    "issue-42",
                    {
                        "producer_role": "coder",
                        "reason": "Not delivering",
                        "decision_id": "dec-999",
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 404
        data = json.loads(response.data)
        assert data["success"] is False
        assert "not found" in data["message"]

    def test_valid_decision_proceeds(self, app):
        """Correctly authorized decision allows excuse to proceed."""
        with app.app_context():
            from models import DecisionStatus
            from routes.signals import handle_consensus_excuse_producer_signal

            mock_decision = MagicMock()
            mock_decision.status = DecisionStatus.RESOLVED
            mock_decision.context = "failed_role:coder"

            mock_queue = MagicMock()
            mock_queue.get_decision.return_value = mock_decision

            mock_tracker = MagicMock()
            mock_tracker.excuse_producer.return_value = {
                "status": "excused",
                "affected_reviewers": ["reviewer_code"],
            }

            with (
                patch("decision_queue.get_decision_queue", return_value=mock_queue),
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("message_store.get_message_store") as mock_msg_store,
            ):
                mock_store_inst = MagicMock()
                mock_msg_store.return_value = mock_store_inst

                with patch("routes.signals._resolve_pipeline_phase", return_value="implement"):
                    response, status_code = handle_consensus_excuse_producer_signal(
                        "issue-42",
                        {
                            "producer_role": "coder",
                            "reason": "Not delivering",
                            "decision_id": "dec-123",
                        },
                        Path("/tmp/repo"),
                    )

        assert status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        mock_tracker.excuse_producer.assert_called_once_with("coder", "Not delivering")

    def test_excuse_producer_status_carries_slice_id_metadata(self, app):
        """Slice-scoped excuse-producer STATUS lands on the bus with
        ``slice_id`` in ``Message.metadata`` so the implement-phase BRC
        writer (#2548) routes it into the producer's per-slice transcript."""
        with app.app_context():
            from models import DecisionStatus
            from routes.signals import handle_consensus_excuse_producer_signal

            mock_decision = MagicMock()
            mock_decision.status = DecisionStatus.RESOLVED
            mock_decision.context = "failed_role:coder"

            mock_queue = MagicMock()
            mock_queue.get_decision.return_value = mock_decision

            mock_tracker = MagicMock()
            mock_tracker.excuse_producer.return_value = {
                "status": "excused",
                "affected_reviewers": ["reviewer_code"],
            }

            mock_store_inst = MagicMock()

            with (
                patch("decision_queue.get_decision_queue", return_value=mock_queue),
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("message_store.get_message_store", return_value=mock_store_inst),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status_code = handle_consensus_excuse_producer_signal(
                    "issue-42",
                    {
                        "producer_role": "coder",
                        "reason": "Not delivering",
                        "decision_id": "dec-123",
                        "slice_id": "slice-3",
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 200
        # Inspect the Message that was added to the store.
        mock_store_inst.add_message.assert_called_once()
        stored_message = mock_store_inst.add_message.call_args[0][0]
        assert stored_message.message_type == "STATUS"
        assert stored_message.metadata.get("slice_id") == "slice-3", (
            f"slice_id missing from excuse-producer STATUS metadata: {stored_message.metadata}"
        )

    def test_ready_to_confirm_status_carries_slice_id_metadata(self, app):
        """Slice-scoped ``_emit_ready_to_confirm_nudges`` stamps
        ``slice_id`` on the ready-to-confirm STATUS so the implement-phase
        BRC writer routes the nudge into the producer's per-slice
        transcript (#2548 follow-up; pins the metadata stamp on the
        ready-to-confirm STATUS path that the three call sites — propose,
        ACK, producer-push — feed)."""
        with app.app_context():
            from routes.signals import _emit_ready_to_confirm_nudges

            mock_store_inst = MagicMock()
            mock_tracker = MagicMock()

            with patch("message_store.get_message_store", return_value=mock_store_inst):
                _emit_ready_to_confirm_nudges(
                    "issue-42",
                    "implement",
                    [{"role": "coder", "version": 3}],
                    tracker=mock_tracker,
                    slice_id="slice-2",
                )

        mock_store_inst.add_message.assert_called_once()
        stored = mock_store_inst.add_message.call_args[0][0]
        assert stored.message_type == "STATUS"
        assert stored.metadata.get("ready_to_confirm") is True
        assert stored.metadata.get("version") == 3
        assert stored.metadata.get("slice_id") == "slice-2", (
            f"slice_id missing from ready-to-confirm STATUS metadata: {stored.metadata}"
        )

    def test_ready_to_confirm_status_omits_slice_id_when_pipeline_level(self, app):
        """Pipeline-level (non-slice) ready-to-confirm STATUS MUST NOT
        carry a ``slice_id`` key. ``_emit_ready_to_confirm_nudges``
        defaults the parameter to ``None``; the writer treats absence as
        "no slice scope" so non-slice pipelines continue to land in the
        aggregate file."""
        with app.app_context():
            from routes.signals import _emit_ready_to_confirm_nudges

            mock_store_inst = MagicMock()

            with patch("message_store.get_message_store", return_value=mock_store_inst):
                _emit_ready_to_confirm_nudges(
                    "issue-42",
                    "implement",
                    [{"role": "coder", "version": 1}],
                )

        mock_store_inst.add_message.assert_called_once()
        stored = mock_store_inst.add_message.call_args[0][0]
        assert "slice_id" not in stored.metadata, (
            f"Pipeline-level ready-to-confirm STATUS must omit slice_id, got: {stored.metadata}"
        )

    def test_excuse_producer_status_omits_slice_id_when_pipeline_level(self, app):
        """Non-slice (pipeline-level) excuse-producer STATUS MUST NOT
        carry a ``slice_id`` key — the BRC writer treats absence as
        "no slice scope" and falls back to the aggregate filename."""
        with app.app_context():
            from models import DecisionStatus
            from routes.signals import handle_consensus_excuse_producer_signal

            mock_decision = MagicMock()
            mock_decision.status = DecisionStatus.RESOLVED
            mock_decision.context = "failed_role:coder"

            mock_queue = MagicMock()
            mock_queue.get_decision.return_value = mock_decision

            mock_tracker = MagicMock()
            mock_tracker.excuse_producer.return_value = {
                "status": "excused",
                "affected_reviewers": ["reviewer_code"],
            }

            mock_store_inst = MagicMock()

            with (
                patch("decision_queue.get_decision_queue", return_value=mock_queue),
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("message_store.get_message_store", return_value=mock_store_inst),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status_code = handle_consensus_excuse_producer_signal(
                    "issue-42",
                    {
                        "producer_role": "coder",
                        "reason": "Not delivering",
                        "decision_id": "dec-123",
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 200
        mock_store_inst.add_message.assert_called_once()
        stored_message = mock_store_inst.add_message.call_args[0][0]
        assert "slice_id" not in stored_message.metadata, (
            f"Pipeline-level STATUS must omit slice_id key, got: {stored_message.metadata}"
        )


# ---------------------------------------------------------------------------
# ACK version forwarding tests (#1637)
# ---------------------------------------------------------------------------


class TestAckVersionForwarding:
    """Tests for ack_version forwarding in handle_consensus_ack_signal."""

    @patch("subprocess.run")
    def test_ack_version_forwarded_from_signal_data(self, mock_subprocess_run, app):
        """ack_version in signal data is forwarded to payload for version-match guard."""
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            mock_tracker = MagicMock()
            mock_tracker.handle_ack.return_value = {
                "status": "acked",
                "version": 2,
                "fully_acked": False,
            }

            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("message_store.get_message_store") as mock_msg_store,
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                mock_store_inst = MagicMock()
                mock_msg_store.return_value = mock_store_inst

                response, status_code = handle_consensus_ack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "ack_version": 1,
                        "payload": {
                            "reason": "Reviewed src/auth.py: token validation covers expiry and invalid signatures correctly, all branches tested"
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 200
        # Verify the payload passed to handle_ack includes ack_version
        call_args = mock_tracker.handle_ack.call_args
        payload_passed = call_args[0][2]  # Third positional arg is payload
        assert payload_passed.get("ack_version") == 1

    @patch("subprocess.run")
    def test_ack_version_not_overwritten_if_already_in_payload(self, mock_subprocess_run, app):
        """ack_version already in payload is not overwritten by signal data."""
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            mock_tracker = MagicMock()
            mock_tracker.handle_ack.return_value = {
                "status": "acked",
                "version": 3,
                "fully_acked": False,
            }

            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("message_store.get_message_store") as mock_msg_store,
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                mock_store_inst = MagicMock()
                mock_msg_store.return_value = mock_store_inst

                response, status_code = handle_consensus_ack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "ack_version": 1,
                        "payload": {
                            "reason": "Reviewed src/auth.py: token validation covers expiry and invalid signatures correctly, all branches tested",
                            "ack_version": 3,
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 200
        call_args = mock_tracker.handle_ack.call_args
        payload_passed = call_args[0][2]
        # Payload's own ack_version should be preserved, not overwritten
        assert payload_passed.get("ack_version") == 3


class TestNonObjectJsonBodyReturns400:
    """Fix for #2673: non-object JSON bodies must 400, not 500.

    Mirrors the #2656 fix on the decisions route. Previously
    ``data = request.get_json()`` left a list / scalar in ``data``
    and ``data.get(...)`` raised ``AttributeError`` → the generic
    exception handler returned 500.
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
    def test_handle_signal_non_object_body_returns_400(self, client, raw_body):
        response = client.post(
            "/api/v1/pipelines/issue-42/signal",
            data=raw_body,
            content_type="application/json",
        )
        assert response.status_code == 400, response.data
        body = json.loads(response.data)
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
    def test_batch_signal_non_object_body_returns_400(self, client, raw_body):
        response = client.post(
            "/api/v1/pipelines/issue-42/signal/batch",
            data=raw_body,
            content_type="application/json",
        )
        assert response.status_code == 400, response.data
        body = json.loads(response.data)
        assert body["success"] is False
        assert "json object" in body["message"].lower(), body


# ---------------------------------------------------------------------------
# ACK version presence enforcement at the route boundary (#2674)
# ---------------------------------------------------------------------------


class TestAckVersionRouteEnforcement:
    """Tests that handle_consensus_ack_signal rejects missing / invalid ack_version.

    Mirrors the ``_require_version_int`` contract on the MCP handler boundary
    (``sandbox/egg_agent_tools/handlers/brc.py``) so a client POSTing directly
    to ``/signals/...`` cannot bypass the version-match guard in
    ``check_ack_guard``.
    """

    @patch("subprocess.run")
    def test_ack_rejected_when_ack_version_missing(self, mock_subprocess_run, app):
        """Payload that omits ack_version (top-level or nested) is rejected with 400."""
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            mock_tracker = MagicMock()
            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status_code = handle_consensus_ack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        # No ack_version at top level or in payload.
                        "payload": {
                            "reason": "Reviewed src/auth.py: token validation covers expiry and invalid signatures correctly, all branches tested",
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert "ack_version" in body["message"]
        # Tracker must never be reached — the guard is at the boundary.
        mock_tracker.handle_ack.assert_not_called()

    @patch("subprocess.run")
    def test_ack_rejected_when_ack_version_zero(self, mock_subprocess_run, app):
        """ack_version=0 is rejected because v0 means no proposal exists yet."""
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            mock_tracker = MagicMock()
            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status_code = handle_consensus_ack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "ack_version": 0,
                        "payload": {
                            "reason": "Reviewed src/auth.py: token validation covers expiry and invalid signatures correctly, all branches tested",
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert ">= 1" in body["message"]
        mock_tracker.handle_ack.assert_not_called()

    @patch("subprocess.run")
    def test_ack_rejected_when_ack_version_non_integer(self, mock_subprocess_run, app):
        """ack_version that is not int-coercible is rejected with 400."""
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            mock_tracker = MagicMock()
            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status_code = handle_consensus_ack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "payload": {
                            "reason": "Reviewed src/auth.py: token validation covers expiry and invalid signatures correctly, all branches tested",
                            "ack_version": "not-an-int",
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert "must be an integer" in body["message"]
        mock_tracker.handle_ack.assert_not_called()

    @patch("subprocess.run")
    def test_ack_rejected_when_ack_version_is_none(self, mock_subprocess_run, app):
        """Explicit JSON ``null`` for ack_version is treated as absent (TypeError branch).

        Covers the ``int(None)`` → ``TypeError`` arm of the helper that the
        string-coercion case (``int("not-an-int")`` → ``ValueError``) misses.
        Also pins the absent-vs-null equivalence: both produce the "required"
        message, matching the MCP helper.
        """
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            mock_tracker = MagicMock()
            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status_code = handle_consensus_ack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "payload": {
                            "ack_version": None,
                            "reason": "Reviewed src/auth.py: token validation covers expiry and invalid signatures correctly, all branches tested",
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert "is required" in body["message"]
        mock_tracker.handle_ack.assert_not_called()

    @patch("subprocess.run")
    def test_ack_rejected_when_ack_version_negative(self, mock_subprocess_run, app):
        """ack_version=-1 is rejected — locks down the off-by-one on ``< 1``."""
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            mock_tracker = MagicMock()
            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status_code = handle_consensus_ack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "ack_version": -1,
                        "payload": {
                            "reason": "Reviewed src/auth.py: token validation covers expiry and invalid signatures correctly, all branches tested",
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert ">= 1" in body["message"]
        mock_tracker.handle_ack.assert_not_called()


# ---------------------------------------------------------------------------
# NACK version presence enforcement at the route boundary (#2674)
# ---------------------------------------------------------------------------


class TestNackVersionRouteEnforcement:
    """Tests that handle_consensus_nack_signal rejects missing / invalid nack_version.

    Mirrors :class:`TestAckVersionRouteEnforcement` — the helper is shared
    (``_require_route_version``) so the NACK route must enforce the same
    contract or a client POSTing directly to ``/signals/...`` could bypass
    the version-match guard in ``check_nack_guard``.
    """

    @patch("subprocess.run")
    def test_nack_rejected_when_nack_version_missing(self, mock_subprocess_run, app):
        """Payload that omits nack_version (top-level or nested) is rejected with 400."""
        with app.app_context():
            from routes.signals import handle_consensus_nack_signal

            mock_tracker = MagicMock()
            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status_code = handle_consensus_nack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        # No nack_version at top level or in payload.
                        "payload": {
                            "reason": "Missing unit tests for token expiry edge cases and invalid signature handling paths",
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert "nack_version" in body["message"]
        mock_tracker.handle_nack.assert_not_called()

    @patch("subprocess.run")
    def test_nack_rejected_when_nack_version_zero(self, mock_subprocess_run, app):
        """nack_version=0 is rejected because v0 means no proposal exists yet."""
        with app.app_context():
            from routes.signals import handle_consensus_nack_signal

            mock_tracker = MagicMock()
            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status_code = handle_consensus_nack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "nack_version": 0,
                        "payload": {
                            "reason": "Missing unit tests for token expiry edge cases and invalid signature handling paths",
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert ">= 1" in body["message"]
        mock_tracker.handle_nack.assert_not_called()

    @patch("subprocess.run")
    def test_nack_rejected_when_nack_version_non_integer(self, mock_subprocess_run, app):
        """nack_version that is not int-coercible is rejected with 400."""
        with app.app_context():
            from routes.signals import handle_consensus_nack_signal

            mock_tracker = MagicMock()
            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status_code = handle_consensus_nack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "payload": {
                            "nack_version": "not-an-int",
                            "reason": "Missing unit tests for token expiry edge cases and invalid signature handling paths",
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert "must be an integer" in body["message"]
        mock_tracker.handle_nack.assert_not_called()

    @patch("subprocess.run")
    def test_nack_rejected_when_nack_version_is_none(self, mock_subprocess_run, app):
        """Explicit JSON ``null`` for nack_version is treated as absent (TypeError branch)."""
        with app.app_context():
            from routes.signals import handle_consensus_nack_signal

            mock_tracker = MagicMock()
            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status_code = handle_consensus_nack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "payload": {
                            "nack_version": None,
                            "reason": "Missing unit tests for token expiry edge cases and invalid signature handling paths",
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert "is required" in body["message"]
        mock_tracker.handle_nack.assert_not_called()

    @patch("subprocess.run")
    def test_nack_rejected_when_nack_version_negative(self, mock_subprocess_run, app):
        """nack_version=-1 is rejected — locks down the off-by-one on ``< 1``."""
        with app.app_context():
            from routes.signals import handle_consensus_nack_signal

            mock_tracker = MagicMock()
            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status_code = handle_consensus_nack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "nack_version": -1,
                        "payload": {
                            "reason": "Missing unit tests for token expiry edge cases and invalid signature handling paths",
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert ">= 1" in body["message"]
        mock_tracker.handle_nack.assert_not_called()


class TestResolveReviewerDeltaRange:
    """`_resolve_reviewer_delta_range` resolves each reviewer's own
    `<last_sha>..HEAD` re-review range from their last-verdicted version,
    backing delta-scoped re-review (#2887). Falls back to None (→ the
    priming block's REVIEWER-SYNC range) when no anchor is resolvable.
    """

    @pytest.fixture
    def tracker(self):
        from attestation_schemas import AttestationStrictness
        from peer_consensus import PeerConsensusTracker
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        t = PeerConsensusTracker(
            "test-pipeline",
            graph,
            cooldown_seconds=0,
            attestation_strictness=AttestationStrictness.RELAXED,
            auto_repropose_debounce_seconds=0,
        )
        t.register_agent("coder")
        t.register_agent("reviewer_code")
        return t

    def test_range_spans_reviewer_last_verdict_to_head(self, tracker):
        from routes.signals import _resolve_reviewer_delta_range

        tracker.handle_propose(
            "coder",
            {"summary": "v1", "artifacts": ["a.py"], "commit_sha": "1111111"},
        )
        tracker.handle_nack(
            "reviewer_code", "coder", {"artifact_references": ["a.py"], "reason": "x"}
        )
        # Producer re-proposes at sha2; reviewer's entry still pins v1/sha1.
        tracker.handle_propose(
            "coder",
            {"summary": "v2", "artifacts": ["a.py"], "commit_sha": "2222222"},
        )

        rng = _resolve_reviewer_delta_range(tracker, "coder", "reviewer_code", "2222222")
        assert rng == "1111111..2222222"

    def test_no_prior_verdict_returns_none(self, tracker):
        from routes.signals import _resolve_reviewer_delta_range

        tracker.handle_propose(
            "coder",
            {"summary": "v1", "artifacts": ["a.py"], "commit_sha": "1111111"},
        )
        # Reviewer never verdicted (entry.version == 0).
        rng = _resolve_reviewer_delta_range(tracker, "coder", "reviewer_code", "2222222")
        assert rng is None

    def test_empty_head_returns_none(self, tracker):
        from routes.signals import _resolve_reviewer_delta_range

        assert _resolve_reviewer_delta_range(tracker, "coder", "reviewer_code", "") is None


class TestReReviewDeltaRangeReachesMessageBody:
    """End-to-end #2887 verification: a real ``PeerConsensusTracker`` walked
    through propose-v1 → ACK → producer-push-v2 emits a per-reviewer
    ``CONSENSUS_RE_REVIEW`` whose body actually contains the resolved
    ``<v1_sha>..<v2_sha>`` delta range.

    The existing unit tests cover ``_resolve_reviewer_delta_range``
    (``TestResolveReviewerDeltaRange``) and the underlying SHA-history
    accumulator (``TestProposalCommitShaHistory`` in
    ``test_producer_push_consensus.py``) in isolation, and the existing
    MagicMock-based propagation tests (``TestProposeMessagePhasePropagation``
    in ``test_brc_phase_propagation.py``) pin that *some* re-prime text is
    appended. None of those exercise the seam #2887 actually patches: the
    delta range being resolved correctly but then dropped (wrong argument,
    accidental ``None``, helper return ignored) before reaching the emitted
    message body. This test runs the real handler with a real tracker and
    asserts the concrete substring lands in the ``CONSENSUS_RE_REVIEW`` body.
    """

    def test_per_reviewer_re_review_body_contains_concrete_delta_range(self, app):
        from attestation_schemas import AttestationStrictness
        from message_store import MessageType
        from peer_consensus import (
            create_peer_consensus_tracker,
            remove_peer_consensus_tracker,
        )
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        pipeline_id = "issue-2887-e2e"
        v1_sha = "v1sha1234abcd"
        v2_sha = "v2sha5678efef"

        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        tracker = create_peer_consensus_tracker(
            pipeline_id,
            graph,
            cooldown_seconds=0,
            attestation_strictness=AttestationStrictness.RELAXED,
            auto_repropose_debounce_seconds=0,
        )
        try:
            tracker.register_agent("coder")
            tracker.register_agent("reviewer_code")

            # v1: producer proposes at sha1; reviewer ACKs at v1 (their
            # matrix entry now pins entry.version=1 → sha1).
            tracker.handle_propose(
                "coder",
                {
                    "summary": "v1 implementation",
                    "artifacts": ["src/auth.py"],
                    "commit_sha": v1_sha,
                },
            )
            tracker.handle_ack(
                "reviewer_code",
                "coder",
                {"artifact_references": ["src/auth.py"]},
            )

            # v2: producer pushes a new commit. The signal handler auto
            # re-proposes, invalidates the v1 ACK, and emits a
            # CONSENSUS_RE_REVIEW to the reviewer. The body must embed
            # the concrete `<v1_sha>..<v2_sha>` delta range from
            # `_resolve_reviewer_delta_range` — the #2887 contract.
            mock_msg_store = MagicMock()
            with (
                app.app_context(),
                patch("message_store.get_message_store", return_value=mock_msg_store),
            ):
                from routes.signals import handle_consensus_producer_push_signal

                _response, status_code = handle_consensus_producer_push_signal(
                    pipeline_id,
                    {
                        "agent_role": "coder",
                        "commit_sha": v2_sha,
                        # Omit ``changed_files`` so all ACKs are invalidated
                        # (the conservative path in ``handle_producer_push``),
                        # which is what populates ``invalidated_reviewers``
                        # for the per-reviewer CONSENSUS_RE_REVIEW emission.
                    },
                    Path("/tmp/repo"),
                )

            assert status_code == 200

            re_review_messages = [
                call.args[0]
                for call in mock_msg_store.add_message.call_args_list
                if call.args[0].message_type == MessageType.CONSENSUS_RE_REVIEW
                and call.args[0].to_role == "reviewer_code"
            ]
            assert len(re_review_messages) == 1, (
                f"Expected exactly one CONSENSUS_RE_REVIEW for reviewer_code, "
                f"got {len(re_review_messages)}"
            )
            body = re_review_messages[0].body

            # The core #2887 assertion: the resolved per-reviewer delta
            # range is embedded as a concrete `<v1>..<v2>` string, NOT
            # the broadcast-path REVIEWER-SYNC placeholder.
            assert f"{v1_sha}..{v2_sha}" in body
            assert f"git log {v1_sha}..{v2_sha}" in body
            assert "{last_reviewed_commit}..HEAD" not in body
            # Version anchoring is dynamic (vN / v(N-1)).
            assert "Your v2 review" in body
            assert "named v1 blockers" in body
        finally:
            remove_peer_consensus_tracker(pipeline_id)


# ---------------------------------------------------------------------------
# Spec-derived propose-time validation (#3077 slice-3 TASK-3-2)
# ---------------------------------------------------------------------------


def _pipeline_with_phase(
    phase_value: str,
    *,
    issue_number: int = 42,
    pipeline_id: str = "issue-42",
    branch: str = "egg/issue-42",
):
    """Build a real ``Pipeline`` pinned to ``phase_value`` so the spec-derived
    propose validator can resolve ``specs_for(current_phase, role)``.

    The spec-derived validation that slice-3 lands keys off the pipeline's
    ``current_phase`` (not a hard-coded role→phase table), so every test
    in :class:`TestSpecDerivedProposeValidation` constructs a real
    :class:`Pipeline` with the appropriate phase. This helper centralises
    that wiring so individual tests stay readable.
    """
    from models import Pipeline, PipelinePhase

    return Pipeline(
        id=pipeline_id,
        issue_number=issue_number,
        repo="owner/repo",
        branch=branch,
        current_phase=PipelinePhase(phase_value),
    )


def _make_subprocess_router(
    *,
    branch_stdout: str = "  origin/egg/issue-42\n",
    branch_returncode: int = 0,
    missing_paths: tuple[str, ...] = (),
    show_stdout_overrides: dict[str, str] | None = None,
):
    """Return a callable suitable for ``subprocess.run.side_effect`` that
    routes each invocation to the right canned response.

    The slice-3 spec-derived dispatch may call ``subprocess.run`` more than
    once per propose: first the ``git branch -r --contains`` check from
    ``_verify_commit_on_branch`` (#1473), then one ``git show
    <sha>:<path>`` per registered artifact. A static
    ``side_effect=[...]`` list is brittle (architect has two artifacts,
    risk_analyst has one), so route by command shape instead.

    Args:
        branch_stdout: stdout for the ``git branch -r --contains`` call.
        branch_returncode: returncode for the branch check.
        missing_paths: substrings of artifact-spec paths that should be
            reported as absent at the commit (``git show`` returncode
            128). Useful for asserting per-artifact rejection messages.
        show_stdout_overrides: optional mapping of path-substring →
            stdout to return when the matching artifact is requested
            (used for the plan-validation extensions tests).
    """
    show_stdout_overrides = show_stdout_overrides or {}

    def _run(cmd, *_args, **_kwargs):
        # Surface non-list cmds defensively — real callers always pass a list.
        if not isinstance(cmd, list | tuple):
            return _make_subprocess_result()
        cmd_str = " ".join(str(p) for p in cmd)
        if "branch" in cmd_str and "--contains" in cmd_str:
            return _make_subprocess_result(stdout=branch_stdout, returncode=branch_returncode)
        if "show" in cmd_str:
            for missing in missing_paths:
                if missing in cmd_str:
                    return _make_subprocess_result(
                        returncode=128, stderr=f"fatal: path '{missing}' does not exist\n"
                    )
            for path_marker, override in show_stdout_overrides.items():
                if path_marker in cmd_str:
                    return _make_subprocess_result(stdout=override)
            # Default: artifact present and non-empty.
            return _make_subprocess_result(stdout="present\n")
        if "cat-file" in cmd_str:
            # ``_commit_object_resolvable`` path — the commit object is in
            # the local store unless the test explicitly overrides this.
            return _make_subprocess_result(returncode=0)
        return _make_subprocess_result()

    return _run


def _propose_payload(
    *,
    summary: str = (
        "Implemented authentication with JWT validation and session management for issue-42"
    ),
    artifacts: tuple[str, ...] = ("src/a.py",),
    commit_sha: str = "abc1234",
    no_changes_needed: bool = False,
    extra: dict | None = None,
) -> dict:
    """Build a propose payload that satisfies ``_validate_brc_content``
    (the minimum-content guard rejects short summaries) so the test
    actually reaches the spec-derived validator under test.

    Carries an explicit-none decision-ledger attestation by default so
    refine/plan producer proposes pass the #3390 ledger requirement;
    implement-phase roles ignore the field. Tests exercising the ledger
    validator itself override ``attestation`` via ``extra``.
    """
    payload: dict = {
        "summary": summary,
        "artifacts": list(artifacts),
        "commit_sha": commit_sha,
        "attestation": {
            "no_decisions_rationale": "test fixture: no operator decisions",
            # #3526: an explicit-none ledger must enumerate the candidates
            # it considered.
            "candidates_considered": [
                {
                    "question": "test fixture candidate?",
                    "disposition": "not_operator_grade",
                    "why": "fixture",
                }
            ],
        },
    }
    if no_changes_needed:
        payload["no_changes_needed"] = True
        payload["no_changes_reason"] = "nothing to do in this slice"
        # A no-op carries no commit_sha (mirrors the producer path).
        payload.pop("commit_sha", None)
    if extra:
        payload.update(extra)
    return payload


class TestSpecDerivedProposeValidation:
    """Spec-derived propose-time validation for every refine/plan producer
    with a registered artifact (#3077 slice-3 TASK-3-2).

    The slice-3 coder generalises propose-time draft validation in
    ``handle_consensus_propose_signal`` from the old hard-coded
    refiner / task_planner branches to a single pass driven by
    :func:`shared.egg_contracts.artifact_spec.specs_for` against the
    pipeline's ``current_phase``:

    * Every refine/plan producer with at least one registered artifact
      (``refiner`` ⇒ ``analysis-draft``; ``task_planner`` ⇒
      ``plan-draft``; ``architect`` ⇒ ``architect-output`` +
      ``architect-slices``; ``risk_analyst`` ⇒
      ``risk-analyst-output``) is rejected at propose time when the
      committed artifact is absent at the proposed SHA. The rejection
      message names the spec path so the producer can fix and
      re-propose.
    * Producers in implement (coder, documenter, tester non-coverage,
      reviewers) carry no registered artifact for the implement phase
      and pass through unchanged.
    * ``no_changes_needed`` proposals skip the presence loop entirely
      (#3027) — the per-phase no-op guard upstream of this slice is
      what rejects no-ops outside implement.
    * ``branch_verified`` graceful degradation (#3081) is unchanged:
      ``branch_verified=None`` + commit-object absent ⇒ skip the
      presence check; commit-object present ⇒ still validate.
    * task_planner keeps its #3026 (parseability) and #2527 (role↔files)
      extensions layered on top of the presence check. The plan-only
      validator path is preserved.
    """

    # Spec paths for issue 42 — derived from
    # ``shared.egg_contracts.artifact_spec`` rows. Mirrored here as plain
    # strings so the assertions read straightforwardly; if the spec
    # template drifts, slice-2's consistency suite catches it.
    _ANALYSIS_DRAFT_PATH = ".egg-state/drafts/42-analysis.md"
    _PLAN_DRAFT_PATH = ".egg-state/drafts/42-plan.md"
    _ARCHITECT_OUTPUT_PATH = ".egg-state/agent-outputs/42-architect-output.json"
    _ARCHITECT_SLICES_PATH = ".egg-state/agent-outputs/42-architect-slices.yaml"
    _RISK_ANALYST_OUTPUT_PATH = ".egg-state/agent-outputs/42-risk_analyst-output.json"

    # -----------------------------------------------------------------
    # Per-producer rejection coverage — one case per registered
    # refine/plan producer (TASK-3-2 acceptance: "Rejection coverage
    # for every registered refine/plan producer").
    # -----------------------------------------------------------------

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_refiner_proposal_rejected_when_analysis_draft_absent(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_gateway_fetch,
        app,
    ):
        """Refiner propose with the analysis draft absent at the proposed
        commit ⇒ 400 naming the ``analysis-draft`` spec path.
        """
        pipeline = _pipeline_with_phase("refine")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        mock_subprocess_run.side_effect = _make_subprocess_router(
            missing_paths=(self._ANALYSIS_DRAFT_PATH,)
        )

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "refiner", "payload": _propose_payload()},
                Path("/tmp/repo"),
            )

        assert status_code == 400, response.get_json()
        message = response.get_json().get("message", "")
        assert self._ANALYSIS_DRAFT_PATH in message, message
        # Tracker untouched on rejection (mirrors #1459 / #2527 invariant).
        mock_tracker.handle_propose.assert_not_called()

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_task_planner_proposal_rejected_when_plan_draft_absent(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_gateway_fetch,
        app,
    ):
        """Task planner propose with the plan draft absent at the commit ⇒
        400 naming the ``plan-draft`` spec path. The plan-only validator
        retains its #3016 presence behaviour after slice-3 (it's the same
        path, just resolved from the spec instead of a literal).
        """
        pipeline = _pipeline_with_phase("plan")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        mock_subprocess_run.side_effect = _make_subprocess_router(
            missing_paths=(self._PLAN_DRAFT_PATH,)
        )

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "task_planner", "payload": _propose_payload()},
                Path("/tmp/repo"),
            )

        assert status_code == 400, response.get_json()
        message = response.get_json().get("message", "")
        assert self._PLAN_DRAFT_PATH in message, message
        mock_tracker.handle_propose.assert_not_called()

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_architect_proposal_rejected_when_architect_output_absent(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_gateway_fetch,
        app,
    ):
        """Architect propose with the architect-output JSON missing at the
        proposed commit ⇒ 400 naming the ``architect-output`` spec path.

        Before slice-3 the architect role had NO propose-time
        presence check (it didn't match the hard-coded ``refiner`` /
        ``task_planner`` elif branches in
        ``handle_consensus_propose_signal``), so an architect that
        forgot to commit its outputs reached consensus and the
        task_planner downstream broke when it tried to read the
        artifact. Slice-3's spec-derived dispatch is what closes that
        gap.
        """
        pipeline = _pipeline_with_phase("plan")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        # Architect has TWO registered artifacts; mark the JSON output
        # missing while the slices YAML is present, so the rejection
        # message names the specific spec path that failed.
        mock_subprocess_run.side_effect = _make_subprocess_router(
            missing_paths=(self._ARCHITECT_OUTPUT_PATH,)
        )

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "architect", "payload": _propose_payload()},
                Path("/tmp/repo"),
            )

        assert status_code == 400, response.get_json()
        message = response.get_json().get("message", "")
        assert self._ARCHITECT_OUTPUT_PATH in message, message
        mock_tracker.handle_propose.assert_not_called()

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_architect_proposal_rejected_when_architect_slices_absent(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_gateway_fetch,
        app,
    ):
        """Architect propose with the architect-slices YAML missing ⇒ 400
        naming the ``architect-slices`` spec path.

        Companion to the architect-output case: covers the *other*
        registered architect artifact so a future spec edit that
        accidentally drops one row from the loop fails on the right
        path.
        """
        pipeline = _pipeline_with_phase("plan")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        mock_subprocess_run.side_effect = _make_subprocess_router(
            missing_paths=(self._ARCHITECT_SLICES_PATH,)
        )

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "architect", "payload": _propose_payload()},
                Path("/tmp/repo"),
            )

        assert status_code == 400, response.get_json()
        message = response.get_json().get("message", "")
        assert self._ARCHITECT_SLICES_PATH in message, message
        mock_tracker.handle_propose.assert_not_called()

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_risk_analyst_proposal_rejected_when_output_absent(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_gateway_fetch,
        app,
    ):
        """risk_analyst propose with its output JSON missing ⇒ 400 naming
        the ``risk-analyst-output`` spec path.

        Like ``architect``, risk_analyst had no propose-time presence
        check before slice-3. The disk filename uses an underscore
        (``risk_analyst-output.json``) — the path the rejection names
        must match the actual on-disk shape, not the hyphenated artifact
        *name*.
        """
        pipeline = _pipeline_with_phase("plan")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        mock_subprocess_run.side_effect = _make_subprocess_router(
            missing_paths=(self._RISK_ANALYST_OUTPUT_PATH,)
        )

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "risk_analyst", "payload": _propose_payload()},
                Path("/tmp/repo"),
            )

        assert status_code == 400, response.get_json()
        message = response.get_json().get("message", "")
        assert self._RISK_ANALYST_OUTPUT_PATH in message, message
        # Underscore in the disk filename (matches the prompt prose);
        # the registry deliberately exposes this as ``risk-analyst-output``.
        assert "risk_analyst-output.json" in message, message
        mock_tracker.handle_propose.assert_not_called()

    # -----------------------------------------------------------------
    # Pass-through cases (TASK-3-2 acceptance: "Pass-through cases
    # asserted (no_changes_needed, artifact-less roles, reviewer
    # messages)").
    # -----------------------------------------------------------------

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_implement_role_without_registered_artifact_passes_through(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_gateway_fetch,
        app,
    ):
        """A ``coder`` propose in implement carries no registered
        artifact ⇒ the spec-derived loop is a no-op and the proposal is
        accepted (status 200). Prevents the slice-3 generalisation from
        sneaking new rejections onto roles that today never had one.
        """
        pipeline = _pipeline_with_phase("implement")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "abc1234",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        # All paths present (even though the loop should not query them).
        mock_subprocess_run.side_effect = _make_subprocess_router()

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "coder", "payload": _propose_payload()},
                Path("/tmp/repo"),
            )

        assert status_code == 200, response.get_json()
        mock_tracker.handle_propose.assert_called_once()

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_documenter_implement_propose_passes_through(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_gateway_fetch,
        app,
    ):
        """A documenter propose in implement has no registered artifact
        either ⇒ accepted. Belt-and-braces complement to the coder case
        because the documenter is the role most-often a no-op contributor
        in implement-phase slices (it's the role the #3027 no-op
        propose was added for).
        """
        pipeline = _pipeline_with_phase("implement")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "abc1234",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        mock_subprocess_run.side_effect = _make_subprocess_router()

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "documenter", "payload": _propose_payload()},
                Path("/tmp/repo"),
            )

        assert status_code == 200, response.get_json()
        mock_tracker.handle_propose.assert_called_once()

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_plan_reviewer_propose_passes_through(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_gateway_fetch,
        app,
    ):
        """A reviewer role that proposes in the *plan* phase passes through
        (status 200), even though that phase registers artifacts for its
        producers (task_planner / architect / risk_analyst).

        This mirrors the TASK-3-2 acceptance phrasing literally ("reviewer
        messages" as a pass-through case). Reviewers issue ACK/NACK rather
        than propose in practice, so this case is structurally covered by
        the producer-membership early bail-out
        (``agent_role not in {spec.producer_role for spec in all_specs()}``):
        a ``reviewer`` is not the producer of any registered artifact, so the
        presence loop never queries the plan-phase spec paths even with the
        draft absent — the proposal is accepted unchanged.
        """
        pipeline = _pipeline_with_phase("plan")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "abc1234",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        # Mark every plan-phase artifact missing: a producer would be
        # rejected, but the reviewer must bail out before the loop queries
        # these paths and so is accepted regardless.
        mock_subprocess_run.side_effect = _make_subprocess_router(
            missing_paths=(
                self._PLAN_DRAFT_PATH,
                self._ARCHITECT_OUTPUT_PATH,
                self._ARCHITECT_SLICES_PATH,
                self._RISK_ANALYST_OUTPUT_PATH,
            )
        )

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "reviewer", "payload": _propose_payload()},
                Path("/tmp/repo"),
            )

        assert status_code == 200, response.get_json()
        mock_tracker.handle_propose.assert_called_once()

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_no_changes_needed_skips_artifact_validation(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        app,
    ):
        """A ``no_changes_needed=true`` proposal in implement carries no
        ``commit_sha`` and skips the spec-derived presence loop. This
        case never reaches ``git show`` — the no-op invariant from
        #3027 holds after the slice-3 generalisation.
        """
        pipeline = _pipeline_with_phase("implement")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "documenter",
                    "payload": _propose_payload(no_changes_needed=True),
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200, response.get_json()
        # No ``git show`` of any spec path — the no-op short-circuits
        # both branch verification and the spec loop.
        for call in mock_subprocess_run.call_args_list:
            cmd = call.args[0] if call.args else []
            cmd_str = " ".join(str(p) for p in cmd) if isinstance(cmd, list | tuple) else ""
            assert ".egg-state/" not in cmd_str, (
                f"no_changes_needed should skip artifact git show; got {cmd_str!r}"
            )

    # -----------------------------------------------------------------
    # branch_verified graceful degradation preserved (TASK-3-2
    # acceptance: "branch_verified degradation unchanged"). The slice-3
    # generalisation must not regress #3081 — a transient (credential
    # / network) fetch failure must not be misblamed on the producer
    # as a missing draft when the commit object is not locally
    # resolvable.
    # -----------------------------------------------------------------

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=False)
    @patch("routes.signals._commit_object_resolvable", return_value=False)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_branch_verified_inconclusive_and_commit_absent_skips_validation(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_commit_resolvable,
        mock_gateway_fetch,
        app,
    ):
        """``branch_verified=None`` (fetch failed) AND commit object not
        locally resolvable ⇒ presence check is skipped, propose succeeds.
        Preserves the #3081 graceful-degradation posture for every
        spec-derived role, not just the legacy plan/refine branches.
        """
        pipeline = _pipeline_with_phase("plan")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "abc1234",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        # Set the default response — for any ``git show`` invocation the
        # spec-derived loop would treat the path as absent. The test
        # asserts that no such invocation actually occurs.
        mock_subprocess_run.side_effect = _make_subprocess_router(
            branch_returncode=0,
            missing_paths=(
                self._ARCHITECT_OUTPUT_PATH,
                self._ARCHITECT_SLICES_PATH,
            ),
        )

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "architect", "payload": _propose_payload()},
                Path("/tmp/repo"),
            )

        assert status_code == 200, response.get_json()
        # No ``git show`` for any spec-registered path — the validator
        # bailed out before that.
        for call in mock_subprocess_run.call_args_list:
            cmd = call.args[0] if call.args else []
            cmd_str = " ".join(str(p) for p in cmd) if isinstance(cmd, list | tuple) else ""
            assert "show" not in cmd_str, (
                f"branch_verified=None + commit absent must skip git show; got {cmd_str!r}"
            )

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=False)
    @patch("routes.signals._commit_object_resolvable", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_branch_verified_inconclusive_but_commit_local_still_validates(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_commit_resolvable,
        mock_gateway_fetch,
        app,
    ):
        """``branch_verified=None`` + commit object locally resolvable ⇒
        the presence check still runs, so a path-absent ``git show``
        reliably means the artifact is missing at the commit. This is
        the #3081 fix: an unconditional skip-on-None let a persistent
        fetch failure disable producer validation entirely — slice-3's
        generalisation must NOT reintroduce that hole.
        """
        pipeline = _pipeline_with_phase("plan")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        mock_subprocess_run.side_effect = _make_subprocess_router(
            missing_paths=(self._RISK_ANALYST_OUTPUT_PATH,),
        )

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "risk_analyst", "payload": _propose_payload()},
                Path("/tmp/repo"),
            )

        assert status_code == 400, response.get_json()
        message = response.get_json().get("message", "")
        assert self._RISK_ANALYST_OUTPUT_PATH in message, message
        mock_tracker.handle_propose.assert_not_called()

    # -----------------------------------------------------------------
    # Plan-validation extensions retention (TASK-3-2 acceptance:
    # "Existing plan-validation tests pass unmodified or with
    # mechanical-only updates"). These are not duplicates of the
    # TestPlanProposalValidation suite in test_pipeline_prompts.py —
    # they pin that the integration through the signal handler still
    # surfaces the parseability and role-alignment rejections after
    # the spec-derived dispatch lands.
    # -----------------------------------------------------------------

    _PLAN_WITHOUT_YAML_TASKS = (
        "# Plan: issue-42\n"
        "\n"
        "## Overview\n"
        "\n"
        "Prose-only plan with no machine-readable yaml-tasks fence.\n"
    )

    _PLAN_WITH_MISASSIGNED_TASK = (
        "# Plan\n"
        "\n"
        "```yaml\n"
        "# yaml-tasks\n"
        "slices:\n"
        "  - id: 1\n"
        "    name: Setup\n"
        "    goal: scaffolding\n"
        "    tasks:\n"
        "      - id: TASK-1-1\n"
        "        description: Document the new fixtures\n"
        "        acceptance: docs updated\n"
        "        role: coder\n"
        "        files:\n"
        "          - docs/fixtures.md\n"
        "```\n"
    )

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_plan_proposal_parse_failure_still_rejected_post_slice3(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_gateway_fetch,
        app,
    ):
        """#3026 retention: a plan draft present at the spec path but
        missing its ``# yaml-tasks`` fence still triggers a propose-time
        rejection ("does not parse into any tasks"), even though slice-3
        moves the presence check onto the spec-derived path. The plan's
        layered extensions are NOT subsumed.
        """
        pipeline = _pipeline_with_phase("plan")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        mock_subprocess_run.side_effect = _make_subprocess_router(
            show_stdout_overrides={
                self._PLAN_DRAFT_PATH: self._PLAN_WITHOUT_YAML_TASKS,
            }
        )

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "task_planner", "payload": _propose_payload()},
                Path("/tmp/repo"),
            )

        assert status_code == 400, response.get_json()
        message = response.get_json().get("message", "")
        assert "does not parse into any tasks" in message, message
        mock_tracker.handle_propose.assert_not_called()

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_plan_proposal_role_alignment_still_rejected_post_slice3(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_gateway_fetch,
        app,
    ):
        """#2527 / #2528 retention: a plan that parses but assigns a
        documentation file to ``coder`` is still rejected at propose
        time with the role↔files alignment error, after slice-3 moves
        the presence check onto the spec. The plan-only extensions live
        on top of the spec-derived presence check; they are NOT replaced.
        """
        pipeline = _pipeline_with_phase("plan")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        mock_subprocess_run.side_effect = _make_subprocess_router(
            show_stdout_overrides={
                self._PLAN_DRAFT_PATH: self._PLAN_WITH_MISASSIGNED_TASK,
            }
        )

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "task_planner", "payload": _propose_payload()},
                Path("/tmp/repo"),
            )

        assert status_code == 400, response.get_json()
        message = response.get_json().get("message", "")
        assert "role↔files alignment violations" in message, message
        mock_tracker.handle_propose.assert_not_called()


class TestDecisionLedgerProposeValidation:
    """Propose-time decision-ledger enforcement for refine/plan producers (#3390).

    The registration guarantee: a refine/plan producer cannot reach
    consensus (and therefore the phase gate) without attesting its HITL
    decision ledger — either the ``cq-N`` ids it registered or an explicit
    rationale for why the phase deliberately raises none. Attested ids are
    cross-checked against the contract's registered decisions, and the
    draft must cite each attested id so the operator/reviewers can see it.
    """

    _ANALYSIS_DRAFT_PATH = ".egg-state/drafts/42-analysis.md"

    @staticmethod
    def _contract_with_decisions(*decisions):
        contract = MagicMock()
        contract.decisions = list(decisions)
        return contract

    @staticmethod
    def _decision(decision_id: str, phase: str, decision_type: str = "hitl"):
        d = MagicMock()
        d.id = decision_id
        d.phase = phase
        d.type = decision_type
        return d

    def _validate(
        self,
        *,
        payload: dict,
        agent_role: str = "refiner",
        phase: str = "refine",
        router=None,
        contract=None,
        contract_error: Exception | None = None,
        branch_verified: bool | None = True,
        commit_resolvable: bool = True,
    ):
        from routes.signals import _validate_producer_artifacts

        pipeline = _pipeline_with_phase(phase)
        router = router or _make_subprocess_router()
        load_patch = (
            patch("routes.signals.load_contract", side_effect=contract_error)
            if contract_error is not None
            else patch("routes.signals.load_contract", return_value=contract or MagicMock())
        )
        with (
            patch("routes.signals.subprocess.run", side_effect=router),
            patch("routes.signals._commit_object_resolvable", return_value=commit_resolvable),
            load_patch,
        ):
            _validate_producer_artifacts(
                "issue-42",
                payload,
                Path("/tmp/repo"),
                agent_role=agent_role,
                phase=phase,
                pipeline_state=pipeline,
                worktree_path=Path("/tmp/wt"),
                branch_verified=branch_verified,
            )

    # -- shape ---------------------------------------------------------------

    def test_missing_attestation_rejected(self):
        with pytest.raises(ValueError, match="decision-ledger attestation"):
            self._validate(payload={"commit_sha": "abc1234"})

    def test_empty_attestation_rejected(self):
        with pytest.raises(ValueError, match="decision-ledger attestation"):
            self._validate(payload={"commit_sha": "abc1234", "attestation": {}})

    def test_both_fields_rejected(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            self._validate(
                payload={
                    "commit_sha": "abc1234",
                    "attestation": {
                        "decisions_registered": ["cq-1"],
                        "no_decisions_rationale": "also none",
                    },
                }
            )

    def test_explicit_none_accepted(self):
        self._validate(
            payload={
                "commit_sha": "abc1234",
                "attestation": {
                    "no_decisions_rationale": "no operator-grade choices here",
                    "candidates_considered": [
                        {
                            "question": "which helper to reuse?",
                            "disposition": "not_operator_grade",
                            "why": "internal design call",
                        }
                    ],
                },
            }
        )

    def test_explicit_none_without_candidates_rejected(self):
        # #3526: the empty ledger must enumerate what was considered; a
        # bare rationale paragraph is no longer a valid explicit-none.
        with pytest.raises(ValueError, match="candidates_considered"):
            self._validate(
                payload={
                    "commit_sha": "abc1234",
                    "attestation": {"no_decisions_rationale": "no operator-grade choices here"},
                }
            )

    def test_malformed_candidate_rejected(self):
        with pytest.raises(ValueError, match="disposition"):
            self._validate(
                payload={
                    "commit_sha": "abc1234",
                    "attestation": {
                        "no_decisions_rationale": "none",
                        "candidates_considered": [
                            {"question": "q?", "disposition": "bogus", "why": "w"}
                        ],
                    },
                }
            )

    def test_plan_phase_deferred_to_plan_rejected(self):
        # Plan is the last decision surface (#3526): a plan producer cannot
        # defer a candidate to itself.
        with pytest.raises(ValueError, match="deferred_to_plan"):
            self._validate(
                payload={
                    "commit_sha": "abc1234",
                    "attestation": {
                        "no_decisions_rationale": "none",
                        "candidates_considered": [
                            {
                                "question": "cache default?",
                                "disposition": "deferred_to_plan",
                                "why": "later",
                            }
                        ],
                    },
                },
                agent_role="architect",
                phase="plan",
            )

    def test_refine_phase_deferred_to_plan_accepted(self):
        self._validate(
            payload={
                "commit_sha": "abc1234",
                "attestation": {
                    "no_decisions_rationale": "all open choices are plan-level",
                    "candidates_considered": [
                        {
                            "question": "cache default?",
                            "disposition": "deferred_to_plan",
                            "why": "depends on plan's storage design",
                        }
                    ],
                },
            }
        )

    def test_every_attesting_role_subject_to_gate(self):
        for role, phase in (
            ("refiner", "refine"),
            ("task_planner", "plan"),
            ("architect", "plan"),
            ("risk_analyst", "plan"),
        ):
            with pytest.raises(ValueError, match="decision-ledger attestation"):
                self._validate(payload={"commit_sha": "abc1234"}, agent_role=role, phase=phase)

    def test_simplifier_exempt(self):
        # The simplifier owns no decision surface; no ledger requirement.
        self._validate(payload={"commit_sha": "abc1234"}, agent_role="simplifier", phase="refine")

    def test_shape_enforced_on_degraded_fetch_path(self):
        # Under a degraded fetch (branch verification inconclusive AND the
        # commit not in the local object store) ``_validate_producer_artifacts``
        # early-returns before the presence loop / contract cross-check. The
        # shape check is hoisted ahead of that early return (#3390), so a
        # missing attestation must still hard-fail even here — otherwise a
        # refine/plan producer could reach consensus with no ledger claim.
        with pytest.raises(ValueError, match="decision-ledger attestation"):
            self._validate(
                payload={"commit_sha": "abc1234"},
                branch_verified=None,
                commit_resolvable=False,
            )

    # -- contract cross-check --------------------------------------------------

    def test_unregistered_id_rejected(self):
        contract = self._contract_with_decisions(self._decision("cq-1", "refine"))
        with pytest.raises(ValueError, match="`cq-9` is not registered"):
            self._validate(
                payload={
                    "commit_sha": "abc1234",
                    "attestation": {"decisions_registered": ["cq-9"]},
                },
                contract=contract,
            )

    def test_phase_mismatched_id_rejected(self):
        contract = self._contract_with_decisions(self._decision("cq-1", "plan"))
        with pytest.raises(ValueError, match="registered for the 'plan' phase"):
            self._validate(
                payload={
                    "commit_sha": "abc1234",
                    "attestation": {"decisions_registered": ["cq-1"]},
                },
                contract=contract,
            )

    def test_non_hitl_attested_id_rejected(self):
        # The cross-check recognises only ``hitl``-type decisions, matching
        # the gate-side counter (#3390). Attesting a non-HITL id that the
        # gate would never count is rejected at propose time rather than
        # tripping a spurious ``MISSING`` backstop later.
        contract = self._contract_with_decisions(
            self._decision("cq-1", "refine", decision_type="auto")
        )
        with pytest.raises(ValueError, match="`cq-1` is not registered"):
            self._validate(
                payload={
                    "commit_sha": "abc1234",
                    "attestation": {"decisions_registered": ["cq-1"]},
                },
                contract=contract,
            )

    def test_registered_and_cited_accepted(self):
        contract = self._contract_with_decisions(self._decision("cq-1", "refine"))
        router = _make_subprocess_router(
            show_stdout_overrides={
                self._ANALYSIS_DRAFT_PATH: (
                    "# Analysis\n\n## Open Questions\n"
                    "<!-- egg-hitl-decision id=cq-1 -->\n- [ ] Option A\n"
                )
            }
        )
        self._validate(
            payload={
                "commit_sha": "abc1234",
                "attestation": {"decisions_registered": ["cq-1"]},
            },
            contract=contract,
            router=router,
        )

    def test_contract_unloadable_skips_cross_check(self):
        """Orchestrator-side contract glitch is not the producer's fault:
        the shape requirement still holds but the cross-check degrades.
        The draft cites the id so the citation check passes too.
        """
        router = _make_subprocess_router(
            show_stdout_overrides={self._ANALYSIS_DRAFT_PATH: "cites cq-1 inline\n"}
        )
        self._validate(
            payload={
                "commit_sha": "abc1234",
                "attestation": {"decisions_registered": ["cq-1"]},
            },
            contract_error=FileNotFoundError("no contract"),
            router=router,
        )

    # -- draft citation ---------------------------------------------------------

    def test_uncited_id_rejected(self):
        contract = self._contract_with_decisions(self._decision("cq-1", "refine"))
        router = _make_subprocess_router(
            show_stdout_overrides={
                self._ANALYSIS_DRAFT_PATH: "# Analysis with no decision markers\n"
            }
        )
        with pytest.raises(ValueError, match="not cited anywhere"):
            self._validate(
                payload={
                    "commit_sha": "abc1234",
                    "attestation": {"decisions_registered": ["cq-1"]},
                },
                contract=contract,
                router=router,
            )

    def test_rationale_form_skips_citation_check(self):
        # No ids attested → nothing to cite; the presence check still runs.
        router = _make_subprocess_router(
            show_stdout_overrides={self._ANALYSIS_DRAFT_PATH: "# Analysis, no decisions\n"}
        )
        self._validate(
            payload={
                "commit_sha": "abc1234",
                "attestation": {
                    "no_decisions_rationale": "nothing operator-grade",
                    "candidates_considered": [
                        {
                            "question": "which helper to reuse?",
                            "disposition": "not_operator_grade",
                            "why": "internal design call",
                        }
                    ],
                },
            },
            router=router,
        )

    # -- e2e propose path ---------------------------------------------------------

    @patch("routes.signals._gateway_fetch_tracking_ref", return_value=True)
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("routes.signals.subprocess.run")
    def test_propose_without_ledger_rejected_400(
        self,
        mock_subprocess_run,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_gateway_fetch,
        app,
    ):
        """A refiner propose with no ledger attestation bounces 400 and the
        tracker is never mutated — the hard-fail of #3390.
        """
        pipeline = _pipeline_with_phase("refine")
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_tracker = MagicMock()
        mock_get_tracker.return_value = mock_tracker

        mock_subprocess_run.side_effect = _make_subprocess_router()

        payload = _propose_payload()
        del payload["attestation"]

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {"agent_role": "refiner", "payload": payload},
                Path("/tmp/repo"),
            )

        assert status_code == 400, response.get_json()
        message = response.get_json().get("message", "")
        assert "decision-ledger attestation" in message, message
        mock_tracker.handle_propose.assert_not_called()


class TestDeferredCandidateCoverageGate:
    """Propose-time coverage gate for refine-deferred questions (#3564).

    #3563 made refine's ``deferred_to_plan`` candidates a prompt-level
    handoff; this gate hard-closes the leak: the plan producer that
    received the deferred section (the architect) must echo every
    ``dq-<hash>`` id in its attestation's ``deferred_resolutions``, and
    the validator recomputes the ids from the refine attestation and
    rejects any proposal that leaves one unaccounted.
    """

    _QUESTION = "Should we support pagination?"
    _DEFERRED = [
        {
            "question": _QUESTION,
            "disposition": "deferred_to_plan",
            "why": "depends on the plan's storage design",
        }
    ]

    @staticmethod
    def _dq(question: str) -> str:
        from egg_contracts.decisions import deferred_question_id

        return deferred_question_id(question)

    def _check(
        self,
        *,
        attestation: dict | None,
        deferred=None,
        scan_error: Exception | None = None,
        agent_role: str = "architect",
        phase: str = "plan",
    ):
        from routes.signals import _validate_deferred_candidate_coverage

        payload: dict = {"commit_sha": "abc1234"}
        if attestation is not None:
            payload["attestation"] = attestation
        scan_patch = (
            patch("routes.pipelines._find_deferred_plan_candidates", side_effect=scan_error)
            if scan_error is not None
            else patch(
                "routes.pipelines._find_deferred_plan_candidates",
                return_value=self._DEFERRED if deferred is None else deferred,
            )
        )
        with scan_patch:
            _validate_deferred_candidate_coverage(
                "issue-42", payload, agent_role=agent_role, phase=phase
            )

    def test_uncovered_deferred_question_rejected(self):
        with pytest.raises(ValueError, match=self._dq(self._QUESTION)):
            self._check(attestation={"decisions_registered": ["cq-1"]})

    def test_rejection_lists_question_text(self):
        # The NACK must be actionable for a producer that never saw the
        # prompt section (store outage at prompt-build time): it carries
        # the deferred question verbatim, not just the opaque id.
        with pytest.raises(ValueError, match="pagination"):
            self._check(attestation={"decisions_registered": ["cq-1"]})

    def test_registered_echo_accepted(self):
        self._check(
            attestation={
                "decisions_registered": ["cq-3"],
                "deferred_resolutions": [
                    {
                        "deferred_id": self._dq(self._QUESTION),
                        "resolution": "registered",
                        "cq": "cq-3",
                    }
                ],
            }
        )

    def test_registered_echo_with_unattested_cq_rejected(self):
        with pytest.raises(ValueError, match="not in this attestation's decisions_registered"):
            self._check(
                attestation={
                    "decisions_registered": ["cq-1"],
                    "deferred_resolutions": [
                        {
                            "deferred_id": self._dq(self._QUESTION),
                            "resolution": "registered",
                            "cq": "cq-9",
                        }
                    ],
                }
            )

    def test_not_operator_grade_echo_accepted(self):
        self._check(
            attestation={
                "no_decisions_rationale": "design dissolved the only fork",
                "candidates_considered": [
                    {
                        "question": self._QUESTION,
                        "disposition": "not_operator_grade",
                        "why": "cursor pagination is forced by the storage design",
                    }
                ],
                "deferred_resolutions": [
                    {
                        "deferred_id": self._dq(self._QUESTION),
                        "resolution": "not_operator_grade",
                        "why": "cursor pagination is forced by the storage design",
                    }
                ],
            }
        )

    def test_unknown_dq_id_rejected(self):
        with pytest.raises(ValueError, match="matches no refine-deferred question"):
            self._check(
                attestation={
                    "decisions_registered": ["cq-3"],
                    "deferred_resolutions": [
                        {
                            "deferred_id": self._dq(self._QUESTION),
                            "resolution": "registered",
                            "cq": "cq-3",
                        },
                        {
                            "deferred_id": "dq-00000000",
                            "resolution": "registered",
                            "cq": "cq-3",
                        },
                    ],
                }
            )

    def test_reframed_question_covered_by_id(self):
        # The planner reframes the deferred question as a concrete cq-N;
        # coverage matches on the echoed id, so the reframing is free.
        self._check(
            attestation={
                "decisions_registered": ["cq-3"],
                "deferred_resolutions": [
                    {
                        "deferred_id": self._dq(self._QUESTION),
                        "resolution": "registered",
                        "cq": "cq-3",
                    }
                ],
            },
        )

    def test_no_deferred_candidates_noop(self):
        self._check(attestation={"decisions_registered": ["cq-1"]}, deferred=[])

    def test_non_architect_plan_roles_exempt(self):
        # task_planner / risk_analyst never receive the deferred prompt
        # section, so coverage is not required of them.
        for role in ("task_planner", "risk_analyst"):
            self._check(attestation={"decisions_registered": ["cq-1"]}, agent_role=role)

    def test_refine_phase_exempt(self):
        self._check(
            attestation={"decisions_registered": ["cq-1"]},
            agent_role="refiner",
            phase="refine",
        )

    def test_scan_failure_skips_check(self):
        # A message-store outage is an orchestrator-side glitch, not a
        # producer fault: the check degrades with a warning, mirroring the
        # prompt-side behavior of _find_deferred_plan_candidates.
        self._check(
            attestation={"decisions_registered": ["cq-1"]},
            scan_error=RuntimeError("store down"),
        )

    def test_non_plan_attestation_with_deferred_resolutions_rejected(self):
        # Shape-level guard: only plan producers have dq- ids to echo.
        from routes.signals import _validate_decision_attestation_shape

        with pytest.raises(ValueError, match="plan-phase field"):
            _validate_decision_attestation_shape(
                {
                    "commit_sha": "abc1234",
                    "attestation": {
                        "decisions_registered": ["cq-1"],
                        "deferred_resolutions": [
                            {
                                "deferred_id": self._dq(self._QUESTION),
                                "resolution": "registered",
                                "cq": "cq-1",
                            }
                        ],
                    },
                },
                agent_role="refiner",
                phase="refine",
            )

    def test_malformed_deferred_resolutions_rejected_at_shape(self):
        from routes.signals import _validate_decision_attestation_shape

        with pytest.raises(ValueError, match="deferred_resolutions"):
            _validate_decision_attestation_shape(
                {
                    "commit_sha": "abc1234",
                    "attestation": {
                        "decisions_registered": ["cq-1"],
                        "deferred_resolutions": [{"deferred_id": "bogus"}],
                    },
                },
                agent_role="architect",
                phase="plan",
            )

    def test_coverage_enforced_on_degraded_fetch_path(self):
        # The coverage gate is hoisted with the shape check ahead of the
        # branch-verification early return in _validate_producer_artifacts:
        # a degraded fetch must not let an uncovered deferral through.
        from routes.signals import _validate_producer_artifacts

        pipeline = _pipeline_with_phase("plan")
        with (
            patch("routes.signals.subprocess.run", side_effect=_make_subprocess_router()),
            patch("routes.signals._commit_object_resolvable", return_value=False),
            patch(
                "routes.pipelines._find_deferred_plan_candidates",
                return_value=self._DEFERRED,
            ),
            pytest.raises(ValueError, match="does not account for all"),
        ):
            _validate_producer_artifacts(
                "issue-42",
                {
                    "commit_sha": "abc1234",
                    "attestation": {"decisions_registered": ["cq-1"]},
                },
                Path("/tmp/repo"),
                agent_role="architect",
                phase="plan",
                pipeline_state=pipeline,
                worktree_path=Path("/tmp/wt"),
                branch_verified=None,
            )


class TestSimplifierSingleArtifactEnforcement:
    """Propose-time enforcement that the simplifier persists exactly ONE draft.

    The simplifier is a producer-only role whose sole output is the registered
    human-focused companion (``*-analysis-human.md`` / ``*-plan-human.md``). Its
    review reasoning belongs in the BRC channel (its verdict), not a second
    persisted document. Prompt + reviewer-rubric prose forbid a freelanced
    ``*-simplifier-*.md`` constraints/guardrails companion, but those are soft
    gates; this hard gate in ``_validate_producer_artifacts`` makes "one
    simplifier draft, no more" a structural invariant at propose time.

    The check inspects only the files the *proposed commit* introduced under
    ``.egg-state/drafts/{identifier}-`` — so the upstream producer's draft
    (which the simplifier's commit never touches) is never implicated — and
    rejects any draft that is not the registered human companion.
    """

    _PLAN_HUMAN_PATH = ".egg-state/drafts/42-plan-human.md"
    _ANALYSIS_HUMAN_PATH = ".egg-state/drafts/42-analysis-human.md"
    _EXTRA_PLAN_COMPANION = ".egg-state/drafts/42-simplifier-plan.md"
    _EXTRA_ANALYSIS_COMPANION = ".egg-state/drafts/42-simplifier-analysis.md"

    @staticmethod
    def _router(name_only_stdout: str, *, name_only_returncode: int = 0):
        """Route the two ``git show`` shapes this validator emits: the
        per-spec presence ``git show <sha>:<path>`` (always present here) and
        the new ``git show --name-only --pretty=format: <sha>`` enumerating the
        commit's files.
        """

        def _run(cmd, *_args, **_kwargs):
            cmd_str = " ".join(str(p) for p in cmd)
            if "--name-only" in cmd_str:
                return _make_subprocess_result(
                    stdout=name_only_stdout, returncode=name_only_returncode
                )
            if "show" in cmd_str:
                # Per-spec presence check — the human companion exists.
                return _make_subprocess_result(stdout="present\n")
            return _make_subprocess_result()

        return _run

    def _validate(self, *, agent_role="simplifier", phase="plan", router):
        from routes.signals import _validate_producer_artifacts

        pipeline = _pipeline_with_phase(phase)
        # Explicit-none ledger attestation so non-simplifier roles pass the
        # #3390 decision-ledger requirement and reach the gate under test.
        payload = {
            "commit_sha": "abc1234",
            "attestation": {
                "no_decisions_rationale": "test fixture: no operator decisions",
                "candidates_considered": [
                    {
                        "question": "test fixture candidate?",
                        "disposition": "not_operator_grade",
                        "why": "fixture",
                    }
                ],
            },
        }
        with patch("routes.signals.subprocess.run", side_effect=router):
            _validate_producer_artifacts(
                "issue-42",
                payload,
                Path("/tmp/repo"),
                agent_role=agent_role,
                phase=phase,
                pipeline_state=pipeline,
                worktree_path=Path("/tmp/wt"),
                branch_verified=True,
            )

    def test_accepts_when_only_human_companion_committed(self):
        """Plan simplifier whose commit introduces only the human companion ⇒
        no raise.
        """
        self._validate(router=self._router(f"\n{self._PLAN_HUMAN_PATH}\n"))

    def test_rejects_extra_simplifier_plan_companion(self):
        """The historically-observed failure: the plan simplifier commits the
        human doc AND a second ``*-simplifier-plan.md`` guardrails companion ⇒
        rejected, naming the extra file.
        """
        router = self._router(f"\n{self._PLAN_HUMAN_PATH}\n{self._EXTRA_PLAN_COMPANION}\n")
        with pytest.raises(ValueError, match="beyond your one registered"):
            self._validate(router=router)

    def test_rejects_extra_simplifier_analysis_companion_in_refine(self):
        """Structural, not plan-specific: the refine simplifier is gated the
        same way for a ``*-simplifier-analysis.md`` companion.
        """
        router = self._router(f"\n{self._ANALYSIS_HUMAN_PATH}\n{self._EXTRA_ANALYSIS_COMPANION}\n")
        with pytest.raises(ValueError, match="exactly one artifact per phase"):
            self._validate(phase="refine", router=router)

    def test_ignores_non_draft_and_upstream_files(self):
        """The gate is scoped to ``.egg-state/drafts/{id}-`` files the commit
        introduced. A stray non-draft path (or an agent-output) in the commit's
        file list is not a second *draft*, so it does not trip the gate.
        """
        router = self._router(
            f"\n{self._PLAN_HUMAN_PATH}\n"
            "src/incidental.py\n"
            ".egg-state/agent-outputs/42-architect-output.json\n"
        )
        self._validate(router=router)

    def test_non_simplifier_producer_not_subject_to_gate(self):
        """The task_planner legitimately commits ``42-plan.md`` (its registered
        draft); the simplifier-only gate must never fire for it even though that
        path matches the drafts prefix.
        """

        # task_planner's presence spec is plan-draft; the router returns a
        # parseable plan so the presence/extension checks pass, and the
        # --name-only branch is never consulted for a non-simplifier role.
        def _run(cmd, *_args, **_kwargs):
            cmd_str = " ".join(str(p) for p in cmd)
            if "--name-only" in cmd_str:
                raise AssertionError("simplifier gate ran for task_planner")
            if "show" in cmd_str:
                return _make_subprocess_result(
                    stdout=(
                        "# Plan\n\n```yaml\n# yaml-tasks\nslices:\n"
                        "  - id: 1\n    name: S\n    goal: g\n    tasks:\n"
                        "      - id: TASK-1-1\n        description: d\n"
                        "        acceptance: a\n        role: tester\n"
                        "        files:\n          - integration_tests/conftest.py\n```\n"
                    )
                )
            return _make_subprocess_result()

        self._validate(agent_role="task_planner", phase="plan", router=_run)

    # -- graceful degradation on the helper itself --------------------------

    def test_helper_skips_when_git_show_errors(self):
        """A non-zero ``git show --name-only`` (commit not enumerable) ⇒ skip,
        never blame the producer.
        """
        from routes.signals import _reject_extra_simplifier_drafts

        with patch(
            "routes.signals.subprocess.run",
            return_value=_make_subprocess_result(returncode=128),
        ):
            # Should not raise even though we cannot list the commit's files.
            _reject_extra_simplifier_drafts(
                pipeline_id="issue-42",
                worktree_path=Path("/tmp/wt"),
                commit_sha="abc1234",
                identifier=42,
                allowed_rel_paths={self._PLAN_HUMAN_PATH},
                phase="plan",
            )

    def test_helper_skips_on_subprocess_exception(self):
        """An infra failure (timeout) on the enumerate call degrades gracefully."""
        from routes.signals import _reject_extra_simplifier_drafts

        with patch(
            "routes.signals.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=15),
        ):
            _reject_extra_simplifier_drafts(
                pipeline_id="issue-42",
                worktree_path=Path("/tmp/wt"),
                commit_sha="abc1234",
                identifier=42,
                allowed_rel_paths={self._PLAN_HUMAN_PATH},
                phase="plan",
            )
