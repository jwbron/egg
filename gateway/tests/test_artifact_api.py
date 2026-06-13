"""Pins the slice-4 ``gateway/artifact_api.py`` forwarder contract (#3077 TASK-4-4 / task-4-2).

The gateway artifact blueprint (``POST /api/v1/artifact/get``) is a thin
session-authenticated forwarder onto the orchestrator's
``orchestrator/routes/artifacts.py`` route (see
``orchestrator/tests/test_artifact_routes.py`` for the orchestrator-side
contract).  Per HITL Q2 of #3077 the gateway must NOT accept a repo path:
the only handle is a spec-registered ``name``, and unregistered names are
rejected with the orchestrator's structured 4xx body forwarded verbatim.

These tests mirror ``test_contract_api.py`` style — session auth from the
mock session manager, ``urlopen`` patched on the blueprint module so the
forwarder is exercised without a live orchestrator — and pin three
invariants that the slice-4 prose retirement (slice-5) and the
``egg-artifact`` sandbox helper (TASK-4-3) depend on:

1. **Session auth before forwarding** — an unauthenticated POST never
   reaches the orchestrator (and never lands in the orchestrator audit log
   as a phantom request).
2. **Verbatim 4xx passthrough** — the orchestrator's strict-mode error
   bodies (unregistered name listing alternatives, non-hex ref, absent at
   ref) reach the agent unaltered so ``egg-artifact`` can render the right
   stderr message without re-encoding the orchestrator's response.
3. **No path field** — the schema strips/rejects any ``path`` key from the
   request body so a hand-crafted curl can never reach the orchestrator
   with an attacker-supplied path.  This is the slice-4 ratchet against
   the very class of breach the artifact endpoint is meant to make
   impossible.
"""

from __future__ import annotations

import io
import json
import sys
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import auth
import pytest
import session_manager
from session_manager import SessionValidationResult

import gateway

# Imported via gateway/tests/conftest.py if gateway/artifact_api.py exists
# (slice-4 BRC parallel mode); the per-test ``pytest.importorskip`` keeps
# the suite collectible on branches where the producer hasn't landed yet
# while still failing loudly once the file is in the tree but mis-shapen.
artifact_api = pytest.importorskip(
    "artifact_api",
    reason="gateway/artifact_api.py not yet present on this branch — "
    "expected once slice-4 coder (TASK-4-2) commits land via "
    "consensus_wrapper.sync_to_proposals.",
)


@pytest.fixture
def client():
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


@pytest.fixture
def auth_headers():
    """Session-authenticated headers with a mock session on ``g``.

    Lifted verbatim from ``test_contract_api.py::auth_headers`` so the
    forwarder is exercised under the same session shape the contract API
    accepts in production.  Keeping them aligned is a soft ratchet against
    auth drift between sibling blueprints.
    """
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.pipeline_id = "issue-3077"
    mock_session.agent_role = "reviewer_code"
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
            current_session_manager,
            "validate_session_for_request",
            return_value=mock_result,
        ),
        patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
    ):
        yield {"Authorization": "Bearer test-session-token"}


