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
3. Built-in default — ``"opus"`` for all roles. Fable has been
   disabled, so ``FABLE_DEFAULT_MODEL`` is now ``"opus"`` and the
   refine/plan phase roles (:data:`_FABLE_DEFAULT_ROLES`) unify on opus
   alongside everything else.

Classifier: a model string matching one of the recognised Claude
aliases (``opus``, ``opus[1m]``, ``sonnet``, ``sonnet[1m]``, ``haiku``,
``fable``, ``fable[1m]``, ``claude-*``) routes through the Anthropic
upstream — the agent's
``--model`` flag is set to that alias verbatim and the gateway
forwards the request body byte-for-byte. Any other string is treated
as a LiteLLM-side model name (#2832): the upstream is ``"litellm"``,
the agent is spawned with ``--model <upstream>[1m]`` and the
``ANTHROPIC_CUSTOM_MODEL_OPTION`` / ``…_OPTION_NAME`` env vars set so
Claude Code registers the custom model with a 1M-context-window
compaction profile. Models whose real context window is below 1M
(``_SUB_1M_CONTEXT_MODELS`` — e.g. Kimi 256K) are the
exception: they get the bare ``<upstream>`` alias so Claude Code uses
its 200K default and compacts before their true limit, since Claude
Code has no sub-1M custom-model profile (#2987). The gateway no longer
rewrites the request body; Claude Code strips the ``[1m]`` suffix
before sending, and LiteLLM matches the resulting bare name against
its ``model_list``.

The resolver is a pure function over its three inputs (role,
PipelineConfig, repo) so callers can use it from spawn, restart, and
test paths without further plumbing.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from egg_contracts.agent_roles import EGG_REPO, AgentRole, get_roles_for_phase

logger = logging.getLogger(__name__)

# Built-in fallback when neither PipelineConfig.agent_models nor the
# repository-level default_agent_model is set. Matches today's hardcoded
# default in ``orchestrator/consensus_wrapper.py::build_consensus_wrapped_command``.
DEFAULT_AGENT_MODEL = "opus"

# Built-in default for the refine and plan phases. Fable has been
# disabled, so the drafting-heavy upstream phases (analysis, slice DAG,
# contract shape) now run on opus alongside implement and downstream
# phases. Both pipeline-level ``agent_models`` and the repo-level
# ``default_agent_model`` still override this (precedence unchanged).
FABLE_DEFAULT_MODEL = "opus"

# Effort level pinned on fable-routed agents (threaded to ``--effort``
# on the ``python3 -m egg_agent`` command). Claude Code's built-in
# default for fable is currently also "high", but that table is whatever
# the image's installed Claude Code build says at build time — pinning
# keeps token spend deliberate if a future stable release changes the
# default (the way opus moved xhigh→high across 4.7→4.8). Every other
# model gets ``effort=None`` and keeps inheriting Claude Code's
# per-model default, so opus agents are byte-identical to today.
#
# The pin is keyed on the exact ``fable`` / ``fable[1m]`` aliases, NOT on
# the versioned ``claude-fable-*`` family. An operator who configures
# ``agent_models[role] = "claude-fable-5"`` matches the generic
# ``^claude-`` regex in ``_is_claude_alias`` and gets ``effort=None`` —
# i.e. the versioned name escapes the drift defense the alias was pinned
# to provide. Use the bare ``fable`` alias (or add the versioned name to
# ``_FABLE_ALIASES``) if you want the pin to apply.
FABLE_EFFORT = "high"
_FABLE_ALIASES = frozenset({"fable", "fable[1m]"})

# Role values that pick up FABLE_DEFAULT_MODEL at tier 3. Derived from
# the phase→role tables so new refine/plan roles inherit the default
# without a parallel list here. ``repo=EGG_REPO`` includes the egg-only
# reviewers (e.g. reviewer_agent_design) — membership is keyed by role
# only, and roles that never spawn for a repo are simply never resolved.
_FABLE_DEFAULT_ROLES: frozenset[str] = frozenset(
    role.value
    for phase in ("refine", "plan")
    for role in get_roles_for_phase(phase, include_reviewers=True, repo=EGG_REPO)
)

# Overseer decision-tier models (#2270 §1, folds #2813). The overseer's work
# is split across three cost/capability tiers instead of the single bespoke
# ``overseer_decision_maker_model`` field (now deprecated):
#
#   - ``classify``    (haiku)  — high-volume, single-shot Haiku-tier
#     classification (``overseer/classifier.py``).
#   - ``routine``     (sonnet) — routine corrective decisions
#     (``overseer/decision_maker.py``).
#   - ``adversarial`` (opus)   — high-stakes / adversarial adjudication. The
#     §1 headline fix: this decision tier runs on Opus (the fleet standard),
#     NOT Sonnet — Sonnet mis-classifies the overseer's own bootstrap as a
#     prompt-injection attack and crash-loops.
#
# This is the tier→default-model table. ``resolve_overseer_model`` turns a tier
# into a full :class:`AgentModelDecision` through the SAME per-agent resolver
# every other agent uses, so the spawn path gets the Claude-Code alias +
# upstream identically — no bespoke field. The classify / routine tiers are
# fixed cheap defaults; the operator override surface is the ``overseer`` entry
# in ``agent_models`` (and, for back-compat, the deprecated field), which apply
# to the adversarial/decision tier.
OVERSEER_TIER_MODELS: dict[str, str] = {
    "classify": "haiku",
    "routine": "sonnet",
    "adversarial": "opus",
}

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

# Non-Claude models whose REAL upstream context window is below 1M. Claude Code
# offers a custom model (registered via ``ANTHROPIC_CUSTOM_MODEL_OPTION``) only
# TWO compaction profiles: the 1M window — opted into by the ``[1m]`` suffix,
# whose qualifier in Claude Code is literally ``/\[1m\]/i`` — or its 200K
# default. There is no arbitrary ``[256k]`` size suffix, and
# ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` only takes effect under ``DISABLE_COMPACT``
# (which we never set). So a model whose true window sits between 200K and 1M
# cannot be expressed exactly; for these we WITHHOLD ``[1m]`` and take the 200K
# default, which auto-compacts safely below their real limit. Appending ``[1m]``
# instead would make Claude Code treat them as 1M and defer compaction to ~1M,
# overflowing the upstream mid-turn. Keyed by bare upstream name; the value
# documents the real window (only membership is used). Add a model here when its
# window is <1M. See #2987.
_SUB_1M_CONTEXT_MODELS: dict[str, int] = {
    "kimi-k2.7-code": 262_144,
}

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
        "fable",
        "fable[1m]",
    }
)
_CLAUDE_VERSIONED_RE = re.compile(r"^claude-")

