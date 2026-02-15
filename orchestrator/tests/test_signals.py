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
