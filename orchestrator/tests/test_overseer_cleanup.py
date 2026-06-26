"""Slice-9 cleanup contract tests (issue #2270, task-9-3).

Slice-9 is the **cleanup tail** of the overseer overhaul: collapse the per-class
fail-soft scaffolding into a single exception-isolation point, de-duplicate the
advisor-escalation plumbing, and *harden* the two-tier ``file_issue`` dedup —
all while staying net-negative in line count and **without regressing
detection**. The whole point of a behaviour-preserving cleanup is that the
contract the earlier slices pinned still holds afterwards, so this module pins
the four behaviours the cleanup must not break:

1. **Two-tier dedup hardening** (``overseer.issue_filer.IssueDedupLedger``) — a
   coarse time-windowed ``(anomaly_type, agent_role)`` tier *and* a fine
   content-addressed exact-body tier; both must pass to file, the gate is
   idempotent under repeats, and ``reset()`` clears both tiers.
2. **Fail-soft collapse** (``health_checks.detection_plane.DetectionPlane``) — a
   buggy detector degrades to "no finding" through the single
   :meth:`DetectionPlane.evaluate_one` wrapper and never crashes the loop or
   starves its sibling detectors.
3. **Shadow→enforce gate default** (``models.PipelineConfig
   .overseer_auto_file_issues_mode``) — unattended auto-escalation ships in
   *shadow* by default (telemetry/HITL-gated), not *live*; per §4 the rollout is
   shadow-first.
4. **Net-negative sanity check** — the slice-1 calibration corpus stays green:
   deletion did not drop a detector or re-introduce a false-positive flood.

Production modules are imported via :func:`pytest.importorskip` so the file is
green on the tester branch alone (modules simply skip) and runs strict once the
integrated slice branch resolves them — the same convention as
``test_detection_plane.py`` / ``test_overseer_calibration.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Path setup — make orchestrator/ and the in-tests ``overseer_calibration``
# package importable (mirrors test_detection_plane.py / test_overseer_calibration.py).
# ---------------------------------------------------------------------------

_tests_dir = Path(__file__).parent
if str(_tests_dir) not in sys.path:
    sys.path.insert(0, str(_tests_dir))

_orchestrator_dir = _tests_dir.parent
if str(_orchestrator_dir) not in sys.path:
    sys.path.insert(0, str(_orchestrator_dir))

from overseer_calibration.corpus import (  # noqa: E402
    CorpusRow,
    Scoreboard,
    assert_row,
    evaluate,
    load_corpus,
    register_detector,
    resolve_detector,
)

# Production surfaces under cleanup. importorskip keeps the module green before
# the integrated slice branch resolves them.
issue_filer = pytest.importorskip("overseer.issue_filer")
detection_plane = pytest.importorskip("health_checks.detection_plane")
models = pytest.importorskip("models")

IssueDedupLedger = issue_filer.IssueDedupLedger
DetectionPlane = detection_plane.DetectionPlane


_CORPUS: list[CorpusRow] = load_corpus()


# ---------------------------------------------------------------------------
# A deterministic, injectable clock so the time-windowed tier is unit-testable
# without sleeping (the production ledger takes ``clock: Callable[[], float]``).
# ---------------------------------------------------------------------------


class _Clock:
    """Manually-advanced monotonic clock for the dedup window tier."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


# ===========================================================================
# 1. Two-tier file_issue dedup hardening
# ===========================================================================


