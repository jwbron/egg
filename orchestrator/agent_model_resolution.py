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

Classifier: a model string matching one of the recognised Claude
aliases (``opus``, ``opus[1m]``, ``sonnet``, ``sonnet[1m]``, ``haiku``,
``claude-*``) routes through the Anthropic upstream — the agent's
``--model`` flag is set to that alias verbatim and the gateway
forwards the request body byte-for-byte. Any other string is treated
as a LiteLLM-side model name (#2832): the upstream is ``"litellm"``,
the agent is spawned with ``--model <upstream>[1m]`` and the
``ANTHROPIC_CUSTOM_MODEL_OPTION`` / ``…_OPTION_NAME`` env vars set so
Claude Code registers the custom model with a 1M-context-window
compaction profile. The gateway no longer rewrites the request body;
Claude Code strips the ``[1m]`` suffix before sending, and LiteLLM
matches the resulting bare name against its ``model_list``.

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

# Suffix Claude Code uses to opt a custom (non-Claude) model into 1M-context
# compaction math. The suffix is stripped before send, so LiteLLM keys on the
# bare model name; the orchestrator also configures a ``<name>[1m]`` alias
# in ``litellm-configmap.yaml`` as a defensive guard against the documented
# startup-probe leak path.
_CONTEXT_1M_SUFFIX = "[1m]"

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
            this is the resolved Claude alias verbatim. For LiteLLM-routed
            models this is the upstream-side model name with the ``[1m]``
            context-window-opt-in suffix appended (e.g.
            ``"qwen3-coder-30b[1m]"``); Claude Code strips the suffix
            before the request hits the wire.
        upstream: One of :data:`UPSTREAM_ANTHROPIC` or
            :data:`UPSTREAM_LITELLM`. The gateway's UpstreamRegistry
            keys per-request ``httpx.Client`` + credential by this name.
        upstream_model: The bare upstream-side model name (no ``[1m]``
            suffix), recorded on ``Session.upstream_model`` as audit
            metadata. ``None`` on the Anthropic path. The gateway does
            not rewrite the request body — Claude Code already sends the
            bare name once the ``[1m]`` suffix is stripped — so this is
            now an audit field rather than a routing input.
    """

    claude_code_alias: str
    upstream: str
    upstream_model: str | None

    def env_vars(self) -> dict[str, str]:
        """Env vars to inject into the agent's sandbox for this decision.

        On the LiteLLM path Claude Code needs to be told the custom
        model exists (``ANTHROPIC_CUSTOM_MODEL_OPTION``) and what its
        wire-name should be (``…_OPTION_NAME``) — without these,
        compaction math falls back to the 200k Claude default and the
        agent throws away >80% of a 1M-window upstream's capacity. The
        Anthropic path returns an empty dict so default-Claude spawns
        carry no extra env (the existing pre-#2832 wire shape).
        """
        if self.upstream == UPSTREAM_ANTHROPIC or self.upstream_model is None:
            return {}
        return {
            "ANTHROPIC_CUSTOM_MODEL_OPTION": self.claude_code_alias,
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": self.upstream_model,
        }


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
    # LiteLLM path (#2832): the operator may pass the bare upstream name
    # (e.g. ``qwen3-coder-30b``) or pre-suffix it (``qwen3-coder-30b[1m]``).
    # Normalise so ``upstream_model`` is always the bare name LiteLLM keys
    # on and ``claude_code_alias`` always carries the suffix Claude Code
    # needs to opt into 1M-context compaction math.
    bare = model.removesuffix(_CONTEXT_1M_SUFFIX) if model.endswith(_CONTEXT_1M_SUFFIX) else model
    return AgentModelDecision(
        claude_code_alias=f"{bare}{_CONTEXT_1M_SUFFIX}",
        upstream=UPSTREAM_LITELLM,
        upstream_model=bare,
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
    "UPSTREAM_ANTHROPIC",
    "UPSTREAM_LITELLM",
    "classify_model",
    "resolve_agent_model",
]
