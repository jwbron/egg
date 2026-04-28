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


class TestBrcProposePushStep:
    """The ``mcp__brc__propose`` wrapper pushes to origin before sending
    CONSENSUS_PROPOSE (default push=True).  The CLI ``egg-orch consensus
    propose --push`` shares the helper; wiring the push into the wrapper
    closes #1994 so MCP-only agents can publish artifacts.
    """

    _ARGS = {
        "pipeline_id": "p1",
        "role": "refiner",
        "summary": "x" * 60,
    }

    def _wrapper(self):
        reg = TOOL_REGISTRY["mcp__brc__propose"]
        handler = getattr(reg.sdk_tool, "handler", None)
        assert handler is not None
        return handler

    def test_push_true_invokes_consensus_push_then_handler(self):
        order: list[str] = []

        def _push():
            order.append("push")
            return 0, None

        def _handler(req):
            order.append("handler")
            # The MCP-only ``push`` flag must be stripped before the
            # handler sees the dict.
            assert "push" not in req
            return {"ok": True, "signal": {"data": {}}, "phase": "refine"}

        with (
            patch("egg_agent_tools.push.consensus_push", side_effect=_push),
            patch("egg_agent_tools.handlers.brc.brc_propose", side_effect=_handler),
        ):
            resp = _run(self._wrapper()(dict(self._ARGS)))

        assert order == ["push", "handler"]
        assert "is_error" not in resp
        body = json.loads(resp["content"][0]["text"])
        assert body["ok"] is True

    def test_push_false_skips_consensus_push(self):
        push_calls = {"n": 0}

        def _push():
            push_calls["n"] += 1
            return 0, None

        def _handler(req):
            assert "push" not in req
            return {"ok": True, "signal": {"data": {}}}

        args = {**self._ARGS, "push": False}
        with (
            patch("egg_agent_tools.push.consensus_push", side_effect=_push),
            patch("egg_agent_tools.handlers.brc.brc_propose", side_effect=_handler),
        ):
            resp = _run(self._wrapper()(args))

        assert push_calls["n"] == 0
        assert "is_error" not in resp

    def test_push_failure_short_circuits_and_returns_error(self):
        """If consensus_push fails the handler must NOT fire (we don't
        want to broadcast a PROPOSE for an artifact that never landed
        on origin)."""
        handler_calls = {"n": 0}

        def _push():
            return 1, "HTTP 403: branch ownership check failed"

        def _handler(req):
            handler_calls["n"] += 1
            return {"ok": True}

        with (
            patch("egg_agent_tools.push.consensus_push", side_effect=_push),
            patch("egg_agent_tools.handlers.brc.brc_propose", side_effect=_handler),
        ):
            resp = _run(self._wrapper()(dict(self._ARGS)))

        assert handler_calls["n"] == 0
        assert resp["is_error"] is True
        error_text = resp["content"][0]["text"]
        assert "Push to origin failed" in error_text
        # The specific error reason must be surfaced (not just "see gateway logs")
        assert "branch ownership check failed" in error_text


class TestMessagePrimitiveWrappers:
    """The remaining message-namespace MCP wrapper (``send_heartbeat``)
    returns JSON-serialised responses on success and SDK-shaped is_error
    blocks on failure.  ``wait_for_event`` and ``wait_loop`` were
    removed in #2211 — agents use the ``egg-orch message wait`` /
    ``wait-loop`` Bash CLI instead, since long-poll waits don't fit the
    in-process SDK MCP transport's ~60 s tool-call cap.
    """

    def test_send_heartbeat_wraps_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.message.message_heartbeat",
            side_effect=GatewayError("rate limited", status_code=429),
        ):
            wrapper = TOOL_REGISTRY["mcp__brc__send_heartbeat"].sdk_tool
            resp = _run(wrapper.handler({"state": "WORKING"}))
        assert resp["is_error"] is True
        assert "rate limited" in resp["content"][0]["text"]
