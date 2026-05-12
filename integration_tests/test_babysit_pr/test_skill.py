"""Integration tests for the ``babysit_pr`` MCP tool (skill handler).

The /babysit-pr slash-command skill is a thin UX wrapper around the
``babysit_pr`` MCP tool in ``orchestrator.mcp_tools``. These tests
exercise the tool handler's contract:

* Input validation (missing / negative / wrong-type pr_number, missing repo).
* Happy path posts ``mode=babysit`` to ``POST /api/v1/pipelines`` and
  calls ``POST /api/v1/pipelines/<id>/start``.
* 409 duplicate-pipeline response surfaces as a structured user-facing
  error.
* 400 fork-PR / merged-PR / empty-diff errors bubble the ``reason``
  from the orchestrator response so the skill can render a useful
  message to the operator.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest


def _make_http_error(code: int, body: dict) -> HTTPError:
    """Build an ``HTTPError`` with a JSON body the tool can decode."""
    return HTTPError(
        url="http://orchestrator/api/v1/pipelines",
        code=code,
        msg="",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(json.dumps(body).encode()),
    )


@pytest.fixture
def handler():
    """Return a ``PipelineToolHandler`` instance with no network I/O."""
    from mcp_tools import PipelineToolHandler

    return PipelineToolHandler(
        orchestrator_url="http://orchestrator",
        gateway_url="http://gateway",
    )


@pytest.mark.integration
class TestBabysitPRInputValidation:
    """Argument validation — no orchestrator calls on bad input."""

    def test_missing_pr_number(self, handler):
        result = handler._handle_babysit_pr({"repo": "owner/repo"})
        assert "error" in result
        assert "pr_number" in result["error"].lower()

    def test_negative_pr_number(self, handler):
        result = handler._handle_babysit_pr({"pr_number": -1, "repo": "owner/repo"})
        assert "error" in result
        assert "positive integer" in result["error"].lower()

    def test_zero_pr_number(self, handler):
        result = handler._handle_babysit_pr({"pr_number": 0, "repo": "owner/repo"})
        assert "error" in result

    def test_string_pr_number(self, handler):
        result = handler._handle_babysit_pr({"pr_number": "42", "repo": "owner/repo"})
        assert "error" in result
        assert "positive integer" in result["error"].lower()

    def test_missing_repo(self, handler):
        result = handler._handle_babysit_pr({"pr_number": 42})
        assert "error" in result
        assert "repo" in result["error"].lower()

    def test_empty_repo(self, handler):
        result = handler._handle_babysit_pr({"pr_number": 42, "repo": ""})
        assert "error" in result
        assert "repo" in result["error"].lower()


@pytest.mark.integration
class TestBabysitPRHappyPath:
    """Happy path posts correct payload and starts the pipeline."""

    def test_handler_constructed_ok(self, handler):
        # Sanity check that the fixture gives us a usable handler.
        assert handler.orchestrator_url == "http://orchestrator"

    def test_posts_mode_babysit_and_starts(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                {"data": {"pipeline": {"id": "pr-42"}}},  # create
                {"data": {}},  # start
            ]
            result = handler._handle_babysit_pr({"pr_number": 42, "repo": "owner/repo"})

        # POST /api/v1/pipelines with the right payload
        create_call = mock_req.call_args_list[0]
        assert create_call[0][0] == "/api/v1/pipelines"
        payload = create_call[1]["data"]
        assert payload["mode"] == "babysit"
        assert payload["pr_number"] == 42
        assert payload["repo"] == "owner/repo"
        assert payload["pipeline_id"] == "pr-42"

        # POST /api/v1/pipelines/pr-42/start
        start_call = mock_req.call_args_list[1]
        assert start_call[0][0] == "/api/v1/pipelines/pr-42/start"

        assert result == {
            "task_id": "pr-42",
            "status": "started",
            "message": "Babysit-pr cycle started for PR #42",
        }

    def test_forwards_optional_branch_and_base_branch(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                {"data": {"pipeline": {"id": "pr-7"}}},
                {"data": {}},
            ]
            handler._handle_babysit_pr(
                {
                    "pr_number": 7,
                    "repo": "owner/repo",
                    "branch": "feature-x",
                    "base_branch": "develop",
                }
            )

        create_call = mock_req.call_args_list[0]
        payload = create_call[1]["data"]
        assert payload["branch"] == "feature-x"
        assert payload["base_branch"] == "develop"

    def test_start_failure_returns_created_not_started(self, handler):
        """Pipeline created but start endpoint failed → report gracefully."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                {"data": {"pipeline": {"id": "pr-99"}}},
                Exception("start failed"),
            ]
            result = handler._handle_babysit_pr({"pr_number": 99, "repo": "owner/repo"})

        assert result["task_id"] == "pr-99"
        assert result["status"] == "created_not_started"
        assert "failed to start" in result["message"].lower()


