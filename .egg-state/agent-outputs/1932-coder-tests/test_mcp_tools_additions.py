"""Additions to ``orchestrator/tests/test_mcp_tools.py`` for
``wait_for_status_change`` (issue #1932 TASK-4-2 + TASK-4-4).

HANDOFF NOTE to tester: these test classes were authored by the
coder alongside the Phase 2 handler implementation.  All cases
pass on commit ``1258ff399``.

Merge instructions:
    1. Append the two classes below (``TestWaitForStatusChange``,
       ``TestBuildStatusSnapshotRefactor``) to
       ``orchestrator/tests/test_mcp_tools.py`` just BEFORE the
       existing ``class TestAdvancePhase`` block.
    2. Append the ``test_wait_for_status_change_does_not_double_sleep``
       method to the existing ``TestGetStatusWait`` class (right
       after ``test_other_tools_ignore_wait``).
    3. In ``TestToolRouting.test_all_tools_registered``, add
       ``"wait_for_status_change"`` to the ``expected`` set.

All three additions are self-contained and use the existing
``handler`` fixture.
"""

from unittest.mock import patch


class TestWaitForStatusChange:
    """Tests for ``_handle_wait_for_status_change`` (issue #1932 TASK-2-3)."""

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

    def _messages_response(self):
        return {"data": {"messages": []}}

    def test_dispatcher_routes_wait_tool(self, handler):
        with patch.object(
            handler,
            "_handle_wait_for_status_change",
            return_value={"changed": False, "no_change": True, "cursor": "msg:|evt:0"},
        ) as mock_handler:
            result = handler.handle_tool_call(
                "wait_for_status_change",
                {"task_id": "issue-42", "wait": 25},
            )

        mock_handler.assert_called_once()
        assert result == {
            "changed": False,
            "no_change": True,
            "cursor": "msg:|evt:0",
        }

    def test_no_change_envelope_passed_through_verbatim(self, handler):
        route_response = {
            "data": {
                "changed": False,
                "no_change": True,
                "current_phase": "implement",
                "status": "running",
                "phase_elapsed_seconds": 152,
                "cursor": "msg:1738012750-0|evt:148",
                "concurrent": {"consensus": {"is_complete": False}},
            }
        }
        with patch.object(handler, "_make_request", return_value=route_response):
            result = handler.handle_tool_call(
                "wait_for_status_change",
                {"task_id": "issue-42", "wait": 25},
            )
        assert result["changed"] is False
        assert result["no_change"] is True
        assert result["current_phase"] == "implement"
        assert result["cursor"] == "msg:1738012750-0|evt:148"
        assert "pipeline" not in result
        assert "running_agents" not in result
        assert "recent_messages" not in result

    def test_changed_true_envelope_merges_snapshot(self, handler):
        route_response = {
            "data": {
                "changed": True,
                "trigger": "event",
                "event_type": "phase.started",
                "current_phase": "plan",
                "status": "running",
                "phase_elapsed_seconds": 10,
                "cursor": "msg:abc|evt:5",
            }
        }
        with patch.object(
            handler,
            "_make_request",
            side_effect=[
                route_response,
                self._pipeline_response(),
                self._messages_response(),
            ],
        ):
            result = handler.handle_tool_call(
                "wait_for_status_change",
                {"task_id": "issue-42", "wait": 25},
            )

        assert result["changed"] is True
        assert result["trigger"] == "event"
        assert result["event_type"] == "phase.started"
        assert result["cursor"] == "msg:abc|evt:5"
        assert result["pipeline"]["id"] == "issue-42"
        assert result["pipeline"]["repo"] == "org/repo"
        assert len(result["running_agents"]) == 1
        assert len(result["completed_agents"]) == 1
        assert "recent_messages" in result
        assert result["current_phase"] == "plan"

    def test_changed_true_message_envelope_merges_snapshot(self, handler):
        route_response = {
            "data": {
                "changed": True,
                "trigger": "message",
                "messages": [
                    {
                        "id": "msg-1",
                        "message_type": "OVERSEER_ALERT",
                        "from_role": "overseer",
                        "subject": "stall detected",
                        "body": "coder hasn't emitted heartbeat in 60s",
                        "timestamp": "2026-04-23T07:00:00Z",
                    }
                ],
                "current_phase": "implement",
                "status": "running",
                "cursor": "msg:msg-1|evt:5",
            }
        }
        with patch.object(
            handler,
            "_make_request",
            side_effect=[
                route_response,
                self._pipeline_response(),
                self._messages_response(),
            ],
        ):
            result = handler.handle_tool_call(
                "wait_for_status_change",
                {"task_id": "issue-42", "wait": 25},
            )

        assert result["changed"] is True
        assert result["trigger"] == "message"
        assert len(result["messages"]) == 1
        assert result["messages"][0]["message_type"] == "OVERSEER_ALERT"
        assert result["pipeline"]["id"] == "issue-42"

    def test_since_cursor_in_query_string(self, handler):
        route_response = {
            "data": {"changed": False, "no_change": True, "cursor": "msg:x|evt:1"}
        }
        with patch.object(
            handler, "_make_request", return_value=route_response
        ) as mock_req:
            handler.handle_tool_call(
                "wait_for_status_change",
                {
                    "task_id": "issue-42",
                    "wait": 25,
                    "since": "msg:1738012734-0|evt:142",
                },
            )
            called_with = mock_req.call_args_list[0][0][0]
            assert "wait=25" in called_with
            assert "since=msg" in called_with
            assert "%7C" in called_with

    def test_empty_since_omits_param(self, handler):
        route_response = {
            "data": {"changed": False, "no_change": True, "cursor": "msg:|evt:0"}
        }
        with patch.object(
            handler, "_make_request", return_value=route_response
        ) as mock_req:
            handler.handle_tool_call(
                "wait_for_status_change",
                {"task_id": "issue-42", "wait": 25, "since": ""},
            )
            called_with = mock_req.call_args_list[0][0][0]
            assert "since=" not in called_with


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
            status_via_handler = handler.handle_tool_call(
                "get_status", {"task_id": "issue-42"}
            )
        assert snapshot_direct == status_via_handler


