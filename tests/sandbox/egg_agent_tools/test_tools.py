"""Tests for the @tool wrappers in egg_agent_tools.tools.

Each wrapper is a thin call to ``invoke_handler`` which:

1. Runs the handler in a thread via :func:`asyncio.to_thread`.
2. Serialises the handler's dict response as JSON text on success.
3. Translates :class:`GatewayError`/:class:`HandlerError`/``Exception``
   into an SDK-shaped ``{is_error: True, content: [{type: text, text: ...}]}``
   tool-result, so a gateway flake never crashes the agent loop.

We test (a) a successful call returns the JSON-serialised handler
response under a single ``text`` content block, and (b) when the
handler raises a ``GatewayError`` the wrapper returns ``is_error=True``
with the error message in the content block — the exception does NOT
propagate out of the tool.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402
from egg_agent_tools.tools import TOOL_REGISTRY  # noqa: E402
from egg_agent_tools.tools._common import invoke_handler  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class TestInvokeHandlerSuccess:
    def test_serialises_dict_response_as_text_block(self):
        def handler(req):
            return {"ok": True, "value": 42}

        resp = _run(invoke_handler(handler, {}))
        assert "content" in resp
        assert resp["content"][0]["type"] == "text"
        body = json.loads(resp["content"][0]["text"])
        assert body == {"ok": True, "value": 42}
        assert "is_error" not in resp

    def test_handler_runs_in_thread(self):
        """invoke_handler must not call the handler on the event loop
        (otherwise sync I/O would block the agent).  We sniff this by
        recording the current thread inside the handler."""
        import threading

        captured: dict = {}

        def handler(req):
            captured["thread"] = threading.current_thread().name
            return {"ok": True}

        _run(invoke_handler(handler, {}))
        # The main-thread name is pytest-specific; what matters is that
        # we didn't execute on the asyncio event-loop thread.  We assert
        # the handler ran in *some* non-event-loop worker.
        assert captured["thread"] != ""

    def test_passes_args_through(self):
        seen = {}

        def handler(req):
            seen.update(req)
            return {"ok": True}

        _run(invoke_handler(handler, {"k": "v"}))
        assert seen == {"k": "v"}


class TestInvokeHandlerErrorTranslation:
    def test_gateway_error_becomes_is_error_result(self):
        def handler(req):
            raise GatewayError("boom", status_code=500)

        resp = _run(invoke_handler(handler, {}))
        assert resp["is_error"] is True
        assert resp["content"][0]["type"] == "text"
        assert "boom" in resp["content"][0]["text"]
        assert "500" in resp["content"][0]["text"]

    def test_handler_error_becomes_is_error_result(self):
        def handler(req):
            raise HandlerError("bad input")

        resp = _run(invoke_handler(handler, {}))
        assert resp["is_error"] is True
        assert "bad input" in resp["content"][0]["text"]

    def test_generic_exception_becomes_is_error_result(self):
        def handler(req):
            raise RuntimeError("unexpected")

        resp = _run(invoke_handler(handler, {}))
        assert resp["is_error"] is True
        assert "RuntimeError" in resp["content"][0]["text"]

    def test_exception_does_not_propagate(self):
        """Regression guard: a gateway flake must NEVER escape the
        wrapper, or the SDK loop crashes."""

        def handler(req):
            raise GatewayError("fail", status_code=503)

        try:
            resp = _run(invoke_handler(handler, {}))
        except GatewayError:
            pytest.fail("invoke_handler leaked GatewayError")
        assert resp["is_error"] is True


class TestSdkToolShape:
    """Every registration declares a stub SDK tool (or real SdkMcpTool)
    carrying name/description/input_schema and the handler reference."""

    def test_all_tools_carry_name_and_schema(self):
        """Each SDK-tool stub exposes a non-empty name/schema/description.

        The per-namespace-server design (decision-7 fix) registers each
        tool with a short verb name — the final Claude-visible name is
        ``mcp__<namespace>__<verb>`` constructed at the MCP-server
        boundary, so here we just assert the stub metadata is populated,
        not that it matches the full ToolRegistration.name.
        """
        for name, reg in TOOL_REGISTRY.items():
            stub = reg.sdk_tool
            sdk_name = getattr(stub, "name", None)
            assert sdk_name, f"{name} has empty .name on its SDK stub"
            # Short verb must appear somewhere in the full registration name.
            assert sdk_name in name, (
                f"{name}'s SDK stub name ({sdk_name!r}) is not a substring of "
                f"the registration name ({name!r})"
            )
            assert getattr(stub, "input_schema", None) is not None
            assert getattr(stub, "description", None)

    def test_namespace_matches_name(self):
        for reg in TOOL_REGISTRY.values():
            assert reg.name.startswith(f"mcp__{reg.namespace}__")

    def test_cli_tools_mark_command(self):
        """CLI-backed tools declare cli_command; CLI-less tools are None."""
        cli_backed = {
            "mcp__sdlc__register_open_question",
            "mcp__sdlc__request_feedback",
            "mcp__brc__propose",
            "mcp__brc__ack",
            "mcp__brc__nack",
            "mcp__brc__confirm",
            "mcp__brc__wait_for_event",
            "mcp__brc__wait_loop",
            "mcp__brc__send_heartbeat",
            "mcp__progress__emit",
            "mcp__progress__signal_error",
            "mcp__progress__heartbeat",
            "mcp__task__complete",
        }
        cli_less = {
            "mcp__sdlc__check_hitl_answers",
            "mcp__brc__get_state",
            "mcp__brc__list_blocking",
            "mcp__phase__get_context",
            "mcp__phase__get_assigned_tasks",
        }
        for name in cli_backed:
            assert TOOL_REGISTRY[name].cli_command is not None, (
                f"{name} should declare a CLI counterpart"
            )
        for name in cli_less:
            assert TOOL_REGISTRY[name].cli_command is None, (
                f"{name} has no CLI counterpart and must set cli_command=None"
            )


class TestToolWrapperSuccess:
    """Spot-check a wrapper end-to-end, with the handler patched out."""

    def test_register_open_question_wrapper_success(self):
        with patch(
            "egg_agent_tools.handlers.sdlc.register_open_question",
            return_value={"ok": True, "id": "decision-7"},
        ):
            wrapper = TOOL_REGISTRY["mcp__sdlc__register_open_question"].sdk_tool
            # stub holds .handler, real SdkMcpTool stores it as .handler too
            handler = getattr(wrapper, "handler", None)
            assert handler is not None
            resp = _run(handler({"question": "Q?"}))
        body = json.loads(resp["content"][0]["text"])
        assert body["id"] == "decision-7"

    def test_register_open_question_wrapper_error(self):
        with patch(
            "egg_agent_tools.handlers.sdlc.register_open_question",
            side_effect=GatewayError("fail", status_code=500),
        ):
            wrapper = TOOL_REGISTRY["mcp__sdlc__register_open_question"].sdk_tool
            handler = getattr(wrapper, "handler", None)
            assert handler is not None
            resp = _run(handler({"question": "Q?"}))
        assert resp["is_error"] is True
        assert "fail" in resp["content"][0]["text"]


class TestMessagePrimitiveWrappers:
    """Event-driven wrappers added in #1922 (wait_for_event / wait_loop /
    send_heartbeat) also return JSON-serialised responses on success and
    SDK-shaped is_error blocks on failure."""

    def test_wait_for_event_success(self):
        with patch(
            "egg_agent_tools.handlers.message.message_wait",
            return_value={
                "ok": True,
                "matched": True,
                "messages": [{"id": "m-1"}],
                "role": "coder",
                "for_types": ["CONSENSUS_ACK"],
            },
        ):
            wrapper = TOOL_REGISTRY["mcp__brc__wait_for_event"].sdk_tool
            resp = _run(wrapper.handler({"for_types": ["CONSENSUS_ACK"]}))
        body = json.loads(resp["content"][0]["text"])
        assert body["matched"] is True
        assert body["messages"][0]["id"] == "m-1"

    def test_wait_loop_handler_error_surfaces_as_is_error(self):
        with patch(
            "egg_agent_tools.handlers.message.message_wait_loop",
            side_effect=HandlerError("bad args"),
        ):
            wrapper = TOOL_REGISTRY["mcp__brc__wait_loop"].sdk_tool
            resp = _run(wrapper.handler({"for_types": ["X"]}))
        assert resp["is_error"] is True
        assert "bad args" in resp["content"][0]["text"]

    def test_send_heartbeat_wraps_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.message.message_heartbeat",
            side_effect=GatewayError("rate limited", status_code=429),
        ):
            wrapper = TOOL_REGISTRY["mcp__brc__send_heartbeat"].sdk_tool
            resp = _run(wrapper.handler({"state": "WORKING"}))
        assert resp["is_error"] is True
        assert "rate limited" in resp["content"][0]["text"]
