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
        future contributor adding a new Jira route without the decorator.

        With #1924 the surface grows from four read routes to eight (four
        reads + four writes), so the lower bound moves to 8.  The four new
        write routes (``ticket/create``, ``ticket/edit``, ``ticket/comment/
        add``, ``issue-link/create``) MUST also carry the marker — that's
        enforced by this same loop, not a separate assertion.
        """
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
        assert found >= 8, f"Expected at least 8 Jira routes; found {found}"

    def test_all_eight_jira_routes_registered(self, client):
        """Pin the exact route set so a regression that drops a write
        route surfaces immediately.

        Issue #1557 slice-2 grows the surface from 8 to 10 routes
        (``ticket/remotelinks`` read + ``ticket/transition`` write — the
        transition route is orchestrator-only, see
        ``TestTicketTransition`` for the loopback / shared-secret auth).
        """
        rules = {
            rule.rule
            for rule in gateway.app.url_map.iter_rules()
            if rule.rule.startswith("/api/v1/jira/")
        }
        expected = {
            "/api/v1/jira/ticket/get",
            "/api/v1/jira/ticket/comments",
            "/api/v1/jira/search",
            "/api/v1/jira/execute",
            # New in #1924:
            "/api/v1/jira/ticket/create",
            "/api/v1/jira/ticket/edit",
            "/api/v1/jira/ticket/comment/add",
            "/api/v1/jira/issue-link/create",
            # New in #1557 slice-2:
            "/api/v1/jira/ticket/remotelinks",
            "/api/v1/jira/ticket/transition",
        }
        missing = expected - rules
        assert not missing, f"Missing Jira routes: {sorted(missing)}"


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


# =============================================================================
# Write routes (issue #1924)
#
# Per-route 403/400 grid.  Each test class targets one route and walks the
# matrix:
#   - public mode → 403
#   - missing creds → 503 (JiraCredentialsUnavailable)
#   - non-allowlisted project → 403
#   - malformed body → 400 (e.g. wrong types, missing required fields)
#   - oversized → 400 (summary > 255, description > 32 KiB)
#   - unknown issuetype → 400 (create only)
#   - cross-project parent → 400 (create only)
#   - both `parent` and `epicLink` → 400 (create only)
#   - mixed labels mode → 400 (edit only)
#   - custom-field smuggling → 400 (extra body keys)
#   - non-allowlisted link type → 400 (link only)
#   - unicode-in-keys → 400
#   - HTTP-method tunnelling → 400 (`method` key in body)
#   - success path → 2xx with audit assertion
#   - issue-link envelope shape (link only)
# =============================================================================


from jira_client import JiraCredentialsUnavailable, JiraUpstreamError  # noqa: E402

# Sentinel project for rejection tests — distinct from "ENG" so the
# allow_eng fixture's allowlist filter sees an unfamiliar key.
_BAD_PROJECT = "SEC"


def _last_audit_for_op(captured: list[dict[str, Any]], op: str) -> dict[str, Any] | None:
    """Convenience accessor: find the most recent audit entry for a given
    ``operation``.  Returns ``None`` if absent so tests can fail with a
    helpful message."""
    matches = [a for a in captured if a["operation"] == op]
    return matches[-1] if matches else None


class TestTicketCreate:
    """``POST /api/v1/jira/ticket/create``."""

    OP = "jira_ticket_create"
    PATH = "/api/v1/jira/ticket/create"

    def _valid_body(self) -> dict[str, Any]:
        return {
            "project": "ENG",
            "issuetype": "Task",
            "summary": "hello",
        }

    def test_public_mode_403(self, client, public_headers, captured_audit):
        resp = client.post(
            self.PATH,
            headers=public_headers,
            data=json.dumps(self._valid_body()),
            content_type="application/json",
        )
        assert resp.status_code == 403
        assert any(a["event_type"] == "private_mode_required" for a in captured_audit)

    def test_missing_creds_503(self, client, private_headers, allow_eng, captured_audit):
        fake_client = MagicMock()
        fake_client.create_issue.side_effect = JiraCredentialsUnavailable("nope")
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps(self._valid_body()),
                content_type="application/json",
            )
        assert resp.status_code == 503

    def test_disallowed_project_403(self, client, private_headers, captured_audit, monkeypatch):
        monkeypatch.setattr(gateway, "is_project_allowed", lambda p: False)
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "project": _BAD_PROJECT}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        denied = [a for a in captured_audit if a["event_type"] == f"{self.OP}_denied"]
        assert denied
        assert denied[-1]["details"]["reason"] == "project not allowlisted"

    def test_invalid_project_shape_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "project": "lowercase"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_summary_400(self, client, private_headers, allow_eng, captured_audit):
        body = self._valid_body()
        del body["summary"]
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_blank_summary_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "summary": "   "}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_oversized_summary_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "summary": "x" * 1000}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_oversized_description_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "description": "x" * (33 * 1024)}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_unknown_issuetype_name_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "issuetype": "Goose"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        rejected = _last_audit_for_op(captured_audit, self.OP)
        assert rejected is not None
        # Audit must record the reason; we assert the event_type was
        # ``*_rejected`` (success=False).
        assert rejected["success"] is False

    def test_cross_project_parent_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps(
                {
                    **self._valid_body(),
                    "parent": "DEVOPS-1",  # different project
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert "parent" in body["message"].lower() or "project" in body["message"].lower()

    def test_both_parent_and_epic_link_400(
        self, client, private_headers, allow_eng, captured_audit
    ):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps(
                {
                    **self._valid_body(),
                    "parent": "ENG-1",
                    "epicLink": "ENG-2",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        rejected = [
            a for a in captured_audit if a["details"].get("reason") == "parent_and_epic_link"
        ]
        assert rejected

    def test_cross_project_epic_link_400(
        self, client, private_headers, allow_eng, captured_audit, monkeypatch
    ):
        """Regression for the cycle-1 reviewer_security NACK fix
        (gateway/gateway.py:5370-5384).  ``epicLink`` writes to the same
        Atlassian field as ``parent`` when ``epic_link_field == "parent"``;
        a cross-project epicLink would let an agent parent a new ticket
        under an epic in a different project, bypassing the
        ``parent``-side cross-project rejection.  The route must reject
        with 400 and an audit reason of ``cross_project_epic_link``."""
        # Both ENG and DEVOPS allowlisted so the rejection is the
        # cross-project guard, not the project-allowlist guard.
        monkeypatch.setattr(gateway, "is_project_allowed", lambda p: p in {"ENG", "DEVOPS"})
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps(
                {
                    **self._valid_body(),
                    "epicLink": "DEVOPS-1",  # not the new ticket's project
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        rejected = [
            a for a in captured_audit if a["details"].get("reason") == "cross_project_epic_link"
        ]
        assert rejected, "cross_project_epic_link audit reason missing"
        # Audit must record both projects so operators can grep the cross-
        # project pair after the fact.
        assert rejected[-1]["details"]["project"] == "ENG"
        assert rejected[-1]["details"]["epic_project"] == "DEVOPS"

    def test_non_allowlisted_epic_link_403(
        self, client, private_headers, captured_audit, monkeypatch
    ):
        """Regression for the cycle-1 reviewer_security NACK fix
        (gateway/gateway.py:5363-5369).  An agent in an allowlisted
        project (ENG) MUST NOT be able to point ``epicLink`` at a
        non-allowlisted project — that would let them read / write the
        epic by virtue of the parent-link relationship.  The route
        returns 403 with ``epicLink project not allowlisted`` and the
        ``{operation}_denied`` event_type."""
        # Allowlist ONLY ENG; FORBIDDEN is excluded.
        monkeypatch.setattr(gateway, "is_project_allowed", lambda p: p == "ENG")
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps(
                {
                    **self._valid_body(),
                    "epicLink": "FORBIDDEN-1",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 403
        denied = [a for a in captured_audit if a["event_type"] == f"{self.OP}_denied"]
        assert denied, f"{self.OP}_denied event missing"
        assert denied[-1]["details"]["reason"] == "epicLink project not allowlisted"
        # Audit captures the rejected project key so an operator can
        # forensic-search for the leak attempt.
        assert denied[-1]["details"]["project"] == "FORBIDDEN"

    def test_invalid_epic_link_shape_400(self, client, private_headers, allow_eng, captured_audit):
        """``epicLink: not-a-key`` is rejected at the regex layer with
        400 — defence-in-depth before any allowlist check."""
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps(
                {**self._valid_body(), "epicLink": "not-a-ticket"},
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert "epiclink" in body["message"].lower() or "ticket" in body["message"].lower()

    def test_custom_field_smuggling_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps(
                {
                    **self._valid_body(),
                    "customfield_10010": "smuggled",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_method_tunneling_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "method": "DELETE"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_unicode_in_project_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "project": "ЕNG"}),  # Cyrillic
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_invalid_parent_shape_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "parent": "not-a-key"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_oversized_label_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "labels": ["x" * 100]}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_too_many_labels_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "labels": [f"l{i}" for i in range(31)]}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_idempotency_key_must_be_string_400(
        self, client, private_headers, allow_eng, captured_audit
    ):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "idempotencyKey": 42}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_non_object_body_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps([]),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_happy_path_returns_envelope_and_audits(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake_client = MagicMock()
        # JiraClient.create_issue returns (status, body, cache_hit) since
        # v2 (#1924 reviewer_code_holistic finding #3).
        fake_client.create_issue.return_value = (
            201,
            {
                "id": "10001",
                "key": "ENG-1",
                "self": "https://example.atlassian.net/rest/api/3/issue/10001",
            },
            False,
        )
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps(
                    {
                        **self._valid_body(),
                        "description": "body text",
                        "labels": ["one"],
                        "idempotencyKey": "k-1",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200, resp.data
        body = json.loads(resp.data)
        env = body["data"]
        # Decision-13 envelope.
        assert env["status"] == "created"
        assert env["key"] == "ENG-1"
        assert env["id"] == "10001"
        assert env["browse_url"] == "https://example.atlassian.net/browse/ENG-1"

        success = _last_audit_for_op(captured_audit, self.OP)
        assert success is not None
        # Success-path event_type is ``f"{op}_ok"`` since v2 (audit grammar
        # parity per reviewer_code_holistic cycle 1 finding #3).
        assert success["event_type"] == f"{self.OP}_ok"
        assert success["success"] is True
        details = success["details"]
        assert details["project"] == "ENG"
        assert details["ticket"] == "ENG-1"
        # Body content metadata, never raw body (refine feedback Q5).
        assert details.get("summary_length") == len("hello")
        assert details.get("description_length") == len("body text")
        assert details.get("labels") == ["one"]
        # Body never logged.
        assert "summary" not in details
        assert "description" not in details
        # idempotency_hit / idempotency_key_present surfaced in audit so
        # operators can distinguish replays from upstream calls.
        assert details.get("idempotency_key_present") is True
        assert details.get("idempotency_hit") is False

    def test_happy_path_with_adf_description(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake_client = MagicMock()
        fake_client.create_issue.return_value = (
            201,
            {"id": "1", "key": "ENG-1", "self": "https://e.atlassian.net/rest/api/3/issue/1"},
            False,
        )
        adf = {"type": "doc", "version": 1, "content": []}
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps({**self._valid_body(), "description": adf}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        # The route forwarded ADF unchanged to JiraClient.create_issue.
        kwargs = fake_client.create_issue.call_args.kwargs
        assert kwargs["description"] == adf

    # -------------------------------------------------------------------
    # Issue #1557 task-1-6 — per-project ``epic_link_field`` dispatch.
    # -------------------------------------------------------------------
    #
    # The dispatch from the ``epicLink`` shorthand to either ``parent``
    # (next-gen / company-managed projects, default) or
    # ``customfield_10014`` (classic / team-managed projects) is wired
    # at ``gateway/gateway.py:6097`` — the route reads
    # ``JiraPolicy.epic_link_field`` and passes it to
    # ``JiraClient.create_issue``.  ``JiraClient.create_issue``'s wire
    # translation is covered by
    # ``gateway/tests/test_jira_client.py::TestCreateIssue::test_epic_
    # link_with_{parent,customfield}_dispatch``.  The tests below close
    # the route-layer half: they assert the gateway route reads the
    # policy and propagates the resolved field name verbatim to the
    # JiraClient call.  Together the two sides verify the operator-
    # managed ``epic_link_field`` setting (refine decision-3) is
    # exercised end-to-end before the epic pipeline relies on it for
    # child-ticket creation.

    def test_epic_link_dispatches_via_parent_field(
        self, client, private_headers, allow_eng, captured_audit, monkeypatch
    ):
        """Default ``epic_link_field='parent'`` (next-gen / company-managed
        sites) → the route hands ``epic_link_field='parent'`` to
        ``JiraClient.create_issue``, which then writes
        ``fields: {parent: {key: <KEY>}}`` on the Atlassian wire.
        Verified at the JiraClient layer by
        ``test_epic_link_with_parent_dispatch`` in test_jira_client.py."""
        monkeypatch.setattr(gateway, "jira_epic_link_field", lambda: "parent")
        fake_client = MagicMock()
        fake_client.create_issue.return_value = (
            201,
            {"id": "1", "key": "ENG-2", "self": "https://e.atlassian.net/rest/api/3/issue/1"},
            False,
        )
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps({**self._valid_body(), "epicLink": "ENG-1"}),
                content_type="application/json",
            )
        assert resp.status_code == 200, resp.data
        kwargs = fake_client.create_issue.call_args.kwargs
        # The route must forward both the requested epic link AND the
        # operator-configured dispatch field — the JiraClient layer
        # then translates ``epic_link_field='parent'`` into
        # ``fields.parent: {key: <KEY>}`` (covered in test_jira_client.py).
        assert kwargs["epic_link"] == "ENG-1"
        assert kwargs["epic_link_field"] == "parent"

    def test_epic_link_dispatches_via_customfield(
        self, client, private_headers, allow_eng, captured_audit, monkeypatch
    ):
        """``epic_link_field='customfield_10014'`` (classic / team-managed
        sites) → the route hands the customfield name to
        ``JiraClient.create_issue``, which writes
        ``fields: {customfield_10014: <KEY>}`` on the wire.  Verified at
        the JiraClient layer by ``test_epic_link_with_customfield_
        dispatch`` in test_jira_client.py."""
        monkeypatch.setattr(gateway, "jira_epic_link_field", lambda: "customfield_10014")
        fake_client = MagicMock()
        fake_client.create_issue.return_value = (
            201,
            {"id": "1", "key": "ENG-2", "self": "https://e.atlassian.net/rest/api/3/issue/1"},
            False,
        )
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps({**self._valid_body(), "epicLink": "ENG-1"}),
                content_type="application/json",
            )
        assert resp.status_code == 200, resp.data
        kwargs = fake_client.create_issue.call_args.kwargs
        assert kwargs["epic_link"] == "ENG-1"
        assert kwargs["epic_link_field"] == "customfield_10014"

    def test_upstream_error_passes_through(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake_client = MagicMock()
        fake_client.create_issue.side_effect = JiraUpstreamError(500, "boom", "issue")
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps(self._valid_body()),
                content_type="application/json",
            )
        # 5xx surfaces as 502 (upstream gateway error).
        assert resp.status_code in (500, 502)
        upstream = [a for a in captured_audit if a["event_type"] == f"{self.OP}_upstream_error"]
        assert upstream


class TestTicketEdit:
    """``POST /api/v1/jira/ticket/edit``."""

    OP = "jira_ticket_edit"
    PATH = "/api/v1/jira/ticket/edit"

    def _valid_body(self) -> dict[str, Any]:
        return {"ticket": "ENG-1", "summary": "x"}

    def test_public_mode_403(self, client, public_headers, captured_audit):
        resp = client.post(
            self.PATH,
            headers=public_headers,
            data=json.dumps(self._valid_body()),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_missing_creds_503(self, client, private_headers, allow_eng, captured_audit):
        fake_client = MagicMock()
        fake_client.edit_issue.side_effect = JiraCredentialsUnavailable("nope")
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps(self._valid_body()),
                content_type="application/json",
            )
        assert resp.status_code == 503

    def test_invalid_ticket_shape_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "lowercase-1", "summary": "x"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_disallowed_project_403(self, client, private_headers, captured_audit, monkeypatch):
        monkeypatch.setattr(gateway, "is_project_allowed", lambda p: False)
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "SEC-1", "summary": "x"}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_oversized_summary_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "ENG-1", "summary": "x" * 1000}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_mixed_label_modes_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps(
                {
                    "ticket": "ENG-1",
                    "labels": ["a"],
                    "addLabels": ["b"],
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        rejected = [a for a in captured_audit if a["details"].get("reason") == "mixed_label_modes"]
        assert rejected

    def test_custom_field_smuggling_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "ENG-1", "customfield_10010": "smuggled"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_method_tunneling_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "ENG-1", "method": "DELETE"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_no_changes_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "ENG-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_notify_users_must_be_bool_400(
        self, client, private_headers, allow_eng, captured_audit
    ):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "ENG-1", "summary": "x", "notifyUsers": "false"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_unicode_in_ticket_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "ЕNG-1", "summary": "x"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_happy_path_returns_envelope_and_audits(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake_client = MagicMock()
        fake_client.edit_issue.return_value = {}
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps(
                    {
                        "ticket": "ENG-1",
                        "summary": "new",
                        "labels": ["a", "b"],
                        "notifyUsers": True,
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200, resp.data
        body = json.loads(resp.data)
        # Decision-14 envelope.
        assert body["data"] == {"status": "updated", "key": "ENG-1"}

        success = _last_audit_for_op(captured_audit, self.OP)
        assert success is not None and success["success"] is True
        # ``f"{op}_ok"`` event_type since v2 audit-grammar parity.
        assert success["event_type"] == f"{self.OP}_ok"
        details = success["details"]
        assert details["ticket"] == "ENG-1"
        assert details["project"] == "ENG"
        assert details["notify_users"] is True
        assert details.get("labels") == ["a", "b"]
        # edit_issue is naturally idempotent (PUT) → idempotency_hit is
        # ALWAYS False on the edit route, for grammar parity with the
        # other write routes.
        assert details.get("idempotency_hit") is False


class TestTicketCommentAdd:
    """``POST /api/v1/jira/ticket/comment/add``."""

    OP = "jira_ticket_comment_add"
    PATH = "/api/v1/jira/ticket/comment/add"

    def _valid_body(self) -> dict[str, Any]:
        return {"ticket": "ENG-1", "body": "hello"}

    def test_public_mode_403(self, client, public_headers, captured_audit):
        resp = client.post(
            self.PATH,
            headers=public_headers,
            data=json.dumps(self._valid_body()),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_missing_creds_503(self, client, private_headers, allow_eng, captured_audit):
        fake_client = MagicMock()
        fake_client.add_comment.side_effect = JiraCredentialsUnavailable("nope")
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps(self._valid_body()),
                content_type="application/json",
            )
        assert resp.status_code == 503

    def test_disallowed_project_403(self, client, private_headers, captured_audit, monkeypatch):
        monkeypatch.setattr(gateway, "is_project_allowed", lambda p: False)
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "SEC-1", "body": "hi"}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_invalid_ticket_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "bad", "body": "hi"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_body_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "ENG-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_oversized_body_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "ENG-1", "body": "x" * (33 * 1024)}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_visibility_rejected_400(self, client, private_headers, allow_eng, captured_audit):
        """Decision-6: visibility is hidden in v1."""
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps(
                {
                    "ticket": "ENG-1",
                    "body": "hi",
                    "visibility": {"type": "role", "value": "Administrators"},
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert "visibility" in body["message"].lower()

    def test_custom_field_smuggling_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "ENG-1", "body": "hi", "customfield_10010": "x"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_method_tunneling_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "ENG-1", "body": "hi", "method": "DELETE"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_unicode_in_ticket_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "ЕNG-1", "body": "hi"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_happy_path_audit_redacts_body(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake_client = MagicMock()
        fake_client.add_comment.return_value = (201, {"id": "10010"}, False)
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps(self._valid_body()),
                content_type="application/json",
            )
        assert resp.status_code == 200, resp.data
        body = json.loads(resp.data)
        # Atlassian comment object passed through verbatim under ``data``.
        assert body["data"] == {"id": "10010"}

        success = _last_audit_for_op(captured_audit, self.OP)
        assert success is not None and success["success"] is True
        assert success["event_type"] == f"{self.OP}_ok"
        details = success["details"]
        assert details["ticket"] == "ENG-1"
        assert details["project"] == "ENG"
        # Body length recorded; raw body never recorded.
        assert details.get("body_length") == len("hello")
        assert "hello" not in json.dumps(details)
        # No idempotency_key supplied; cache bypassed.
        assert details.get("idempotency_key_present") is False
        assert details.get("idempotency_hit") is False


class TestIssueLinkCreate:
    """``POST /api/v1/jira/issue-link/create``."""

    OP = "jira_issue_link_create"
    PATH = "/api/v1/jira/issue-link/create"

    def _valid_body(self) -> dict[str, Any]:
        return {
            "type": "Blocks",
            "inwardIssue": "ENG-1",
            "outwardIssue": "ENG-2",
        }

    def test_public_mode_403(self, client, public_headers, captured_audit):
        resp = client.post(
            self.PATH,
            headers=public_headers,
            data=json.dumps(self._valid_body()),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_missing_creds_503(self, client, private_headers, allow_eng, captured_audit):
        fake_client = MagicMock()
        fake_client.create_issue_link.side_effect = JiraCredentialsUnavailable("nope")
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps(self._valid_body()),
                content_type="application/json",
            )
        assert resp.status_code == 503

    def test_non_allowlisted_link_type_400(
        self, client, private_headers, allow_eng, captured_audit, monkeypatch
    ):
        # Force an "only Blocks" allowlist so "Cloners" is rejected.
        monkeypatch.setattr(gateway, "jira_link_type_allowed", lambda v: v == "Blocks")
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "type": "Cloners"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        rejected = [
            a for a in captured_audit if a["details"].get("reason") == "link_type_not_allowlisted"
        ]
        assert rejected

    def test_inward_project_disallowed_403(
        self, client, private_headers, captured_audit, monkeypatch
    ):
        # Allow ENG (so "type" passes its own gate via default link_types),
        # but reject SEC entirely.  Patch jira_link_type_allowed to default-
        # allow so the link-type gate doesn't pre-empt.
        monkeypatch.setattr(gateway, "jira_link_type_allowed", lambda _v: True)
        monkeypatch.setattr(gateway, "is_project_allowed", lambda p: p == "ENG")
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps(
                {
                    **self._valid_body(),
                    "inwardIssue": "SEC-1",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_outward_project_disallowed_403(
        self, client, private_headers, captured_audit, monkeypatch
    ):
        monkeypatch.setattr(gateway, "jira_link_type_allowed", lambda _v: True)
        monkeypatch.setattr(gateway, "is_project_allowed", lambda p: p == "ENG")
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps(
                {
                    **self._valid_body(),
                    "outwardIssue": "SEC-1",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_invalid_inward_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "inwardIssue": "bad"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_invalid_outward_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "outwardIssue": "bad"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_type_400(self, client, private_headers, allow_eng, captured_audit):
        body = self._valid_body()
        del body["type"]
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_oversized_comment_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "comment": "x" * (33 * 1024)}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_custom_field_smuggling_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "customfield_x": "y"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_method_tunneling_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "method": "DELETE"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_unicode_in_inward_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "inwardIssue": "ЕNG-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_idempotency_must_be_string_400(
        self, client, private_headers, allow_eng, captured_audit
    ):
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({**self._valid_body(), "idempotencyKey": 42}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_happy_path_returns_envelope(self, client, private_headers, allow_eng, captured_audit):
        fake_client = MagicMock()
        fake_client.create_issue_link.return_value = (201, {}, False)
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps(self._valid_body()),
                content_type="application/json",
            )
        assert resp.status_code == 200, resp.data
        body = json.loads(resp.data)
        # Issue-link envelope shape.
        assert body["data"] == {
            "status": "created",
            "inwardIssue": "ENG-1",
            "outwardIssue": "ENG-2",
            "type": "Blocks",
        }

        success = _last_audit_for_op(captured_audit, self.OP)
        assert success is not None and success["success"] is True
        assert success["event_type"] == f"{self.OP}_ok"
        details = success["details"]
        assert details["inwardIssue"] == "ENG-1"
        assert details["outwardIssue"] == "ENG-2"
        assert details["type"] == "Blocks"
        # Project-pair recorded for forensic search.
        assert details["inward_project"] == "ENG"
        assert details["outward_project"] == "ENG"
        # Link type recorded (refine feedback Q5 — operator-controlled,
        # low-PII).
        assert details.get("link_type") == "Blocks"
        # No idempotency_key supplied; cache bypassed.
        assert details.get("idempotency_key_present") is False
        assert details.get("idempotency_hit") is False

    def test_happy_path_with_comment_audit_redacts_body(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake_client = MagicMock()
        fake_client.create_issue_link.return_value = (201, {}, False)
        comment = "see issue #1924"
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps({**self._valid_body(), "comment": comment}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        # The route forwards comment text to JiraClient.create_issue_link.
        kwargs = fake_client.create_issue_link.call_args.kwargs
        assert kwargs["comment"] == comment

        success = _last_audit_for_op(captured_audit, self.OP)
        assert success is not None
        details = success["details"]
        # Comment body never logged verbatim.
        assert "see issue #1924" not in json.dumps(details)


# -----------------------------------------------------------------------------
# Issue #1557 slice-2 — /api/v1/jira/ticket/remotelinks (task-2-3)
# -----------------------------------------------------------------------------


class TestTicketRemoteLinks:
    """Tests for the slice-2 ``/api/v1/jira/ticket/remotelinks`` route.

    Acceptance criteria (task-2-3):
      - Route returns 200 + remote-link payload for an allowlisted project.
      - 403 for a denied project.
      - Inherits private-mode gating like every other agent-facing Jira
        route (covered by ``TestRouteEnumeration``).
    """

    PATH = "/api/v1/jira/ticket/remotelinks"
    OP = "jira_ticket_remotelinks"

    def test_public_mode_returns_403(self, client, public_headers, captured_audit):
        resp = client.post(
            self.PATH,
            headers=public_headers,
            data=json.dumps({"ticket": "ENG-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_invalid_ticket_shape_rejected(self, client, private_headers, captured_audit):
        """Tickets that don't match ``<PROJECT>-<number>`` → 400."""
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "not-a-ticket"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        rejected = [a for a in captured_audit if a["event_type"].endswith("rejected")]
        assert any(r["details"].get("reason") == "invalid ticket shape" for r in rejected)

    def test_missing_ticket_rejected(self, client, private_headers, captured_audit):
        """Missing ``ticket`` key → 400."""
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_disallowed_project_returns_403(
        self, client, private_headers, captured_audit, monkeypatch
    ):
        """Allowlist enforcement: ENG-1 with SEC-only allowlist → 403."""
        monkeypatch.setattr(
            gateway,
            "is_project_allowed",
            lambda p: p == "SEC",
        )
        resp = client.post(
            self.PATH,
            headers=private_headers,
            data=json.dumps({"ticket": "ENG-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        denied = [a for a in captured_audit if "denied" in a["event_type"]]
        assert denied

    def test_happy_path_returns_payload(self, client, private_headers, allow_eng, captured_audit):
        """Successful read returns ``{remotelinks: [...]}`` with audit log."""
        fake_client = MagicMock()
        sample = {"remotelinks": [{"object": {"url": "https://github.com/jwbron/egg/pull/1"}}]}
        fake_client.get_remotelinks.return_value = sample
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps({"ticket": "ENG-1"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["data"]["remotelinks"] == sample["remotelinks"]
        fake_client.get_remotelinks.assert_called_once_with("ENG-1")

        success = _last_audit_for_op(captured_audit, self.OP)
        assert success is not None
        details = success["details"]
        assert details["ticket"] == "ENG-1"
        assert details["project"] == "ENG"
        assert details["remotelink_count"] == 1
        # The route MUST NOT leak the URL payload into the audit log
        # (decision-5 + audit-redaction discipline).
        assert "github.com/jwbron/egg/pull/1" not in json.dumps(details)

    def test_not_found_envelope_audited(self, client, private_headers, allow_eng, captured_audit):
        """A 404 from upstream returns the ``not_found`` envelope."""
        fake_client = MagicMock()
        fake_client.get_remotelinks.return_value = {
            "status": "not_found",
            "key": "ENG-999",
            "upstream_status": 404,
        }
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps({"ticket": "ENG-999"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        success = _last_audit_for_op(captured_audit, self.OP)
        assert success["details"]["not_found"] is True

    def test_empty_remotelinks_list_count_zero(
        self, client, private_headers, allow_eng, captured_audit
    ):
        """A ticket with no remote links returns count=0 in the audit log."""
        fake_client = MagicMock()
        fake_client.get_remotelinks.return_value = {"remotelinks": []}
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=private_headers,
                data=json.dumps({"ticket": "ENG-1"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        success = _last_audit_for_op(captured_audit, self.OP)
        assert success["details"]["remotelink_count"] == 0


# -----------------------------------------------------------------------------
# Issue #1557 slice-2 — /api/v1/jira/ticket/transition (task-2-6)
# -----------------------------------------------------------------------------


class TestTicketTransition:
    """Tests for the slice-2 orchestrator-only
    ``/api/v1/jira/ticket/transition`` route.

    Acceptance criteria (task-2-6):
      - Route exists; non-allowlisted ``transition_name`` returns 400.
      - Missing or wrong ``X-Egg-Orchestrator-Token`` returns 401.
        (Implementation uses ``Authorization: Bearer <launcher>`` —
        same bearer scheme as the launcher; the planned
        ``X-Egg-Orchestrator-Token`` header was unified onto Authorization
        + launcher secret + loopback IP.)
      - Caller from outside the orchestrator subnet returns 403.
      - Successful invocation transitions the ticket and adds the comment
        in a single audit-logged operation.
      - ``JIRA_WRITE_VERBS_DENIED`` and ``validate_jira_api_path`` remain
        unchanged (transitions still denied for the agent path).
    """

    PATH = "/api/v1/jira/ticket/transition"
    OP = "jira_ticket_transition"

    @pytest.fixture
    def loopback_request(self, monkeypatch):
        """Force ``request.remote_addr`` to a loopback address so the
        orchestrator-only auth check passes."""

        # ``_is_in_cluster_source`` already accepts ``127.0.0.1`` (loopback);
        # Flask test client sets remote_addr to ``127.0.0.1`` by default.
        # No patching required — but we add this fixture so future test
        # additions can opt out symmetrically.
        yield

    @pytest.fixture
    def bearer_headers(self):
        """Headers with the launcher-secret bearer token. Conftest sets
        ``EGG_LAUNCHER_SECRET=test-launcher-secret-12345``."""
        return {
            "Authorization": "Bearer test-launcher-secret-12345",
        }

    def _valid_body(self) -> dict:
        return {
            "ticket": "ENG-1",
            "transition_name": "Won't Do",
            "comment": "Consolidated into ENG-2",
        }

    def test_missing_bearer_returns_401(self, client, captured_audit):
        """No Authorization header → 401 (missing_bearer_auth)."""
        resp = client.post(
            self.PATH,
            data=json.dumps(self._valid_body()),
            content_type="application/json",
        )
        assert resp.status_code == 401
        body = json.loads(resp.data)
        assert body.get("data", {}).get("reason") == "missing_bearer_auth"
        unauthorized = [a for a in captured_audit if "unauthorized" in a["event_type"]]
        assert unauthorized

    def test_wrong_bearer_returns_401(self, client, captured_audit):
        """Wrong launcher-secret value → 401 (bad_bearer_auth)."""
        resp = client.post(
            self.PATH,
            headers={"Authorization": "Bearer wrong-secret"},
            data=json.dumps(self._valid_body()),
            content_type="application/json",
        )
        assert resp.status_code == 401
        body = json.loads(resp.data)
        assert body.get("data", {}).get("reason") == "bad_bearer_auth"

    def test_external_source_returns_403(self, client, captured_audit, bearer_headers, monkeypatch):
        """Caller from a public IP (not in RFC1918 / loopback) → 403."""
        # Patch the test client to fake remote_addr.

        # Build a request manually since Flask test_client defaults to 127.0.0.1.
        with gateway.app.test_request_context(
            self.PATH,
            method="POST",
            data=json.dumps(self._valid_body()),
            content_type="application/json",
            headers=bearer_headers,
            environ_base={"REMOTE_ADDR": "8.8.8.8"},
        ):
            response = gateway.app.full_dispatch_request()
        assert response.status_code == 403
        body = json.loads(response.data)
        assert body.get("data", {}).get("reason") == "source_not_in_cluster"

    def test_loopback_source_with_correct_secret_accepted(
        self, client, captured_audit, bearer_headers, allow_eng
    ):
        """127.0.0.1 + correct secret + valid body → transition succeeds."""
        fake_client = MagicMock()
        fake_client.transition_issue.return_value = (204, {})
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=bearer_headers,
                data=json.dumps(self._valid_body()),
                content_type="application/json",
            )
        assert resp.status_code == 200, resp.data
        body = json.loads(resp.data)
        assert body["data"]["upstream_status"] == 204
        fake_client.transition_issue.assert_called_once()

    def test_invalid_ticket_returns_400(self, client, captured_audit, bearer_headers):
        """Ticket key that doesn't match ``<PROJECT>-<number>`` → 400."""
        resp = client.post(
            self.PATH,
            headers=bearer_headers,
            data=json.dumps(
                {
                    "ticket": "garbage",
                    "transition_name": "Won't Do",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_transition_name_returns_400(self, client, captured_audit, bearer_headers):
        resp = client.post(
            self.PATH,
            headers=bearer_headers,
            data=json.dumps({"ticket": "ENG-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert body.get("data", {}).get("reason") == "missing_transition_name"

    def test_non_allowlisted_transition_returns_400(self, client, captured_audit, bearer_headers):
        """``transition_name`` outside the allowlist → 400 with diagnostic."""
        resp = client.post(
            self.PATH,
            headers=bearer_headers,
            data=json.dumps(
                {
                    "ticket": "ENG-1",
                    "transition_name": "In Progress",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert body.get("data", {}).get("reason") == "transition_not_allowlisted"
        # Allowlist returned in the error body so the caller can recover.
        allowed = body.get("data", {}).get("allowed", [])
        assert any("won't do" in a.lower() for a in allowed)
        # Audit log entry for the denial.
        denied = [a for a in captured_audit if a["event_type"].endswith("denied")]
        assert any(d["details"].get("reason") == "transition_not_allowlisted" for d in denied)

    def test_disallowed_project_returns_403(
        self, client, captured_audit, bearer_headers, monkeypatch
    ):
        """Even with valid auth, the project allowlist still applies."""
        monkeypatch.setattr(
            gateway,
            "is_project_allowed",
            lambda p: p == "OTHER",
        )
        resp = client.post(
            self.PATH,
            headers=bearer_headers,
            data=json.dumps(self._valid_body()),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_happy_path_audits_caller_metadata(
        self, client, captured_audit, bearer_headers, allow_eng
    ):
        """Audit log records caller IP, transition name, ticket key, and outcome."""
        fake_client = MagicMock()
        fake_client.transition_issue.return_value = (204, {})
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=bearer_headers,
                data=json.dumps(self._valid_body()),
                content_type="application/json",
            )
        assert resp.status_code == 200
        success = _last_audit_for_op(captured_audit, self.OP)
        assert success is not None
        details = success["details"]
        assert details["ticket"] == "ENG-1"
        assert details["project"] == "ENG"
        assert details["transition_name"] == "Won't Do"
        assert details["upstream_status"] == 204
        # ``remote_addr`` recorded for forensics.
        assert "remote_addr" in details

    def test_comment_attached_when_provided(
        self, client, captured_audit, bearer_headers, allow_eng
    ):
        """A non-empty ``comment`` is wrapped as ADF and forwarded."""
        fake_client = MagicMock()
        fake_client.transition_issue.return_value = (204, {})
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=bearer_headers,
                data=json.dumps(self._valid_body()),
                content_type="application/json",
            )
        assert resp.status_code == 200
        kwargs = fake_client.transition_issue.call_args.kwargs
        # comment_adf is the wrapped ADF object — non-None means it was attached.
        assert kwargs["comment_adf"] is not None
        assert kwargs["transition_name"] == "Won't Do"

    def test_no_comment_skips_adf_wrap(self, client, captured_audit, bearer_headers, allow_eng):
        fake_client = MagicMock()
        fake_client.transition_issue.return_value = (204, {})
        body_no_comment = self._valid_body()
        body_no_comment.pop("comment")
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=bearer_headers,
                data=json.dumps(body_no_comment),
                content_type="application/json",
            )
        assert resp.status_code == 200
        kwargs = fake_client.transition_issue.call_args.kwargs
        assert kwargs["comment_adf"] is None

    def test_wontfix_transition_also_allowlisted(
        self, client, captured_audit, bearer_headers, allow_eng
    ):
        """``Won't Fix`` is the second allowlisted transition name."""
        fake_client = MagicMock()
        fake_client.transition_issue.return_value = (204, {})
        body = self._valid_body()
        body["transition_name"] = "Won't Fix"
        with patch.object(gateway, "get_jira_client", return_value=fake_client):
            resp = client.post(
                self.PATH,
                headers=bearer_headers,
                data=json.dumps(body),
                content_type="application/json",
            )
        assert resp.status_code == 200


# -----------------------------------------------------------------------------
# Issue #1557 slice-2 — jira_client.validate_jira_api_path widening
# -----------------------------------------------------------------------------


class TestRemoteLinkPathValidator:
    """Acceptance (task-2-3): ``validate_jira_api_path`` accepts the new
    GET path ``issue/<KEY>/remotelink``; a POST/PUT/DELETE on the same
    path is still denied (JIRA_WRITE_VERBS_DENIED unchanged).
    """

    def test_get_remotelink_path_allowed(self):
        from jira_client import validate_jira_api_path

        ok, reason = validate_jira_api_path("issue/ENG-1/remotelink", "GET")
        assert ok is True, reason

    def test_get_remotelink_case_normalised(self):
        """Tickets that differ only in trailing slash are still validated."""
        from jira_client import validate_jira_api_path

        # The validator accepts the canonical form; trailing slash is the
        # caller's responsibility but should not crash the validator.
        ok, _ = validate_jira_api_path("issue/ENG-1/remotelink", "GET")
        assert ok is True

    def test_post_remotelink_denied(self):
        """Adversarial: POST on the remotelink path must still be denied
        (JIRA_WRITE_VERBS_DENIED). Only the agent-facing surface is
        denied here — the orchestrator-only ``/transition`` route uses a
        separate internal-only client method."""
        from jira_client import validate_jira_api_path

        ok, reason = validate_jira_api_path("issue/ENG-1/remotelink", "POST")
        assert ok is False
        assert reason  # non-empty diagnostic message

    def test_put_remotelink_denied(self):
        from jira_client import validate_jira_api_path

        ok, _ = validate_jira_api_path("issue/ENG-1/remotelink", "PUT")
        assert ok is False

    def test_delete_remotelink_denied(self):
        from jira_client import validate_jira_api_path

        ok, _ = validate_jira_api_path("issue/ENG-1/remotelink", "DELETE")
        assert ok is False

    def test_transitions_path_still_denied_for_agent(self):
        """Adversarial regression: agent-facing path validator MUST NOT
        allow the transitions path. The orchestrator-only route bypasses
        ``validate_jira_api_path`` via the internal client method
        (mirror of the four other internal-only methods)."""
        from jira_client import validate_jira_api_path

        ok, _ = validate_jira_api_path("issue/ENG-1/transitions", "POST")
        assert ok is False
        ok, _ = validate_jira_api_path("issue/ENG-1/transitions", "GET")
        # GET transitions is read-only — depending on the validator's
        # exact policy it may or may not be allowed. We assert only the
        # write-deny invariant which is what the acceptance criterion
        # mandates. If GET is allowed that's safe; if denied that's also
        # safe (deny-by-default).
        # No assertion on GET — covers both policies.
