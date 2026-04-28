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

    def test_help_includes_write_verbs(self, mock_gateway):
        """The wrapper's ``show_usage`` must mention all four #1924 write
        verbs so an agent grepping ``jira help`` discovers them."""
        proc = _run_wrapper(mock_gateway, ["help"])
        assert proc.returncode == 0
        out = proc.stdout + proc.stderr
        for snippet in (
            "jira ticket create",
            "jira ticket edit",
            "jira ticket comment add",
            "jira link create",
        ):
            assert snippet in out, f"missing {snippet!r} from `jira help`"

    def test_unknown_verb(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["transition", "ENG-1"])
        assert proc.returncode != 0
        assert "unknown" in proc.stderr.lower()


# =============================================================================
# Write subcommands (issue #1924)
# =============================================================================


def _enqueue_success(mock_gateway, data: dict | None = None) -> None:
    mock_gateway["server"].response_queue.append(
        {
            "status": 200,
            "body": {"success": True, "data": data or {}},
        }
    )


class TestTicketCreate:
    """``jira ticket create`` — POST /api/v1/jira/ticket/create."""

    def test_minimal_required_flags(self, mock_gateway):
        _enqueue_success(
            mock_gateway,
            {
                "status": "created",
                "key": "ENG-1",
                "id": "10001",
                "browse_url": "https://e.atlassian.net/browse/ENG-1",
            },
        )
        proc = _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "create",
                "--project",
                "ENG",
                "--type",
                "Task",
                "--summary",
                "hello",
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/jira/ticket/create"
        assert rec["body"] == {
            "project": "ENG",
            "issuetype": "Task",
            "summary": "hello",
        }
        # JSON envelope printed to stdout on success.
        out = json.loads(proc.stdout)
        assert out["key"] == "ENG-1"

    def test_all_flags_forwarded(self, mock_gateway):
        _enqueue_success(mock_gateway, {"status": "created", "key": "ENG-2"})
        proc = _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "create",
                "--project",
                "ENG",
                "--type",
                "Task",
                "--summary",
                "hi",
                "--description",
                "long body",
                "--labels",
                "alpha,beta",
                "--parent",
                "ENG-100",
                "--idempotency-key",
                "k-1",
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"] == {
            "project": "ENG",
            "issuetype": "Task",
            "summary": "hi",
            "description": "long body",
            "labels": ["alpha", "beta"],
            "parent": "ENG-100",
            "idempotencyKey": "k-1",
        }

    def test_epic_link_flag(self, mock_gateway):
        _enqueue_success(mock_gateway, {"status": "created", "key": "ENG-3"})
        proc = _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "create",
                "--project",
                "ENG",
                "--type",
                "Task",
                "--summary",
                "hi",
                "--epic-link",
                "ENG-99",
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["epicLink"] == "ENG-99"

    def test_description_file_read(self, mock_gateway, tmp_path):
        path = tmp_path / "desc.txt"
        path.write_text("body from file\nsecond line")
        _enqueue_success(mock_gateway, {"status": "created", "key": "ENG-1"})
        proc = _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "create",
                "--project",
                "ENG",
                "--type",
                "Task",
                "--summary",
                "hi",
                "--description-file",
                str(path),
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["description"] == "body from file\nsecond line"

    def test_description_stdin(self, mock_gateway):
        _enqueue_success(mock_gateway, {"status": "created", "key": "ENG-1"})
        env = os.environ.copy()
        env["GATEWAY_URL"] = mock_gateway["url"]
        env["EGG_SESSION_TOKEN"] = "tok"
        proc = subprocess.run(
            [
                "bash",
                str(WRAPPER),
                "ticket",
                "create",
                "--project",
                "ENG",
                "--type",
                "Task",
                "--summary",
                "hi",
                "--description-stdin",
            ],
            input="from stdin",
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["description"] == "from stdin"

    def test_mutually_exclusive_description_flags_rejected(self, mock_gateway, tmp_path):
        path = tmp_path / "desc.txt"
        path.write_text("from file")
        # No response queued — wrapper should fail before the gateway call.
        proc = _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "create",
                "--project",
                "ENG",
                "--type",
                "Task",
                "--summary",
                "hi",
                "--description",
                "literal",
                "--description-file",
                str(path),
            ],
        )
        assert proc.returncode != 0
        assert "mutually exclusive" in proc.stderr.lower()
        # No upstream call recorded.
        assert mock_gateway["server"].recorded == []

    def test_missing_required_flag_rejected(self, mock_gateway):
        proc = _run_wrapper(
            mock_gateway,
            ["ticket", "create", "--project", "ENG", "--summary", "hi"],
        )
        assert proc.returncode != 0
        assert "required" in proc.stderr.lower()
        assert mock_gateway["server"].recorded == []

    def test_unknown_flag_rejected(self, mock_gateway):
        proc = _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "create",
                "--project",
                "ENG",
                "--type",
                "Task",
                "--summary",
                "hi",
                "--bogus",
                "x",
            ],
        )
        assert proc.returncode != 0
        assert "unknown" in proc.stderr.lower()

    def test_gateway_400_surfaces_error(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 400,
                "body": {
                    "success": False,
                    "message": "summary exceeds maximum length (255 chars)",
                },
            }
        )
        proc = _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "create",
                "--project",
                "ENG",
                "--type",
                "Task",
                "--summary",
                "x" * 1000,
            ],
        )
        assert proc.returncode != 0
        assert "exceeds maximum" in proc.stderr.lower()