# Append this method to the existing TestGetStatusWait class in
# tests/test_mcp_tools.py (right after ``test_other_tools_ignore_wait``):

WAIT_TOOL_DOUBLE_SLEEP_REGRESSION = '''
    @patch("mcp_server._async_sleep", new_callable=AsyncMock)
    def test_wait_for_status_change_does_not_double_sleep(self, mock_sleep):
        """Regression pin for issue #1932 R16.

        ``wait_for_status_change`` blocks server-side inside the Flask
        route for up to 25s.  If a future author generalises
        ``_apply_get_status_wait`` from ``tool_name == 'get_status'`` to
        "any tool with a ``wait`` param", the wait tool would silently
        get a second ``asyncio.sleep`` on the event loop — blowing
        through the upstream Claude Code client timeout (~30s) and
        effectively breaking the feature.  This test pins the
        short-circuit so that refactor surfaces as a test failure
        instead of a latent 50-second double-sleep.
        """
        from mcp_server import _apply_get_status_wait

        kwargs = {"task_id": "issue-42", "wait": 25, "since": "msg:|evt:0"}
        asyncio.run(_apply_get_status_wait("wait_for_status_change", kwargs))

        mock_sleep.assert_not_called()
        # The wait param must remain in kwargs so the tool handler
        # sees it and forwards it to the Flask route as the
        # server-side cap — consuming it here would leak the intent.
        assert kwargs["wait"] == 25
        assert kwargs["since"] == "msg:|evt:0"
'''


# And in TestToolRouting.test_all_tools_registered, add:
EXPECTED_TOOL_NAME_ADDITION = '"wait_for_status_change"'
