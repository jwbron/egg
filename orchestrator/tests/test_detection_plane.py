"""Slice-4 detection-plane contract tests (issue #2270, task-4-3).

Slice-4 builds the orchestrator-side, in-process **detection plane**: a set of
deterministic detectors that run over an :class:`EventStreamSnapshot` and return
``Optional[Finding]``. When a finding's ``requires_adjudication`` flag is set,
the orchestrator escalates by spawning a *normal* on-demand OVERSEER agent (the
slice-3 normalized spawn path) that returns a structured verdict. This module
is the **tester contract** that pins that production surface; the coder
reconciles ``health_checks/detection_plane.py`` to it (the same
tester-leads-coder flow used in slices 2 and 3).

The production surface this contract pins (``health_checks.detection_plane``):

* ``Finding`` — a frozen dataclass with exactly the five fields the §4 design
  names: ``finding_class``, ``severity``, ``evidence``, ``recommended_action``,
  ``requires_adjudication`` (defaulting to ``False``). It must satisfy the
  duck-typed ``Finding`` protocol the slice-1 calibration corpus already
  declares, so the production type plugs straight into the harness.
* ``detect_phase_stall(snapshot) -> Finding | None`` — the one detector slice-4
  delivers. It fires (``phase_stall`` / ``high`` / ``requires_adjudication=True``)
  on a genuinely wedged phase and stays silent on the #3230 false-stall (a
  producer drafting under orchestrator-owned spawning).
* ``DetectionPlane`` — runs its detectors over a snapshot via ``.evaluate()``;
  ``DetectionPlane.default()`` is pre-wired with the slice-4 detectors.
* ``escalate_findings(findings, *, spawn_adjudicator)`` — the escalation gate:
  it invokes ``spawn_adjudicator`` exactly once per finding whose
  ``requires_adjudication`` is set, and never for the rest. The injected
  callback is what the orchestrator wires to the slice-3 ``spawn_agent_job(
  agent_role=OVERSEER, ...)`` path; the unit test injects a spy.

The whole module ``importorskip``s the production plane, so it is green before
the coder's code lands (the module simply skips) and runs strict once it does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — make the in-tests ``overseer_calibration`` package importable
# (mirrors test_overseer_calibration.py) so the production detectors can be
# exercised against the real slice-1 corpus snapshots.
# ---------------------------------------------------------------------------

_tests_dir = Path(__file__).parent
if str(_tests_dir) not in sys.path:
    sys.path.insert(0, str(_tests_dir))

from overseer_calibration.corpus import (  # noqa: E402
    CorpusRow,
    load_corpus,
)
from overseer_calibration.corpus import (
    Finding as CorpusFinding,
)

# Skip the whole module until the coder's production plane lands. On the
# integrated slice branch the import resolves and every test runs strict.
detection_plane = pytest.importorskip("health_checks.detection_plane")


_CORPUS: list[CorpusRow] = load_corpus()


def _row(row_id: str) -> CorpusRow:
    """Fetch a labelled corpus row by id (single source of truth for inputs)."""
    for row in _CORPUS:
        if row.row_id == row_id:
            return row
    raise AssertionError(f"corpus row {row_id!r} not found — fixtures drifted")


# ---------------------------------------------------------------------------
# Finding contract — the production dataclass shape the §4 design names.
# ---------------------------------------------------------------------------


def test_finding_has_the_five_design_fields() -> None:
    """``Finding`` carries exactly the §4 fields and defaults adjudication off."""
    finding = detection_plane.Finding(
        finding_class="phase_stall",
        severity="high",
        evidence={"started_age_s": 5400},
        recommended_action="advance or fail the wedged phase",
    )
    assert finding.finding_class == "phase_stall"
    assert finding.severity == "high"
    assert finding.evidence == {"started_age_s": 5400}
    assert finding.recommended_action
    # requires_adjudication is a real bool and defaults to False (cheap, no spawn).
    assert finding.requires_adjudication is False


def test_finding_satisfies_corpus_protocol() -> None:
    """The production ``Finding`` duck-types onto the slice-1 corpus protocol.

    This is what lets the production type plug into the calibration harness
    without the corpus importing production code.
    """
    finding = detection_plane.Finding(
        finding_class="phase_stall",
        severity="high",
        evidence={},
        recommended_action="advance or fail the wedged phase",
        requires_adjudication=True,
    )
    assert isinstance(finding, CorpusFinding)


def test_finding_is_frozen() -> None:
    """Findings are immutable value objects (frozen dataclass)."""
    finding = detection_plane.Finding(
        finding_class="phase_stall",
        severity="high",
        evidence={},
        recommended_action="x",
    )
    with pytest.raises((AttributeError, TypeError)):
        finding.severity = "low"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# detect_phase_stall — the slice-4 detector, validated on the real corpus rows.
# ---------------------------------------------------------------------------


def test_phase_stall_fires_on_genuine_wedge() -> None:
    """A genuinely wedged phase yields phase_stall / high / requires adjudication."""
    finding = detection_plane.detect_phase_stall(_row("phase_stall__bad").snapshot)
    assert finding is not None, "the detection plane must fire on a real stall"
    assert finding.finding_class == "phase_stall"
    assert finding.severity == "high"
    assert finding.requires_adjudication is True
    # The evidence must be structured and carry the wedge signal for the verdict.
    assert finding.evidence
    assert finding.recommended_action


def test_phase_stall_silent_on_3230_false_stall() -> None:
    """#3230: a producer drafting under orchestrator-owned spawning is NOT a stall.

    Zero running agents but ``lifecycle_owner == orchestrator`` (a one-shot
    agent is about to be spawned) must yield ``None`` — this is the lifecycle-
    owner-aware fix and the core false-positive the calibration corpus pins.
    """
    finding = detection_plane.detect_phase_stall(_row("false_stall_3230__normal").snapshot)
    assert finding is None, "lifecycle-owner-aware stall detector must not cry wolf"


# ---------------------------------------------------------------------------
# DetectionPlane.evaluate — runs the wired detectors over a snapshot.
# ---------------------------------------------------------------------------


def test_default_plane_wires_phase_stall() -> None:
    """``DetectionPlane.default()`` ships with the slice-4 detector(s) wired."""
    plane = detection_plane.DetectionPlane.default()
    assert "phase_stall" in plane.detectors


def test_plane_evaluate_surfaces_the_stall_finding() -> None:
    """``evaluate`` collects the non-None findings from every wired detector."""
    plane = detection_plane.DetectionPlane.default()
    findings = plane.evaluate(_row("phase_stall__bad").snapshot)
    classes = {f.finding_class for f in findings}
    assert "phase_stall" in classes
    stall = next(f for f in findings if f.finding_class == "phase_stall")
    assert stall.requires_adjudication is True


def test_plane_evaluate_silent_on_false_stall() -> None:
    """``evaluate`` returns no phase_stall finding on the #3230 false-stall row."""
    plane = detection_plane.DetectionPlane.default()
    findings = plane.evaluate(_row("false_stall_3230__normal").snapshot)
    assert all(f.finding_class != "phase_stall" for f in findings)


