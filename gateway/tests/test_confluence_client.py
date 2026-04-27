"""
Tests for ``gateway/confluence_client.py``.

Covers Phase 1 / Task 4-2 acceptance:

- URL / header / body construction per public verb (httpx.MockTransport).
- Default ``body-format=storage`` on read methods + override accepted.
- ``validate_confluence_api_path`` positive and negative grids.
- 429 single-retry honouring ``Retry-After``; second 429 surfaces error;
  write verbs do NOT retry.
- 404 envelope on read methods (`get_page`, descendants, footer-comments,
  inline-comments, get_space_pages); ``search_cql`` and ``execute_raw``
  raise instead.
- 403 → ``ConfluenceUpstreamForbidden`` from every read method.
- v1 inline-comment fallback fires on v2 404 with ``used_fallback`` flag.
- footer-comment nested-reply fallback merges replies under ``_replies``.
- ``list_spaces`` filtering against the operator allowlist; cache populated.
- ``redact_response`` strips the three default keys recursively, preserves
  page / space ``_links.webui`` URLs, redacts ``_links.self`` user URLs.
- ``CONFLUENCE_RESPONSE_MAX_BYTES`` raised as ``ConfluenceResponseTooLarge``.
- Confluence-original CQL fixture (``text ~ "RFC"``) — risk R17.
"""

from __future__ import annotations

# Modules loaded via conftest.
import confluence_client
import httpx
import pytest
from confluence_client import (
    CONFLUENCE_RESPONSE_MAX_BYTES,
    ConfluenceClient,
    ConfluenceResponseTooLarge,
    ConfluenceUpstreamError,
    ConfluenceUpstreamForbidden,
    redact_response,
    validate_confluence_api_path,
)
from confluence_credentials import ConfluenceCredentials

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def fake_creds() -> ConfluenceCredentials:
    return ConfluenceCredentials(
        base_url="https://example.atlassian.net/wiki",
        username="alice@example.com",
        api_token="atk-xyz",
    )


def _make_client(
    handler,
    creds: ConfluenceCredentials,
) -> ConfluenceClient:
    """Build a ``ConfluenceClient`` whose upstream HTTP lands in ``handler``."""
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return ConfluenceClient(
        creds_provider=lambda: creds,
        http_client=http,
    )


# -----------------------------------------------------------------------------
# validate_confluence_api_path
# -----------------------------------------------------------------------------


