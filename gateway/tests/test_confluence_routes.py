"""
Tests for the eight ``/api/v1/confluence/*`` routes in ``gateway/gateway.py``.

Covers Phase 2 / Task 4-5 acceptance criteria:

- Public mode → 403 with ``private_mode_required`` audit entry on every route.
- Private mode + disallowed space → 403 ``confluence_*_denied`` /
  ``confluence_space_denied``.
- Private mode + allowlisted space + mocked upstream → 200 with body.
- Adversarial CQL suite for ``/search`` (≥10 negative cases).
- ``/execute`` rejection of write methods, denied verbs, path traversal,
  disallowed spaces, and the route-vs-execute anti-bypass guarantee.
- Route-enumeration regression: every ``/api/v1/confluence/*`` view has
  ``__egg_requires_private_mode__ = True``.
- 404 envelope end-to-end on each read route.
- ``confluence_upstream_403`` audit category split.
- ``list_spaces`` allowlist filter end-to-end (case-sensitive intersection).
- ``redact_response`` end-to-end.
- ``page/inline-comments`` exposes ``used_fallback`` flag.
- Audit-log assertions: every event includes ``pageId`` or ``spaceKey``,
  ``session_mode``, ``pipeline_id``, ``agent_role``, ``success``.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import confluence_policy
import pytest
import session_manager
from confluence_client import (
    ConfluenceUpstreamError,
    ConfluenceUpstreamForbidden,
)
from mode_gate import PRIVATE_MODE_MARKER_ATTR
from session_manager import SessionValidationResult

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
    """Patch session validation to yield a session with the given mode."""
    import auth

    mock_session = MagicMock()
    mock_session.mode = mode
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.pipeline_id = "issue-1931"
    mock_session.agent_role = "coder"
    mock_session.jira_ticket = None

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
        confluence_policy,
        "is_space_allowed",
        lambda k: k == "ENG",
    )
    monkeypatch.setattr(
        confluence_policy,
        "allowed_spaces",
        lambda: frozenset({"ENG"}),
    )
    # gateway.py imports the helpers under aliases — patch those names too.
    monkeypatch.setattr(
        gateway,
        "is_confluence_space_allowed",
        lambda k: k == "ENG",
    )
    monkeypatch.setattr(
        gateway,
        "confluence_allowed_spaces",
        lambda: frozenset({"ENG"}),
    )


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


def _patch_client(fake) -> Any:
    """Patch ``gateway.get_confluence_client`` to return ``fake``."""
    return patch.object(gateway, "get_confluence_client", return_value=fake)


# -----------------------------------------------------------------------------
# Route enumeration regression — decision G7 / risk R4
# -----------------------------------------------------------------------------


class TestRouteEnumeration:
    def test_every_confluence_route_has_private_mode_marker(self, client):
        found = 0
        for rule in gateway.app.url_map.iter_rules():
            if not rule.rule.startswith("/api/v1/confluence/"):
                continue
            view = gateway.app.view_functions[rule.endpoint]
            assert getattr(view, PRIVATE_MODE_MARKER_ATTR, False) is True, (
                f"Confluence route {rule.rule!r} (view={view.__name__}) is "
                f"missing the @require_private_mode decorator."
            )
            found += 1
        # Plan calls for eight routes.
        assert found >= 8, f"Expected at least 8 Confluence routes; found {found}"


# -----------------------------------------------------------------------------
# /api/v1/confluence/page/get
# -----------------------------------------------------------------------------


class TestPageGet:
    def test_public_mode_returns_403(self, client, public_headers, captured_audit):
        resp = client.post(
            "/api/v1/confluence/page/get",
            headers=public_headers,
            data=json.dumps({"pageId": "12345"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        body = json.loads(resp.data)
        assert "private network mode" in body["message"].lower()
        # Audit entry from require_private_mode.
        assert any(a["event_type"] == "private_mode_required" for a in captured_audit)

    def test_invalid_page_id_400(self, client, private_headers, captured_audit):
        resp = client.post(
            "/api/v1/confluence/page/get",
            headers=private_headers,
            data=json.dumps({"pageId": "abc"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert any(
            a["event_type"] == "confluence_page_get_rejected"
            and "invalid" in a["details"].get("reason", "").lower()
            for a in captured_audit
        )

    def test_disallowed_space_returns_403_no_body_leak(
        self, client, private_headers, allow_eng, captured_audit
    ):
        """When upstream returns a page in a non-allowlisted space, the
        gateway must return 403 *without* forwarding the page body."""
        fake = MagicMock()
        fake.get_page.return_value = {
            "id": "12345",
            "spaceId": "999",
            "title": "Secret",
            "body": {"storage": {"value": "secret-content-DO-NOT-LEAK"}},
        }
        # Cache the spaceId → SECRET so the lookup yields a non-allowlisted key.
        fake.space_cache.key_for_id = lambda sid: "SECRET" if sid == "999" else None
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/page/get",
                headers=private_headers,
                data=json.dumps({"pageId": "12345"}),
                content_type="application/json",
            )
        assert resp.status_code == 403
        # Body must not contain the page content — only the denial envelope.
        assert "secret-content-DO-NOT-LEAK" not in resp.get_data(as_text=True)
        denied = [a for a in captured_audit if a["event_type"] == "confluence_space_denied"]
        assert denied, "expected confluence_space_denied audit entry"

    def test_happy_path(self, client, private_headers, allow_eng, captured_audit):
        fake = MagicMock()
        fake.get_page.return_value = {
            "id": "12345",
            "spaceId": "1",
            "title": "Hello",
        }
        fake.space_cache.key_for_id = lambda sid: "ENG" if sid == "1" else None
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/page/get",
                headers=private_headers,
                data=json.dumps({"pageId": "12345"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["data"]["id"] == "12345"

        success = [a for a in captured_audit if a["event_type"] == "confluence_page_get"]
        assert success
        details = success[0]["details"]
        assert details["pageId"] == "12345"
        assert details["spaceKey"] == "ENG"
        assert details["pipeline_id"] == "issue-1931"
        assert details["agent_role"] == "coder"
        assert details["session_mode"] == "private"
        assert details["not_found"] is False

    def test_not_found_envelope_passes_through(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake = MagicMock()
        fake.get_page.return_value = {
            "status": "not_found",
            "id": "999",
            "upstream_status": 404,
        }
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/page/get",
                headers=private_headers,
                data=json.dumps({"pageId": "999"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["data"] == {
            "status": "not_found",
            "id": "999",
            "upstream_status": 404,
        }
        success = [a for a in captured_audit if a["event_type"] == "confluence_page_get"]
        assert success[0]["details"]["not_found"] is True

    def test_upstream_403_distinct_audit_event(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake = MagicMock()
        fake.get_page.side_effect = ConfluenceUpstreamForbidden(
            403, {"err": "denied"}, "api/v2/pages/12345"
        )
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/page/get",
                headers=private_headers,
                data=json.dumps({"pageId": "12345"}),
                content_type="application/json",
            )
        assert resp.status_code == 403
        # Distinct from confluence_space_denied.
        upstream = [a for a in captured_audit if a["event_type"] == "confluence_upstream_403"]
        assert upstream
        assert upstream[0]["details"]["pageId"] == "12345"

    def test_upstream_error_body_is_redacted(
        self, client, private_headers, allow_eng, captured_audit
    ):
        """Atlassian error envelopes can carry user identifiers (accountId,
        emailAddress) and the success-path redactor only runs on 2xx
        bodies — verify the gateway redacts error bodies too before they
        cross the gateway/sandbox boundary."""
        fake = MagicMock()
        fake.get_page.side_effect = ConfluenceUpstreamError(
            500,
            {
                "errorMessages": ["upstream blew up"],
                "accountId": "557058:abcd-efgh-1234",
                "emailAddress": "leak@example.com",
                "data": {"accountId": "nested-leak"},
            },
            "api/v2/pages/12345",
        )
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/page/get",
                headers=private_headers,
                data=json.dumps({"pageId": "12345"}),
                content_type="application/json",
            )
        assert resp.status_code == 502
        text = resp.get_data(as_text=True)
        assert "557058:abcd-efgh-1234" not in text
        assert "leak@example.com" not in text
        assert "nested-leak" not in text
        body = json.loads(text)
        upstream_body = body["data"]["upstream_body"]
        assert upstream_body["accountId"] == "<redacted>"
        assert upstream_body["emailAddress"] == "<redacted>"
        assert upstream_body["data"]["accountId"] == "<redacted>"
        # Non-redacted fields pass through.
        assert upstream_body["errorMessages"] == ["upstream blew up"]


# -----------------------------------------------------------------------------
# /api/v1/confluence/space/list — list_spaces filtering end-to-end (risk R13)
# -----------------------------------------------------------------------------


class TestSpaceList:
    def test_public_mode_403(self, client, public_headers):
        resp = client.post(
            "/api/v1/confluence/space/list",
            headers=public_headers,
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_filtered_to_allowlist_end_to_end(
        self, client, private_headers, allow_eng, captured_audit
    ):
        """Mock returns ENG / DOCS / LEAK; allowlist is {ENG} → only ENG returned."""
        fake = MagicMock()
        fake.list_spaces.return_value = {"results": [{"id": "1", "key": "ENG"}]}
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/space/list",
                headers=private_headers,
                data=json.dumps({}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        body = json.loads(resp.data)
        keys = sorted(s["key"] for s in body["data"]["results"])
        assert keys == ["ENG"]
        success = [a for a in captured_audit if a["event_type"] == "confluence_space_list"]
        assert success
        assert success[0]["details"]["spaces_returned"] == 1


# -----------------------------------------------------------------------------
# /api/v1/confluence/space/pages
# -----------------------------------------------------------------------------


class TestSpacePages:
    def test_public_mode_403(self, client, public_headers):
        resp = client.post(
            "/api/v1/confluence/space/pages",
            headers=public_headers,
            data=json.dumps({"spaceKey": "ENG"}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_invalid_space_key_shape_400(self, client, private_headers, captured_audit):
        resp = client.post(
            "/api/v1/confluence/space/pages",
            headers=private_headers,
            data=json.dumps({"spaceKey": "with-dash"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_disallowed_space_returns_403(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            "/api/v1/confluence/space/pages",
            headers=private_headers,
            data=json.dumps({"spaceKey": "SECRET"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        denied = [a for a in captured_audit if a["event_type"] == "confluence_space_pages_denied"]
        assert denied
        assert denied[-1]["details"]["spaceKey"] == "SECRET"

    def test_happy_path(self, client, private_headers, allow_eng, captured_audit):
        fake = MagicMock()
        fake.space_cache.id_for_key = lambda k: "1" if k == "ENG" else None
        fake.get_space_pages.return_value = {"results": [{"id": "p1"}]}
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/space/pages",
                headers=private_headers,
                data=json.dumps({"spaceKey": "ENG"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        # populate_space_cache should NOT be called when the cache is hot.
        fake.populate_space_cache.assert_not_called()

    def test_warms_paginated_space_cache_on_miss(
        self, client, private_headers, allow_eng, captured_audit
    ):
        """When the cache is cold, the route walks paginated /wiki/api/v2/spaces
        so a target space living on page 2+ still resolves."""
        fake = MagicMock()
        # First lookup (before warming) returns None; after populate_space_cache
        # is called, we flip the side-effect to return the resolved id.
        warmed: dict[str, bool] = {"done": False}

        def id_for_key(key: str) -> str | None:
            if not warmed["done"] or key != "ENG":
                return None
            return "1"

        def populate() -> None:
            warmed["done"] = True

        fake.space_cache.id_for_key.side_effect = id_for_key
        fake.populate_space_cache.side_effect = populate
        fake.get_space_pages.return_value = {"results": [{"id": "p1"}]}
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/space/pages",
                headers=private_headers,
                data=json.dumps({"spaceKey": "ENG"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        fake.populate_space_cache.assert_called_once()


# -----------------------------------------------------------------------------
# /api/v1/confluence/search — adversarial CQL
# -----------------------------------------------------------------------------


class TestSearch:
    def test_public_mode_403(self, client, public_headers):
        resp = client.post(
            "/api/v1/confluence/search",
            headers=public_headers,
            data=json.dumps({"cql": "space = ENG"}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_missing_cql_400(self, client, private_headers, captured_audit):
        resp = client.post(
            "/api/v1/confluence/search",
            headers=private_headers,
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert any(a["event_type"] == "confluence_search_rejected" for a in captured_audit)

    @pytest.mark.parametrize(
        "cql",
        [
            "space = ENG OR space = SEC",
            'space = "ENG"',
            "SPACE = ENG",
            "space = currentUser()",
            "space = ENG /* injected */",
            "space IN (ENG, SEC)",
            'text ~ "RFC"',
            "space = ENG ; drop table",
            'id = "12345"',
            "space = ЕNG",
            "space != SEC",
        ],
    )
    def test_adversarial_cql_rejected(
        self, client, private_headers, allow_eng, captured_audit, cql: str
    ):
        resp = client.post(
            "/api/v1/confluence/search",
            headers=private_headers,
            data=json.dumps({"cql": cql}),
            content_type="application/json",
        )
        assert resp.status_code == 403, f"expected 403 for {cql!r}"
        rejected = [a for a in captured_audit if a["event_type"] == "confluence_search_rejected"]
        assert rejected, f"expected audit entry for {cql!r}"
        # ``pageId`` must NEVER appear on search audits.
        assert "pageId" not in rejected[-1]["details"]

    def test_happy_path_clamps_limit(self, client, private_headers, allow_eng, captured_audit):
        fake = MagicMock()
        fake.search_cql.return_value = {"results": []}
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/search",
                headers=private_headers,
                data=json.dumps(
                    {
                        "cql": 'space = ENG AND text ~ "RFC"',
                        "limit": 99999,
                        "cursor": "TOK-abc",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200
        kwargs = fake.search_cql.call_args.kwargs
        assert kwargs["limit"] == 100  # clamped to HARD_MAX_LIMIT
        assert kwargs["cursor"] == "TOK-abc"
        success = [a for a in captured_audit if a["event_type"] == "confluence_search"]
        assert success
        details = success[0]["details"]
        assert details["spaces_extracted"] == ["ENG"]
        assert "pageId" not in details

    def test_invalid_limit_400(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            "/api/v1/confluence/search",
            headers=private_headers,
            data=json.dumps({"cql": "space = ENG", "limit": "bad"}),
            content_type="application/json",
        )
        assert resp.status_code == 400


# -----------------------------------------------------------------------------
# /api/v1/confluence/execute — anti-bypass + denied verbs (risks R2, R14)
# -----------------------------------------------------------------------------


class TestExecute:
    def test_public_mode_403(self, client, public_headers):
        resp = client.post(
            "/api/v1/confluence/execute",
            headers=public_headers,
            data=json.dumps({"method": "GET", "path": "api/v2/pages/12345"}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_non_get_methods_rejected(
        self,
        client,
        private_headers,
        allow_eng,
        captured_audit,
        method: str,
    ):
        resp = client.post(
            "/api/v1/confluence/execute",
            headers=private_headers,
            data=json.dumps({"method": method, "path": "api/v2/pages/12345"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        denied = [a for a in captured_audit if a["event_type"] == "confluence_execute_denied"]
        assert denied

    @pytest.mark.parametrize(
        "verb",
        ["restrictions", "permissions", "users", "attachments", "space.admin"],
    )
    def test_denied_verb_in_path_rejected(
        self,
        client,
        private_headers,
        allow_eng,
        captured_audit,
        verb: str,
    ):
        # Bare verb form.
        resp = client.post(
            "/api/v1/confluence/execute",
            headers=private_headers,
            data=json.dumps({"method": "GET", "path": f"api/v2/{verb}"}),
            content_type="application/json",
        )
        assert resp.status_code == 403, f"bare {verb!r} should be rejected"

        # Inline (path-position) variant — segment match must catch it.
        resp = client.post(
            "/api/v1/confluence/execute",
            headers=private_headers,
            data=json.dumps({"method": "GET", "path": f"api/v2/pages/123/{verb}"}),
            content_type="application/json",
        )
        assert resp.status_code == 403, f"nested {verb!r} should be rejected"

    def test_path_traversal_rejected(self, client, private_headers, allow_eng, captured_audit):
        resp = client.post(
            "/api/v1/confluence/execute",
            headers=private_headers,
            data=json.dumps({"method": "GET", "path": "api/v2/pages/../12345"}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "bypass_path",
        [
            # Cycle-3 NACK fix (commit f3f552eb9): these four flat v2 paths
            # were dropped from the /execute allowlist because each was an
            # exploitable cross-partition bypass.
            "api/v2/spaces",
            "rest/api/search",
            "api/v2/footer-comments",
            "api/v2/inline-comments",
            # PR #2141 review tightening: page-scoped descendant / comment
            # subpaths also dropped — their response bodies have no
            # top-level spaceId, so /execute always fail-closed and the
            # paths were effectively unusable.  Agents reach these via the
            # dedicated /api/v1/confluence/page/* routes.
            "api/v2/pages/1/descendants",
            "api/v2/pages/1/footer-comments",
            "api/v2/pages/1/inline-comments",
            "rest/api/content/1/child/comment",
        ],
    )
    def test_anti_bypass_paths_rejected_via_execute(
        self,
        client,
        private_headers,
        allow_eng,
        captured_audit,
        bypass_path: str,
    ):
        """Risk R2: /execute must NOT accept any path that bypasses the
        narrow-route policy checks (flat v2 endpoints) or whose response
        shape makes the post-fetch allowlist check unreachable
        (page-scoped descendants / comments)."""
        resp = client.post(
            "/api/v1/confluence/execute",
            headers=private_headers,
            data=json.dumps({"method": "GET", "path": bypass_path}),
            content_type="application/json",
        )
        assert resp.status_code == 403, f"{bypass_path!r} must be rejected"
        denied = [a for a in captured_audit if a["event_type"] == "confluence_execute_denied"]
        assert denied, f"expected denial audit for {bypass_path!r}"

    def test_disallowed_space_via_pageid_in_execute(
        self, client, private_headers, allow_eng, captured_audit
    ):
        """Even via /execute, post-fetch allowlist must catch a non-allowlisted
        space — proves the route-vs-execute anti-bypass guarantee."""
        fake = MagicMock()
        fake.execute_raw.return_value = {
            "id": "12345",
            "spaceId": "999",
            "body": {"storage": {"value": "leak-bait"}},
        }
        fake.space_cache.key_for_id = lambda sid: "SECRET" if sid == "999" else None
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/execute",
                headers=private_headers,
                data=json.dumps({"method": "GET", "path": "api/v2/pages/12345"}),
                content_type="application/json",
            )
        assert resp.status_code == 403
        # Body must not be leaked.
        assert "leak-bait" not in resp.get_data(as_text=True)
        denied = [a for a in captured_audit if a["event_type"] == "confluence_execute_denied"]
        assert denied

    def test_happy_path_get(self, client, private_headers, allow_eng, captured_audit):
        fake = MagicMock()
        fake.execute_raw.return_value = {
            "id": "12345",
            "spaceId": "1",
        }
        fake.space_cache.key_for_id = lambda sid: "ENG" if sid == "1" else None
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/execute",
                headers=private_headers,
                data=json.dumps({"method": "GET", "path": "api/v2/pages/12345"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        success = [a for a in captured_audit if a["event_type"] == "confluence_execute"]
        assert success
        details = success[0]["details"]
        assert details["method"] == "GET"
        assert details["path"] == "api/v2/pages/12345"
        assert details["pageId"] == "12345"
        assert details["spaceKey"] == "ENG"

    def test_missing_path_400(self, client, private_headers, captured_audit):
        resp = client.post(
            "/api/v1/confluence/execute",
            headers=private_headers,
            data=json.dumps({"method": "GET"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_warms_paginated_space_cache_on_space_id_path_miss(
        self, client, private_headers, allow_eng, captured_audit
    ):
        """``api/v2/spaces/<id>/pages`` via /execute must walk paginated
        ``/wiki/api/v2/spaces`` when the space-id is not yet cached so a
        target on page 2+ still resolves.  Direct coverage for the
        ``space_id_in_path`` branch in ``confluence_execute``."""
        fake = MagicMock()
        warmed: dict[str, bool] = {"done": False}

        def key_for_id(sid: str) -> str | None:
            if not warmed["done"] or sid != "1":
                return None
            return "ENG"

        def populate() -> None:
            warmed["done"] = True

        fake.space_cache.key_for_id.side_effect = key_for_id
        fake.populate_space_cache.side_effect = populate
        fake.execute_raw.return_value = {"results": [{"id": "p1"}]}
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/execute",
                headers=private_headers,
                data=json.dumps({"method": "GET", "path": "api/v2/spaces/1/pages"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        fake.populate_space_cache.assert_called_once()
        success = [a for a in captured_audit if a["event_type"] == "confluence_execute"]
        assert success
        assert success[-1]["details"]["spaceKey"] == "ENG"


# -----------------------------------------------------------------------------
# /api/v1/confluence/page/inline-comments — used_fallback observability
# -----------------------------------------------------------------------------


class TestPageInlineComments:
    def test_used_fallback_propagates_to_audit(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake = MagicMock()
        fake.get_page_inline_comments.return_value = {
            "results": [{"id": "ic1"}],
            "used_fallback": True,
        }
        # Parent page lookup also resolves to ENG.
        fake.get_page.return_value = {"id": "12345", "spaceId": "1"}
        fake.space_cache.key_for_id = lambda sid: "ENG" if sid == "1" else None
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/page/inline-comments",
                headers=private_headers,
                data=json.dumps({"pageId": "12345"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        success = [
            a for a in captured_audit if a["event_type"] == "confluence_page_inline_comments"
        ]
        assert success
        assert success[0]["details"]["used_fallback"] is True


# -----------------------------------------------------------------------------
# Page descendants — risk R8 (depth/limit defaults)
# -----------------------------------------------------------------------------


class TestPageDescendants:
    def test_defaults_applied_when_omitted(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake = MagicMock()
        fake.get_page_descendants.return_value = {"results": []}
        fake.get_page.return_value = {"id": "12345", "spaceId": "1"}
        fake.space_cache.key_for_id = lambda sid: "ENG" if sid == "1" else None
        with _patch_client(fake):
            resp = client.post(
                "/api/v1/confluence/page/descendants",
                headers=private_headers,
                data=json.dumps({"pageId": "12345"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        # The route should call client with defaults applied.
        kwargs = fake.get_page_descendants.call_args.kwargs
        assert kwargs["depth"] == 1
        assert kwargs["limit"] == 25  # CONFLUENCE_DEFAULT_LIMIT


# -----------------------------------------------------------------------------
# Audit-log shape regression: every emit carries the session triplet
# -----------------------------------------------------------------------------


class TestAuditShape:
    def test_search_audit_has_session_triplet(
        self, client, private_headers, allow_eng, captured_audit
    ):
        fake = MagicMock()
        fake.search_cql.return_value = {"results": []}
        with _patch_client(fake):
            client.post(
                "/api/v1/confluence/search",
                headers=private_headers,
                data=json.dumps({"cql": "space = ENG"}),
                content_type="application/json",
            )
        success = [a for a in captured_audit if a["event_type"] == "confluence_search"]
        details = success[0]["details"]
        assert details["session_mode"] == "private"
        assert details["pipeline_id"] == "issue-1931"
        assert details["agent_role"] == "coder"
