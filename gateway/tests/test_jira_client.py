"""
Tests for gateway/jira_client.py.

Covers:
- URL / header / body construction per method, using ``httpx.MockTransport``
- Default ``expand=renderedBody,renderedFields`` on ``get_ticket`` /
  ``get_comments``
- ``validate_jira_api_path`` positive + negative (transitions, worklog,
  attachments, watchers, DELETE/PUT/PATCH, ``..``, duplicate slashes,
  non-ASCII, unknown paths)
- ``search.jql`` pagination (``nextPageToken`` round-trip) and ``maxResults``
  clamping
- 429 single-retry honouring ``Retry-After`` (capped at 30s); write verbs
  don't retry (future-safety)
- 404 envelope for ``get_ticket`` / ``get_comments`` (dict return, no raise);
  ``execute_raw`` / ``search`` still raise ``JiraUpstreamError`` on 404
- ``validate_fields`` (32-cap, regex, None → [])
"""

from __future__ import annotations

from typing import Any

import httpx

# Modules loaded via conftest.
import jira_client
import pytest
from jira_client import (
    HARD_MAX_RESULTS,
    MAX_FIELDS,
    JiraClient,
    JiraUpstreamError,
    validate_fields,
    validate_jira_api_path,
)
from jira_credentials import JiraCredentials

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def fake_creds() -> JiraCredentials:
    return JiraCredentials(
        base_url="https://example.atlassian.net",
        username="alice@example.com",
        api_token="atk-xyz",
    )


@pytest.fixture
def captured_requests() -> list[httpx.Request]:
    """Collector fixture the mock transport appends to."""
    return []


def _make_client(
    handler,
    creds: JiraCredentials,
) -> JiraClient:
    """Build a JiraClient whose upstream HTTP lands in ``handler``."""
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return JiraClient(
        creds_provider=lambda: creds,
        http_client=http,
    )


# -----------------------------------------------------------------------------
# validate_jira_api_path
# -----------------------------------------------------------------------------


class TestValidateJiraApiPath:
    """Path + method allowlist behaviour."""

    @pytest.mark.parametrize(
        "path",
        [
            "issue/FOO-1",
            "issue/FOO-123",
            "issue/FOO-1/comment",
            "issue/A1-7",
            "issue/PROJ_X-42",
            "project",
            "project/FOO",
            "project/ENG",
            "project/PROJ_X",
        ],
    )
    def test_positive_get_paths(self, path: str):
        ok, reason = validate_jira_api_path(path, "GET")
        assert ok, f"{path!r} should have been accepted: {reason}"

    def test_search_jql_removed_from_execute_allowlist(self):
        """Cycle-2 fix: ``search/jql`` is intentionally NOT in the execute
        allowlist so ``POST /api/v1/jira/execute`` cannot bypass the JQL
        project-scope extractor (see commit 7895474bb).  The dedicated
        ``/api/v1/jira/search`` route remains the only path to Atlassian's
        JQL search."""
        ok, reason = validate_jira_api_path("search/jql", "GET")
        assert not ok
        assert "allowlist" in reason.lower()

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", ""])
    def test_non_get_methods_rejected(self, method: str):
        ok, reason = validate_jira_api_path("issue/FOO-1", method)
        assert not ok
        assert "not allowed" in reason.lower() or "denied" in reason.lower()

    @pytest.mark.parametrize(
        "bad_segment",
        ["transitions", "worklog", "attachments", "watchers"],
    )
    def test_denied_verb_in_path(self, bad_segment: str):
        ok, reason = validate_jira_api_path(f"issue/FOO-1/{bad_segment}", "GET")
        assert not ok
        assert bad_segment in reason

    def test_path_traversal_rejected(self):
        ok, reason = validate_jira_api_path("issue/../FOO-1", "GET")
        assert not ok
        assert ".." in reason

    def test_duplicate_slashes_rejected(self):
        ok, reason = validate_jira_api_path("issue//FOO-1", "GET")
        assert not ok
        assert "duplicate" in reason.lower() or "slash" in reason.lower()

    def test_leading_double_slash_rejected(self):
        """Bug-fix regression: ``//issue/FOO-1`` normalises to a valid shape
        but must still be rejected (Phase 3-3 fix in commit 02dfb306e)."""
        ok, reason = validate_jira_api_path("//issue/FOO-1", "GET")
        assert not ok

    def test_non_ascii_rejected(self):
        # Cyrillic 'A' (U+0410) looks like Latin 'A' but is unicode — must fail.
        ok, reason = validate_jira_api_path("issue/АBC-1", "GET")
        assert not ok
        assert "ascii" in reason.lower() or "non-" in reason.lower()

    def test_empty_path_rejected(self):
        ok, _ = validate_jira_api_path("", "GET")
        assert not ok
        ok, _ = validate_jira_api_path("/", "GET")
        assert not ok

    def test_non_string_path_rejected(self):
        ok, _ = validate_jira_api_path(None, "GET")  # type: ignore[arg-type]
        assert not ok

    def test_query_string_is_stripped_before_check(self):
        ok, _ = validate_jira_api_path("issue/FOO-1?foo=bar", "GET")
        assert ok

    def test_random_unknown_path_rejected(self):
        ok, _ = validate_jira_api_path("whoami", "GET")
        assert not ok
        ok, _ = validate_jira_api_path("serverInfo", "GET")
        assert not ok