class TestValidateConfluenceApiPath:
    @pytest.mark.parametrize(
        "path",
        [
            "api/v2/pages/12345",
            "api/v2/spaces/42/pages",
        ],
    )
    def test_positive_get_paths(self, path: str):
        ok, reason = validate_confluence_api_path(path, "GET")
        assert ok, f"{path!r} should have been accepted: {reason}"

    @pytest.mark.parametrize(
        "path",
        [
            # Cycle-3 reviewer_code/security NACK fix (commit f3f552eb9): these
            # four flat v2 endpoints were dropped from the /execute allowlist
            # because each one was an exploitable cross-partition bypass:
            #   - api/v2/spaces             — full tenant enumeration via
            #                                 execute_raw bypassing list_spaces'
            #                                 allowlist filter (decision-11).
            #   - rest/api/search           — arbitrary CQL via execute_raw,
            #                                 bypassing extract_search_spaces.
            #   - api/v2/footer-comments    — flat-endpoint smuggling via
            #     api/v2/inline-comments      page-id query param while spaceKey
            #                                 fakes the gate (Atlassian ignores
            #                                 spaceKey upstream).
            #
            # Post-review tightening (PR #2141): the page-scoped descendant
            # and comment subpaths
            #   - api/v2/pages/<id>/descendants
            #   - api/v2/pages/<id>/footer-comments
            #   - api/v2/pages/<id>/inline-comments
            #   - rest/api/content/<id>/child/comment
            # are also dropped from /execute.  Their response bodies have no
            # top-level spaceId so the /execute post-fetch allowlist check
            # always fail-closed; agents reach those endpoints through the
            # dedicated /api/v1/confluence/page/* routes which fetch the
            # parent page and resolve spaceKey explicitly.  All four remain
            # reachable INTERNALLY by ConfluenceClient methods that construct
            # them directly; only the agent-facing /execute escape hatch is
            # closed.
            "api/v2/spaces",
            "rest/api/search",
            "api/v2/footer-comments",
            "api/v2/inline-comments",
            "api/v2/pages/1/descendants",
            "api/v2/pages/1/footer-comments",
            "api/v2/pages/1/inline-comments",
            "rest/api/content/1/child/comment",
        ],
    )
    def test_anti_bypass_paths_rejected(self, path: str):
        """Risk R2 / cycle-3 fix: these paths must NOT be in the /execute
        allowlist — they expose cross-partition bypasses."""
        ok, reason = validate_confluence_api_path(path, "GET")
        assert not ok, f"{path!r} must be rejected to close the bypass"
        assert "allowlist" in reason.lower() or "not in" in reason.lower()

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", ""])
    def test_non_get_methods_rejected(self, method: str):
        ok, reason = validate_confluence_api_path("api/v2/pages/1", method)
        assert not ok
        assert (
            "not allowed" in reason.lower()
            or "denied" in reason.lower()
            or "permanently denied" in reason.lower()
        )

    @pytest.mark.parametrize(
        "bad_segment",
        ["restrictions", "permissions", "space.admin", "users", "attachments"],
    )
    def test_denied_verb_in_path(self, bad_segment: str):
        # Both naked and inline forms should be rejected.
        for path in (
            f"api/v2/{bad_segment}",
            f"api/v2/pages/123/{bad_segment}",
        ):
            ok, reason = validate_confluence_api_path(path, "GET")
            assert not ok, f"{path!r} should have been rejected"

    def test_path_traversal_rejected(self):
        ok, reason = validate_confluence_api_path("api/v2/pages/../12345", "GET")
        assert not ok
        assert ".." in reason

    def test_duplicate_slashes_rejected(self):
        ok, reason = validate_confluence_api_path("api//v2/pages/1", "GET")
        assert not ok
        assert "duplicate" in reason.lower() or "slash" in reason.lower()

    def test_leading_double_slash_rejected(self):
        ok, _ = validate_confluence_api_path("//api/v2/pages/1", "GET")
        assert not ok

    def test_non_ascii_rejected(self):
        ok, reason = validate_confluence_api_path("api/v2/pages/1234А", "GET")  # Cyrillic А
        assert not ok
        assert "ascii" in reason.lower() or "non-" in reason.lower()

    def test_empty_path_rejected(self):
        ok, _ = validate_confluence_api_path("", "GET")
        assert not ok
        ok, _ = validate_confluence_api_path("/", "GET")
        assert not ok

    def test_non_string_path_rejected(self):
        ok, _ = validate_confluence_api_path(None, "GET")  # type: ignore[arg-type]
        assert not ok

    def test_query_string_is_stripped_before_check(self):
        ok, _ = validate_confluence_api_path("api/v2/pages/1?body-format=storage", "GET")
        assert ok

    def test_random_unknown_path_rejected(self):
        ok, _ = validate_confluence_api_path("whoami", "GET")
        assert not ok
        ok, _ = validate_confluence_api_path("api/v2/users/123", "GET")
        assert not ok

    def test_pages_id_must_be_numeric(self):
        ok, _ = validate_confluence_api_path("api/v2/pages/abc", "GET")
        assert not ok


# -----------------------------------------------------------------------------
# get_page
# -----------------------------------------------------------------------------


