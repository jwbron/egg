"""Regression tests for routes/pipelines.py helpers.

Covers issue #1783: the BRC/consensus timeout path used bare relative
imports that crashed under k3s's top-level-module layout.

Covers issue #2243: the BRC progress gate must defer the auto
consensus-failure decision while the bus or container heartbeats have
fired within the gate window.

Covers issue #2264: the consensus-timeout fallback used to open a
``choice``-typed HITL decision; it now publishes an ``OVERSEER_ALERT``
so the SDLC skill surfaces it as a non-blocking notification rather
than a binary gate.
"""

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from events import EventType
from models import Pipeline, PipelinePhase
from routes.pipelines import (
    _check_brc_progress_gate,
    _handle_brc_consensus_timeout,
    _latest_active_role_heartbeat,
)


@pytest.fixture
def pipeline():
    p = Pipeline(id="issue-1783-test", issue_number=1783, repo="owner/repo")
    p.current_phase = PipelinePhase.REFINE
    return p


def _make_store():
    """MagicMock state store kept for call-site compatibility.

    Issue #2264 removed the ``_persist_hitl_decision`` call from the
    timeout handler, so the store argument is unused in the new alert
    flow. The fixture is retained so the test signatures don't drift
    from the production call site (which still passes the state store).
    """
    return MagicMock()


def _capture_alerts():
    """Return ``(patch_context, alerts_list)`` for capturing OVERSEER_ALERT writes.

    Patches ``routes.pipelines._get_message_store`` to return a factory
    that returns a fake store whose ``add_message`` appends each message
    to ``alerts_list``. Tests assert against the captured list.
    """
    alerts: list = []
    fake_store = MagicMock()
    fake_store.add_message.side_effect = lambda msg: alerts.append(msg) or msg
    factory = MagicMock(return_value=fake_store)
    return patch("routes.pipelines._get_message_store", return_value=factory), alerts


