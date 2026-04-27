"""
Tests for the sandbox ``confluence`` CLI wrapper.

Subprocess-invokes the wrapper against a stdlib ``http.server`` mock gateway
and asserts:

- Each verb constructs the correct request path + JSON body.
- The ``Authorization: Bearer $EGG_SESSION_TOKEN`` header is always sent.
- 2xx responses print the ``data`` subtree on stdout and exit 0.
- Non-2xx responses print an error on stderr and exit non-zero.
- Missing ``EGG_SESSION_TOKEN`` fails closed.
- Missing gateway fails closed with the standard error banner.

The wrapper lives at ``sandbox/scripts/confluence`` (canonical) or the
artifact location ``.egg-state/agent-outputs/1931-sandbox-scripts-confluence``
(until a maintainer ``git mv``s it post-merge — same arrangement as the Jira
wrapper in #1556).  Either is acceptable; the test prefers the canonical
location when present.

Note: this file exercises the wrapper directly; the actual route enforcement
(private-mode gate, space allowlist, CQL extractor, etc.) is covered in
``gateway/tests/test_confluence_routes.py``.  The wrapper's only job is to
translate CLI args into a well-formed ``POST /api/v1/confluence/*`` request
and surface the gateway's response.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

# -----------------------------------------------------------------------------
# Locate the wrapper
# -----------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL = _REPO_ROOT / "sandbox" / "scripts" / "confluence"
_ARTIFACT = _REPO_ROOT / ".egg-state" / "agent-outputs" / "1931-sandbox-scripts-confluence"


def _locate_wrapper() -> Path:
    if _CANONICAL.exists():
        return _CANONICAL
    if _ARTIFACT.exists():
        return _ARTIFACT
    pytest.skip(
        "sandbox confluence wrapper not found at "
        f"{_CANONICAL} or {_ARTIFACT} — coder proposal #1931 may be incomplete.",
        allow_module_level=True,
    )


WRAPPER = _locate_wrapper()


# -----------------------------------------------------------------------------
# Mock gateway
# -----------------------------------------------------------------------------


class _RecordingHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence default access-log chatter during tests.
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/api/v1/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            body = raw.decode("utf-8", errors="replace")
        self.server.recorded.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization", ""),
                "content_type": self.headers.get("Content-Type", ""),
            }
        )
        queue = self.server.response_queue  # type: ignore[attr-defined]
        if queue:
            response = queue.pop(0)
        else:
            response = {"status": 200, "body": {"success": True, "data": {}}}
        self.send_response(response["status"])
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        payload = response["body"]
        if isinstance(payload, dict):
            self.wfile.write(json.dumps(payload).encode("utf-8"))
        else:
            self.wfile.write(str(payload).encode("utf-8"))


@pytest.fixture
def mock_gateway():
    server = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server.recorded = []  # type: ignore[attr-defined]
    server.response_queue = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _, port = server.server_address
        yield {
            "url": f"http://127.0.0.1:{port}",
            "server": server,
        }
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _run_wrapper(
    mock_gateway: dict,
    argv: list[str],
    session_token: str | None = "test-session-token",
    extra_env: dict[str, str] | None = None,
    gateway_url_override: str | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GATEWAY_URL"] = (
        gateway_url_override if gateway_url_override is not None else mock_gateway["url"]
    )
    if session_token is None:
        env.pop("EGG_SESSION_TOKEN", None)
    else:
        env["EGG_SESSION_TOKEN"] = session_token
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(WRAPPER), *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


# -----------------------------------------------------------------------------
# Pre-flight checks
# -----------------------------------------------------------------------------


class TestEnvChecks:
    def test_missing_session_token_fails_closed(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["page", "get", "12345"], session_token=None)
        assert proc.returncode != 0
        assert "egg_session_token" in proc.stderr.lower() or "session token" in proc.stderr.lower()

    def test_missing_gateway_fails_closed(self, mock_gateway, tmp_path):
        # Use a port we know is closed.  127.0.0.1:1 is invalid for a server.
        proc = _run_wrapper(
            mock_gateway,
            ["page", "get", "12345"],
            gateway_url_override="http://127.0.0.1:1",
        )
        assert proc.returncode != 0
        assert "gateway" in proc.stderr.lower()


# -----------------------------------------------------------------------------
# page get
# -----------------------------------------------------------------------------


class TestPageGet:
    def test_builds_request_and_prints_data(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 200,
                "body": {"success": True, "data": {"id": "12345"}},
            }
        )
        proc = _run_wrapper(mock_gateway, ["page", "get", "12345"])
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/confluence/page/get"
        assert rec["body"] == {"pageId": "12345"}
        assert rec["authorization"] == "Bearer test-session-token"
        out = json.loads(proc.stdout)
        assert out == {"id": "12345"}

    def test_body_format_flag_forwarded(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {}}}
        )
        proc = _run_wrapper(
            mock_gateway,
            ["page", "get", "12345", "--body-format", "storage,atlas_doc_format"],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"] == {
            "pageId": "12345",
            "bodyFormat": ["storage", "atlas_doc_format"],
        }

    def test_expand_flag_forwarded(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {}}}
        )
        proc = _run_wrapper(
            mock_gateway,
            ["page", "get", "12345", "--expand", "history,version"],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["expand"] == ["history", "version"]

    def test_missing_page_id_fails(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["page", "get"])
        assert proc.returncode != 0
        assert "pageid required" in proc.stderr.lower()

    def test_403_response_prints_error(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 403,
                "body": {
                    "success": False,
                    "message": "Confluence space not allowlisted",
                    "details": {"spaceKey": "SECRET"},
                },
            }
        )
        proc = _run_wrapper(mock_gateway, ["page", "get", "12345"])
        assert proc.returncode != 0
        assert "allowlist" in proc.stderr.lower() or "forbidden" in proc.stderr.lower()

    def test_413_response_hint(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 413,
                "body": {
                    "success": False,
                    "message": "Confluence response too large",
                },
            }
        )
        proc = _run_wrapper(mock_gateway, ["page", "get", "12345"])
        assert proc.returncode != 0
        assert "too large" in proc.stderr.lower()

    def test_429_response_hint(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 429,
                "body": {"success": False, "message": "Rate limited"},
            }
        )
        proc = _run_wrapper(mock_gateway, ["page", "get", "12345"])
        assert proc.returncode != 0
        assert "rate limit" in proc.stderr.lower()


# -----------------------------------------------------------------------------
# page descendants
# -----------------------------------------------------------------------------


class TestPageDescendants:
    def test_happy_path(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {}}}
        )
        proc = _run_wrapper(
            mock_gateway,
            [
                "page",
                "descendants",
                "12345",
                "--depth",
                "2",
                "--limit",
                "10",
                "--cursor",
                "TOK-abc",
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/confluence/page/descendants"
        assert rec["body"]["pageId"] == "12345"
        assert rec["body"]["limit"] == 10
        assert rec["body"]["cursor"] == "TOK-abc"

    def test_non_int_limit_fails(self, mock_gateway):
        proc = _run_wrapper(
            mock_gateway,
            ["page", "descendants", "12345", "--limit", "bad"],
        )
        assert proc.returncode != 0
        assert "integer" in proc.stderr.lower() or "--limit" in proc.stderr.lower()


# -----------------------------------------------------------------------------
# page footer-comments — --include-replies
# -----------------------------------------------------------------------------


class TestFooterComments:
    def test_include_replies_toggle_reaches_body(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {}}}
        )
        proc = _run_wrapper(
            mock_gateway,
            [
                "page",
                "footer-comments",
                "12345",
                "--include-replies",
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/confluence/page/footer-comments"
        assert rec["body"]["pageId"] == "12345"
        assert rec["body"]["includeReplies"] is True

    def test_default_include_replies_is_false(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {}}}
        )
        proc = _run_wrapper(
            mock_gateway,
            ["page", "footer-comments", "12345"],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["includeReplies"] is False


# -----------------------------------------------------------------------------
# page inline-comments
# -----------------------------------------------------------------------------


class TestInlineComments:
    def test_happy_path(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {}}}
        )
        proc = _run_wrapper(mock_gateway, ["page", "inline-comments", "12345"])
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/confluence/page/inline-comments"
        assert rec["body"]["pageId"] == "12345"


# -----------------------------------------------------------------------------
# space pages / list
# -----------------------------------------------------------------------------


class TestSpaceVerbs:
    def test_space_list(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {}}}
        )
        proc = _run_wrapper(mock_gateway, ["space", "list", "--limit", "10"])
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/confluence/space/list"
        assert rec["body"]["limit"] == 10

    def test_space_pages(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {}}}
        )
        proc = _run_wrapper(mock_gateway, ["space", "pages", "ENG", "--limit", "10"])
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/confluence/space/pages"
        assert rec["body"]["spaceKey"] == "ENG"
        assert rec["body"]["limit"] == 10

    def test_space_pages_missing_key_fails(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["space", "pages"])
        assert proc.returncode != 0
        assert "spacekey required" in proc.stderr.lower()


# -----------------------------------------------------------------------------
# search — happy + adversarial passthrough
# -----------------------------------------------------------------------------


class TestSearch:
    def test_happy_path(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {"results": []}}}
        )
        proc = _run_wrapper(
            mock_gateway,
            [
                "search",
                'space = ENG AND text ~ "RFC"',
                "--limit",
                "25",
                "--cursor",
                "TOK-1",
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/confluence/search"
        assert rec["body"] == {
            "cql": 'space = ENG AND text ~ "RFC"',
            "limit": 25,
            "cursor": "TOK-1",
        }

    def test_search_403_from_gateway(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 403,
                "body": {
                    "success": False,
                    "message": "CQL rejected: space under OR",
                    "details": {"reason": "space under OR"},
                },
            }
        )
        proc = _run_wrapper(
            mock_gateway,
            ["search", "space = ENG OR space = SEC"],
        )
        assert proc.returncode != 0
        assert "rejected" in proc.stderr.lower()


# -----------------------------------------------------------------------------
# execute — happy + denied
# -----------------------------------------------------------------------------


class TestExecute:
    def test_happy_get(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {"id": "1"}}}
        )
        proc = _run_wrapper(
            mock_gateway,
            ["execute", "GET", "api/v2/pages/12345"],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/confluence/execute"
        assert rec["body"]["method"] == "GET"
        assert rec["body"]["path"] == "api/v2/pages/12345"

    def test_happy_with_query(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {}}}
        )
        proc = _run_wrapper(
            mock_gateway,
            [
                "execute",
                "GET",
                "api/v2/pages/12345",
                "--query",
                "body-format=storage,expand=history",
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["query"] == {
            "body-format": "storage",
            "expand": "history",
        }

    def test_denied_method_returns_error(self, mock_gateway):
        """Wrapper forwards any verb; gateway is the enforcement surface."""
        mock_gateway["server"].response_queue.append(
            {
                "status": 403,
                "body": {
                    "success": False,
                    "message": "Confluence API call rejected: HTTP method 'DELETE' not allowed",
                    "details": {"method": "DELETE"},
                },
            }
        )
        proc = _run_wrapper(
            mock_gateway,
            ["execute", "DELETE", "api/v2/pages/12345"],
        )
        assert proc.returncode != 0
        assert "rejected" in proc.stderr.lower() or "forbidden" in proc.stderr.lower()


# -----------------------------------------------------------------------------
# help / unknown
# -----------------------------------------------------------------------------


class TestUsage:
    def test_help_exits_zero(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["help"])
        assert proc.returncode == 0
        assert "confluence" in proc.stdout.lower()

    def test_unknown_subcommand(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["unknown"])
        assert proc.returncode != 0
        assert "unknown" in proc.stderr.lower()
