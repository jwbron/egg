"""Overseer calibration corpus — labelled known-normal / known-bad fixtures.

This module is the **head of the issue-#2270 overseer-overhaul chain** and the
regression bedrock every later detector plugs into (AC-3, deliverable #1). It
exposes:

* the data model — :class:`EventStreamSnapshot`, :class:`RunningAgent`,
  :class:`ExpectedFinding`, :class:`CorpusRow`, and the :class:`Finding` /
  ``Detector`` protocols a detector-under-test must satisfy;
* :func:`load_corpus` — the loader that parses ``fixtures.json`` into typed
  :class:`CorpusRow` records;
* the harness contract — :func:`match_finding`, :func:`assert_row`,
  :func:`evaluate`, and :class:`Scoreboard` — that encodes the AC-3 rule:
  a detector MUST yield ``None`` on every known-normal row and the expected
  :class:`Finding` on every known-bad row.

No production code is imported here on purpose. Slice-1 ships only the corpus
and the contract; the real detectors (and the production ``Finding`` /
``EventStreamSnapshot`` types in ``health_checks/``) arrive in slices 4 / 7 / 8
and register themselves through :func:`register_detector`. Until a detector is
registered, the harness treats its known-bad rows as ``xfail`` (see
``test_overseer_calibration.py``). Because detector outputs are matched
structurally (duck-typed on ``finding_class`` / ``severity``), the production
``Finding`` plugs straight into this harness without the corpus importing it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

_FIXTURES_PATH = Path(__file__).with_name("fixtures.json")

# Slices that deliver a real detector. A known-bad row must name one of these
# in ``delivered_in_slice`` — that is what the harness xfails until the detector
# lands and flips the row to strict (#2270 slices 4/7/8).
DETECTOR_DELIVERY_SLICES: frozenset[int] = frozenset({4, 7, 8})


# ---------------------------------------------------------------------------
# Enums — stable string vocabularies for labels, severities, finding classes.
# ---------------------------------------------------------------------------


class Label(StrEnum):
    """Whether a corpus row is a known-normal or a known-bad input."""

    KNOWN_NORMAL = "known_normal"
    KNOWN_BAD = "known_bad"


class LifecycleOwner(StrEnum):
    """Who owns the agent lifecycle at the moment the snapshot was taken.

    The ``#3230`` false-stall fix turns on this distinction: a producer
    drafting under ``ORCHESTRATOR``-owned spawning is *not* "a phase with 0
    running agents" — the orchestrator is about to spawn the next one-shot
    agent. ``NONE`` means nothing is queued to make progress.
    """

    ORCHESTRATOR = "orchestrator"
    AGENT = "agent"
    NONE = "none"


class Severity(StrEnum):
    """Finding severity, mirroring the alert vocabulary."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FindingClass(StrEnum):
    """One class per incident the corpus pins.

    Slice-8's coverage-gap survey will extend this; the harness matches on the
    raw string so a detector may emit a class not listed here without breaking
    structural matching.
    """

    OVERSEER_SELF_INJECTION = "overseer_self_injection"
    ALERT_REFLECTION = "alert_reflection"
    PHASE_STALL = "phase_stall"
    HEARTBEAT_STALL = "heartbeat_stall"
    BRANCH_DIVERGENCE = "branch_divergence"
    CONTAINER_DEATH = "container_death"


# ---------------------------------------------------------------------------
# Detector contract — the duck-typed shape a detector-under-test returns.
# ---------------------------------------------------------------------------


@runtime_checkable
class Finding(Protocol):
    """Structural shape a detector returns (mirrors the slice-4 design).

    Any object exposing these attributes is a valid finding — the production
    ``Finding`` dataclass landing in ``health_checks/`` in slice-4 satisfies
    this protocol without the corpus importing it.
    """

    finding_class: str
    severity: str
    evidence: Any
    recommended_action: str
    requires_adjudication: bool


# A detector is a pure function over a snapshot returning a Finding or None.
Detector = Callable[["EventStreamSnapshot"], Finding | None]