class TestHandleBrcConsensusTimeout:
    """The timeout handler publishes an OVERSEER_ALERT (issue #2264).

    Migration from the old auto-``choice`` HITL decision to a
    non-blocking ``OVERSEER_ALERT`` notification. The SDLC skill's
    existing alert flow (Check agent logs / Acknowledge / Cancel
    pipeline) handles operator interaction; the platform no longer
    gates the pipeline on a binary choice.
    """

    @patch("routes.pipelines._emit_event")
    def test_no_brc_tracker_publishes_overseer_alert(self, mock_emit, pipeline):
        store = _make_store()
        capture, alerts = _capture_alerts()
        with (
            patch("peer_consensus.get_peer_consensus_tracker", return_value=None),
            capture,
        ):
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=["reviewer_code"],
                store=store,
            )

        # No HITL decision opened — the protocol shape is now a notification.
        assert len(pipeline.decisions) == 0
        # CONSENSUS_TIMEOUT audit event still fires on the no-tracker path.
        mock_emit.assert_called_once_with(
            EventType.CONSENSUS_TIMEOUT,
            pipeline.id,
            data={
                "timeout_minutes": 30.0,
                "blocking_agents": ["reviewer_code"],
            },
        )
        # OVERSEER_ALERT published with the issue #2264 metadata schema.
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.message_type == "OVERSEER_ALERT"
        assert alert.from_role == "orchestrator"
        assert alert.to_role == "all"
        # Subject role slot follows the SDLC-skill convention
        # ``<anomaly_type>: <agent_role> [<priority>]`` so "Check
        # agent logs" extracts a role the host can pass to
        # ``get_container_logs``. With one blocking agent reported,
        # that role lands in the subject.
        assert alert.subject == "consensus-timeout: reviewer_code [medium]"
        assert alert.metadata["anomaly_type"] == "consensus-timeout"
        assert alert.metadata["consensus_timeout_minutes"] == 30
        assert alert.metadata["blocking_agents"] == ["reviewer_code"]
        assert alert.metadata["priority"] == "medium"
        assert alert.metadata["phase"] == PipelinePhase.REFINE.value
        # No tracker / heartbeat info available in this scenario.
        assert alert.metadata["latest_proposal_at"] is None
        assert alert.metadata["latest_heartbeat_at"] is None
        # slice_id absent from metadata when call-site passes None.
        assert "slice_id" not in alert.metadata

    @patch("routes.pipelines._emit_event")
    def test_brc_escalate_publishes_high_priority_alert(self, mock_emit, pipeline):
        store = _make_store()
        tracker = MagicMock()
        # The tracker's escalate result carries ``critical_blockers``
        # — the alert must narrow to those rather than including
        # advisory roles from the caller-supplied ``blocking_agents``.
        tracker.handle_timeout.return_value = {
            "action": "escalate",
            "critical_blockers": [
                {
                    "reviewer_role": "reviewer_code",
                    "producer_role": "coder",
                    "state": "pending",
                }
            ],
        }
        tracker.is_timeout_handled.return_value = True
        proposal_ts = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
        tracker.get_latest_proposal_timestamp.return_value = proposal_ts

        capture, alerts = _capture_alerts()
        with (
            patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
            capture,
        ):
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline.id,
                consensus_timeout=1800.0,
                # Caller-supplied list includes an advisory role; the
                # alert metadata must drop it on the escalate path.
                blocking_agents=["reviewer_code", "tester_advisory"],
                store=store,
                slice_id="slice-1",
            )

        assert len(pipeline.decisions) == 0
        # No CONSENSUS_TIMEOUT event on the escalate path — the
        # tracker already emitted CONSENSUS_FAILURE internally.
        mock_emit.assert_not_called()
        assert len(alerts) == 1
        alert = alerts[0]
        # First critical-blocker role lands in the subject's role slot.
        assert alert.subject == "consensus-timeout: reviewer_code [high]"
        assert alert.metadata["priority"] == "high"
        assert alert.metadata["latest_proposal_at"] == proposal_ts.isoformat()
        assert alert.metadata["slice_id"] == "slice-1"
        # Critical blockers only — advisory ``tester_advisory`` excluded.
        assert alert.metadata["blocking_agents"] == ["reviewer_code", "coder"]

    @patch("routes.pipelines._emit_event")
    def test_brc_escalate_falls_back_to_caller_blocking_agents(self, mock_emit, pipeline):
        # Defensive: if the tracker's escalate result omits
        # ``critical_blockers`` (older return shape, or the matrix
        # cleared between handle_timeout calls), fall back to the
        # caller-supplied list rather than emitting an alert with no
        # roles in the subject.
        store = _make_store()
        tracker = MagicMock()
        tracker.handle_timeout.return_value = {"action": "escalate"}
        tracker.is_timeout_handled.return_value = True
        tracker.get_latest_proposal_timestamp.return_value = None

        capture, alerts = _capture_alerts()
        with (
            patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
            capture,
        ):
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=["reviewer_code"],
                store=store,
            )

        mock_emit.assert_not_called()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.subject == "consensus-timeout: reviewer_code [high]"
        assert alert.metadata["blocking_agents"] == ["reviewer_code"]

    @patch("routes.pipelines._emit_event")
    def test_brc_handled_without_escalate_no_alert(self, mock_emit, pipeline):
        # The advisory-only path (proceed_with_notification) was
        # silent before #2264 and stays silent: the tracker has
        # already emitted CONSENSUS_TIMEOUT internally and the handler
        # does not re-notify the operator.
        store = _make_store()
        tracker = MagicMock()
        tracker.handle_timeout.return_value = {"action": "proceed"}
        tracker.is_timeout_handled.return_value = True
        capture, alerts = _capture_alerts()

        with (
            patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
            capture,
        ):
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=[],
                store=store,
            )

        assert len(pipeline.decisions) == 0
        assert alerts == []
        mock_emit.assert_not_called()

    @patch("routes.pipelines.logger")
    @patch("routes.pipelines._emit_event")
    def test_tracker_import_error_falls_back_to_alert(self, mock_emit, mock_logger, pipeline):
        # Regression test for issue #1783: bare relative import raised
        # ImportError under k3s. After #2264 the fallback is an
        # OVERSEER_ALERT rather than a HITL decision.
        store = _make_store()
        capture, alerts = _capture_alerts()
        with (
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                side_effect=ImportError("simulated k3s import failure"),
            ),
            capture,
        ):
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=["reviewer_code"],
                store=store,
            )

        # The warning log records the import failure.
        assert any(
            "BRC timeout check failed" in (call.args[0] if call.args else "")
            for call in mock_logger.warning.call_args_list
        )
        # Fallback OVERSEER_ALERT is still published.
        assert len(pipeline.decisions) == 0
        assert len(alerts) == 1
        assert "[medium]" in alerts[0].subject
        mock_emit.assert_called_once_with(
            EventType.CONSENSUS_TIMEOUT,
            pipeline.id,
            data={
                "timeout_minutes": 30.0,
                "blocking_agents": ["reviewer_code"],
            },
        )

    @patch("routes.pipelines.logger")
    @patch("routes.pipelines._emit_event")
    def test_message_store_failure_is_logged_not_swallowed(self, mock_emit, mock_logger, pipeline):
        # Issue #2264: the publish helper must log message-store add
        # failures with ``exc_info`` rather than silently dropping
        # them, so a wedged broker doesn't render the consensus
        # timeout invisible to operators (mirrors the #1783 contract
        # for the old _persist_hitl_decision path).
        store = _make_store()
        bad_store = MagicMock()
        bad_store.add_message.side_effect = RuntimeError("broker boom")
        factory = MagicMock(return_value=bad_store)
        with (
            patch("peer_consensus.get_peer_consensus_tracker", return_value=None),
            patch("routes.pipelines._get_message_store", return_value=factory),
        ):
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=[],
                store=store,
            )

        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args and "Failed to publish consensus-timeout OVERSEER_ALERT" in call.args[0]
        ]
        assert warning_calls, "message store failure must be logged with exc_info"
        _, kwargs = warning_calls[0]
        assert kwargs.get("pipeline_id") == pipeline.id
        assert kwargs.get("exc_info") is True
        # No alert was successfully captured but the audit event
        # still fired on the no-tracker path.
        mock_emit.assert_called_once()


