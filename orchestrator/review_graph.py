"""Asymmetric review graph for BRC consensus protocol.

Defines which roles review which other roles' work. The graph is
directed: reviewers judge producers, not the reverse. This eliminates
the circular ACK problem where a producer would be incentivized to
NACK a negative review of their own code.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


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

    def to_dict(self) -> dict[str, str]:
        return {
            "reviewer_role": self.reviewer_role,
            "producer_role": self.producer_role,
            "criticality": self.criticality.value,
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

    def get_edge(self, reviewer: str, producer: str) -> ReviewEdge | None:
        """Get a specific review edge."""
        for e in self._edges:
            if e.reviewer_role == reviewer and e.producer_role == producer:
                return e
        return None

    def all_roles(self) -> set[str]:
        """Get all roles participating in the graph."""
        return self._producer_roles | self._reviewer_roles

    def to_dict(self) -> dict[str, Any]:
        """Serialize the graph."""
        return {
            "edges": [e.to_dict() for e in self._edges],
            "producers": sorted(self._producer_roles),
            "reviewers": sorted(self._reviewer_roles),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewGraph":
        """Deserialize a graph."""
        edges = [
            ReviewEdge(
                reviewer_role=e["reviewer_role"],
                producer_role=e["producer_role"],
                criticality=ReviewCriticality(e.get("criticality", "critical")),
            )
            for e in data.get("edges", [])
        ]
        return cls(edges)


def get_default_implement_graph() -> ReviewGraph:
    """Get the default review graph for the implement phase.

    Review adjacency per the BRC spec:
    - reviewer_code reviews coder and tester (critical)
    - reviewer_contract reviews coder (critical)
    - checker reviews coder (critical)
    - tester reviews coder (critical, implicitly via tests)

    Producers: coder, tester, documenter
    Reviewers: reviewer_code, reviewer_contract, checker, tester (dual-role)
    """
    return ReviewGraph(
        [
            # reviewer_code reviews coder (critical)
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            # reviewer_code reviews tester (critical)
            ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
            # reviewer_contract reviews coder (critical)
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
            # checker reviews coder (critical)
            ReviewEdge("checker", "coder", ReviewCriticality.CRITICAL),
            # tester reviews coder (critical — via writing and running tests)
            ReviewEdge("tester", "coder", ReviewCriticality.CRITICAL),
            # reviewer_code reviews documenter (advisory)
            ReviewEdge("reviewer_code", "documenter", ReviewCriticality.ADVISORY),
        ]
    )


# Phase-to-graph mapping for convenient lookup
_PHASE_GRAPHS: dict[str, ReviewGraph] = {}


def get_review_graph_for_phase(phase: str) -> ReviewGraph:
    """Get the review graph for a pipeline phase.

    Currently only the implement phase has a defined review graph.
    Other phases return an empty graph.
    """
    if phase == "implement":
        return get_default_implement_graph()
    return _PHASE_GRAPHS.get(phase, ReviewGraph())


def register_phase_graph(phase: str, graph: ReviewGraph) -> None:
    """Register a custom review graph for a phase."""
    _PHASE_GRAPHS[phase] = graph
