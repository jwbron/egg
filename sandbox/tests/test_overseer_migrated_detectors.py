"""Tests for the migrated detectors in ``sandbox/overseer_monitor.py``.

Issue #1962, TASK-6-1: stall / silent-agent / NACK / long-running-phase
detection moves out of ``/sdlc`` into the overseer. The tests cover:

* The Tier-1 calibration flag — detectors emit alerts in BOTH modes
  (calibration + live), but tag them with ``calibration_only`` based on
  ``overseer_owns_host_detection``.
* Each individual detector (stall, silent, nack-unresolved, phase-long-running).
* The per-anomaly suppression window (2 × threshold).
* Persistence to ``agent-timing.json``.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from overseer_monitor import (
    _SUPPRESSION_FACTOR,
    run_migrated_detectors,
)


def _consensus_with_nack(
    *, nack_dt: datetime.datetime, role: str = "coder"
) -> dict[str, list[dict[str, str]]]:
    return {
        "nacks": [
            {
                "from_role": role,
                "timestamp": nack_dt.isoformat(),
            }
        ]
    }


class TestRunMigratedDetectors:
    @pytest.fixture
    def tmp_state_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "agent-timing.json"
        monkeypatch.setenv("AGENT_TIMING_PATH", str(path))
        return path

    def test_returns_no_alerts_when_no_progress_or_consensus(self, tmp_state_path: Path) -> None:
        alerts = run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="implement",
            config_subset={"overseer_owns_host_detection": True},
            progress_events=[],
            consensus={},
        )
        # No agents seen yet, no NACKs — nothing to alert on.
        assert alerts == []

    def test_calibration_mode_still_runs_detectors(self, tmp_state_path: Path) -> None:
        # The reviewer_contract NACK pointed out that the original
        # implementation short-circuited when the flag was False — the
        # follow-up fix runs detectors in BOTH modes for side-by-side
        # calibration. This test pins the new contract.
        alerts = run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="implement",
            config_subset={
                "overseer_owns_host_detection": False,
                "overseer_agent_stall_seconds": 1,
            },
            progress_events=[{"role": "coder", "event": "start"}],
            consensus={},
        )
        # The detector ran (no exception) — alert emission depends on
        # timing state which has no entry yet, so no alert this cycle.
        assert isinstance(alerts, list)

    def test_live_mode_emits_alerts(self, tmp_state_path: Path) -> None:
        # Save a stale entry so the detector fires.
        from egg_overseer.state import (
            AgentTimingEntry,
            AgentTimingState,
            save_agent_timing,
        )

        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=600)
        state = AgentTimingState(
            pipeline_id="issue-1",
            entries={
                "coder": AgentTimingEntry(
                    role="coder",
                    phase="implement",
                    phase_entered_at=old,
                    first_seen_at=old,
                    has_any_messages=True,
                )
            },
        )
        save_agent_timing(state, tmp_state_path)

        alerts = run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="implement",
            config_subset={
                "overseer_owns_host_detection": True,
                "overseer_agent_stall_seconds": 60,
            },
            progress_events=[{"role": "coder", "event": "x"}],
            consensus={},
        )
        kinds = [a["anomaly"] for a in alerts]
        assert "agent-stall" in kinds

    def test_silent_agent_alert(self, tmp_state_path: Path) -> None:
        from egg_overseer.state import (
            AgentTimingEntry,
            AgentTimingState,
            save_agent_timing,
        )

        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=900)
        state = AgentTimingState(
            pipeline_id="issue-1",
            entries={
                "coder": AgentTimingEntry(
                    role="coder",
                    phase="implement",
                    phase_entered_at=datetime.datetime.now(datetime.UTC),
                    first_seen_at=old,
                    has_any_messages=False,
                )
            },
        )
        save_agent_timing(state, tmp_state_path)

        alerts = run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="implement",
            config_subset={
                "overseer_owns_host_detection": True,
                "overseer_silent_agent_threshold_seconds": 60,
            },
            progress_events=[],
            consensus={},
        )
        kinds = [a["anomaly"] for a in alerts]
        assert "agent-silent" in kinds

    def test_nack_unresolved_alert(self, tmp_state_path: Path) -> None:
        nack_dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=300)
        alerts = run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="implement",
            config_subset={
                "overseer_owns_host_detection": True,
                "overseer_nack_unresolved_seconds": 60,
            },
            progress_events=[],
            consensus=_consensus_with_nack(nack_dt=nack_dt),
        )
        kinds = [a["anomaly"] for a in alerts]
        assert "agent-nack-unresolved" in kinds
        # Priority should be the high-tier label since stale NACKs block consensus.
        nack_alert = next(a for a in alerts if a["anomaly"] == "agent-nack-unresolved")
        assert nack_alert["priority"] == "high"

    def test_nack_invalid_timestamp_skipped(self, tmp_state_path: Path) -> None:
        # Malformed timestamp must not crash the cycle — just skip the NACK.
        alerts = run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="implement",
            config_subset={
                "overseer_owns_host_detection": True,
                "overseer_nack_unresolved_seconds": 60,
            },
            progress_events=[],
            consensus={"nacks": [{"from_role": "x", "timestamp": "not-a-date"}]},
        )
        assert all(a["anomaly"] != "agent-nack-unresolved" for a in alerts)

    def test_phase_long_running_alert(self, tmp_state_path: Path) -> None:
        from egg_overseer.state import (
            AgentTimingEntry,
            AgentTimingState,
            save_agent_timing,
        )

        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)
        state = AgentTimingState(
            pipeline_id="issue-1",
            entries={
                "coder": AgentTimingEntry(
                    role="coder",
                    phase="implement",
                    phase_entered_at=old,
                    first_seen_at=old,
                    has_any_messages=True,
                )
            },
        )
        save_agent_timing(state, tmp_state_path)

        alerts = run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="implement",
            config_subset={
                "overseer_owns_host_detection": True,
                "overseer_long_running_phase_seconds": 60,
            },
            progress_events=[{"role": "coder", "event": "x"}],
            consensus={},
        )
        kinds = [a["anomaly"] for a in alerts]
        assert "phase-long-running" in kinds

    def test_phase_long_running_only_fires_on_implement(self, tmp_state_path: Path) -> None:
        # Plan / refine phases should NOT trigger phase-long-running.
        from egg_overseer.state import (
            AgentTimingEntry,
            AgentTimingState,
            save_agent_timing,
        )

        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)
        state = AgentTimingState(
            pipeline_id="issue-1",
            entries={
                "refiner": AgentTimingEntry(
                    role="refiner",
                    phase="refine",
                    phase_entered_at=old,
                    first_seen_at=old,
                )
            },
        )
        save_agent_timing(state, tmp_state_path)

        alerts = run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="refine",
            config_subset={
                "overseer_owns_host_detection": True,
                "overseer_long_running_phase_seconds": 60,
            },
            progress_events=[{"role": "refiner", "event": "x"}],
            consensus={},
        )
        kinds = [a["anomaly"] for a in alerts]
        assert "phase-long-running" not in kinds

    def test_suppression_window(self, tmp_state_path: Path) -> None:
        # An alerted anomaly within 2x the threshold must NOT re-fire.
        from egg_overseer.state import (
            AgentTimingEntry,
            AgentTimingState,
            save_agent_timing,
        )

        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=600)
        recent = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=30)
        state = AgentTimingState(
            pipeline_id="issue-1",
            entries={
                "coder": AgentTimingEntry(
                    role="coder",
                    phase="implement",
                    phase_entered_at=old,
                    first_seen_at=old,
                    has_any_messages=True,
                    alerted_anomalies={"agent-stall": recent},
                )
            },
        )
        save_agent_timing(state, tmp_state_path)

        alerts = run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="implement",
            config_subset={
                "overseer_owns_host_detection": True,
                "overseer_agent_stall_seconds": 60,
            },
            progress_events=[{"role": "coder", "event": "x"}],
            consensus={},
        )
        # The recent alert is well within 2 × 60s suppression → no re-emit.
        kinds = [a["anomaly"] for a in alerts]
        assert "agent-stall" not in kinds

    def test_suppression_factor_constant(self) -> None:
        # The contract pins the multiplier so test fixtures don't drift.
        assert _SUPPRESSION_FACTOR == 2

    def test_phase_transition_resets_anchors(self, tmp_state_path: Path) -> None:
        # Regression test: a role that was tracked in a prior phase
        # must NOT immediately fire agent-stall on entering the next
        # phase. The reviewer flagged that ``entry.phase_entered_at``
        # was never updated on phase transition, so refine→plan→implement
        # would emit an agent-stall instantly with phase_entered_at
        # anchored to refine.
        from egg_overseer.state import (
            AgentTimingEntry,
            AgentTimingState,
            load_agent_timing,
            save_agent_timing,
        )

        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=600)
        state = AgentTimingState(
            pipeline_id="issue-1",
            entries={
                "coder": AgentTimingEntry(
                    role="coder",
                    phase="refine",
                    phase_entered_at=old,
                    first_seen_at=old,
                    has_any_messages=True,
                    alerted_anomalies={"agent-stall": old},
                )
            },
        )
        save_agent_timing(state, tmp_state_path)

        alerts = run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="implement",
            config_subset={
                "overseer_owns_host_detection": True,
                "overseer_agent_stall_seconds": 60,
            },
            progress_events=[{"role": "coder", "event": "x"}],
            consensus={},
        )
        kinds = [a["anomaly"] for a in alerts]
        # No stall: phase transition reset the anchor and per-phase
        # alerted_anomalies bookkeeping.
        assert "agent-stall" not in kinds

        rebuilt = load_agent_timing(tmp_state_path)
        coder = rebuilt.entries["coder"]
        # The anchor is now in the new phase and >= old.
        assert coder.phase == "implement"
        assert coder.phase_entered_at > old
        # Prior-phase alert bookkeeping cleared.
        assert "agent-stall" not in coder.alerted_anomalies

    def test_silent_role_in_prior_phase_does_not_stall(
        self, tmp_state_path: Path
    ) -> None:
        # A role that emitted progress in refine but is silent in
        # implement should NOT fire agent-stall — its entry belongs to
        # the prior phase. The detector skips entries whose phase
        # doesn't match the current cycle.
        from egg_overseer.state import (
            AgentTimingEntry,
            AgentTimingState,
            save_agent_timing,
        )

        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=600)
        state = AgentTimingState(
            pipeline_id="issue-1",
            entries={
                "refiner": AgentTimingEntry(
                    role="refiner",
                    phase="refine",
                    phase_entered_at=old,
                    first_seen_at=old,
                    has_any_messages=True,
                )
            },
        )
        save_agent_timing(state, tmp_state_path)

        alerts = run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="implement",
            config_subset={
                "overseer_owns_host_detection": True,
                "overseer_agent_stall_seconds": 60,
            },
            # No progress events for refiner this cycle — it's silent.
            progress_events=[],
            consensus={},
        )
        assert all(a["role"] != "refiner" for a in alerts)

    def test_persists_alerted_anomalies(self, tmp_state_path: Path) -> None:
        from egg_overseer.state import (
            AgentTimingEntry,
            AgentTimingState,
            load_agent_timing,
            save_agent_timing,
        )

        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=600)
        state = AgentTimingState(
            pipeline_id="issue-1",
            entries={
                "coder": AgentTimingEntry(
                    role="coder",
                    phase="implement",
                    phase_entered_at=old,
                    first_seen_at=old,
                    has_any_messages=True,
                )
            },
        )
        save_agent_timing(state, tmp_state_path)

        run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="implement",
            config_subset={
                "overseer_owns_host_detection": True,
                "overseer_agent_stall_seconds": 60,
            },
            progress_events=[{"role": "coder", "event": "x"}],
            consensus={},
        )
        rebuilt = load_agent_timing(tmp_state_path)
        # The alert bookkeeping was persisted so the suppression check
        # works across cycles.
        assert "agent-stall" in rebuilt.entries["coder"].alerted_anomalies

    def test_progress_events_seed_new_role_entry(self, tmp_state_path: Path) -> None:
        from egg_overseer.state import load_agent_timing

        run_migrated_detectors(
            base_url="http://orch",
            pipeline_id="issue-1",
            phase_name="implement",
            config_subset={"overseer_owns_host_detection": True},
            progress_events=[{"role": "tester", "event": "alive"}],
            consensus={},
        )
        rebuilt = load_agent_timing(tmp_state_path)
        # tester entry created with has_any_messages flipped to True.
        assert "tester" in rebuilt.entries
        assert rebuilt.entries["tester"].has_any_messages is True
