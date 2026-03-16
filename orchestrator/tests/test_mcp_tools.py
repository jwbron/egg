"""Tests for MCP tool handlers.

Tests cover the 10 new tools added for comprehensive platform interface:
- check_health, list_containers, get_container_logs, send_message,
  get_consensus_status, get_phase, get_pipeline_snapshot (orchestrator-backed)
- list_checkpoints, search_checkpoints, get_contract (gateway-backed)
"""

from unittest.mock import MagicMock, patch

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


class TestCheckHealth:
    def test_both_healthy(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"status": "healthy"}
            with patch("orchestrator.gateway_client.GatewayClient") as MockGW:
                mock_gw = MagicMock()
                mock_health = MagicMock()
                mock_health.healthy = True
                mock_health.status = "healthy"
                mock_health.version = "1.0"
                mock_gw.check_health.return_value = mock_health
                MockGW.return_value = mock_gw

                result = handler.handle_tool_call("check_health", {})

        assert result["healthy"] is True
        assert result["orchestrator"]["healthy"] is True
        assert result["gateway"]["healthy"] is True

    def test_orchestrator_unreachable(self, handler):
        with patch.object(handler, "_make_request", side_effect=Exception("connection refused")):
            with patch("orchestrator.gateway_client.GatewayClient") as MockGW:
                mock_gw = MagicMock()
                mock_health = MagicMock()
                mock_health.healthy = True
                mock_health.status = "healthy"
                mock_health.version = "1.0"
                mock_gw.check_health.return_value = mock_health
                MockGW.return_value = mock_gw

                result = handler.handle_tool_call("check_health", {})

        assert result["healthy"] is False
        assert result["orchestrator"]["healthy"] is False
        assert "unreachable" in result["orchestrator"]["status"]


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
                    "message_type": "QUESTION",
                },
            )

        data = mock_req.call_args[1]["data"]
        assert data["subject"] == "Status check"
        assert data["message_type"] == "QUESTION"


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


class TestGetContract:
    def test_with_issue_number(self, handler):
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

        mock_gw.assert_called_once_with("/api/v1/contract/42")

    def test_missing_both_params(self, handler):
        result = handler.handle_tool_call("get_contract", {})
        assert "error" in result

    def test_task_id_without_issue(self, handler):
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"data": {"pipeline": {"issue_number": None}}}
            result = handler.handle_tool_call("get_contract", {"task_id": "prompt-based"})

        assert "error" in result


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
    """Verify all 15 tools are routed correctly."""

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
        }
        assert tool_names == expected

    def test_unknown_tool(self, handler):
        result = handler.handle_tool_call("nonexistent", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]