# -----------------------------------------------------------------------------
# validate_fields
# -----------------------------------------------------------------------------


class TestValidateFields:
    def test_none_returns_empty_list(self):
        assert validate_fields(None) == []

    def test_empty_list_returns_empty(self):
        assert validate_fields([]) == []

    def test_valid_fields_pass_through(self):
        result = validate_fields(["summary", "status", "assignee"])
        assert result == ["summary", "status", "assignee"]

    def test_dotted_names_allowed(self):
        assert validate_fields(["custom.one", "a-b", "a_b"]) == [
            "custom.one",
            "a-b",
            "a_b",
        ]

    def test_reject_over_max_fields(self):
        too_many = [f"f{i}" for i in range(MAX_FIELDS + 1)]
        with pytest.raises(ValueError, match="exceeds maximum"):
            validate_fields(too_many)

    @pytest.mark.parametrize(
        "bad",
        ["1starts_with_digit", "has space", "has,comma", "has$dollar", ""],
    )
    def test_reject_bad_field_name(self, bad: str):
        with pytest.raises(ValueError):
            validate_fields(["summary", bad])

    def test_reject_non_string_entry(self):
        with pytest.raises(ValueError):
            validate_fields(["summary", 42])  # type: ignore[list-item]

    def test_reject_non_list(self):
        with pytest.raises(ValueError):
            validate_fields("summary")  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# JiraClient request plumbing
# -----------------------------------------------------------------------------


class TestGetTicket:
    def test_default_expand_is_rendered_body_and_rendered_fields(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"key": "FOO-1", "fields": {"summary": "hi"}})

        client = _make_client(handler, fake_creds)
        body = client.get_ticket("FOO-1")

        assert body == {"key": "FOO-1", "fields": {"summary": "hi"}}
        assert len(captured) == 1
        url = captured[0].url
        assert str(url).startswith("https://example.atlassian.net/rest/api/3/issue/FOO-1")
        assert url.params["expand"] == "renderedBody,renderedFields"
        # Basic auth header present
        assert captured[0].headers["authorization"].startswith("Basic ")
        # Accept JSON
        assert "application/json" in captured[0].headers["accept"]

    def test_explicit_expand_override(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"key": "FOO-1"})

        client = _make_client(handler, fake_creds)
        client.get_ticket("FOO-1", expand=[])
        assert "expand" not in captured[0].url.params

    def test_fields_passed_through(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"key": "FOO-1"})

        client = _make_client(handler, fake_creds)
        client.get_ticket("FOO-1", fields=["summary", "status"])
        assert captured[0].url.params["fields"] == "summary,status"

    def test_404_returns_not_found_envelope(self, fake_creds: JiraCredentials):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"errorMessages": ["does not exist"]})

        client = _make_client(handler, fake_creds)
        body = client.get_ticket("FOO-1")
        assert body == {"status": "not_found", "key": "FOO-1", "upstream_status": 404}

    def test_500_raises_upstream_error(self, fake_creds: JiraCredentials):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = _make_client(handler, fake_creds)
        with pytest.raises(JiraUpstreamError) as exc_info:
            client.get_ticket("FOO-1")
        assert exc_info.value.status_code == 500


class TestGetComments:
    def test_uses_expand_rendered_body(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"comments": []})

        client = _make_client(handler, fake_creds)
        client.get_comments("FOO-1")
        url = captured[0].url
        assert str(url).endswith("/issue/FOO-1/comment?expand=renderedBody")

    def test_404_returns_envelope(self, fake_creds: JiraCredentials):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = _make_client(handler, fake_creds)
        assert client.get_comments("FOO-1") == {
            "status": "not_found",
            "key": "FOO-1",
            "upstream_status": 404,
        }


class TestSearch:
    def test_posts_to_search_jql_with_body(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"issues": [], "nextPageToken": None},
            )

        client = _make_client(handler, fake_creds)
        client.search("project = ENG", fields=["summary"], max_results=25)

        assert len(captured) == 1
        assert captured[0].method == "POST"
        assert str(captured[0].url).endswith("/rest/api/3/search/jql")
        import json as _json

        body = _json.loads(captured[0].content)
        assert body["jql"] == "project = ENG"
        assert body["fields"] == ["summary"]
        assert body["maxResults"] == 25

    def test_next_page_token_round_trips(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"issues": []})

        client = _make_client(handler, fake_creds)
        client.search("project = ENG", next_page_token="TOK-1")

        import json as _json

        body = _json.loads(captured[0].content)
        assert body["nextPageToken"] == "TOK-1"

    def test_max_results_clamped_to_hard_max(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"issues": []})

        client = _make_client(handler, fake_creds)
        client.search("project = ENG", max_results=9999)

        import json as _json

        body = _json.loads(captured[0].content)
        assert body["maxResults"] == HARD_MAX_RESULTS

    def test_missing_jql_raises(self, fake_creds: JiraCredentials):
        client = _make_client(lambda _r: httpx.Response(200, json={}), fake_creds)
        with pytest.raises(ValueError):
            client.search("")
        with pytest.raises(ValueError):
            client.search("   ")

    def test_search_404_raises_upstream_error(self, fake_creds: JiraCredentials):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = _make_client(handler, fake_creds)
        with pytest.raises(JiraUpstreamError) as exc_info:
            client.search("project = ENG")
        assert exc_info.value.status_code == 404


