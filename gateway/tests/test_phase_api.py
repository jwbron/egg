"""
Tests for Phase API endpoints.

Tests cover:
- POST /api/v1/phase/advance - Advance to next phase
- POST /api/v1/phase/filter - Check if operation is allowed
- GET /api/v1/phase/current/<issue_number> - Get current phase
- GET /api/v1/phase/permissions/<phase> - Get phase permissions
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import auth
import phase_api
import pytest
import session_manager
from session_manager import SessionValidationResult

import gateway


@pytest.fixture
def client():
    """Create test client for Flask app."""
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


@pytest.fixture
def auth_headers():
    """Return valid session authentication headers with mocked session validation."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = "human"  # Default to human for most tests

    mock_result = SessionValidationResult(valid=True, session=mock_session)

    from private_repo_policy import PrivateRepoPolicyResult

    mock_policy_result = PrivateRepoPolicyResult(
        allowed=True,
        reason="Test mode - access allowed",
        visibility="public",
    )

    # Clear auth module's cached references
    auth._session_manager = None
    auth._rate_limiter = None

    if "gateway.auth" in sys.modules:
        sys.modules["gateway.auth"]._session_manager = None
        sys.modules["gateway.auth"]._rate_limiter = None

    current_session_manager = sys.modules.get("session_manager", session_manager)

    with (
        patch.object(
            current_session_manager, "validate_session_for_request", return_value=mock_result
        ),
        patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
    ):
        yield {"Authorization": "Bearer test-session-token"}


@pytest.fixture
def mock_contract():
    """Create a mock contract."""
    from egg_contracts.models import Contract, IssueInfo, PipelinePhase

    return Contract(
        schemaVersion="1.0",
        issue=IssueInfo(
            number=123,
            title="Test Issue",
            url="https://github.com/test/repo/issues/123",
        ),
        current_phase=PipelinePhase.REFINE,
    )


