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
    """Unit tests for _verify_commit_on_branch helper."""

    @patch("routes.signals.subprocess.run")
    def test_returns_none_when_branch_contains_fails(self, mock_run):
        """branch --contains failure returns None (non-blocking)."""
        from routes.signals import _verify_commit_on_branch

        mock_run.side_effect = [
            _make_subprocess_result(returncode=0),  # fetch ok
            _make_subprocess_result(
                returncode=128, stderr="not a valid commit"
            ),  # branch --contains fails
        ]
        result = _verify_commit_on_branch("abc123", "egg/issue-42", Path("/tmp/wt"), "pipe-1")
        assert result is None

    @patch("routes.signals.subprocess.run")
    def test_returns_none_on_unexpected_exception(self, mock_run):
        """Unexpected exception returns None (non-blocking)."""
        from routes.signals import _verify_commit_on_branch

        mock_run.side_effect = OSError("disk error")
        result = _verify_commit_on_branch("abc123", "egg/issue-42", Path("/tmp/wt"), "pipe-1")
        assert result is None

    @patch("routes.signals.subprocess.run")
    def test_returns_true_when_multiple_branches_include_expected(self, mock_run):
        """Returns True when expected branch is among multiple branches."""
        from routes.signals import _verify_commit_on_branch

        mock_run.side_effect = [
            _make_subprocess_result(returncode=0),  # fetch ok
            _make_subprocess_result(stdout="  origin/egg/issue-42\n  origin/main\n"),
        ]
        result = _verify_commit_on_branch("abc123", "egg/issue-42", Path("/tmp/wt"), "pipe-1")
        assert result is True

    @patch("routes.signals.subprocess.run")
    def test_returns_false_when_branch_not_in_output(self, mock_run):
        """Returns False when commit exists but not on expected branch."""
        from routes.signals import _verify_commit_on_branch

        mock_run.side_effect = [
            _make_subprocess_result(returncode=0),  # fetch ok
            _make_subprocess_result(stdout="  origin/egg/other-branch\n"),
        ]
        result = _verify_commit_on_branch("abc123", "egg/issue-42", Path("/tmp/wt"), "pipe-1")
        assert result is False

    @patch("routes.signals.subprocess.run")
    def test_returns_false_on_empty_branch_output(self, mock_run):
        """Returns False when branch --contains returns empty output."""
        from routes.signals import _verify_commit_on_branch

        mock_run.side_effect = [
            _make_subprocess_result(returncode=0),  # fetch ok
            _make_subprocess_result(stdout=""),  # empty - commit on no branches
        ]
        result = _verify_commit_on_branch("abc123", "egg/issue-42", Path("/tmp/wt"), "pipe-1")
        assert result is False

    @patch("routes.signals.subprocess.run")
    def test_returns_none_on_fetch_timeout(self, mock_run):
        """Fetch timeout returns None (non-blocking)."""
        from routes.signals import _verify_commit_on_branch

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)
        result = _verify_commit_on_branch("abc123", "egg/issue-42", Path("/tmp/wt"), "pipe-1")
        assert result is None


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

        # subprocess.run calls: fetch succeeds, branch --contains returns origin/egg/issue-42
        # (no rev-parse for progress check — mock_pipeline has no phase_start_sha)
        mock_subprocess_run.side_effect = [
            _make_subprocess_result(returncode=0),  # fetch
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

    @patch("routes.signals.subprocess.run")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    def test_commit_not_on_correct_branch_rejected_409(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        app,
        mock_pipeline,
    ):
        """(b) completion with commit NOT on correct branch -> 409 rejected."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        # fetch succeeds, but branch --contains shows different branch
        mock_subprocess_run.side_effect = [
            _make_subprocess_result(returncode=0),  # fetch
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

        # fetch succeeds, branch --contains returns different branch
        mock_subprocess_run.side_effect = [
            _make_subprocess_result(returncode=0),  # fetch
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

        # fetch succeeds, branch --contains returns correct branch
        mock_subprocess_run.side_effect = [
            _make_subprocess_result(returncode=0),  # fetch
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
        "no slice scope" so babysit_pr et al. continue to land in the
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
        "no slice scope" and falls back to the aggregate filename
        (babysit_pr et al.)."""
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