# ---------------------------------------------------------------------------
# Snapshot data model — the input detectors evaluate.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunningAgent:
    """One agent in the running-agent set, annotated with its lifecycle owner."""

    role: str
    state: str
    lifecycle_owner: str = LifecycleOwner.ORCHESTRATOR.value
    exit_code: int | None = None
    exit_reason: str | None = None
    last_tool_call_age_s: float | None = None
    last_heartbeat_age_s: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunningAgent:
        return cls(
            role=data["role"],
            state=data["state"],
            lifecycle_owner=data.get("lifecycle_owner", LifecycleOwner.ORCHESTRATOR.value),
            exit_code=data.get("exit_code"),
            exit_reason=data.get("exit_reason"),
            last_tool_call_age_s=data.get("last_tool_call_age_s"),
            last_heartbeat_age_s=data.get("last_heartbeat_age_s"),
        )


@dataclass(frozen=True)
class EventStreamSnapshot:
    """A recorded/synthesized snapshot of pipeline state a detector evaluates.

    Mirrors the slice-4 detection-plane input: the running-agent set (with
    lifecycle-owner annotation per #3230), the BRC consensus matrix, phase /
    decision state, container transitions, gateway error counters, cost
    counters, the mid-turn message stream (alert-reflection), and git state
    (branch-divergence). Slice-4 builds the production type; this fixture form
    is intentionally permissive — ``raw`` retains the full source dict for
    forward-compatible fields a later detector may read.
    """

    snapshot_id: str
    pipeline_id: str
    phase: str
    running_agents: tuple[RunningAgent, ...] = ()
    consensus: dict[str, Any] = field(default_factory=dict)
    phase_state: dict[str, Any] = field(default_factory=dict)
    decision_state: dict[str, Any] = field(default_factory=dict)
    container_transitions: tuple[dict[str, Any], ...] = ()
    gateway_error_counters: dict[str, Any] = field(default_factory=dict)
    cost_counters: dict[str, Any] = field(default_factory=dict)
    midturn_messages: tuple[dict[str, Any], ...] = ()
    git_state: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventStreamSnapshot:
        return cls(
            snapshot_id=data["snapshot_id"],
            pipeline_id=data.get("pipeline_id", ""),
            phase=data.get("phase", ""),
            running_agents=tuple(
                RunningAgent.from_dict(a) for a in data.get("running_agents", [])
            ),
            consensus=dict(data.get("consensus", {})),
            phase_state=dict(data.get("phase_state", {})),
            decision_state=dict(data.get("decision_state", {})),
            container_transitions=tuple(data.get("container_transitions", [])),
            gateway_error_counters=dict(data.get("gateway_error_counters", {})),
            cost_counters=dict(data.get("cost_counters", {})),
            midturn_messages=tuple(data.get("midturn_messages", [])),
            git_state=dict(data.get("git_state", {})),
            raw=dict(data),
        )


@dataclass(frozen=True)
class ExpectedFinding:
    """The *label* of what a correct detector should emit on a known-bad row.

    This is the expectation, not a production finding — the harness compares a
    detector's output against it structurally via :func:`match_finding`.
    """

    finding_class: str
    severity: str
    requires_adjudication: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExpectedFinding:
        return cls(
            finding_class=data["finding_class"],
            severity=data["severity"],
            requires_adjudication=bool(data.get("requires_adjudication", False)),
        )


