"""Tests for MCP tool handlers.

Tests cover the 10 new tools added for comprehensive platform interface:
- check_health, list_containers, get_container_logs, send_message,
  get_consensus_status, get_phase, get_pipeline_snapshot (orchestrator-backed)
- list_checkpoints, search_checkpoints, get_contract (gateway-backed)
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import HTTPError

import pytest
from egg_config.constants import TEST_GATEWAY_PORT
from mcp_tools import PipelineToolHandler


@pytest.fixture
def handler():
    """Create a PipelineToolHandler with a test gateway URL."""
    return PipelineToolHandler(
        orchestrator_url="http://localhost:9849",
        gateway_url=f"http://test-gateway:{TEST_GATEWAY_PORT}",
    )


def _mock_gateway_health_response(data):
    """Create a mock opener that returns the given JSON data for gateway health."""
    import json

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(data).encode()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_response
    return mock_opener


class TestCheckHealth:
    def test_both_healthy(self, handler):
        mock_opener = _mock_gateway_health_response(
            {
                "status": "healthy",
                "version": "1.0",
                "healthy_since": "2026-04-22T03:42:15+00:00",
                "last_unhealthy_at": "2026-04-22T03:42:14+00:00",
                "process_start_time": "2026-04-22T03:42:00+00:00",
                "recent_transitions": [
                    {"ts": "2026-04-22T03:42:14+00:00", "state": "unhealthy"},
                    {"ts": "2026-04-22T03:42:15+00:00", "state": "healthy"},
                ],
            }
        )
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "status": "healthy",
                "healthy_since": "2026-04-22T03:41:00+00:00",
                "last_unhealthy_at": None,
                "process_start_time": "2026-04-22T03:41:00+00:00",
                "recent_transitions": [
                    {"ts": "2026-04-22T03:41:00+00:00", "state": "healthy"},
                ],
            }
            with patch("urllib.request.build_opener", return_value=mock_opener):
                result = handler.handle_tool_call("check_health", {})

        assert result["healthy"] is True
        assert result["orchestrator"]["healthy"] is True
        assert result["gateway"]["healthy"] is True
        assert result["gateway"]["version"] == "1.0"

        # Readiness history flows through per issue #1855.
        assert result["orchestrator"]["healthy_since"] == "2026-04-22T03:41:00+00:00"
        assert result["orchestrator"]["last_unhealthy_at"] is None
        assert result["gateway"]["healthy_since"] == "2026-04-22T03:42:15+00:00"
        assert result["gateway"]["last_unhealthy_at"] == "2026-04-22T03:42:14+00:00"
        assert len(result["gateway"]["recent_transitions"]) == 2

    def test_orchestrator_unreachable(self, handler):
        mock_opener = _mock_gateway_health_response({"status": "healthy", "version": "1.0"})
        with patch.object(handler, "_make_request", side_effect=Exception("connection refused")):
            with patch("urllib.request.build_opener", return_value=mock_opener):
                result = handler.handle_tool_call("check_health", {})

        assert result["healthy"] is False
        assert result["orchestrator"]["healthy"] is False
        assert "unreachable" in result["orchestrator"]["status"]
        # Unreachable services report null history fields so callers can
        # distinguish "no data" from "observed unhealthy".
        assert result["orchestrator"]["healthy_since"] is None
        assert result["orchestrator"]["last_unhealthy_at"] is None
        assert result["orchestrator"]["recent_transitions"] == []

    def test_gateway_unreachable(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"status": "healthy"}
            with patch("urllib.request.build_opener") as mock_build:
                mock_opener = MagicMock()
                mock_opener.open.side_effect = Exception("connection refused")
                mock_build.return_value = mock_opener
                result = handler.handle_tool_call("check_health", {})

        assert result["healthy"] is False
        assert result["orchestrator"]["healthy"] is True
        assert result["gateway"]["healthy"] is False
        assert "unreachable" in result["gateway"]["status"]
        assert result["gateway"]["healthy_since"] is None
        assert result["gateway"]["last_unhealthy_at"] is None
        assert result["gateway"]["recent_transitions"] == []


class TestListContainers:
    def test_list_all(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"containers": [{"container_id": "abc", "status": "running"}]},
            }
            result = handler.handle_tool_call("list_containers", {"task_id": "issue-42"})

        mock_req.assert_called_once_with("/api/v1/pipelines/issue-42/containers?all=true")
        assert result["data"]["containers"][0]["container_id"] == "abc"

    def test_exclude_stopped(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True, "data": {"containers": []}}
            handler.handle_tool_call(
                "list_containers", {"task_id": "issue-42", "include_stopped": False}
            )

        mock_req.assert_called_once_with("/api/v1/pipelines/issue-42/containers?all=false")


class TestGetContainerLogs:
    def test_with_explicit_container_id(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True, "data": {"logs": "test output"}}
            result = handler.handle_tool_call(
                "get_container_logs",
                {"task_id": "issue-42", "container_id": "abc123", "lines": 50},
            )

        mock_req.assert_called_once_with(
            "/api/v1/pipelines/issue-42/containers/abc123/logs?tail=50"
        )
        assert result["logs"] == "test output"
        assert result["container_id"] == "abc123"
        assert result["agent_role"] is None
        assert result["status"] is None

    def test_auto_select_running(self, handler):
        containers_response = {
            "data": {
                "containers": [
                    {
                        "container_id": "stopped1",
                        "status": "exited",
                        "agent_role": "coder",
                        "started_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "container_id": "running1",
                        "status": "running",
                        "agent_role": "tester",
                        "started_at": "2026-01-01T01:00:00Z",
                    },
                ]
            }
        }
        logs_response = {"success": True, "data": {"logs": "tester logs"}}

        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [containers_response, logs_response]
            result = handler.handle_tool_call("get_container_logs", {"task_id": "issue-42"})

        assert result["container_id"] == "running1"
        assert result["agent_role"] == "tester"
        assert result["logs"] == "tester logs"

    def test_auto_select_by_role(self, handler):
        containers_response = {
            "data": {
                "containers": [
                    {
                        "container_id": "c1",
                        "status": "running",
                        "agent_role": "coder",
                        "started_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "container_id": "c2",
                        "status": "running",
                        "agent_role": "tester",
                        "started_at": "2026-01-01T01:00:00Z",
                    },
                ]
            }
        }
        logs_response = {"success": True, "data": {"logs": "coder logs"}}

        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [containers_response, logs_response]
            result = handler.handle_tool_call(
                "get_container_logs", {"task_id": "issue-42", "agent_role": "coder"}
            )

        assert result["container_id"] == "c1"
        assert result["agent_role"] == "coder"

    def test_no_containers(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"data": {"containers": []}}
            result = handler.handle_tool_call("get_container_logs", {"task_id": "issue-42"})

        assert "error" in result

    def test_auto_select_most_recent_stopped(self, handler):
        """When no running containers, picks the most recently started."""
        containers_response = {
            "data": {
                "containers": [
                    {
                        "container_id": "old",
                        "status": "exited",
                        "agent_role": "coder",
                        "started_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "container_id": "new",
                        "status": "exited",
                        "agent_role": "tester",
                        "started_at": "2026-01-02T00:00:00Z",
                    },
                ]
            }
        }
        logs_response = {"success": True, "data": {"logs": "latest"}}

        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [containers_response, logs_response]
            result = handler.handle_tool_call("get_container_logs", {"task_id": "issue-42"})

        assert result["container_id"] == "new"


class TestSendMessage:
    def test_send_basic(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True, "data": {"message": {}}}
            handler.handle_tool_call(
                "send_message",
                {"task_id": "issue-42", "to_role": "coder", "body": "Hello"},
            )

        mock_req.assert_called_once()
        call_args = mock_req.call_args
        assert call_args[1]["method"] == "POST"
        data = call_args[1]["data"]
        assert data["from_role"] == "overseer"
        assert data["to_role"] == "coder"
        assert data["body"] == "Hello"
        assert data["message_type"] == "STATUS"

    def test_send_with_subject(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True}
            handler.handle_tool_call(
                "send_message",
                {
                    "task_id": "issue-42",
                    "to_role": "tester",
                    "body": "check status",
                    "subject": "Status check",
                    "message_type": "STATUS",
                },
            )

        data = mock_req.call_args[1]["data"]
        assert data["subject"] == "Status check"
        assert data["message_type"] == "STATUS"


class TestGetConsensusStatus:
    def test_structured_consensus(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                # Pipeline GET
                {
                    "data": {
                        "pipeline": {
                            "id": "issue-42",
                            "current_phase": "implement",
                            "status": "running",
                        }
                    }
                },
                # Status GET
                {
                    "data": {
                        "concurrent": {
                            "consensus": {
                                "is_complete": False,
                                "blocking_agents": ["tester"],
                                "has_unresolved_nacks": True,
                                "unresolved_nacks": [
                                    {
                                        "reviewer": "reviewer_code",
                                        "producer": "coder",
                                        "reason": "bug",
                                    }
                                ],
                                "agents": {"coder": {"producer_phase": "PROPOSED"}},
                            }
                        }
                    }
                },
            ]
            result = handler.handle_tool_call("get_consensus_status", {"task_id": "issue-42"})

        assert result["consensus"]["is_complete"] is False
        assert result["consensus"]["blocking_agents"] == ["tester"]
        assert result["consensus"]["has_unresolved_nacks"] is True

    def test_fallback_to_messages(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                # Pipeline GET
                {
                    "data": {
                        "pipeline": {
                            "id": "issue-42",
                            "current_phase": "implement",
                            "status": "running",
                        }
                    }
                },
                # Status GET (no concurrent data)
                {"data": {}},
                # Messages GET
                {
                    "data": {
                        "messages": [
                            {"message_type": "CONSENSUS_PROPOSE", "from_role": "coder"},
                            {"message_type": "CONSENSUS_ACK", "from_role": "reviewer_code"},
                            {"message_type": "CONSENSUS_CONFIRMED", "from_role": "coder"},
                            {"message_type": "CONSENSUS_CONFIRMED", "from_role": "reviewer_code"},
                        ]
                    }
                },
            ]
            result = handler.handle_tool_call("get_consensus_status", {"task_id": "issue-42"})

        assert result["consensus"]["is_complete"] is True
        assert "note" in result["consensus"]

    def test_fallback_when_consensus_has_empty_agents(self, handler):
        """Regression test for #1229: empty agents dict should trigger fallback."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                # Pipeline GET
                {
                    "data": {
                        "pipeline": {
                            "id": "issue-42",
                            "current_phase": "refine",
                            "status": "running",
                        }
                    }
                },
                # Status GET — concurrent enabled but consensus has empty agents
                # (tracker lost after restart)
                {
                    "data": {
                        "concurrent": {
                            "enabled": True,
                            "consensus": {
                                "agents": {},
                                "is_complete": False,
                                "blocking_agents": [],
                            },
                        }
                    }
                },
                # Messages GET — BRC messages still in Redis
                {
                    "data": {
                        "messages": [
                            {"message_type": "CONSENSUS_PROPOSE", "from_role": "refiner"},
                            {
                                "message_type": "CONSENSUS_NACK",
                                "from_role": "reviewer_refine",
                                "to_role": "refiner",
                                "body": "Missing test coverage",
                            },
                        ]
                    }
                },
            ]
            result = handler.handle_tool_call("get_consensus_status", {"task_id": "issue-42"})

        assert result["consensus"]["has_unresolved_nacks"] is True
        assert "refiner" in result["consensus"]["blocking_agents"]
        assert "note" in result["consensus"]


