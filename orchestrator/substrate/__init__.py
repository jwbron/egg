"""Substrate-swap package — walking-skeleton spike for issue #2623.

The four-protocol substrate model (``AgentSpawner``, ``MessageBus``,
``PolicyEnforcer``, ``WorktreeManager``) lets egg's orchestrator run
either against the existing k3s/Redis/gateway stack OR natively inside
a Claude Code session, selected at boot via the ``EGG_SUBSTRATE`` env
var per HITL decision cq-1.

See ``docs/architecture/claude-code-substrate.md`` for the full ADR.

INTERFACE STABILITY: v0.x unstable.

Multi-exception ``except`` discipline (must read before editing)
----------------------------------------------------------------

This package — and the whole repo (pyproject.toml's
``requires-python = ">=3.14"``) — targets Python 3.14+. Python 3.14
introduces the parenthesless ``except A, B:`` syntax (PEP 758, 2025);
under ruff's ``target-version = "py314"`` formatter, the redundant
parens in ``except (A, B):`` are stripped to that 3.14-only shape.
On any older interpreter (3.13 and below) the stripped form is a
SyntaxError.

We deliberately keep the parenthesised form in source for two reasons:
(1) it parses on every interpreter from 3.0 onward, so contributors
copying snippets into a 3.13 venv (or the ADR's "Python 3.14+" claim
in SKILL.md isn't honored) get a clearer error path; (2) the
parenthesised form is unambiguous to read — ``except A, B:`` shares
its grammar with a Python-2-era binding form some readers still see
in muscle memory. To preserve the parens against ``ruff format``,
multi-exception ``except`` clauses carry a trailing
``# fmt: skip``::

    except (subprocess.SubprocessError, OSError):  # fmt: skip

The cheapest preflight is to grep for the bare form
(``grep -nE 'except [A-Za-z.]+ *, *[A-Za-z.]+ *:' orchestrator/
plugins/``) before every commit; a CI lint rule that catches this
shape is tracked in the follow-up issue beyond #2717.

The protocols and ``SubstrateBundle`` shape are part of a walking-
skeleton spike (cq-11). The follow-up rollout issue may reshape them
in incompatible ways; downstream consumers should not assume API
stability until the follow-up issue formally promotes the protocols.

Usage::

    from orchestrator.substrate import select_substrate
    bundle = select_substrate(os.environ)
    result = bundle.spawner.spawn(role, prompt, env, worktree)

TODO (follow-up issue): build a single-class
``KubernetesSpawnerAdapter`` that consumes the full feature set of
``KubernetesSpawner`` (image overrides, repo volumes, slice-scoped
spawning, retry policy) instead of the minimal
``K3sSpawnerAdapter`` shim that lives here today.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .k3s_adapter import K3sSpawnerAdapter
from .message_bus import MessageBus
from .policy import PolicyEnforcer
from .spawner import AgentResult, AgentSpawner
from .worktree import WorktreeManager

__all__ = [
    "AgentResult",
    "AgentSpawner",
    "K3sSpawnerAdapter",
    "MessageBus",
    "PolicyEnforcer",
    "SubstrateBundle",
    "WorktreeManager",
    "select_substrate",
]


@dataclass
class SubstrateBundle:
    """The four substrate implementations selected by ``EGG_SUBSTRATE``.

    Fields:
        name: ``"k3s"`` or ``"claude-code"``.
        spawner: ``AgentSpawner`` instance.
        bus: ``MessageBus`` instance.
        policy: ``PolicyEnforcer`` instance.
        worktrees: ``WorktreeManager`` instance.
    """

    name: str
    spawner: AgentSpawner
    bus: MessageBus
    policy: PolicyEnforcer
    worktrees: WorktreeManager


def select_substrate(
    env: Mapping[str, str],
    *,
    k3s_legacy_spawn_fn: Any | None = None,
    k3s_message_bus: MessageBus | None = None,
    k3s_policy: PolicyEnforcer | None = None,
    k3s_worktrees: WorktreeManager | None = None,
) -> SubstrateBundle:
    """Return the substrate bundle selected by ``EGG_SUBSTRATE``.

    Args:
        env: Mapping with environment variables (typically
            ``os.environ``).
        k3s_legacy_spawn_fn: Optional callable returned by
            ``KubernetesSpawner.create_concurrent_spawn_fn(...)``.
            When supplied (and ``EGG_SUBSTRATE`` resolves to ``"k3s"``),
            the bundle's ``spawner`` is a ``K3sSpawnerAdapter``
            wrapping it.
        k3s_message_bus / k3s_policy / k3s_worktrees: Optional pre-
            built implementations for the k3s leg. The k3s production
            path already wires these up at boot via
            ``orchestrator.cli.cmd_serve``; the parameters exist so
            unit tests can supply lightweight test doubles without
            standing up the full daemon.

    Returns:
        A ``SubstrateBundle`` with the four implementations.

    Raises:
        ValueError: If ``EGG_SUBSTRATE`` resolves to an unknown value.

    Notes:
        The ``"k3s"`` leg always returns a *working* spawner — even
        without ``k3s_legacy_spawn_fn`` supplied, the bundle's
        ``spawner`` is a ``K3sSpawnerAdapter`` constructed against a
        lazily-built ``KubernetesSpawner.create_concurrent_spawn_fn``
        proxy. This is the cq-1 contract: both substrates are co-equal
        from day one.

        The non-spawner k3s legs (``bus``, ``policy``, ``worktrees``)
        fall back to ``_K3sPlaceholder`` when no implementation is
        injected. These placeholders raise ``NotImplementedError``
        when invoked. The production k3s boot path (started by
        ``orchestrator.cli.cmd_serve``) does not invoke
        ``select_substrate`` — it wires ``MessageStore`` /
        ``RedisMessageStore`` / ``WorktreeManager`` directly — so the
        placeholder is only ever reached by tests that explicitly opt
        into the k3s leg without supplying overrides. This is
        deliberate: it gives callers a structured "not wired up" error
        rather than silently returning a non-functional bundle.
    """
    name = (env.get("EGG_SUBSTRATE") or "k3s").lower()

    if name == "k3s":
        spawner = _build_k3s_spawner(k3s_legacy_spawn_fn)
        bus = (
            k3s_message_bus
            if k3s_message_bus is not None
            else _K3sPlaceholder(
                "message_bus",
                "Production k3s message bus is wired in orchestrator.cli.cmd_serve "
                "via get_message_store(); supply k3s_message_bus= for tests.",
            )
        )
        policy = (
            k3s_policy
            if k3s_policy is not None
            else _K3sPlaceholder(
                "policy",
                "Production k3s policy is gateway-enforced via "
                "gateway/phase_filter.py; supply k3s_policy= for tests.",
            )
        )
        worktrees = (
            k3s_worktrees
            if k3s_worktrees is not None
            else _K3sPlaceholder(
                "worktrees",
                "Production k3s worktrees are managed by the gateway sidecar "
                "(gateway/worktree_manager.py); supply k3s_worktrees= for tests.",
            )
        )
        return SubstrateBundle(
            name="k3s",
            spawner=spawner,  # type: ignore[arg-type]
            bus=bus,  # type: ignore[arg-type]
            policy=policy,  # type: ignore[arg-type]
            worktrees=worktrees,  # type: ignore[arg-type]
        )

    if name == "claude-code":
        # Lazy import — the claude_code module pulls in egg_harness
        # which is heavier than the protocol surface.
        from .claude_code.message_bus import InProcessMessageBus
        from .claude_code.policy import PreToolUseHookPolicy
        from .claude_code.spawner import ClaudeCodeSpawner
        from .claude_code.worktree import LocalWorktreeManager

        return SubstrateBundle(
            name="claude-code",
            spawner=ClaudeCodeSpawner(role_rubric_loader=_load_egg_sdlc_role_rubric),
            bus=InProcessMessageBus(),
            policy=PreToolUseHookPolicy(),
            worktrees=LocalWorktreeManager(),
        )

    raise ValueError(f"Unknown EGG_SUBSTRATE={name!r}; expected 'k3s' or 'claude-code'.")


def _build_k3s_spawner(legacy_spawn_fn: Any | None) -> AgentSpawner:
    """Construct the k3s ``AgentSpawner`` for ``select_substrate``.

    Args:
        legacy_spawn_fn: Optional pre-built spawn callable returned by
            ``KubernetesSpawner.create_concurrent_spawn_fn(...)``.

    Returns:
        Always a ``K3sSpawnerAdapter`` (directly when
        ``legacy_spawn_fn`` is supplied; via a lazily-resolved factory
        otherwise). The cq-1 contract — co-equal substrates from day
        one — requires that ``select_substrate({})`` produce a
        functional spawner, not a deferred stub. The
        ``_LazyK3sSpawner`` constructs the underlying
        ``KubernetesSpawner.create_concurrent_spawn_fn(...)`` on the
        first ``.spawn()`` call using env-derived defaults. If the
        env does not supply enough configuration (e.g.
        ``EGG_PIPELINE_ID`` is missing), the underlying spawner
        raises a clear ``ValueError`` rather than a ``NotImplementedError``
        — operators learn that the k3s leg needs the same env vars
        the daemon's ``cmd_serve`` consumes.
    """
    if legacy_spawn_fn is not None:
        return K3sSpawnerAdapter(legacy_spawn_fn)
    return _LazyK3sSpawner()


#: Per-role mapping naming which #2717 rollout slice ships the rubric
#: markdown for that role. Updated as each slice lands its
#: documenter-owned rubric files. Roles absent from this map are
#: "deferred indefinitely" (overseer / inspector / autofixer /
#: conflict_resolver — intentionally unhandled per task-3-6).
#:
#: Source of truth for "is this role part of the rollout?". Whether
#: the rubric *file* has actually landed on disk is checked by the
#: loader via ``Path.is_file()`` — no parallel "landed roles" registry
#: that could drift from the filesystem state.
#:
#: The slice numbers match issue #2717's plan:
#:   slice-1: refiner (already shipped in #2715) + 2 refine reviewers
#:            (task-1-4 documenter).
#:   slice-2: 3 plan producers + reviewer_plan (task-2-3 documenter).
#:   slice-3: 3 implement producers + 5 implement reviewers
#:            (task-3-4 / task-3-5 documenter).
_ROLE_RUBRIC_SLICES: dict[str, str] = {
    # Slice-1 (refine team).
    "refiner": "slice-1",
    "reviewer_refine": "slice-1",
    "reviewer_agent_design": "slice-1",
    # Slice-2 (plan team).
    "architect": "slice-2",
    "task_planner": "slice-2",
    "risk_analyst": "slice-2",
    "reviewer_plan": "slice-2",
    # Slice-3 (implement team).
    "coder": "slice-3",
    "tester": "slice-3",
    "documenter": "slice-3",
    "reviewer_code": "slice-3",
    "reviewer_code_holistic": "slice-3",
    "reviewer_contract": "slice-3",
    "reviewer_security": "slice-3",
    "reviewer_concurrency": "slice-3",
}

#: Roles whose rubric markdown has been landed by a documenter in this
#: slice. The set grows as each slice's documenter task completes;
#: today (#2717 slice-2) it contains the refiner (shipped in #2715),
#: the two refine-team reviewers (task-1-4, slice-1) and the four
#: plan-team rubrics (task-2-3, slice-2 documenter, lands in parallel
#: with this loader update — TASK-2-3 → TASK-2-2 sequencing within
#: the slice).
_RUBRIC_LANDED_ROLES: frozenset[str] = frozenset(
    {
        # Slice-1 (refine team).
        "refiner",
        "reviewer_refine",
        "reviewer_agent_design",
        # Slice-2 (plan team — task-2-2 extends the loader; the
        # documenter ships the markdown files via task-2-3 within the
        # same slice).
        "architect",
        "task_planner",
        "risk_analyst",
        "reviewer_plan",
    }
)


def _load_egg_sdlc_role_rubric(role: Any) -> str:
    """Load the role rubric markdown from ``plugins/egg-sdlc/skills/egg-sdlc/agents/<role>.md``.

    Reviewer v1 blocker #5: without an injected loader, the default
    fallback was a one-line "you are <role>" string and the 119-line
    refiner rubric file was dead. The production wiring of the
    claude-code substrate must point ``ClaudeCodeSpawner`` at the
    plugin's per-role markdown so ``build_system_prompt(sources)``
    actually receives the rubric (the structural depth fix from
    #2622).

    Issue #2717 rollout scope: slice-1 added the refine team
    (refiner + reviewer_refine + reviewer_agent_design); slice-2
    extends the rubric-supported set to the plan team (architect +
    task_planner + risk_analyst + reviewer_plan). Implement-team
    roles continue to raise ``ValueError`` with a pointer to the
    slice (slice-3) that ships their rubric, so the structured-error
    contract for missing rubrics stays consistent across the
    rollout. The mapping lives in ``_ROLE_RUBRIC_SLICES`` so future
    slices can extend it without touching this loader's body.

    Args:
        role: ``AgentRole`` (or a string-equivalent) identifying the
            role to load.

    Returns:
        Markdown body of the rubric file (frontmatter retained — the
        frontmatter is informational only per ``refiner.md``).

    Raises:
        ValueError: when the role is not yet in the rubric-supported
            set for this slice, OR when the supported-role's rubric
            file does not exist on disk (typically because the
            documenter hasn't landed it yet within the same slice;
            sequence TASK-1-4 → TASK-1-6 within slice-1).
    """
    from pathlib import Path as _Path

    role_name = role.value if hasattr(role, "value") else str(role)
    here = _Path(__file__).resolve()
    # orchestrator/substrate/__init__.py  →  repo root.
    # TODO(cq-12 follow-up): once the egg Python packages publish to
    # pip and the egg-sdlc plugin no longer relies on the from-source
    # install, this `parent.parent.parent / plugins / …` walk breaks
    # — site-packages does not co-locate the plugins directory. The
    # follow-up should swap to a packaging-aware resolution (e.g.
    # ``importlib.resources.files("egg_sdlc_plugin").joinpath(...)``
    # behind a published namespace), with a from-source fallback for
    # the walking-skeleton install path. See SKILL.md's install
    # section and the bridge-gap callout.
    repo_root = here.parent.parent.parent
    rubric_path = (
        repo_root / "plugins" / "egg-sdlc" / "skills" / "egg-sdlc" / "agents" / f"{role_name}.md"
    )

    # Fence: roles outside the rubric-landed set for this slice raise
    # with a structured pointer to the rollout slice that ships them.
    # Roles not in _ROLE_RUBRIC_SLICES at all are "indefinitely
    # deferred" (overseer / inspector / autofixer / conflict_resolver).
    if role_name not in _RUBRIC_LANDED_ROLES:
        slice_hint = _ROLE_RUBRIC_SLICES.get(role_name)
        if slice_hint is None:
            raise ValueError(
                f"egg-sdlc role rubric missing for role={role_name!r}. "
                f"This role is not part of the #2717 rollout's rubric "
                "set; if your pipeline needs it, file a follow-up issue."
            )
        raise ValueError(
            f"egg-sdlc role rubric for role={role_name!r} is deferred to "
            f"follow-up {slice_hint} of issue #2717's rollout. "
            "See docs/architecture/claude-code-substrate.md for the slice DAG."
        )

    if not rubric_path.is_file():
        # Supported role, but the documenter's .md file hasn't landed
        # in this slice yet. Surface as a clear "rubric missing in
        # slice-1" error so the reviewer / operator knows the
        # documenter task is still in flight.
        raise ValueError(
            f"egg-sdlc role rubric missing on disk at {rubric_path} for "
            f"role={role_name!r}. The role is in this slice's "
            "rubric-supported set but the markdown file has not been "
            "landed yet — sequence the documenter's rubric task "
            "(e.g. TASK-1-4 for slice-1's refine reviewers, TASK-2-3 "
            "for slice-2's plan team) before the loader update "
            "(TASK-1-6 / TASK-2-2) within the same slice."
        )
    return rubric_path.read_text(encoding="utf-8")


class _LazyK3sSpawner:
    """``AgentSpawner`` that lazily constructs the real k3s factory.

    Satisfies the cq-1 "co-equal substrates from day one" contract:
    ``select_substrate({})`` returns a functional spawner even when
    no ``k3s_legacy_spawn_fn`` was injected. On the first
    ``.spawn()`` call, the lazy adapter:

    1. Reads ``EGG_PIPELINE_ID`` / ``EGG_GATEWAY_MODE`` from ``env``.
    2. Instantiates a ``KubernetesSpawner`` (lazy k8s/gateway
       clients).
    3. Calls ``create_concurrent_spawn_fn(...)`` to build the legacy
       spawn callable.
    4. Wraps it in a ``K3sSpawnerAdapter`` and delegates.

    This means a unit test or out-of-band caller who runs the k3s
    bundle without injecting a spawn fn will reach a real factory
    call. If the runtime environment lacks the k3s prereqs (e.g.
    no kubectl on PATH, no gateway URL), the underlying call raises
    with the same error message a real ``cmd_serve`` boot would
    produce — preferable to a silent ``NotImplementedError``.
    """

    def __init__(self) -> None:
        self._adapter: K3sSpawnerAdapter | None = None
        self._lock = threading.RLock()

    def _build_adapter(self, env: Mapping[str, str]) -> K3sSpawnerAdapter:
        try:
            from orchestrator.kubernetes_spawner import KubernetesSpawner
        except ImportError:  # pragma: no cover
            from kubernetes_spawner import (  # type: ignore[no-redef, import-untyped]
                KubernetesSpawner,
            )
        pipeline_id = env.get("EGG_PIPELINE_ID") or env.get("PIPELINE_ID") or "unknown"
        mode = env.get("EGG_GATEWAY_MODE") or env.get("MODE") or "local"
        spawner = KubernetesSpawner()
        legacy_fn = spawner.create_concurrent_spawn_fn(
            pipeline_id=pipeline_id,
            issue_number=None,
            repo_volumes=None,
            mode=mode,
            repos=None,
            phase=env.get("EGG_PHASE"),
        )
        return K3sSpawnerAdapter(legacy_fn)

    def spawn(
        self,
        role: Any,
        prompt: str,
        env: Mapping[str, str],
        worktree: Any,
    ) -> AgentResult:
        with self._lock:
            if self._adapter is None:
                self._adapter = self._build_adapter(env)
        return self._adapter.spawn(role, prompt, env, worktree)


class _K3sPlaceholder:
    """Placeholder for the bus/policy/worktree slots of the k3s leg
    when no real implementation is injected.

    Provides a typed "not wired up" error so the failure mode is
    actionable. The production k3s daemon wires real implementations
    directly; tests that need the k3s leg must inject them.
    """

    def __init__(self, kind: str, hint: str) -> None:
        self._kind = kind
        self._hint = hint

    def __getattr__(self, name: str) -> Any:
        raise NotImplementedError(
            f"k3s {self._kind} placeholder has no method {name!r}. {self._hint}"
        )
