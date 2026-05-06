"""MCP tool handler tests for list_agent_local_commits / salvage_agent_commits (#2429)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from egg_config.constants import TEST_GATEWAY_PORT
from mcp_tools import PIPELINE_TOOLS, PipelineToolHandler


@pytest.fixture
def handler():
    return PipelineToolHandler(
        orchestrator_url="http://localhost:9849",
        gateway_url=f"http://test-gateway:{TEST_GATEWAY_PORT}",
    )


class TestSchemaRegistration:
    def test_both_tools_are_registered(self):
        names = {t["name"] for t in PIPELINE_TOOLS}
        assert "list_agent_local_commits" in names
        assert "salvage_agent_commits" in names

    def test_required_field_is_task_id_only(self):
        for name in ("list_agent_local_commits", "salvage_agent_commits"):
            tool = next(t for t in PIPELINE_TOOLS if t["name"] == name)
            assert tool["inputSchema"]["required"] == ["task_id"]


class TestListAgentLocalCommits:
    def test_no_filter(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "data": {
                    "pipeline_id": "issue-99",
                    "worktrees": [
                        {
                            "worktree_id": "issue-99-coder",
                            "agent_role": "coder",
                            "slice_id": None,
                            "commits": [{"sha": "abc", "summary": "x", "files_changed": 1}],
                            "assigned_branch": "egg/issue-99/work",
                            "anchor_ref": "refs/remotes/origin/egg/issue-99/work",
                            "error": None,
                        }
                    ],
                }
            }
            result = handler.handle_tool_call(
                "list_agent_local_commits",
                {"task_id": "issue-99"},
            )

        mock_req.assert_called_once_with("/api/v1/pipelines/issue-99/local-commits")
        assert result["pipeline_id"] == "issue-99"
        assert result["n_worktrees"] == 1
        assert result["n_commits"] == 1
        assert result["worktrees"][0]["agent_role"] == "coder"

    def test_with_role_and_slice_filters(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"data": {"pipeline_id": "issue-99", "worktrees": []}}
            handler.handle_tool_call(
                "list_agent_local_commits",
                {"task_id": "issue-99", "agent_role": "coder", "slice_id": "slice-2"},
            )
        # Query string must contain both filters.
        url = mock_req.call_args.args[0]
        assert url.startswith("/api/v1/pipelines/issue-99/local-commits?")
        assert "agent_role=coder" in url
        assert "slice_id=slice-2" in url

    def test_url_quotes_task_id(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"data": {"pipeline_id": "x", "worktrees": []}}
            handler.handle_tool_call(
                "list_agent_local_commits",
                {"task_id": "name with spaces"},
            )
        url = mock_req.call_args.args[0]
        assert "name%20with%20spaces" in url

    def test_request_failure_returns_error(self, handler):
        with patch.object(handler, "_make_request", side_effect=RuntimeError("down")):
            result = handler.handle_tool_call(
                "list_agent_local_commits",
                {"task_id": "issue-99"},
            )
        assert "error" in result
        assert "down" in result["error"]


class TestSalvageAgentCommits:
    def test_aggregates_response(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "data": {
                    "pipeline_id": "issue-99",
                    "results": [
                        {
                            "worktree_id": "issue-99-coder",
                            "agent_role": "coder",
                            "slice_id": None,
                            "recovery_ref": "egg/recovered/issue-99/coder/abc123def456",
                            "head_sha": "abc123def456",
                            "n_commits": 3,
                            "ok": True,
                            "error": None,
                        },
                        {
                            "worktree_id": "issue-99-tester",
                            "agent_role": "tester",
                            "slice_id": None,
                            "recovery_ref": None,
                            "head_sha": None,
                            "n_commits": 0,
                            "ok": True,
                            "error": None,
                        },
                        {
                            "worktree_id": "issue-99-documenter",
                            "agent_role": "documenter",
                            "slice_id": None,
                            "recovery_ref": None,
                            "head_sha": None,
                            "n_commits": 0,
                            "ok": False,
                            "error": "non_fast_forward: rejected",
                        },
                    ],
                }
            }
            result = handler.handle_tool_call(
                "salvage_agent_commits",
                {"task_id": "issue-99"},
            )

        # Posted to the salvage endpoint with launcher-auth on the
        # orchestrator side; the MCP layer just proxies.
        endpoint = mock_req.call_args.args[0]
        assert endpoint == "/api/v1/pipelines/issue-99/salvage"
        method = mock_req.call_args.kwargs.get("method")
        assert method == "POST"

        assert result["pipeline_id"] == "issue-99"
        assert result["n_worktrees"] == 3
        assert result["n_salvaged"] == 1
        assert result["n_failed"] == 1
        assert result["recovery_refs"] == [
            "egg/recovered/issue-99/coder/abc123def456",
        ]

    def test_with_role_filter(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"data": {"pipeline_id": "issue-99", "results": []}}
            handler.handle_tool_call(
                "salvage_agent_commits",
                {"task_id": "issue-99", "agent_role": "coder"},
            )
        endpoint = mock_req.call_args.args[0]
        assert endpoint.startswith("/api/v1/pipelines/issue-99/salvage?")
        assert "agent_role=coder" in endpoint

    def test_request_failure_returns_error(self, handler):
        with patch.object(handler, "_make_request", side_effect=RuntimeError("boom")):
            result = handler.handle_tool_call(
                "salvage_agent_commits",
                {"task_id": "issue-99"},
            )
        assert "error" in result
        assert "boom" in result["error"]