class TestInferConsensusFromMessages:
    def test_all_confirmed(self, handler):
        messages = [
            {"message_type": "CONSENSUS_CONFIRMED", "from_role": "coder"},
            {"message_type": "CONSENSUS_CONFIRMED", "from_role": "tester"},
        ]
        result = handler._infer_consensus_from_messages(messages)
        assert result["is_complete"] is True
        assert len(result["blocking_agents"]) == 0

    def test_unresolved_nack(self, handler):
        messages = [
            {"message_type": "CONSENSUS_PROPOSE", "from_role": "coder"},
            {
                "message_type": "CONSENSUS_NACK",
                "from_role": "reviewer",
                "to_role": "coder",
                "body": "bug in line 42",
            },
        ]
        result = handler._infer_consensus_from_messages(messages)
        assert result["has_unresolved_nacks"] is True
        assert result["unresolved_nacks"][0]["reason"] == "bug in line 42"

    def test_nack_resolved_by_repropose(self, handler):
        messages = [
            {"message_type": "CONSENSUS_PROPOSE", "from_role": "coder"},
            {
                "message_type": "CONSENSUS_NACK",
                "from_role": "reviewer",
                "to_role": "coder",
                "body": "bug",
            },
            {"message_type": "CONSENSUS_PROPOSE", "from_role": "coder"},
        ]
        result = handler._infer_consensus_from_messages(messages)
        assert result["has_unresolved_nacks"] is False


class TestGetPhase:
    def test_get_phase(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"current_phase": "implement", "status": "running"},
            }
            result = handler.handle_tool_call("get_phase", {"task_id": "issue-42"})

        mock_req.assert_called_once_with("/api/v1/pipelines/issue-42/phase")
        assert result["data"]["current_phase"] == "implement"


class TestGetPipelineSnapshot:
    def test_full_snapshot(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                # Pipeline GET
                {"data": {"pipeline": {"id": "issue-42", "status": "running", "decisions": []}}},
                # Phase GET
                {"data": {"current_phase": "implement", "status": "running"}},
                # Status GET
                {"data": {"concurrent": {"consensus": {}}}},
                # Containers GET
                {"data": {"containers": [{"container_id": "c1"}]}},
                # Messages GET
                {"data": {"messages": [{"from_role": "coder"}]}},
            ]
            result = handler.handle_tool_call("get_pipeline_snapshot", {"task_id": "issue-42"})

        assert "pipeline" in result
        assert "phase" in result
        assert "containers" in result
        assert "recent_messages" in result
        assert "pending_decisions" in result

    def test_snapshot_without_optional(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                # Pipeline GET
                {"data": {"pipeline": {"id": "issue-42", "decisions": []}}},
                # Phase GET
                {"data": {}},
                # Status GET
                {"data": {}},
            ]
            result = handler.handle_tool_call(
                "get_pipeline_snapshot",
                {"task_id": "issue-42", "include_messages": False, "include_containers": False},
            )

        assert "pipeline" in result
        assert "containers" not in result
        assert "recent_messages" not in result


class TestListCheckpoints:
    def test_with_filters(self, handler):
        with patch.object(handler, "_make_gateway_request") as mock_gw:
            mock_gw.return_value = {
                "success": True,
                "data": {"checkpoints": [{"id": "ckpt-1"}]},
            }
            handler.handle_tool_call(
                "list_checkpoints",
                {"pipeline": "issue-42", "agent_type": "coder", "limit": 5},
            )

        call_url = mock_gw.call_args[0][0]
        assert "pipeline=issue-42" in call_url
        assert "agent_type=coder" in call_url
        assert "limit=5" in call_url

    def test_no_filters(self, handler):
        with patch.object(handler, "_make_gateway_request") as mock_gw:
            mock_gw.return_value = {"success": True, "data": {"checkpoints": []}}
            handler.handle_tool_call("list_checkpoints", {})

        call_url = mock_gw.call_args[0][0]
        assert "limit=20" in call_url

    def test_with_repo_param(self, handler):
        """Verify repo is forwarded as source_repo query param (#1514)."""
        with patch.object(handler, "_make_gateway_request") as mock_gw:
            mock_gw.return_value = {"success": True, "data": {"checkpoints": []}}
            handler.handle_tool_call(
                "list_checkpoints",
                {"repo": "owner/repo-checkpoints", "issue": 42},
            )

        call_url = mock_gw.call_args[0][0]
        assert "source_repo=owner%2Frepo-checkpoints" in call_url
        assert "issue=42" in call_url

    def test_without_repo_param_no_source_repo(self, handler):
        """Verify source_repo is not added when repo is not provided."""
        with patch.object(handler, "_make_gateway_request") as mock_gw:
            mock_gw.return_value = {"success": True, "data": {"checkpoints": []}}
            handler.handle_tool_call("list_checkpoints", {"pipeline": "issue-42"})

        call_url = mock_gw.call_args[0][0]
        assert "source_repo" not in call_url


class TestSearchCheckpoints:
    def test_text_filtering(self, handler):
        with patch.object(handler, "_make_gateway_request") as mock_gw:
            mock_gw.return_value = {
                "data": {
                    "checkpoints": [
                        {
                            "session_id": "s1",
                            "agent_type": "coder",
                            "pipeline_phase": "implement",
                            "pipeline_id": "issue-42",
                            "branch": "egg/issue-42",
                            "repo": "org/repo",
                            "session_status": "completed",
                        },
                        {
                            "session_id": "s2",
                            "agent_type": "tester",
                            "pipeline_phase": "implement",
                            "pipeline_id": "issue-99",
                            "branch": "egg/issue-99",
                            "repo": "org/repo",
                            "session_status": "failed",
                        },
                    ]
                }
            }
            result = handler.handle_tool_call("search_checkpoints", {"text": "issue-42"})

        assert result["total"] == 1
        assert result["checkpoints"][0]["session_id"] == "s1"
        assert "metadata only" in result["note"]

    def test_case_insensitive(self, handler):
        with patch.object(handler, "_make_gateway_request") as mock_gw:
            mock_gw.return_value = {
                "data": {
                    "checkpoints": [
                        {
                            "session_id": "s1",
                            "agent_type": "CODER",
                            "pipeline_phase": "",
                            "pipeline_id": "",
                            "branch": "",
                            "repo": "",
                            "session_status": "",
                        },
                    ]
                }
            }
            result = handler.handle_tool_call("search_checkpoints", {"text": "coder"})

        assert result["total"] == 1

    def test_with_repo_param(self, handler):
        """Verify repo is forwarded as source_repo query param (#1514)."""
        with patch.object(handler, "_make_gateway_request") as mock_gw:
            mock_gw.return_value = {"data": {"checkpoints": []}}
            handler.handle_tool_call(
                "search_checkpoints",
                {"text": "coder", "repo": "owner/repo-checkpoints"},
            )

        call_url = mock_gw.call_args[0][0]
        assert "source_repo=owner%2Frepo-checkpoints" in call_url

    def test_without_repo_param_no_source_repo(self, handler):
        """Verify source_repo is not added when repo is not provided."""
        with patch.object(handler, "_make_gateway_request") as mock_gw:
            mock_gw.return_value = {"data": {"checkpoints": []}}
            handler.handle_tool_call("search_checkpoints", {"text": "some-search"})

        call_url = mock_gw.call_args[0][0]
        assert "source_repo" not in call_url


class TestGetContract:
    def test_with_issue_number_and_active_pipeline(self, handler):
        """issue_number resolves pipeline_id from active pipelines list."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "data": {"pipelines": [{"id": "issue-42", "issue_number": 42}]}
            }
            with patch.object(handler, "_make_gateway_request") as mock_gw:
                mock_gw.return_value = {"success": True, "data": {"contract": {}}}
                handler.handle_tool_call("get_contract", {"issue_number": 42})

        mock_gw.assert_called_once_with("/api/v1/contract/42?pipeline_id=issue-42")

    def test_with_issue_number_picks_latest_pipeline(self, handler):
        """When multiple active pipelines match the issue, pick the latest by created_at."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "data": {
                    "pipelines": [
                        {"id": "old-run", "issue_number": 42, "created_at": "2026-04-01T10:00:00Z"},
                        {
                            "id": "newest-run",
                            "issue_number": 42,
                            "created_at": "2026-04-03T10:00:00Z",
                        },
                        {"id": "mid-run", "issue_number": 42, "created_at": "2026-04-02T10:00:00Z"},
                    ]
                }
            }
            with patch.object(handler, "_make_gateway_request") as mock_gw:
                mock_gw.return_value = {"success": True, "data": {"contract": {}}}
                handler.handle_tool_call("get_contract", {"issue_number": 42})

        mock_gw.assert_called_once_with("/api/v1/contract/42?pipeline_id=newest-run")

    def test_with_issue_number_no_active_pipeline(self, handler):
        """issue_number without matching active pipeline omits pipeline_id."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"data": {"pipelines": []}}
            with patch.object(handler, "_make_gateway_request") as mock_gw:
                mock_gw.return_value = {"success": True, "data": {"contract": {}}}
                handler.handle_tool_call("get_contract", {"issue_number": 42})

        mock_gw.assert_called_once_with("/api/v1/contract/42")

    def test_with_task_id_lookup(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"data": {"pipeline": {"issue_number": 42}}}
            with patch.object(handler, "_make_gateway_request") as mock_gw:
                mock_gw.return_value = {"success": True, "data": {"contract": {}}}
                handler.handle_tool_call("get_contract", {"task_id": "issue-42"})

        mock_gw.assert_called_once_with("/api/v1/contract/42?pipeline_id=issue-42")

    def test_missing_both_params(self, handler):
        result = handler.handle_tool_call("get_contract", {})
        assert "error" in result

    def test_task_id_without_issue(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"data": {"pipeline": {"issue_number": None}}}
            result = handler.handle_tool_call("get_contract", {"task_id": "prompt-based"})

        assert "error" in result

    def test_pipeline_lookup_failure_is_graceful(self, handler):
        """If listing pipelines fails, the request proceeds without pipeline_id."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = Exception("connection refused")
            with patch.object(handler, "_make_gateway_request") as mock_gw:
                mock_gw.return_value = {"success": True, "data": {"contract": {}}}
                handler.handle_tool_call("get_contract", {"issue_number": 42})

        mock_gw.assert_called_once_with("/api/v1/contract/42")


class TestGatewayAuth:
    def test_session_cached(self, handler):
        """Verify session token is cached after first creation."""
        handler._gateway_session_token = "cached-token"
        token = handler._ensure_gateway_session()
        assert token == "cached-token"

    def test_session_cleared_on_none(self, handler):
        """Verify new session is created when cache is empty."""
        assert handler._gateway_session_token is None
        with (
            patch.dict("os.environ", {"EGG_LAUNCHER_SECRET": "test-secret"}),
            patch("orchestrator.gateway_client.GatewayClient") as MockGW,
        ):
            mock_client = MagicMock()
            mock_session = MagicMock()
            mock_session.session_token = "new-token"
            mock_client.self_ip = "127.0.0.1"
            mock_client.register_session.return_value = mock_session
            MockGW.return_value = mock_client

            token = handler._ensure_gateway_session()

        assert token == "new-token"
        assert handler._gateway_session_token == "new-token"
        MockGW.assert_called_once_with(
            gateway_host="test-gateway",
            gateway_port=TEST_GATEWAY_PORT,
            launcher_secret="test-secret",
        )

    def test_session_registration_passes_pipeline_id(self, handler):
        """Verify register_session is called with pipeline_id='mcp-server' (#1514)."""
        assert handler._gateway_session_token is None
        with (
            patch.dict("os.environ", {"EGG_LAUNCHER_SECRET": "test-secret"}),
            patch("orchestrator.gateway_client.GatewayClient") as MockGW,
        ):
            mock_client = MagicMock()
            mock_session = MagicMock()
            mock_session.session_token = "new-token"
            mock_client.self_ip = "10.0.0.1"
            mock_client.register_session.return_value = mock_session
            MockGW.return_value = mock_client

            handler._ensure_gateway_session()

        mock_client.register_session.assert_called_once_with(
            container_id="mcp-server",
            container_ip="10.0.0.1",
            mode="public",
            pipeline_id="mcp-server",
        )

    def test_missing_launcher_secret(self, handler):
        """Verify RuntimeError when EGG_LAUNCHER_SECRET is not set."""
        assert handler._gateway_session_token is None
        with (
            patch.dict("os.environ", {}, clear=False),
            pytest.raises(RuntimeError, match="EGG_LAUNCHER_SECRET required"),
        ):
            # Ensure the env var is absent
            import os

            os.environ.pop("EGG_LAUNCHER_SECRET", None)
            handler._ensure_gateway_session()

    def test_401_retry(self, handler):
        """Verify 401 triggers session re-registration and retry."""
        from urllib.error import HTTPError

        handler._gateway_session_token = "stale-token"

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"data": "ok"}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        http_401 = HTTPError(url="http://test", code=401, msg="Unauthorized", hdrs={}, fp=None)
        calls = []

        def mock_open(req, timeout=None):
            calls.append(req.get_header("Authorization"))
            if len(calls) == 1:
                raise http_401
            return mock_response

        with (
            patch("urllib.request.build_opener") as mock_opener,
            patch.dict("os.environ", {"EGG_LAUNCHER_SECRET": "test-secret"}),
            patch("orchestrator.gateway_client.GatewayClient") as MockGW,
        ):
            mock_opener_inst = MagicMock()
            mock_opener_inst.open = mock_open
            mock_opener.return_value = mock_opener_inst

            mock_client = MagicMock()
            mock_session = MagicMock()
            mock_session.session_token = "fresh-token"
            mock_client.self_ip = "127.0.0.1"
            mock_client.register_session.return_value = mock_session
            MockGW.return_value = mock_client

            result = handler._make_gateway_request("/api/v1/test")

        assert result == {"data": "ok"}
        assert len(calls) == 2
        assert calls[0] == "Bearer stale-token"
        assert calls[1] == "Bearer fresh-token"
        assert handler._gateway_session_token == "fresh-token"


