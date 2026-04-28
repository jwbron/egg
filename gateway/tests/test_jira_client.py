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
            "project/FOO",
            "project/ENG",
            "project/PROJ_X",
        ],
    )
    def test_positive_get_paths(self, path: str):
        ok, reason = validate_jira_api_path(path, "GET")
        assert ok, f"{path!r} should have been accepted: {reason}"

    def test_bare_project_removed_from_execute_allowlist(self):
        """Bare ``project`` path returns ALL projects visible to the API
        token, bypassing the project allowlist.  Only ``project/<KEY>``
        is permitted (reviewer_code finding #2)."""
        ok, reason = validate_jira_api_path("project", "GET")
        assert not ok
        assert "allowlist" in reason.lower()

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


# =============================================================================
# Write verbs (issue #1924)
#
# The four ``JiraClient`` write methods bypass ``validate_jira_api_path`` and
# call ``_request`` directly with hardcoded paths.  These tests assert the
# exact wire shape the gateway sends, the idempotency-cache wiring, the
# ``notifyUsers`` dispatch on ``edit_issue``, and the 429-audit-emit
# behaviour required by refine feedback Q1.
# =============================================================================


import json as _wire_json  # noqa: E402 — used only by the write-method tests


@pytest.fixture
def reset_idempotency_cache():
    """Idempotency cache is module-level — wipe it before / after each
    test that exercises the cache to avoid cross-test bleed."""
    import jira_idempotency

    jira_idempotency.clear_cache()
    yield
    jira_idempotency.clear_cache()


def _decode_request_json(request: httpx.Request) -> dict:
    return _wire_json.loads(request.content)


# -----------------------------------------------------------------------------
# create_issue
# -----------------------------------------------------------------------------


