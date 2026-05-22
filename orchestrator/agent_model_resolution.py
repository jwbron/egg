"""Per-agent model resolution for the SDLC pipeline.

This module owns the precedence and classification logic that decides,
for any given agent role, which model the agent should run on and which
upstream the gateway should route its ``/v1/messages`` traffic to.

Precedence (highest first), matching #2769 task-2-3:

1. ``PipelineConfig.agent_models[role]`` — per-pipeline override the
   operator passes on submission. Keys are validated against
   :class:`AgentRole` at construction time.
2. ``repositories.yaml`` ``default_agent_model`` — repository-level
   default surfaced via :func:`config.repo_config.get_default_agent_model`.
3. Built-in ``"opus"`` default — preserves today's Claude-only behaviour.

Classifier (cq-5 mitigation): a model string matching one of the
recognised Claude aliases (``opus``, ``opus[1m]``, ``sonnet``, ``haiku``,
``claude-*``) routes through the Anthropic upstream and the agent's
``--model`` flag is set to that alias verbatim. Any other string is
treated as a LiteLLM-side model name: the upstream is ``"litellm"``,
the upstream-side model name is preserved (the gateway rewrites the
request body before forwarding — see ``_rewrite_upstream_model`` in
``gateway/gateway.py``), and Claude Code is handed the recognised alias
``"opus"`` so its compaction math stays sane.

The resolver is a pure function over its three inputs (role,
PipelineConfig, repo) so callers can use it from spawn, restart, and
test paths without further plumbing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from egg_contracts.agent_roles import AgentRole

# Built-in fallback when neither PipelineConfig.agent_models nor the
# repository-level default_agent_model is set. Matches today's hardcoded
# default in ``orchestrator/consensus_wrapper.py::build_consensus_wrapped_command``.
DEFAULT_AGENT_MODEL = "opus"

# Upstream identifiers used by the gateway's UpstreamRegistry
# (gateway/upstream_registry.py).
UPSTREAM_ANTHROPIC = "anthropic"
UPSTREAM_LITELLM = "litellm"

# Claude alias presented to Claude Code when the resolved model is a
# non-Claude model routed through LiteLLM (cq-5 mitigation).
LITELLM_CLAUDE_CODE_ALIAS = "opus"

# Recognised Claude aliases that route through the Anthropic upstream.
# Exact-match set plus a regex for the version-pinned ``claude-*`` family
# (e.g. ``claude-3-5-sonnet-20241022``, ``claude-opus-4-20250514``).
_CLAUDE_EXACT_ALIASES = frozenset(
    {
        "opus",
        "opus[1m]",
        "sonnet",
        "sonnet[1m]",
        "haiku",
    }
)
_CLAUDE_VERSIONED_RE = re.compile(r"^claude-")


@dataclass(frozen=True)
class AgentModelDecision:
    """Resolved per-agent model decision.

    Attributes:
        claude_code_alias: The string passed to ``python3 -m egg_agent
            --model`` inside the sandbox. For Anthropic-routed models
            this is the resolved model name verbatim. For LiteLLM-routed
            models this is always :data:`LITELLM_CLAUDE_CODE_ALIAS` (cq-5
            mitigation) so Claude Code's compaction heuristics stay
            calibrated against a known Claude family.
        upstream: One of :data:`UPSTREAM_ANTHROPIC` or
            :data:`UPSTREAM_LITELLM`. The gateway's UpstreamRegistry
            keys per-request ``httpx.Client`` + credential by this name.
        upstream_model: The upstream-side model name to rewrite the
            request body's ``model`` field to (gateway-side
            ``_rewrite_upstream_model``). ``None`` on the Anthropic path
            — the body is forwarded byte-for-byte unchanged.
    """

    claude_code_alias: str
    upstream: str
    upstream_model: str | None


def _is_claude_alias(model: str) -> bool:  # noqa: EGG201 - docstring example shows versioned model ID format
    """Return True when *model* is a recognised Claude family alias.

    Matches the explicit aliases the Claude Code harness understands
    (``opus``, ``opus[1m]``, ``sonnet``, ``sonnet[1m]``, ``haiku``)
    plus the versioned ``claude-*`` family (e.g.
    ``claude-3-5-sonnet-20241022``). Used by the classifier in
    :func:`resolve_agent_model` to pick the upstream.
    """
    if model in _CLAUDE_EXACT_ALIASES:
        return True
    return bool(_CLAUDE_VERSIONED_RE.match(model))


def classify_model(model: str) -> AgentModelDecision:
    """Classify a raw model string into an :class:`AgentModelDecision`.

    Separated from :func:`resolve_agent_model` so callers that already
    hold a resolved model string can reuse the classifier without
    re-running precedence resolution.
    """
    if _is_claude_alias(model):
        return AgentModelDecision(
            claude_code_alias=model,
            upstream=UPSTREAM_ANTHROPIC,
            upstream_model=None,
        )
    return AgentModelDecision(
        claude_code_alias=LITELLM_CLAUDE_CODE_ALIAS,
        upstream=UPSTREAM_LITELLM,
        upstream_model=model,
    )


def resolve_agent_model(
    role: AgentRole | str,
    pipeline_config: object | None,
    repo: str | None,
) -> AgentModelDecision:
    """Resolve the model decision for *role* per the precedence rules.

    Args:
        role: The :class:`AgentRole` (or its raw value) being spawned.
        pipeline_config: A ``PipelineConfig`` instance (typed loosely as
            ``object`` to avoid an import cycle with ``orchestrator.models``).
            ``None`` is treated as an empty config — the resolver falls
            through to the repo-level default and then the built-in.
        repo: Repository in ``owner/repo`` format, or ``None`` when the
            caller has no repo context. Used to look up
            ``default_agent_model`` from ``repositories.yaml``.

    Returns:
        An :class:`AgentModelDecision` with the Claude-Code-facing alias,
        the chosen upstream name, and the upstream-side model name (or
        ``None`` on the Anthropic path).
    """
    role_value = role.value if isinstance(role, AgentRole) else role

    # Tier 1: per-pipeline override.
    if pipeline_config is not None:
        agent_models = getattr(pipeline_config, "agent_models", None)
        if isinstance(agent_models, dict):
            override = agent_models.get(role_value)
            if override:
                return classify_model(override)

    # Tier 2: repository-level default.
    if repo:
        # Lazy import: ``config.repo_config`` reads from disk on first
        # call and we want to defer that until a caller actually needs
        # repo-level resolution. Also avoids pulling the config module
        # into every test that exercises the classifier directly.
        #
        # Dual-import with fallback: the orchestrator Dockerfile flattens
        # ``config/repo_config.py`` to ``/app/repo_config.py`` at the top
        # level (``orchestrator/Dockerfile:66``), so the production
        # container has no ``config/`` package — only the source-tree
        # layout does. This mirrors the established pattern at
        # ``shared/egg_restrictions/patterns.py:913-916`` and
        # ``orchestrator/routes/signals.py:961-964``.
        try:
            from config.repo_config import get_default_agent_model
        except ImportError:
            from repo_config import (  # type: ignore[import-not-found, no-redef]
                get_default_agent_model,
            )

        repo_default = get_default_agent_model(repo)
        if repo_default:
            return classify_model(repo_default)

    # Tier 3: built-in default.
    return classify_model(DEFAULT_AGENT_MODEL)


__all__ = [
    "AgentModelDecision",
    "DEFAULT_AGENT_MODEL",
    "LITELLM_CLAUDE_CODE_ALIAS",
    "UPSTREAM_ANTHROPIC",
    "UPSTREAM_LITELLM",
    "classify_model",
    "resolve_agent_model",
]