# ---------------------------------------------------------------------------
# Escalation gate — the adjudicator spawns ONLY when requires_adjudication.
# ---------------------------------------------------------------------------


class _SpawnSpy:
    """Records each adjudicator spawn so the gating can be asserted."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, finding: object) -> object:
        self.calls.append(finding)
        return finding


def _finding(finding_class: str, *, requires_adjudication: bool) -> object:
    return detection_plane.Finding(
        finding_class=finding_class,
        severity="high",
        evidence={},
        recommended_action="x",
        requires_adjudication=requires_adjudication,
    )


def test_escalation_spawns_only_for_adjudication_findings() -> None:
    """The adjudicator spawns exactly once per requires_adjudication finding."""
    needs = _finding("phase_stall", requires_adjudication=True)
    cheap = _finding("heartbeat_stall", requires_adjudication=False)
    spy = _SpawnSpy()

    escalated = detection_plane.escalate_findings([cheap, needs], spawn_adjudicator=spy)

    # Exactly one spawn, and it was for the adjudication-required finding only.
    assert len(spy.calls) == 1
    assert spy.calls[0] is needs
    assert list(escalated) == [needs]


def test_escalation_no_spawn_when_nothing_requires_adjudication() -> None:
    """A finding that doesn't require adjudication must NOT spawn the overseer.

    This is the cost guard: deterministic detectors resolve cheaply in-process;
    only ambiguous findings pay for an Opus adjudicator.
    """
    cheap = _finding("heartbeat_stall", requires_adjudication=False)
    spy = _SpawnSpy()

    escalated = detection_plane.escalate_findings([cheap], spawn_adjudicator=spy)

    assert spy.calls == []
    assert list(escalated) == []


def test_escalation_no_spawn_on_empty_findings() -> None:
    """No findings → no adjudicator spawn (the steady-state healthy path)."""
    spy = _SpawnSpy()
    escalated = detection_plane.escalate_findings([], spawn_adjudicator=spy)
    assert spy.calls == []
    assert list(escalated) == []


def test_phase_stall_finding_routes_to_adjudicator_end_to_end() -> None:
    """The real phase_stall finding escalates: evaluate → escalate spawns once."""
    plane = detection_plane.DetectionPlane.default()
    findings = plane.evaluate(_row("phase_stall__bad").snapshot)
    spy = _SpawnSpy()

    detection_plane.escalate_findings(findings, spawn_adjudicator=spy)

    assert len(spy.calls) == 1
    assert spy.calls[0].finding_class == "phase_stall"