class TestToolRouting:
    """Verify all registered MCP tools are routed correctly."""

    def test_all_tools_registered(self, handler):
        from mcp_tools import PIPELINE_TOOLS

        tool_names = {t["name"] for t in PIPELINE_TOOLS}
        expected = {
            "submit_task",
            "get_status",
            "provide_input",
            "list_tasks",
            "cancel_task",
            "check_health",
            "list_containers",
            "get_container_logs",
            "send_message",
            "get_consensus_status",
            "get_phase",
            "get_pipeline_snapshot",
            "list_checkpoints",
            "search_checkpoints",
            "get_contract",
            "validate_config",
            "restart_agent",
            "restart_phase",
            "advance_phase",
            "start_pipeline",
            "start_phase",
            "complete_phase",
            "populate_contract",
            "babysit_pr",
            # Custom-phase primitive (#1762 run_agent_task)
            "run_agent_task",
            # Deployment-diagnostic tools (#1759)
            "get_deployment_context",
            "validate_deployment_manifests",
            "prune_stale_worktrees",
            "validate_network_isolation",
            "rebuild_and_rollout",
            "get_service_logs",
        }
        assert tool_names == expected

    def test_unknown_tool(self, handler):
        result = handler.handle_tool_call("nonexistent", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_checkpoint_tools_have_repo_property(self):
        """Verify list_checkpoints and search_checkpoints schemas include repo (#1514)."""
        from mcp_tools import PIPELINE_TOOLS

        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        for tool_name in ("list_checkpoints", "search_checkpoints"):
            schema = tools_by_name[tool_name]["inputSchema"]
            props = schema["properties"]
            assert "repo" in props, f"{tool_name} schema missing 'repo' property"
            assert props["repo"]["type"] == "string"
            assert "owner/repo" in props["repo"]["description"]


class TestStartPipeline:
    """Tests for the start_pipeline MCP tool (#2411)."""

    def test_tool_definition_exists(self):
        from mcp_tools import PIPELINE_TOOLS

        tool_names = [t["name"] for t in PIPELINE_TOOLS]
        assert "start_pipeline" in tool_names

    def test_tool_definition_requires_task_id(self):
        from mcp_tools import PIPELINE_TOOLS

        tool = next(t for t in PIPELINE_TOOLS if t["name"] == "start_pipeline")
        schema = tool["inputSchema"]
        assert "task_id" in schema.get("required", [])

    def test_calls_pipeline_start_endpoint(self, handler):
        """start_pipeline should POST to /pipelines/{id}/start, not /phase/start."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True, "data": {"status": "running"}}
            handler.handle_tool_call("start_pipeline", {"task_id": "issue-2411"})

        mock_req.assert_called_once()
        endpoint = mock_req.call_args[0][0]
        assert "/api/v1/pipelines/issue-2411/start" in endpoint
        assert "/phase/" not in endpoint
        assert mock_req.call_args.kwargs.get("method") == "POST"

    def test_url_encodes_task_id(self, handler):
        """task_id with reserved characters should be URL-encoded."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True}
            handler.handle_tool_call("start_pipeline", {"task_id": "issue-1/odd"})

        endpoint = mock_req.call_args[0][0]
        assert "issue-1%2Fodd" in endpoint

    def test_distinct_from_start_phase(self, handler):
        """start_pipeline and start_phase must hit different endpoints."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True}

            handler.handle_tool_call("start_pipeline", {"task_id": "issue-99"})
            pipeline_endpoint = mock_req.call_args[0][0]

            handler.handle_tool_call("start_phase", {"task_id": "issue-99"})
            phase_endpoint = mock_req.call_args[0][0]

        assert pipeline_endpoint != phase_endpoint
        assert pipeline_endpoint.endswith("/start")
        assert phase_endpoint.endswith("/phase/start")


class TestValidateConfig:
    def test_valid_config(self, handler):
        result = handler.handle_tool_call(
            "validate_config",
            {"config": {"start_phase": "implement", "hitl_gates": False}},
        )
        assert result["valid"] is True
        assert result["config"]["start_phase"] == "implement"
        assert result["config"]["hitl_gates"] is False

    def test_valid_config_defaults(self, handler):
        result = handler.handle_tool_call("validate_config", {"config": {}})
        assert result["valid"] is True
        assert "config" in result

    def test_invalid_start_phase(self, handler):
        result = handler.handle_tool_call(
            "validate_config", {"config": {"start_phase": "nonexistent"}}
        )
        assert result["valid"] is False
        assert any("start_phase" in str(e) for e in result["errors"])

    def test_config_as_json_string(self, handler):
        result = handler.handle_tool_call(
            "validate_config",
            {"config": '{"start_phase": "implement", "hitl_gates": false}'},
        )
        assert result["valid"] is True
        assert result["config"]["start_phase"] == "implement"

    def test_config_as_invalid_json_string(self, handler):
        result = handler.handle_tool_call("validate_config", {"config": "not valid json"})
        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert result["errors"][0]["field"] == "config"
        assert "Invalid JSON" in result["errors"][0]["message"]


class TestSubmitTaskConfigDeserialization:
    """Test that submit_task correctly deserializes config strings."""

    @patch("urllib.request.build_opener")
    def test_config_string_deserialized_to_dict(self, mock_build_opener, handler):
        """Config passed as a JSON string should be deserialized before forwarding."""
        import json

        # Mock the HTTP responses for both pipeline creation and start
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"data": {"pipeline": {"id": "test-pipeline"}}}
        ).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_response
        mock_build_opener.return_value = mock_opener

        config_str = '{"start_phase": "implement", "hitl_gates": false}'
        handler.handle_tool_call(
            "submit_task",
            {
                "description": "Test task",
                "repo": "owner/repo",
                "config": config_str,
            },
        )

        # Verify the request was made with deserialized config
        call_args = mock_opener.open.call_args_list[0]
        request_obj = call_args[0][0]
        body = json.loads(request_obj.data)
        assert isinstance(body["config"], dict)
        assert body["config"]["start_phase"] == "implement"
        assert body["config"]["hitl_gates"] is False

    def test_config_malformed_json_string(self, handler):
        """Malformed JSON config string should return a structured error."""
        result = handler.handle_tool_call(
            "submit_task",
            {
                "description": "Test task",
                "repo": "owner/repo",
                "config": "not valid json",
            },
        )
        assert "error" in result
        assert "Invalid config JSON" in result["error"]


class TestSubmitTaskErrorPropagation:
    """Test that submit_task propagates API error details (#1396)."""

    @patch("urllib.request.build_opener")
    def test_500_includes_api_error_message(self, mock_build_opener, handler):
        """HTTP 500 from the API should return the response body message."""
        error_body = json.dumps(
            {
                "success": False,
                "message": "Failed to create pipeline: Git command failed: index.lock",
            }
        ).encode()

        mock_opener = MagicMock()
        http_error = HTTPError(
            url="http://localhost:9849/api/v1/pipelines",
            code=500,
            msg="INTERNAL SERVER ERROR",
            hdrs={},
            fp=MagicMock(),
        )
        http_error.read = MagicMock(return_value=error_body)
        mock_opener.open.side_effect = http_error
        mock_build_opener.return_value = mock_opener

        result = handler.handle_tool_call(
            "submit_task",
            {"description": "Test task", "repo": "owner/repo", "jira_ticket": "KORE-1234"},
        )

        assert "error" in result
        assert "Git command failed" in result["error"]

    @patch("urllib.request.build_opener")
    def test_500_with_unreadable_body_falls_back(self, mock_build_opener, handler):
        """HTTP 500 with unreadable body should still return a structured error."""
        mock_opener = MagicMock()
        http_error = HTTPError(
            url="http://localhost:9849/api/v1/pipelines",
            code=500,
            msg="INTERNAL SERVER ERROR",
            hdrs={},
            fp=MagicMock(),
        )
        http_error.read = MagicMock(side_effect=Exception("read failed"))
        mock_opener.open.side_effect = http_error
        mock_build_opener.return_value = mock_opener

        result = handler.handle_tool_call(
            "submit_task",
            {"description": "Test task", "repo": "owner/repo", "base_branch": "develop"},
        )

        assert "error" in result
        assert "HTTP 500" in result["error"]


class TestGetStatusSyncHandler:
    """The sync ``_handle_get_status`` must not perform any blocking sleep.

    The ``wait`` parameter is handled in the async tool wrapper
    (``mcp_server._apply_get_status_wait``) so the anyio worker thread pool
    is not held during polling delays.  These tests pin that invariant.
    """

    def _pipeline_response(self):
        return {
            "data": {
                "pipeline": {
                    "id": "issue-42",
                    "current_phase": "implement",
                    "status": "running",
                    "repo": "org/repo",
                    "issue_number": 42,
                    "created_at": "2026-01-01T00:00:00Z",
                    "phases": {
                        "implement": {
                            "agents": [
                                {"role": "coder", "status": "running"},
                                {"role": "tester", "status": "complete"},
                            ]
                        }
                    },
                    "decisions": [],
                }
            }
        }

    def _pipeline_response_with_pr(self, pr_url: str):
        """Pipeline fixture with a PR phase artifact containing ``pr_url``."""
        resp = self._pipeline_response()
        resp["data"]["pipeline"]["current_phase"] = "pr"
        resp["data"]["pipeline"]["status"] = "complete"
        resp["data"]["pipeline"]["phases"]["pr"] = {
            "agents": [],
            "artifacts": {"pr_url": pr_url},
        }
        return resp

    def _messages_response(self):
        return {"data": {"messages": []}}

    def _mock_requests(self, handler):
        return patch.object(
            handler,
            "_make_request",
            side_effect=[self._pipeline_response(), self._messages_response()],
        )

    @patch("time.sleep")
    def test_handler_ignores_wait_no_sleep(self, mock_sleep, handler):
        """The sync handler never calls time.sleep, even when wait is set."""
        for wait_val in (0, 5, 60, 600, -10, "30", None):
            with self._mock_requests(handler):
                handler.handle_tool_call("get_status", {"task_id": "issue-42", "wait": wait_val})
        mock_sleep.assert_not_called()

    def test_pr_info_null_when_no_pr_phase(self, handler):
        """pr_url / pr_number are None while the pipeline is still pre-PR (#1625)."""
        with patch.object(
            handler,
            "_make_request",
            side_effect=[self._pipeline_response(), self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "pr_url" not in result["pipeline"]
        assert "pr_number" not in result["pipeline"]

    def test_pr_info_populated_from_pr_phase_artifact(self, handler):
        """pr_url / pr_number are extracted from phases.pr.artifacts.pr_url (#1625)."""
        pr_response = self._pipeline_response_with_pr("https://github.com/owner/repo/pull/1624")
        with patch.object(
            handler,
            "_make_request",
            side_effect=[pr_response, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert result["pipeline"]["pr_url"] == "https://github.com/owner/repo/pull/1624"
        assert result["pipeline"]["pr_number"] == 1624

    def test_pr_number_null_for_malformed_url(self, handler):
        """A URL without /pull/N still surfaces pr_url; pr_number stays None (#1625)."""
        pr_response = self._pipeline_response_with_pr("not-a-valid-pr-url")
        with patch.object(
            handler,
            "_make_request",
            side_effect=[pr_response, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert result["pipeline"]["pr_url"] == "not-a-valid-pr-url"
        assert "pr_number" not in result["pipeline"]

    # -- Server-computed timing tests (#1702) ----------------------------------

    def _pipeline_response_with_timing(
        self,
        phase_started_at=None,
        agent_started_at=None,
        completed_agent_started_at=None,
    ):
        """Pipeline fixture with optional ``started_at`` on phase and agents."""
        resp = self._pipeline_response()
        if phase_started_at is not None:
            resp["data"]["pipeline"]["phases"]["implement"]["started_at"] = phase_started_at
        agent_list = resp["data"]["pipeline"]["phases"]["implement"]["agents"]
        if agent_started_at is not None:
            agent_list[0]["started_at"] = agent_started_at  # coder (running)
        if completed_agent_started_at is not None:
            agent_list[1]["started_at"] = completed_agent_started_at  # tester (complete)
        return resp

    def test_phase_timing_present_when_started_at_set(self, handler):
        """phase_started_at and phase_elapsed_seconds appear when the phase has started_at (#1702)."""
        ts = "2026-01-01T00:00:00+00:00"
        resp = self._pipeline_response_with_timing(phase_started_at=ts)
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "phase_started_at" in result
        assert "phase_elapsed_seconds" in result
        assert isinstance(result["phase_elapsed_seconds"], int)
        assert result["phase_elapsed_seconds"] >= 0

    def test_phase_timing_omitted_when_started_at_absent(self, handler):
        """phase_started_at and phase_elapsed_seconds omitted when started_at is missing (#1702)."""
        resp = self._pipeline_response_with_timing()  # no started_at
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "phase_started_at" not in result
        assert "phase_elapsed_seconds" not in result

    def test_phase_timing_omitted_when_started_at_none(self, handler):
        """phase_started_at and phase_elapsed_seconds omitted when started_at is explicitly None (#1702)."""
        resp = self._pipeline_response_with_timing()
        resp["data"]["pipeline"]["phases"]["implement"]["started_at"] = None
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "phase_started_at" not in result
        assert "phase_elapsed_seconds" not in result

    def test_phase_timing_omitted_for_invalid_started_at(self, handler):
        """Invalid started_at format is silently ignored (#1702)."""
        resp = self._pipeline_response_with_timing(phase_started_at="not-a-timestamp")
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "phase_started_at" not in result
        assert "phase_elapsed_seconds" not in result

    def test_phase_elapsed_seconds_is_integer(self, handler):
        """phase_elapsed_seconds is an integer, not a float (#1702)."""
        from datetime import UTC, datetime, timedelta

        # Use a timestamp 100 seconds in the past
        ts = (datetime.now(UTC) - timedelta(seconds=100)).isoformat()
        resp = self._pipeline_response_with_timing(phase_started_at=ts)
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert isinstance(result["phase_elapsed_seconds"], int)
        # Allow some tolerance for test execution time
        assert 95 <= result["phase_elapsed_seconds"] <= 110

    def test_phase_timing_handles_naive_datetime(self, handler):
        """Naive datetime (no timezone) is treated as UTC (#1702)."""
        # Naive ISO format without timezone suffix
        ts = "2026-01-01T00:00:00"
        resp = self._pipeline_response_with_timing(phase_started_at=ts)
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "phase_started_at" in result
        assert "phase_elapsed_seconds" in result
        assert result["phase_elapsed_seconds"] >= 0

    def test_agent_elapsed_seconds_present_for_running_agent(self, handler):
        """Running agents get elapsed_seconds when started_at is present (#1702)."""
        from datetime import UTC, datetime, timedelta

        agent_ts = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        resp = self._pipeline_response_with_timing(agent_started_at=agent_ts)
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        running = result["running_agents"]
        assert len(running) == 1
        assert "elapsed_seconds" in running[0]
        assert isinstance(running[0]["elapsed_seconds"], int)
        assert 195 <= running[0]["elapsed_seconds"] <= 210

    def test_agent_elapsed_seconds_omitted_without_started_at(self, handler):
        """Running agents without started_at do not get elapsed_seconds (#1702)."""
        resp = self._pipeline_response_with_timing()  # no agent started_at
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        running = result["running_agents"]
        assert len(running) == 1
        assert "elapsed_seconds" not in running[0]

    def test_completed_agents_not_enriched_with_elapsed(self, handler):
        """Completed agents do not receive elapsed_seconds even with started_at (#1702)."""
        from datetime import UTC, datetime, timedelta

        agent_ts = (datetime.now(UTC) - timedelta(seconds=300)).isoformat()
        resp = self._pipeline_response_with_timing(completed_agent_started_at=agent_ts)
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        completed = result["completed_agents"]
        assert len(completed) == 1
        assert "elapsed_seconds" not in completed[0]

    def test_agent_elapsed_omitted_for_invalid_started_at(self, handler):
        """Invalid started_at on an agent is silently skipped (#1702)."""
        resp = self._pipeline_response_with_timing(agent_started_at="garbage-timestamp")
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        running = result["running_agents"]
        assert len(running) == 1
        assert "elapsed_seconds" not in running[0]

    def test_both_phase_and_agent_timing_together(self, handler):
        """Phase timing and agent timing coexist correctly (#1702)."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        phase_ts = (now - timedelta(seconds=500)).isoformat()
        agent_ts = (now - timedelta(seconds=300)).isoformat()
        resp = self._pipeline_response_with_timing(
            phase_started_at=phase_ts, agent_started_at=agent_ts
        )
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        # Phase timing
        assert "phase_started_at" in result
        assert 495 <= result["phase_elapsed_seconds"] <= 510

        # Agent timing
        running = result["running_agents"]
        assert len(running) == 1
        assert 295 <= running[0]["elapsed_seconds"] <= 310

    def test_agent_elapsed_handles_naive_datetime(self, handler):
        """Naive agent started_at is treated as UTC (#1702)."""
        from datetime import UTC, datetime, timedelta

        # Naive ISO format without timezone
        agent_ts = (datetime.now(UTC) - timedelta(seconds=150)).strftime("%Y-%m-%dT%H:%M:%S")
        resp = self._pipeline_response_with_timing(agent_started_at=agent_ts)
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        running = result["running_agents"]
        assert len(running) == 1
        assert "elapsed_seconds" in running[0]
        assert 145 <= running[0]["elapsed_seconds"] <= 160

    def test_phase_started_at_preserved_as_iso_string(self, handler):
        """phase_started_at in the response is an ISO 8601 string (#1702)."""
        ts = "2026-04-13T18:22:40.769068+00:00"
        resp = self._pipeline_response_with_timing(phase_started_at=ts)
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        from datetime import datetime

        # Should be a valid ISO 8601 string
        parsed = datetime.fromisoformat(result["phase_started_at"])
        assert parsed.tzinfo is not None  # timezone-aware


class TestGetStatusWedgedNoSuccessor:
    """Tests for the ``wedged_no_successor`` watchdog field (#2166).

    The watchdog flags a pipeline that is nominally RUNNING but stalled
    between phases — current phase reports COMPLETE, no HITL gate is
    pending, yet no successor has been scheduled within 60s of completion.
    """

    def _wedged_pipeline_response(
        self,
        *,
        pipeline_status: str = "running",
        phase_status: str = "complete",
        completed_at: str | None = None,
        decisions: list | None = None,
    ):
        """Pipeline fixture for wedge scenarios.

        Defaults produce a wedge candidate (running + complete phase + no
        decisions). Pass overrides to construct negative cases.
        """
        from datetime import UTC, datetime, timedelta

        if completed_at is None:
            # Default: 90s ago — past the 60s threshold.
            completed_at = (datetime.now(UTC) - timedelta(seconds=90)).isoformat()
        return {
            "data": {
                "pipeline": {
                    "id": "issue-42",
                    "current_phase": "plan",
                    "status": pipeline_status,
                    "repo": "org/repo",
                    "issue_number": 42,
                    "created_at": "2026-01-01T00:00:00Z",
                    "phases": {
                        "plan": {
                            "status": phase_status,
                            "completed_at": completed_at,
                            "agents": [],
                        }
                    },
                    "decisions": decisions if decisions is not None else [],
                }
            }
        }

    def _messages_response(self):
        return {"data": {"messages": []}}

    def test_wedge_surfaced_when_complete_and_stale(self, handler):
        """Phase COMPLETE + RUNNING + no decisions + completed >60s ago → wedge surfaces."""
        from datetime import UTC, datetime, timedelta

        completed_at = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        resp = self._wedged_pipeline_response(completed_at=completed_at)
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "wedged_no_successor" in result
        wedge = result["wedged_no_successor"]
        assert wedge["phase"] == "plan"
        assert isinstance(wedge["since_seconds"], int)
        assert 115 <= wedge["since_seconds"] <= 130
        # completed_at echoed back as ISO string
        from datetime import datetime as _dt

        parsed = _dt.fromisoformat(wedge["completed_at"])
        assert parsed.tzinfo is not None

    def test_no_wedge_when_phase_still_running(self, handler):
        """Phase RUNNING (mid-execution) is not a wedge."""
        resp = self._wedged_pipeline_response(phase_status="running", completed_at=None)
        # Drop completed_at — a running phase wouldn't have one set yet.
        resp["data"]["pipeline"]["phases"]["plan"]["completed_at"] = None
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "wedged_no_successor" not in result

    def test_no_wedge_when_pending_decision_present(self, handler):
        """Pending HITL gate is the expected pre-advance state, not a wedge.

        Pin ``pipeline_status="running"`` (default) so the pending decision
        is the *only* clause blocking the wedge — otherwise the watchdog
        short-circuits on the status guard and never evaluates
        ``pending_decisions``.
        """
        resp = self._wedged_pipeline_response(
            decisions=[{"id": "d1", "status": "pending", "type": "phase_gate"}],
        )
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "wedged_no_successor" not in result

    def test_no_wedge_when_within_threshold(self, handler):
        """Phase completed <60s ago is normal mid-spawn window, not a wedge."""
        from datetime import UTC, datetime, timedelta

        completed_at = (datetime.now(UTC) - timedelta(seconds=15)).isoformat()
        resp = self._wedged_pipeline_response(completed_at=completed_at)
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "wedged_no_successor" not in result

    def test_no_wedge_for_terminal_pipeline(self, handler):
        """Pipeline status COMPLETE (terminal) is not a wedge even if stale."""
        from datetime import UTC, datetime, timedelta

        completed_at = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        resp = self._wedged_pipeline_response(pipeline_status="complete", completed_at=completed_at)
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "wedged_no_successor" not in result

    def test_no_wedge_when_completed_at_missing(self, handler):
        """Missing completed_at cannot prove staleness — no warning."""
        resp = self._wedged_pipeline_response()
        resp["data"]["pipeline"]["phases"]["plan"]["completed_at"] = None
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "wedged_no_successor" not in result

    def test_no_wedge_for_invalid_completed_at(self, handler):
        """Garbage completed_at is silently skipped (parser fails closed)."""
        resp = self._wedged_pipeline_response(completed_at="not-a-timestamp")
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "wedged_no_successor" not in result

    def test_wedge_handles_naive_completed_at(self, handler):
        """Naive completed_at (no tzinfo) is treated as UTC."""
        from datetime import UTC, datetime, timedelta

        # Naive ISO timestamp 100s in the past
        completed_at = (datetime.now(UTC) - timedelta(seconds=100)).strftime("%Y-%m-%dT%H:%M:%S")
        resp = self._wedged_pipeline_response(completed_at=completed_at)
        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "wedged_no_successor" in result
        assert 95 <= result["wedged_no_successor"]["since_seconds"] <= 110

    def test_wedge_with_real_pipeline_model_dump(self, handler):
        """Watchdog fires for a fixture grounded in ``Pipeline.model_dump(mode="json")``.

        The other tests in this class hand-roll a minimal dict shape. This
        one constructs a real ``Pipeline`` (matching what
        ``routes/pipelines.py`` returns) so a future field rename in
        ``PhaseExecution`` (e.g. renaming ``completed_at``) would break
        this test instead of silently passing.
        """
        from datetime import UTC, datetime, timedelta

        from egg_contracts.models import PipelinePhase

        from orchestrator.models import (
            PhaseExecution,
            Pipeline,
            PipelineStatus,
        )

        completed_at = datetime.now(UTC) - timedelta(seconds=120)
        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="org/repo",
            current_phase=PipelinePhase.PLAN,
            status=PipelineStatus.RUNNING,
            phases={
                "plan": PhaseExecution(
                    phase=PipelinePhase.PLAN,
                    status=PipelineStatus.COMPLETE,
                    completed_at=completed_at,
                ),
            },
        )
        resp = {"data": {"pipeline": pipeline.model_dump(mode="json")}}

        with patch.object(
            handler,
            "_make_request",
            side_effect=[resp, self._messages_response()],
        ):
            result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})

        assert "wedged_no_successor" in result
        wedge = result["wedged_no_successor"]
        assert wedge["phase"] == "plan"
        assert 115 <= wedge["since_seconds"] <= 130


class TestGetStatusWait:
    """Tests for the async ``wait`` handling in ``mcp_server``.

    The wait delay moved from ``time.sleep`` in the sync tool handler to
    ``asyncio.sleep`` in the async wrapper so polling loops no longer hold
    anyio worker threads for the full delay.
    """

    @patch("mcp_server._async_sleep", new_callable=AsyncMock)
    def test_wait_sleeps_on_event_loop(self, mock_sleep):
        """wait > 0 awaits asyncio.sleep for the requested value."""
        from mcp_server import _apply_get_status_wait

        kwargs = {"task_id": "issue-42", "wait": 10}
        asyncio.run(_apply_get_status_wait("get_status", kwargs))

        mock_sleep.assert_awaited_once_with(10)
        assert "wait" not in kwargs  # consumed before dispatch to sync handler

    @patch("mcp_server._async_sleep", new_callable=AsyncMock)
    def test_wait_capped_at_max(self, mock_sleep):
        """wait values above GET_STATUS_MAX_WAIT are capped; cap is 25s."""
        from mcp_server import GET_STATUS_MAX_WAIT, _apply_get_status_wait

        assert GET_STATUS_MAX_WAIT == 25

        asyncio.run(_apply_get_status_wait("get_status", {"task_id": "issue-42", "wait": 300}))
        mock_sleep.assert_awaited_once_with(GET_STATUS_MAX_WAIT)

    @patch("mcp_server._async_sleep", new_callable=AsyncMock)
    def test_wait_zero_no_sleep(self, mock_sleep):
        """wait=0 (default) does not await asyncio.sleep."""
        from mcp_server import _apply_get_status_wait

        asyncio.run(_apply_get_status_wait("get_status", {"task_id": "issue-42", "wait": 0}))
        mock_sleep.assert_not_called()

    @patch("mcp_server._async_sleep", new_callable=AsyncMock)
    def test_wait_missing_no_sleep(self, mock_sleep):
        """Omitting wait (absent key) does not await asyncio.sleep."""
        from mcp_server import _apply_get_status_wait

        asyncio.run(_apply_get_status_wait("get_status", {"task_id": "issue-42"}))
        mock_sleep.assert_not_called()

    @patch("mcp_server._async_sleep", new_callable=AsyncMock)
    def test_wait_negative_no_sleep(self, mock_sleep):
        """Negative wait values are ignored."""
        from mcp_server import _apply_get_status_wait

        asyncio.run(_apply_get_status_wait("get_status", {"task_id": "issue-42", "wait": -5}))
        mock_sleep.assert_not_called()

    @patch("mcp_server._async_sleep", new_callable=AsyncMock)
    def test_wait_non_numeric_no_sleep(self, mock_sleep):
        """Non-numeric wait values (str, None, bool, list) are ignored."""
        from mcp_server import _apply_get_status_wait

        for bad in ("30", None, True, False, [10]):
            asyncio.run(_apply_get_status_wait("get_status", {"task_id": "issue-42", "wait": bad}))
        mock_sleep.assert_not_called()

    @patch("mcp_server._async_sleep", new_callable=AsyncMock)
    def test_other_tools_ignore_wait(self, mock_sleep):
        """Wait handling only applies to get_status; other tools pass through."""
        from mcp_server import _apply_get_status_wait

        kwargs = {"task_id": "issue-42", "wait": 10}
        asyncio.run(_apply_get_status_wait("restart_phase", kwargs))

        mock_sleep.assert_not_called()
        assert kwargs["wait"] == 10  # preserved — non-get_status tools own it


class TestBuildStatusSnapshotRefactor:
    """Pin that ``_build_status_snapshot`` extraction (TASK-2-2)
    preserves byte-identical behaviour for ``_handle_get_status``.
    """

    def _pipeline_response(self):
        return {
            "data": {
                "pipeline": {
                    "id": "issue-42",
                    "current_phase": "implement",
                    "status": "running",
                    "repo": "org/repo",
                    "issue_number": 42,
                    "created_at": "2026-01-01T00:00:00Z",
                    "phases": {
                        "implement": {
                            "agents": [
                                {"role": "coder", "status": "running"},
                                {"role": "tester", "status": "complete"},
                            ]
                        }
                    },
                    "decisions": [],
                }
            }
        }

    def test_handle_get_status_delegates_to_snapshot(self, handler):
        with patch.object(
            handler,
            "_make_request",
            side_effect=[self._pipeline_response(), {"data": {"messages": []}}],
        ):
            snapshot_direct = handler._build_status_snapshot("issue-42")
        with patch.object(
            handler,
            "_make_request",
            side_effect=[self._pipeline_response(), {"data": {"messages": []}}],
        ):
            status_via_handler = handler.handle_tool_call("get_status", {"task_id": "issue-42"})
        assert snapshot_direct == status_via_handler


class TestAdvancePhase:
    """Tests for the advance_phase MCP tool handler."""

    def test_advance_non_force(self, handler):
        """Non-force advance sends target_phase without stopping containers."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "message": "Phase advanced to implement",
                "data": {"previous_phase": "plan", "current_phase": "implement"},
            }
            result = handler.handle_tool_call(
                "advance_phase",
                {"task_id": "issue-42", "target_phase": "implement"},
            )

        mock_req.assert_called_once_with(
            "/api/v1/pipelines/issue-42/phase",
            method="POST",
            data={"target_phase": "implement", "force": False},
        )
        assert result["success"] is True
        assert result["data"]["current_phase"] == "implement"
        assert "stopped_containers" not in result

    def test_advance_force_stops_containers(self, handler):
        """Force advance lists containers, stops running ones, then advances."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                # GET containers (only running containers, all=false)
                {
                    "data": {
                        "containers": [
                            {"container_id": "c1", "status": "running"},
                            {"container_id": "c2", "status": "running"},
                            {"container_id": "c3", "status": "exited"},
                        ]
                    }
                },
                # POST stop c1
                {"success": True},
                # POST stop c2
                {"success": True},
                # POST phase advance
                {
                    "success": True,
                    "message": "Phase advanced to implement",
                    "data": {"previous_phase": "plan", "current_phase": "implement"},
                },
            ]
            result = handler.handle_tool_call(
                "advance_phase",
                {"task_id": "issue-42", "target_phase": "implement", "force": True},
            )

        assert mock_req.call_count == 4
        # First call: list containers
        assert mock_req.call_args_list[0][0][0] == "/api/v1/pipelines/issue-42/containers?all=false"
        # Second call: stop c1
        assert mock_req.call_args_list[1][0][0] == "/api/v1/pipelines/issue-42/containers/c1/stop"
        assert mock_req.call_args_list[1][1]["method"] == "POST"
        # Third call: stop c2
        assert mock_req.call_args_list[2][0][0] == "/api/v1/pipelines/issue-42/containers/c2/stop"
        # Fourth call: advance phase
        assert mock_req.call_args_list[3][0][0] == "/api/v1/pipelines/issue-42/phase"
        assert mock_req.call_args_list[3][1]["data"]["force"] is True

        assert result["success"] is True
        assert result["stopped_containers"] == ["c1", "c2"]

    def test_advance_force_no_running_containers(self, handler):
        """Force advance with no running containers still advances successfully."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                # GET containers (none running)
                {"data": {"containers": []}},
                # POST phase advance
                {
                    "success": True,
                    "data": {"previous_phase": "plan", "current_phase": "implement"},
                },
            ]
            result = handler.handle_tool_call(
                "advance_phase",
                {"task_id": "issue-42", "target_phase": "implement", "force": True},
            )

        assert mock_req.call_count == 2
        assert result["success"] is True
        assert "stopped_containers" not in result

    def test_advance_force_container_stop_fails(self, handler):
        """If container stop fails, log warning and proceed with advance."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                # GET containers
                {"data": {"containers": [{"container_id": "c1", "status": "running"}]}},
                # POST stop c1 fails
                Exception("container stop timeout"),
                # POST phase advance still succeeds
                {
                    "success": True,
                    "data": {"previous_phase": "plan", "current_phase": "implement"},
                },
            ]
            result = handler.handle_tool_call(
                "advance_phase",
                {"task_id": "issue-42", "target_phase": "implement", "force": True},
            )

        assert result["success"] is True
        # c1 was not successfully stopped, so not in stopped_containers
        assert "stopped_containers" not in result
        assert result["failed_containers"] == ["c1"]

    def test_advance_force_container_list_fails(self, handler):
        """If listing containers fails, log warning and proceed with advance."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                # GET containers fails
                Exception("connection refused"),
                # POST phase advance still succeeds
                {
                    "success": True,
                    "data": {"previous_phase": "refine", "current_phase": "plan"},
                },
            ]
            result = handler.handle_tool_call(
                "advance_phase",
                {"task_id": "issue-42", "target_phase": "plan", "force": True},
            )

        assert result["success"] is True
        assert "stopped_containers" not in result

    def test_advance_url_encodes_task_id(self, handler):
        """Task ID with special characters is URL-encoded."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"previous_phase": "plan", "current_phase": "implement"},
            }
            handler.handle_tool_call(
                "advance_phase",
                {"task_id": "owner/repo#42", "target_phase": "implement"},
            )

        call_url = mock_req.call_args[0][0]
        assert "owner%2Frepo%2342" in call_url

    def test_advance_force_skips_exited_containers(self, handler):
        """Only running containers are stopped; exited containers are skipped."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                # GET containers: only one running, one exited
                {
                    "data": {
                        "containers": [
                            {"container_id": "exited1", "status": "exited"},
                            {"container_id": "running1", "status": "running"},
                        ]
                    }
                },
                # POST stop running1
                {"success": True},
                # POST phase advance
                {
                    "success": True,
                    "data": {"previous_phase": "plan", "current_phase": "implement"},
                },
            ]
            result = handler.handle_tool_call(
                "advance_phase",
                {"task_id": "issue-42", "target_phase": "implement", "force": True},
            )

        assert mock_req.call_count == 3
        assert result["stopped_containers"] == ["running1"]

    def test_advance_force_phase_post_fails_preserves_container_info(self, handler):
        """If phase advance fails after containers stopped, response includes container info."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = [
                # GET containers
                {
                    "data": {
                        "containers": [
                            {"container_id": "c1", "status": "running"},
                            {"container_id": "c2", "status": "running"},
                        ]
                    }
                },
                # POST stop c1 succeeds
                {"success": True},
                # POST stop c2 fails
                Exception("container stop timeout"),
                # POST phase advance fails
                Exception("phase advance endpoint unavailable"),
            ]
            result = handler.handle_tool_call(
                "advance_phase",
                {"task_id": "issue-42", "target_phase": "implement", "force": True},
            )

        assert "error" in result
        assert "Phase advance failed" in result["error"]
        assert result["stopped_containers"] == ["c1"]
        assert result["failed_containers"] == ["c2"]

    def test_advance_invalid_target_phase_does_not_touch_containers(self, handler):
        """Invalid target_phase returns an error before any container stops.

        Regression for #1755: previously, force=true with an invalid
        target_phase (e.g. 'complete') stopped containers first and
        surfaced failed_containers in the error response, implying the
        teardown had already happened when validation failed.
        """
        with patch.object(handler, "_make_request") as mock_req:
            result = handler.handle_tool_call(
                "advance_phase",
                {"task_id": "issue-42", "target_phase": "complete", "force": True},
            )

        mock_req.assert_not_called()
        assert "error" in result
        assert "Invalid target_phase" in result["error"]
        assert "stopped_containers" not in result
        assert "failed_containers" not in result


class TestStartPhase:
    """Tests for the start_phase MCP tool handler."""

    def test_start_success(self, handler):
        """start_phase proxies to POST /phase/start endpoint."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "message": "Phase 'implement' marked running (does not spawn agents)",
                "data": {"phase": "implement", "status": "running"},
            }
            result = handler.handle_tool_call("start_phase", {"task_id": "issue-42"})

        mock_req.assert_called_once_with(
            "/api/v1/pipelines/issue-42/phase/start",
            method="POST",
        )
        assert result["success"] is True
        assert result["data"]["status"] == "running"

    def test_start_url_encodes_task_id(self, handler):
        """Task ID with special characters is URL-encoded."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True}
            handler.handle_tool_call("start_phase", {"task_id": "org/repo#7"})

        call_url = mock_req.call_args[0][0]
        assert "org%2Frepo%237" in call_url

    def test_start_already_running(self, handler):
        """start_phase returns error when phase is already running."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": False,
                "message": "Phase implement is already running",
            }
            result = handler.handle_tool_call("start_phase", {"task_id": "issue-42"})

        assert result["success"] is False
        assert "already running" in result["message"]


class TestCompletePhase:
    """Tests for the complete_phase MCP tool handler."""

    def test_complete_without_artifacts(self, handler):
        """complete_phase without artifacts sends no data body."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "message": "Phase 'implement' marked complete; call advance_phase to transition",
                "data": {"phase": "implement", "current_phase": "implement", "next_phase": "pr"},
            }
            result = handler.handle_tool_call("complete_phase", {"task_id": "issue-42"})

        mock_req.assert_called_once_with(
            "/api/v1/pipelines/issue-42/phase/complete",
            method="POST",
            data=None,
        )
        assert result["success"] is True
        assert result["data"]["current_phase"] == "implement"
        assert result["data"]["next_phase"] == "pr"

    def test_complete_with_artifacts(self, handler):
        """complete_phase with artifacts passes them in the request body."""
        artifacts = {"commit_sha": "abc123", "pr_url": "https://github.com/org/repo/pull/1"}
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "message": "Phase 'implement' marked complete; call advance_phase to transition",
                "data": {"phase": "implement", "current_phase": "implement", "next_phase": "pr"},
            }
            result = handler.handle_tool_call(
                "complete_phase",
                {"task_id": "issue-42", "artifacts": artifacts},
            )

        mock_req.assert_called_once_with(
            "/api/v1/pipelines/issue-42/phase/complete",
            method="POST",
            data={"artifacts": artifacts},
        )
        assert result["success"] is True

    def test_complete_url_encodes_task_id(self, handler):
        """Task ID with special characters is URL-encoded."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True}
            handler.handle_tool_call("complete_phase", {"task_id": "org/repo#7"})

        call_url = mock_req.call_args[0][0]
        assert "org%2Frepo%237" in call_url


class TestPopulateContract:
    """Tests for the populate_contract MCP tool handler."""

    def test_populate_success(self, handler):
        """populate_contract proxies to POST /phase/populate-contract."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "message": "Contract populated from plan",
                "data": {"phase_count": 2, "task_count": 6},
            }
            result = handler.handle_tool_call("populate_contract", {"task_id": "issue-42"})

        mock_req.assert_called_once_with(
            "/api/v1/pipelines/issue-42/phase/populate-contract",
            method="POST",
        )
        assert result["success"] is True
        assert result["data"]["phase_count"] == 2
        assert result["data"]["task_count"] == 6

    def test_populate_url_encodes_task_id(self, handler):
        """Task ID with special characters is URL-encoded."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True}
            handler.handle_tool_call("populate_contract", {"task_id": "org/repo#7"})

        call_url = mock_req.call_args[0][0]
        assert "org%2Frepo%237" in call_url

    def test_populate_not_found(self, handler):
        """populate_contract returns error when pipeline not found."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": False,
                "message": "Pipeline issue-999 not found",
            }
            result = handler.handle_tool_call("populate_contract", {"task_id": "issue-999"})

        assert result["success"] is False
        assert "not found" in result["message"]


