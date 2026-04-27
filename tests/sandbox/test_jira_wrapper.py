"""
Tests for the sandbox ``jira`` CLI wrapper.

Subprocess-invokes the wrapper against a stdlib ``http.server`` mock gateway
and asserts:

- Each verb constructs the correct request path + JSON body.
- The ``Authorization: Bearer $EGG_SESSION_TOKEN`` header is always sent.
- 2xx responses print the ``data`` subtree on stdout and exit 0.
- Non-2xx responses print an error on stderr and exit non-zero.
- Missing ``EGG_SESSION_TOKEN`` fails closed.
- Missing gateway fails closed with the standard error banner.

Note: this file exercises the wrapper directly; the actual route
enforcement (private-mode gate, project allowlist, etc.) is covered in
``gateway/tests/test_jira_routes.py``.  The wrapper's only job is to
translate CLI args into a well-formed ``POST /api/v1/jira/*`` request and
surface the gateway's response.
"""

from __future__ import annotations

import json
import os
import socket
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
_CANONICAL = _REPO_ROOT / "sandbox" / "scripts" / "jira"


def _locate_wrapper() -> Path:
    """Return the wrapper path; skip the test if it is not present."""
    if _CANONICAL.exists():
        return _CANONICAL
    pytest.skip(
        f"sandbox jira wrapper not found at {_CANONICAL}.",
        allow_module_level=True,
    )


WRAPPER = _locate_wrapper()


# -----------------------------------------------------------------------------
# Mock gateway: a single-thread HTTP server that records every request.
# -----------------------------------------------------------------------------


class _RecordingHandler(BaseHTTPRequestHandler):
    """HTTP handler that records request path/body/headers and echoes a scripted
    response supplied by the test via ``server.response_queue``."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence default access-log chatter during tests.
        pass

    def do_GET(self):  # noqa: N802 — stdlib naming
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
    """Spin up a local HTTP server and yield its URL + control handles."""
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
    session_token: str = "test-session-token",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GATEWAY_URL"] = mock_gateway["url"]
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
# Happy-path verbs
# -----------------------------------------------------------------------------


class TestTicketGet:
    def test_builds_request_and_prints_data(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 200,
                "body": {"success": True, "data": {"key": "ENG-1"}},
            }
        )
        proc = _run_wrapper(mock_gateway, ["ticket", "get", "ENG-1"])
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/jira/ticket/get"
        assert rec["body"] == {"ticket": "ENG-1"}
        assert rec["authorization"] == "Bearer test-session-token"
        out = json.loads(proc.stdout)
        assert out == {"key": "ENG-1"}

    def test_fields_flag_forwarded(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {}}}
        )
        proc = _run_wrapper(
            mock_gateway,
            ["ticket", "get", "ENG-1", "--fields", "summary,status"],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"] == {
            "ticket": "ENG-1",
            "fields": ["summary", "status"],
        }

    def test_missing_key_fails(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["ticket", "get"])
        assert proc.returncode != 0
        assert "ticket key required" in proc.stderr.lower()

    def test_403_response_prints_error(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 403,
                "body": {
                    "success": False,
                    "message": "Jira project not allowlisted",
                    "details": {"project": "SEC"},
                },
            }
        )
        proc = _run_wrapper(mock_gateway, ["ticket", "get", "SEC-1"])
        assert proc.returncode != 0
        assert "not allowlisted" in proc.stderr.lower()

    def test_401_response_prints_auth_hint(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 401,
                "body": {
                    "success": False,
                    "message": "Unauthorized",
                },
            }
        )
        proc = _run_wrapper(mock_gateway, ["ticket", "get", "ENG-1"])
        assert proc.returncode != 0
        assert "authentication failed" in proc.stderr.lower()
        assert "session token" in proc.stderr.lower()

    def test_429_response_prints_rate_limit_hint(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 429,
                "body": {
                    "success": False,
                    "message": "Rate limited",
                },
            }
        )
        proc = _run_wrapper(mock_gateway, ["ticket", "get", "ENG-1"])
        assert proc.returncode != 0
        assert "rate limit exceeded" in proc.stderr.lower()


class TestTicketComments:
    def test_happy_path(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 200,
                "body": {"success": True, "data": {"comments": []}},
            }
        )
        proc = _run_wrapper(mock_gateway, ["ticket", "comments", "ENG-1"])
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/jira/ticket/comments"
        assert rec["body"] == {"ticket": "ENG-1"}

    def test_failure(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 503,
                "body": {
                    "success": False,
                    "message": "Jira credentials not configured on the gateway",
                },
            }
        )
        proc = _run_wrapper(mock_gateway, ["ticket", "comments", "ENG-1"])
        assert proc.returncode != 0
        assert "credentials" in proc.stderr.lower()


class TestSearch:
    def test_happy_path(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {"issues": []}}}
        )
        proc = _run_wrapper(
            mock_gateway,
            [
                "search",
                "project = ENG",
                "--max-results",
                "25",
                "--fields",
                "summary",
                "--next-page-token",
                "TOK-1",
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/jira/search"
        assert rec["body"] == {
            "jql": "project = ENG",
            "fields": ["summary"],
            "maxResults": 25,
            "nextPageToken": "TOK-1",
        }

    def test_non_int_max_results_fails(self, mock_gateway):
        proc = _run_wrapper(
            mock_gateway,
            ["search", "project = ENG", "--max-results", "bad"],
        )
        assert proc.returncode != 0
        assert "--max-results" in proc.stderr.lower() or "integer" in proc.stderr.lower()

    def test_search_403_from_gateway(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 403,
                "body": {
                    "success": False,
                    "message": "JQL rejected: project under OR",
                    "details": {"reason": "project under OR"},
                },
            }
        )
        proc = _run_wrapper(
            mock_gateway,
            ["search", "project = ENG OR project = SEC"],
        )
        assert proc.returncode != 0
        assert "rejected" in proc.stderr.lower()


class TestExecute:
    def test_happy_get(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {"key": "ENG"}}}
        )
        proc = _run_wrapper(
            mock_gateway,
            ["execute", "GET", "project/ENG"],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/jira/execute"
        assert rec["body"] == {"method": "GET", "path": "project/ENG"}

    def test_happy_with_query(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {"status": 200, "body": {"success": True, "data": {}}}
        )
        proc = _run_wrapper(
            mock_gateway,
            ["execute", "GET", "issue/ENG-1", "--query", "fields=summary,expand=renderedBody"],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["method"] == "GET"
        assert rec["body"]["path"] == "issue/ENG-1"
        assert rec["body"]["query"] == {
            "fields": "summary",
            "expand": "renderedBody",
        }

    def test_denied_method_returns_error(self, mock_gateway):
        """The wrapper forwards any verb; the gateway is the enforcement
        surface.  We verify the gateway's 403 response is surfaced correctly."""
        mock_gateway["server"].response_queue.append(
            {
                "status": 403,
                "body": {
                    "success": False,
                    "message": "Jira API call rejected: HTTP method 'DELETE' not allowed for Jira",
                    "details": {"method": "DELETE"},
                },
            }
        )
        proc = _run_wrapper(
            mock_gateway,
            ["execute", "DELETE", "issue/ENG-1"],
        )
        assert proc.returncode != 0
        assert "delete" in proc.stderr.lower() or "not allowed" in proc.stderr.lower()


