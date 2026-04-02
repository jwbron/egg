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


@pytest.fixture(autouse=True)
def mock_worktree_helpers():
    """Auto-mock _get_worktree_helpers to return passthrough mapping by default.

    The session fixture sets container_id='test-container', which triggers
    worktree path mapping. Without this mock, tests would fail because the
    real worktree doesn't exist on disk.

    Individual tests that need to verify worktree mapping behaviour should
    override this by patching _get_worktree_helpers themselves.
    """
    # Passthrough: returns the repo_path unchanged regardless of container_id
    passthrough_map = MagicMock(side_effect=lambda path, cid, op: path)
    dummy_err = MagicMock()

    # Clear the module-level cache so each test gets a fresh lookup
    old_cache = contract_api._cached_worktree_helpers
    contract_api._cached_worktree_helpers = None
    with patch.object(
        contract_api,
        "_get_worktree_helpers",
        return_value=(passthrough_map, dummy_err),
    ):
        yield
    contract_api._cached_worktree_helpers = old_cache


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


# ---------------------------------------------------------------------------
# Worktree path mapping tests
# ---------------------------------------------------------------------------


class TestWorktreePathMapping:
    """Tests for worktree path mapping in contract API endpoints."""

    def test_get_contract_with_container_id_maps_to_worktree(self, client, auth_headers):
        """GET contract with container_id query param maps repo_path to worktree path."""
        mock_contract = MagicMock()
        mock_exported = {"issue": 42, "phases": []}

        with (
            patch.object(contract_api, "load_contract", return_value=mock_contract) as mock_load,
            patch.object(contract_api, "export_contract", return_value=mock_exported),
            patch.object(
                contract_api,
                "_get_worktree_helpers",
                return_value=(
                    MagicMock(return_value="/home/egg/.egg-worktrees/test-ctr/test"),
                    MagicMock(),
                ),
            ),
        ):
            response = client.get(
                "/api/v1/contract/42?repo_path=/home/egg/repos/test&container_id=test-ctr",
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        # Verify load_contract was called with the worktree-mapped path
        from pathlib import Path

        mock_load.assert_called_once_with(42, Path("/home/egg/.egg-worktrees/test-ctr/test"))

    def test_get_contract_without_container_id_uses_original_path(self, client, auth_headers):
        """GET contract without container_id uses original repo_path unchanged."""
        mock_contract = MagicMock()
        mock_exported = {"issue": 42, "phases": []}

        with (
            patch.object(contract_api, "load_contract", return_value=mock_contract) as mock_load,
            patch.object(contract_api, "export_contract", return_value=mock_exported),
            patch.object(
                contract_api,
                "_get_worktree_helpers",
                return_value=(
                    # map_container_path_to_worktree returns original path when no container_id
                    MagicMock(return_value="/home/egg/repos/test"),
                    MagicMock(),
                ),
            ),
        ):
            response = client.get(
                "/api/v1/contract/42?repo_path=/home/egg/repos/test",
                headers=auth_headers,
            )

        assert response.status_code == 200
        from pathlib import Path

        mock_load.assert_called_once_with(42, Path("/home/egg/repos/test"))

    def test_get_contract_worktree_not_found_returns_error(self, client, auth_headers):
        """GET contract returns error when container_id worktree doesn't exist."""
        from flask import jsonify as flask_jsonify

        def make_err(cid):
            return flask_jsonify(
                {"success": False, "message": f"Worktree not found for '{cid}'"}
            ), 500

        with patch.object(
            contract_api,
            "_get_worktree_helpers",
            return_value=(
                MagicMock(return_value=None),  # worktree not found
                make_err,
            ),
        ):
            response = client.get(
                "/api/v1/contract/42?repo_path=/home/egg/repos/test&container_id=bad-ctr",
                headers=auth_headers,
            )

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data["success"] is False

    def test_mutate_contract_with_container_id_maps_to_worktree(self, client, auth_headers):
        """POST mutate with container_id in body maps repo_path to worktree path."""
        from egg_contracts import MutationResult, Role

        mock_contract = MagicMock()
        mock_updated_contract = MagicMock()
        mock_exported = {"issue": 42, "phases": []}
        mock_mutation_result = MutationResult(
            success=True,
            message="Mutation applied",
            contract=mock_updated_contract,
        )

        mock_map_fn = MagicMock(return_value="/home/egg/.egg-worktrees/test-ctr/test")

        with (
            patch.object(contract_api, "get_role_from_context", return_value=Role.IMPLEMENTER),
            patch.object(contract_api, "load_contract", return_value=mock_contract) as mock_load,
            patch.object(contract_api, "apply_mutation", return_value=mock_mutation_result),
            patch.object(contract_api, "save_contract") as mock_save,
            patch.object(contract_api, "export_contract", return_value=mock_exported),
            patch.object(
                contract_api,
                "_get_worktree_helpers",
                return_value=(mock_map_fn, MagicMock()),
            ),
        ):
            response = client.post(
                "/api/v1/contract/mutate",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "issue_number": 42,
                        "field_path": "phases.0.tasks.0.commit",
                        "new_value": "abc1234",
                        "repo_path": "/home/egg/repos/test",
                        "container_id": "test-ctr",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 200
        from pathlib import Path

        # Verify load was called with worktree path
        mock_load.assert_called_once_with(42, Path("/home/egg/.egg-worktrees/test-ctr/test"))
        # Verify save was called with worktree path
        mock_save.assert_called_once_with(
            mock_updated_contract, Path("/home/egg/.egg-worktrees/test-ctr/test")
        )
        # Verify map function was called with container_id
        mock_map_fn.assert_called_once_with("/home/egg/repos/test", "test-ctr", "contract")

    def test_mutate_contract_worktree_not_found_returns_error(self, client, auth_headers):
        """POST mutate returns error when container_id worktree doesn't exist."""
        from egg_contracts import Role
        from flask import jsonify as flask_jsonify

        def make_err(cid):
            return flask_jsonify(
                {"success": False, "message": f"Worktree not found for '{cid}'"}
            ), 500

        with (
            patch.object(contract_api, "get_role_from_context", return_value=Role.IMPLEMENTER),
            patch.object(
                contract_api,
                "_get_worktree_helpers",
                return_value=(
                    MagicMock(return_value=None),
                    make_err,
                ),
            ),
        ):
            response = client.post(
                "/api/v1/contract/mutate",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "issue_number": 42,
                        "field_path": "phases.0.tasks.0.commit",
                        "new_value": "abc1234",
                        "repo_path": "/home/egg/repos/test",
                        "container_id": "bad-ctr",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data["success"] is False

    def test_exists_with_container_id_maps_to_worktree(self, client, auth_headers):
        """GET exists with container_id maps repo_path to worktree path."""
        from pathlib import Path

        mock_map_fn = MagicMock(return_value="/home/egg/.egg-worktrees/test-ctr/test")

        with (
            patch.object(contract_api, "contract_exists", return_value=True) as mock_exists,
            patch.object(
                contract_api,
                "_get_worktree_helpers",
                return_value=(mock_map_fn, MagicMock()),
            ),
        ):
            response = client.get(
                "/api/v1/contract/exists/42?repo_path=/home/egg/repos/test&container_id=test-ctr",
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["data"]["exists"] is True
        mock_exists.assert_called_once_with(42, Path("/home/egg/.egg-worktrees/test-ctr/test"))
        mock_map_fn.assert_called_once_with("/home/egg/repos/test", "test-ctr", "contract")

    def test_exists_worktree_not_found_returns_error(self, client, auth_headers):
        """GET exists returns error when container_id worktree doesn't exist."""
        from flask import jsonify as flask_jsonify

        def make_err(cid):
            return flask_jsonify(
                {"success": False, "message": f"Worktree not found for '{cid}'"}
            ), 500

        with patch.object(
            contract_api,
            "_get_worktree_helpers",
            return_value=(
                MagicMock(return_value=None),
                make_err,
            ),
        ):
            response = client.get(
                "/api/v1/contract/exists/42?repo_path=/home/egg/repos/test&container_id=bad-ctr",
                headers=auth_headers,
            )

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data["success"] is False


# ---------------------------------------------------------------------------
# get_repo_path_from_request container_id extraction tests
# ---------------------------------------------------------------------------


class TestGetRepoPathFromRequestContainerId:
    """Tests for container_id extraction in get_repo_path_from_request()."""

    def test_container_id_from_query_params(self, client, auth_headers):
        """Extracts container_id from query params for GET requests."""
        with client.application.test_request_context(
            "/?repo_path=/home/egg/repos/test&container_id=my-container"
        ):
            from flask import g

            g.session = None
            path, error, container_id = contract_api.get_repo_path_from_request(from_query=True)

        assert container_id == "my-container"
        assert error is None
        from pathlib import Path

        assert path == Path("/home/egg/repos/test")

    def test_container_id_from_post_body(self, client, auth_headers):
        """Extracts container_id from JSON body for POST requests."""
        with client.application.test_request_context(
            "/",
            method="POST",
            data=json.dumps({"repo_path": "/home/egg/repos/test", "container_id": "my-container"}),
            content_type="application/json",
        ):
            from flask import g

            g.session = None
            path, error, container_id = contract_api.get_repo_path_from_request(from_query=False)

        assert container_id == "my-container"
        assert error is None
        from pathlib import Path

        assert path == Path("/home/egg/repos/test")

    def test_container_id_falls_back_to_session(self, client, auth_headers):
        """Falls back to session container_id when not in request."""
        mock_session = MagicMock()
        mock_session.container_id = "session-container"
        mock_session.repo_path = None

        with client.application.test_request_context("/?repo_path=/home/egg/repos/test"):
            from flask import g

            g.session = mock_session
            path, error, container_id = contract_api.get_repo_path_from_request(from_query=True)

        assert container_id == "session-container"

    def test_container_id_none_when_not_available(self, client, auth_headers):
        """Returns None container_id when not in request or session."""
        with client.application.test_request_context("/?repo_path=/home/egg/repos/test"):
            from flask import g

            g.session = None
            path, error, container_id = contract_api.get_repo_path_from_request(from_query=True)

        assert container_id is None

    def test_request_container_id_takes_priority_over_session(self, client, auth_headers):
        """Request container_id takes priority over session container_id."""
        mock_session = MagicMock()
        mock_session.container_id = "session-container"
        mock_session.repo_path = None

        with client.application.test_request_context(
            "/?repo_path=/home/egg/repos/test&container_id=request-container"
        ):
            from flask import g

            g.session = mock_session
            path, error, container_id = contract_api.get_repo_path_from_request(from_query=True)

        assert container_id == "request-container"


# ---------------------------------------------------------------------------
# _get_worktree_helpers tests
# ---------------------------------------------------------------------------


class TestGetWorktreeHelpers:
    """Tests for _get_worktree_helpers() lazy import function."""

    def test_returns_callable_tuple(self, client):
        """Returns a 2-tuple of callable functions."""
        map_fn, err_fn = contract_api._get_worktree_helpers()
        assert callable(map_fn)
        assert callable(err_fn)
