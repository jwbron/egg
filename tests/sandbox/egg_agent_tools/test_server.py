"""Tests for egg_agent_tools.server (factory + system prompt nudge).

Covers:
- build_sandbox_mcp_server registers the expected tools (45: the 18
  iteration-1 verbs plus the 12 iteration-2 additions landed in #1917,
  minus 2 wait verbs removed in #2211, plus
  ``mcp__brc__resolve_obligation`` added in #2338, plus
  ``check_file_restriction`` / ``report_impasse`` in #2529, minus the
  3 checkpoint verbs removed in #2993, plus the 17 Atlassian-gateway
  verbs in #2994, across the sdlc, brc, phase, progress, task,
  confluence, and jira namespaces).
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

# Iteration-1 verbs (16 — original 18 minus the 2 wait verbs removed
# in #2211).  Kept in its own set for documentation so the reader can
# see the #1917 additions clearly.
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

# Iteration-2 additions (9, #1917): 2 sdlc, 3 task, 1 phase, 2
# progress, 1 brc.  The anchor trio and directed peer send/poll verbs
# were deferred per decisions 2 and 14.
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

# #2994 — Atlassian gateway routes exposed as MCP servers for
# discoverability.  Two net-new namespaces (``confluence``/``jira``)
# mirroring the ``sandbox/scripts/{confluence,jira}`` bash wrappers
# one-for-one; all gateway-backed, all ``cli_command=None`` (their CLI
# analog is a bash wrapper, not an ``egg-*`` argparse tree).
_ATLASSIAN_TOOL_NAMES = {
    # confluence (8)
    "mcp__confluence__page_get",
    "mcp__confluence__page_descendants",
    "mcp__confluence__page_footer_comments",
    "mcp__confluence__page_inline_comments",
    "mcp__confluence__space_pages",
    "mcp__confluence__space_list",
    "mcp__confluence__search",
    "mcp__confluence__execute",
    # jira (9)
    "mcp__jira__ticket_get",
    "mcp__jira__ticket_comments",
    "mcp__jira__ticket_remotelinks",
    "mcp__jira__search",
    "mcp__jira__ticket_create",
    "mcp__jira__ticket_edit",
    "mcp__jira__ticket_comment_add",
    "mcp__jira__link_create",
    "mcp__jira__execute",
}

EXPECTED_TOOL_NAMES = (
    _ITER1_TOOL_NAMES | _ITER2_TOOL_NAMES | _POST_ITER2_TOOL_NAMES | _ATLASSIAN_TOOL_NAMES
)

EXPECTED_NAMESPACES = {
    "sdlc",
    "brc",
    "phase",
    "progress",
    "task",
    "confluence",
    "jira",
}


class TestToolRegistry:
    def test_tool_count_registered(self):
        # 18 iter-1 + 12 iter-2 (#1917) = 30, then -2 in #2211
        # (``wait_for_event`` + ``wait_loop`` removed — agents now use
        # the ``egg-orch message wait`` / ``wait-loop`` Bash CLI for
        # blocking waits per the transport-mismatch carve-out) = 28,
        # then +1 in #2338 (``mcp__brc__resolve_obligation``) = 29,
        # then +2 in #2529 (``check_file_restriction`` +
        # ``report_impasse`` — runtime escape hatch) = 31,
        # then -3 in #2993 (checkpoint subsystem removed) = 28,
        # then +17 in #2994 (8 confluence + 9 jira Atlassian-gateway
        # verbs) = 45.
        # Derived assertion: trips when a future iteration drifts the
        # count without updating the prose verb-counts in
        # docs/reference/agent-tools.md.
        assert len(TOOL_LIST) == 45

    def test_expected_names_present(self):
        names = set(TOOL_REGISTRY.keys())
        assert names == EXPECTED_TOOL_NAMES

    def test_tool_list_matches_namespace_mapping(self):
        flat: list[str] = []
        for tools in TOOL_NAMESPACES.values():
            flat.extend(tools)
        assert set(flat) == EXPECTED_TOOL_NAMES

    def test_namespace_set(self):
        # Derived assertion: exactly seven namespaces.  The iter-1 five
        # (sdlc/brc/phase/progress/task) plus the two Atlassian-gateway
        # namespaces `confluence`/`jira` (#2994).  The `checkpoint`
        # namespace was removed in #2993.
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
