"""Sparse approval matrix for BRC consensus protocol.

Tracks ACK/NACK state per review edge (reviewer->producer), supports
scoped re-evaluation when producers re-propose, and provides queries
for consensus evaluation.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from review_graph import ReviewGraph


class ApprovalState(StrEnum):
    """State of a single review edge."""

    PENDING = "pending"
    ACKED = "acked"
    NACKED = "nacked"


@dataclass
class ApprovalEntry:
    """State of a single reviewer->producer edge in the matrix."""

    reviewer_role: str
    producer_role: str
    state: ApprovalState = ApprovalState.PENDING
    version: int = 0  # Proposal version this applies to
    artifact_refs: list[str] = field(default_factory=list)  # Artifacts referenced in ACK
    nack_artifact_refs: list[str] = field(
        default_factory=list
    )  # Artifacts cited in most recent NACK
    reason: str = ""  # Reason for NACK
    timestamp: datetime | None = None
    ack_commit_sha: str = ""  # Commit SHA at time of ACK (INV-6)
    # Optional human-facing obligation attached to a conditional ACK (#1998).
    # Empty string for unconditional ACKs. Cleared on NACK and on re-propose.
    pre_merge_condition: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer_role": self.reviewer_role,
            "producer_role": self.producer_role,
            "state": self.state.value,
            "version": self.version,
            "artifact_refs": self.artifact_refs,
            "nack_artifact_refs": self.nack_artifact_refs,
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "ack_commit_sha": self.ack_commit_sha,
            "pre_merge_condition": self.pre_merge_condition,
        }


class ApprovalMatrix:
    """Sparse approval matrix tracking ACK/NACK per review edge.

    Keyed by (reviewer_role, producer_role). Initialized from a
    ReviewGraph — only edges defined in the graph are tracked.
    """

    def __init__(self, graph: ReviewGraph) -> None:
        self._graph = graph
        # (reviewer, producer) -> ApprovalEntry
        self._entries: dict[tuple[str, str], ApprovalEntry] = {}
        # producer -> current proposal version
        self._proposal_versions: dict[str, int] = {}
        # (reviewer, producer) -> revision count (bounded revision rounds)
        self._revision_counts: dict[tuple[str, str], int] = {}

        # Initialize entries from graph edges
        for edge in graph.edges:
            key = (edge.reviewer_role, edge.producer_role)
            self._entries[key] = ApprovalEntry(
                reviewer_role=edge.reviewer_role,
                producer_role=edge.producer_role,
            )
            self._revision_counts[key] = 0

    def record_proposal(self, producer: str) -> int:
        """Record a new proposal from a producer. Returns the version number."""
        version = self._proposal_versions.get(producer, 0) + 1
        self._proposal_versions[producer] = version
        return version

    def get_proposal_version(self, producer: str) -> int:
        """Get the current proposal version for a producer."""
        return self._proposal_versions.get(producer, 0)

    def record_ack(
        self,
        reviewer: str,
        producer: str,
        version: int,
        artifact_refs: list[str] | None = None,
        commit_sha: str = "",
        pre_merge_condition: str = "",
    ) -> ApprovalEntry:
        """Record an ACK from a reviewer for a producer's proposal.

        ``pre_merge_condition`` (optional, issue #1998) carries a human-facing
        obligation attached to a conditional ACK — e.g. "a human must ``git mv
        old/path new/path`` before merging". Empty string means an
        unconditional ACK. The condition is scoped to this (reviewer, producer,
        version) edge and is cleared on NACK and re-propose.
        """
        key = (reviewer, producer)
        entry = self._entries.get(key)
        if entry is None:
            raise ValueError(f"No review edge: {reviewer} -> {producer}")

        entry.state = ApprovalState.ACKED
        entry.version = version
        entry.artifact_refs = artifact_refs or []
        # Note: nack_artifact_refs intentionally NOT cleared here —
        # is_context_change_nack() needs the previous NACK's refs to
        # persist through ACK transitions for context-change detection.
        entry.reason = ""
        entry.timestamp = datetime.now(UTC)
        entry.ack_commit_sha = commit_sha
        entry.pre_merge_condition = (pre_merge_condition or "").strip()
        return entry

    def record_nack(
        self,
        reviewer: str,
        producer: str,
        version: int,
        reason: str,
        artifact_refs: list[str] | None = None,
    ) -> ApprovalEntry:
        """Record a NACK from a reviewer for a producer's proposal."""
        key = (reviewer, producer)
        entry = self._entries.get(key)
        if entry is None:
            raise ValueError(f"No review edge: {reviewer} -> {producer}")

        entry.state = ApprovalState.NACKED
        entry.version = version
        entry.artifact_refs = []
        entry.nack_artifact_refs = artifact_refs or []
        entry.reason = reason
        entry.timestamp = datetime.now(UTC)
        # A NACK supersedes any prior conditional ACK on this edge — the
        # producer must re-propose, so any deferred obligation is moot (#1998).
        entry.pre_merge_condition = ""

        # Increment revision count for this edge
        self._revision_counts[key] = self._revision_counts.get(key, 0) + 1

        return entry

    def is_context_change_nack(
        self,
        reviewer: str,
        producer: str,
        new_artifact_refs: list[str],
    ) -> bool:
        """Check if a NACK references different artifacts than the previous NACK.

        Returns True if the new NACK's artifact refs don't overlap with the
        previous NACK's artifact refs for that edge, indicating a context change
        rather than genuine oscillation on the same issue.
        """
        key = (reviewer, producer)
        entry = self._entries.get(key)
        if entry is None:
            return False
        prev_refs = set(entry.nack_artifact_refs)
        new_refs = set(new_artifact_refs)
        if not prev_refs or not new_refs:
            return False
        return not bool(prev_refs & new_refs)

    def is_fully_acked(self, producer: str) -> bool:
        """Check if all critical reviewers have ACKed the producer's latest proposal.

        Advisory reviewers are excluded from the check — their ACK is
        informational but does not block consensus.
        """
        latest_version = self._proposal_versions.get(producer, 0)
        if latest_version == 0:
            return False

        reviewers = self._graph.critical_reviewers_for(producer)
        for reviewer in reviewers:
            key = (reviewer, producer)
            entry = self._entries.get(key)
            if (
                entry is None
                or entry.state != ApprovalState.ACKED
                or entry.version != latest_version
            ):
                return False
        return True

    def is_fully_confirmed(self, confirmed_roles: set[str]) -> bool:
        """Check if all roles in the graph have confirmed."""
        return self._graph.all_roles().issubset(confirmed_roles)

    def get_blocking_edges(self, producer: str) -> list[ApprovalEntry]:
        """Get review edges that are blocking consensus for a producer."""
        latest_version = self._proposal_versions.get(producer, 0)
        blocking = []
        for reviewer in self._graph.reviewers_for(producer):
            key = (reviewer, producer)
            entry = self._entries.get(key)
            if entry and (entry.state != ApprovalState.ACKED or entry.version != latest_version):
                blocking.append(entry)
        return blocking

    def get_all_blocking_edges(self) -> list[ApprovalEntry]:
        """Get all blocking edges across all producers."""
        blocking = []
        for producer in self._graph._producer_roles:
            blocking.extend(self.get_blocking_edges(producer))
        return blocking

    def invalidate_ack(
        self,
        reviewer: str,
        producer: str,
    ) -> bool:
        """Invalidate a reviewer's ACK for scoped re-evaluation.

        Returns True if an ACK was invalidated, False if not applicable.
        """
        key = (reviewer, producer)
        entry = self._entries.get(key)
        if entry and entry.state == ApprovalState.ACKED:
            entry.state = ApprovalState.PENDING
            entry.artifact_refs = []
            entry.reason = ""
            # Invalidation drops the ACK entirely; any condition that rode on
            # that ACK goes with it (#1998). The reviewer must re-ACK to
            # re-attach a condition if they still want one.
            entry.pre_merge_condition = ""
            return True
        return False

    def invalidate_overlapping_acks(
        self,
        producer: str,
        changed_artifacts: list[str],
    ) -> list[str]:
        """Invalidate ACKs whose referenced artifacts overlap with changed artifacts.

        Used for scoped re-evaluation: when a producer re-proposes after a NACK,
        only ACKs referencing the changed artifacts need re-review.

        Returns list of reviewer roles whose ACKs were invalidated.
        """
        changed_set = set(changed_artifacts)
        invalidated_reviewers = []

        for reviewer in self._graph.reviewers_for(producer):
            key = (reviewer, producer)
            entry = self._entries.get(key)
            if entry and entry.state == ApprovalState.ACKED:
                # Check if any of the ACK's referenced artifacts overlap
                ack_refs = set(entry.artifact_refs)
                if ack_refs & changed_set:
                    self.invalidate_ack(reviewer, producer)
                    invalidated_reviewers.append(reviewer)

        return invalidated_reviewers

    def revision_count(self, reviewer: str, producer: str) -> int:
        """Get the revision count for a specific review edge."""
        return self._revision_counts.get((reviewer, producer), 0)

    def get_entry(self, reviewer: str, producer: str) -> ApprovalEntry | None:
        """Get a specific approval entry."""
        return self._entries.get((reviewer, producer))

    def has_reviewed(self, reviewer: str, producer: str) -> bool:
        """Check if a reviewer has submitted any evaluation for a producer."""
        entry = self._entries.get((reviewer, producer))
        return entry is not None and entry.state != ApprovalState.PENDING

    def get_nack_entries_for(self, producer: str) -> list[tuple[str, ApprovalEntry]]:
        """Get all NACKed entries for a producer.

        Returns list of (reviewer_role, ApprovalEntry) tuples where the
        entry state is NACKED.
        """
        nacked = []
        for reviewer in self._graph.reviewers_for(producer):
            key = (reviewer, producer)
            entry = self._entries.get(key)
            if entry and entry.state == ApprovalState.NACKED:
                nacked.append((reviewer, entry))
        return nacked

    def has_unresolved_nacks_as_producer(self, producer: str) -> bool:
        """Check if any reviewer has NACKed the producer at the current proposal version.

        Returns True if the producer has unresolved NACKs — i.e., a reviewer
        NACKed at the same version as the current proposal, meaning the
        producer hasn't re-proposed since the NACK.
        """
        current_version = self._proposal_versions.get(producer, 0)
        for reviewer in self._graph.reviewers_for(producer):
            key = (reviewer, producer)
            entry = self._entries.get(key)
            if entry and entry.state == ApprovalState.NACKED and entry.version == current_version:
                return True
        return False

    def get_pre_merge_conditions(self) -> list[dict[str, Any]]:
        """Return conditional-ACK obligations scoped to the latest proposal.

        A condition only counts if the reviewer ACKed *the current* proposal
        version. Stale conditions (attached to a superseded version) are
        dropped — the producer has re-proposed and the reviewer hasn't
        re-asserted the obligation on the new version.

        Returns a list of dicts: ``{reviewer, producer, condition, version}``,
        one per active conditional ACK. Callers (e.g. the PR-body builder,
        HITL gate) surface these to humans so merge-time obligations aren't
        silently dropped (#1998).
        """
        conditions: list[dict[str, Any]] = []
        for (reviewer, producer), entry in self._entries.items():
            if entry.state != ApprovalState.ACKED:
                continue
            if not entry.pre_merge_condition:
                continue
            latest_version = self._proposal_versions.get(producer, 0)
            if entry.version != latest_version:
                # Condition rode on a superseded proposal — the reviewer
                # hasn't re-attached it to the current version.
                continue
            conditions.append(
                {
                    "reviewer": reviewer,
                    "producer": producer,
                    "condition": entry.pre_merge_condition,
                    "version": entry.version,
                }
            )
        return conditions

    def get_latest_entry_timestamp(self) -> datetime | None:
        """Return the timestamp of the most recent ACK or NACK across all edges.

        Used by the BRC progress gate (#2243) to detect reviewer activity in
        the window before opening an HITL consensus-failure decision. Returns
        None if no edge has ever transitioned out of PENDING.
        """
        latest: datetime | None = None
        for entry in self._entries.values():
            ts = entry.timestamp
            if ts is None:
                continue
            if latest is None or ts > latest:
                latest = ts
        return latest

    def get_latest_review_versions(self, reviewer: str) -> dict[str, int]:
        """Get the version of each review the reviewer has submitted.

        Returns a dict mapping producer_role to the version number of the
        reviewer's latest ACK/NACK. Entries still in PENDING state are
        excluded.
        """
        versions: dict[str, int] = {}
        for producer in self._graph.producers_for(reviewer):
            key = (reviewer, producer)
            entry = self._entries.get(key)
            if entry and entry.state != ApprovalState.PENDING:
                versions[producer] = entry.version
        return versions

    def to_dict(self) -> dict[str, Any]:
        """Serialize the matrix."""
        return {
            "entries": {f"{k[0]}->{k[1]}": v.to_dict() for k, v in self._entries.items()},
            "proposal_versions": dict(self._proposal_versions),
            "revision_counts": {f"{k[0]}->{k[1]}": v for k, v in self._revision_counts.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], graph: ReviewGraph) -> "ApprovalMatrix":
        """Deserialize a matrix."""
        matrix = cls(graph)

        matrix._proposal_versions = data.get("proposal_versions", {})

        for key_str, entry_data in data.get("entries", {}).items():
            reviewer, producer = key_str.split("->")
            key = (reviewer, producer)
            if key in matrix._entries:
                matrix._entries[key] = ApprovalEntry(
                    reviewer_role=reviewer,
                    producer_role=producer,
                    state=ApprovalState(entry_data["state"]),
                    version=entry_data.get("version", 0),
                    artifact_refs=entry_data.get("artifact_refs", []),
                    nack_artifact_refs=entry_data.get("nack_artifact_refs", []),
                    reason=entry_data.get("reason", ""),
                    timestamp=(
                        datetime.fromisoformat(entry_data["timestamp"])
                        if entry_data.get("timestamp")
                        else None
                    ),
                    ack_commit_sha=entry_data.get("ack_commit_sha", ""),
                    pre_merge_condition=entry_data.get("pre_merge_condition", ""),
                )

        for key_str, count in data.get("revision_counts", {}).items():
            reviewer, producer = key_str.split("->")
            matrix._revision_counts[(reviewer, producer)] = count

        return matrix
