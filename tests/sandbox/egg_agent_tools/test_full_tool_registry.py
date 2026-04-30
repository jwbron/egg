"""Integration: load TOOL_LIST via the real SDK factory.

Covers TASK-6-2 of #1917:

- Every tool in ``TOOL_LIST`` can be handed to
  ``claude_agent_sdk.create_sdk_mcp_server`` without registration errors.
- Every tool has a non-empty description (the SDK will happily render
  an empty string as an agent-visible tool — that's a footgun).
- The four completion/mutation verbs (``task_complete``,
  ``phase__complete_phase``, ``task__add_commit``,
  ``sdlc__verify_criterion``) explicitly name their state-machine
  effect so an agent picks the right verb without re-deriving the
  taxonomy (same spirit as #1944).

The SDK may be absent in CI — skip cleanly when it is.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools import TOOL_LIST  # noqa: E402
from egg_agent_tools.tools import TOOL_REGISTRY  # noqa: E402


def _require_sdk():
    try:
        from claude_agent_sdk import create_sdk_mcp_server  # noqa: F401
    except ImportError:
        pytest.skip("claude_agent_sdk not installed in CI")


class TestTOOLLISTLoadsCleanlyViaCreateSdkMcpServer:
    def test_create_sdk_mcp_server_accepts_tool_list(self):
        _require_sdk()
        from claude_agent_sdk import create_sdk_mcp_server

        server = create_sdk_mcp_server(
            name="egg-test-registry",
            version="0.0.1",
            tools=TOOL_LIST,
        )
        assert server is not None

    def test_every_tool_has_non_empty_description(self):
        """Empty descriptions would render an anonymous tool to the
        agent — always a bug."""
        offenders: list[str] = []
        for tool in TOOL_LIST:
            desc = getattr(tool, "description", "") or ""
            if not desc.strip():
                offenders.append(getattr(tool, "name", "<unknown>"))
        assert not offenders, (
            "Tools with empty descriptions (add one in the @tool decorator):\n"
            + "\n".join(f"  - {n}" for n in offenders)
        )


# The SDK's SdkMcpTool carries the short verb name (``propose``) not
# the mcp__namespace__ form.  The ToolRegistration sibling object is
# what carries the namespace prefix, so we look up via sdk_tool name →
# registration.
def _registration_for_sdk_tool(tool) -> object:
    short_name = getattr(tool, "name", None)
    assert short_name is not None
    for reg in TOOL_REGISTRY.values():
        if getattr(reg.sdk_tool, "name", None) == short_name:
            return reg
    raise LookupError(f"no ToolRegistration found for sdk_tool name {short_name!r}")


class TestStateMachineEffectNamedInDescription:
    """Completion / mutation verbs should explicitly mention their
    state-machine effect so agents pick the right verb without guessing.

    Targeted at:
    - ``mcp__task__complete`` — transitions task status to 'complete'
    - ``mcp__phase__complete_phase`` — transitions phase status
    - ``mcp__task__add_commit`` — sets commit field; does NOT mark complete
    - ``mcp__sdlc__verify_criterion`` — flips verified to True
    """

    # (registry name, required substring in description — matched
    # case-insensitively so prose can vary).
    _STATE_VERBS = (
        ("mcp__task__complete", "state-machine effect"),
        ("mcp__phase__complete_phase", "state-machine effect"),
        ("mcp__task__add_commit", "state-machine effect"),
        ("mcp__sdlc__verify_criterion", "state-machine effect"),
    )

    @pytest.mark.parametrize("tool_name,required", _STATE_VERBS)
    def test_state_machine_effect_named(self, tool_name: str, required: str):
        reg = TOOL_REGISTRY[tool_name]
        desc = getattr(reg.sdk_tool, "description", "") or ""
        assert required.lower() in desc.lower(), (
            f"{tool_name} description lacks '{required}' phrase; iter-2 "
            f"plan (and spirit of #1944) requires naming the state-machine "
            f"effect explicitly so agents self-select.\nGot: {desc!r}"
        )

    def test_add_commit_explicitly_does_not_mark_complete(self):
        """Extra-strong assertion for ``add_commit``: the description
        must tell the agent the tool does NOT mark the task complete.
        Without this, agents routinely skip ``task__complete``."""
        desc = (TOOL_REGISTRY["mcp__task__add_commit"].sdk_tool.description or "").lower()
        assert "not mark the task complete" in desc or "does not mark" in desc, (
            "add_commit description must explicitly say it does NOT mark the task complete."
        )

    def test_verify_criterion_names_reviewer_role(self):
        """REVIEWER-only; agents must see that in the description."""
        desc = TOOL_REGISTRY["mcp__sdlc__verify_criterion"].sdk_tool.description or ""
        assert "REVIEWER" in desc or "reviewer" in desc, (
            "verify_criterion description must name the REVIEWER role requirement (decision-7)."
        )


class TestToolCountAndNamespaces:
    """Derived assertions locked here so the integration suite trips on
    silent drift.

    Count is **29** — ``mcp__brc__wait_for_event`` and
    ``mcp__brc__wait_loop`` were removed in #2211 (long-poll waits don't
    fit the in-process SDK MCP transport's ~60 s tool-call cap; agents
    now use ``egg-orch message wait`` / ``wait-loop`` via Bash).
    ``mcp__brc__resolve_obligation`` was added in #2338 to mark a
    conditional-ACK obligation satisfied in-cycle.
    """

    EXPECTED_TOOL_COUNT = 29

    def test_tool_count(self):
        assert len(TOOL_LIST) == self.EXPECTED_TOOL_COUNT

    def test_registration_count(self):
        assert len(TOOL_REGISTRY) == self.EXPECTED_TOOL_COUNT

    def test_tool_list_names_unique(self):
        """Catches a namespace-prefix collision (two registrations
        ending up with the same SdkMcpTool name)."""
        short_names = [getattr(t, "name", "") for t in TOOL_LIST]
        # Verb names may repeat across namespaces (e.g. two `complete`
        # tools) — the full mcp__<ns>__<verb> path must be unique.
        full_names = [reg.name for reg in TOOL_REGISTRY.values()]
        assert len(set(full_names)) == len(full_names), (
            "TOOL_REGISTRY keys collided — two registrations share a mcp__<ns>__ path."
        )
        assert len(short_names) == self.EXPECTED_TOOL_COUNT, (
            f"TOOL_LIST length != {self.EXPECTED_TOOL_COUNT}"
        )
