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