class TestGetPage:
    def test_default_body_format_is_storage(self, fake_creds: ConfluenceCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"id": "12345", "spaceId": "1"})

        client = _make_client(handler, fake_creds)
        body = client.get_page("12345")
        assert body["id"] == "12345"

        assert len(captured) == 1
        url = captured[0].url
        assert str(url).startswith("https://example.atlassian.net/wiki/api/v2/pages/12345")
        assert url.params["body-format"] == "storage"
        # Basic-auth + Accept JSON.
        assert captured[0].headers["authorization"].startswith("Basic ")
        assert "application/json" in captured[0].headers["accept"]

    def test_explicit_body_format_override(self, fake_creds: ConfluenceCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"id": "1"})

        client = _make_client(handler, fake_creds)
        client.get_page("1", body_format=["storage", "atlas_doc_format"])
        assert captured[0].url.params["body-format"] == "storage,atlas_doc_format"

    def test_invalid_body_format_raises(self, fake_creds: ConfluenceCredentials):
        client = _make_client(lambda r: httpx.Response(200, json={}), fake_creds)
        with pytest.raises(ValueError):
            client.get_page("1", body_format=["bogus"])

    def test_invalid_page_id_raises(self, fake_creds: ConfluenceCredentials):
        client = _make_client(lambda r: httpx.Response(200, json={}), fake_creds)
        with pytest.raises(ValueError):
            client.get_page("abc")
        with pytest.raises(ValueError):
            client.get_page("")

    def test_404_returns_envelope(self, fake_creds: ConfluenceCredentials):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "no such page"})

        client = _make_client(handler, fake_creds)
        body = client.get_page("12345")
        assert body == {
            "status": "not_found",
            "id": "12345",
            "upstream_status": 404,
        }

    def test_403_raises_forbidden(self, fake_creds: ConfluenceCredentials):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "denied"})

        client = _make_client(handler, fake_creds)
        with pytest.raises(ConfluenceUpstreamForbidden) as exc_info:
            client.get_page("12345")
        assert exc_info.value.status_code == 403

    def test_500_raises_upstream_error(self, fake_creds: ConfluenceCredentials):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = _make_client(handler, fake_creds)
        with pytest.raises(ConfluenceUpstreamError) as exc_info:
            client.get_page("12345")
        assert exc_info.value.status_code == 500

    def test_expand_list_round_trips(self, fake_creds: ConfluenceCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"id": "1"})

        client = _make_client(handler, fake_creds)
        client.get_page("1", expand=["history", "version"])
        assert captured[0].url.params["expand"] == "history,version"


# -----------------------------------------------------------------------------
# get_page_descendants
# -----------------------------------------------------------------------------


class TestDescendants:
    def test_optional_params_passed_through(self, fake_creds: ConfluenceCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"results": []})

        client = _make_client(handler, fake_creds)
        client.get_page_descendants("1", depth=2, limit=10, cursor="TOK")
        params = captured[0].url.params
        assert params["depth"] == "2"
        assert params["limit"] == "10"
        assert params["cursor"] == "TOK"

    def test_no_optional_params(self, fake_creds: ConfluenceCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"results": []})

        client = _make_client(handler, fake_creds)
        client.get_page_descendants("1")
        # No depth/limit/cursor params on the wire.
        params = captured[0].url.params
        assert "depth" not in params
        assert "limit" not in params
        assert "cursor" not in params

    def test_404_envelope(self, fake_creds: ConfluenceCredentials):
        client = _make_client(lambda _r: httpx.Response(404), fake_creds)
        assert client.get_page_descendants("1") == {
            "status": "not_found",
            "id": "1",
            "upstream_status": 404,
        }


# -----------------------------------------------------------------------------
# get_page_footer_comments — nested-reply fallback
# -----------------------------------------------------------------------------


