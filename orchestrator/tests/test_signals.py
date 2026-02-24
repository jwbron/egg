"""
Tests for signal handler contract-role guards.

Verifies that handle_complete_signal and handle_error_signal skip
contract dispatcher interaction for non-contract roles (e.g. REFINER)
and interact with the dispatcher for contract-mapped roles (e.g. CODER).
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


class TestCompleteSignalNonContractRole:
    """handle_complete_signal with a non-contract role skips dispatcher."""

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.create_dispatcher")
    def test_refiner_skips_dispatcher(
        self,
        mock_create_dispatcher,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        """REFINER role should not create or interact with dispatcher."""
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
        mock_create_dispatcher.assert_not_called()

    @patch("routes.signals.save_agent_output")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.create_dispatcher")
    def test_coder_uses_dispatcher(
        self,
        mock_create_dispatcher,
        mock_get_store,
        mock_resolve_wt,
        mock_save_output,
        app,
        mock_pipeline,
    ):
        """CODER role should create dispatcher and record completion."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_dispatcher = MagicMock()
        mock_dispatcher.is_complete.return_value = False
        mock_create_dispatcher.return_value = mock_dispatcher

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
        mock_create_dispatcher.assert_called_once()
        mock_dispatcher.complete_agent.assert_called_once()
        mock_dispatcher.save_contract.assert_called_once()


class TestErrorSignalNonContractRole:
    """handle_error_signal with a non-contract role skips dispatcher."""

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.create_dispatcher")
    def test_refiner_skips_dispatcher(
        self,
        mock_create_dispatcher,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        """REFINER error signal should not create or interact with dispatcher."""
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
        mock_create_dispatcher.assert_not_called()

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.create_dispatcher")
    def test_coder_uses_dispatcher(
        self,
        mock_create_dispatcher,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        """CODER error signal should create dispatcher and record failure."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_dispatcher = MagicMock()
        mock_create_dispatcher.return_value = mock_dispatcher

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
        mock_create_dispatcher.assert_called_once()
        mock_dispatcher.fail_agent.assert_called_once()
        mock_dispatcher.save_contract.assert_called_once()


class TestErrorSignalContractNotFound:
    """handle_error_signal returns 200 when contract is missing."""

    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.create_dispatcher")
    def test_contract_not_found_returns_200(
        self,
        mock_create_dispatcher,
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

        mock_create_dispatcher.side_effect = ContractNotFoundError(42, Path("/tmp/worktree"))

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
# Completion signal branch verification tests (TASK-5-3)
# ---------------------------------------------------------------------------

import subprocess


def _make_subprocess_result(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestCompletionBranchVerification:
    """Verify commit location when agent signals completion with a commit SHA."""

    @patch("routes.signals.subprocess.run")
    @patch("routes.signals.resolve_worktree_path")
    @patch("routes.signals.get_state_store")
    @patch("routes.signals.create_dispatcher")
    def test_commit_on_correct_branch_accepted(
        self,
        mock_create_dispatcher,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        app,
        mock_pipeline,
    ):
        """(a) completion with commit on correct branch → accepted."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_dispatcher = MagicMock()
        mock_dispatcher.is_complete.return_value = False
        mock_create_dispatcher.return_value = mock_dispatcher

        # subprocess.run calls: fetch succeeds, branch --contains returns origin/egg/issue-42
        mock_subprocess_run.side_effect = [
            _make_subprocess_result(returncode=0),  # fetch
            _make_subprocess_result(stdout="  origin/egg/issue-42\n"),  # branch --contains
            _make_subprocess_result(stdout="abc1234def\n"),  # rev-parse for progress check
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
        """(b) completion with commit NOT on correct branch → 409 rejected."""
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
    @patch("routes.signals.create_dispatcher")
    def test_commit_none_accepted_without_check(
        self,
        mock_create_dispatcher,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        """(c) completion with commit=None → accepted without check."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_dispatcher = MagicMock()
        mock_dispatcher.is_complete.return_value = True
        mock_create_dispatcher.return_value = mock_dispatcher

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
    @patch("routes.signals.create_dispatcher")
    def test_branch_fetch_fails_accepted_with_warning(
        self,
        mock_create_dispatcher,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        app,
        mock_pipeline,
    ):
        """(d) branch fetch fails → signal accepted with warning."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_resolve_wt.return_value = Path("/tmp/worktree")

        mock_dispatcher = MagicMock()
        mock_dispatcher.is_complete.return_value = False
        mock_create_dispatcher.return_value = mock_dispatcher

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
    @patch("routes.signals.create_dispatcher")
    def test_no_new_commits_warns_but_accepts(
        self,
        mock_create_dispatcher,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        app,
    ):
        """(e) no new commits since phase start → warning logged but accepted."""
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

        mock_dispatcher = MagicMock()
        mock_dispatcher.is_complete.return_value = False
        mock_create_dispatcher.return_value = mock_dispatcher

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