class TestCreateIssue:
    def test_minimal_body_shape(self, fake_creds: JiraCredentials, reset_idempotency_cache):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                201,
                json={"id": "10001", "key": "ENG-1", "self": "https://e/rest/api/3/issue/10001"},
            )

        client = _make_client(handler, fake_creds)
        status, body = client.create_issue(
            project_key="ENG",
            issuetype="Task",
            summary="hello",
        )

        assert status == 201
        assert body["key"] == "ENG-1"
        assert len(captured) == 1
        assert captured[0].method == "POST"
        assert str(captured[0].url).endswith("/rest/api/3/issue")
        wire = _decode_request_json(captured[0])
        assert wire == {
            "fields": {
                "project": {"key": "ENG"},
                "summary": "hello",
                "issuetype": {"name": "Task"},
            }
        }

    def test_string_issuetype_normalised_to_dict(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "1", "key": "ENG-1"})

        client = _make_client(handler, fake_creds)
        client.create_issue(project_key="ENG", issuetype="Story", summary="x")
        wire = _decode_request_json(captured[0])
        assert wire["fields"]["issuetype"] == {"name": "Story"}

    def test_dict_issuetype_with_id(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "1", "key": "ENG-1"})

        client = _make_client(handler, fake_creds)
        client.create_issue(
            project_key="ENG",
            issuetype={"id": "10001"},
            summary="x",
        )
        wire = _decode_request_json(captured[0])
        assert wire["fields"]["issuetype"] == {"id": "10001"}

    def test_invalid_issuetype_raises(self, fake_creds: JiraCredentials):
        client = _make_client(lambda _r: httpx.Response(200, json={}), fake_creds)
        with pytest.raises(ValueError, match="issuetype"):
            client.create_issue(project_key="ENG", issuetype=42, summary="x")  # type: ignore[arg-type]

    def test_description_string_wrapped_to_adf(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "1", "key": "ENG-1"})

        client = _make_client(handler, fake_creds)
        client.create_issue(
            project_key="ENG",
            issuetype="Task",
            summary="hi",
            description="hello world",
        )
        wire = _decode_request_json(captured[0])
        desc = wire["fields"]["description"]
        # Plain text → minimal ADF doc.
        assert desc["type"] == "doc"
        assert desc["version"] == 1
        assert desc["content"][0]["content"][0]["text"] == "hello world"

    def test_description_adf_passes_through(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "1", "key": "ENG-1"})

        client = _make_client(handler, fake_creds)
        adf = {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": []}],
        }
        client.create_issue(
            project_key="ENG",
            issuetype="Task",
            summary="hi",
            description=adf,
        )
        wire = _decode_request_json(captured[0])
        assert wire["fields"]["description"] == adf

    def test_labels_serialised_as_list(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "1", "key": "ENG-1"})

        client = _make_client(handler, fake_creds)
        client.create_issue(
            project_key="ENG",
            issuetype="Task",
            summary="hi",
            labels=["a", "b"],
        )
        wire = _decode_request_json(captured[0])
        assert wire["fields"]["labels"] == ["a", "b"]

    def test_parent_emits_parent_block(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "1", "key": "ENG-1"})

        client = _make_client(handler, fake_creds)
        client.create_issue(
            project_key="ENG",
            issuetype="Sub-task",
            summary="x",
            parent="ENG-100",
        )
        wire = _decode_request_json(captured[0])
        assert wire["fields"]["parent"] == {"key": "ENG-100"}

    def test_epic_link_with_parent_dispatch(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "1", "key": "ENG-1"})

        client = _make_client(handler, fake_creds)
        client.create_issue(
            project_key="ENG",
            issuetype="Task",
            summary="x",
            epic_link="ENG-99",
            epic_link_field="parent",
        )
        wire = _decode_request_json(captured[0])
        # epic_link uses parent dispatch for next-gen sites.
        assert wire["fields"]["parent"] == {"key": "ENG-99"}
        assert "customfield_10014" not in wire["fields"]

    def test_epic_link_with_customfield_dispatch(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "1", "key": "ENG-1"})

        client = _make_client(handler, fake_creds)
        client.create_issue(
            project_key="ENG",
            issuetype="Task",
            summary="x",
            epic_link="ENG-99",
            epic_link_field="customfield_10014",
        )
        wire = _decode_request_json(captured[0])
        # Classic / team-managed sites still expose Epic Link as a custom
        # field; the gateway must NOT auto-translate to ``parent`` here.
        assert wire["fields"]["customfield_10014"] == "ENG-99"
        assert "parent" not in wire["fields"]

    def test_parent_and_epic_link_combined_raises(self, fake_creds: JiraCredentials):
        client = _make_client(lambda _r: httpx.Response(201, json={}), fake_creds)
        with pytest.raises(ValueError, match="mutually exclusive"):
            client.create_issue(
                project_key="ENG",
                issuetype="Task",
                summary="x",
                parent="ENG-1",
                epic_link="ENG-2",
            )

    def test_idempotency_hit_avoids_second_request(
        self, fake_creds: JiraCredentials, reset_idempotency_cache
    ):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(201, json={"id": "1", "key": "ENG-1"})

        client = _make_client(handler, fake_creds)
        a = client.create_issue(
            project_key="ENG",
            issuetype="Task",
            summary="x",
            idempotency_key="key-1",
        )
        b = client.create_issue(
            project_key="ENG",
            issuetype="Task",
            summary="x",
            idempotency_key="key-1",
        )
        assert a == b
        assert calls["n"] == 1

    def test_idempotency_miss_with_different_keys(
        self, fake_creds: JiraCredentials, reset_idempotency_cache
    ):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(201, json={"id": str(calls["n"]), "key": f"ENG-{calls['n']}"})

        client = _make_client(handler, fake_creds)
        client.create_issue(project_key="ENG", issuetype="Task", summary="a", idempotency_key="k1")
        client.create_issue(project_key="ENG", issuetype="Task", summary="b", idempotency_key="k2")
        assert calls["n"] == 2

    def test_no_idempotency_key_bypasses_cache(
        self, fake_creds: JiraCredentials, reset_idempotency_cache
    ):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(201, json={"id": "1", "key": "ENG-1"})

        client = _make_client(handler, fake_creds)
        client.create_issue(project_key="ENG", issuetype="Task", summary="x")
        client.create_issue(project_key="ENG", issuetype="Task", summary="x")
        assert calls["n"] == 2

    def test_429_emits_audit_and_does_not_retry(
        self, fake_creds: JiraCredentials, monkeypatch: pytest.MonkeyPatch
    ):
        """Refine feedback Q1: write verbs MUST emit
        ``jira_upstream_rate_limited`` on 429 even though they don't retry."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "1"})

        captured_audits: list[dict[str, Any]] = []

        def fake_audit(*, path, method, attempt, retry_after):
            captured_audits.append(
                {
                    "path": path,
                    "method": method,
                    "attempt": attempt,
                    "retry_after": retry_after,
                }
            )

        monkeypatch.setattr(jira_client, "_emit_rate_limited_audit", fake_audit)
        monkeypatch.setattr(jira_client.time, "sleep", lambda _s: None)

        client = _make_client(handler, fake_creds)
        with pytest.raises(JiraUpstreamError) as exc_info:
            client.create_issue(project_key="ENG", issuetype="Task", summary="x")
        assert exc_info.value.status_code == 429
        # POST to ``issue`` doesn't retry — exactly one upstream call.
        assert calls["n"] == 1
        # Exactly one audit fired with method=POST.
        assert len(captured_audits) == 1
        assert captured_audits[0]["method"] == "POST"
        assert captured_audits[0]["path"] == "issue"


# -----------------------------------------------------------------------------
# edit_issue
# -----------------------------------------------------------------------------


class TestEditIssue:
    def test_summary_only_replace_mode(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        client = _make_client(handler, fake_creds)
        result = client.edit_issue(key="ENG-1", summary="new summary")

        assert result == {}  # 204 → empty dict
        assert captured[0].method == "PUT"
        assert str(captured[0].url).startswith(
            "https://example.atlassian.net/rest/api/3/issue/ENG-1"
        )
        wire = _decode_request_json(captured[0])
        assert wire == {"fields": {"summary": "new summary"}}

    def test_replace_labels_mode(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        client = _make_client(handler, fake_creds)
        client.edit_issue(key="ENG-1", labels=["a", "b"])
        wire = _decode_request_json(captured[0])
        assert wire == {"fields": {"labels": ["a", "b"]}}
        # No update.labels list when in replace mode.
        assert "update" not in wire

    def test_incremental_labels_mode(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        client = _make_client(handler, fake_creds)
        client.edit_issue(key="ENG-1", add_labels=["x"], remove_labels=["y"])
        wire = _decode_request_json(captured[0])
        assert wire == {
            "update": {
                "labels": [{"add": "x"}, {"remove": "y"}],
            }
        }
        # Replace mode not engaged.
        assert "fields" not in wire

    def test_combined_replace_and_incremental_raises(self, fake_creds: JiraCredentials):
        client = _make_client(lambda _r: httpx.Response(204), fake_creds)
        with pytest.raises(ValueError, match="mutually exclusive"):
            client.edit_issue(key="ENG-1", labels=["a"], add_labels=["b"])

    def test_no_fields_raises(self, fake_creds: JiraCredentials):
        client = _make_client(lambda _r: httpx.Response(204), fake_creds)
        with pytest.raises(ValueError, match="at least one"):
            client.edit_issue(key="ENG-1")

    def test_notify_users_false_sends_query_param(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        client = _make_client(handler, fake_creds)
        client.edit_issue(key="ENG-1", summary="hi", notify_users=False)
        # default is False; query param must be present.
        assert captured[0].url.params.get("notifyUsers") == "false"

    def test_notify_users_true_omits_query_param(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        client = _make_client(handler, fake_creds)
        client.edit_issue(key="ENG-1", summary="hi", notify_users=True)
        # ``notifyUsers=true`` matches Atlassian's default → no query param.
        assert "notifyUsers" not in captured[0].url.params

    def test_description_string_wrapped_to_adf(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        client = _make_client(handler, fake_creds)
        client.edit_issue(key="ENG-1", description="some text")
        wire = _decode_request_json(captured[0])
        assert wire["fields"]["description"]["type"] == "doc"

    def test_description_adf_passes_through(self, fake_creds: JiraCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        client = _make_client(handler, fake_creds)
        adf = {"type": "doc", "version": 1, "content": []}
        client.edit_issue(key="ENG-1", description=adf)
        wire = _decode_request_json(captured[0])
        assert wire["fields"]["description"] == adf

    def test_429_emits_audit_no_retry(
        self, fake_creds: JiraCredentials, monkeypatch: pytest.MonkeyPatch
    ):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "1"})

        captured_audits: list[dict[str, Any]] = []
        monkeypatch.setattr(
            jira_client,
            "_emit_rate_limited_audit",
            lambda *, path, method, attempt, retry_after: captured_audits.append(
                {"path": path, "method": method}
            ),
        )
        monkeypatch.setattr(jira_client.time, "sleep", lambda _s: None)

        client = _make_client(handler, fake_creds)
        with pytest.raises(JiraUpstreamError):
            client.edit_issue(key="ENG-1", summary="x")
        # PUT is one-shot — exactly one upstream call.
        assert calls["n"] == 1
        assert len(captured_audits) == 1
        assert captured_audits[0]["method"] == "PUT"


# -----------------------------------------------------------------------------
# add_comment
# -----------------------------------------------------------------------------


class TestAddComment:
    def test_text_body_wrapped_to_adf(self, fake_creds: JiraCredentials, reset_idempotency_cache):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "1"})

        client = _make_client(handler, fake_creds)
        status, body = client.add_comment(key="ENG-1", body="hi there")
        assert status == 201
        assert captured[0].method == "POST"
        assert str(captured[0].url).endswith("/issue/ENG-1/comment")
        wire = _decode_request_json(captured[0])
        adf = wire["body"]
        assert adf["type"] == "doc"
        assert adf["content"][0]["content"][0]["text"] == "hi there"

    def test_adf_body_passthrough(self, fake_creds: JiraCredentials, reset_idempotency_cache):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "1"})

        client = _make_client(handler, fake_creds)
        adf = {"type": "doc", "version": 1, "content": []}
        client.add_comment(key="ENG-1", body=adf)
        wire = _decode_request_json(captured[0])
        assert wire["body"] == adf

    def test_idempotency_hit_skips_upstream(
        self, fake_creds: JiraCredentials, reset_idempotency_cache
    ):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(201, json={"id": "1"})

        client = _make_client(handler, fake_creds)
        client.add_comment(key="ENG-1", body="hi", idempotency_key="cmt-1")
        client.add_comment(key="ENG-1", body="hi", idempotency_key="cmt-1")
        assert calls["n"] == 1

    def test_idempotency_namespaced_per_project(
        self, fake_creds: JiraCredentials, reset_idempotency_cache
    ):
        """Same opaque idempotency key against tickets in DIFFERENT
        projects must not collide — the cache key is namespaced by
        ``project`` (extracted from the ticket key)."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(201, json={"id": str(calls["n"])})

        client = _make_client(handler, fake_creds)
        client.add_comment(key="ENG-1", body="hi", idempotency_key="k")
        client.add_comment(key="DEVOPS-1", body="hi", idempotency_key="k")
        assert calls["n"] == 2

    def test_429_emits_audit(self, fake_creds: JiraCredentials, monkeypatch: pytest.MonkeyPatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "1"})

        captured_audits: list[dict[str, Any]] = []
        monkeypatch.setattr(
            jira_client,
            "_emit_rate_limited_audit",
            lambda *, path, method, attempt, retry_after: captured_audits.append(
                {"method": method, "path": path}
            ),
        )
        monkeypatch.setattr(jira_client.time, "sleep", lambda _s: None)

        client = _make_client(handler, fake_creds)
        with pytest.raises(JiraUpstreamError):
            client.add_comment(key="ENG-1", body="hi")
        assert len(captured_audits) == 1
        assert captured_audits[0]["method"] == "POST"