class TestExecuteRaw:
    def test_404_raises_not_envelope(self, fake_creds: JiraCredentials):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = _make_client(handler, fake_creds)
        with pytest.raises(JiraUpstreamError) as exc_info:
            client.execute_raw("GET", "project/ENG")
        assert exc_info.value.status_code == 404

    def test_happy_path(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"key": "ENG"})

        client = _make_client(handler, fake_creds)
        body = client.execute_raw("GET", "project/ENG")
        assert body == {"key": "ENG"}
        assert captured[0].method == "GET"


class Test429Retry:
    def test_get_retries_once_on_429(
        self, fake_creds: JiraCredentials, monkeypatch: pytest.MonkeyPatch
    ):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"key": "FOO-1"})

        # Patch time.sleep so the test doesn't actually wait.
        slept: list[float] = []
        monkeypatch.setattr(jira_client.time, "sleep", lambda s: slept.append(s))

        client = _make_client(handler, fake_creds)
        body = client.get_ticket("FOO-1")
        assert body == {"key": "FOO-1"}
        assert calls["n"] == 2
        assert slept == [1]

    def test_retry_after_clamped(
        self, fake_creds: JiraCredentials, monkeypatch: pytest.MonkeyPatch
    ):
        """``Retry-After: 600`` must be clamped — the client caps at 30s so a
        pathological value can't lock up a worker for minutes."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "600"})
            return httpx.Response(200, json={"ok": True})

        slept: list[float] = []
        monkeypatch.setattr(jira_client.time, "sleep", lambda s: slept.append(s))

        client = _make_client(handler, fake_creds)
        client.get_ticket("FOO-1")
        assert slept == [jira_client._RETRY_AFTER_CAP_SECONDS]

    def test_second_429_is_passed_through(
        self, fake_creds: JiraCredentials, monkeypatch: pytest.MonkeyPatch
    ):
        """Two back-to-back 429s surface as ``JiraUpstreamError(status=429)``."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "1"})

        monkeypatch.setattr(jira_client.time, "sleep", lambda _s: None)
        client = _make_client(handler, fake_creds)
        with pytest.raises(JiraUpstreamError) as exc_info:
            client.get_ticket("FOO-1")
        assert exc_info.value.status_code == 429

    def test_non_get_does_not_retry(
        self, fake_creds: JiraCredentials, monkeypatch: pytest.MonkeyPatch
    ):
        """Write verbs never retry (future-safety).  The ``search`` route is
        POST — if we ever see a 429 there, we surface it immediately."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "1"})

        monkeypatch.setattr(jira_client.time, "sleep", lambda _s: None)
        client = _make_client(handler, fake_creds)
        with pytest.raises(JiraUpstreamError):
            client.search("project = ENG")
        assert calls["n"] == 1

    @pytest.mark.parametrize(
        "value, expected",
        [
            (None, jira_client._DEFAULT_RETRY_AFTER_SECONDS),
            ("", jira_client._DEFAULT_RETRY_AFTER_SECONDS),
            ("not-a-number", jira_client._DEFAULT_RETRY_AFTER_SECONDS),
            ("0", jira_client._DEFAULT_RETRY_AFTER_SECONDS),
            ("-5", jira_client._DEFAULT_RETRY_AFTER_SECONDS),
            ("2", 2),
            ("9999", jira_client._RETRY_AFTER_CAP_SECONDS),
        ],
    )
    def test_parse_retry_after_values(self, value: Any, expected: int):
        assert jira_client._parse_retry_after(value) == expected


class TestClientAuthHeader:
    """Every request carries a Basic-auth header derived from credentials."""

    def test_auth_header_on_each_request(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        client = _make_client(handler, fake_creds)
        client.get_ticket("FOO-1")
        client.get_comments("FOO-1")
        client.search("project = ENG")
        client.execute_raw("GET", "project/ENG")

        assert len(captured) == 4
        for req in captured:
            assert req.headers["authorization"] == fake_creds.basic_auth_header()


class TestSingletonLifecycle:
    """`get_jira_client()` / `reset_jira_client()` produce a consistent handle."""

    def test_singleton_returns_same_instance(self):
        jira_client.reset_jira_client()
        a = jira_client.get_jira_client()
        b = jira_client.get_jira_client()
        assert a is b

        jira_client.reset_jira_client()
        c = jira_client.get_jira_client()
        assert c is not a