# Real backend context-window resolution + reseed threshold (#3200, AC-3
# foundation). The reseed gate (slice-6) keys its 80% margin on the TRUE
# upstream window, NOT the ``[1m]`` alias: the alias only selects Claude Code's
# compaction PROFILE. Computing ``0.80 * 1_000_000`` for a model whose real
# backend is, say, 128K defers the reseed past the real limit and overflows the
# upstream mid-turn — the "mis-trigger bug" the issue calls out.

# Window attributed to every recognised Claude family alias. Per the ratified
# plan (task-2-1), Claude aliases resolve to the 1M window; the ``[1m]`` suffix
# is not load-bearing for Claude routes here.
_CLAUDE_BACKEND_WINDOW = 1_000_000

# The 200K compaction profile — the fallback real window for a non-Claude model
# that is not in the sub-1M registry, and the ceiling for the conservative
# unknown-model resolution (:func:`_conservative_unknown_window`).
_PROFILE_200K_WINDOW = 200_000

# Reseed-threshold knobs (task-2-2). The floor is an initial, operator-tunable
# knob (NOT derived — a context-rot / cost ceiling); the margin reseeds
# deterministically below Claude Code's ~95% lossy auto-compaction wall. Both
# are read live inside :func:`reseed_threshold` so an operator can override them
# (reassign / monkeypatch) without re-importing.
RESEED_THRESHOLD_FLOOR = 400_000
RESEED_THRESHOLD_MARGIN = 0.80