@dataclass(frozen=True)
class CorpusRow:
    """One labelled calibration row.

    Attributes:
        row_id: Stable identifier (used as the pytest parametrize id).
        label: known-normal | known-bad.
        incident: Human-readable incident name.
        pins: Issue / defect ids this row pins.
        snapshot: The :class:`EventStreamSnapshot` fed to the detector.
        expected: The :class:`ExpectedFinding` for a known-bad row, or ``None``
            for a known-normal row.
        detector_key: Which detector should evaluate this row.
        delivered_in_slice: The slice that delivers that detector (one of
            :data:`DETECTOR_DELIVERY_SLICES`) for known-bad rows; ``None`` for
            known-normal rows, which a null detector satisfies trivially.
        notes: Why this row exists / what it pins.
    """

    row_id: str
    label: Label
    incident: str
    pins: tuple[str, ...]
    snapshot: EventStreamSnapshot
    expected: ExpectedFinding | None
    detector_key: str
    delivered_in_slice: int | None = None
    notes: str = ""

    @property
    def is_known_bad(self) -> bool:
        return self.label is Label.KNOWN_BAD

    @property
    def lifecycle_owner(self) -> str:
        """The lifecycle owner for this snapshot (phase-state level, #3230)."""
        owner = self.snapshot.phase_state.get("lifecycle_owner")
        if owner:
            return str(owner)
        # Fall back to the first running agent's owner, else NONE.
        if self.snapshot.running_agents:
            return self.snapshot.running_agents[0].lifecycle_owner
        return LifecycleOwner.NONE.value

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CorpusRow:
        label = Label(data["label"])
        expected_raw = data.get("expected")
        expected = ExpectedFinding.from_dict(expected_raw) if expected_raw else None
        row = cls(
            row_id=data["row_id"],
            label=label,
            incident=data.get("incident", ""),
            pins=tuple(data.get("pins", [])),
            snapshot=EventStreamSnapshot.from_dict(data["snapshot"]),
            expected=expected,
            detector_key=data["detector_key"],
            delivered_in_slice=data.get("delivered_in_slice"),
            notes=data.get("notes", ""),
        )
        row._validate()
        return row

    def _validate(self) -> None:
        """Enforce the corpus invariants so a malformed fixture fails loudly."""
        if self.label is Label.KNOWN_BAD:
            if self.expected is None:
                raise ValueError(f"known-bad row {self.row_id!r} must have an expected finding")
            if self.delivered_in_slice not in DETECTOR_DELIVERY_SLICES:
                raise ValueError(
                    f"known-bad row {self.row_id!r} must name a detector-delivery slice "
                    f"({sorted(DETECTOR_DELIVERY_SLICES)}); got {self.delivered_in_slice!r}"
                )
        else:  # known-normal
            if self.expected is not None:
                raise ValueError(
                    f"known-normal row {self.row_id!r} must NOT carry an expected finding"
                )


# ---------------------------------------------------------------------------
# Loader.
# ---------------------------------------------------------------------------


def load_corpus(path: Path | None = None) -> list[CorpusRow]:
    """Load and validate the calibration corpus from ``fixtures.json``.

    Args:
        path: Optional override for the fixtures file (used in tests).

    Returns:
        The labelled rows, in fixture order.

    Raises:
        ValueError: If a row violates the corpus invariants (see
            :meth:`CorpusRow._validate`) or two rows share a ``row_id``.
    """
    fixtures_path = path or _FIXTURES_PATH
    raw = json.loads(fixtures_path.read_text())
    rows = [CorpusRow.from_dict(r) for r in raw.get("rows", [])]

    seen: set[str] = set()
    for row in rows:
        if row.row_id in seen:
            raise ValueError(f"duplicate corpus row_id: {row.row_id!r}")
        seen.add(row.row_id)
    return rows


# ---------------------------------------------------------------------------
# Detector registry — later slices register their real detectors here so the
# harness flips their rows from xfail to strict automatically.
# ---------------------------------------------------------------------------

_DETECTOR_REGISTRY: dict[str, Detector] = {}


def register_detector(detector_key: str, detector: Detector) -> None:
    """Register a real detector under ``detector_key`` (slices 4/7/8)."""
    _DETECTOR_REGISTRY[detector_key] = detector


def resolve_detector(detector_key: str) -> Detector | None:
    """Return the registered detector for ``detector_key``, or ``None``."""
    return _DETECTOR_REGISTRY.get(detector_key)