@pytest.mark.integration
class TestBabysitPRErrorPaths:
    """409 duplicate and 400 fork/merged/empty-diff errors surface clearly."""

    def test_409_duplicate_pipeline(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = _make_http_error(
                409,
                {
                    "message": "Pipeline pr-42 already exists",
                    "details": {
                        "reason": "duplicate_pipeline",
                        "existing_pipeline_id": "pr-42",
                        "existing_status": "running",
                        "existing_phase": "implement",
                    },
                },
            )
            result = handler._handle_babysit_pr({"pr_number": 42, "repo": "owner/repo"})

        assert "error" in result
        assert "already exists" in result["error"].lower()
        assert result["existing_pipeline_id"] == "pr-42"
        assert result["existing_status"] == "running"
        assert result["existing_phase"] == "implement"

    def test_400_fork_pr(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = _make_http_error(
                400,
                {
                    "message": "PR #42 is from a fork (forker/repo).",
                    "details": {"reason": "pr_from_fork", "pr_number": 42},
                },
            )
            result = handler._handle_babysit_pr({"pr_number": 42, "repo": "owner/repo"})

        assert "error" in result
        assert "fork" in result["error"].lower()
        assert result["reason"] == "pr_from_fork"

    def test_409_merged_pr(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = _make_http_error(
                409,
                {
                    "message": "PR #42 is already merged",
                    "details": {"reason": "pr_merged", "pr_number": 42},
                },
            )
            result = handler._handle_babysit_pr({"pr_number": 42, "repo": "owner/repo"})

        assert "error" in result
        assert result["reason"] == "pr_merged"

    def test_409_empty_diff(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = _make_http_error(
                409,
                {
                    "message": "PR #42 has no changed files",
                    "details": {"reason": "pr_empty_diff", "pr_number": 42},
                },
            )
            result = handler._handle_babysit_pr({"pr_number": 42, "repo": "owner/repo"})

        assert "error" in result
        assert result["reason"] == "pr_empty_diff"

    def test_invalid_config_json_string(self, handler):
        result = handler._handle_babysit_pr(
            {"pr_number": 42, "repo": "owner/repo", "config": "{not valid json"}
        )
        assert "error" in result
        assert "invalid config json" in result["error"].lower()

    def test_config_dict_forwarded(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                {"data": {"pipeline": {"id": "pr-42"}}},
                {"data": {}},
            ]
            handler._handle_babysit_pr(
                {
                    "pr_number": 42,
                    "repo": "owner/repo",
                    "config": {"hitl_gates": False},
                }
            )
        create_call = mock_req.call_args_list[0]
        payload = create_call[1]["data"]
        assert payload["config"] == {"hitl_gates": False}


@pytest.mark.integration
class TestBabysitPRToolRegistration:
    """The ``babysit_pr`` tool is registered in the MCP tool list."""

    def test_tool_exposed(self):
        from mcp_tools import PIPELINE_TOOLS

        names = [t["name"] for t in PIPELINE_TOOLS]
        assert "babysit_pr" in names

    def test_tool_schema_has_required_fields(self):
        from mcp_tools import PIPELINE_TOOLS

        tool = next(t for t in PIPELINE_TOOLS if t["name"] == "babysit_pr")
        required = set(tool["inputSchema"]["required"])
        # pr_number is intentionally not in required so the handler can return
        # a structured {"error": "pr_number must be a positive integer"} envelope
        # when it is omitted, rather than Pydantic raising "Field required".
        assert "repo" in required
        props = tool["inputSchema"]["properties"]
        assert "pr_number" in props
        assert props["pr_number"]["type"] == "integer"
        assert props["repo"]["type"] == "string"