class TestBrcProgressGate:
    """Issue #2243 — defer the auto consensus-failure HITL decision while
    BRC bus or container heartbeats have advanced within the gate window.

    The gate is the operator-friendly half of the fix: previously, at
    ``consensus_timeout_minutes`` the orchestrator opened a `choice`
    decision unconditionally, even when producers were minutes away
    from their first commit (decision-15 / decision-17 on
    ``issue-1557-v2``).  The gate keeps the polling loop polling while
    signals are alive, and only opens the decision once the bus and
    containers have both gone quiet for ``gate_seconds``.
    """

    PIPELINE_ID = "issue-2243-test"

    def _patch_tracker(self, latest_progress: datetime | None):
        tracker = MagicMock()
        tracker.get_latest_progress_timestamp.return_value = latest_progress
        return patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker)

    def _patch_health_monitor(self, last_heartbeats: dict[str, float] | None):
        if last_heartbeats is None:
            return patch("health_monitor.get_health_monitor", return_value=None)
        hm = MagicMock()
        hm._lock = MagicMock()
        hm._lock.__enter__ = MagicMock(return_value=hm._lock)
        hm._lock.__exit__ = MagicMock(return_value=False)
        hm._last_heartbeat = dict(last_heartbeats)
        return patch("health_monitor.get_health_monitor", return_value=hm)

    def test_disabled_gate_returns_no_defer(self):
        defer, reason = _check_brc_progress_gate(self.PIPELINE_ID, None, ["coder"], 0)
        assert defer is False
        assert reason is None

    def test_recent_proposal_defers(self):
        recent = datetime.now(UTC) - timedelta(seconds=30)
        with (
            self._patch_tracker(recent),
            self._patch_health_monitor(None),
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder"], gate_seconds=300
            )
        assert defer is True
        assert reason is not None and "BRC bus" in reason

    def test_stale_proposal_does_not_defer(self):
        stale = datetime.now(UTC) - timedelta(seconds=600)
        with (
            self._patch_tracker(stale),
            self._patch_health_monitor(None),
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder"], gate_seconds=300
            )
        assert defer is False
        assert reason is None

    def test_recent_heartbeat_defers_when_bus_silent(self):
        # Bus completely silent (decision-17 shape: coder mid-merge-conflict
        # before its first CONSENSUS_PROPOSE), but the container is still
        # emitting heartbeats.  The gate should still defer.
        recent_hb = time.time() - 30
        with (
            self._patch_tracker(None),
            self._patch_health_monitor({"coder": recent_hb}),
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder", "tester"], gate_seconds=300
            )
        assert defer is True
        assert reason is not None and "heartbeat" in reason

    def test_stale_heartbeat_does_not_defer(self):
        stale_hb = time.time() - 600
        with (
            self._patch_tracker(None),
            self._patch_health_monitor({"coder": stale_hb}),
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder"], gate_seconds=300
            )
        assert defer is False
        assert reason is None

    def test_heartbeat_for_inactive_role_is_ignored(self):
        # Cross-phase pollution: the singleton HealthMonitor's
        # ``_last_heartbeat`` may carry a stale entry for a role that
        # isn't part of the current phase.  The gate must filter so a
        # ghost heartbeat from a finished phase doesn't keep the gate
        # deferring forever.
        recent_hb = time.time() - 30
        with (
            self._patch_tracker(None),
            self._patch_health_monitor({"refiner": recent_hb}),
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder", "tester"], gate_seconds=300
            )
        assert defer is False
        assert reason is None

    def test_empty_active_roles_does_not_defer_on_heartbeat(self):
        # Contract: an empty ``active_role_names`` means the caller has
        # no live containers to gate on, so match nothing rather than
        # accept every stale heartbeat in the singleton HealthMonitor.
        # ``_run_concurrent_phase`` exits before reaching the gate when
        # there are no live containers, but the contract is explicit so
        # a future caller can't accidentally widen the gate.
        recent_hb = time.time() - 30
        with (
            self._patch_tracker(None),
            self._patch_health_monitor({"coder": recent_hb, "refiner": recent_hb}),
        ):
            defer, reason = _check_brc_progress_gate(self.PIPELINE_ID, None, [], gate_seconds=300)
        assert defer is False
        assert reason is None

    def test_no_signals_returns_no_defer(self):
        with (
            self._patch_tracker(None),
            self._patch_health_monitor({}),
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder"], gate_seconds=300
            )
        assert defer is False
        assert reason is None

    def test_tracker_failure_logged_and_not_treated_as_defer(self):
        # If the tracker collector raises, treat it as "no signal" rather
        # than as a defer — a crashed signal source must never silently
        # keep us off the HITL surface.
        with (
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                side_effect=RuntimeError("simulated tracker failure"),
            ),
            self._patch_health_monitor({}),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder"], gate_seconds=300
            )
        assert defer is False
        assert reason is None
        assert any(
            "tracker check failed" in (call.args[0] if call.args else "")
            for call in mock_logger.warning.call_args_list
        )

    def test_heartbeat_failure_logged_and_not_treated_as_defer(self):
        # Same as above for the heartbeat collector.
        bad_hm = MagicMock()
        bad_hm._lock = MagicMock()
        bad_hm._lock.__enter__ = MagicMock(side_effect=RuntimeError("hm boom"))
        bad_hm._lock.__exit__ = MagicMock(return_value=False)
        with (
            self._patch_tracker(None),
            patch("health_monitor.get_health_monitor", return_value=bad_hm),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder"], gate_seconds=300
            )
        assert defer is False
        assert reason is None
        assert any(
            "heartbeat check failed" in (call.args[0] if call.args else "")
            for call in mock_logger.warning.call_args_list
        )


