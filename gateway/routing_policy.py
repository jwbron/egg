"""
Routing Policy for the Gateway LLM Proxy.

The gateway is the single router for every ``/v1/messages`` request (issue
#2987). On top of the spawn-time per-agent upstream selection
(``Session.upstream``, driven by ``agent_models``), this module adds a
**hot-reloadable, model-keyed routing policy** with two independent levers:

- ``switchover`` — *proactive* remap. Before the first send, a wire model
  name (the request body's ``"model"``) can be remapped to a different
  upstream and/or model. This is the knob operators use to globally
  re-route a wire model without respawning agents.
- ``fallbacks`` — *reactive* chain. When the primary upstream returns a
  trigger status (quota / transient error), the proxy advances through an
  ordered list of fallback hops for that wire model.

**Consolidation principle: fallback is a property of the model, not the
pipeline.** The policy keys on the wire model, so an explicit per-pipeline
``agent_models`` override inherits the same fallback chain for free — there
is no per-pipeline fallback threaded through the session.

The policy file is YAML, loaded from ``/secrets/routing-policy.yaml`` with
the same mtime-invalidated cache as the credential managers
(``anthropic_credentials.py``). It rides the proven
``~/.config/egg/`` → ``gateway-secrets`` → ``/secrets`` mount: ``make
k3s-secrets`` bundles every file under ``~/.config/egg/`` as a Secret key,
and the whole-volume mount means kubelet propagates an updated
``routing-policy.yaml`` to the running gateway pod **without a restart and
without losing in-flight turns**. (It is config riding a Secret for the
hot-reload mount, not a credential.)

Failure posture is **fail-open to the default route**: a missing, empty, or
malformed policy file yields an empty policy (no switchover, no fallbacks),
so a typo in the policy never takes down inference — it just falls back to
the byte-identical pre-#2987 single-upstream behavior. This mirrors the
credential managers' "return None / no-op on problem" stance.

Trigger defaults (overridable per-file, issue #2987 review):

- ``advance_on`` defaults to ``{429}`` — **quota only**. Cross-model
  escalation is expensive (it can land on Opus), so a transient blip from a
  cheap open model must NOT silently spend Opus dollars, and a genuinely
  buggy upstream 500 must NOT be papered over by a green Opus response that
  masks the defect.
- ``retry_same_on`` defaults to ``{500, 502, 503, 529}`` with
  ``retry_same_max = 1`` — a transient 5xx retries the **same** upstream
  once before being surfaced. Broader 5xx *escalation* (advancing to a
  different model on 5xx) is opt-in: add the codes to ``advance_on``.

Context-window safety (issue #2987 gotcha, NOT runtime-enforced here): the
Claude Code compaction profile is fixed at spawn via the ``[1m]`` /
``ANTHROPIC_CUSTOM_MODEL_OPTION`` registration. A fallback/switchover target
with a *smaller* real context window than the source (e.g. Kimi 256K, GLM
202K under a 1M source) can overflow mid-conversation. ``1M → Opus`` is
safe; ``1M → 256K`` is risky. The gateway cannot know each model's window,
so this is a **policy-authoring constraint** documented in
``config/routing-policy.template.yaml`` and the upstream-routing
architecture doc — not a check this module makes.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

logger = get_logger("gateway.routing-policy")


# Default policy path. Mirrors token_refresher.py: ``EGG_CONFIG_DIR`` points
# at ``/secrets`` in the k8s gateway pod (see gateway-deployment.yaml), and
# falls back to the host config dir for standalone / dev runs. An explicit
# ``EGG_ROUTING_POLICY_PATH`` override wins (used by tests).
_DEFAULT_CONFIG_DIR = Path(os.environ.get("EGG_CONFIG_DIR", Path.home() / ".config" / "egg"))
ROUTING_POLICY_PATH = Path(
    os.environ.get("EGG_ROUTING_POLICY_PATH", _DEFAULT_CONFIG_DIR / "routing-policy.yaml")
)


# Trigger defaults — see module docstring for the rationale (issue #2987
# review). Quota (429) advances to a different model; transient 5xx retries
# the SAME upstream once and is then surfaced rather than escalated.
DEFAULT_ADVANCE_ON: frozenset[int] = frozenset({429})
DEFAULT_RETRY_SAME_ON: frozenset[int] = frozenset({500, 502, 503, 529})
DEFAULT_RETRY_SAME_MAX: int = 1


@dataclass(frozen=True)
class RouteHop:
    """A single routing hop: which upstream, and an optional model rewrite.

    ``model`` is the bare upstream-side model name. When set, the proxy
    rewrites the request body's ``"model"`` field before forwarding (the
    narrowly-scoped reintroduction of the helper removed in #2832). A hop to
    the ``anthropic`` upstream that rewrites a non-Claude wire model MUST set
    ``model`` to a real Claude id, else Anthropic 404s the unknown model.
    """

    upstream: str
    model: str | None = None


@dataclass(frozen=True)
class TriggerConfig:
    """Status-code policy governing same-hop retry vs cross-model advance."""

    advance_on: frozenset[int] = DEFAULT_ADVANCE_ON
    retry_same_on: frozenset[int] = DEFAULT_RETRY_SAME_ON
    retry_same_max: int = DEFAULT_RETRY_SAME_MAX


@dataclass(frozen=True)
class RoutingPolicy:
    """Parsed, immutable routing policy.

    An *empty* policy (no switchover entries, no fallback chains, default
    triggers) is the no-op default and yields byte-identical pre-#2987
    behavior.
    """

    switchover: dict[str, RouteHop]
    fallbacks: dict[str, tuple[RouteHop, ...]]
    triggers: TriggerConfig

    @property
    def is_empty(self) -> bool:
        return not self.switchover and not self.fallbacks

    def switchover_for(self, wire_model: str | None) -> RouteHop | None:
        """Return the proactive remap for ``wire_model``, or ``None``."""
        if not wire_model:
            return None
        return self.switchover.get(wire_model)

    def fallback_chain_for(self, wire_model: str | None) -> tuple[RouteHop, ...]:
        """Return the reactive fallback chain for ``wire_model`` (``()`` if none)."""
        if not wire_model:
            return ()
        return self.fallbacks.get(wire_model, ())


# The empty / no-op policy. Returned whenever no policy file exists or it
# cannot be parsed — the fail-open default.
EMPTY_POLICY = RoutingPolicy(switchover={}, fallbacks={}, triggers=TriggerConfig())


def _parse_hop(raw: object, *, context: str) -> RouteHop | None:
    """Parse one hop dict into a ``RouteHop``; ``None`` (with a warning) on a
    malformed entry so a single bad hop never invalidates the whole file."""
    if not isinstance(raw, dict):
        logger.warning("Routing-policy hop is not a mapping; skipping", context=context)
        return None
    upstream = raw.get("upstream")
    if not isinstance(upstream, str) or not upstream.strip():
        logger.warning(
            "Routing-policy hop missing a non-empty 'upstream'; skipping",
            context=context,
        )
        return None
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        logger.warning(
            "Routing-policy hop has a non-string 'model'; ignoring the rewrite",
            context=context,
        )
        model = None
    return RouteHop(upstream=upstream.strip(), model=model.strip() if model else None)


def _parse_triggers(raw: object) -> TriggerConfig:
    """Parse the optional ``triggers`` block, falling back to defaults per
    field. A malformed block degrades to defaults rather than failing."""
    if raw is None:
        return TriggerConfig()
    if not isinstance(raw, dict):
        logger.warning("Routing-policy 'triggers' is not a mapping; using defaults")
        return TriggerConfig()

    def _int_set(key: str, default: frozenset[int]) -> frozenset[int]:
        if key not in raw:
            return default
        value = raw[key]
        if not isinstance(value, list) or not all(isinstance(v, int) for v in value):
            logger.warning(
                "Routing-policy trigger list is not a list of ints; using default",
                key=key,
            )
            return default
        return frozenset(value)

    advance_on = _int_set("advance_on", DEFAULT_ADVANCE_ON)
    retry_same_on = _int_set("retry_same_on", DEFAULT_RETRY_SAME_ON)
    retry_same_max = raw.get("retry_same_max", DEFAULT_RETRY_SAME_MAX)
    if not isinstance(retry_same_max, int) or retry_same_max < 0:
        logger.warning("Routing-policy 'retry_same_max' is invalid; using default")
        retry_same_max = DEFAULT_RETRY_SAME_MAX
    return TriggerConfig(
        advance_on=advance_on,
        retry_same_on=retry_same_on,
        retry_same_max=retry_same_max,
    )


def parse_routing_policy(raw: object) -> RoutingPolicy:
    """Parse a loaded YAML document into a ``RoutingPolicy``.

    Fail-open: a top-level shape that is not a mapping yields the empty
    policy. Within a well-shaped document, individual malformed entries are
    dropped with a warning so one bad line never disables all routing.
    """
    if raw is None:
        return EMPTY_POLICY
    if not isinstance(raw, dict):
        logger.warning("Routing policy is not a mapping; ignoring (fail-open to default route)")
        return EMPTY_POLICY

    switchover: dict[str, RouteHop] = {}
    raw_switch = raw.get("switchover") or {}
    if isinstance(raw_switch, dict):
        for wire_model, hop_raw in raw_switch.items():
            hop = _parse_hop(hop_raw, context=f"switchover[{wire_model}]")
            if hop is not None:
                switchover[str(wire_model)] = hop
    else:
        logger.warning("Routing-policy 'switchover' is not a mapping; ignoring")

    fallbacks: dict[str, tuple[RouteHop, ...]] = {}
    raw_fallbacks = raw.get("fallbacks") or {}
    if isinstance(raw_fallbacks, dict):
        for wire_model, chain_raw in raw_fallbacks.items():
            if not isinstance(chain_raw, list):
                logger.warning(
                    "Routing-policy fallback chain is not a list; skipping",
                    wire_model=str(wire_model),
                )
                continue
            hops = tuple(
                hop
                for hop in (
                    _parse_hop(h, context=f"fallbacks[{wire_model}][{i}]")
                    for i, h in enumerate(chain_raw)
                )
                if hop is not None
            )
            if hops:
                fallbacks[str(wire_model)] = hops
    else:
        logger.warning("Routing-policy 'fallbacks' is not a mapping; ignoring")

    return RoutingPolicy(
        switchover=switchover,
        fallbacks=fallbacks,
        triggers=_parse_triggers(raw.get("triggers")),
    )


class RoutingPolicyManager:
    """Loads and caches the routing policy with mtime invalidation.

    Mirrors ``AnthropicCredentialsManager``: stat the file on every
    ``get_policy()``, reload only when the mtime changes. Thread-safe for
    concurrent request handling. A missing file is the no-op default and is
    not warned about on every call.
    """

    def __init__(self, policy_path: Path | None = None) -> None:
        self._policy_path = policy_path or ROUTING_POLICY_PATH
        self._policy: RoutingPolicy = EMPTY_POLICY
        self._cached_mtime: float = 0.0
        self._lock = threading.Lock()

    def get_policy(self) -> RoutingPolicy:
        """Return the cached policy, reloading on mtime change."""
        try:
            current_mtime = self._policy_path.stat().st_mtime
        except OSError:
            # No policy file — the no-op default. Reset the cache so a
            # later-created file is picked up on its first appearance.
            with self._lock:
                self._policy = EMPTY_POLICY
                self._cached_mtime = 0.0
            return EMPTY_POLICY

        with self._lock:
            if current_mtime != self._cached_mtime:
                self._policy = self._load_policy()
                self._cached_mtime = current_mtime
            return self._policy

    def _load_policy(self) -> RoutingPolicy:
        try:
            text = self._policy_path.read_text()
        except OSError as e:
            logger.warning(
                "Failed to read routing policy; using default route",
                path=str(self._policy_path),
                error=str(e),
            )
            return EMPTY_POLICY
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as e:
            logger.warning(
                "Routing policy is not valid YAML; using default route",
                path=str(self._policy_path),
                error=str(e),
            )
            return EMPTY_POLICY

        policy = parse_routing_policy(raw)
        if policy.is_empty:
            logger.info(
                "Routing policy loaded (no switchover/fallbacks)", path=str(self._policy_path)
            )
        else:
            logger.info(
                "Routing policy loaded",
                path=str(self._policy_path),
                switchover_models=sorted(policy.switchover.keys()),
                fallback_models=sorted(policy.fallbacks.keys()),
                advance_on=sorted(policy.triggers.advance_on),
                retry_same_on=sorted(policy.triggers.retry_same_on),
            )
        return policy

    def reload(self) -> None:
        """Force a reload on next ``get_policy()`` (for tests / config updates)."""
        with self._lock:
            self._cached_mtime = 0.0
            self._policy = EMPTY_POLICY


# Module-level singleton mirrors the credential managers' lifetime.
_routing_policy_manager: RoutingPolicyManager | None = None
_manager_lock = threading.Lock()


def get_routing_policy_manager() -> RoutingPolicyManager:
    """Return the module-level ``RoutingPolicyManager`` singleton."""
    global _routing_policy_manager
    if _routing_policy_manager is None:
        with _manager_lock:
            if _routing_policy_manager is None:
                _routing_policy_manager = RoutingPolicyManager()
    return _routing_policy_manager


def reset_routing_policy_manager() -> None:
    """Reset the module-level manager. For tests only."""
    global _routing_policy_manager
    with _manager_lock:
        _routing_policy_manager = None


__all__ = [
    "DEFAULT_ADVANCE_ON",
    "DEFAULT_RETRY_SAME_MAX",
    "DEFAULT_RETRY_SAME_ON",
    "EMPTY_POLICY",
    "ROUTING_POLICY_PATH",
    "RouteHop",
    "RoutingPolicy",
    "RoutingPolicyManager",
    "TriggerConfig",
    "get_routing_policy_manager",
    "parse_routing_policy",
    "reset_routing_policy_manager",
]
