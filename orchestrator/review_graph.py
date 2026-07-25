"""Asymmetric review graph for BRC consensus protocol.

Defines which roles review which other roles' work. The graph is
directed: reviewers judge producers, not the reverse. This eliminates
the circular ACK problem where a producer would be incentivized to
NACK a negative review of their own code.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from egg_logging import get_logger

if TYPE_CHECKING:
    from risk_router import RiskConfig, RiskRouteDecision

logger = get_logger("orchestrator.review_graph")


class ReviewCriticality(StrEnum):
    """How critical a review edge is for consensus."""

    CRITICAL = "critical"  # Must ACK for consensus (blocks if unresolved)
    ADVISORY = "advisory"  # Flags but doesn't block consensus


@dataclass(frozen=True)
class ReviewEdge:
    """A directed edge in the review graph: reviewer -> producer."""

    reviewer_role: str
    producer_role: str
    criticality: ReviewCriticality = ReviewCriticality.CRITICAL
    # A "wake-only" edge is an advisory edge the reviewer NEVER votes on.
    # It models the de-roled simplifier (#3381): a producer of the
    # human-focused companion that retains an advisory edge over the
    # upstream refine/plan producer but issues no ACK/NACK on it.
    #
    # It does NOT drive the wake. The simplifier is woken to write its
    # companion by the ordinary producer **propose-arm**: a WORKING
    # producer re-derives ``propose`` on every event-loop poll, and a clean
    # orient-and-exit is a *legitimate* outcome that frees the spawn-dedupe
    # key, so the orchestrator re-spawns it until it proposes (see
    # ``test_event_loop.py::test_stale_exit_is_a_non_trigger_through_loop``).
    # The simplifier self-gates on the upstream draft existing, so it
    # orients-and-exits until the draft is committed, then proposes.
    #
    # ``wake_only`` exists only to NEUTRALIZE the residual advisory edge so
    # it carries no review obligation: it is excluded from pending-review
    # derivation (so the de-roled reviewer is never assigned a spawn-able
    # ``ack`` it cannot satisfy — the regression #3381 fixed) and from the
    # reviewer confirm guards (so it can confirm without a verdict it will
    # never cast). A wake-only edge is always ADVISORY.
    wake_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer_role": self.reviewer_role,
            "producer_role": self.producer_role,
            "criticality": self.criticality.value,
            "wake_only": self.wake_only,
        }


class ReviewGraph:
    """Asymmetric review topology for a pipeline phase.

    Producers create artifacts and propose them for review.
    Reviewers evaluate producers' proposals and issue ACK/NACK.
    The tester has a dual role: producer (test artifacts) AND
    reviewer (evaluates coder's work by running tests).
    """

    def __init__(self, edges: list[ReviewEdge] | None = None) -> None:
        self._edges: list[ReviewEdge] = list(edges or [])
        self._producer_roles: set[str] = set()
        self._reviewer_roles: set[str] = set()
        for edge in self._edges:
            self._producer_roles.add(edge.producer_role)
            self._reviewer_roles.add(edge.reviewer_role)

    @property
    def edges(self) -> list[ReviewEdge]:
        return list(self._edges)

    def add_edge(self, edge: ReviewEdge) -> None:
        """Add a review edge."""
        self._edges.append(edge)
        self._producer_roles.add(edge.producer_role)
        self._reviewer_roles.add(edge.reviewer_role)

    def reviewers_for(self, producer: str) -> list[str]:
        """Get all reviewer roles assigned to review a producer."""
        return [e.reviewer_role for e in self._edges if e.producer_role == producer]

    def producers_for(self, reviewer: str) -> list[str]:
        """Get all producer roles that a reviewer is assigned to review."""
        return [e.producer_role for e in self._edges if e.reviewer_role == reviewer]

    def is_reviewer(self, role: str) -> bool:
        """Check if a role acts as a reviewer in this graph."""
        return role in self._reviewer_roles

    def is_producer(self, role: str) -> bool:
        """Check if a role acts as a producer in this graph."""
        return role in self._producer_roles

    def is_dual_role(self, role: str) -> bool:
        """Check if a role is both a producer and a reviewer."""
        return self.is_producer(role) and self.is_reviewer(role)

    def critical_reviewers_for(self, producer: str) -> list[str]:
        """Get reviewer roles with critical criticality for a producer."""
        return [
            e.reviewer_role
            for e in self._edges
            if e.producer_role == producer and e.criticality == ReviewCriticality.CRITICAL
        ]

    def advisory_reviewers_for(self, producer: str) -> list[str]:
        """Get reviewer roles with advisory criticality for a producer."""
        return [
            e.reviewer_role
            for e in self._edges
            if e.producer_role == producer and e.criticality == ReviewCriticality.ADVISORY
        ]

    def wake_only_producers_for(self, reviewer: str) -> set[str]:
        """Producers a reviewer reaches via a wake-only edge.

        A wake-only edge drives the event-pump wake-wire but carries no
        review obligation — the reviewer never casts a verdict on it
        (#3381, the de-roled simplifier). These producers must be excluded
        from pending-review derivation and from the reviewer confirm guards
        so the de-roled reviewer is never assigned an ``ack`` it can no
        longer satisfy, and can confirm without a verdict it will never
        cast.
        """
        return {e.producer_role for e in self._edges if e.reviewer_role == reviewer and e.wake_only}

    def get_edge(self, reviewer: str, producer: str) -> ReviewEdge | None:
        """Get a specific review edge."""
        for e in self._edges:
            if e.reviewer_role == reviewer and e.producer_role == producer:
                return e
        return None

    def demote_edges_for_reviewer(
        self,
        reviewer: str,
        new_criticality: ReviewCriticality | None = None,
    ) -> list[str]:
        """Demote all CRITICAL edges for a reviewer to a new criticality.

        Args:
            reviewer: Reviewer role whose edges should be demoted.
            new_criticality: Target criticality (defaults to ADVISORY).

        Returns:
            List of producer roles whose edges were demoted.
        """
        if new_criticality is None:
            new_criticality = ReviewCriticality.ADVISORY
        demoted: list[str] = []
        new_edges: list[ReviewEdge] = []
        for e in self._edges:
            if e.reviewer_role == reviewer and e.criticality == ReviewCriticality.CRITICAL:
                new_edges.append(ReviewEdge(e.reviewer_role, e.producer_role, new_criticality))
                demoted.append(e.producer_role)
            else:
                new_edges.append(e)
        self._edges = new_edges
        return demoted

    def remove_edge(self, reviewer: str, producer: str) -> bool:
        """Remove a review edge. Returns True if edge was found and removed."""
        for i, e in enumerate(self._edges):
            if e.reviewer_role == reviewer and e.producer_role == producer:
                self._edges.pop(i)
                # Recalculate role sets
                self._producer_roles = {e.producer_role for e in self._edges}
                self._reviewer_roles = {e.reviewer_role for e in self._edges}
                return True
        return False

    def all_roles(self) -> set[str]:
        """Get all roles participating in the graph."""
        return self._producer_roles | self._reviewer_roles

    def producer_roles(self) -> set[str]:
        """Get all roles that act as producers in the graph.

        Returned as a snapshot copy so callers can mutate or iterate without
        risking concurrent modification of the internal set.
        """
        return set(self._producer_roles)

    def reviewer_roles(self) -> set[str]:
        """Get all roles that act as reviewers in the graph.

        Returned as a snapshot copy so callers can mutate or iterate without
        risking concurrent modification of the internal set.
        """
        return set(self._reviewer_roles)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the graph."""
        return {
            "edges": [e.to_dict() for e in self._edges],
            "producers": sorted(self._producer_roles),
            "reviewers": sorted(self._reviewer_roles),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewGraph:
        """Deserialize a graph."""
        edges = [
            ReviewEdge(
                reviewer_role=e["reviewer_role"],
                producer_role=e["producer_role"],
                criticality=ReviewCriticality(e.get("criticality", "critical")),
                wake_only=bool(e.get("wake_only", False)),
            )
            for e in data.get("edges", [])
        ]
        return cls(edges)


def get_default_refine_graph() -> ReviewGraph:
    """Get the default review graph for the refine phase.

    Review adjacency per the phase-role mappings:
    - reviewer_refine reviews refiner (critical)
    - reviewer_agent_design reviews refiner (critical)
    - first_principles_reviewer reviews refiner (critical)

    Producers: refiner
    Reviewers: reviewer_refine, reviewer_agent_design, first_principles_reviewer
    """
    return ReviewGraph(
        [
            # reviewer_refine reviews refiner (critical)
            ReviewEdge("reviewer_refine", "refiner", ReviewCriticality.CRITICAL),
            # reviewer_agent_design reviews refiner (critical)
            ReviewEdge("reviewer_agent_design", "refiner", ReviewCriticality.CRITICAL),
            # first_principles_reviewer reviews refiner (critical). The CRITICAL
            # edge is the "must weigh in" lever: refine consensus cannot close
            # until this role has reviewed and ACKed the refiner. It does NOT
            # NACK on first-principles grounds — premise/direction concerns are
            # the operator's call, not the refiner's to fix (the seed is
            # operator-owned), so it ACKs and raises any redirect as a
            # phase-scoped HITL decision, which independently holds the
            # refine→plan completion gate open until the operator resolves it.
            ReviewEdge("first_principles_reviewer", "refiner", ReviewCriticality.CRITICAL),
            # The simplifier produces the human-focused analysis companion
            # (faithful + jargon-free), gated CRITICAL by reviewer_refine.
            # It is a PRODUCER ONLY (#3381): the propose-arm wakes it to
            # write the companion (it self-gates on the refiner's draft
            # existing), and it casts no verdict on anyone. It retains a
            # WAKE-ONLY advisory edge over the refiner only as a structural
            # marker; wake_only carries no review obligation — it excludes
            # the edge from pending-review derivation and the confirm guards
            # so the simplifier is never derived a spawn-able ``ack`` it
            # cannot satisfy and can confirm its own companion without ever
            # voting on the refiner. (See the ReviewEdge.wake_only docstring
            # for why the propose-arm, not this edge, is the wake.)
            ReviewEdge("reviewer_refine", "simplifier", ReviewCriticality.CRITICAL),
            ReviewEdge("simplifier", "refiner", ReviewCriticality.ADVISORY, wake_only=True),
        ]
    )


def get_default_plan_graph() -> ReviewGraph:
    """Get the default review graph for the plan phase.

    Review adjacency per the phase-role mappings (issue #2809):
    - reviewer_plan reviews architect (critical) — structural lens
    - reviewer_plan reviews task_planner (critical) — structural lens
    - reviewer_plan reviews risk_analyst (advisory) — risk register
      still reviewable as a producer artifact
    - risk_analyst reviews architect (critical) — risk lens, dual-role
    - risk_analyst reviews task_planner (critical) — risk lens, dual-role

    Plan-phase consensus therefore requires **both** ``reviewer_plan``
    and ``risk_analyst`` to ACK every CRITICAL producer (architect,
    task_planner). The two lenses catch what one wouldn't: ``reviewer_plan``
    audits structure (slice DAG shape, slice_size, role assignments,
    test strategy, rollback, PR block); ``risk_analyst`` audits the
    risk surface (what could go wrong with this design; blocking
    concerns). ``risk_analyst`` is dual-role — it produces the risk
    register and also reviews its upstream peers, mirroring the
    implement-phase ``tester`` pattern (#2749).

    Producers: architect, task_planner, risk_analyst
    Reviewers: reviewer_plan, risk_analyst (dual-role)
    """
    return ReviewGraph(
        [
            # reviewer_plan reviews architect (critical) — structural lens
            ReviewEdge("reviewer_plan", "architect", ReviewCriticality.CRITICAL),
            # reviewer_plan reviews task_planner (critical) — structural lens
            ReviewEdge("reviewer_plan", "task_planner", ReviewCriticality.CRITICAL),
            # reviewer_plan reviews risk_analyst (advisory) — risk
            # register still reviewable as a producer artifact
            ReviewEdge("reviewer_plan", "risk_analyst", ReviewCriticality.ADVISORY),
            # risk_analyst reviews architect (critical — risk lens, #2809)
            ReviewEdge("risk_analyst", "architect", ReviewCriticality.CRITICAL),
            # risk_analyst reviews task_planner (critical — risk lens, #2809)
            ReviewEdge("risk_analyst", "task_planner", ReviewCriticality.CRITICAL),
            # The simplifier produces the human-focused plan companion,
            # gated CRITICAL by reviewer_plan. Wake-only like the refine-phase
            # simplifier: a PRODUCER ONLY (#3381) woken to write the companion
            # by the propose-arm (self-gating on task_planner's draft), casting
            # no verdict. It retains a WAKE-ONLY advisory edge over task_planner
            # only as a structural marker; wake_only excludes it from
            # pending-review derivation and the confirm guards (see the refine
            # graph above and the ReviewEdge.wake_only docstring).
            ReviewEdge("reviewer_plan", "simplifier", ReviewCriticality.CRITICAL),
            ReviewEdge("simplifier", "task_planner", ReviewCriticality.ADVISORY, wake_only=True),
        ]
    )


def get_default_implement_graph() -> ReviewGraph:
    """Get the default review graph for the implement phase.

    Review adjacency per the BRC spec:
    - reviewer_code reviews coder and tester (critical)
    - reviewer_code_holistic reviews coder and tester (critical) — issue
      #2126: distinct CRITICAL role so a holistic NACK on architectural
      coherence stands on its own.
    - reviewer_contract reviews coder, tester, and documenter (critical)
    - tester reviews coder (critical, implicitly via tests and lint/type-checks)
    - reviewer_security reviews coder and tester (critical) — lens reviewer
    - reviewer_concurrency reviews coder and tester (critical) — lens reviewer

    Issue #2139 promoted ``reviewer_security`` and
    ``reviewer_concurrency`` from ADVISORY to CRITICAL: a NACK from
    either lens now blocks consensus until the producer re-proposes,
    closing #1997.

    Issue #3114 extended ``reviewer_contract`` from a single coder edge
    to a CRITICAL edge to every producer: the contract enforcer's ACK is
    gated on the producer's contract task rows being complete (see
    ``routes/signals.py:_contract_completeness_rejection``), and that
    gate only blocks producers the enforcer actually reviews. Before
    this, a documenter's contract rows could go entirely undelivered —
    its only reviewer was advisory — and the slice still closed.

    Producers: coder, tester, documenter
    Reviewers: reviewer_code, reviewer_code_holistic, reviewer_contract,
    tester (dual-role), reviewer_security, reviewer_concurrency
    """
    return ReviewGraph(
        [
            # reviewer_code reviews coder (critical)
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            # reviewer_code reviews tester (critical)
            ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
            # reviewer_code_holistic reviews coder (critical — issue #2126)
            ReviewEdge("reviewer_code_holistic", "coder", ReviewCriticality.CRITICAL),
            # reviewer_code_holistic reviews tester (critical — issue #2126)
            ReviewEdge("reviewer_code_holistic", "tester", ReviewCriticality.CRITICAL),
            # reviewer_contract reviews coder (critical)
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
            # reviewer_contract reviews tester and documenter (critical —
            # #3114): the contract enforcer needs a blocking edge to EVERY
            # producer or a producer's contract rows can go undelivered
            # with no reviewer able to refuse.
            ReviewEdge("reviewer_contract", "tester", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_contract", "documenter", ReviewCriticality.CRITICAL),
            # tester reviews coder (critical — via writing/running tests and lint/type-checks)
            ReviewEdge("tester", "coder", ReviewCriticality.CRITICAL),
            # reviewer_code reviews documenter (advisory)
            ReviewEdge("reviewer_code", "documenter", ReviewCriticality.ADVISORY),
            # reviewer_security reviews coder (critical — security lens, #2139)
            ReviewEdge("reviewer_security", "coder", ReviewCriticality.CRITICAL),
            # reviewer_security reviews tester (critical — security lens, #2139)
            ReviewEdge("reviewer_security", "tester", ReviewCriticality.CRITICAL),
            # reviewer_concurrency reviews coder (critical — concurrency lens, #2139)
            ReviewEdge("reviewer_concurrency", "coder", ReviewCriticality.CRITICAL),
            # reviewer_concurrency reviews tester (critical — concurrency lens, #2139)
            ReviewEdge("reviewer_concurrency", "tester", ReviewCriticality.CRITICAL),
        ]
    )


# Phase-to-graph mapping for convenient lookup
_PHASE_GRAPHS: dict[str, ReviewGraph] = {}


_DEFAULT_PHASE_GRAPH_FACTORIES: dict[str, Callable[[], ReviewGraph]] = {
    "refine": get_default_refine_graph,
    "plan": get_default_plan_graph,
    "implement": get_default_implement_graph,
}


from egg_contracts.agent_roles import EGG_ONLY_REVIEWER_NAMES as _EGG_ONLY_REVIEWERS
from egg_contracts.agent_roles import EGG_REPO as _EGG_REPO


def get_review_graph_for_phase(
    phase: str,
    repo: str | None = None,
    *,
    changed_files: Iterable[str] | None = None,
    repo_root: str | None = None,
) -> ReviewGraph:
    """Get the review graph for a pipeline phase.

    Returns the default review graph for refine, plan, and implement phases.
    Other phases return an empty graph unless a custom graph has been registered.

    Args:
        phase: Pipeline phase name.
        repo: Repository in owner/name format. When provided, egg-specific
            reviewer roles are excluded for non-egg repos.
        changed_files: Optional per-slice changed-file set (#3523 S6). This is
            the single seam through which the deterministic risk router
            (:mod:`risk_router`) reaches EVERY consumer of the review graph:
            callers that know the slice's changed files pass them here, and the
            gating is applied ONCE at this chokepoint rather than forked into
            each caller. ``None`` (the default) preserves the pre-#3523 graph
            byte-for-byte regardless of the flag, so non-slice callers are
            untouched. Gating only ever *narrows* the implement-phase critical
            lens set — never refine/plan — and always honours the router's HARD
            floors (no-match => full graph + loud warning; security lens
            un-gatable on auth/session/input-boundary paths).
        repo_root: Optional repo root for resolving ``.egg/review-risk.yaml``
            (defaults to the process CWD via :func:`risk_router.default_config_path`).
    """
    if phase in _PHASE_GRAPHS:
        graph = _PHASE_GRAPHS[phase]
    else:
        factory = _DEFAULT_PHASE_GRAPH_FACTORIES.get(phase)
        graph = factory() if factory is not None else ReviewGraph()

    # Strip egg-specific reviewers for non-egg repos.
    # Work on a copy to avoid mutating a registered singleton from _PHASE_GRAPHS.
    if repo is not None and repo != _EGG_REPO:
        graph = ReviewGraph(list(graph.edges))
        for role in _EGG_ONLY_REVIEWERS:
            for producer in graph.producers_for(role):
                graph.remove_edge(role, producer)

    # Risk-router gating (#3523 S6). Applied last, only for the implement phase
    # and only when a caller threads the slice's changed-file set. The flag
    # defaults ``off``, so this is inert unless an operator opts in.
    if changed_files is not None and phase == "implement":
        graph = _maybe_gate_graph_by_risk(graph, changed_files, repo_root=repo_root)

    return graph


def register_phase_graph(phase: str, graph: ReviewGraph) -> None:
    """Register a custom review graph for a phase."""
    _PHASE_GRAPHS[phase] = graph


# ---------------------------------------------------------------------------
# Risk-router wiring (#3523 S6 / task-6-1)
# ---------------------------------------------------------------------------
#
# Slice-5 (``risk_router.py``) is the PURE half: it maps a slice's changed-file
# set to (lenses, tier, stance) against ``.egg/review-risk.yaml`` and encodes
# the HARD floors, but nothing imports it. This section is the WIRING half: it
# threads the router into the single review-graph resolution chokepoint
# (:func:`get_review_graph_for_phase`) and the effort plumbing
# (``agent_model_resolution.resolve_agent_model`` imports :func:`risk_router_mode`
# and :func:`resolve_risk_decision` from here).
#
# Everything rides ONE staged flag, ``EGG_RISK_ROUTER``, resolved EXACTLY like
# ``slice_green_gate.green_gate_mode()`` (``off`` default, unknown => ``off``):
#   * ``off``  — inert. The live graph + efforts are byte-identical to legacy.
#   * ``log``  — compute the would-be gated graph / tier / effort and record it
#                (:func:`risk_route_log_record` + a structured log line), but
#                run the UNCHANGED full graph. The soak mode.
#   * ``on``   — apply the router's lens gating + effort.
#
# The HARD floors are re-asserted at THIS wiring layer, not merely trusted from
# the pure core: a config that fails to load falls open to the full graph
# (missing config must never mean less review), and the security lens is never
# dropped off a protected path even if a future edit to the pure core regressed.

RISK_ROUTER_ENV_VAR = "EGG_RISK_ROUTER"

_RISK_ROUTER_ENABLED_VALUES = frozenset({"on", "1", "true", "yes"})
_RISK_ROUTER_LOG_VALUES = frozenset({"log", "log-only", "log_only"})


def risk_router_mode() -> Literal["off", "log", "on"]:
    """Resolve the ``EGG_RISK_ROUTER`` switch to ``off`` / ``log`` / ``on``.

    Resolved EXACTLY like ``slice_green_gate.green_gate_mode()``: an unknown
    value resolves to ``off`` so an operator typo degrades to "router does
    nothing" (full graph, legacy effort), never to "silently review less".
    """
    raw = os.environ.get(RISK_ROUTER_ENV_VAR, "off").strip().lower()
    if raw in _RISK_ROUTER_ENABLED_VALUES:
        return "on"
    if raw in _RISK_ROUTER_LOG_VALUES:
        return "log"
    return "off"


def resolve_risk_decision(
    changed_files: Iterable[str],
    *,
    repo_root: str | None = None,
) -> RiskRouteDecision | None:
    """Load ``.egg/review-risk.yaml`` and route ``changed_files`` (fail-open).

    Shared by the graph-gating seam here and the effort seam in
    ``agent_model_resolution``. Returns ``None`` when the config cannot be
    loaded — a bad or missing risk config must never *narrow* review, so both
    callers fall back to the full graph / legacy effort on ``None`` (the HARD
    "missing config never means less review" floor, enforced at the wiring
    layer). A successfully-loaded config always yields a decision, including
    the router's own no-match full-graph-plus-warning floor.
    """
    from risk_router import default_config_path, load_risk_config, route_slice

    try:
        config: RiskConfig = load_risk_config(default_config_path(repo_root))
    except Exception as exc:  # noqa: BLE001 — a bad config fails OPEN to full review
        logger.warning(
            "risk_router: review-risk.yaml failed to load; falling back to the "
            "FULL review graph + legacy effort (fail-open). See #3523 S6.",
            error=str(exc),
        )
        return None
    return route_slice(changed_files, config)


def _gate_able_lenses() -> frozenset[str]:
    """The implement-phase critical lens universe the router may narrow.

    Deliberately the SAME set as ``risk_router.FULL_IMPLEMENT_LENSES`` — a
    reviewer role NOT in this set (e.g. ``tester``, which reviews by executing
    the proposal and stays cold-start, or the advisory ``reviewer_code ->
    documenter`` edge's producer) is never gated off, regardless of the router
    decision. Imported lazily so this module stays import-light.
    """
    from risk_router import FULL_IMPLEMENT_LENSES

    return FULL_IMPLEMENT_LENSES


def apply_risk_router(graph: ReviewGraph, decision: RiskRouteDecision) -> ReviewGraph:
    """Return a copy of ``graph`` narrowed to the router's lens set (pure).

    Only edges whose ``reviewer_role`` is in the gate-able implement lens
    universe AND absent from ``decision.lenses`` are dropped; every other edge
    (tester, advisory documenter edges, any non-implement reviewer) is kept
    verbatim. The security lens is re-asserted un-gatable at this layer: if the
    decision forced security on (a protected path), ``reviewer_security`` edges
    are never dropped even if some upstream bug omitted it from ``decision.lenses``.
    """
    keep_lenses = set(decision.lenses)
    from risk_router import REVIEWER_SECURITY

    if decision.forced_security:
        keep_lenses.add(REVIEWER_SECURITY)
    gate_able = _gate_able_lenses()

    kept: list[ReviewEdge] = []
    for edge in graph.edges:
        if edge.reviewer_role in gate_able and edge.reviewer_role not in keep_lenses:
            continue
        kept.append(edge)
    return ReviewGraph(kept)


def risk_route_log_record(graph: ReviewGraph, decision: RiskRouteDecision) -> dict[str, Any]:
    """A JSON-serializable would-be-gating record for ``log`` mode (pure).

    In ``log`` mode the caller records this alongside the unchanged full graph
    so an operator can compare what the router *would* have done — which lenses
    it would drop, the risk tier, the effort it would pin, and the stance —
    before flipping the flag to ``on``. Real per-wave *token* cost is captured
    separately by the gateway/LiteLLM per-session cost logging (#3523 §5); the
    dropped-lens count here is the structural cost proxy the router itself owns.
    """
    gated = apply_risk_router(graph, decision)
    full_reviewers = graph.reviewer_roles()
    gated_reviewers = gated.reviewer_roles()
    dropped = sorted(full_reviewers - gated_reviewers)
    return {
        "mode": "log",
        "risk_tier": decision.tier.name.lower(),
        "effort": decision.effort,
        "stance": decision.stance.value if decision.stance is not None else None,
        "lenses": sorted(decision.lenses),
        "dropped_lenses": dropped,
        "dropped_lens_count": len(dropped),
        "unrouted": decision.unrouted,
        "forced_security": decision.forced_security,
        "warnings": list(decision.warnings),
    }


def _maybe_gate_graph_by_risk(
    graph: ReviewGraph,
    changed_files: Iterable[str],
    *,
    repo_root: str | None = None,
) -> ReviewGraph:
    """Apply router gating to ``graph`` per the ``EGG_RISK_ROUTER`` mode.

    ``off`` => return ``graph`` unchanged. ``log`` => record the would-be
    gating (structured log + :func:`risk_route_log_record`) but return the
    UNCHANGED full graph. ``on`` => return the narrowed graph. A ``None``
    decision (config load failed) falls open to the full graph in every mode.
    """
    mode = risk_router_mode()
    if mode == "off":
        return graph

    # Materialize once — the iterable may be a generator, and both the log
    # line and the routing need it.
    files = list(changed_files)
    decision = resolve_risk_decision(files, repo_root=repo_root)
    if decision is None:
        return graph  # fail-open: bad/missing config never narrows review

    if mode == "log":
        record = risk_route_log_record(graph, decision)
        logger.info(
            "risk_router log-mode: would gate the implement review graph (#3523 S6)",
            changed_file_count=len(files),
            **record,
        )
        for warning in decision.warnings:
            logger.warning("risk_router: %s", warning)
        return graph

    # on: apply the gating. Surface the router's loud warnings regardless.
    for warning in decision.warnings:
        logger.warning("risk_router: %s", warning)
    return apply_risk_router(graph, decision)