class TestFailClosed:
    def test_missing_session_token_fails(self, mock_gateway):
        proc = _run_wrapper(
            mock_gateway,
            ["ticket", "get", "ENG-1"],
            session_token=None,
        )
        assert proc.returncode != 0
        assert "EGG_SESSION_TOKEN" in proc.stderr

    def test_missing_gateway_url_fails(self, tmp_path):
        env = os.environ.copy()
        env.pop("GATEWAY_URL", None)
        env.pop("EGG_SESSION_TOKEN", None)
        proc = subprocess.run(
            ["bash", str(WRAPPER), "ticket", "get", "ENG-1"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert proc.returncode != 0
        assert "GATEWAY_URL" in proc.stderr

    def test_gateway_unreachable_fails_closed(self):
        """If /api/v1/health is unreachable, the wrapper must refuse the call."""
        # Grab a free port that nothing is listening on.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = os.environ.copy()
        env["GATEWAY_URL"] = f"http://127.0.0.1:{port}"
        env["EGG_SESSION_TOKEN"] = "tok"
        proc = subprocess.run(
            ["bash", str(WRAPPER), "ticket", "get", "ENG-1"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert proc.returncode != 0
        assert "GATEWAY SIDECAR NOT AVAILABLE" in proc.stderr


class TestUsage:
    def test_help(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["help"])
        assert proc.returncode == 0
        assert "jira ticket get" in proc.stderr or "jira ticket get" in proc.stdout

    def test_unknown_verb(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["transition", "ENG-1"])
        assert proc.returncode != 0
        assert "unknown" in proc.stderr.lower()