class TestPhaseManagementToolSchemas:
    """Verify tool schema definitions for the 4 phase management tools."""

    def test_advance_phase_schema(self):
        """advance_phase has task_id and target_phase required, force optional."""
        from mcp_tools import PIPELINE_TOOLS

        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        schema = tools_by_name["advance_phase"]["inputSchema"]
        assert set(schema["required"]) == {"task_id", "target_phase"}
        assert "force" in schema["properties"]
        assert schema["properties"]["force"]["type"] == "boolean"

    def test_start_phase_schema(self):
        """start_phase has only task_id required."""
        from mcp_tools import PIPELINE_TOOLS

        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        schema = tools_by_name["start_phase"]["inputSchema"]
        assert schema["required"] == ["task_id"]
        assert "task_id" in schema["properties"]

    def test_complete_phase_schema(self):
        """complete_phase has task_id required, artifacts optional."""
        from mcp_tools import PIPELINE_TOOLS

        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        schema = tools_by_name["complete_phase"]["inputSchema"]
        assert schema["required"] == ["task_id"]
        assert "artifacts" in schema["properties"]
        assert schema["properties"]["artifacts"]["type"] == "object"

    def test_populate_contract_schema(self):
        """populate_contract has only task_id required."""
        from mcp_tools import PIPELINE_TOOLS

        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        schema = tools_by_name["populate_contract"]["inputSchema"]
        assert schema["required"] == ["task_id"]
        assert "task_id" in schema["properties"]


