"""Tests for egg_agent_tools.server (factory + system prompt nudge).

Covers:
- build_sandbox_mcp_server registers the expected iteration-2 tools (29:
  the 18 iteration-1 verbs plus the 12 iteration-2 additions landed in
  #1917, minus 2 wait verbs removed in #2211, plus
  ``mcp__brc__resolve_obligation`` added in #2338, across the sdlc,
  brc, phase, progress, task, and checkpoint namespaces).
- SYSTEM_PROMPT_NUDGE stays <=200 words.
- Symmetric drift test: every mcp__<namespace>__ substring in the nudge
  corresponds to a registered namespace, and every registered namespace
  appears in the nudge (bidirectional match).
- Derived-count / namespace-set assertions so future iterations that
  add/remove a tool trip this suite instead of silently drifting the
  prose verb count in ``docs/reference/agent-tools.md``.
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

# Iteration-1 verbs (18).  Kept in its own set for documentation so the
# reader can see the #1917 additions clearly.
_ITER1_TOOL_NAMES = {
    "mcp__sdlc__register_open_question",
    "mcp__sdlc__request_feedback",
    "mcp__sdlc__check_hitl_answers",
    "mcp__brc__propose",
    "mcp__brc__ack",
    "mcp__brc__nack",
    "mcp__brc__confirm",
    "mcp__brc__get_state",
    "mcp__brc__list_blocking",
    "mcp__brc__send_heartbeat",
    "mcp__phase__get_context",
    "mcp__phase__get_assigned_tasks",
    "mcp__progress__emit",
    "mcp__progress__signal_error",
    "mcp__progress__heartbeat",
    "mcp__task__complete",
}

# Iteration-2 additions (12, #1917): 2 sdlc, 3 task, 1 phase, 2
# progress, 1 brc, 3 checkpoint.  The anchor trio and directed peer
# send/poll verbs were deferred per decisions 2 and 14.
_ITER2_TOOL_NAMES = {
    # sdlc
    "mcp__sdlc__show_contract",
    "mcp__sdlc__verify_criterion",
    # task
    "mcp__task__add_commit",
    "mcp__task__update_notes",
    "mcp__task__mark_gap",
    # phase
    "mcp__phase__complete_phase",
    # progress
    "mcp__progress__overseer_alert",
    "mcp__progress__query_status",
    # brc
    "mcp__brc__read_peer_artifact",
    # checkpoint (new namespace)
    "mcp__checkpoint__list",
    "mcp__checkpoint__show",
    "mcp__checkpoint__search",
}

# Post-#1917 additions tracked separately so the diff stays auditable.
_POST_ITER2_TOOL_NAMES = {
    # brc — #2338 in-cycle conditional-ACK obligation resolution.
    "mcp__brc__resolve_obligation",
    # sdlc — #2529 runtime escape-hatch (file-restriction self-check
    # + typed Impasse signal).
    "mcp__sdlc__check_file_restriction",
    "mcp__sdlc__report_impasse",
}

EXPECTED_TOOL_NAMES = _ITER1_TOOL_NAMES | _ITER2_TOOL_NAMES | _POST_ITER2_TOOL_NAMES

EXPECTED_NAMESPACES = {
    "sdlc",
    "brc",
    "phase",
    "progress",
    "task",
    "checkpoint",
}


class TestToolRegistry:
    def test_tool_count_registered(self):
        # 18 iter-1 + 12 iter-2 (#1917) = 30, then -2 in #2211
        # (``wait_for_event`` + ``wait_loop`` removed — agents now use
        # the ``egg-orch message wait`` / ``wait-loop`` Bash CLI for
        # blocking waits per the transport-mismatch carve-out) = 28,
        # then +1 in #2338 (``mcp__brc__resolve_obligation``) = 29,
        # then +2 in #2529 (``check_file_restriction`` +
        # ``report_impasse`` — runtime escape hatch) = 31.
        # Derived assertion: trips when a future iteration drifts the
        # count without updating the prose verb-counts in
        # docs/reference/agent-tools.md.
        assert len(TOOL_LIST) == 31

    def test_expected_names_present(self):
        names = set(TOOL_REGISTRY.keys())
        assert names == EXPECTED_TOOL_NAMES

    def test_tool_list_matches_namespace_mapping(self):
        flat: list[str] = []
        for tools in TOOL_NAMESPACES.values():
            flat.extend(tools)
        assert set(flat) == EXPECTED_TOOL_NAMES

    def test_namespace_set_is_six(self):
        # Derived assertion: exactly six namespaces.  Adds `checkpoint`
        # alongside the iter-1 five (sdlc/brc/phase/progress/task).
        assert set(TOOL_NAMESPACES.keys()) == EXPECTED_NAMESPACES

    def test_iter2_tools_land_in_correct_namespace(self):
        """Each iter-2 verb must live under the namespace the plan
        assigns it.  Catches a tool silently landing in the wrong
        namespace (e.g. ``mcp__brc__query_status``)."""
        expected_ns = {
            "mcp__sdlc__show_contract": "sdlc",
            "mcp__sdlc__verify_criterion": "sdlc",
            "mcp__task__add_commit": "task",
            "mcp__task__update_notes": "task",
            "mcp__task__mark_gap": "task",
            "mcp__phase__complete_phase": "phase",
            "mcp__progress__overseer_alert": "progress",
            "mcp__progress__query_status": "progress",
            "mcp__brc__read_peer_artifact": "brc",
            "mcp__checkpoint__list": "checkpoint",
            "mcp__checkpoint__show": "checkpoint",
            "mcp__checkpoint__search": "checkpoint",
        }
        for tool_name, namespace in expected_ns.items():
            assert TOOL_REGISTRY[tool_name].namespace == namespace, (
                f"{tool_name} should live in the '{namespace}' namespace; "
                f"found {TOOL_REGISTRY[tool_name].namespace!r}"
            )


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
        namespace lands (e.g. #1917 added ``checkpoint``)."""
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

    def test_checkpoint_namespace_mentioned_in_nudge(self):
        """Explicit assertion for the new #1917 namespace so the drift
        test flags if someone removes the checkpoint wiring."""
        assert "mcp__checkpoint__" in SYSTEM_PROMPT_NUDGE