class TestIssueDedupLedger:
    """Tier-1 (time-windowed type+role) AND Tier-2 (exact-body) must both pass."""

    def test_first_file_of_a_kind_is_allowed(self) -> None:
        ledger = IssueDedupLedger(window_seconds=300.0, clock=_Clock())
        assert ledger.should_file(anomaly_type="container_death", agent_role="coder", body="first")

    def test_tier1_suppresses_same_type_role_within_window(self) -> None:
        """A persistent anomaly re-detected each poll cycle is filed once."""
        clock = _Clock()
        ledger = IssueDedupLedger(window_seconds=300.0, clock=clock)

        assert ledger.should_file(anomaly_type="container_death", agent_role="coder", body="body-A")
        # Same (type, role), *different* body, 100s later — still inside the
        # 300s window → Tier 1 suppresses it.
        clock.advance(100.0)
        assert not ledger.should_file(
            anomaly_type="container_death", agent_role="coder", body="body-B"
        )

    def test_tier1_reopens_after_window_lapses(self) -> None:
        """Once the coarse window lapses, a fresh-body anomaly may file again."""
        clock = _Clock()
        ledger = IssueDedupLedger(window_seconds=300.0, clock=clock)

        assert ledger.should_file(anomaly_type="container_death", agent_role="coder", body="body-A")
        clock.advance(301.0)  # past the window
        # New body so Tier 2 does not block; window lapsed so Tier 1 allows it.
        assert ledger.should_file(anomaly_type="container_death", agent_role="coder", body="body-B")

    def test_tier2_suppresses_exact_body_even_after_window(self) -> None:
        """A byte-identical body is never filed twice — even past the window."""
        clock = _Clock()
        ledger = IssueDedupLedger(window_seconds=300.0, clock=clock)

        identical = "exact identical issue body"
        assert ledger.should_file(
            anomaly_type="container_death", agent_role="coder", body=identical
        )
        clock.advance(10_000.0)  # far past any Tier-1 window
        # Tier 1 would allow (window long lapsed) but Tier 2 still blocks.
        assert not ledger.should_file(
            anomaly_type="container_death", agent_role="coder", body=identical
        )

    def test_distinct_type_role_keys_are_independent(self) -> None:
        """Tier 1 keys on (type, role): different keys do not cross-suppress."""
        clock = _Clock()
        ledger = IssueDedupLedger(window_seconds=300.0, clock=clock)

        assert ledger.should_file(anomaly_type="container_death", agent_role="coder", body="b1")
        # Different role → independent Tier-1 key, novel body → files.
        assert ledger.should_file(anomaly_type="container_death", agent_role="tester", body="b2")
        # Different anomaly_type → independent Tier-1 key, novel body → files.
        assert ledger.should_file(anomaly_type="gateway_error_spike", agent_role="coder", body="b3")

    def test_suppressed_call_records_nothing_so_gate_is_idempotent(self) -> None:
        """A suppressed (False) call must not consume the window — the gate is
        idempotent under repeats: it does not slide the Tier-1 timestamp forward
        on every poll, so the window still expires on schedule."""
        clock = _Clock()
        ledger = IssueDedupLedger(window_seconds=300.0, clock=clock)

        assert ledger.should_file(anomaly_type="container_death", agent_role="coder", body="body-A")
        # Hammer the gate inside the window — all suppressed, none reset the clock.
        for _ in range(5):
            clock.advance(50.0)
            assert not ledger.should_file(
                anomaly_type="container_death", agent_role="coder", body="body-X"
            )
        # 250s elapsed; advance just past the original 300s window from t0.
        clock.advance(60.0)  # total 310s after the first file
        assert ledger.should_file(anomaly_type="container_death", agent_role="coder", body="body-Y")

    def test_reset_clears_both_tiers(self) -> None:
        """``reset()`` (e.g. on generation reset) wipes both dedup tiers."""
        clock = _Clock()
        ledger = IssueDedupLedger(window_seconds=300.0, clock=clock)

        body = "same-body"
        assert ledger.should_file(anomaly_type="container_death", agent_role="coder", body=body)
        assert not ledger.should_file(anomaly_type="container_death", agent_role="coder", body=body)

        ledger.reset()

        # After reset both tiers are empty: the identical body files again with
        # no clock advance (proves Tier 2 cleared) and inside the window (Tier 1).
        assert ledger.should_file(anomaly_type="container_death", agent_role="coder", body=body)


# ===========================================================================
# 2. Per-class fail-soft collapse — a single exception-isolation point
# ===========================================================================


def _snapshot():
    """A valid snapshot to drive detectors through the plane.

    Reuse a real corpus snapshot rather than hand-build one — the fake detectors
    below ignore its contents, and this keeps the test resilient to the
    ``EventStreamSnapshot`` schema (required fields like ``snapshot_id``).
    """
    return _CORPUS[0].snapshot


class _Detector:
    """A tiny detector object satisfying the duck-typed ``Detector`` protocol."""

    def __init__(self, detector_key: str, *, raises: bool = False, finding=None):
        self.detector_key = detector_key
        self.name = detector_key
        self._raises = raises
        self._finding = finding

    def __call__(self, _snapshot):
        if self._raises:
            raise RuntimeError(f"boom from {self.detector_key}")
        return self._finding


class TestFailSoftCollapse:
    """A buggy detector degrades to no-finding through one collapse point."""

    def test_evaluate_one_swallows_detector_exception(self) -> None:
        plane = DetectionPlane()
        assert plane.evaluate_one(_Detector("boom", raises=True), _snapshot()) is None

    def test_raising_detector_does_not_starve_siblings(self) -> None:
        """The fail-soft wrapper is *per-detector*: one crash must not block the
        rest of the plane from running and producing their findings."""
        sentinel = object()
        plane = DetectionPlane()
        plane.register(_Detector("boom", raises=True))
        plane.register(_Detector("good", finding=sentinel))

        findings = plane.evaluate(_snapshot())
        assert findings == [sentinel]

    def test_evaluate_returns_empty_when_all_detectors_raise(self) -> None:
        plane = DetectionPlane()
        plane.register(_Detector("boom1", raises=True))
        plane.register(_Detector("boom2", raises=True))
        assert plane.evaluate(_snapshot()) == []

    def test_collapse_is_a_single_wrapper_used_by_evaluate(self) -> None:
        """``evaluate`` routes every detector through ``evaluate_one`` — the one
        place the fail-soft scaffolding collapsed to. A detector that raises only
        on its first call but would succeed later still yields nothing (proving
        no retry/duplicate-wrapper path survives the collapse)."""

        class _OnceBoom:
            detector_key = "once"
            name = "once"

            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, _snapshot):
                self.calls += 1
                raise RuntimeError("always")

        det = _OnceBoom()
        plane = DetectionPlane()
        plane.register(det)
        assert plane.evaluate(_snapshot()) == []
        # Exactly one invocation per evaluate — no hidden retry layer.
        assert det.calls == 1


