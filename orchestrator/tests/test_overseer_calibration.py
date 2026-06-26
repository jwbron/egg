"""Overseer calibration harness (issue #2270, slice-1, AC-3 / deliverable #1).

Runs the labelled corpus (``overseer_calibration/fixtures.json``) through the
detector-under-test and asserts the AC-3 contract:

* every **known-normal** row MUST yield ``None`` (no over-firing), and
* every **known-bad** row MUST yield the expected ``Finding``.

Slice-1 ships the corpus and the harness only — the real detectors arrive in
slices 4 (detection plane), 7 (signal-calibration fixes), and 8 (coverage-gap
survey) and register themselves via
:func:`overseer_calibration.corpus.register_detector`. Until a detector is
registered, this harness falls back to the null detector, which makes every
known-bad row fail — so each known-bad row is marked ``xfail`` with a reason
naming the slice that delivers it. When a later slice registers the detector,
its rows flip to strict automatically (the xfail marker evaporates because the
detector resolves), which is exactly the "flip the rows to strict" step in
tasks 4-3 / 7-5 / 8-4.

The slice-1 invariant the harness asserts unconditionally: with no detectors
registered, the baseline must have **zero false positives** — a calibrated
overseer never cries wolf on a known-normal input.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — make the in-tests ``overseer_calibration`` package importable.
# ---------------------------------------------------------------------------

_tests_dir = Path(__file__).parent
if str(_tests_dir) not in sys.path:
    sys.path.insert(0, str(_tests_dir))

from overseer_calibration.corpus import (  # noqa: E402
    CorpusRow,
    Label,
    Scoreboard,
    assert_row,
    evaluate,
    load_corpus,
    null_detector,
    register_detector,
    resolve_detector,
)

# ---------------------------------------------------------------------------
# Slice-4 bridge (task-4-3): register the production detection-plane detectors
# into the corpus registry. This is what "flips the plane's rows to strict" —
# once ``health_checks.detection_plane`` is importable, its detectors resolve,
# the xfail markers in :func:`_row_param` evaporate, and the slice-4 known-bad
# rows run as ordinary strict assertions. Until the coder's plane lands the
# import fails, nothing registers, and the rows stay xfail (slice-1 baseline),
# keeping ``make test`` green on the tester branch alone.
#
# Production code must never import the test-only corpus, so the bridge runs
# here (test → production), not the other way around.
try:
    from health_checks.detection_plane import detect_phase_stall
except ImportError:
    pass
else:
    register_detector("phase_stall", detect_phase_stall)

# ---------------------------------------------------------------------------
# Slice-7 bridge (task-7-5): register the §2 signal-calibration detectors so
# their corpus rows flip from xfail to strict. Same test → production direction
# as the slice-4 bridge. Each detector is co-located with the production behavior
# it guards (task-7-2/-3/-4), so the bridge imports each from its real home:
#   * ``alert_reflection``  ← shared ``egg_agent.midturn_messages`` (intent gate)
#   * ``heartbeat_stall``   ← ``health_checks.tier1.consensus_stall`` (#2242)
#   * ``branch_divergence`` ← ``routes.pipelines`` (ancestor/patch-id, #2222/#2224)
# Each import is isolated so a missing detector leaves only its own row xfail
# (slice-1 baseline) rather than dropping all three — keeping ``make test`` green
# on the tester branch alone. ``test_slice7_signal_calibration_rows_are_strict``
# enforces all three once the slice lands.
try:
    from egg_agent.midturn_messages import detect_alert_reflection
except ImportError:
    pass
else:
    register_detector("alert_reflection", detect_alert_reflection)

try:
    from health_checks.tier1.consensus_stall import detect_heartbeat_stall
except ImportError:
    pass
else:
    register_detector("heartbeat_stall", detect_heartbeat_stall)

try:
    from routes.pipelines import detect_branch_divergence
except ImportError:
    pass
else:
    register_detector("branch_divergence", detect_branch_divergence)

# Load once at collection time so rows can be parametrized with per-row marks.
_CORPUS: list[CorpusRow] = load_corpus()


def _row_param(row: CorpusRow) -> pytest.ParameterSet:
    """Wrap a row as a pytest param, xfailing known-bad rows with no detector."""
    marks: list[pytest.MarkDecorator] = []
    detector = resolve_detector(row.detector_key)
    if detector is None and row.is_known_bad:
        marks.append(
            pytest.mark.xfail(
                reason=(
                    f"detector {row.detector_key!r} delivered in "
                    f"slice-{row.delivered_in_slice} (#2270); flips to strict then"
                ),
                strict=False,
            )
        )
    return pytest.param(row, id=row.row_id, marks=marks)


_ROW_PARAMS = [_row_param(row) for row in _CORPUS]


# ---------------------------------------------------------------------------
# Corpus sanity — the fixtures load and are well-formed.
# ---------------------------------------------------------------------------


def test_corpus_loads_and_is_non_trivial() -> None:
    """The corpus parses, has both labels, and pins each row to an issue/defect."""
    assert _CORPUS, "calibration corpus is empty"
    labels = {row.label for row in _CORPUS}
    assert Label.KNOWN_NORMAL in labels, "corpus needs known-normal rows"
    assert Label.KNOWN_BAD in labels, "corpus needs known-bad rows"
    for row in _CORPUS:
        assert row.pins, f"row {row.row_id!r} must pin at least one issue/defect"
        assert row.detector_key, f"row {row.row_id!r} must name a detector_key"
        assert row.notes, f"row {row.row_id!r} must explain why it exists"


def test_every_incident_class_has_both_polarities() -> None:
    """Each detector_key in the corpus carries a known-normal and a known-bad row.

    A calibration pair (does-not-fire / does-fire) is what makes a row a real
    precision *and* recall test once the detector lands.
    """
    by_key: dict[str, set[Label]] = {}
    for row in _CORPUS:
        by_key.setdefault(row.detector_key, set()).add(row.label)
    for key, labels in by_key.items():
        assert labels == {Label.KNOWN_NORMAL, Label.KNOWN_BAD}, (
            f"detector {key!r} needs both a known-normal and a known-bad row; got {sorted(labels)}"
        )


def test_known_bad_rows_target_delivery_slices() -> None:
    """Every known-bad row is delivered by a real slice (4/7/8) — xfail coverage.

    This is the contract behind ``test_calibration_contract``'s xfail markers:
    the xfailed set is *exactly* the known-bad rows, and each names the slice
    that will flip it to strict.
    """
    for row in _CORPUS:
        if row.is_known_bad:
            assert row.delivered_in_slice in {4, 7, 8}, (
                f"known-bad row {row.row_id!r} must be delivered in slice 4/7/8"
            )


# ---------------------------------------------------------------------------
# The AC-3 contract — one parametrized assertion per row.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row", _ROW_PARAMS)
def test_calibration_contract(row: CorpusRow) -> None:
    """A detector yields None on known-normal rows and the expected Finding on bad.

    Known-bad rows whose detector is not yet registered are xfail (see
    :func:`_row_param`); they fall back to the null detector here and fail the
    assertion, which xfail records as expected until the detector lands.
    """
    detector = resolve_detector(row.detector_key) or null_detector
    assert_row(detector, row)


# ---------------------------------------------------------------------------
# Scoreboard — precision/recall over the whole corpus.
# ---------------------------------------------------------------------------


def test_scoreboard_precision_is_invariant(capsys: pytest.CaptureFixture[str]) -> None:
    """Precision stays pinned at 1.0 at every slice; recall climbs as detectors land.

    Emits the precision/recall scoreboard for visibility. Slice-1 shipped this
    with zero detectors registered (recall 0.0, every row undelivered). As
    detectors land — slice-4 (phase_stall), 7, 8 — they register through the
    corpus bridge and their known-bad rows become true positives, so recall
    climbs. The permanent invariant, generalized here for task-4-3, is that the
    overseer must NEVER cry wolf: zero false positives, precision 1.0, at every
    registration state. Computing the expectations from what is actually
    registered keeps this test correct on the tester branch (nothing
    registered) and on the integrated slice branch (phase_stall registered)
    alike.
    """
    board: Scoreboard = evaluate(_CORPUS)
    print(board)

    normal_rows = [r for r in _CORPUS if not r.is_known_bad]
    bad_rows = [r for r in _CORPUS if r.is_known_bad]
    registered_bad = [r for r in bad_rows if resolve_detector(r.detector_key) is not None]
    undelivered_rows = [r for r in _CORPUS if resolve_detector(r.detector_key) is None]

    # The permanent invariant: never over-fire on a known-normal input.
    assert board.false_positive == 0, "the overseer must never cry wolf"
    assert board.true_negative == len(normal_rows)
    assert board.precision == pytest.approx(1.0)

    # Recall is exactly the known-bad rows whose detector has landed.
    assert board.true_positive == len(registered_bad)
    assert board.false_negative == len(bad_rows) - len(registered_bad)
    expected_recall = 1.0 if not bad_rows else len(registered_bad) / len(bad_rows)
    assert board.recall == pytest.approx(expected_recall)

    # Undelivered tracks rows whose detector is not yet registered.
    assert board.undelivered == len(undelivered_rows)
    assert board.total == len(_CORPUS)

    out = capsys.readouterr().out
    assert "scoreboard" in out.lower()


def test_slice4_detection_plane_rows_are_strict() -> None:
    """task-4-3: once the slice-4 plane is importable, its corpus rows pass strict.

    Skips on the tester branch alone (the production plane is not importable
    yet); on the integrated slice branch it asserts the phase_stall detector is
    registered (no longer xfail) and that BOTH its known-bad row and its #3230
    known-normal companion satisfy the AC-3 contract under the *real* detector.
    """
    pytest.importorskip("health_checks.detection_plane")

    plane_rows = [r for r in _CORPUS if r.delivered_in_slice == 4]
    assert plane_rows, "slice-4 must deliver at least one detector row"

    for row in plane_rows:
        detector = resolve_detector(row.detector_key)
        assert detector is not None, (
            f"slice-4 detector {row.detector_key!r} must be registered (strict, not xfail)"
        )
        assert_row(detector, row)

    # The #3230 false-stall companion must stay silent under the real detector.
    companion_keys = {r.detector_key for r in plane_rows}
    for row in _CORPUS:
        if row.detector_key in companion_keys and not row.is_known_bad:
            detector = resolve_detector(row.detector_key)
            assert detector is not None
            assert_row(detector, row)


# ---------------------------------------------------------------------------
# Slice-7 (task-7-5): §2 signal-calibration rows flip to strict.
# ---------------------------------------------------------------------------

_SLICE7_DETECTOR_KEYS = ("alert_reflection", "heartbeat_stall", "branch_divergence")


def test_slice7_signal_calibration_rows_are_strict() -> None:
    """task-7-5: once slice-7 lands, its §2 corpus rows pass under real detectors.

    Skips on the tester branch alone (the production detectors are not registered
    yet); on the integrated slice branch it asserts each §2 detector
    (``alert_reflection`` #2270-§2b, ``heartbeat_stall`` #2242, ``branch_divergence``
    #2222/#2224) is registered — no longer xfail — and that BOTH its known-bad
    row AND its known-normal companion satisfy the AC-3 contract under the *real*
    detector. The known-normal companions are the calibration teeth: they pin the
    "stop crying wolf" fixes (an informational alert rendered as binding is the
    only ``alert_reflection`` trigger; a 45 s heartbeat gap with 2.5 s since the
    last tool call is NOT a ``heartbeat_stall``; an ancestor-of-base / patch-id
    match is NOT ``branch_divergence`` even with a ``(#NNNN)`` subject).
    """
    pytest.importorskip("health_checks.detection_plane")

    missing = [k for k in _SLICE7_DETECTOR_KEYS if resolve_detector(k) is None]
    if missing:
        pytest.skip(f"slice-7 §2 detectors not yet registered: {sorted(missing)}")

    slice7_rows = [r for r in _CORPUS if r.delivered_in_slice == 7]
    assert slice7_rows, "slice-7 must deliver at least one detector row"
    delivered_keys = {r.detector_key for r in slice7_rows}
    assert delivered_keys == set(_SLICE7_DETECTOR_KEYS), (
        f"slice-7 known-bad rows must cover exactly {sorted(_SLICE7_DETECTOR_KEYS)}; "
        f"got {sorted(delivered_keys)}"
    )

    # Every slice-7 known-bad row fires the expected finding under the real detector.
    for row in slice7_rows:
        detector = resolve_detector(row.detector_key)
        assert detector is not None, (
            f"slice-7 detector {row.detector_key!r} must be registered (strict, not xfail)"
        )
        assert_row(detector, row)

    # Every known-normal companion of a slice-7 detector stays silent (precision).
    for row in _CORPUS:
        if row.detector_key in set(_SLICE7_DETECTOR_KEYS) and not row.is_known_bad:
            detector = resolve_detector(row.detector_key)
            assert detector is not None
            assert_row(detector, row)


# ---------------------------------------------------------------------------
# Focused detector unit tests — the individual §2 fixes (task-7-5 "each fix
# unit-tested"). These build EventStreamSnapshots directly and assert the
# discriminating behavior the calibration rows encode, so a regression points at
# the exact rule that broke rather than at an opaque corpus-row failure.
# ---------------------------------------------------------------------------


def _snapshot(data: dict):
    """Build an EventStreamSnapshot from a dict (skips if the plane is absent)."""
    dp = pytest.importorskip("health_checks.detection_plane")
    return dp.EventStreamSnapshot.from_dict(data)


def test_lifecycle_owner_aware_stall_silences_orchestrator_owned_gap() -> None:
    """§2a / #3230: a RUNNING phase with 0 agents but an orchestrator-owned
    lifecycle is NOT a stall — the orchestrator is about to spawn the next
    one-shot agent. The pre-fix detector treated "0 running agents" as wedged.
    """
    dp = pytest.importorskip("health_checks.detection_plane")
    snap = _snapshot(
        {
            "snapshot_id": "lifecycle_owned_gap",
            "phase": "implement",
            "running_agents": [],
            "phase_state": {
                "status": "RUNNING",
                "lifecycle_owner": "orchestrator",
                "started_age_s": 100_000,  # far past any grace window
            },
            "decision_state": {"pending_hitl": False, "open_decisions": 0},
        }
    )
    assert dp.detect_phase_stall(snap) is None


def test_lifecycle_owner_aware_stall_fires_when_nothing_queued() -> None:
    """§2a / #3230: a RUNNING phase wedged past grace with NO owner queued to
    spawn the next agent IS the genuine stall — and it escalates to adjudication.
    """
    dp = pytest.importorskip("health_checks.detection_plane")
    snap = _snapshot(
        {
            "snapshot_id": "genuine_stall",
            "phase": "implement",
            "running_agents": [],
            "phase_state": {
                "status": "RUNNING",
                "lifecycle_owner": "none",
                "awaiting_spawn": False,
                "started_age_s": 100_000,
            },
            "decision_state": {"pending_hitl": False, "open_decisions": 0},
            "consensus": {"blocking_agents": ["coder"]},
        }
    )
    finding = dp.detect_phase_stall(snap)
    assert finding is not None
    assert str(finding.finding_class) == "phase_stall"
    assert finding.requires_adjudication is True


def test_heartbeat_stall_ignores_heartbeat_gap_with_live_tool_calls() -> None:
    """§2d / #2242: an agent calling tools every ~2-3 s is working, even if its
    last *heartbeat* was 45 s ago. Gating on heartbeat age alone produced the
    #2242 false stalls; the fix requires BOTH tool-call and heartbeat age stale.
    """
    consensus_stall = pytest.importorskip("health_checks.tier1.consensus_stall")
    snap = _snapshot(
        {
            "snapshot_id": "heartbeat_gap_but_working",
            "phase": "implement",
            "running_agents": [
                {
                    "role": "coder",
                    "state": "WORKING",
                    "lifecycle_owner": "orchestrator",
                    "last_tool_call_age_s": 2.5,
                    "last_heartbeat_age_s": 45.0,
                }
            ],
            "phase_state": {"status": "RUNNING", "started_age_s": 300},
        }
    )
    assert consensus_stall.detect_heartbeat_stall(snap) is None


def test_heartbeat_stall_fires_when_no_tool_calls() -> None:
    """§2d / #2242: a WORKING agent with no tool call for 900 s is genuinely
    stalled and fires ``heartbeat_stall`` / high.
    """
    consensus_stall = pytest.importorskip("health_checks.tier1.consensus_stall")
    snap = _snapshot(
        {
            "snapshot_id": "genuine_heartbeat_stall",
            "phase": "implement",
            "running_agents": [
                {
                    "role": "coder",
                    "state": "WORKING",
                    "lifecycle_owner": "orchestrator",
                    "last_tool_call_age_s": 900.0,
                    "last_heartbeat_age_s": 900.0,
                }
            ],
            "phase_state": {"status": "RUNNING", "started_age_s": 1200},
        }
    )
    finding = consensus_stall.detect_heartbeat_stall(snap)
    assert finding is not None
    assert str(finding.finding_class) == "heartbeat_stall"
    assert str(finding.severity) == "high"


def test_branch_divergence_uses_ancestor_or_patch_id_not_subject() -> None:
    """§2c / #2222/#2224: divergence is decided by ancestor-of-base OR patch-id
    match, NOT by a ``(#NNNN)`` subject. A branch that is an ancestor of base, or
    whose patch-id matches the merged commit, is clean even if its PR subject
    "looks like" a merge.
    """
    pipelines_mod = pytest.importorskip("routes.pipelines")
    detect_branch_divergence = pipelines_mod.detect_branch_divergence

    # #2222 false positive: subject-divergence true, but ancestor + patch-id clean.
    clean = _snapshot(
        {
            "snapshot_id": "branch_clean_despite_subject",
            "phase": "pr",
            "git_state": {
                "is_ancestor_of_base": True,
                "patch_id_matches": True,
                "pr_subject_divergence": True,
            },
        }
    )
    assert detect_branch_divergence(clean) is None

    # #2224 real contamination: neither an ancestor nor a patch-id match.
    contaminated = _snapshot(
        {
            "snapshot_id": "branch_contaminated",
            "phase": "pr",
            "git_state": {
                "is_ancestor_of_base": False,
                "patch_id_matches": False,
                "pr_subject_divergence": False,
            },
        }
    )
    finding = detect_branch_divergence(contaminated)
    assert finding is not None
    assert str(finding.finding_class) == "branch_divergence"


@pytest.mark.asyncio
async def test_activity_pattern_vocabulary_is_closed_and_coerced() -> None:
    """§2e / #2059/#2132: the thrashing/spinning/improper-tool-use verdict set is a
    CLOSED vocabulary, and ``classify_activity_pattern`` coerces any out-of-vocab
    classifier output back to the safe default so the verdict can never leak an
    unbounded free-text pattern (the #2059/#2132 "improper tool use" defect).
    """
    classifier = pytest.importorskip("overseer.classifier")
    ActivityPattern = classifier.ActivityPattern

    # The definition is exactly the four first-class verdicts.
    assert {p.value for p in ActivityPattern} == {
        "productive",
        "thrashing",
        "spinning",
        "improper_tool_use",
    }

    actions = [{"tool": "Edit", "result": "error"}, {"tool": "Edit", "result": "error"}]

    async def _fake_classifier(prompt: str, context: str) -> str:
        return '{"pattern": "thrashing", "confidence": 0.9, "reasoning": "x"}'

    async def _fake_classifier_oov(prompt: str, context: str) -> str:
        return '{"pattern": "made_up_pattern", "confidence": 0.9, "reasoning": "x"}'

    with patch.object(classifier, "_call_classifier", _fake_classifier):
        classifier._cache.clear()
        in_vocab = await classifier.classify_activity_pattern(actions)
    assert in_vocab["pattern"] == ActivityPattern.THRASHING.value

    with patch.object(classifier, "_call_classifier", _fake_classifier_oov):
        classifier._cache.clear()
        coerced = await classifier.classify_activity_pattern(actions)
    assert coerced["pattern"] in {p.value for p in ActivityPattern}
    assert coerced["pattern"] == ActivityPattern.PRODUCTIVE.value