# Context guardrails for agent spawns (#3175). Every SDK turn re-sends
# the whole conversation, and cached tokens bill at a
# discounted-but-nonzero rate on every route (~10% of the input price
# on Anthropic; a comparable blended rate on LiteLLM upstreams), so one
# careless tool call that dumps tens of kilotokens (verbose ``pytest
# -v``, a whole-megafile Read, an unbounded MCP result) is re-billed on
# every subsequent turn for the life of the session. These caps bound
# the size a single tool result can park in the conversation. They are
# guardrails, not constraints: thresholds are sized so normal work
# never hits them, and every cap carries its own remedy — Claude Code
# spills oversized Bash and MCP results to a file the agent can
# Read/grep, and the Read cap's deny message points at
# ``offset``/``limit`` paging plus an agent-writable override file
# (``shared/egg_agent/tool_output_cap.py``).
#
# Tuple shape: (sandbox env var, orchestrator-side override env var,
# default). Operators override a value by setting the override variable
# on the orchestrator; setting it to the empty string omits that
# guardrail from the injection entirely. The override names are
# deliberately distinct from the sandbox-side names so a value in the
# orchestrator's own environment (e.g. a dev running it under Claude
# Code) is never forwarded by accident.
#
# The Bash and MCP caps apply to EVERY route — the dump arithmetic is
# route-independent; only the multiplier differs:
# - ``BASH_MAX_OUTPUT_LENGTH`` (characters): Claude Code's built-in
#   post-hoc Bash truncation — oversized output is saved to a session
#   file and the agent gets the path plus a preview. 20k chars ≈ 5k
#   tokens per result.
# - ``MAX_MCP_OUTPUT_TOKENS`` (tokens): Claude Code's MCP result cap
#   (built-in default 25k); excess is persisted to disk and replaced
#   with a file reference.
_CONTEXT_GUARDRAILS: tuple[tuple[str, str, str], ...] = (
    ("BASH_MAX_OUTPUT_LENGTH", "EGG_AGENT_BASH_MAX_OUTPUT_LENGTH", "20000"),
    ("MAX_MCP_OUTPUT_TOKENS", "EGG_AGENT_MAX_MCP_OUTPUT_TOKENS", "15000"),
)

# LiteLLM-only extra: a tighter Read cap. The predictive whole-file-Read
# deny in ``tool_output_cap.py`` already guards every route at its
# built-in 256 KiB default; on the LiteLLM path — where turn counts run
# 3-5x the Claude baseline, multiplying the re-bill — this pushes big
# files toward paging earlier (64 KiB ≈ 16k tokens). The Claude route
# deliberately keeps the 256 KiB default rather than getting this var.
_LITELLM_EXTRA_GUARDRAILS: tuple[tuple[str, str, str], ...] = (
    ("EGG_READ_CAP_BYTES", "EGG_LITELLM_READ_CAP_BYTES", str(64 * 1024)),
)