def _make_http_error(code: int, body: dict) -> HTTPError:
    """Build an HTTPError with a readable JSON body for babysit-pr tests."""
    import io

    return HTTPError(
        url="http://orchestrator/api/v1/pipelines",
        code=code,
        msg="",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(json.dumps(body).encode()),
    )


class TestBabysitPr:
    """Tests for PipelineToolHandler._handle_babysit_pr and its tool schema."""

    def test_missing_pr_number(self, handler):
        result = handler.handle_tool_call("babysit_pr", {"repo": "owner/repo"})
        assert "error" in result
        assert "pr_number" in result["error"]

    def test_negative_pr_number(self, handler):
        result = handler.handle_tool_call("babysit_pr", {"pr_number": -1, "repo": "owner/repo"})
        assert "error" in result
        assert "positive integer" in result["error"]

    def test_zero_pr_number(self, handler):
        result = handler.handle_tool_call("babysit_pr", {"pr_number": 0, "repo": "owner/repo"})
        assert "error" in result
        assert "pr_number" in result["error"]

    def test_string_pr_number(self, handler):
        result = handler.handle_tool_call("babysit_pr", {"pr_number": "42", "repo": "owner/repo"})
        assert "error" in result
        assert "positive integer" in result["error"]

    def test_missing_repo(self, handler):
        result = handler.handle_tool_call("babysit_pr", {"pr_number": 42})
        assert "error" in result
        assert "repo" in result["error"]

    def test_empty_repo(self, handler):
        result = handler.handle_tool_call("babysit_pr", {"pr_number": 42, "repo": ""})
        assert "error" in result
        assert "repo" in result["error"]

    def test_happy_path_posts_correct_payload(self, handler):
        create_response = {"data": {"pipeline": {"id": "pr-42"}}}
        start_response = {"data": {"started": True}}
        with patch.object(
            handler,
            "_make_request",
            side_effect=[create_response, start_response],
        ) as mock_req:
            result = handler.handle_tool_call("babysit_pr", {"pr_number": 42, "repo": "owner/repo"})

        assert mock_req.call_count == 2
        # First call — create pipeline
        create_call = mock_req.call_args_list[0]
        assert create_call.args[0] == "/api/v1/pipelines"
        assert create_call.kwargs["method"] == "POST"
        payload = create_call.kwargs["data"]
        assert payload["mode"] == "babysit"
        assert payload["pr_number"] == 42
        assert payload["repo"] == "owner/repo"
        assert payload["pipeline_id"] == "pr-42"

        # Second call — start pipeline
        start_call = mock_req.call_args_list[1]
        assert start_call.args[0] == "/api/v1/pipelines/pr-42/start"
        assert start_call.kwargs["method"] == "POST"

        assert result == {
            "task_id": "pr-42",
            "status": "started",
            "message": "Babysit-pr cycle started for PR #42",
        }

    def test_forwards_optional_branch_and_base_branch(self, handler):
        create_response = {"data": {"pipeline": {"id": "pr-7"}}}
        start_response = {"data": {"started": True}}
        with patch.object(
            handler,
            "_make_request",
            side_effect=[create_response, start_response],
        ) as mock_req:
            handler.handle_tool_call(
                "babysit_pr",
                {
                    "pr_number": 7,
                    "repo": "owner/repo",
                    "branch": "feature-x",
                    "base_branch": "develop",
                },
            )

        payload = mock_req.call_args_list[0].kwargs["data"]
        assert payload["branch"] == "feature-x"
        assert payload["base_branch"] == "develop"

    def test_config_dict_forwarded_as_is(self, handler):
        create_response = {"data": {"pipeline": {"id": "pr-1"}}}
        start_response = {"data": {"started": True}}
        with patch.object(
            handler,
            "_make_request",
            side_effect=[create_response, start_response],
        ) as mock_req:
            handler.handle_tool_call(
                "babysit_pr",
                {
                    "pr_number": 1,
                    "repo": "owner/repo",
                    "config": {"hitl_gates": False},
                },
            )

        payload = mock_req.call_args_list[0].kwargs["data"]
        assert payload["config"] == {"hitl_gates": False}

    def test_config_json_string_parsed(self, handler):
        create_response = {"data": {"pipeline": {"id": "pr-1"}}}
        start_response = {"data": {"started": True}}
        with patch.object(
            handler,
            "_make_request",
            side_effect=[create_response, start_response],
        ) as mock_req:
            handler.handle_tool_call(
                "babysit_pr",
                {
                    "pr_number": 1,
                    "repo": "owner/repo",
                    "config": '{"hitl_gates": false}',
                },
            )

        payload = mock_req.call_args_list[0].kwargs["data"]
        assert payload["config"] == {"hitl_gates": False}

    def test_invalid_config_json_returns_error(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            result = handler.handle_tool_call(
                "babysit_pr",
                {
                    "pr_number": 1,
                    "repo": "owner/repo",
                    "config": "{not valid json",
                },
            )

        # Should short-circuit before making any HTTP request.
        mock_req.assert_not_called()
        assert "error" in result
        assert "Invalid config JSON" in result["error"]

    def test_409_duplicate_pipeline_includes_existing_fields(self, handler):
        http_error = _make_http_error(
            409,
            {
                "message": "Pipeline already exists",
                "details": {
                    "reason": "duplicate_pipeline",
                    "existing_pipeline_id": "pr-42",
                    "existing_status": "running",
                    "existing_phase": "implement",
                },
            },
        )
        with patch.object(handler, "_make_request", side_effect=http_error):
            result = handler.handle_tool_call("babysit_pr", {"pr_number": 42, "repo": "owner/repo"})

        assert result["error"] == "Pipeline already exists"
        assert result["reason"] == "duplicate_pipeline"
        assert result["existing_pipeline_id"] == "pr-42"
        assert result["existing_status"] == "running"
        assert result["existing_phase"] == "implement"

    def test_400_fork_pr_includes_reason(self, handler):
        http_error = _make_http_error(
            400,
            {
                "message": "PR is from a fork",
                "details": {"reason": "pr_from_fork"},
            },
        )
        with patch.object(handler, "_make_request", side_effect=http_error):
            result = handler.handle_tool_call("babysit_pr", {"pr_number": 42, "repo": "owner/repo"})

        assert result["error"] == "PR is from a fork"
        assert result["reason"] == "pr_from_fork"

    def test_409_merged_pr_includes_reason(self, handler):
        http_error = _make_http_error(
            409,
            {
                "message": "PR is already merged",
                "details": {"reason": "pr_merged"},
            },
        )
        with patch.object(handler, "_make_request", side_effect=http_error):
            result = handler.handle_tool_call("babysit_pr", {"pr_number": 42, "repo": "owner/repo"})

        assert result["error"] == "PR is already merged"
        assert result["reason"] == "pr_merged"

    def test_409_empty_diff_includes_reason(self, handler):
        http_error = _make_http_error(
            409,
            {
                "message": "PR has an empty diff",
                "details": {"reason": "pr_empty_diff"},
            },
        )
        with patch.object(handler, "_make_request", side_effect=http_error):
            result = handler.handle_tool_call("babysit_pr", {"pr_number": 42, "repo": "owner/repo"})

        assert result["error"] == "PR has an empty diff"
        assert result["reason"] == "pr_empty_diff"

    def test_start_failure_returns_created_not_started(self, handler):
        create_response = {"data": {"pipeline": {"id": "pr-99"}}}
        with patch.object(
            handler,
            "_make_request",
            side_effect=[create_response, Exception("start failed")],
        ):
            result = handler.handle_tool_call("babysit_pr", {"pr_number": 99, "repo": "owner/repo"})

        assert result["status"] == "created_not_started"
        assert result["task_id"] == "pr-99"
        assert "failed to start" in result["message"]

    def test_tool_registered_in_pipeline_tools(self):
        from mcp_tools import PIPELINE_TOOLS

        assert "babysit_pr" in [t["name"] for t in PIPELINE_TOOLS]

    def test_tool_schema_required_fields(self):
        from mcp_tools import PIPELINE_TOOLS

        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        schema = tools_by_name["babysit_pr"]["inputSchema"]
        assert schema["required"] == ["pr_number", "repo"]
        assert schema["properties"]["pr_number"]["type"] == "integer"
        assert schema["properties"]["repo"]["type"] == "string"


class TestMakeRequestBody:
    """Regression tests for #1787: empty-body POST breaks Flask get_json().

    The MCP lifecycle tools (complete_phase, populate_contract, start_phase)
    previously sent no body at all when their optional fields were omitted.
    Flask's default get_json() raises BadRequest(400) for an empty body with
    Content-Type: application/json, so these tools returned an opaque 400.
    """

    def _capture_opener(self):
        """Build a mock urllib opener that records the Request and returns {}.

        Returns (mock_opener, captured) where captured["req"] is the Request
        object passed to opener.open().
        """
        import json as _json

        captured = {}

        mock_response = MagicMock()
        mock_response.read.return_value = _json.dumps({}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        def _open(req, timeout=None):
            captured["req"] = req
            return mock_response

        mock_opener = MagicMock()
        mock_opener.open.side_effect = _open
        return mock_opener, captured

    def test_post_with_no_data_sends_empty_json_object(self, handler):
        """POST with data=None must send b'{}', not no body."""
        mock_opener, captured = self._capture_opener()
        with patch("urllib.request.build_opener", return_value=mock_opener):
            handler._make_request("/api/v1/pipelines/foo/phase/complete", method="POST")

        req = captured["req"]
        assert req.data == b"{}"
        assert req.get_header("Content-type") == "application/json"

    def test_post_with_empty_dict_sends_empty_json_object(self, handler):
        """POST with data={} must send b'{}', not no body."""
        mock_opener, captured = self._capture_opener()
        with patch("urllib.request.build_opener", return_value=mock_opener):
            handler._make_request("/api/v1/pipelines/foo/phase/complete", method="POST", data={})

        assert captured["req"].data == b"{}"

    def test_post_with_data_sends_data(self, handler):
        """POST with data preserves existing behavior."""
        mock_opener, captured = self._capture_opener()
        with patch("urllib.request.build_opener", return_value=mock_opener):
            handler._make_request(
                "/api/v1/pipelines/foo/phase/complete",
                method="POST",
                data={"artifacts": {"k": "v"}},
            )

        assert json.loads(captured["req"].data) == {"artifacts": {"k": "v"}}

    def test_get_with_no_data_sends_no_body(self, handler):
        """GET must not send a body just because POST now does."""
        mock_opener, captured = self._capture_opener()
        with patch("urllib.request.build_opener", return_value=mock_opener):
            handler._make_request("/api/v1/pipelines/foo", method="GET")

        assert captured["req"].data is None


# ---------------------------------------------------------------------------
# Deployment-diagnostic MCP tools (#1759)
# ---------------------------------------------------------------------------


class TestGetDeploymentContextTool:
    """``get_deployment_context`` — thin GET proxy that unwraps ``data``."""

    def test_returns_data_from_orchestrator(self, handler):
        payload = {
            "runtime": "kubernetes",
            "namespace": "egg-system",
            "cni": "calico",
            "network_policy_enforcement": True,
            "is_k3s": True,
        }
        with patch.object(
            handler,
            "_make_request",
            return_value={"success": True, "data": payload},
        ) as mock_req:
            result = handler.handle_tool_call("get_deployment_context", {})
        mock_req.assert_called_once_with("/api/v1/deployment/context", method="GET")
        assert result == payload

    def test_docker_not_available_is_surfaced_as_data_field(self, handler):
        """Docker clusters return a ``not_available_on_runtime`` payload, not an error."""
        with patch.object(
            handler,
            "_make_request",
            return_value={
                "success": True,
                "data": {"error": "not_available_on_runtime", "runtime": "docker"},
            },
        ):
            result = handler.handle_tool_call("get_deployment_context", {})
        assert result["error"] == "not_available_on_runtime"
        assert result["runtime"] == "docker"

    def test_http_failure_wraps_into_error(self, handler):
        with patch.object(handler, "_make_request", side_effect=RuntimeError("connection refused")):
            result = handler.handle_tool_call("get_deployment_context", {})
        assert "error" in result
        assert "get_deployment_context failed" in result["error"]


class TestValidateDeploymentManifestsTool:
    """``validate_deployment_manifests`` — POST with optional ``overlay_path``."""

    def test_sends_overlay_path_when_provided(self, handler):
        with patch.object(
            handler,
            "_make_request",
            return_value={"success": True, "data": {"warnings": []}},
        ) as mock_req:
            handler.handle_tool_call(
                "validate_deployment_manifests",
                {"overlay_path": "k8s/overlays/staging"},
            )
        kwargs = mock_req.call_args.kwargs
        args = mock_req.call_args.args
        assert args[0] == "/api/v1/deployment/validate-manifests"
        assert kwargs["method"] == "POST"
        assert kwargs["data"] == {"overlay_path": "k8s/overlays/staging"}

    def test_omits_overlay_path_when_absent(self, handler):
        """No ``overlay_path`` → empty body so the server applies its default."""
        with patch.object(
            handler,
            "_make_request",
            return_value={"success": True, "data": {"warnings": []}},
        ) as mock_req:
            handler.handle_tool_call("validate_deployment_manifests", {})
        kwargs = mock_req.call_args.kwargs
        assert kwargs["data"] == {}

    def test_returns_warnings_list_on_success(self, handler):
        warnings = [{"rule": "secret-missing", "severity": "error"}]
        with patch.object(
            handler,
            "_make_request",
            return_value={"success": True, "data": {"warnings": warnings}},
        ):
            result = handler.handle_tool_call("validate_deployment_manifests", {})
        assert result["warnings"] == warnings

    def test_http_failure_wraps_into_error(self, handler):
        with patch.object(handler, "_make_request", side_effect=RuntimeError("500")):
            result = handler.handle_tool_call("validate_deployment_manifests", {})
        assert "error" in result
        assert "validate_deployment_manifests failed" in result["error"]


class TestPruneStaleWorktreesTool:
    """``prune_stale_worktrees`` — POST proxy; dry-run by default."""

    def test_dry_run_defaults_to_true(self, handler):
        with patch.object(
            handler,
            "_make_request",
            return_value={"success": True, "data": {"removed_count": 0}},
        ) as mock_req:
            handler.handle_tool_call("prune_stale_worktrees", {})
        kwargs = mock_req.call_args.kwargs
        assert kwargs["data"] == {"dry_run": True}

    def test_dry_run_false_forwarded(self, handler):
        with patch.object(
            handler,
            "_make_request",
            return_value={"success": True, "data": {}},
        ) as mock_req:
            handler.handle_tool_call("prune_stale_worktrees", {"dry_run": False})
        kwargs = mock_req.call_args.kwargs
        assert kwargs["data"]["dry_run"] is False

    def test_repo_argument_is_silently_ignored(self, handler):
        """``repo`` was dropped from the schema (see docstring); if callers
        pass it anyway, the handler must NOT forward it upstream — otherwise
        the gateway would 400 and the operator would see an opaque error.

        This is the MEDIUM-2 NACK fix: dropping ``repo`` from the public
        schema rather than silently filtering at the route boundary.
        """
        with patch.object(
            handler,
            "_make_request",
            return_value={"success": True, "data": {}},
        ) as mock_req:
            handler.handle_tool_call(
                "prune_stale_worktrees", {"dry_run": True, "repo": "owner/repo"}
            )
        kwargs = mock_req.call_args.kwargs
        assert "repo" not in kwargs["data"], (
            "repo was dropped from the schema and must not reach the gateway"
        )
        assert kwargs["data"] == {"dry_run": True}

    def test_upstream_failure_wraps_into_error(self, handler):
        with patch.object(handler, "_make_request", side_effect=RuntimeError("gateway down")):
            result = handler.handle_tool_call("prune_stale_worktrees", {})
        assert "error" in result
        assert "prune_stale_worktrees failed" in result["error"]


class TestValidateNetworkIsolationTool:
    """``validate_network_isolation`` — requires ``pipeline_id``."""

    def test_missing_pipeline_id_returns_error(self, handler):
        result = handler.handle_tool_call("validate_network_isolation", {})
        assert result == {"error": "pipeline_id is required"}

    def test_defaults_role_to_coder(self, handler):
        with patch.object(
            handler,
            "_make_request",
            return_value={"success": True, "data": {"probe_id": "abc"}},
        ) as mock_req:
            handler.handle_tool_call("validate_network_isolation", {"pipeline_id": "p1"})
        kwargs = mock_req.call_args.kwargs
        assert kwargs["data"] == {"pipeline_id": "p1", "role": "coder"}

    def test_role_is_forwarded(self, handler):
        with patch.object(
            handler,
            "_make_request",
            return_value={"success": True, "data": {}},
        ) as mock_req:
            handler.handle_tool_call(
                "validate_network_isolation",
                {"pipeline_id": "p2", "role": "reviewer_code"},
            )
        kwargs = mock_req.call_args.kwargs
        assert kwargs["data"]["role"] == "reviewer_code"

    def test_returns_probe_result(self, handler):
        data = {
            "probe_id": "hex",
            "result": {
                "gateway_reachable": True,
                "internet_blocked": True,
                "agent_pods_unreachable": True,
                "orchestrator_direct_blocked": True,
            },
        }
        with patch.object(
            handler,
            "_make_request",
            return_value={"success": True, "data": data},
        ):
            result = handler.handle_tool_call("validate_network_isolation", {"pipeline_id": "p1"})
        assert result["probe_id"] == "hex"
        assert result["result"]["internet_blocked"] is True

    def test_upstream_failure_wraps_into_error(self, handler):
        with patch.object(handler, "_make_request", side_effect=RuntimeError("boom")):
            result = handler.handle_tool_call("validate_network_isolation", {"pipeline_id": "p1"})
        assert "error" in result
        assert "validate_network_isolation failed" in result["error"]


class TestRebuildAndRolloutTool:
    """``rebuild_and_rollout`` — fire-and-forget by default; long-poll when ``wait=True``."""

    def test_no_wait_returns_stream_id(self, handler):
        with patch.object(
            handler,
            "_make_request",
            return_value={
                "success": True,
                "data": {"progress_stream_id": "abc123", "started_at": 1},
            },
        ) as mock_req:
            result = handler.handle_tool_call("rebuild_and_rollout", {})
        # Initial POST kicks off the rollout.
        first_call = mock_req.call_args_list[0]
        assert first_call.args[0] == "/api/v1/deployment/rebuild-and-rollout"
        assert first_call.kwargs["method"] == "POST"
        assert result["progress_stream_id"] == "abc123"

    def test_not_available_on_runtime_short_circuits(self, handler):
        with patch.object(
            handler,
            "_make_request",
            return_value={
                "success": True,
                "data": {"error": "not_available_on_runtime", "runtime": "docker"},
            },
        ):
            result = handler.handle_tool_call("rebuild_and_rollout", {})
        assert result["error"] == "not_available_on_runtime"

    def test_409_surfaces_rollout_already_in_progress(self, handler):
        """A 409 from the orchestrator becomes a structured error payload."""

        class _FakeErr(HTTPError):
            def __init__(self):
                super().__init__(url="http://x", code=409, msg="conflict", hdrs={}, fp=None)

            def read(self):
                import json as _json

                return _json.dumps(
                    {
                        "success": False,
                        "message": "rollout_already_in_progress",
                        "data": {
                            "error": "rollout_already_in_progress",
                            "progress_stream_id": "existing",
                        },
                    }
                ).encode()

        with patch.object(handler, "_make_request", side_effect=_FakeErr()):
            result = handler.handle_tool_call("rebuild_and_rollout", {})
        assert result["error"] == "rollout_already_in_progress"
        assert result["progress_stream_id"] == "existing"

    def test_other_http_error_returns_generic_error(self, handler):
        class _FakeErr(HTTPError):
            def __init__(self):
                super().__init__(url="http://x", code=500, msg="boom", hdrs={}, fp=None)

            def read(self):
                return b""

        with patch.object(handler, "_make_request", side_effect=_FakeErr()):
            result = handler.handle_tool_call("rebuild_and_rollout", {})
        assert "error" in result
        assert "HTTP 500" in result["error"]

    def test_wait_polls_until_terminal_record(self, handler):
        """``wait=True`` polls the stream endpoint and returns the terminal event."""
        started = {
            "success": True,
            "data": {"progress_stream_id": "stream-1", "started_at": 1},
        }
        poll1 = {
            "success": True,
            "data": {
                "stream_id": "stream-1",
                "events": [{"phase": "line", "line": "building"}],
                "next_since": 1,
                "done": False,
            },
        }
        poll2 = {
            "success": True,
            "data": {
                "stream_id": "stream-1",
                "events": [
                    {
                        "phase": "done",
                        "exit_code": 0,
                        "rolled_out_images": {"egg-orchestrator:dev": "imported"},
                    }
                ],
                "next_since": 2,
                "done": True,
            },
        }

        call_count = {"n": 0}

        def _fake_request(endpoint, method="GET", data=None, timeout=30):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return started
            if call_count["n"] == 2:
                return poll1
            return poll2

        with (
            patch.object(handler, "_make_request", side_effect=_fake_request),
            patch("time.sleep"),
        ):
            result = handler.handle_tool_call("rebuild_and_rollout", {"wait": True})

        assert result["progress_stream_id"] == "stream-1"
        assert result["exit_code"] == 0
        assert result["rolled_out_images"] == {"egg-orchestrator:dev": "imported"}
        assert result["terminal"]["phase"] == "done"

    def test_wait_poll_failure_returns_stream_id_and_error(self, handler):
        """If polling fails mid-rollout, we still return the stream id for resume."""
        started = {
            "success": True,
            "data": {"progress_stream_id": "s-x", "started_at": 1},
        }
        call_count = {"n": 0}

        def _fake_request(endpoint, method="GET", data=None, timeout=30):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return started
            raise RuntimeError("poll dropped")

        with (
            patch.object(handler, "_make_request", side_effect=_fake_request),
            patch("time.sleep"),
        ):
            result = handler.handle_tool_call("rebuild_and_rollout", {"wait": True})

        assert result["progress_stream_id"] == "s-x"
        assert "error" in result
        assert "poll" in result["error"].lower()


class TestGetServiceLogsTool:
    """``get_service_logs`` — thin GET proxy with query-string construction."""

    def test_requires_service(self, handler):
        result = handler.handle_tool_call("get_service_logs", {})
        assert result.get("error") == "service is required"

    def test_forwards_service_lines_and_since_seconds(self, handler):
        with patch.object(
            handler,
            "_make_request",
            return_value={
                "success": True,
                "data": {
                    "service": "gateway",
                    "namespace": "egg-system",
                    "pods": [{"pod": "gateway-abc", "logs": "ok"}],
                },
            },
        ) as mock_req:
            result = handler.handle_tool_call(
                "get_service_logs",
                {"service": "gateway", "lines": 50, "since_seconds": 300},
            )

        # The args go into the query string, not the body.
        endpoint = mock_req.call_args.args[0]
        assert endpoint.startswith("/api/v1/deployment/logs?")
        assert "service=gateway" in endpoint
        assert "lines=50" in endpoint
        assert "since_seconds=300" in endpoint
        assert mock_req.call_args.kwargs["method"] == "GET"
        assert result["pods"][0]["pod"] == "gateway-abc"

    def test_service_is_url_encoded(self, handler):
        """Any odd characters in service are url-encoded, not splatted raw."""
        with patch.object(
            handler,
            "_make_request",
            return_value={"success": True, "data": {}},
        ) as mock_req:
            handler.handle_tool_call("get_service_logs", {"service": "a/b c"})
        endpoint = mock_req.call_args.args[0]
        assert "service=a%2Fb%20c" in endpoint

    def test_http_failure_wraps_into_error(self, handler):
        with patch.object(handler, "_make_request", side_effect=RuntimeError("gateway down")):
            result = handler.handle_tool_call("get_service_logs", {"service": "gateway"})
        assert "error" in result
        assert "get_service_logs failed" in result["error"]

    def test_http_error_surfaces_orchestrator_message(self, handler):
        """HTTPError body must be unwrapped so the real cause is visible.

        Before #1870, the caller saw only ``HTTP Error 500: INTERNAL
        SERVER ERROR`` because urllib's HTTPError string hides the
        response body. The handler now reads the JSON body and pulls
        out the orchestrator's ``message`` field.
        """
        import io

        body = json.dumps(
            {
                "success": False,
                "message": (
                    "Failed to read deployment gateway in egg-system: (403) Reason: Forbidden"
                ),
            }
        ).encode()
        http_err = HTTPError(
            url="http://localhost:9849/api/v1/deployment/logs",
            code=500,
            msg="INTERNAL SERVER ERROR",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(body),
        )

        with patch.object(handler, "_make_request", side_effect=http_err):
            result = handler.handle_tool_call("get_service_logs", {"service": "gateway"})

        assert "error" in result
        assert "HTTP 500" in result["error"]
        assert "Forbidden" in result["error"]
        assert "egg-system" in result["error"]

    def test_http_error_without_json_body_falls_back_to_default(self, handler):
        """Non-JSON or empty bodies fall back to the urllib default string."""
        import io

        http_err = HTTPError(
            url="http://localhost:9849/api/v1/deployment/logs",
            code=502,
            msg="Bad Gateway",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"<html>nginx</html>"),
        )

        with patch.object(handler, "_make_request", side_effect=http_err):
            result = handler.handle_tool_call("get_service_logs", {"service": "gateway"})

        assert "error" in result
        # Must hit the fallback path ("get_service_logs failed: <str(exc)>"),
        # NOT the structured path ("get_service_logs failed (HTTP N): <detail>").
        assert result["error"] == "get_service_logs failed: HTTP Error 502: Bad Gateway"


class TestPipelineToolsSchemasForDeployment:
    """Guard the input-schema shape of the five new tools."""

    def test_get_service_logs_enumerates_allowed_services(self):
        from mcp_tools import PIPELINE_TOOLS

        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        schema = tools_by_name["get_service_logs"]["inputSchema"]
        props = schema["properties"]
        assert schema.get("required") == ["service"]
        assert set(props["service"].get("enum", [])) == {"gateway", "orchestrator"}
        assert props["lines"]["default"] == 100

    def test_get_service_logs_enum_matches_route_allowlist(self):
        """MCP schema enum and route allowlist must stay in sync."""
        from mcp_tools import PIPELINE_TOOLS
        from routes.deployment import _SERVICE_LOG_ALLOWLIST

        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        schema_enum = set(
            tools_by_name["get_service_logs"]["inputSchema"]["properties"]["service"]["enum"]
        )
        assert schema_enum == _SERVICE_LOG_ALLOWLIST

    def test_validate_network_isolation_requires_pipeline_id(self):
        from mcp_tools import PIPELINE_TOOLS

        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        schema = tools_by_name["validate_network_isolation"]["inputSchema"]
        assert schema.get("required") == ["pipeline_id"]

    def test_prune_stale_worktrees_dry_run_default_true(self):
        from mcp_tools import PIPELINE_TOOLS

        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        props = tools_by_name["prune_stale_worktrees"]["inputSchema"]["properties"]
        assert props["dry_run"]["default"] is True

    def test_prune_stale_worktrees_has_no_repo_argument(self):
        """Per the MEDIUM-2 NACK fix: ``repo`` was dropped because the
        gateway helper always sweeps every repo; advertising a scope
        argument that silently no-ops would mislead operators.
        """
        from mcp_tools import PIPELINE_TOOLS

        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        schema = tools_by_name["prune_stale_worktrees"]["inputSchema"]
        props = schema["properties"]
        assert "repo" not in props, (
            "repo must stay out of the schema — see NACK response on 8434d4dcf"
        )
        # Ensure ``additionalProperties`` (or no 'required' list) doesn't
        # accidentally invite callers to pass arbitrary scope args.
        required = schema.get("required", [])
        assert "repo" not in required

    def test_rebuild_and_rollout_wait_default_false(self):
        from mcp_tools import PIPELINE_TOOLS

        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        props = tools_by_name["rebuild_and_rollout"]["inputSchema"]["properties"]
        assert props["wait"]["default"] is False