def _make_urlopen(status: int, body: dict, *, capture: list | None = None):
    """``urlopen`` stand-in matching the ``test_contract_api.py`` helper.

    Any list passed as ``capture`` collects the forwarder's outgoing
    request so tests can pin the URL, method, headers, and body — the
    contract the orchestrator route signs on.
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


# ---------------------------------------------------------------------------
# Happy path forwarding
# ---------------------------------------------------------------------------


class TestArtifactGetForwarding:
    """Forwarder must reach the orchestrator with the verified role + body."""

    def test_forwards_to_orchestrator_with_role_header(self, client, auth_headers):
        """Session role -> ``X-Egg-Role`` header on the forwarded request.

        The role MUST come from the session (mock_session.agent_role above),
        NOT from the body — a body-level role would let an agent forge a
        higher-privilege role on the orchestrator side.  This is the same
        anti-forgery rule ``contract_api.mutate_contract`` enforces.
        """
        captured: list = []
        orch_body = {
            "success": True,
            "data": {
                "name": "plan-draft",
                "ref": "abcdef0123456789abcdef0123456789abcdef01",
                "path": ".egg-state/drafts/3077-plan.md",
                "content": "# Plan body\n",
                "truncated": False,
            },
        }
        with patch.object(
            artifact_api,
            "urlopen",
            _make_urlopen(200, orch_body, capture=captured),
        ):
            response = client.post(
                "/api/v1/artifact/get",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "name": "plan-draft",
                        "ref": "abcdef0123456789abcdef0123456789abcdef01",
                        "pipeline_id": "issue-3077",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 200
        forwarded_body = response.get_json()
        assert forwarded_body == orch_body, (
            "200 body must be the orchestrator response forwarded verbatim"
        )

        assert len(captured) == 1
        forwarded = captured[0]
        # The forwarder targets the orchestrator's artifacts route — the
        # exact URL is the orchestrator-side coder's choice (slice-4
        # task-4-1), but ``/artifact`` MUST be in the path so a misrouted
        # request (e.g. to /contract) is impossible.
        assert "/artifact" in forwarded["url"]
        assert forwarded["method"] == "POST"
        header_map = {k.lower(): v for k, v in forwarded["headers"].items()}
        assert header_map.get("x-egg-role") == "reviewer", (
            "session agent_role 'reviewer_code' maps to coarse contract role 'reviewer'"
        )
        # The session's pipeline_id must be on the forwarded body even
        # when the agent only sent ``name``+``ref`` — the orchestrator
        # needs it to resolve ``_pipeline_identifier``.
        assert forwarded["body"]["name"] == "plan-draft"
        assert forwarded["body"]["ref"] == "abcdef0123456789abcdef0123456789abcdef01"
        assert forwarded["body"]["pipeline_id"] == "issue-3077"

    def test_forwarded_body_strips_path_field(self, client, auth_headers):
        """Strict per HITL Q2: a body-level ``path`` MUST NOT reach the orchestrator.

        Either the gateway responds 400 (schema rejection) or the path is
        silently dropped before the body is forwarded.  Either way, the
        orchestrator must NEVER receive ``path`` — a path that reaches the
        orchestrator could trip a future logging hook into recording the
        attempted bypass as if it were legitimate.
        """
        captured: list = []
        with patch.object(
            artifact_api,
            "urlopen",
            _make_urlopen(
                200,
                {
                    "success": True,
                    "data": {
                        "name": "plan-draft",
                        "path": ".egg-state/drafts/3077-plan.md",
                        "content": "ok",
                        "truncated": False,
                    },
                },
                capture=captured,
            ),
        ):
            response = client.post(
                "/api/v1/artifact/get",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "name": "plan-draft",
                        "ref": "abcdef0123456789abcdef0123456789abcdef01",
                        "pipeline_id": "issue-3077",
                        "path": "/etc/passwd",
                    }
                ),
                content_type="application/json",
            )

        if response.status_code == 200:
            assert captured, "200 must have reached the forwarder"
            forwarded_body = captured[0]["body"] or {}
            assert "path" not in forwarded_body, (
                "gateway must strip 'path' from the body before forwarding upstream"
            )
        else:
            # Schema-level rejection is also acceptable; the bar is "path
            # never gets through".  Even on rejection the forwarder must
            # not have been invoked, so the malicious body cannot end up
            # in the orchestrator's audit log.
            assert response.status_code == 400
            assert not captured, (
                "schema-rejected request must never reach the orchestrator forwarder"
            )

    def test_requires_session_auth(self, client):
        """Unauthenticated request -> 401/403 BEFORE the forwarder runs.

        Auth failures must short-circuit before ``urlopen`` so a leaked
        forwarder call cannot poison the orchestrator's audit log with a
        forged session.  We assert ``urlopen`` is never called, the same
        invariant ``test_contract_api.py`` doesn't explicitly cover —
        bringing the bar up for the new endpoint.
        """
        with patch.object(artifact_api, "urlopen") as mock_urlopen:
            response = client.post(
                "/api/v1/artifact/get",
                data=json.dumps(
                    {
                        "name": "plan-draft",
                        "ref": "abcdef0123456789abcdef0123456789abcdef01",
                        "pipeline_id": "issue-3077",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code in (401, 403)
        mock_urlopen.assert_not_called()

    def test_required_fields_validated_locally(self, client, auth_headers):
        """Missing ``name`` or ``ref`` -> 400 from the gateway, no upstream call.

        Schema validation belongs on the gateway so a missing field is
        caught at the edge rather than burning an orchestrator round-trip;
        ``contract_api.mutate_contract`` enforces the same shape.
        """
        with patch.object(artifact_api, "urlopen") as mock_urlopen:
            response = client.post(
                "/api/v1/artifact/get",
                headers=auth_headers,
                data=json.dumps({"name": "plan-draft", "pipeline_id": "issue-3077"}),
                content_type="application/json",
            )

        assert response.status_code == 400
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# Orchestrator error passthrough
# ---------------------------------------------------------------------------


class TestArtifactGetErrorPassthrough:
    """Orchestrator's structured 4xx bodies must reach the agent unaltered."""

    @pytest.mark.parametrize(
        ("upstream_status", "upstream_body"),
        [
            pytest.param(
                400,
                {
                    "success": False,
                    "message": (
                        "Unknown artifact 'definitely-not-registered'. "
                        "Registered names: analysis-draft, plan-draft, "
                        "architect-output, architect-slices, "
                        "risk-analyst-output"
                    ),
                },
                id="unregistered-name",
            ),
            pytest.param(
                400,
                {"success": False, "message": "ref must be a hex commit SHA"},
                id="non-hex-ref",
            ),
            pytest.param(
                404,
                {
                    "success": False,
                    "message": (
                        "plan-draft not found at ref abcdef01 (.egg-state/drafts/3077-plan.md)"
                    ),
                },
                id="absent-at-ref",
            ),
        ],
    )
    def test_orchestrator_4xx_relayed_verbatim(
        self,
        client,
        auth_headers,
        upstream_status,
        upstream_body,
    ):
        """The forwarder must NOT wrap upstream 4xx bodies in a generic envelope.

        ``egg-artifact`` reads ``message`` directly to print the user-facing
        stderr line.  If the gateway rewrote the body, the registered-name
        hint would be lost and an agent debugging an "unknown name" 400 would
        have no clue which names exist.  We pin this for every strict-mode
        4xx the orchestrator can emit.
        """
        error_payload = json.dumps(upstream_body).encode()

        def raising_urlopen(req, timeout=None):
            raise HTTPError(
                url=req.full_url,
                code=upstream_status,
                msg="strict-mode rejection",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(error_payload),
            )

        with patch.object(artifact_api, "urlopen", raising_urlopen):
            response = client.post(
                "/api/v1/artifact/get",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "name": "definitely-not-registered",
                        "ref": "abcdef0123456789abcdef0123456789abcdef01",
                        "pipeline_id": "issue-3077",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == upstream_status
        assert response.get_json() == upstream_body

    def test_orchestrator_unreachable_502(self, client, auth_headers):
        """Orchestrator unreachable -> 502 (distinguish "down" from "absent").

        Matches ``contract_api``'s passthrough rule so the sandbox helper
        can print "retry in a moment" for 502 and "fix your input" for 4xx.
        """

        def raising_urlopen(req, timeout=None):
            raise URLError("connection refused")

        with patch.object(artifact_api, "urlopen", raising_urlopen):
            response = client.post(
                "/api/v1/artifact/get",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "name": "plan-draft",
                        "ref": "abcdef0123456789abcdef0123456789abcdef01",
                        "pipeline_id": "issue-3077",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 502

    def test_orchestrator_success_truncated_flag_passes_through(
        self,
        client,
        auth_headers,
    ):
        """``truncated: true`` MUST reach the sandbox helper unchanged.

        The CLI uses the flag to print a "...(content truncated)" notice
        without re-probing the orchestrator.  Re-encoding by the gateway
        would silently drop the flag.
        """
        orch_body = {
            "success": True,
            "data": {
                "name": "plan-draft",
                "ref": "abcdef0123456789abcdef0123456789abcdef01",
                "path": ".egg-state/drafts/3077-plan.md",
                "content": "x" * 1024,
                "truncated": True,
            },
        }
        with patch.object(
            artifact_api,
            "urlopen",
            _make_urlopen(200, orch_body),
        ):
            response = client.post(
                "/api/v1/artifact/get",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "name": "plan-draft",
                        "ref": "abcdef0123456789abcdef0123456789abcdef01",
                        "pipeline_id": "issue-3077",
                    }
                ),
                content_type="application/json",
            )

        assert response.status_code == 200
        assert response.get_json()["data"]["truncated"] is True