def context_guardrail_env(upstream: str) -> dict[str, str]:
    """Context-guardrail env vars for an agent spawn (#3175).

    Returns the route-independent Bash/MCP caps for every *upstream*,
    plus the tighter Read cap on the LiteLLM path. Reads the operator
    overrides from the orchestrator's environment on every call
    (spawn-frequency, so no caching) and validates each as a positive
    integer — an unparseable or non-positive override logs a warning
    and falls back to the built-in default rather than forwarding
    garbage the sandbox would misread. An empty-string override opts
    that guardrail out entirely.
    """
    guardrails = _CONTEXT_GUARDRAILS
    if upstream == UPSTREAM_LITELLM:
        guardrails = guardrails + _LITELLM_EXTRA_GUARDRAILS
    env: dict[str, str] = {}
    for target, override, default in guardrails:
        raw = os.environ.get(override)
        if raw is None:
            env[target] = default
            continue
        value = raw.strip()
        if not value:
            # Explicit per-guardrail opt-out: don't inject the var, so the
            # sandbox keeps Claude Code's (or tool_output_cap's) own default.
            continue
        try:
            if int(value) <= 0:
                raise ValueError(value)
        except ValueError:
            logger.warning(
                "Ignoring %s=%r: expected a positive integer (or empty to "
                "opt out); falling back to the default %s=%s. See #3175.",
                override,
                raw,
                target,
                default,
            )
            value = default
        env[target] = value
    return env


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
        effort: Effort level passed to the agent command as ``--effort``,
            or ``None`` to omit the flag and inherit Claude Code's
            per-model default. Currently :data:`FABLE_EFFORT` for
            fable-routed decisions and ``None`` for everything else.
    """

    claude_code_alias: str
    upstream: str
    upstream_model: str | None
    effort: str | None = None

    def env_vars(self) -> dict[str, str]:
        """Env vars to inject into the agent's sandbox for this decision.

        On the LiteLLM path Claude Code needs to be told the custom
        model exists (``ANTHROPIC_CUSTOM_MODEL_OPTION``) and what its
        wire-name should be (``…_OPTION_NAME``) — without these,
        compaction math falls back to the 200k Claude default and the
        agent throws away >80% of a 1M-window upstream's capacity. We
        also set ``ANTHROPIC_AUTH_METHOD=api_key`` to mark the path as
        api-key auth so config validation / startup logging don't
        demand an Anthropic OAuth token — the actual OAuth-token skip
        in the entrypoint's ``setup_anthropic_api()`` is driven by
        ``ANTHROPIC_CUSTOM_MODEL_OPTION``, not this var. LiteLLM-routed
        agents talk to the gateway, not anthropic.com, and the gateway
        injects its own credentials at proxy time. The Anthropic path
        carries none of these registration vars, so default-Claude
        spawns keep the pre-#2832 wire shape.

        We also redirect the other two resolution paths that would
        otherwise emit a Claude model name the LiteLLM proxy can't
        resolve — each 400s with ``ProxyModelNotFoundError`` on the
        LiteLLM path, the exact symptom this covers:

        - ``CLAUDE_CODE_SUBAGENT_MODEL`` — the model Claude Code uses
          for all Task-tool subagents and agent teams. Generic Task
          subagents inherit the main agent's model, but the **built-in**
          subagents (``Explore`` etc.) hardcode a versioned ``haiku``
          model in their ``model`` frontmatter; this var overrides that
          frontmatter so they route to the configured upstream instead.
          Pinned to the main agent's resolved ``claude_code_alias`` so
          subagents share its compaction profile — the ``[1m]``-suffixed
          1M-window profile for the standard LiteLLM path, the bare
          alias (Claude Code's 200K default) for ``_SUB_1M_CONTEXT_MODELS``.
          Mirrors the host ``cllm`` wrapper's
          ``CLAUDE_CODE_SUBAGENT_MODEL`` export.
        - ``ANTHROPIC_DEFAULT_HAIKU_MODEL`` (and its deprecated alias
          ``ANTHROPIC_SMALL_FAST_MODEL``, set for older Claude Code
          builds where the rename hasn't landed) — the model the
          ``haiku`` alias and Claude Code's background / "small-fast"
          helper calls resolve to. Pinned to the **bare** upstream name
          rather than the ``[1m]`` alias: these vars are documented to
          take a model name and the ``[1m]`` suffix is read per-variable,
          and small/fast helper calls don't need the 1M window.

        Every route additionally gets the context guardrails from
        :func:`context_guardrail_env` (#3175) — per-turn re-billing of
        the full conversation makes a single oversized tool result
        disproportionately expensive on any route, so Bash/MCP result
        sizes are bounded everywhere (with built-in remedies; see the
        guardrail table's comment), and the LiteLLM path adds a tighter
        Read cap on top. The Anthropic path therefore no longer returns
        an empty dict (the pre-#3175 shape): it carries exactly the
        Bash/MCP guardrails and none of the custom-model registration,
        so the Claude *wire* shape is unchanged.
        """
        if self.upstream == UPSTREAM_ANTHROPIC or self.upstream_model is None:
            return context_guardrail_env(self.upstream)
        return {
            "ANTHROPIC_CUSTOM_MODEL_OPTION": self.claude_code_alias,
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": self.upstream_model,
            "ANTHROPIC_AUTH_METHOD": "api_key",
            "CLAUDE_CODE_SUBAGENT_MODEL": self.claude_code_alias,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.upstream_model,
            "ANTHROPIC_SMALL_FAST_MODEL": self.upstream_model,
            **context_guardrail_env(self.upstream),
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
            effort=FABLE_EFFORT if model in _FABLE_ALIASES else None,
        )
    # LiteLLM path (#2832): the operator may pass the bare upstream name
    # (e.g. ``qwen3-coder-30b``) or pre-suffix it (``qwen3-coder-30b[1m]``).
    # Normalise so ``upstream_model`` is always the bare name LiteLLM keys on.
    had_suffix = model.endswith(_CONTEXT_1M_SUFFIX)
    bare = model.removesuffix(_CONTEXT_1M_SUFFIX) if had_suffix else model
    # ``claude_code_alias`` carries the ``[1m]`` suffix so Claude Code opts the
    # custom model into 1M-context compaction math — EXCEPT for models whose
    # real window is below 1M (``_SUB_1M_CONTEXT_MODELS``): those take the bare
    # name so Claude Code uses its 200K default and compacts before their true
    # limit instead of overflowing it. A pre-suffixed sub-1M model (e.g.
    # ``kimi-k2.7-code[1m]``) is normalised back to bare here — the registry
    # is authoritative over an operator's stray suffix.
    if bare in _SUB_1M_CONTEXT_MODELS:
        claude_code_alias = bare
        if had_suffix:
            # Surface the override so an operator who deliberately requested
            # ``[1m]`` for a sub-1M model can see from the logs why it was
            # dropped, rather than chasing a silent compaction discrepancy.
            logger.warning(
                "Ignoring [1m] suffix on sub-1M model %r: %r is in "
                "_SUB_1M_CONTEXT_MODELS (real window <1M), so the bare alias "
                "is used to keep Claude Code on its 200K compaction profile "
                "(appending [1m] would defer compaction toward 1M and "
                "overflow the upstream mid-turn). See #2987.",
                model,
                bare,
            )
    else:
        claude_code_alias = f"{bare}{_CONTEXT_1M_SUFFIX}"
    return AgentModelDecision(
        claude_code_alias=claude_code_alias,
        upstream=UPSTREAM_LITELLM,
        upstream_model=bare,
    )


def _conservative_unknown_window() -> int:
    """Real window for an unregistered / unknown model.

    The 200K compaction profile — Claude Code's default for any model not
    opted into ``[1m]``, so an unregistered backend auto-compacts safely below
    its real limit rather than mis-triggering the reseed gate (deferring the
    reseed past the real window). Matches contract task-2-1 ("200_000 for an
    unregistered non-Claude model").

    Deliberately returns the 200K profile DIRECTLY rather than the ``min`` over
    :data:`_SUB_1M_CONTEXT_MODELS`: an unknown model's true window has no
    logical relationship to whichever sub-1M backends happen to be registered,
    so coupling them would let an unrelated registry edit (e.g. adding a 50K
    model) silently drop every unknown model's window — a surprising,
    hard-to-trace side effect. A genuinely smaller-than-200K backend should be
    registered in :data:`_SUB_1M_CONTEXT_MODELS` so it resolves by its own
    name, not lower this shared default.
    """
    return _PROFILE_200K_WINDOW


def real_backend_window(model: str) -> int:
    """Resolve the REAL upstream context window (in tokens) for *model*.

    This is the true backend window the agent's session runs against —
    distinct from the ``[1m]`` alias, which only selects Claude Code's
    compaction *profile*. The reseed gate (#3200 slice-6) keys its 80% margin
    on this value so it never computes ``0.80 * 1_000_000`` for a backend whose
    real window is smaller (the mis-trigger bug: deferring the reseed past the
    real limit overflows the upstream mid-turn).

    Resolution keys on the BARE model name (the ``[1m]`` suffix is stripped
    first, so it is never load-bearing here):

    - A member of :data:`_SUB_1M_CONTEXT_MODELS` -> its registered real window
      (e.g. ``kimi-k2.7-code`` -> 262_144). Checked FIRST so a
      ``kimi-k2.7-code[1m]`` alias still returns 262_144, never the
      [1m]-implied 1M.
    - A recognised Claude family alias (``opus`` / ``opus[1m]`` / ``sonnet`` /
      ``claude-*`` ...) -> :data:`_CLAUDE_BACKEND_WINDOW` (1_000_000).
    - Anything else (an unregistered non-Claude / 200K-profile model) -> the
      conservative unknown window (:func:`_conservative_unknown_window`),
      which is 200_000 under the current registry.
    """
    bare = model.removesuffix(_CONTEXT_1M_SUFFIX) if model.endswith(_CONTEXT_1M_SUFFIX) else model
    # Registry first: authoritative over the [1m] alias for sub-1M backends, so
    # a stray ``[1m]`` suffix can never inflate a sub-1M window to the implied 1M.
    if bare in _SUB_1M_CONTEXT_MODELS:
        return _SUB_1M_CONTEXT_MODELS[bare]
    # Key the Claude check on ``bare`` too (consistent with the registry lookup
    # above and the docstring's "keys on the BARE name" contract). This is also
    # more correct than checking ``model``: ``haiku[1m]`` is NOT in
    # ``_CLAUDE_EXACT_ALIASES`` (only opus/sonnet/fable have ``[1m]`` members),
    # so ``_is_claude_alias("haiku[1m]")`` is False — only the bare ``haiku``
    # match resolves it to the Claude window.
    if _is_claude_alias(bare):
        return _CLAUDE_BACKEND_WINDOW
    return _conservative_unknown_window()


def reseed_threshold(model: str) -> int:
    """Token-occupancy threshold above which the event-pump gate reseeds.

    ``min(RESEED_THRESHOLD_FLOOR, floor(RESEED_THRESHOLD_MARGIN *
    real_backend_window(model)))`` (#3200 task-2-2). The floor
    (:data:`RESEED_THRESHOLD_FLOOR`, 400_000) is an absolute context-rot / cost
    ceiling — an initial knob, not derived. The margin
    (:data:`RESEED_THRESHOLD_MARGIN`, 0.80) reseeds deterministically below
    Claude Code's ~95% lossy auto-compaction wall, computed against the REAL
    backend window (never the ``[1m]`` alias). Worked examples: ``opus[1m]`` ->
    400_000; a 200K-profile model -> 160_000; a 128K-class backend -> ~102_000.
    """
    real = real_backend_window(model)
    return min(RESEED_THRESHOLD_FLOOR, int(RESEED_THRESHOLD_MARGIN * real))


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

    # Tier 3: built-in default — opus for all roles. Fable has been
    # disabled, so FABLE_DEFAULT_MODEL is now "opus" and the refine/plan
    # branch resolves to the same value as the default branch. The branch
    # is retained so re-enabling fable for refine/plan is a one-line
    # change to FABLE_DEFAULT_MODEL.
    if role_value in _FABLE_DEFAULT_ROLES:
        return classify_model(FABLE_DEFAULT_MODEL)
    return classify_model(DEFAULT_AGENT_MODEL)


def _overseer_decision_override(pipeline_config: object | None) -> str | None:
    """Operator override for the overseer's adversarial/decision tier, or None.

    Precedence:
      1. ``agent_models["overseer"]`` — the per-pipeline per-role surface
         (accepted only once ``overseer`` is promoted into
         ``MODEL_OVERRIDE_ROLES``; defensive read so it works either way).
      2. ``overseer_decision_maker_model`` — DEPRECATED back-compat. A
         non-default value still maps through rather than being a silent
         no-op (its full inertness lands in slice-9).

    Returns the raw model string to classify, or ``None`` when neither is set
    (the tier default then applies).
    """
    if pipeline_config is None:
        return None
    agent_models = getattr(pipeline_config, "agent_models", None)
    if isinstance(agent_models, dict):
        override = agent_models.get("overseer")
        if override:
            return override
    deprecated = getattr(pipeline_config, "overseer_decision_maker_model", None)
    # The field default is the routine-tier model ("sonnet"); only a value the
    # operator deliberately changed counts as an override.
    if deprecated and deprecated != OVERSEER_TIER_MODELS["routine"]:
        return deprecated
    return None


def resolve_overseer_model(
    tier: str,
    pipeline_config: object | None = None,
    repo: str | None = None,
) -> AgentModelDecision:
    """Resolve one overseer decision *tier* to an :class:`AgentModelDecision`.

    #2270 §1 (folds #2813). The overseer's model flows through the SAME
    per-agent resolver every other agent uses — there is no bespoke spawn
    path that reads ``overseer_decision_maker_model`` directly.

    Tiers (:data:`OVERSEER_TIER_MODELS`):
      - ``classify``    -> haiku  (cheap, high-volume classification);
      - ``routine``     -> sonnet (routine corrective decisions);
      - ``adversarial`` -> opus   (high-stakes / adversarial adjudication —
        the §1 fix that stops the overseer running its decision tier on
        Sonnet and mis-flagging its own bootstrap as an attack).

    Override precedence for the **adversarial/decision** tier: an explicit
    ``agent_models["overseer"]`` (per-pipeline) or the deprecated
    ``overseer_decision_maker_model`` beats the tier default; otherwise the
    tier resolves through :func:`resolve_agent_model` for
    :data:`AgentRole.OVERSEER` (repo-level default → built-in ``opus``). The
    classify and routine tiers stay on their fixed cheap defaults regardless
    of the override, so a stray decision-model override can never inflate the
    high-volume classifier's cost.

    Args:
        tier: One of ``"classify"``, ``"routine"``, ``"adversarial"``.
        pipeline_config: A ``PipelineConfig`` (typed loosely to avoid an
            import cycle) or ``None``.
        repo: Repository in ``owner/repo`` form, or ``None``.

    Returns:
        The resolved :class:`AgentModelDecision` for the tier.

    Raises:
        ValueError: If *tier* is not a recognised overseer tier.
    """
    if tier not in OVERSEER_TIER_MODELS:
        raise ValueError(
            f"Unknown overseer tier {tier!r}; expected one of "
            f"{sorted(OVERSEER_TIER_MODELS)}"
        )

    if tier == "adversarial":
        override = _overseer_decision_override(pipeline_config)
        if override:
            return classify_model(override)
        # No explicit override: resolve like every other agent so the
        # repo-level default and built-in opus apply.
        return resolve_agent_model(AgentRole.OVERSEER, pipeline_config, repo)

    # classify / routine: fixed cheap tiers, never overridden by the decision
    # model so the high-volume classifier stays on haiku.
    return classify_model(OVERSEER_TIER_MODELS[tier])


__all__ = [
    "AgentModelDecision",
    "DEFAULT_AGENT_MODEL",
    "FABLE_DEFAULT_MODEL",
    "FABLE_EFFORT",
    "OVERSEER_TIER_MODELS",
    "RESEED_THRESHOLD_FLOOR",
    "RESEED_THRESHOLD_MARGIN",
    "UPSTREAM_ANTHROPIC",
    "UPSTREAM_LITELLM",
    "classify_model",
    "context_guardrail_env",
    "real_backend_window",
    "reseed_threshold",
    "resolve_agent_model",
    "resolve_overseer_model",
]
