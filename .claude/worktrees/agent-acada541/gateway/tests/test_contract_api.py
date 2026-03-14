"""
Tests for Contract API endpoints.

Tests cover:
- get_role_from_context() role resolution
- GET /api/v1/contract/<issue_number> - Get contract state
- GET /api/v1/contract/exists/<issue_number> - Check contract existence
- POST /api/v1/contract/validate - Validate mutation
- POST /api/v1/contract/mutate - Apply mutation
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import auth
import contract_api
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
    """Return valid session authentication headers with mocked session validation.

    Note: We patch sys.modules entries directly to handle cases where other tests
    may have loaded different module instances into sys.modules.
    """
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None

    mock_result = SessionValidationResult(valid=True, session=mock_session)

    from private_repo_policy import PrivateRepoPolicyResult

    mock_policy_result = PrivateRepoPolicyResult(
        allowed=True,
        reason="Test mode - access allowed",
        visibility="public",
    )

    # Clear auth module's cached references so it picks up our patched module
    auth._session_manager = None
    auth._rate_limiter = None

    # Also clear any package-style cached references
    if "gateway.auth" in sys.modules:
        sys.modules["gateway.auth"]._session_manager = None
        sys.modules["gateway.auth"]._rate_limiter = None

    # Patch the module that's currently in sys.modules
    current_session_manager = sys.modules.get("session_manager", session_manager)

    with (
        patch.object(
            current_session_manager, "validate_session_for_request", return_value=mock_result
        ),
        patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
    ):
        yield {"Authorization": "Bearer test-session-token"}


# ---------------------------------------------------------------------------
# get_role_from_context tests
# ---------------------------------------------------------------------------


class TestGetRoleFromContext:
    """Tests for get_role_from_context() helper function."""

    def test_role_from_session_agent_role(self, client, auth_headers):
        """Role is resolved from g.session.agent_role when present."""
        mock_session = MagicMock()
        mock_session.agent_role = "implementer"

        with client.application.test_request_context():
            from flask import g

            g.session = mock_session
            role = contract_api.get_role_from_context()

        assert role is not None
        assert role.value == "implementer"

    def test_role_from_x_egg_role_header_when_enabled(self, client, auth_headers):
        """Role is resolved from X-Egg-Role header when EGG_ENABLE_TEST_ROLE_HEADER=1."""
        with (
            client.application.test_request_context(headers={"X-Egg-Role": "reviewer"}),
            patch.dict(os.environ, {"EGG_ENABLE_TEST_ROLE_HEADER": "1"}, clear=False),
        ):
            from flask import g

            g.session = None
            role = contract_api.get_role_from_context()

        assert role is not None
        assert role.value == "reviewer"

    def test_role_from_x_egg_role_header_blocked_when_env_not_set(self, client, auth_headers):
        """X-Egg-Role header is ignored when EGG_ENABLE_TEST_ROLE_HEADER is not set."""
        env = os.environ.copy()
        env.pop("EGG_ENABLE_TEST_ROLE_HEADER", None)
        env.pop("EGG_AGENT_ROLE", None)

        with (
            client.application.test_request_context(headers={"X-Egg-Role": "reviewer"}),
            patch.dict(os.environ, env, clear=True),
        ):
            from flask import g

            g.session = None
            role = contract_api.get_role_from_context()

        assert role is None

    def test_role_from_env_var(self, client, auth_headers):
        """Role is resolved from EGG_AGENT_ROLE env var as fallback."""
        env = os.environ.copy()
        env.pop("EGG_ENABLE_TEST_ROLE_HEADER", None)
        env["EGG_AGENT_ROLE"] = "human"

        with (
            client.application.test_request_context(),
            patch.dict(os.environ, env, clear=True),
        ):
            from flask import g

            g.session = None
            role = contract_api.get_role_from_context()

        assert role is not None
        assert role.value == "human"

    def test_invalid_role_returns_none(self, client, auth_headers):
        """Invalid role string returns None."""
        env = os.environ.copy()
        env.pop("EGG_ENABLE_TEST_ROLE_HEADER", None)
        env["EGG_AGENT_ROLE"] = "superadmin"

        with (
            client.application.test_request_context(),
            patch.dict(os.environ, env, clear=True),
        ):
            from flask import g

            g.session = None
            role = contract_api.get_role_from_context()

        assert role is None

    def test_no_role_set_returns_none(self, client, auth_headers):
        """Returns None when no role source is available."""
        env = os.environ.copy()
        env.pop("EGG_ENABLE_TEST_ROLE_HEADER", None)
        env.pop("EGG_AGENT_ROLE", None)

        with (
            client.application.test_request_context(),
            patch.dict(os.environ, env, clear=True),
        ):
            from flask import g

            g.session = None
            role = contract_api.get_role_from_context()

        assert role is None


# ---------------------------------------------------------------------------
# GET /api/v1/contract/<issue_number> tests
# ---------------------------------------------------------------------------


class TestGetContract:
    """Tests for GET /api/v1/contract/<issue_number> endpoint."""

    def test_get_contract_success(self, client, auth_headers):
        """Successfully retrieves a contract."""
        mock_contract = MagicMock()
        mock_exported = {"issue": 42, "phases": []}

        with (
            patch.object(contract_api, "load_contract", return_value=mock_contract) as mock_load,
            patch.object(
                contract_api, "export_contract", return_value=mock_exported
            ) as mock_export,
        ):
            response = client.get(
                "/api/v1/contract/42?repo_path=/home/egg/repos/test",
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["message"] == "Contract retrieved"
        assert data["data"]["issue"] == 42
        mock_load.assert_called_once()
        mock_export.assert_called_once_with(mock_contract, include_audit_log=False)

    def test_get_contract_not_found(self, client, auth_headers):
        """Returns 404 when contract is not found."""
        from pathlib import Path

        from egg_contracts import ContractNotFoundError

        with patch.object(
            contract_api,
            "load_contract",
            side_effect=ContractNotFoundError(999, Path("/home/egg/repos/test")),
        ):
            response = client.get(
                "/api/v1/contract/999?repo_path=/home/egg/repos/test",
                headers=auth_headers,
            )

        assert response.status_code == 404
        data = json.loads(response.data)
        assert data["success"] is False
        assert "not found" in data["message"].lower()

    def test_get_contract_validation_error(self, client, auth_headers):
        """Returns 500 when contract validation fails."""
        from egg_contracts import ContractValidationError

        with patch.object(
            contract_api,
            "load_contract",
            side_effect=ContractValidationError(42, ["Bad schema"]),
        ):
            response = client.get(
                "/api/v1/contract/42?repo_path=/home/egg/repos/test",
                headers=auth_headers,
            )

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data["success"] is False
        assert "validation failed" in data["message"].lower()

    def test_get_contract_with_audit_log(self, client, auth_headers):
        """Passes include_audit_log=True when query param is set."""
        mock_contract = MagicMock()
        mock_exported = {"issue": 42, "phases": [], "audit_log": []}

        with (
            patch.object(contract_api, "load_contract", return_value=mock_contract),
            patch.object(
                contract_api, "export_contract", return_value=mock_exported
            ) as mock_export,
        ):
            response = client.get(
                "/api/v1/contract/42?repo_path=/home/egg/repos/test&include_audit_log=true",
                headers=auth_headers,
            )

        assert response.status_code == 200
        mock_export.assert_called_once_with(mock_contract, include_audit_log=True)


# ---------------------------------------------------------------------------
# GET /api/v1/contract/exists/<issue_number> tests
# ---------------------------------------------------------------------------


class TestCheckContractExists:
    """Tests for GET /api/v1/contract/exists/<issue_number> endpoint."""

    def test_contract_exists(self, client, auth_headers):
        """Returns exists=True when contract exists."""
        with patch.object(contract_api, "contract_exists", return_value=True):
            response = client.get(
                "/api/v1/contract/exists/42?repo_path=/home/egg/repos/test",
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["data"]["exists"] is True
        assert "exists" in data["message"].lower()

    def test_contract_does_not_exist(self, client, auth_headers):
        """Returns exists=False when contract does not exist."""
        with patch.object(contract_api, "contract_exists", return_value=False):
            response = client.get(
                "/api/v1/contract/exists/999?repo_path=/home/egg/repos/test",
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["data"]["exists"] is False
        assert "does not exist" in data["message"].lower()


# ---------------------------------------------------------------------------
# POST /api/v1/contract/validate tests
# ---------------------------------------------------------------------------


class TestValidateContractMutation:
    """Tests for POST /api/v1/contract/validate endpoint."""

    def test_missing_body_returns_400(self, client, auth_headers):
        """Returns 400 when request body is empty JSON."""
        response = client.post(
            "/api/v1/contract/validate",
            headers=auth_headers,
            data=json.dumps(None),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        assert "Missing request body" in data["message"]

    def test_missing_field_path_returns_400(self, client, auth_headers):
        """Returns 400 when field_path is missing."""
        response = client.post(
            "/api/v1/contract/validate",
            headers=auth_headers,
            data=json.dumps({"new_value": "complete"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        assert "field_path" in data["message"]

    def test_missing_new_value_returns_400(self, client, auth_headers):
        """Returns 400 when new_value is missing."""
        response = client.post(
            "/api/v1/contract/validate",
            headers=auth_headers,
            data=json.dumps({"field_path": "phases.0.tasks.0.status"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        assert "new_value" in data["message"]

    def test_no_role_returns_403(self, client, auth_headers):
        """Returns 403 when agent role cannot be determined."""
        with patch.object(contract_api, "get_role_from_context", return_value=None):
            response = client.post(
                "/api/v1/contract/validate",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "field_path": "phases.0.tasks.0.status",
                        "new_value": "complete",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 403
        data = json.loads(response.data)
        assert data["success"] is False
        assert "role" in data["message"].lower()

    def test_valid_mutation(self, client, auth_headers):
        """Returns success when mutation is valid."""
        from egg_contracts import Role, ValidationResult

        mock_result = ValidationResult(valid=True, message="Mutation allowed")

        with (
            patch.object(contract_api, "get_role_from_context", return_value=Role.IMPLEMENTER),
            patch.object(contract_api, "validate_mutation", return_value=mock_result),
        ):
            response = client.post(
                "/api/v1/contract/validate",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "field_path": "phases.0.tasks.0.commit",
                        "new_value": "abc1234",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert "allowed" in data["message"].lower()

    def test_invalid_mutation_returns_403(self, client, auth_headers):
        """Returns 403 when mutation is not allowed for the role."""
        from egg_contracts import Role, ValidationResult

        mock_result = ValidationResult(
            valid=False,
            message="Cannot modify field 'phases.*.tasks.*.status'.",
            field_path="phases.0.tasks.0.status",
            required_role="reviewer",
        )

        with (
            patch.object(contract_api, "get_role_from_context", return_value=Role.IMPLEMENTER),
            patch.object(contract_api, "validate_mutation", return_value=mock_result),
        ):
            response = client.post(
                "/api/v1/contract/validate",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "field_path": "phases.0.tasks.0.status",
                        "new_value": "complete",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 403
        data = json.loads(response.data)
        assert data["success"] is False
        assert "details" in data
        assert data["details"]["role"] == "implementer"
        assert data["details"]["required_role"] == "reviewer"


# ---------------------------------------------------------------------------
# POST /api/v1/contract/mutate tests
# ---------------------------------------------------------------------------


class TestMutateContract:
    """Tests for POST /api/v1/contract/mutate endpoint."""

    def test_missing_body_returns_400(self, client, auth_headers):
        """Returns 400 when request body is empty JSON."""
        response = client.post(
            "/api/v1/contract/mutate",
            headers=auth_headers,
            data=json.dumps(None),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        assert "Missing request body" in data["message"]

    def test_missing_issue_number_returns_400(self, client, auth_headers):
        """Returns 400 when issue_number is missing."""
        response = client.post(
            "/api/v1/contract/mutate",
            headers=auth_headers,
            data=json.dumps(
                {
                    "field_path": "phases.0.tasks.0.commit",
                    "new_value": "abc1234",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        assert "issue_number" in data["message"]

    def test_missing_field_path_returns_400(self, client, auth_headers):
        """Returns 400 when field_path is missing."""
        response = client.post(
            "/api/v1/contract/mutate",
            headers=auth_headers,
            data=json.dumps(
                {
                    "issue_number": 42,
                    "new_value": "abc1234",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        assert "field_path" in data["message"]

    def test_missing_new_value_returns_400(self, client, auth_headers):
        """Returns 400 when new_value is missing."""
        response = client.post(
            "/api/v1/contract/mutate",
            headers=auth_headers,
            data=json.dumps(
                {
                    "issue_number": 42,
                    "field_path": "phases.0.tasks.0.commit",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        assert "new_value" in data["message"]

    def test_no_role_returns_403(self, client, auth_headers):
        """Returns 403 when agent role cannot be determined."""
        with patch.object(contract_api, "get_role_from_context", return_value=None):
            response = client.post(
                "/api/v1/contract/mutate",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "issue_number": 42,
                        "field_path": "phases.0.tasks.0.commit",
                        "new_value": "abc1234",
                        "repo_path": "/home/egg/repos/test",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 403
        data = json.loads(response.data)
        assert data["success"] is False
        assert "role" in data["message"].lower()

    def test_contract_not_found_returns_404(self, client, auth_headers):
        """Returns 404 when contract is not found."""
        from pathlib import Path

        from egg_contracts import ContractNotFoundError, Role

        with (
            patch.object(contract_api, "get_role_from_context", return_value=Role.IMPLEMENTER),
            patch.object(
                contract_api,
                "load_contract",
                side_effect=ContractNotFoundError(999, Path("/home/egg/repos/test")),
            ),
        ):
            response = client.post(
                "/api/v1/contract/mutate",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "issue_number": 999,
                        "field_path": "phases.0.tasks.0.commit",
                        "new_value": "abc1234",
                        "repo_path": "/home/egg/repos/test",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 404
        data = json.loads(response.data)
        assert data["success"] is False
        assert "not found" in data["message"].lower()

    def test_mutation_denied_returns_403(self, client, auth_headers):
        """Returns 403 when mutation is denied by role-based enforcement."""
        from egg_contracts import MutationResult, Role

        mock_contract = MagicMock()
        mock_mutation_result = MutationResult(
            success=False,
            message="Cannot modify field 'phases.*.tasks.*.status'. "
            "Role 'implementer' is not authorized.",
        )

        with (
            patch.object(contract_api, "get_role_from_context", return_value=Role.IMPLEMENTER),
            patch.object(contract_api, "load_contract", return_value=mock_contract),
            patch.object(contract_api, "apply_mutation", return_value=mock_mutation_result),
        ):
            response = client.post(
                "/api/v1/contract/mutate",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "issue_number": 42,
                        "field_path": "phases.0.tasks.0.status",
                        "new_value": "complete",
                        "repo_path": "/home/egg/repos/test",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 403
        data = json.loads(response.data)
        assert data["success"] is False
        assert "details" in data
        assert data["details"]["role"] == "implementer"

    def test_mutate_success(self, client, auth_headers):
        """Successfully applies a mutation and saves the contract."""
        from egg_contracts import MutationResult, Role

        mock_contract = MagicMock()
        mock_updated_contract = MagicMock()
        mock_exported = {"issue": 42, "phases": [{"tasks": [{"commit": "abc1234"}]}]}

        mock_mutation_result = MutationResult(
            success=True,
            message="Mutation applied successfully",
            contract=mock_updated_contract,
        )

        with (
            patch.object(contract_api, "get_role_from_context", return_value=Role.IMPLEMENTER),
            patch.object(contract_api, "load_contract", return_value=mock_contract),
            patch.object(contract_api, "apply_mutation", return_value=mock_mutation_result),
            patch.object(contract_api, "save_contract") as mock_save,
            patch.object(contract_api, "export_contract", return_value=mock_exported),
        ):
            response = client.post(
                "/api/v1/contract/mutate",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "issue_number": 42,
                        "field_path": "phases.0.tasks.0.commit",
                        "new_value": "abc1234",
                        "actor": "james-in-a-box",
                        "reason": "Implementation complete",
                        "repo_path": "/home/egg/repos/test",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert "applied" in data["message"].lower()
        assert data["data"]["contract"]["issue"] == 42
        mock_save.assert_called_once()