class TestFooterComments:
    def test_simple_call_no_replies(self, fake_creds: ConfluenceCredentials):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": [{"id": "c1"}]})

        client = _make_client(handler, fake_creds)
        body = client.get_page_footer_comments("1")
        # Without include_replies, no _replies key.
        assert "_replies" not in body

    def test_include_replies_merges_secondary_call(self, fake_creds: ConfluenceCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if "footer-comments" in str(request.url) and "/pages/" in str(request.url):
                return httpx.Response(200, json={"results": [{"id": "c1"}]})
            # Nested-reply call.
            return httpx.Response(200, json={"results": [{"id": "r1", "parentCommentId": "c1"}]})

        client = _make_client(handler, fake_creds)
        body = client.get_page_footer_comments("1", include_replies=True)
        assert body["_replies"]["results"][0]["id"] == "r1"
        # Two upstream calls fired — primary + replies.
        assert len(captured) == 2
        # Second call hit api/v2/footer-comments and carried page-id + depth=all.
        secondary = captured[1].url
        assert "api/v2/footer-comments" in str(secondary)
        assert secondary.params["page-id"] == "1"
        assert secondary.params["depth"] == "all"

    def test_replies_fetch_failure_logged_and_dropped(self, fake_creds: ConfluenceCredentials):
        """Failing replies-side call must NOT fail the primary fetch."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "/pages/" in str(request.url):
                return httpx.Response(200, json={"results": [{"id": "c1"}]})
            return httpx.Response(500)

        client = _make_client(handler, fake_creds)
        body = client.get_page_footer_comments("1", include_replies=True)
        # Primary results survived — secondary failure swallowed.
        assert body["results"] == [{"id": "c1"}]
        assert "_replies" not in body

    def test_404_envelope(self, fake_creds: ConfluenceCredentials):
        client = _make_client(lambda _r: httpx.Response(404), fake_creds)
        assert client.get_page_footer_comments("1") == {
            "status": "not_found",
            "id": "1",
            "upstream_status": 404,
        }


# -----------------------------------------------------------------------------
# get_page_inline_comments — v2 → v1 fallback
# -----------------------------------------------------------------------------


class TestInlineComments:
    def test_v2_happy_path_no_fallback(self, fake_creds: ConfluenceCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"results": [{"id": "ic1"}]})

        client = _make_client(handler, fake_creds)
        body = client.get_page_inline_comments("1")
        # No "used_fallback" because v2 responded 200.
        assert "used_fallback" not in body
        assert len(captured) == 1
        assert "/api/v2/pages/1/inline-comments" in str(captured[0].url)

    def test_v2_404_falls_back_to_v1(self, fake_creds: ConfluenceCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if "/api/v2/pages/1/inline-comments" in str(request.url):
                return httpx.Response(404)
            return httpx.Response(200, json={"results": [{"id": "v1_ic"}]})

        client = _make_client(handler, fake_creds)
        body = client.get_page_inline_comments("1")
        assert body["used_fallback"] is True
        assert body["results"] == [{"id": "v1_ic"}]
        # v1 fallback hit the rest/api/content/{id}/child/comment endpoint.
        assert len(captured) == 2
        assert "rest/api/content/1/child/comment" in str(captured[1].url)
        assert captured[1].url.params["location"] == "inline"

    def test_v2_404_v1_404_returns_envelope_with_fallback_flag(
        self, fake_creds: ConfluenceCredentials
    ):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = _make_client(handler, fake_creds)
        body = client.get_page_inline_comments("1")
        assert body["status"] == "not_found"
        assert body["upstream_status"] == 404
        assert body["used_fallback"] is True

    def test_v2_403_does_not_fall_back(self, fake_creds: ConfluenceCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(403)

        client = _make_client(handler, fake_creds)
        with pytest.raises(ConfluenceUpstreamForbidden):
            client.get_page_inline_comments("1")
        assert len(captured) == 1, "v1 fallback must not fire on 403"


# -----------------------------------------------------------------------------
# list_spaces — operator allowlist filter
# -----------------------------------------------------------------------------


class TestListSpaces:
    def test_filters_to_allowlist(self, fake_creds: ConfluenceCredentials):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "1", "key": "ENG"},
                        {"id": "2", "key": "DOCS"},
                        {"id": "9", "key": "LEAK"},
                    ]
                },
            )

        client = _make_client(handler, fake_creds)
        body = client.list_spaces(frozenset({"ENG", "DOCS"}))
        keys = sorted(s["key"] for s in body["results"])
        assert keys == ["DOCS", "ENG"]

    def test_case_sensitive_intersection(self, fake_creds: ConfluenceCredentials):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": [{"id": "1", "key": "ENG"}]})

        client = _make_client(handler, fake_creds)
        # Lowercase allowlist must NOT match uppercase upstream key.
        body = client.list_spaces(frozenset({"eng"}))
        assert body["results"] == []

    def test_populates_space_cache(self, fake_creds: ConfluenceCredentials):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "1", "key": "ENG"},
                        {"id": "9", "key": "LEAK"},
                    ]
                },
            )

        client = _make_client(handler, fake_creds)
        client.list_spaces(frozenset({"ENG"}))
        # Cache holds BOTH entries — the cache populates from upstream
        # before the allowlist filter, so the gateway can resolve a
        # spaceId↔spaceKey for any space the bot can see.
        assert client.space_cache.key_for_id("1") == "ENG"
        assert client.space_cache.key_for_id("9") == "LEAK"
        assert client.space_cache.id_for_key("ENG") == "1"

    def test_403_raises_forbidden(self, fake_creds: ConfluenceCredentials):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        client = _make_client(handler, fake_creds)
        with pytest.raises(ConfluenceUpstreamForbidden):
            client.list_spaces(frozenset())


# -----------------------------------------------------------------------------
# populate_space_cache — paginated cache warming for spaceKey↔spaceId lookups
# -----------------------------------------------------------------------------


class TestPopulateSpaceCache:
    def test_walks_pagination_until_no_next(self, fake_creds: ConfluenceCredentials):
        """The helper must follow ``_links.next`` so a target space living
        on page 2+ still resolves through the cache."""
        pages = iter(
            [
                {
                    "results": [{"id": "1", "key": "ENG"}],
                    "_links": {"next": "/wiki/api/v2/spaces?cursor=PAGE2"},
                },
                {
                    "results": [{"id": "2", "key": "DOCS"}],
                    "_links": {"next": "/wiki/api/v2/spaces?cursor=PAGE3"},
                },
                {
                    "results": [{"id": "3", "key": "TARGET"}],
                    "_links": {},
                },
            ]
        )
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=next(pages))

        client = _make_client(handler, fake_creds)
        client.populate_space_cache()

        assert len(captured) == 3
        # First call: no cursor.  Subsequent calls: cursor lifted from _links.next.
        assert "cursor" not in captured[0].url.params
        assert captured[1].url.params.get("cursor") == "PAGE2"
        assert captured[2].url.params.get("cursor") == "PAGE3"
        assert client.space_cache.key_for_id("3") == "TARGET"
        assert client.space_cache.id_for_key("TARGET") == "3"

    def test_caps_iterations(self, fake_creds: ConfluenceCredentials):
        """Defensive cap: never walk more than ``max_pages`` pages, even if
        Atlassian keeps handing us a ``next`` cursor."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "results": [{"id": str(len(captured)), "key": f"S{len(captured)}"}],
                    "_links": {"next": "/wiki/api/v2/spaces?cursor=MORE"},
                },
            )

        client = _make_client(handler, fake_creds)
        client.populate_space_cache(max_pages=2)
        assert len(captured) == 2

    def test_403_raises_forbidden(self, fake_creds: ConfluenceCredentials):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        client = _make_client(handler, fake_creds)
        with pytest.raises(ConfluenceUpstreamForbidden):
            client.populate_space_cache()


