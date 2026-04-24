"""
Tests for the four ``/api/v1/jira/*`` routes in ``gateway/gateway.py``.

Covers Phase 2 / Task 4-4 acceptance criteria:

- Public mode → 403 on every route, with ``private_mode_required`` audit entry.
- Private mode + disallowed project → 403 ``*_denied`` / ``*_rejected``.
- Private mode + allowlisted project + mocked upstream → 200 with body.
- 404 envelope end-to-end on ``ticket/get`` and ``ticket/comments``.
- Adversarial JQL suite for ``/search``.
- ``/execute`` rejection of write methods, denied verbs, path traversal,
  disallowed projects.
- Route-enumeration regression: every ``/api/v1/jira/*`` view has
  ``__egg_requires_private_mode__ = True``.
- Audit-log assertions include ``session.jira_ticket`` and
  ``projects_extracted`` for search (and ``ticket`` is NOT emitted on search).
"""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import jira_policy
import pytest
import session_manager
from mode_gate import PRIVATE_MODE_MARKER_ATTR
from session_manager import SessionValidationResult

# Import the conftest-loaded modules.
import gateway  # noqa: F401 — registers the app + views

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def client():
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as c:
        yield c


def _patch_session(mode: str):
    """Return a context manager that patches session validation to yield a
    session with the given ``mode`` and a representative jira_ticket."""
    import auth

    mock_session = MagicMock()
    mock_session.mode = mode
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.pipeline_id = "issue-1556"
    mock_session.agent_role = "coder"
    mock_session.jira_ticket = "ENG-123"

    mock_result = SessionValidationResult(valid=True, session=mock_session)

    auth._session_manager = None
    auth._rate_limiter = None
    if "gateway.auth" in sys.modules:
        sys.modules["gateway.auth"]._session_manager = None
        sys.modules["gateway.auth"]._rate_limiter = None

    current_sm = sys.modules.get("session_manager", session_manager)
    return patch.object(
        current_sm,
        "validate_session_for_request",
        return_value=mock_result,
    )


@pytest.fixture
def private_headers():
    with _patch_session("private"):
        yield {"Authorization": "Bearer test-private-token"}


@pytest.fixture
def public_headers():
    with _patch_session("public"):
        yield {"Authorization": "Bearer test-public-token"}


@pytest.fixture
def allow_eng(monkeypatch):
    """Force the policy singleton to say ENG is allowlisted."""
    monkeypatch.setattr(
        jira_policy,
        "is_project_allowed",
        lambda p: p == "ENG",
    )
    # Also patch the import in gateway so the route's lookup uses our mock.
    monkeypatch.setattr(
        gateway,
        "is_project_allowed",
        lambda p: p == "ENG",
    )
    monkeypatch.setattr(jira_policy, "allowed_projects", lambda: frozenset({"ENG"}))
    # `gateway.py` imports `allowed_projects` lazily via `from .jira_policy
    # import allowed_projects`, so the direct module attribute on jira_policy
    # (singleton fallback) covers the route's call.


@pytest.fixture
def captured_audit(monkeypatch):
    """Capture every ``audit_log`` call made by gateway routes."""
    captured: list[dict[str, Any]] = []

    def _capture(event_type, operation, *, success, details=None):
        captured.append(
            {
                "event_type": event_type,
                "operation": operation,
                "success": success,
                "details": dict(details) if details else {},
            }
        )

    monkeypatch.setattr(gateway, "audit_log", _capture)
    return captured


# -----------------------------------------------------------------------------
# Route enumeration regression (risk R4)
# -----------------------------------------------------------------------------


class TestRouteEnumeration:
    def test_every_jira_route_has_private_mode_marker(self, client):
        """Walk ``app.url_map`` for every ``/api/v1/jira/*`` rule and assert
        each view function carries the private-mode marker.  This catches a
        future contributor adding a new Jira route without the decorator."""
        found = 0
        for rule in gateway.app.url_map.iter_rules():
            if not rule.rule.startswith("/api/v1/jira/"):
                continue
            view = gateway.app.view_functions[rule.endpoint]
            assert getattr(view, PRIVATE_MODE_MARKER_ATTR, False) is True, (
                f"Jira route {rule.rule!r} (view={view.__name__}) is missing "
                f"the @require_private_mode decorator."
            )
            found += 1
        assert found >= 4, f"Expected at least 4 Jira routes; found {found}"


# -----------------------------------------------------------------------------
# /api/v1/jira/ticket/get
# -----------------------------------------------------------------------------


