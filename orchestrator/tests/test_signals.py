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
                        "summary": "impl",
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
                        "summary": "impl",
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
                        "summary": "impl",
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
                            "summary": "impl",
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