# ===========================================================================
# 3. Shadow→enforce gate default
# ===========================================================================


class TestShadowEnforceGateDefault:
    """Unattended auto-escalation ships shadow-first (§4): default == 'shadow'."""

    _FIELD = "overseer_auto_file_issues_mode"

    def test_default_is_shadow(self) -> None:
        default = models.PipelineConfig.model_fields[self._FIELD].default
        assert default == "shadow", (
            "auto-issue filing must default to shadow (telemetry/HITL-gated) — "
            "unattended 'live' filing must be an explicit opt-in"
        )

    def test_default_instance_is_shadow(self) -> None:
        cfg = models.PipelineConfig()
        assert getattr(cfg, self._FIELD) == "shadow"

    def test_only_shadow_or_live_are_accepted(self) -> None:
        """The gate is a closed Literal — an arbitrary mode is rejected, so the
        shadow→enforce rollout cannot silently widen to an unknown third mode."""
        base = models.PipelineConfig().model_dump()
        # Both valid modes validate.
        for mode in ("shadow", "live"):
            cfg = models.PipelineConfig.model_validate(base | {self._FIELD: mode})
            assert getattr(cfg, self._FIELD) == mode
        # An unknown mode is rejected by the Literal type.
        with pytest.raises(ValidationError):
            models.PipelineConfig.model_validate(base | {self._FIELD: "enforce-everything"})


# ===========================================================================
# 4. Net-negative sanity check — deletion did not regress detection
# ===========================================================================


class TestDetectionNotRegressedByCleanup:
    """The cleanup must stay behaviour-preserving: the corpus stays green."""

    def test_corpus_never_over_fires_after_cleanup(self) -> None:
        """The permanent invariant: zero false positives, precision 1.0. If the
        cleanup deleted shared plumbing a detector relied on, a known-normal row
        would start firing and this would catch it."""
        board: Scoreboard = evaluate(_CORPUS)
        assert board.false_positive == 0, "the overseer must never cry wolf"
        assert board.precision == pytest.approx(1.0)
        normal_rows = [r for r in _CORPUS if not r.is_known_bad]
        assert board.true_negative == len(normal_rows)
        assert board.total == len(_CORPUS)

    def test_default_plane_still_carries_its_detectors(self) -> None:
        """Net-negative deletion must not drop a detector from the default plane.
        Every detector the plane carries must register and pass its corpus rows
        strict — proving deletion removed dead code, not live detectors."""
        default_plane = detection_plane.default_detection_plane()
        keys = default_plane.detectors
        # The slice-4 spine detector is the floor; slice-8 added many more.
        assert "phase_stall" in keys
        assert len(keys) >= 1

        # Bridge the plane's live detectors into the corpus registry, then assert
        # every corpus row whose detector is present passes the AC-3 contract.
        for key, detector in keys.items():
            register_detector(key, detector)

        checked = 0
        for row in _CORPUS:
            if resolve_detector(row.detector_key) is None:
                continue
            assert_row(resolve_detector(row.detector_key), row)
            checked += 1
        assert checked > 0, "expected at least one live detector's rows to verify"

    def test_recall_did_not_collapse_to_zero(self) -> None:
        """A net-negative cleanup that accidentally unregistered every detector
        would still show precision 1.0 (nothing fires) — so guard recall too:
        once the integrated plane is wired, at least one known-bad row must be
        caught."""
        default_plane = detection_plane.default_detection_plane()
        for key, detector in default_plane.detectors.items():
            register_detector(key, detector)

        board: Scoreboard = evaluate(_CORPUS)
        bad_rows = [r for r in _CORPUS if r.is_known_bad]
        registered_bad = [r for r in bad_rows if resolve_detector(r.detector_key) is not None]
        # Recall is exactly the known-bad rows whose detector is registered.
        assert board.true_positive == len(registered_bad)
        if registered_bad:
            assert board.true_positive > 0, "cleanup must not silence all detectors"
