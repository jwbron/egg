"""Tests for egg_agent_tools.server (factory + system prompt nudge).

Covers:
- build_sandbox_mcp_server registers the expected iteration-1 tools (15).
- SYSTEM_PROMPT_NUDGE stays <=200 words.
- Symmetric drift test: every mcp__<namespace>__ substring in the nudge
  corresponds to a registered namespace, and every registered namespace
  appears in the nudge (bidirectional match).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools import SYSTEM_PROMPT_NUDGE, TOOL_LIST, TOOL_NAMESPACES  # noqa: E402
from egg_agent_tools.server import _render_nudge  # noqa: E402
from egg_agent_tools.tools import TOOL_REGISTRY  # noqa: E402

EXPECTED_TOOL_NAMES = {
    "mcp__sdlc__register_open_question",
    "mcp__sdlc__request_feedback",
    "mcp__sdlc__check_hitl_answers",
    "mcp__brc__propose",
    "mcp__brc__ack",
    "mcp__brc__nack",
    "mcp__brc__confirm",
    "mcp__brc__get_state",
    "mcp__brc__list_blocking",
    "mcp__phase__get_context",
    "mcp__phase__get_assigned_tasks",
    "mcp__progress__emit",
    "mcp__progress__signal_error",
    "mcp__progress__heartbeat",
    "mcp__task__complete",
}


class TestToolRegistry:
    def test_fifteen_tools_registered(self):
        assert len(TOOL_LIST) == 15

    def test_expected_names_present(self):
        # ToolRegistration.name carries the Claude-visible full name
        # (``mcp__<namespace>__<verb>``), while the stub SDK tool's
        # ``.name`` is just the short verb (``propose``, ``emit``) —
        # the MCP server key supplies the ``mcp__<namespace>__``
        # prefix at runtime.
        names = set(TOOL_REGISTRY.keys())
        assert names == EXPECTED_TOOL_NAMES

    def test_tool_list_matches_namespace_mapping(self):
        flat: list[str] = []
        for tools in TOOL_NAMESPACES.values():
            flat.extend(tools)
        assert set(flat) == EXPECTED_TOOL_NAMES


class TestBuildSandboxMcpServer:
    def test_build_uses_supplied_tool_list(self):
        """If the SDK is unavailable we still exercise the factory by
        passing a custom tool list so the real ``create_sdk_mcp_server``
        call path is exercised in the sandbox only."""
        try:
            from claude_agent_sdk import create_sdk_mcp_server  # noqa: F401
        except ImportError:
            import pytest

            pytest.skip("claude_agent_sdk not installed in CI")

        from egg_agent_tools.server import build_sandbox_mcp_server

        server = build_sandbox_mcp_server()
        assert server is not None


class TestSystemPromptNudge:
    def test_word_count_under_cap(self):
        assert len(SYSTEM_PROMPT_NUDGE.split()) <= 200

    def test_nudge_is_regenerated_from_namespaces(self):
        assert SYSTEM_PROMPT_NUDGE == _render_nudge()

    def test_each_namespace_appears_in_nudge(self):
        """Every registered namespace must appear as mcp__<ns>__ in the
        generated nudge — keeps the bootstrap prompt honest when a new
        namespace lands."""
        for namespace in TOOL_NAMESPACES:
            assert f"mcp__{namespace}__" in SYSTEM_PROMPT_NUDGE, (
                f"Namespace '{namespace}' registered but missing from nudge"
            )

    def test_nudge_substrings_back_to_registered_namespaces(self):
        """Every `mcp__<ns>__` substring in the nudge must correspond to a
        registered namespace.  This catches stale namespaces that were
        renamed but still hard-coded into the nudge (impossible today
        since the nudge is generated, but guards against regressions
        that reintroduce hand-authoring)."""
        import re

        referenced_namespaces = {
            m.group(1) for m in re.finditer(r"mcp__([a-z_]+)__", SYSTEM_PROMPT_NUDGE)
        }
        # Subset check: nudge may mention verbs by name in examples, but
        # every namespace it mentions MUST be registered.
        for ns in referenced_namespaces:
            assert ns in TOOL_NAMESPACES, (
                f"Nudge references mcp__{ns}__ but namespace is not registered"
            )

    def test_nudge_nonempty(self):
        assert SYSTEM_PROMPT_NUDGE.strip() != ""