# ---------------------------------------------------------------------------
# Schema: no path field, GET not allowed
# ---------------------------------------------------------------------------


class TestArtifactGetSchema:
    """The gateway blueprint exposes exactly one verb on one URL."""

    def test_get_method_not_allowed(self, client, auth_headers):
        """``GET /api/v1/artifact/get`` -> 405 (POST is the only verb).

        Pins the blueprint definition so a future "make GET an alias"
        proposal has to confront the API-surface ratchet first.
        """
        response = client.get("/api/v1/artifact/get", headers=auth_headers)
        assert response.status_code in (404, 405)

    def test_blueprint_url_prefix_matches_contract_api_convention(self, client, auth_headers):
        """Blueprint mounts at ``/api/v1/artifact`` (singular, mirrors ``contract``).

        The gateway prefix MUST be singular ``artifact`` — the orchestrator
        side uses ``artifacts`` (plural) for the route blueprint, just like
        the ``contract`` (gateway) / ``contracts`` (orchestrator) pair.
        This asymmetry is a deliberate convention; pinning it prevents a
        future "let's align them" rewrite from breaking sandbox CLI
        wiring (``egg-artifact`` calls the gateway, not the orchestrator,
        and any path drift here is invisible to gateway-blind callers).
        """
        with patch.object(artifact_api, "urlopen", _make_urlopen(404, {"success": False})):
            singular = client.post(
                "/api/v1/artifact/get",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "name": "plan-draft",
                        "ref": "abcdef0123456789abcdef0123456789abcdef01",
                        "pipeline_id": "issue-3077",
                    }
                ),
                content_type="application/json",
            )
            # Reachable (not 404 from Flask routing): we proxied to a
            # 404-returning fake upstream, so the body is the upstream
            # 404, not Flask's HTML 404.
            assert singular.status_code == 404
            assert (singular.get_json() or {}).get("success") is False

        plural = client.post(
            "/api/v1/artifacts/get",
            headers=auth_headers,
            data=json.dumps(
                {
                    "name": "plan-draft",
                    "ref": "abcdef0123456789abcdef0123456789abcdef01",
                    "pipeline_id": "issue-3077",
                }
            ),
            content_type="application/json",
        )
        # Plural is NOT a registered route on the gateway — Flask returns
        # its own 404 (HTML).  This is the negative-side ratchet.
        assert plural.status_code == 404