def null_detector(_snapshot: EventStreamSnapshot) -> Finding | None:
    """The slice-1 placeholder detector — it never fires.

    With no production detectors registered yet, every known-normal row passes
    trivially (None == None) and every known-bad row fails (None != Finding),
    which is exactly why known-bad rows are ``xfail`` until their detector lands.
    """
    return None


# ---------------------------------------------------------------------------
# Harness contract — the AC-3 rule + a precision/recall scoreboard.
# ---------------------------------------------------------------------------


def match_finding(result: Finding | None, expected: ExpectedFinding | None) -> bool:
    """Return True if a detector ``result`` matches the row's ``expected``.

    * Known-normal (``expected is None``): the detector must yield ``None``.
    * Known-bad: the result must be a finding whose ``finding_class``,
      ``severity``, and ``requires_adjudication`` equal the expectation.
    """
    if expected is None:
        return result is None
    if result is None:
        return False
    return (
        getattr(result, "finding_class", None) == expected.finding_class
        and getattr(result, "severity", None) == expected.severity
        and bool(getattr(result, "requires_adjudication", False))
        == expected.requires_adjudication
    )


def assert_row(detector: Detector, row: CorpusRow) -> None:
    """Assert the AC-3 contract for a single row, with a descriptive message."""
    result = detector(row.snapshot)
    if not match_finding(result, row.expected):
        if row.expected is None:
            raise AssertionError(
                f"known-normal row {row.row_id!r} ({row.incident}) must yield None, "
                f"but detector {row.detector_key!r} returned {result!r}"
            )
        raise AssertionError(
            f"known-bad row {row.row_id!r} ({row.incident}) expected "
            f"{row.expected!r} from detector {row.detector_key!r}, got {result!r}"
        )


@dataclass(frozen=True)
class Scoreboard:
    """Precision/recall tally produced by :func:`evaluate`.

    ``true_positive``  — known-bad row, detector fired with the correct class.
    ``false_positive`` — known-normal row, detector fired (over-fire).
    ``false_negative`` — known-bad row, detector returned None / wrong class.
    ``true_negative``  — known-normal row, detector correctly silent.
    ``undelivered``    — rows whose detector is not yet registered.
    """

    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0
    undelivered: int = 0

    @property
    def total(self) -> int:
        return (
            self.true_positive
            + self.false_positive
            + self.false_negative
            + self.true_negative
        )

    @property
    def precision(self) -> float:
        """TP / (TP + FP); 1.0 when nothing fired (no false positives)."""
        denom = self.true_positive + self.false_positive
        return 1.0 if denom == 0 else self.true_positive / denom

    @property
    def recall(self) -> float:
        """TP / (TP + FN); 1.0 when there are no known-bad rows to catch."""
        denom = self.true_positive + self.false_negative
        return 1.0 if denom == 0 else self.true_positive / denom

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return (
            "Overseer calibration scoreboard: "
            f"TP={self.true_positive} FP={self.false_positive} "
            f"FN={self.false_negative} TN={self.true_negative} "
            f"undelivered={self.undelivered} | "
            f"precision={self.precision:.3f} recall={self.recall:.3f}"
        )


def evaluate(
    rows: Iterable[CorpusRow],
    resolver: Callable[[str], Detector | None] = resolve_detector,
    *,
    fallback: Detector = null_detector,
) -> Scoreboard:
    """Run each row through its resolved detector and tally a :class:`Scoreboard`.

    Rows whose detector is unregistered fall back to :func:`null_detector` (so
    the baseline can prove it never over-fires) and are also counted under
    ``undelivered`` for visibility.
    """
    tp = fp = fn = tn = undelivered = 0
    for row in rows:
        detector = resolver(row.detector_key)
        if detector is None:
            undelivered += 1
            detector = fallback
        result = detector(row.snapshot)
        if row.is_known_bad:
            if match_finding(result, row.expected):
                tp += 1
            else:
                fn += 1
        else:
            if result is None:
                tn += 1
            else:
                fp += 1
    return Scoreboard(
        true_positive=tp,
        false_positive=fp,
        false_negative=fn,
        true_negative=tn,
        undelivered=undelivered,
    )