class TestLatestActiveRoleHeartbeat:
    """Issue #2264 — direct coverage for the alert-side heartbeat helper.

    ``_latest_active_role_heartbeat`` mirrors the heartbeat half of
    ``_check_brc_progress_gate`` but writes ``latest_heartbeat_at``
    into the consensus-timeout ``OVERSEER_ALERT`` metadata. The
    BRC-progress-gate tests above only exercise the gate's inline
    code; this class covers the helper's branches directly.
    """

    def _patch_health_monitor(self, last_heartbeats: dict[str, float] | None):
        if last_heartbeats is None:
            return patch("health_monitor.get_health_monitor", return_value=None)
        hm = MagicMock()
        hm._lock = MagicMock()
        hm._lock.__enter__ = MagicMock(return_value=hm._lock)
        hm._lock.__exit__ = MagicMock(return_value=False)
        hm._last_heartbeat = dict(last_heartbeats)
        return patch("health_monitor.get_health_monitor", return_value=hm)

    def test_empty_active_roles_returns_none(self):
        # Short-circuit before any health-monitor lookup. The alert
        # then carries ``latest_heartbeat_at: null``.
        assert _latest_active_role_heartbeat([]) is None

    def test_recent_heartbeat_returns_datetime(self):
        recent_hb = time.time() - 30
        with self._patch_health_monitor({"coder": recent_hb}):
            result = _latest_active_role_heartbeat(["coder", "tester"])
        assert isinstance(result, datetime)
        assert result.tzinfo is UTC
        # Within 1s of the input wall-clock value.
        assert abs(result.timestamp() - recent_hb) < 1

    def test_picks_most_recent_across_active_roles(self):
        older = time.time() - 120
        newer = time.time() - 10
        with self._patch_health_monitor({"coder": older, "tester": newer}):
            result = _latest_active_role_heartbeat(["coder", "tester"])
        assert result is not None
        assert abs(result.timestamp() - newer) < 1

    def test_inactive_role_heartbeats_are_ignored(self):
        # Cross-phase pollution: the singleton ``HealthMonitor`` may
        # still carry a ``refiner`` heartbeat during a coder phase.
        # The helper must filter to the active set so the alert
        # doesn't surface a ghost timestamp from a finished phase.
        ghost_hb = time.time() - 30
        with self._patch_health_monitor({"refiner": ghost_hb}):
            result = _latest_active_role_heartbeat(["coder", "tester"])
        assert result is None

    def test_monitor_unavailable_returns_none(self):
        with self._patch_health_monitor(None):
            result = _latest_active_role_heartbeat(["coder"])
        assert result is None

    def test_no_heartbeats_returns_none(self):
        with self._patch_health_monitor({}):
            result = _latest_active_role_heartbeat(["coder"])
        assert result is None

    def test_lookup_failure_logs_with_exc_info_and_returns_none(self):
        # A crashed signal collector must NOT break the alert
        # publish — the helper logs at WARNING with ``exc_info=True``
        # and returns ``None`` so the alert proceeds with a null
        # ``latest_heartbeat_at``.
        bad_hm = MagicMock()
        bad_hm._lock = MagicMock()
        bad_hm._lock.__enter__ = MagicMock(side_effect=RuntimeError("hm boom"))
        bad_hm._lock.__exit__ = MagicMock(return_value=False)
        with (
            patch("health_monitor.get_health_monitor", return_value=bad_hm),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            result = _latest_active_role_heartbeat(["coder"])
        assert result is None
        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args and "heartbeat lookup failed" in call.args[0]
        ]
        assert warning_calls, "heartbeat-lookup failure must be logged"
        _, kwargs = warning_calls[0]
        assert kwargs.get("exc_info") is True