# -----------------------------------------------------------------------------
# create_issue_link
# -----------------------------------------------------------------------------


class TestCreateIssueLink:
    def test_minimal_body(self, fake_creds: JiraCredentials, reset_idempotency_cache):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201)

        client = _make_client(handler, fake_creds)
        status, body = client.create_issue_link(
            link_type="Blocks",
            inward_key="ENG-1",
            outward_key="ENG-2",
        )
        assert status == 201
        assert body == {}  # 201 + empty body → empty dict envelope
        assert captured[0].method == "POST"
        assert str(captured[0].url).endswith("/rest/api/3/issueLink")
        wire = _decode_request_json(captured[0])
        assert wire == {
            "type": {"name": "Blocks"},
            "inwardIssue": {"key": "ENG-1"},
            "outwardIssue": {"key": "ENG-2"},
        }

    def test_with_text_comment_wraps_to_adf(
        self, fake_creds: JiraCredentials, reset_idempotency_cache
    ):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201)

        client = _make_client(handler, fake_creds)
        client.create_issue_link(
            link_type="Blocks",
            inward_key="ENG-1",
            outward_key="ENG-2",
            comment="see issue #1924",
        )
        wire = _decode_request_json(captured[0])
        assert wire["comment"]["body"]["type"] == "doc"
        assert wire["comment"]["body"]["content"][0]["content"][0]["text"] == "see issue #1924"

    def test_with_adf_comment_passthrough(
        self, fake_creds: JiraCredentials, reset_idempotency_cache
    ):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201)

        client = _make_client(handler, fake_creds)
        adf = {"type": "doc", "version": 1, "content": []}
        client.create_issue_link(
            link_type="Blocks",
            inward_key="ENG-1",
            outward_key="ENG-2",
            comment=adf,
        )
        wire = _decode_request_json(captured[0])
        assert wire["comment"] == {"body": adf}

    def test_idempotency_hit_same_triple(
        self, fake_creds: JiraCredentials, reset_idempotency_cache
    ):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(201)

        client = _make_client(handler, fake_creds)
        client.create_issue_link(
            link_type="Blocks",
            inward_key="ENG-1",
            outward_key="ENG-2",
            idempotency_key="link-1",
        )
        client.create_issue_link(
            link_type="Blocks",
            inward_key="ENG-1",
            outward_key="ENG-2",
            idempotency_key="link-1",
        )
        assert calls["n"] == 1

    def test_idempotency_link_cache_aliasing_distinct_triples(
        self, fake_creds: JiraCredentials, reset_idempotency_cache
    ):
        """Refine decision-28: same opaque idempotency key against
        different ``(inward, outward, type)`` triples must produce distinct
        cache entries.  This is the link-cache aliasing test required by
        task-5-1's acceptance criteria, mirrored here at the JiraClient
        level (the cache module test_jira_idempotency.py also covers it
        via its synthetic-project shape)."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(201)

        client = _make_client(handler, fake_creds)
        # Same opaque key, three different triples → three upstream calls.
        client.create_issue_link(
            link_type="Blocks",
            inward_key="ENG-1",
            outward_key="ENG-2",
            idempotency_key="k",
        )
        client.create_issue_link(
            link_type="Blocks",
            inward_key="ENG-1",
            outward_key="ENG-3",
            idempotency_key="k",
        )
        client.create_issue_link(
            link_type="Relates",
            inward_key="ENG-1",
            outward_key="ENG-2",
            idempotency_key="k",
        )
        assert calls["n"] == 3

    def test_a_to_b_and_b_to_a_distinct(self, fake_creds: JiraCredentials, reset_idempotency_cache):
        """Direction matters: A→B and B→A are distinct links."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(201)

        client = _make_client(handler, fake_creds)
        client.create_issue_link(
            link_type="Blocks",
            inward_key="ENG-1",
            outward_key="ENG-2",
            idempotency_key="k",
        )
        client.create_issue_link(
            link_type="Blocks",
            inward_key="ENG-2",
            outward_key="ENG-1",
            idempotency_key="k",
        )
        assert calls["n"] == 2

    def test_429_emits_audit(self, fake_creds: JiraCredentials, monkeypatch: pytest.MonkeyPatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "1"})

        captured_audits: list[dict[str, Any]] = []
        monkeypatch.setattr(
            jira_client,
            "_emit_rate_limited_audit",
            lambda *, path, method, attempt, retry_after: captured_audits.append(
                {"method": method, "path": path}
            ),
        )
        monkeypatch.setattr(jira_client.time, "sleep", lambda _s: None)

        client = _make_client(handler, fake_creds)
        with pytest.raises(JiraUpstreamError):
            client.create_issue_link(
                link_type="Blocks",
                inward_key="ENG-1",
                outward_key="ENG-2",
            )
        assert len(captured_audits) == 1
        assert captured_audits[0]["method"] == "POST"
        assert captured_audits[0]["path"] == "issueLink"


# -----------------------------------------------------------------------------
# Auth header on writes
# -----------------------------------------------------------------------------


class TestWriteAuthHeader:
    def test_basic_auth_on_each_write(self, fake_creds: JiraCredentials, reset_idempotency_cache):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            # 201 for creates, 204 for the PUT.
            if request.method == "PUT":
                return httpx.Response(204)
            return httpx.Response(201, json={"id": "1", "key": "ENG-1"})

        client = _make_client(handler, fake_creds)
        client.create_issue(project_key="ENG", issuetype="Task", summary="x")
        client.edit_issue(key="ENG-1", summary="y")
        client.add_comment(key="ENG-1", body="z")
        client.create_issue_link(link_type="Blocks", inward_key="ENG-1", outward_key="ENG-2")

        assert len(captured) == 4
        for req in captured:
            assert req.headers["authorization"] == fake_creds.basic_auth_header()
            # Content-Type set on every write (we always send a body).
            assert req.headers["content-type"] == "application/json"
