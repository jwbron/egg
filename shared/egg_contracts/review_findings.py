"""Versioned structured-finding schema for BRC review verdicts (#3523, slice-2).

Today a reviewer's NACK is free-form prose. An unreproducible or vague
objection still burns one of the producer's ``max_revision_rounds``, can
trip flip-flop lockout, and escalates healthy slices to HITL. This module
replaces the prose-only verdict *input* with a versioned, structured
**Finding**: the reviewer emits findings, and (in a later slice, #3523 S3)
orchestrator-side code computes the edge verdict from them. Models own
judgment (what to flag, severity, confidence, prose); code owns mechanics
(dedup, verdict, rendering).

This slice (S2) defines the schema and a boundary validator ONLY. Nothing
here is wired into the verdict/consensus path yet — that is task-3-1.

Design mirrors two existing conventions:

- **Module shape** mirrors ``shared/egg_contracts/impasse.py`` — a pydantic
  ``BaseModel`` schema with ``StrEnum`` codes, ``to_dict`` / ``from_dict``
  round-trips, and an ``__all__`` export re-exported from the package
  ``__init__``.
- **Boundary validator** mirrors ``orchestrator/attestation_schemas.py`` —
  a ``validate_*`` entry point that parses an untrusted wire payload into a
  validated model at the message boundary.

The **core** finding shape (``anchor`` file/line, ``summary``, required
``failure_scenario``) mirrors the Claude Code ``/review`` finding shape and
the slice-1 verification ladder in ``shared/prompts/code-review-criteria.md``
(CONFIRMED / PLAUSIBLE / REFUTED, "blocking must reproduce"). egg's
extensions (``role`` lens, ``severity``, ``confidence``, ``suggested_patch``,
``pre_merge_obligation``, mechanism-convergence ``converged_roles``) are
added additively on top.

**Additive evolution.** Every message carries ``schema_version``. New fields
are added with defaults and the version is bumped; existing fields are never
repurposed or removed. Unknown extra fields are *tolerated* (``extra="allow"``)
so a newer producer talking to an older validator does not error and the
extra data round-trips.

**The blocking-eligibility rule.** A ``blocking`` finding without a
``failure_scenario`` is *representable* (constructing it never raises) but is
NOT a valid blocking finding — it is flagged non-blocking-eligible via
:meth:`Finding.is_blocking_eligible` / :meth:`Finding.effective_severity`,
which the verdict computation (S3) will consult. The boundary validator never
raises on this condition; it surfaces it as a warning. This encodes the
"blocking must reproduce" companion rule as data, not prose.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Current wire version. Bump when adding fields; evolve additively only.
FINDINGS_SCHEMA_VERSION = 1


class FindingSeverity(StrEnum):
    """Whether a finding blocks the producer or is advisory-only."""

    BLOCKING = "blocking"
    """The producer must address this before the edge can ACK. Only valid
    when a ``failure_scenario`` is present (see
    :meth:`Finding.is_blocking_eligible`) — the "blocking must reproduce"
    rule from the slice-1 verification ladder.
    """

    ADVISORY = "advisory"
    """Non-blocking. Surfaces as a pre-merge obligation / conditional-ACK
    signal rather than a revision-forcing NACK. A PLAUSIBLE-but-unconfirmed
    candidate is carried here rather than dropped.
    """


class FindingConfidence(StrEnum):
    """Reviewer's confidence in the finding.

    Loosely parallels the slice-1 verification ladder: ``HIGH`` ~ CONFIRMED,
    ``MEDIUM``/``LOW`` ~ PLAUSIBLE. REFUTED candidates are dropped and never
    become findings.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingAnchor(BaseModel):
    """Where a finding points.

    Either a concrete file + line range, or a *slice-level* marker for
    cross-cutting findings that cannot be pinned to a single line (e.g. "the
    new flag is resolved inconsistently across three call sites"). A
    slice-level anchor sets ``slice_level=True`` and may leave ``path`` /
    lines unset.
    """

    model_config = ConfigDict(extra="allow")

    path: str | None = Field(
        default=None,
        description="Repo-relative file path the finding anchors to; None for slice-level.",
    )
    line_start: int | None = Field(
        default=None,
        ge=1,
        description="First line of the anchored range (1-based); None for slice-level.",
    )
    line_end: int | None = Field(
        default=None,
        ge=1,
        description="Last line of the anchored range (1-based, inclusive); None if single line or slice-level.",
    )
    slice_level: bool = Field(
        default=False,
        description=(
            "True for a cross-cutting finding not pinnable to one location. "
            "When True, path/line fields may be absent."
        ),
    )

    def to_dict(self) -> dict[str, Any]:
        """Round-trip-safe dict for JSON serialisation."""
        return {
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "slice_level": self.slice_level,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FindingAnchor:
        """Inverse of :meth:`to_dict` (tolerant of extra keys)."""
        return cls(
            path=data.get("path"),
            line_start=data.get("line_start"),
            line_end=data.get("line_end"),
            slice_level=bool(data.get("slice_level", False)),
        )


class Finding(BaseModel):
    """A single structured review finding.

    Emitted by one reviewer lens. The verdict is NOT decided here — a later
    slice computes the edge verdict from a collection of findings (any
    blocking-eligible finding => NACK; advisory-only => conditional ACK;
    none => ACK).
    """

    # Tolerate unknown fields so a newer producer does not break an older
    # validator and the extra data round-trips (additive evolution).
    model_config = ConfigDict(extra="allow")

    schema_version: int = Field(
        default=FINDINGS_SCHEMA_VERSION,
        description="Wire schema version. Bumped additively; never repurposed.",
    )
    id: str = Field(
        ...,
        min_length=1,
        description="Stable identifier for this finding within a review batch.",
    )
    role: str = Field(
        ...,
        min_length=1,
        description="The reviewer lens that produced this finding (e.g. 'reviewer_security').",
    )
    anchor: FindingAnchor = Field(
        default_factory=FindingAnchor,
        description="Where the finding points: file+line range, or a slice-level marker.",
    )
    summary: str = Field(
        ...,
        min_length=1,
        description="One-sentence statement of the finding.",
    )
    failure_scenario: str = Field(
        default="",
        description=(
            "Concrete inputs/state, then the resulting wrong output, crash, or "
            "data loss. REQUIRED for a finding to be blocking-eligible: a "
            "blocking finding without one is representable but flagged "
            "non-blocking-eligible (never a valid blocking finding, never an "
            "error). Empty is allowed for advisory findings."
        ),
    )
    severity: FindingSeverity = Field(
        default=FindingSeverity.ADVISORY,
        description="'blocking' or 'advisory'. Blocking additionally requires a failure_scenario.",
    )
    confidence: FindingConfidence = Field(
        default=FindingConfidence.MEDIUM,
        description="Reviewer confidence: high | medium | low.",
    )
    evidence: str = Field(
        default="",
        description="Quoted evidence — the triggering line, or what was checked.",
    )
    suggested_patch: str | None = Field(
        default=None,
        description="Optional suggested fix referencing real symbols. Advisory to the producer.",
    )
    pre_merge_obligation: str | None = Field(
        default=None,
        description=(
            "Optional human-performed merge-time action (mirrors the "
            "conditional-ACK pre_merge_condition). Advisory, not blocking."
        ),
    )
    converged_roles: list[str] = Field(
        default_factory=list,
        description=(
            "Additive convergence field: when mechanism-level dedup (#3523 S3) "
            "merges findings from different lenses naming the same causal "
            "mechanism, it records the >=2 producing lenses here. Empty on a "
            "freshly-emitted finding. Not consulted by any verdict code in this "
            "slice."
        ),
    )

    def is_blocking_eligible(self) -> bool:
        """True iff this finding may block the producer.

        Encodes the "blocking must reproduce" rule: a finding is
        blocking-eligible only when its severity is ``blocking`` AND it
        carries a non-empty ``failure_scenario``. A blocking-severity finding
        with no failure scenario is representable but returns False here.
        """
        return self.severity == FindingSeverity.BLOCKING and bool(self.failure_scenario.strip())

    def effective_severity(self) -> FindingSeverity:
        """Severity after applying the blocking-eligibility rule.

        A blocking finding that is not blocking-eligible (no failure
        scenario) is downgraded to ``advisory`` for verdict purposes, so it
        cannot force a revision round on vibes alone. Everything else is
        returned unchanged.
        """
        if self.severity == FindingSeverity.BLOCKING and not self.is_blocking_eligible():
            return FindingSeverity.ADVISORY
        return self.severity

    def to_dict(self) -> dict[str, Any]:
        """Round-trip-safe dict for JSON serialisation."""
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "role": self.role,
            "anchor": self.anchor.to_dict(),
            "summary": self.summary,
            "failure_scenario": self.failure_scenario,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "evidence": self.evidence,
            "suggested_patch": self.suggested_patch,
            "pre_merge_obligation": self.pre_merge_obligation,
            "converged_roles": list(self.converged_roles),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        """Inverse of :meth:`to_dict` (tolerant of missing/extra keys)."""
        raw_anchor = data.get("anchor")
        anchor = (
            FindingAnchor.from_dict(raw_anchor)
            if isinstance(raw_anchor, dict)
            else FindingAnchor()
        )
        return cls(
            schema_version=int(data.get("schema_version", FINDINGS_SCHEMA_VERSION)),
            id=data["id"],
            role=data["role"],
            anchor=anchor,
            summary=data["summary"],
            failure_scenario=data.get("failure_scenario", ""),
            severity=FindingSeverity(data.get("severity", FindingSeverity.ADVISORY.value)),
            confidence=FindingConfidence(data.get("confidence", FindingConfidence.MEDIUM.value)),
            evidence=data.get("evidence", ""),
            suggested_patch=data.get("suggested_patch"),
            pre_merge_obligation=data.get("pre_merge_obligation"),
            converged_roles=list(data.get("converged_roles") or []),
        )


class FindingsPayload(BaseModel):
    """A batch of findings emitted by one reviewer at the wire boundary.

    This is the structured replacement for the prose NACK reason. It is
    validated at the message boundary by :func:`validate_findings_payload`.
    An empty ``findings`` list is valid and represents "nothing to flag"
    (a clean ACK once the verdict path is wired in S3).
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int = Field(
        default=FINDINGS_SCHEMA_VERSION,
        description="Wire schema version for the payload envelope.",
    )
    role: str = Field(
        ...,
        min_length=1,
        description="The reviewer lens emitting this batch of findings.",
    )
    findings: list[Finding] = Field(
        default_factory=list,
        description="Zero or more findings. Empty means nothing flagged.",
    )

    def blocking_eligible_findings(self) -> list[Finding]:
        """Findings that are eligible to block (severity blocking + failure scenario)."""
        return [f for f in self.findings if f.is_blocking_eligible()]

    def non_blocking_eligible_findings(self) -> list[Finding]:
        """Blocking-severity findings that are NOT eligible to block.

        These are the findings a reviewer marked ``blocking`` but did not
        back with a ``failure_scenario`` — representable, but downgraded to
        advisory for verdict purposes. Surfaced as warnings by
        :func:`validate_findings_payload`.
        """
        return [
            f
            for f in self.findings
            if f.severity == FindingSeverity.BLOCKING and not f.is_blocking_eligible()
        ]

    def to_dict(self) -> dict[str, Any]:
        """Round-trip-safe dict for JSON serialisation."""
        return {
            "schema_version": self.schema_version,
            "role": self.role,
            "findings": [f.to_dict() for f in self.findings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FindingsPayload:
        """Inverse of :meth:`to_dict` (tolerant of missing/extra keys)."""
        raw_findings = data.get("findings") or []
        return cls(
            schema_version=int(data.get("schema_version", FINDINGS_SCHEMA_VERSION)),
            role=data["role"],
            findings=[Finding.from_dict(f) for f in raw_findings],
        )


def non_blocking_eligible_warnings(payload: FindingsPayload) -> list[str]:
    """Human-readable warnings for blocking findings that cannot block.

    One warning per blocking-severity finding lacking a ``failure_scenario``.
    Returned (not raised) so the caller can log/surface without failing the
    boundary — encoding "representable but flagged non-blocking-eligible,
    never erroring".
    """
    return [
        (
            f"Finding {f.id!r} (role {f.role!r}) is marked 'blocking' but has no "
            f"failure_scenario; it is not blocking-eligible and will be treated "
            f"as advisory."
        )
        for f in payload.non_blocking_eligible_findings()
    ]


def validate_findings_payload(data: dict[str, Any]) -> FindingsPayload:
    """Validate an untrusted findings payload at the wire boundary.

    Mirrors ``orchestrator.attestation_schemas.validate_attestation``: parse
    a raw dict into a validated model, raising ``ValueError`` on a
    *structurally* malformed payload (missing required ``role``/``id``/
    ``summary``, bad enum value, wrong types).

    It deliberately does NOT raise when a finding is marked ``blocking`` but
    lacks a ``failure_scenario``. Per the S2 acceptance, such a finding is
    representable but flagged non-blocking-eligible — inspect
    :meth:`FindingsPayload.non_blocking_eligible_findings` or
    :func:`non_blocking_eligible_warnings` for that condition.

    Args:
        data: Raw findings payload (e.g. decoded from a BRC message).

    Returns:
        The validated :class:`FindingsPayload`.

    Raises:
        ValueError: If the payload is structurally invalid.
    """
    try:
        return FindingsPayload.from_dict(data)
    except KeyError as exc:
        raise ValueError(f"Findings payload missing required field: {exc}") from exc


__all__ = [
    "FINDINGS_SCHEMA_VERSION",
    "Finding",
    "FindingAnchor",
    "FindingConfidence",
    "FindingSeverity",
    "FindingsPayload",
    "non_blocking_eligible_warnings",
    "validate_findings_payload",
]
