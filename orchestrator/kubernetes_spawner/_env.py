"""Environment / label helpers for kubernetes_spawner (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

from collections.abc import Mapping

from kubernetes_spawner import (
    _FORWARDED_DISCIPLINE_ENV_KEYS,
    _LABEL_VALUE_MAXLEN,
    _WAIT_ALLOWLIST_SYSTEM_SENDERS,
)
from review_graph import get_review_graph_for_phase


def _resolve_wait_producer_allowlist(phase: str | None, role: str, repo: str | None) -> str | None:
    """Build the ``EGG_WAIT_PRODUCER_ALLOWLIST`` value for a spawn (#2725).

    Looks the role up in the BRC review graph for the supplied phase and
    returns a comma-separated allowlist of:

    - the role's graph neighbors — reviewers get the producers they
      review; producers get their reviewers so they wake on ACK/NACK
      and (for dual-role) any producers they also review. For
      dual-role agents (e.g. ``tester`` is both a producer reviewed by
      ``reviewer_code`` and a reviewer of ``coder``) the union of both
      neighbor sets is used so the agent wakes on both directions of
      cross-graph traffic.
    - the system senders ``overseer`` and ``orchestrator`` so
      ``OVERSEER_ALERT`` and ``CONSENSUS_RE_REVIEW`` keep waking the
      agent regardless of the producer set.

    Returns ``None`` when the role has no graph neighbors in the
    requested phase. This omits the env var entirely so the spawn
    preserves legacy wake-on-anything behavior — the wake-storm fix
    is opt-in via graph membership, not a default ratchet.

    ``get_review_graph_for_phase`` is documented to return an empty
    :class:`ReviewGraph` for unknown phases rather than raising, so
    this function intentionally does NOT wrap it in a ``try/except``:
    a programmer-error exception from a future refactor should surface
    loudly during spawn rather than degrade silently to "no allowlist,
    wake on everything," which is the wake-storm we are fixing.
    """
    if not phase:
        return None
    graph = get_review_graph_for_phase(phase, repo)

    neighbors: set[str] = set()
    if graph.is_reviewer(role):
        neighbors.update(graph.producers_for(role))
    if graph.is_producer(role):
        neighbors.update(graph.reviewers_for(role))
    if not neighbors:
        # Role not in the graph (pipeline-level helpers, ad-hoc roles)
        # — no allowlist, no filter, no behavior change.
        return None
    allowlist = sorted(neighbors | set(_WAIT_ALLOWLIST_SYSTEM_SENDERS))
    return ",".join(allowlist)


def _forwarded_discipline_env(source: Mapping[str, str]) -> dict[str, str]:
    """Return the context-discipline flags set and non-blank in *source*.

    Pure: selects the :data:`_FORWARDED_DISCIPLINE_ENV_KEYS` subset of *source*
    (typically ``os.environ``) whose value is set and non-blank. An unset or
    blank flag is omitted, never forwarded as an empty string — so the pod's
    default-OFF parse is identical to the flag simply being absent. Note this is
    a non-blank filter, not a truthiness filter: a value like ``"false"`` is
    forwarded as-is and parsed OFF in-pod.
    """
    out: dict[str, str] = {}
    for key in _FORWARDED_DISCIPLINE_ENV_KEYS:
        value = source.get(key)
        if value and value.strip():
            out[key] = value
    return out


def _dedupe_label_value(dedupe_key: str) -> str:
    """Shorten a dedupe key to a Kubernetes-label-safe value (<=63 chars).

    The dedupe key is a 64-char sha256 hexdigest; k8s rejects label values
    longer than 63 chars. Deterministic truncation keeps the value stable
    across restarts so the spawn-side label and the reconcile-side selector
    always agree on the same string (a 63-hex-char sha256 prefix is 252 bits —
    collision-free for spawn dedupe). Every char of a hex digest is
    alphanumeric, so the truncated prefix is always a valid label value.
    Idempotent for already-short keys.
    """
    return dedupe_key[:_LABEL_VALUE_MAXLEN]
