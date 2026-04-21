"""Tests for the gateway's contract API (pass-through to orchestrator).

After #1781, the gateway no longer reads or writes contract files
directly — it proxies every request to the orchestrator's
``/api/v1/contracts/…`` endpoints.  These tests cover:

- role resolution from session/header/env (unchanged behavior)
- identifier validation before forwarding
- request shape forwarded to the orchestrator
- upstream error relay

Business logic (role permissions, mutation application) lives in the
orchestrator and is tested in ``orchestrator/tests/test_contracts_routes.py``.
"""

from __future__ import annotations

import io
import json
import os
import sys
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import auth
import contract_api
import pytest
import session_manager
from session_manager import SessionValidationResult

import gateway


@pytest.fixture
def client():
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


@pytest.fixture
def auth_headers():
    """Session-authenticated headers with a mock session on g."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.pipeline_id = "test-pipeline"
    mock_session.agent_role = "implementer"
    mock_session.expires_at = None

    mock_result = SessionValidationResult(valid=True, session=mock_session)

    from private_repo_policy import PrivateRepoPolicyResult

    mock_policy_result = PrivateRepoPolicyResult(
        allowed=True,
        reason="Test mode - access allowed",
        visibility="public",
    )

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


def _make_urlopen(status: int, body: dict, *, capture: list | None = None):
    """Build a urlopen stand-in that returns *body* with *status*.

    Any list passed as ``capture`` collects the requests so tests can
    assert on what the gateway forwarded.
    """

    def _fake_urlopen(req, timeout=None):
        if capture is not None:
            payload = req.data.decode() if req.data else None
            capture.append(
                {
                    "url": req.full_url,
                    "method": req.get_method(),
                    "headers": dict(req.header_items()),
                    "body": json.loads(payload) if payload else None,
                }
            )

        class _Resp:
            def __init__(self) -> None:
                self.status = status
                self._data = json.dumps(body).encode()

            def read(self) -> bytes:
                return self._data

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *args) -> None:
                return None

        return _Resp()

    return _fake_urlopen


class TestGetRoleFromContext:
    def test_role_from_session(self, client):
        mock_session = MagicMock()
        mock_session.agent_role = "implementer"

        with client.application.test_request_context():
            from flask import g

            g.session = mock_session
            role = contract_api.get_role_from_context()

        assert role is not None
        assert role.value == "implementer"

    @pytest.mark.parametrize(
        ("fine_role", "expected"),
        [
            ("coder", "implementer"),
            ("reviewer_code", "reviewer"),
            ("overseer", "system"),
        ],
    )
    def test_fine_grained_role_maps_to_coarse(self, client, fine_role, expected):
        mock_session = MagicMock()
        mock_session.agent_role = fine_role

        with client.application.test_request_context():
            from flask import g

            g.session = mock_session
            role = contract_api.get_role_from_context()

        assert role is not None
        assert role.value == expected

    def test_header_honored_only_when_flag_set(self, client):
        with (
            client.application.test_request_context(headers={"X-Egg-Role": "reviewer"}),
            patch.dict(os.environ, {"EGG_ENABLE_TEST_ROLE_HEADER": "1"}, clear=False),
        ):
            from flask import g

            g.session = None
            role = contract_api.get_role_from_context()

        assert role is not None
        assert role.value == "reviewer"

    def test_header_ignored_without_flag(self, client):
        env = {k: v for k, v in os.environ.items() if k not in ("EGG_ENABLE_TEST_ROLE_HEADER",)}
        env.pop("EGG_AGENT_ROLE", None)

        with (
            client.application.test_request_context(headers={"X-Egg-Role": "reviewer"}),
            patch.dict(os.environ, env, clear=True),
        ):
            from flask import g

            g.session = None
            role = contract_api.get_role_from_context()

        assert role is None

    def test_env_fallback(self, client):
        env = {k: v for k, v in os.environ.items() if k != "EGG_ENABLE_TEST_ROLE_HEADER"}
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


class TestProxyToOrchestrator:
    def test_get_forwards_to_orchestrator(self, client, auth_headers):
        captured: list = []
        with patch.object(
            contract_api,
            "urlopen",
            _make_urlopen(200, {"success": True, "data": {"id": 42}}, capture=captured),
        ):
            response = client.get(
                "/api/v1/contract/42?pipeline_id=test-pipeline&repo=egg",
                headers=auth_headers,
            )

        assert response.status_code == 200
        assert len(captured) == 1
        forwarded = captured[0]
        assert "/api/v1/contracts/42" in forwarded["url"]
        assert "pipeline_id=test-pipeline" in forwarded["url"]
        assert forwarded["method"] == "GET"

    def test_get_includes_audit_log_param(self, client, auth_headers):
        captured: list = []
        with patch.object(
            contract_api,
            "urlopen",
            _make_urlopen(200, {"success": True, "data": {}}, capture=captured),
        ):
            client.get(
                "/api/v1/contract/42?include_audit_log=true&pipeline_id=test-pipeline",
                headers=auth_headers,
            )

        assert "include_audit_log=true" in captured[0]["url"]

    def test_mutate_forwards_role_header_and_body(self, client, auth_headers):
        captured: list = []
        with patch.object(
            contract_api,
            "urlopen",
            _make_urlopen(
                200,
                {"success": True, "data": {"contract": {"id": 42}}},
                capture=captured,
            ),
        ):
            response = client.post(
                "/api/v1/contract/mutate",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "identifier": 42,
                        "field_path": "phases.0.tasks.0.commit",
                        "new_value": "abc1234",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 200
        forwarded = captured[0]
        assert forwarded["url"].endswith("/api/v1/contracts/42/mutate")
        header_map = {k.lower(): v for k, v in forwarded["headers"].items()}
        assert header_map.get("x-egg-role") == "implementer"
        assert forwarded["body"]["field_path"] == "phases.0.tasks.0.commit"
        assert forwarded["body"]["new_value"] == "abc1234"
        # pipeline_id comes from the session and is forwarded for bootstrap context.
        assert forwarded["body"]["pipeline_id"] == "test-pipeline"

    def test_mutate_requires_role(self, client):
        env = {k: v for k, v in os.environ.items() if k != "EGG_ENABLE_TEST_ROLE_HEADER"}
        env.pop("EGG_AGENT_ROLE", None)

        no_role_session = MagicMock()
        no_role_session.mode = "public"
        no_role_session.agent_role = None
        no_role_session.pipeline_id = "test-pipeline"
        no_role_session.container_id = "test-container"
        no_role_session.expires_at = None

        from private_repo_policy import PrivateRepoPolicyResult

        policy = PrivateRepoPolicyResult(
            allowed=True,
            reason="Test mode",
            visibility="public",
        )

        auth._session_manager = None
        auth._rate_limiter = None

        with (
            patch.object(
                sys.modules.get("session_manager", session_manager),
                "validate_session_for_request",
                return_value=SessionValidationResult(valid=True, session=no_role_session),
            ),
            patch.object(gateway, "check_private_repo_access", return_value=policy),
            patch.dict(os.environ, env, clear=True),
        ):
            response = client.post(
                "/api/v1/contract/mutate",
                headers={"Authorization": "Bearer test-session-token"},
                data=json.dumps(
                    {
                        "identifier": 42,
                        "field_path": "phases",
                        "new_value": [],
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 403

    def test_upstream_error_relayed(self, client, auth_headers):
        error_body = json.dumps({"success": False, "message": "Not found"}).encode()

        def raising_urlopen(req, timeout=None):
            raise HTTPError(
                url=req.full_url,
                code=404,
                msg="Not Found",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(error_body),
            )

        with patch.object(contract_api, "urlopen", raising_urlopen):
            response = client.get(
                "/api/v1/contract/42?pipeline_id=test-pipeline",
                headers=auth_headers,
            )

        assert response.status_code == 404
        assert json.loads(response.data)["message"] == "Not found"

    def test_orchestrator_unreachable_returns_502(self, client, auth_headers):
        def raising_urlopen(req, timeout=None):
            raise URLError("connection refused")

        with patch.object(contract_api, "urlopen", raising_urlopen):
            response = client.get(
                "/api/v1/contract/42?pipeline_id=test-pipeline",
                headers=auth_headers,
            )

        assert response.status_code == 502

    def test_validate_forwards_without_identifier(self, client, auth_headers):
        captured: list = []
        with patch.object(
            contract_api,
            "urlopen",
            _make_urlopen(200, {"success": True, "message": "Mutation allowed"}, capture=captured),
        ):
            response = client.post(
                "/api/v1/contract/validate",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "field_path": "phases.0.status",
                        "new_value": "complete",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 200
        assert captured[0]["url"].endswith("/api/v1/contract-mutations/validate")


class TestIdentifierValidation:
    @pytest.mark.parametrize(
        "identifier",
        [
            "../../../etc/passwd",
            "drafts/../secret",
            "foo/bar",
            "foo\\bar",
        ],
    )
    def test_get_rejects_traversal(self, client, auth_headers, identifier):
        response = client.get(
            f"/api/v1/contract/{identifier}?pipeline_id=test-pipeline",
            headers=auth_headers,
        )
        # Either our regex rejects (400) or Flask's route matcher doesn't match (404)
        assert response.status_code in (400, 404)

    def test_mutate_rejects_traversal(self, client, auth_headers):
        response = client.post(
            "/api/v1/contract/mutate",
            headers=auth_headers,
            data=json.dumps(
                {
                    "identifier": "../../../etc/passwd",
                    "field_path": "phases",
                    "new_value": [],
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 400
