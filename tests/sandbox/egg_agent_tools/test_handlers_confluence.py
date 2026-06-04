"""Unit tests for egg_agent_tools.handlers.confluence (#2994).

The handlers are thin: build the gateway body (snake_case args →
camelCase fields) and POST it via ``gateway_data_request``.  We patch
that helper and assert the endpoint + body, plus required-field
validation and list/CSV normalisation.  A separate class exercises the
real ``gateway_data_request`` unwrap (success envelope → ``data``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import confluence  # noqa: E402
from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402


def _patch_gateway(return_value=None):
    return patch(
        "egg_agent_tools.handlers.confluence.gateway_data_request",
        return_value=return_value if return_value is not None else {"ok": True},
    )


class TestPageGet:
    def test_minimal_body(self):
        with _patch_gateway() as gw:
            confluence.confluence_page_get({"page_id": "12345"})
        endpoint = gw.call_args.args[0]
        body = gw.call_args.kwargs["body"]
        assert endpoint == "/api/v1/confluence/page/get"
        assert body == {"pageId": "12345"}

    def test_body_format_and_expand_lists(self):
        with _patch_gateway() as gw:
            confluence.confluence_page_get(
                {"page_id": "1", "body_format": ["storage", "view"], "expand": ["version"]}
            )
        body = gw.call_args.kwargs["body"]
        assert body["bodyFormat"] == ["storage", "view"]
        assert body["expand"] == ["version"]

    def test_body_format_accepts_csv_string(self):
        """A model occasionally passes the gateway's CSV form; tolerate it."""
        with _patch_gateway() as gw:
            confluence.confluence_page_get({"page_id": "1", "body_format": "storage, view"})
        assert gw.call_args.kwargs["body"]["bodyFormat"] == ["storage", "view"]

    def test_missing_page_id_raises(self):
        with pytest.raises(HandlerError):
            confluence.confluence_page_get({})

    def test_blank_page_id_raises(self):
        with pytest.raises(HandlerError):
            confluence.confluence_page_get({"page_id": "   "})


class TestPageDescendants:
    def test_translates_pagination(self):
        with _patch_gateway() as gw:
            confluence.confluence_page_descendants(
                {"page_id": "9", "depth": 2, "limit": 50, "cursor": "tok"}
            )
        endpoint = gw.call_args.args[0]
        body = gw.call_args.kwargs["body"]
        assert endpoint == "/api/v1/confluence/page/descendants"
        assert body == {"pageId": "9", "depth": 2, "limit": 50, "cursor": "tok"}


class TestFooterComments:
    def test_include_replies_default_false(self):
        with _patch_gateway() as gw:
            confluence.confluence_page_footer_comments({"page_id": "1"})
        assert gw.call_args.kwargs["body"]["includeReplies"] is False

    def test_include_replies_true(self):
        with _patch_gateway() as gw:
            confluence.confluence_page_footer_comments({"page_id": "1", "include_replies": True})
        assert gw.call_args.kwargs["body"]["includeReplies"] is True


class TestInlineComments:
    def test_endpoint(self):
        with _patch_gateway() as gw:
            confluence.confluence_page_inline_comments({"page_id": "1"})
        assert gw.call_args.args[0] == "/api/v1/confluence/page/inline-comments"


class TestSpacePages:
    def test_space_key_required(self):
        with pytest.raises(HandlerError):
            confluence.confluence_space_pages({})

    def test_body(self):
        with _patch_gateway() as gw:
            confluence.confluence_space_pages({"space_key": "ENG", "limit": 10})
        body = gw.call_args.kwargs["body"]
        assert gw.call_args.args[0] == "/api/v1/confluence/space/pages"
        assert body == {"spaceKey": "ENG", "limit": 10}


class TestSpaceList:
    def test_empty_body(self):
        with _patch_gateway() as gw:
            confluence.confluence_space_list({})
        assert gw.call_args.args[0] == "/api/v1/confluence/space/list"
        assert gw.call_args.kwargs["body"] == {}

    def test_pagination(self):
        with _patch_gateway() as gw:
            confluence.confluence_space_list({"limit": 5, "cursor": "c"})
        assert gw.call_args.kwargs["body"] == {"limit": 5, "cursor": "c"}


class TestSearch:
    def test_cql_required(self):
        with pytest.raises(HandlerError):
            confluence.confluence_search({})

    def test_body(self):
        with _patch_gateway() as gw:
            confluence.confluence_search({"cql": "space = ENG", "limit": 25})
        body = gw.call_args.kwargs["body"]
        assert gw.call_args.args[0] == "/api/v1/confluence/search"
        assert body == {"cql": "space = ENG", "limit": 25}


class TestExecute:
    def test_method_and_path_required(self):
        with pytest.raises(HandlerError):
            confluence.confluence_execute({"method": "GET"})

    def test_query_must_be_object(self):
        with pytest.raises(HandlerError):
            confluence.confluence_execute({"method": "GET", "path": "/x", "query": "k=v"})

    def test_body(self):
        with _patch_gateway() as gw:
            confluence.confluence_execute(
                {"method": "GET", "path": "/wiki/rest/api/space", "query": {"limit": "10"}}
            )
        body = gw.call_args.kwargs["body"]
        assert gw.call_args.args[0] == "/api/v1/confluence/execute"
        assert body == {
            "method": "GET",
            "path": "/wiki/rest/api/space",
            "query": {"limit": "10"},
        }


class TestGatewayDataRequestUnwrap:
    """The shared unwrap helper returns ``data`` on success and raises on
    a success=false body; HTTP-level failures are already raised by
    ``gateway_request`` upstream."""

    def test_returns_data_on_success(self):
        with patch(
            "egg_agent_tools.handlers._gateway.gateway_request",
            return_value={"success": True, "message": "ok", "data": {"results": [1, 2]}},
        ):
            out = confluence.confluence_space_list({})
        assert out == {"results": [1, 2]}

    def test_not_found_envelope_flows_through(self):
        # The gateway returns success=true with data.status == not_found.
        with patch(
            "egg_agent_tools.handlers._gateway.gateway_request",
            return_value={"success": True, "data": {"status": "not_found"}},
        ):
            out = confluence.confluence_page_get({"page_id": "1"})
        assert out == {"status": "not_found"}

    def test_success_false_body_raises(self):
        with patch(
            "egg_agent_tools.handlers._gateway.gateway_request",
            return_value={"success": False, "message": "denied", "details": {"space": "X"}},
        ):
            with pytest.raises(GatewayError) as exc:
                confluence.confluence_space_list({})
        assert "denied" in str(exc.value)