class TestTicketGet:
    def test_public_mode_returns_403_and_audits(self, client, public_headers, captured_audit):
        resp = client.post(
            "/api/v1/jira/ticket/get",
            headers=public_headers,
            data=json.dumps({"ticket": "ENG-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        body = json.loads(resp.data)
        assert "private network mode" in body["message"].lower()
        # Audit entry from require_private_mode.
        assert any(a["event_type"] == "private_mode_required" for a in captured_audit)

    def test_invalid_ticket_shape_rejected(self, client, private_headers, captured_audit):
        resp = client.post(
            "/api/v1/jira/ticket/get",
            headers=private_headers,
            data=json.dumps({"ticket": "lowercase-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert any(
            a["event_type"] == "jira_ticket_get_rejected"
            and "invalid ticket" in a["details"].get("reason", "").lower()
            for a in captured_audit
        )

    def test_disallowed_project_returns_403(
        self, client, private_headers, captured_audit, monkeypatch
    ):
        monkeypatch.setattr(gateway, "is_project_allowed", lambda p: False)
        resp = client.post(
            "/api/v1/jira/ticket/get",
            headers=private_headers,
            data=json.dumps({"ticket": "SEC-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        body = json.loads(resp.data)
        # `make_error` stuffs `details` into the `data` field of the response.
        assert body.get("data", {}).get("project") == "SEC"
        # Audit entry from _project_not_allowlisted_response.
        denied = [a for a in captured_audit if a["event_type"] == "jira_ticket_get_denied"]
        assert denied
        assert denied[0]["details"]["reason"] == "project not allowlisted"

    def test_happy_path(self, client, private_headers, allow_eng, captured_audit):
        fake_client = MagicMock()
        fake_client.get_ticket.return_value = {"key": "ENG-1", "fields": {}}
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                "/api/v1/jira/ticket/get",
                headers=private_headers,
                data=json.dumps({"ticket": "ENG-1"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["data"]["key"] == "ENG-1"
        fake_client.get_ticket.assert_called_once_with("ENG-1", None)

        success = [a for a in captured_audit if a["event_type"] == "jira_ticket_get"]
        assert success
        details = success[0]["details"]
        assert details["ticket"] == "ENG-1"
        assert details["project"] == "ENG"
        assert details["pipeline_id"] == "issue-1556"
        assert details["agent_role"] == "coder"
        assert details["jira_ticket"] == "ENG-123"  # session.jira_ticket
        assert details["not_found"] is False

    def test_not_found_envelope_passes_through_as_200(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake_client = MagicMock()
        fake_client.get_ticket.return_value = {
            "status": "not_found",
            "key": "ENG-999",
            "upstream_status": 404,
        }
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                "/api/v1/jira/ticket/get",
                headers=private_headers,
                data=json.dumps({"ticket": "ENG-999"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["data"] == {
            "status": "not_found",
            "key": "ENG-999",
            "upstream_status": 404,
        }
        success = [a for a in captured_audit if a["event_type"] == "jira_ticket_get"]
        assert success[0]["details"]["not_found"] is True

    def test_invalid_fields_rejected(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            "/api/v1/jira/ticket/get",
            headers=private_headers,
            data=json.dumps({"ticket": "ENG-1", "fields": ["bad field"]}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert any(a["event_type"] == "jira_ticket_get_rejected" for a in captured_audit)


# -----------------------------------------------------------------------------
# /api/v1/jira/search
# -----------------------------------------------------------------------------


class TestSearch:
    def test_public_mode_403(self, client, public_headers):
        resp = client.post(
            "/api/v1/jira/search",
            headers=public_headers,
            data=json.dumps({"jql": "project = ENG"}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_missing_jql_400(self, client, private_headers, captured_audit):
        resp = client.post(
            "/api/v1/jira/search",
            headers=private_headers,
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert any(a["event_type"] == "jira_search_rejected" for a in captured_audit)

    @pytest.mark.parametrize(
        "jql",
        [
            "project = ENG OR project = SEC",
            'project = "ENG"',
            "PROJECT = ENG",
            "project = projectsLeadByUser()",
            "project = ENG /* hack */",
            "project IN (ENG, SEC)",  # SEC not allowlisted
            "status = Open",
            "project = ENG ; drop",
            'key = "ENG-1"',
            "project = ЕNG",  # Cyrillic
        ],
    )
    def test_adversarial_jql_rejected(
        self, client, private_headers, allow_eng, captured_audit, jql: str
    ):
        resp = client.post(
            "/api/v1/jira/search",
            headers=private_headers,
            data=json.dumps({"jql": jql}),
            content_type="application/json",
        )
        assert resp.status_code == 403, f"expected 403 for {jql!r}"
        body = json.loads(resp.data)
        assert "rejected" in body["message"].lower()
        rejected = [a for a in captured_audit if a["event_type"] == "jira_search_rejected"]
        assert rejected, f"expected audit entry for {jql!r}"
        # ``ticket`` must NEVER appear on search audits (Task 2-2 acceptance).
        assert "ticket" not in rejected[-1]["details"]

    def test_happy_path_clamps_max_results(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake_client = MagicMock()
        fake_client.search.return_value = {"issues": [], "nextPageToken": None}
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                "/api/v1/jira/search",
                headers=private_headers,
                data=json.dumps(
                    {
                        "jql": "project = ENG AND status = Open",
                        "maxResults": 99999,
                        "nextPageToken": "TOK-abc",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200
        kwargs = fake_client.search.call_args.kwargs
        assert kwargs["max_results"] == 100  # clamped
        assert kwargs["next_page_token"] == "TOK-abc"

        success = [a for a in captured_audit if a["event_type"] == "jira_search"]
        assert success
        details = success[0]["details"]
        assert details["projects_extracted"] == ["ENG"]
        # Search audits must NOT emit ``ticket``.
        assert "ticket" not in details

    def test_invalid_max_results_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            "/api/v1/jira/search",
            headers=private_headers,
            data=json.dumps({"jql": "project = ENG", "maxResults": "bad"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert any(a["event_type"] == "jira_search_rejected" for a in captured_audit)


# -----------------------------------------------------------------------------
# /api/v1/jira/ticket/comments
# -----------------------------------------------------------------------------


class TestTicketComments:
    def test_public_mode_403(self, client, public_headers):
        resp = client.post(
            "/api/v1/jira/ticket/comments",
            headers=public_headers,
            data=json.dumps({"ticket": "ENG-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_disallowed_project_403(self, client, private_headers, captured_audit, monkeypatch):
        monkeypatch.setattr(gateway, "is_project_allowed", lambda p: False)
        resp = client.post(
            "/api/v1/jira/ticket/comments",
            headers=private_headers,
            data=json.dumps({"ticket": "SEC-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        denied = [a for a in captured_audit if a["event_type"] == "jira_ticket_comments_denied"]
        assert denied

    def test_happy_path(self, client, private_headers, allow_eng, captured_audit):
        fake_client = MagicMock()
        fake_client.get_comments.return_value = {"comments": [{"id": "1", "body": "hi"}]}
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                "/api/v1/jira/ticket/comments",
                headers=private_headers,
                data=json.dumps({"ticket": "ENG-1"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        fake_client.get_comments.assert_called_once_with("ENG-1")

    def test_not_found_envelope(self, client, private_headers, allow_eng, captured_audit):
        fake_client = MagicMock()
        fake_client.get_comments.return_value = {
            "status": "not_found",
            "key": "ENG-9",
            "upstream_status": 404,
        }
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                "/api/v1/jira/ticket/comments",
                headers=private_headers,
                data=json.dumps({"ticket": "ENG-9"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["data"]["status"] == "not_found"


# -----------------------------------------------------------------------------
# /api/v1/jira/execute
# -----------------------------------------------------------------------------


class TestExecute:
    def test_public_mode_403(self, client, public_headers):
        resp = client.post(
            "/api/v1/jira/execute",
            headers=public_headers,
            data=json.dumps({"method": "GET", "path": "issue/ENG-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_non_get_methods_rejected(
        self, client, private_headers, allow_eng, captured_audit, method: str
    ):
        resp = client.post(
            "/api/v1/jira/execute",
            headers=private_headers,
            data=json.dumps({"method": method, "path": "issue/ENG-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        denied = [a for a in captured_audit if a["event_type"] == "jira_execute_denied"]
        assert denied

    @pytest.mark.parametrize(
        "path",
        [
            "issue/ENG-1/transitions",
            "issue/ENG-1/worklog",
            "issue/ENG-1/attachments",
            "issue/ENG-1/watchers",
        ],
    )
    def test_denied_verb_in_path_rejected(
        self, client, private_headers, allow_eng, captured_audit, path: str
    ):
        resp = client.post(
            "/api/v1/jira/execute",
            headers=private_headers,
            data=json.dumps({"method": "GET", "path": path}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_path_traversal_rejected(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            "/api/v1/jira/execute",
            headers=private_headers,
            data=json.dumps({"method": "GET", "path": "issue/../FOO-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_disallowed_project_rejected(
        self, client, private_headers, captured_audit, monkeypatch
    ):
        monkeypatch.setattr(gateway, "is_project_allowed", lambda p: False)
        resp = client.post(
            "/api/v1/jira/execute",
            headers=private_headers,
            data=json.dumps({"method": "GET", "path": "issue/SEC-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        denied = [a for a in captured_audit if a["event_type"] == "jira_execute_denied"]
        assert denied
        assert denied[-1]["details"]["reason"] == "project not allowlisted"

    def test_happy_path_get(self, client, private_headers, allow_eng, captured_audit):
        fake_client = MagicMock()
        fake_client.execute_raw.return_value = {"key": "ENG"}
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                "/api/v1/jira/execute",
                headers=private_headers,
                data=json.dumps(
                    {
                        "method": "GET",
                        "path": "project/ENG",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["data"]["key"] == "ENG"
        # Audit entry for successful execute.
        success = [a for a in captured_audit if a["event_type"] == "jira_execute"]
        assert success
        assert success[0]["details"]["method"] == "GET"
        assert success[0]["details"]["path"] == "project/ENG"
        assert success[0]["details"]["project"] == "ENG"

    def test_missing_path_400(self, client, private_headers, captured_audit):
        resp = client.post(
            "/api/v1/jira/execute",
            headers=private_headers,
            data=json.dumps({"method": "GET"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
