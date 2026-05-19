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

This package targets Python 3.14 (pyproject.toml's
``requires-python = ">=3.14"``) but the SKILL.md / packaging
documentation states "Python 3.11+". To keep both surfaces working,
EVERY ``except`` clause that catches multiple exception types MUST
use the parenthesised tuple form AND carry a ``# fmt: skip``
trailing comment, e.g.::

    except (subprocess.SubprocessError, OSError):  # fmt: skip

Without ``# fmt: skip`` ruff format (with
``target-version = "py314"``) silently strips the redundant parens
back to ``except A, B:`` which is a SyntaxError on Python
3.10/3.11/3.12/3.13 — re-introducing the v1 NACK blocker every
contributor would otherwise step on. The cheapest defense is to
``grep -nE 'except [A-Za-z.]+ *, *[A-Za-z.]+ *:' orchestrator/
plugins/`` before every commit; a CI lint rule that catches this
shape is tracked in the follow-up issue.

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
            spawner=ClaudeCodeSpawner(),
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
        self._lock = __import__("threading").RLock()

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