# -----------------------------------------------------------------------------
# Lazy http client thread-safety
# -----------------------------------------------------------------------------


class TestLazyHttpClient:
    def test_client_is_constructed_once(self, fake_creds: ConfluenceCredentials):
        """``ConfluenceClient._client()`` must memoise the httpx.Client on
        first call — concurrent first requests must not each leak an
        instance.  We can only assert the single-thread invariant cheaply
        (every call returns the same object); the lock-protected
        double-check itself is exercised by the runtime, not by this test."""
        # Construct a ConfluenceClient WITHOUT a pre-built http_client so
        # the lazy path runs.
        client = ConfluenceClient(creds_provider=lambda: fake_creds)
        first = client._client()
        second = client._client()
        assert first is second
        # Sanity: also single-init for parallel callers in the same thread.
        for _ in range(5):
            assert client._client() is first


# -----------------------------------------------------------------------------
# get_space_pages
# -----------------------------------------------------------------------------


class TestGetSpacePages:
    def test_default_body_format(self, fake_creds: ConfluenceCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"results": []})

        client = _make_client(handler, fake_creds)
        client.get_space_pages("42")
        assert captured[0].url.params["body-format"] == "storage"
        assert "/api/v2/spaces/42/pages" in str(captured[0].url)

    def test_404_envelope_uses_space_id(self, fake_creds: ConfluenceCredentials):
        client = _make_client(lambda _r: httpx.Response(404), fake_creds)
        assert client.get_space_pages("42") == {
            "status": "not_found",
            "id": "42",
            "upstream_status": 404,
        }


# -----------------------------------------------------------------------------
# search_cql — Confluence-original fixture (risk R17)
# -----------------------------------------------------------------------------


