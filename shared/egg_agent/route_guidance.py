"""Route-conditional working-style guidance for non-Claude model routes (#3175).

On the LiteLLM route the dominant cost driver is per-turn re-billing of
large agentic contexts: every SDK turn re-sends the whole conversation,
and prompt-cached tokens are discounted, not free — so cost scales with
turns × context-size. Measured against the Claude baseline, open models
also take 3–5× more, smaller steps for comparable events (#3175), which
multiplies the turn factor.

This module renders an advisory system-prompt addendum that
``egg_agent.client.run_agent_async`` appends only when the session is
routed through LiteLLM — signalled by ``ANTHROPIC_CUSTOM_MODEL_OPTION``,
the same route detection the DDG web-tool fallback uses (#2856). The env
var is fixed for the pod's lifetime and the addendum is a pure constant,
so the rendered system prompt is stable per session and cannot break the
cacheable prompt prefix. Claude-route sessions are untouched.

The guidance is working-style steering, not budgets: it removes wasted
round trips (one tool call per turn, unfiltered output dumps, bulk reads
held in the parent session) without truncating reviews or skipping
verification steps. ``EGG_ROUTE_PROMPT_GUIDANCE=false`` is the rollback
escape hatch.
"""

from __future__ import annotations

import os

# The addendum appended to the system prompt on LiteLLM routes. Keep it
# a pure constant (no interpolation) — per-session stability is what
# keeps the cacheable prefix intact across the session's turns. Names
# only the ``general-purpose`` subagent: the sandbox runtime registers
# no other AgentDefinition (no ``agents=`` on ClaudeAgentOptions, no
# ``.claude/agents/*.md``), and naming an unknown type burns a turn on
# the retry (see ``_EXPLORATION_SUBAGENT_GUIDANCE`` in
# ``orchestrator/routes/pipelines.py``).
LITELLM_ROUTE_GUIDANCE: str = """\
## Working style on this model route (advisory)

This session is routed to a model whose billing re-sends the whole
conversation every turn — cached tokens are discounted, not free — so
cost scales with turns × context size. Do the same work in fewer, denser
steps. None of this changes WHAT you must do, only how many round trips
it takes:

1. **Batch independent tool calls in one turn.** When several
   reads/greps/commands do not depend on each other's results, issue
   them together instead of one per turn.
2. **Filter command output instead of dumping it.** Pipe long output
   through `grep`/`head`/`tail` (e.g. `... 2>&1 | tail -50` for a test
   run), page large files with `offset`/`limit`, and bound searches with
   `head_limit`. Anything pasted into the conversation is re-billed on
   every later turn.
3. **Delegate bulk exploration to a subagent.** For wide file surveys or
   deep multi-file investigation, use the Agent tool
   (`subagent_type: general-purpose`) and ask for a focused summary with
   `file:line` citations — the read bulk stays out of this session's
   per-turn cost.

These are working-style preferences, not budgets: never skip
verification, shorten a review, or drop required steps to save turns.
"""

# Values that disable the addendum, mirroring the EGG_MCP_TOOLS /
# EGG_TOOL_OUTPUT_CAP kill-switch parsing.
_DISABLED_VALUES = ("false", "0", "no", "off")


def is_route_guidance_disabled() -> bool:
    """True when the operator disabled the addendum via env."""
    return os.environ.get("EGG_ROUTE_PROMPT_GUIDANCE", "").strip().lower() in _DISABLED_VALUES


def route_guidance_addendum() -> str | None:
    """Return the addendum for this session's route, or ``None``.

    Non-``None`` only when the pod env signals the LiteLLM route
    (``ANTHROPIC_CUSTOM_MODEL_OPTION`` set — exported by
    ``orchestrator.agent_model_resolution.AgentModelDecision.env_vars``)
    and the operator has not set the kill switch.
    """
    if is_route_guidance_disabled():
        return None
    if not os.environ.get("ANTHROPIC_CUSTOM_MODEL_OPTION", "").strip():
        return None
    return LITELLM_ROUTE_GUIDANCE