@pytest.fixture
def mock_contract_implement():
    """Create a mock contract in implement phase."""
    from egg_contracts.models import Contract, IssueInfo, PipelinePhase

    return Contract(
        schemaVersion="1.0",
        issue=IssueInfo(
            number=123,
            title="Test Issue",
            url="https://github.com/test/repo/issues/123",
        ),
        current_phase=PipelinePhase.IMPLEMENT,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/phase/current/<issue_number> tests
# ---------------------------------------------------------------------------


class TestGetCurrentPhase:
    """Tests for GET /api/v1/phase/current/<issue_number>."""

    def test_get_current_phase_success(self, client, auth_headers, mock_contract):
        """Get current phase for an issue."""
        with patch("phase_api.load_contract", return_value=mock_contract):
            response = client.get(
                "/api/v1/phase/current/123",
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["data"]["phase"] == "refine"
        assert data["data"]["exit_requires"] == "human"
        assert data["data"]["next_phase"] == "plan"

    def test_get_current_phase_not_found(self, client, auth_headers):
        """Get current phase for non-existent contract."""
        from egg_contracts import ContractNotFoundError

        with patch(
            "phase_api.load_contract",
            side_effect=ContractNotFoundError(123, Path(".")),
        ):
            response = client.get(
                "/api/v1/phase/current/123",
                headers=auth_headers,
            )

        assert response.status_code == 404
        data = response.get_json()
        assert data["success"] is False


# ---------------------------------------------------------------------------
# GET /api/v1/phase/permissions/<phase> tests
# ---------------------------------------------------------------------------


class TestGetPhasePermissions:
    """Tests for GET /api/v1/phase/permissions/<phase>."""

    def test_get_permissions_refine(self, client, auth_headers):
        """Get permissions for refine phase."""
        response = client.get(
            "/api/v1/phase/permissions/refine",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["data"]["phase"] == "refine"
        assert data["data"]["exit_requires"] == "human"
        assert len(data["data"]["blocked_operations"]) > 0

    def test_get_permissions_invalid_phase(self, client, auth_headers):
        """Get permissions for invalid phase."""
        response = client.get(
            "/api/v1/phase/permissions/invalid_phase",
            headers=auth_headers,
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "valid_phases" in data.get("details", {})


# ---------------------------------------------------------------------------
# POST /api/v1/phase/filter tests
# ---------------------------------------------------------------------------


class TestFilterPhaseOperation:
    """Tests for POST /api/v1/phase/filter."""

    def test_filter_allowed_operation(self, client, auth_headers, mock_contract_implement):
        """Filter an allowed operation."""
        with patch("phase_api.load_contract", return_value=mock_contract_implement):
            response = client.post(
                "/api/v1/phase/filter",
                headers=auth_headers,
                json={
                    "issue_number": 123,
                    "operation_type": "git",
                    "command": "push origin main",
                },
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["data"]["allowed"] is True

    def test_filter_blocked_operation(self, client, auth_headers, mock_contract):
        """Filter a blocked operation."""
        with patch("phase_api.load_contract", return_value=mock_contract):
            response = client.post(
                "/api/v1/phase/filter",
                headers=auth_headers,
                json={
                    "issue_number": 123,
                    "operation_type": "git",
                    "command": "push origin main",
                },
            )

        assert response.status_code == 403
        data = response.get_json()
        assert data["success"] is False
        assert data["details"]["allowed"] is False

    def test_filter_missing_fields(self, client, auth_headers):
        """Filter with missing fields."""
        response = client.post(
            "/api/v1/phase/filter",
            headers=auth_headers,
            json={"issue_number": 123},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "Missing" in data["message"]

    def test_filter_invalid_operation_type(self, client, auth_headers, mock_contract):
        """Filter with invalid operation type."""
        with patch("phase_api.load_contract", return_value=mock_contract):
            response = client.post(
                "/api/v1/phase/filter",
                headers=auth_headers,
                json={
                    "issue_number": 123,
                    "operation_type": "invalid",
                    "command": "something",
                },
            )

        assert response.status_code == 400
        data = response.get_json()
        assert "valid_types" in data.get("details", {})


# ---------------------------------------------------------------------------
# POST /api/v1/phase/advance tests
# ---------------------------------------------------------------------------


class TestAdvancePhase:
    """Tests for POST /api/v1/phase/advance."""

    def test_advance_phase_success(self, client, auth_headers, mock_contract):
        """Advance phase with proper authorization."""
        mock_mutation_result = MagicMock()
        mock_mutation_result.success = True
        mock_mutation_result.contract = mock_contract

        with (
            patch("phase_api.load_contract", return_value=mock_contract),
            patch("phase_api.apply_mutation", return_value=mock_mutation_result),
            patch("phase_api.save_contract"),
        ):
            response = client.post(
                "/api/v1/phase/advance",
                headers=auth_headers,
                json={
                    "issue_number": 123,
                    "reason": "Analysis complete",
                },
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["data"]["from_phase"] == "refine"
        assert data["data"]["to_phase"] == "plan"

    def test_advance_phase_unauthorized(self, client, mock_contract):
        """Advance phase without proper role."""
        # Create a session with implementer role
        mock_session = MagicMock()
        mock_session.mode = "public"
        mock_session.container_id = "test-container"
        mock_session.expires_at = None
        mock_session.agent_role = "implementer"

        mock_result = SessionValidationResult(valid=True, session=mock_session)

        from private_repo_policy import PrivateRepoPolicyResult

        mock_policy_result = PrivateRepoPolicyResult(
            allowed=True,
            reason="Test mode",
            visibility="public",
        )

        auth._session_manager = None
        auth._rate_limiter = None

        current_session_manager = sys.modules.get("session_manager", session_manager)

        with (
            patch.object(
                current_session_manager,
                "validate_session_for_request",
                return_value=mock_result,
            ),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch("phase_api.load_contract", return_value=mock_contract),
        ):
            response = client.post(
                "/api/v1/phase/advance",
                headers={"Authorization": "Bearer test-token"},
                json={"issue_number": 123},
            )

        assert response.status_code == 403
        data = response.get_json()
        assert data["success"] is False
        assert "cannot exit" in data["message"].lower() or "denied" in data["message"].lower()

    def test_advance_phase_terminal_state(self, client, auth_headers):
        """Cannot advance from PR phase (terminal)."""
        from egg_contracts.models import Contract, IssueInfo, PipelinePhase

        terminal_contract = Contract(
            schemaVersion="1.0",
            issue=IssueInfo(
                number=123,
                title="Test Issue",
                url="https://github.com/test/repo/issues/123",
            ),
            current_phase=PipelinePhase.PR,
        )

        with patch("phase_api.load_contract", return_value=terminal_contract):
            response = client.post(
                "/api/v1/phase/advance",
                headers=auth_headers,
                json={"issue_number": 123},
            )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "terminal" in data["message"].lower()

    def test_advance_phase_missing_issue(self, client, auth_headers):
        """Advance phase without issue number."""
        response = client.post(
            "/api/v1/phase/advance",
            headers=auth_headers,
            json={"reason": "test"},  # Provide some data but no issue_number
        )

        assert response.status_code == 400
        data = response.get_json()
        assert "Missing issue_number" in data["message"]

    def test_advance_phase_contract_not_found(self, client, auth_headers):
        """Advance phase for non-existent contract."""
        from egg_contracts import ContractNotFoundError

        with patch(
            "phase_api.load_contract",
            side_effect=ContractNotFoundError(123, Path(".")),
        ):
            response = client.post(
                "/api/v1/phase/advance",
                headers=auth_headers,
                json={"issue_number": 123},
            )

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Role resolution tests
# ---------------------------------------------------------------------------


class TestGetRoleFromContext:
    """Tests for get_role_from_context in phase_api."""

    def test_role_from_session(self, client, auth_headers):
        """Role resolved from session."""
        # auth_headers fixture sets agent_role to "human"
        with client.application.test_request_context():
            from flask import g

            mock_session = MagicMock()
            mock_session.agent_role = "reviewer"
            g.session = mock_session

            role = phase_api.get_role_from_context()

        assert role is not None
        assert role.value == "reviewer"

    def test_role_from_header_when_enabled(self, client, auth_headers):
        """Role resolved from header when enabled."""
        with (
            client.application.test_request_context(headers={"X-Egg-Role": "implementer"}),
            patch.dict(os.environ, {"EGG_ENABLE_TEST_ROLE_HEADER": "1"}, clear=False),
        ):
            from flask import g

            g.session = None
            role = phase_api.get_role_from_context()

        assert role is not None
        assert role.value == "implementer"

    def test_role_from_env(self, client, auth_headers):
        """Role resolved from environment variable."""
        env = os.environ.copy()
        env.pop("EGG_ENABLE_TEST_ROLE_HEADER", None)
        env["EGG_AGENT_ROLE"] = "reviewer"

        with (
            client.application.test_request_context(),
            patch.dict(os.environ, env, clear=True),
        ):
            from flask import g

            g.session = None
            role = phase_api.get_role_from_context()

        assert role is not None
        assert role.value == "reviewer"

    def test_invalid_role_returns_none(self, client, auth_headers):
        """Invalid role returns None."""
        env = os.environ.copy()
        env.pop("EGG_ENABLE_TEST_ROLE_HEADER", None)
        env["EGG_AGENT_ROLE"] = "invalid_role"

        with (
            client.application.test_request_context(),
            patch.dict(os.environ, env, clear=True),
        ):
            from flask import g

            g.session = None
            role = phase_api.get_role_from_context()

        assert role is None