class TestSearchCQL:
    def test_search_cql_constructs_v1_query(self, fake_creds: ConfluenceCredentials):
        """Confluence-original fixture: the canonical CQL ``text ~ "RFC"``
        clause is unique to Confluence (Jira's JQL has no equivalent)."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"results": []})

        client = _make_client(handler, fake_creds)
        client.search_cql('space = ENG AND text ~ "RFC"', limit=25)
        assert "/wiki/rest/api/search" in str(captured[0].url)
        assert captured[0].url.params["cql"] == 'space = ENG AND text ~ "RFC"'
        assert captured[0].url.params["limit"] == "25"

    def test_search_404_raises_not_envelope(self, fake_creds: ConfluenceCredentials):
        client = _make_client(lambda _r: httpx.Response(404), fake_creds)
        with pytest.raises(ConfluenceUpstreamError) as exc_info:
            client.search_cql("space = ENG")
        assert exc_info.value.status_code == 404

    def test_missing_cql_raises(self, fake_creds: ConfluenceCredentials):
        client = _make_client(lambda _r: httpx.Response(200, json={}), fake_creds)
        with pytest.raises(ValueError):
            client.search_cql("")
        with pytest.raises(ValueError):
            client.search_cql("   ")


# -----------------------------------------------------------------------------
# execute_raw — passthrough
# -----------------------------------------------------------------------------


class TestExecuteRaw:
    def test_404_raises_not_envelope(self, fake_creds: ConfluenceCredentials):
        client = _make_client(lambda _r: httpx.Response(404), fake_creds)
        with pytest.raises(ConfluenceUpstreamError) as exc_info:
            client.execute_raw("GET", "api/v2/pages/1")
        assert exc_info.value.status_code == 404

    def test_403_raises_forbidden(self, fake_creds: ConfluenceCredentials):
        client = _make_client(lambda _r: httpx.Response(403), fake_creds)
        with pytest.raises(ConfluenceUpstreamForbidden):
            client.execute_raw("GET", "api/v2/pages/1")

    def test_happy_path(self, fake_creds: ConfluenceCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"id": "1"})

        client = _make_client(handler, fake_creds)
        body = client.execute_raw("GET", "api/v2/pages/1")
        assert body == {"id": "1"}
        assert captured[0].method == "GET"


# -----------------------------------------------------------------------------
# 429 retry policy
# -----------------------------------------------------------------------------


class Test429Retry:
    def test_get_retries_once_on_429(self, fake_creds: ConfluenceCredentials, monkeypatch):
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"id": "1"})

        slept: list[float] = []
        monkeypatch.setattr(confluence_client.time, "sleep", lambda s: slept.append(s))

        client = _make_client(handler, fake_creds)
        client.get_page("1")
        assert calls["n"] == 2
        assert slept == [1]

    def test_retry_after_clamped_at_30(self, fake_creds: ConfluenceCredentials, monkeypatch):
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "600"})
            return httpx.Response(200, json={"id": "1"})

        slept: list[float] = []
        monkeypatch.setattr(confluence_client.time, "sleep", lambda s: slept.append(s))

        client = _make_client(handler, fake_creds)
        client.get_page("1")
        assert slept == [confluence_client._RETRY_AFTER_CAP_SECONDS]

    def test_second_429_surfaces(self, fake_creds: ConfluenceCredentials, monkeypatch):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "1"})

        monkeypatch.setattr(confluence_client.time, "sleep", lambda _s: None)
        client = _make_client(handler, fake_creds)
        with pytest.raises(ConfluenceUpstreamError) as exc_info:
            client.get_page("1")
        assert exc_info.value.status_code == 429

    def test_non_get_does_not_retry(self, fake_creds: ConfluenceCredentials, monkeypatch):
        """Non-GET (only ``execute_raw`` could ever receive one) does not retry."""
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "1"})

        monkeypatch.setattr(confluence_client.time, "sleep", lambda _s: None)
        client = _make_client(handler, fake_creds)
        with pytest.raises(ConfluenceUpstreamError):
            client.execute_raw("POST", "api/v2/pages/1")
        assert calls["n"] == 1

    @pytest.mark.parametrize(
        "value, expected",
        [
            (None, confluence_client._DEFAULT_RETRY_AFTER_SECONDS),
            ("", confluence_client._DEFAULT_RETRY_AFTER_SECONDS),
            (
                "not-a-number",
                confluence_client._DEFAULT_RETRY_AFTER_SECONDS,
            ),
            ("0", confluence_client._DEFAULT_RETRY_AFTER_SECONDS),
            ("-5", confluence_client._DEFAULT_RETRY_AFTER_SECONDS),
            ("2", 2),
            ("9999", confluence_client._RETRY_AFTER_CAP_SECONDS),
        ],
    )
    def test_parse_retry_after(self, value, expected):
        assert confluence_client._parse_retry_after(value) == expected


# -----------------------------------------------------------------------------
# redact_response — decision 10 / risk R6
# -----------------------------------------------------------------------------


class TestRedactResponse:
    def test_strips_account_id_at_any_depth(self):
        payload = {
            "version": {"by": {"accountId": "abc-123"}},
            "items": [{"author": {"accountId": "def-456"}}],
        }
        redact_response(payload)
        assert payload["version"]["by"]["accountId"] == "<redacted>"
        assert payload["items"][0]["author"]["accountId"] == "<redacted>"

    def test_strips_email_address_at_any_depth(self):
        payload = {
            "user": {
                "emailAddress": "alice@example.com",
                "displayName": "Alice",
            }
        }
        redact_response(payload)
        assert payload["user"]["emailAddress"] == "<redacted>"
        # Other fields preserved.
        assert payload["user"]["displayName"] == "Alice"

    def test_redacts_user_profile_webui(self):
        payload = {
            "_links": {
                "webui": "/wiki/people/abc-123",
                "self": "https://example.atlassian.net/wiki/api/v2/users/abc-123",
            }
        }
        redact_response(payload)
        assert payload["_links"]["webui"] == "<redacted>"
        assert payload["_links"]["self"] == "<redacted>"

    def test_preserves_page_webui(self):
        """Page / space ``_links.webui`` URLs must NOT be redacted."""
        payload = {"_links": {"webui": "/wiki/spaces/ENG/pages/12345/Some+Page"}}
        redact_response(payload)
        assert payload["_links"]["webui"] == "/wiki/spaces/ENG/pages/12345/Some+Page"

    def test_recursive_walk_into_lists(self):
        payload = {
            "results": [
                {"by": {"accountId": "x", "emailAddress": "x@x"}},
                {"by": {"accountId": "y"}},
            ]
        }
        redact_response(payload)
        for item in payload["results"]:
            assert item["by"]["accountId"] == "<redacted>"

    def test_adf_mention_node_redacted(self):
        """A real-shaped ADF mention node has the user's ``accountId`` inline."""
        payload = {
            "body": {
                "atlas_doc_format": {
                    "value": {
                        "type": "doc",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {
                                        "type": "mention",
                                        "attrs": {
                                            "id": "abc-123",
                                            "accountId": "abc-123",
                                            "text": "@Alice",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                }
            }
        }
        redact_response(payload)
        mention_attrs = payload["body"]["atlas_doc_format"]["value"]["content"][0]["content"][0][
            "attrs"
        ]
        assert mention_attrs["accountId"] == "<redacted>"
        # Non-redacted keys preserved.
        assert mention_attrs["text"] == "@Alice"

    def test_returns_same_object(self):
        """The redactor mutates in place and returns the input for chaining."""
        payload = {"accountId": "x"}
        result = redact_response(payload)
        assert result is payload


# -----------------------------------------------------------------------------
# Payload-size cap (risk R7)
# -----------------------------------------------------------------------------


class TestPayloadSizeCap:
    def test_oversized_response_raises(self, fake_creds: ConfluenceCredentials):
        # Build a payload whose JSON length crosses CONFLUENCE_RESPONSE_MAX_BYTES.
        big_value = "x" * (CONFLUENCE_RESPONSE_MAX_BYTES + 100)
        big_payload = {"id": "1", "filler": big_value}

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=big_payload)

        client = _make_client(handler, fake_creds)
        with pytest.raises(ConfluenceResponseTooLarge):
            client.get_page("1")


# -----------------------------------------------------------------------------
# Auth header on every request
# -----------------------------------------------------------------------------


class TestAuthHeader:
    def test_basic_auth_on_every_call(self, fake_creds: ConfluenceCredentials):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"results": []})

        client = _make_client(handler, fake_creds)
        client.get_page("1")
        client.get_page_descendants("1")
        client.get_page_footer_comments("1")
        client.get_page_inline_comments("1")
        client.list_spaces(frozenset())
        client.get_space_pages("1")
        client.search_cql("space = ENG")
        client.execute_raw("GET", "api/v2/pages/1")

        for req in captured:
            assert req.headers["authorization"] == fake_creds.basic_auth_header()


# -----------------------------------------------------------------------------
# Module-level singleton lifecycle
# -----------------------------------------------------------------------------


class TestSingletonLifecycle:
    def test_singleton_returns_same_instance(self):
        confluence_client.reset_confluence_client()
        a = confluence_client.get_confluence_client()
        b = confluence_client.get_confluence_client()
        assert a is b

        confluence_client.reset_confluence_client()
        c = confluence_client.get_confluence_client()
        assert c is not a