class TestTicketEdit:
    """``jira ticket edit`` — POST /api/v1/jira/ticket/edit."""

    def test_summary_change(self, mock_gateway):
        _enqueue_success(mock_gateway, {"status": "updated", "key": "ENG-1"})
        proc = _run_wrapper(
            mock_gateway,
            ["ticket", "edit", "ENG-1", "--summary", "new"],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/jira/ticket/edit"
        # Default notify=true (gateway default is false; wrapper sends explicit
        # true unless --no-notify).
        assert rec["body"]["ticket"] == "ENG-1"
        assert rec["body"]["summary"] == "new"
        assert rec["body"]["notifyUsers"] is True

    def test_no_notify_flag(self, mock_gateway):
        _enqueue_success(mock_gateway, {"status": "updated", "key": "ENG-1"})
        proc = _run_wrapper(
            mock_gateway,
            ["ticket", "edit", "ENG-1", "--summary", "x", "--no-notify"],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["notifyUsers"] is False

    def test_replace_labels(self, mock_gateway):
        _enqueue_success(mock_gateway, {"status": "updated", "key": "ENG-1"})
        proc = _run_wrapper(
            mock_gateway,
            ["ticket", "edit", "ENG-1", "--labels", "a,b,c"],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["labels"] == ["a", "b", "c"]
        # No mixing.
        assert "addLabels" not in rec["body"]
        assert "removeLabels" not in rec["body"]

    def test_incremental_labels(self, mock_gateway):
        _enqueue_success(mock_gateway, {"status": "updated", "key": "ENG-1"})
        proc = _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "edit",
                "ENG-1",
                "--add-labels",
                "x,y",
                "--remove-labels",
                "z",
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["addLabels"] == ["x", "y"]
        assert rec["body"]["removeLabels"] == ["z"]
        assert "labels" not in rec["body"]

    def test_mixed_labels_modes_rejected_client_side(self, mock_gateway):
        proc = _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "edit",
                "ENG-1",
                "--labels",
                "a",
                "--add-labels",
                "b",
            ],
        )
        assert proc.returncode != 0
        assert "mutually exclusive" in proc.stderr.lower()
        # Wrapper bails before contacting the gateway.
        assert mock_gateway["server"].recorded == []

    def test_description_file(self, mock_gateway, tmp_path):
        path = tmp_path / "d.txt"
        path.write_text("new body")
        _enqueue_success(mock_gateway, {"status": "updated", "key": "ENG-1"})
        proc = _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "edit",
                "ENG-1",
                "--description-file",
                str(path),
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["description"] == "new body"

    def test_missing_ticket_key(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["ticket", "edit"])
        assert proc.returncode != 0
        assert "ticket" in proc.stderr.lower()

    def test_unknown_flag_rejected(self, mock_gateway):
        proc = _run_wrapper(
            mock_gateway,
            ["ticket", "edit", "ENG-1", "--summary", "x", "--bogus", "y"],
        )
        assert proc.returncode != 0
        assert "unknown" in proc.stderr.lower()


class TestTicketCommentAdd:
    """``jira ticket comment add`` — POST /api/v1/jira/ticket/comment/add."""

    def test_inline_body(self, mock_gateway):
        _enqueue_success(mock_gateway, {"id": "10010"})
        proc = _run_wrapper(
            mock_gateway,
            ["ticket", "comment", "add", "ENG-1", "--body", "hello"],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/jira/ticket/comment/add"
        assert rec["body"] == {"ticket": "ENG-1", "body": "hello"}

    def test_body_file(self, mock_gateway, tmp_path):
        path = tmp_path / "c.txt"
        path.write_text("from file")
        _enqueue_success(mock_gateway, {"id": "10010"})
        proc = _run_wrapper(
            mock_gateway,
            ["ticket", "comment", "add", "ENG-1", "--body-file", str(path)],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["body"] == "from file"

    def test_body_stdin(self, mock_gateway):
        _enqueue_success(mock_gateway, {"id": "10010"})
        env = os.environ.copy()
        env["GATEWAY_URL"] = mock_gateway["url"]
        env["EGG_SESSION_TOKEN"] = "tok"
        proc = subprocess.run(
            [
                "bash",
                str(WRAPPER),
                "ticket",
                "comment",
                "add",
                "ENG-1",
                "--body-stdin",
            ],
            input="from stdin",
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["body"] == "from stdin"

    def test_idempotency_key(self, mock_gateway):
        _enqueue_success(mock_gateway, {"id": "10010"})
        proc = _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "comment",
                "add",
                "ENG-1",
                "--body",
                "hi",
                "--idempotency-key",
                "k-1",
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["idempotencyKey"] == "k-1"

    def test_missing_body_flag_rejected(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["ticket", "comment", "add", "ENG-1"])
        assert proc.returncode != 0
        assert "body" in proc.stderr.lower()
        assert mock_gateway["server"].recorded == []

    def test_mutually_exclusive_body_flags(self, mock_gateway, tmp_path):
        path = tmp_path / "x.txt"
        path.write_text("x")
        proc = _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "comment",
                "add",
                "ENG-1",
                "--body",
                "hi",
                "--body-file",
                str(path),
            ],
        )
        assert proc.returncode != 0
        assert "mutually exclusive" in proc.stderr.lower()
        assert mock_gateway["server"].recorded == []

    def test_missing_ticket_key(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["ticket", "comment", "add"])
        assert proc.returncode != 0
        assert mock_gateway["server"].recorded == []

    def test_unknown_subcommand(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["ticket", "comment", "remove", "ENG-1"])
        assert proc.returncode != 0
        assert "unknown" in proc.stderr.lower()


class TestLinkCreate:
    """``jira link create`` — POST /api/v1/jira/issue-link/create."""

    def test_minimal_flags(self, mock_gateway):
        _enqueue_success(
            mock_gateway,
            {
                "status": "created",
                "inwardIssue": "ENG-1",
                "outwardIssue": "ENG-2",
                "type": "Blocks",
            },
        )
        proc = _run_wrapper(
            mock_gateway,
            [
                "link",
                "create",
                "--type",
                "Blocks",
                "--inward",
                "ENG-1",
                "--outward",
                "ENG-2",
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["path"] == "/api/v1/jira/issue-link/create"
        assert rec["body"] == {
            "type": "Blocks",
            "inwardIssue": "ENG-1",
            "outwardIssue": "ENG-2",
        }

    def test_with_comment_and_idempotency(self, mock_gateway):
        _enqueue_success(mock_gateway, {"status": "created"})
        proc = _run_wrapper(
            mock_gateway,
            [
                "link",
                "create",
                "--type",
                "Relates",
                "--inward",
                "ENG-1",
                "--outward",
                "ENG-2",
                "--comment",
                "see issue #1924",
                "--idempotency-key",
                "k-1",
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["comment"] == "see issue #1924"
        assert rec["body"]["idempotencyKey"] == "k-1"

    def test_comment_file(self, mock_gateway, tmp_path):
        path = tmp_path / "c.txt"
        path.write_text("from file")
        _enqueue_success(mock_gateway, {"status": "created"})
        proc = _run_wrapper(
            mock_gateway,
            [
                "link",
                "create",
                "--type",
                "Blocks",
                "--inward",
                "ENG-1",
                "--outward",
                "ENG-2",
                "--comment-file",
                str(path),
            ],
        )
        assert proc.returncode == 0, proc.stderr
        rec = mock_gateway["server"].recorded[-1]
        assert rec["body"]["comment"] == "from file"

    def test_missing_required_flag(self, mock_gateway):
        proc = _run_wrapper(
            mock_gateway,
            ["link", "create", "--type", "Blocks", "--inward", "ENG-1"],
        )
        assert proc.returncode != 0
        assert "required" in proc.stderr.lower()
        assert mock_gateway["server"].recorded == []

    def test_mutually_exclusive_comment_flags(self, mock_gateway, tmp_path):
        path = tmp_path / "x.txt"
        path.write_text("x")
        proc = _run_wrapper(
            mock_gateway,
            [
                "link",
                "create",
                "--type",
                "Blocks",
                "--inward",
                "ENG-1",
                "--outward",
                "ENG-2",
                "--comment",
                "literal",
                "--comment-file",
                str(path),
            ],
        )
        assert proc.returncode != 0
        assert "mutually exclusive" in proc.stderr.lower()

    def test_unknown_flag(self, mock_gateway):
        proc = _run_wrapper(
            mock_gateway,
            [
                "link",
                "create",
                "--type",
                "Blocks",
                "--inward",
                "ENG-1",
                "--outward",
                "ENG-2",
                "--bogus",
                "y",
            ],
        )
        assert proc.returncode != 0
        assert "unknown" in proc.stderr.lower()

    def test_unknown_link_subcommand(self, mock_gateway):
        proc = _run_wrapper(mock_gateway, ["link", "delete", "x"])
        assert proc.returncode != 0
        assert "unknown" in proc.stderr.lower()

    def test_gateway_400_surfaces_error(self, mock_gateway):
        mock_gateway["server"].response_queue.append(
            {
                "status": 400,
                "body": {
                    "success": False,
                    "message": "Link type 'Cloners' not in allowlist",
                },
            }
        )
        proc = _run_wrapper(
            mock_gateway,
            [
                "link",
                "create",
                "--type",
                "Cloners",
                "--inward",
                "ENG-1",
                "--outward",
                "ENG-2",
            ],
        )
        assert proc.returncode != 0
        assert "allowlist" in proc.stderr.lower()


class TestAuthHeaderOnWriteVerbs:
    """Every write subcommand must send the Bearer session token."""

    def _common_env(self, mock_gateway):
        return mock_gateway

    def test_create_carries_bearer(self, mock_gateway):
        _enqueue_success(mock_gateway, {"status": "created", "key": "ENG-1"})
        _run_wrapper(
            mock_gateway,
            [
                "ticket",
                "create",
                "--project",
                "ENG",
                "--type",
                "Task",
                "--summary",
                "x",
            ],
        )
        rec = mock_gateway["server"].recorded[-1]
        assert rec["authorization"] == "Bearer test-session-token"

    def test_edit_carries_bearer(self, mock_gateway):
        _enqueue_success(mock_gateway, {"status": "updated", "key": "ENG-1"})
        _run_wrapper(mock_gateway, ["ticket", "edit", "ENG-1", "--summary", "x"])
        rec = mock_gateway["server"].recorded[-1]
        assert rec["authorization"] == "Bearer test-session-token"

    def test_comment_add_carries_bearer(self, mock_gateway):
        _enqueue_success(mock_gateway, {"id": "10010"})
        _run_wrapper(
            mock_gateway,
            ["ticket", "comment", "add", "ENG-1", "--body", "hi"],
        )
        rec = mock_gateway["server"].recorded[-1]
        assert rec["authorization"] == "Bearer test-session-token"

    def test_link_create_carries_bearer(self, mock_gateway):
        _enqueue_success(mock_gateway, {"status": "created"})
        _run_wrapper(
            mock_gateway,
            [
                "link",
                "create",
                "--type",
                "Blocks",
                "--inward",
                "ENG-1",
                "--outward",
                "ENG-2",
            ],
        )
        rec = mock_gateway["server"].recorded[-1]
        assert rec["authorization"] == "Bearer test-session-token"
