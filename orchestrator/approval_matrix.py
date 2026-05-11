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
    # SHA that satisfied the obligation within the same PR's diff (#2336). When
    # non-empty, the renderer demotes this entry from the merge-blocking
    # "Pre-merge Obligations" section to a "Resolved within this PR"
    # subsection — the obligation was met in-pipeline and the merger no longer
    # needs to act. Cleared on NACK and on re-propose alongside the condition.
    pre_merge_condition_resolved_in_diff: str = ""
    # Set to True when ``pre_merge_condition`` has been satisfied in-cycle —
    # e.g. another agent landed the conditioning commit on the branch (#2338).
    # Reset on every ``record_ack`` / ``record_nack`` so a re-attached
    # obligation on a later proposal version starts un-resolved.
    obligation_resolved: bool = False
    # Audit fields populated by ``mark_obligation_resolved``: who claimed
    # satisfaction, the commit they pointed at, an optional human note,
    # and when the resolution was recorded (so audit logs can sequence
    # a resolution against any later re-ACK that resets the flag).
    obligation_resolved_by: str = ""
    obligation_resolved_commit: str = ""
    obligation_resolved_note: str = ""
    obligation_resolved_at: datetime | None = None

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
            "pre_merge_condition_resolved_in_diff": (self.pre_merge_condition_resolved_in_diff),
            "obligation_resolved": self.obligation_resolved,
            "obligation_resolved_by": self.obligation_resolved_by,
            "obligation_resolved_commit": self.obligation_resolved_commit,
            "obligation_resolved_note": self.obligation_resolved_note,
            "obligation_resolved_at": (
                self.obligation_resolved_at.isoformat() if self.obligation_resolved_at else None
            ),
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
        pre_merge_condition_resolved_in_diff: str = "",
    ) -> ApprovalEntry:
        """Record an ACK from a reviewer for a producer's proposal.

        ``pre_merge_condition`` (optional, issue #1998) carries a human-facing
        obligation attached to a conditional ACK — e.g. "a human must ``git mv
        old/path new/path`` before merging". Empty string means an
        unconditional ACK. The condition is scoped to this (reviewer, producer,
        version) edge and is cleared on NACK and re-propose.

        ``pre_merge_condition_resolved_in_diff`` (optional, issue #2336) is the
        commit SHA — typically observed by the reviewer between their initial
        conditional ACK and a re-ACK on the current proposal — that satisfied
        the obligation within the same PR's diff. When non-empty, the PR-body
        renderer moves the obligation out of the merge-blocking section and
        into a "Resolved within this PR" subsection. Only meaningful when
        ``pre_merge_condition`` is also non-empty; ignored otherwise.
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
        normalized_condition = (pre_merge_condition or "").strip()
        entry.pre_merge_condition = normalized_condition
        # Resolution is meaningless without an obligation — drop it if the
        # caller passes a SHA on a plain (non-conditional) ACK.
        normalized_resolution = (pre_merge_condition_resolved_in_diff or "").strip()
        entry.pre_merge_condition_resolved_in_diff = (
            normalized_resolution if normalized_condition else ""
        )
        # A fresh ACK supersedes any prior in-cycle resolution: if the
        # reviewer re-attaches an obligation on a new version, the
        # satisfying agent must re-resolve it (#2338).
        entry.obligation_resolved = False
        entry.obligation_resolved_by = ""
        entry.obligation_resolved_commit = ""
        entry.obligation_resolved_note = ""
        entry.obligation_resolved_at = None
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
        entry.pre_merge_condition_resolved_in_diff = ""
        entry.obligation_resolved = False
        entry.obligation_resolved_by = ""
        entry.obligation_resolved_commit = ""
        entry.obligation_resolved_note = ""
        entry.obligation_resolved_at = None

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

    def seed_auto_ack_for_empty_pure_producers(self, producers_with_tasks: set[str]) -> list[str]:
        """Pre-seed proposal + critical-reviewer ACKs for pure producers
        whose role has no tasks in this slice.

        Prevents a BRC deadlock (#2581) where a producer-only slice (e.g.
        tester-only or documenter-only) leaves CODER with no work but
        still spawned: CODER's critical reviewers (REVIEWER_CODE et al.)
        have nothing to review and may NACK indefinitely, since the
        protocol requires every critical reviewer to ACK at the latest
        version.

        Behavior, per producer ``P`` in the graph not present in
        ``producers_with_tasks``:

        * Skip ``P`` if ``graph.is_dual_role(P)`` — a dual-role producer
          (e.g. TESTER also reviews CODER) must always run so it can
          discharge its reviewer responsibilities for the *other*
          producers; auto-ACKing it as a producer is fine in principle,
          but right now no role besides TESTER is dual-role, and skipping
          here keeps the rule trivially aligned with the "tester always
          runs" intent.
        * Otherwise record an empty proposal at version 1, then record
          an ACK at version 1 from **every** critical reviewer of ``P``.
          The ACK from a dual-role reviewer (e.g. TESTER reviewing
          CODER) is a starting state, not a final say: if the
          dual-role reviewer's own producer work later uncovers a need
          for ``P`` to produce something, it can NACK at version 1, which
          overrides the seeded ACK and forces ``P`` to re-propose at
          version 2 via the normal flow.

        The producer container is still spawned by the caller — this only
        pre-seeds the matrix. If the agent later proposes for real, the
        version bumps and the seeded ACKs are superseded by the normal
        flow.

        Returns the list of producer roles that were auto-ACKed (mostly
        useful for logging / tests).
        """
        auto_acked: list[str] = []
        for producer in sorted(self._graph._producer_roles):
            if producer in producers_with_tasks:
                continue
            if self._graph.is_dual_role(producer):
                continue
            version = self.record_proposal(producer)
            for reviewer in self._graph.critical_reviewers_for(producer):
                self.record_ack(reviewer, producer, version=version)
            auto_acked.append(producer)
        return auto_acked

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
            entry.pre_merge_condition_resolved_in_diff = ""
            entry.obligation_resolved = False
            entry.obligation_resolved_by = ""
            entry.obligation_resolved_commit = ""
            entry.obligation_resolved_note = ""
            entry.obligation_resolved_at = None
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

        Obligations that have been resolved in-cycle by another agent (via
        ``mark_obligation_resolved`` / ``mcp__brc__resolve_obligation``) are
        also dropped (#2338) — the conditioning work is already on the
        branch, so transcribing the obligation into the PR body or firing
        the HITL gate would be busywork.

        Returns a list of dicts: ``{reviewer, producer, condition, version,
        resolved_in_diff}``, one per active conditional ACK. ``resolved_in_diff``
        is the empty string for open obligations and the satisfying commit SHA
        when the reviewer marked the obligation resolved within the same PR
        (#2336). Callers (PR-body builder, HITL gate, live-status renderer)
        use it to demote resolved obligations out of the merge-blocking
        section.
        """
        conditions: list[dict[str, Any]] = []
        for (reviewer, producer), entry in self._entries.items():
            if entry.state != ApprovalState.ACKED:
                continue
            if not entry.pre_merge_condition:
                continue
            if entry.obligation_resolved:
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
                    "resolved_in_diff": entry.pre_merge_condition_resolved_in_diff,
                }
            )
        return conditions

    def mark_obligation_resolved(
        self,
        reviewer: str,
        producer: str,
        resolved_by: str = "",
        commit_sha: str = "",
        note: str = "",
    ) -> ApprovalEntry:
        """Mark a conditional-ACK obligation as satisfied in-cycle (#2338).

        Used when the conditioning work has already landed on the producer's
        branch — typically because another role (e.g. the tester) cherry-
        picked the satisfying commit during the same BRC cycle. The matrix
        keeps the original ``pre_merge_condition`` text for audit, but
        ``get_pre_merge_conditions`` filters this entry out so the PR-body
        builder and HITL gate see no obligation. Any subsequent ``record_ack``
        / ``record_nack`` / ``invalidate_ack`` on the edge resets the resolved
        flag, so a re-attached obligation on a later version starts fresh.

        Raises ``ValueError`` when the edge does not exist, is not in ACKED
        state, has no obligation attached, or the resolver is the producer
        themselves. The producer cannot self-resolve their own obligation —
        that would let them single-handedly bypass the reviewer's veto, and
        the second pair of eyes is the whole point of ``pre_merge_condition``.
        If the reviewer wants to drop their own obligation, the existing path
        is to re-ACK without ``pre_merge_condition``.
        """
        key = (reviewer, producer)
        entry = self._entries.get(key)
        if entry is None:
            raise ValueError(f"No review edge: {reviewer} -> {producer}")
        if entry.state != ApprovalState.ACKED:
            raise ValueError(
                f"Cannot resolve obligation on edge {reviewer} -> {producer}: "
                f"edge is in state {entry.state.value}, not ACKED"
            )
        if not entry.pre_merge_condition:
            raise ValueError(
                f"No active obligation on edge {reviewer} -> {producer} "
                "(reviewer has not attached pre_merge_condition on the "
                "current ACK)"
            )
        resolver = (resolved_by or "").strip()
        if resolver and resolver == producer:
            raise ValueError(
                f"Producer {producer!r} cannot self-resolve their own "
                "conditional-ACK obligation; the reviewer must drop the "
                "condition on re-ACK or another role must resolve."
            )
        entry.obligation_resolved = True
        entry.obligation_resolved_by = resolver
        entry.obligation_resolved_commit = (commit_sha or "").strip()
        entry.obligation_resolved_note = (note or "").strip()
        entry.obligation_resolved_at = datetime.now(UTC)
        return entry

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
    def from_dict(cls, data: dict[str, Any], graph: ReviewGraph) -> ApprovalMatrix:
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
                    pre_merge_condition_resolved_in_diff=entry_data.get(
                        "pre_merge_condition_resolved_in_diff", ""
                    ),
                    obligation_resolved=entry_data.get("obligation_resolved", False),
                    obligation_resolved_by=entry_data.get("obligation_resolved_by", ""),
                    obligation_resolved_commit=entry_data.get("obligation_resolved_commit", ""),
                    obligation_resolved_note=entry_data.get("obligation_resolved_note", ""),
                    obligation_resolved_at=(
                        datetime.fromisoformat(entry_data["obligation_resolved_at"])
                        if entry_data.get("obligation_resolved_at")
                        else None
                    ),
                )

        for key_str, count in data.get("revision_counts", {}).items():
            reviewer, producer = key_str.split("->")
            matrix._revision_counts[(reviewer, producer)] = count

        return matrix
