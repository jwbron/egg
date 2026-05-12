"""Phase-aware consensus timeouts honored end-to-end — issue #2635 starting point 2.

The unit tier covers ``resolve_consensus_timeout_minutes`` exhaustively
(see ``orchestrator/tests/test_models.py::TestResolveConsensusTimeoutMinutes``)
but no test verifies the *full* contract: config → resolver →
``_handle_brc_consensus_timeout`` triage → CONSENSUS_TIMEOUT /
CONSENSUS_FAILURE event + (on critical-blocker / fallback paths)
``OVERSEER_ALERT`` message whose ``consensus_timeout_minutes``
metadata reflects the configured value.

Wallclock-bounded "fires within N±5s" assertions aren't feasible
without standing up the real wait loop (which needs containers).
The contract that actually matters to operators is **"the configured
phase-specific value is the one that lands in the alert"** — that's
what's verified here, by composing the resolver with the timeout
handler in-process.

## Gap notes (issue text vs. current code)

* Issue text references ``phase_configs.plan.consensus_timeout_s = 30``.
  The actual field is ``PipelineConfig.consensus_timeout_minutes_plan``
  (minutes, lives on ``PipelineConfig`` rather than the contract's
  ``phase_configs``).  Tests use the real field name.
* An idle reviewer with no ACK/NACK **does** appear in
  ``get_all_blocking_edges()`` — the matrix constructor pre-populates
  one PENDING entry per graph edge, so a producer waiting on a
  no-show reviewer drives the timeout handler to the appropriate
  branch (critical → escalate, advisory-only → notify).  The
  ``test_idle_*`` cases below pin this end-to-end; #2653 was filed on
  the misread that ``_entries`` is empty until ``record_ack`` /
  ``record_nack`` populates it.
* The **advisory-only** branch is intentionally silent at the
  OVERSEER_ALERT layer — see ``test_pipelines_routes.py::
  test_brc_handled_without_escalate_no_alert``.  Operator visibility
  for advisory-only timeouts is the in-tracker CONSENSUS_TIMEOUT
  event, not an alert message.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from _helpers import ack_payload, make_tracker, nack_payload, propose_payload
from events import EventType
from models import (
    PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN,
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
    resolve_consensus_timeout_minutes,
)
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph
from routes.pipelines import _handle_brc_consensus_timeout

pytestmark = pytest.mark.integration


def _make_pipeline(pipeline_id: str, config: PipelineConfig, phase: PipelinePhase) -> Pipeline:
    return Pipeline(
        id=pipeline_id,
        issue_number=2635,
        repo="owner/repo",
        branch=f"egg/{pipeline_id}",
        status=PipelineStatus.RUNNING,
        current_phase=phase,
        config=config,
    )


def _capture_alerts() -> tuple[Any, list[Any]]:
    """Return ``(patch_context, alerts_list)`` for capturing OVERSEER_ALERT writes.

    Mirrors ``orchestrator/tests/test_pipelines_routes.py::_capture_alerts``
    so the in-process integration tests use the same alert-capture
    seam as the unit tier.  The handler reaches the alert store via
    ``routes.pipelines._get_message_store``; we patch that to inject
    a list-backed fake.
    """
    alerts: list[Any] = []
    fake_store = MagicMock()
    fake_store.add_message.side_effect = lambda msg: alerts.append(msg) or msg
    factory = MagicMock(return_value=fake_store)
    return (
        patch("routes.pipelines._get_message_store", return_value=factory),
        alerts,
    )


class TestPhaseAwareTimeoutPropagation:
    """Triage branches honor the configured per-phase timeout.

    Three branches of ``_handle_brc_consensus_timeout``:

    1. No blocking edges → ``proceed`` (no event, no alert).
    2. Advisory blockers only → ``CONSENSUS_TIMEOUT`` (from
       ``handle_timeout``), no alert.
    3. Critical blockers → ``CONSENSUS_FAILURE`` + high-priority
       ``OVERSEER_ALERT`` whose ``consensus_timeout_minutes``
       metadata is the operator-visible knob.
    """

    def test_critical_blocker_lands_phase_override_in_alert(
        self, event_capture, filter_events
    ) -> None:
        """``consensus_timeout_minutes_plan=45`` flows to OVERSEER_ALERT metadata.

        Critical-blocker path is the chosen probe because it
        produces both the audit event AND the operator-facing
        alert.  Together they pin the resolver→handler→alert chain.
        """
        pipeline_id = "issue-2635-timeout-critical-45"
        config = PipelineConfig(consensus_timeout_minutes_plan=45)

        # Resolver picks up the per-phase override.
        assert resolve_consensus_timeout_minutes(config, "plan") == 45
        assert (
            resolve_consensus_timeout_minutes(config, "refine")
            == PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN["refine"]
        )

        pipeline = _make_pipeline(pipeline_id, config, PipelinePhase.PLAN)
        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        tracker = make_tracker(pipeline_id, graph)
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))
        tracker.handle_nack(
            "reviewer_code",
            "coder",
            {"nack_version": 1, **nack_payload(reason="critical regress")},
        )

        capture, alerts = _capture_alerts()
        with capture:
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline_id,
                consensus_timeout=45 * 60,
                blocking_agents=["reviewer_code"],
                store=None,
                slice_id=None,
                active_role_names=["coder", "reviewer_code"],
            )

        # CONSENSUS_FAILURE with ``type=timeout_critical`` is the audit event.
        failures = filter_events(
            event_capture(),
            pipeline_id=pipeline_id,
            event_type=EventType.CONSENSUS_FAILURE,
        )
        assert any(e.data.get("type") == "timeout_critical" for e in failures)

        # OVERSEER_ALERT carries the configured value in metadata.
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.metadata["consensus_timeout_minutes"] == 45
        assert alert.metadata["priority"] == "high"
        assert alert.metadata["phase"] == "plan"
        # Alert lists both endpoints of the blocking critical edge —
        # operators see the reviewer that NACKed and the producer that
        # couldn't get acked.
        assert "reviewer_code" in alert.metadata["blocking_agents"]

    def test_advisory_only_path_is_silent_at_alert_layer(
        self, event_capture, filter_events
    ) -> None:
        """Advisory-only blockers fire CONSENSUS_TIMEOUT but no OVERSEER_ALERT.

        Pins the design constraint documented in
        ``test_pipelines_routes.py::test_brc_handled_without_escalate_no_alert``
        at the integration boundary: a future refactor that adds an
        alert here would change the operator-facing surface and
        should be a deliberate decision.
        """
        pipeline_id = "issue-2635-timeout-advisory"
        pipeline = _make_pipeline(
            pipeline_id,
            PipelineConfig(consensus_timeout_minutes_plan=10),
            PipelinePhase.PLAN,
        )
        graph = ReviewGraph([ReviewEdge("reviewer_contract", "coder", ReviewCriticality.ADVISORY)])
        tracker = make_tracker(pipeline_id, graph)
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))
        tracker.handle_nack(
            "reviewer_contract",
            "coder",
            {"nack_version": 1, **nack_payload(reason="advisory NACK")},
        )

        capture, alerts = _capture_alerts()
        with capture:
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline_id,
                consensus_timeout=10 * 60,
                blocking_agents=["reviewer_contract"],
                store=None,
                slice_id=None,
                active_role_names=["coder", "reviewer_contract"],
            )

        timeouts = filter_events(
            event_capture(),
            pipeline_id=pipeline_id,
            event_type=EventType.CONSENSUS_TIMEOUT,
        )
        assert len(timeouts) == 1
        assert timeouts[0].data["type"] == "timeout_advisory_only"
        # Operator-facing alert layer stays silent on the advisory branch.
        assert alerts == []

    def test_idle_critical_reviewer_fires_overseer_alert(
        self, event_capture, filter_events
    ) -> None:
        """A producer waiting on a no-show critical reviewer escalates at timeout.

        Pins the operator-facing alert for the case operators most
        care about: producer proposed, critical reviewer never showed
        up.  The matrix constructor pre-populates one PENDING edge
        per graph edge, so ``get_all_blocking_edges`` returns the
        idle reviewer and the handler takes the escalate branch.
        Issue #2653 was filed on the assumption that this branch was
        silent; the test verifies it is not.
        """
        pipeline_id = "issue-2653-timeout-idle-critical"
        config = PipelineConfig(consensus_timeout_minutes_plan=20)
        pipeline = _make_pipeline(pipeline_id, config, PipelinePhase.PLAN)
        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        tracker = make_tracker(pipeline_id, graph)
        # Producer proposes; reviewer never ACKs or NACKs.
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))

        capture, alerts = _capture_alerts()
        with capture:
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline_id,
                consensus_timeout=20 * 60,
                blocking_agents=["reviewer_code"],
                store=None,
                slice_id=None,
                active_role_names=["coder", "reviewer_code"],
            )

        failures = filter_events(
            event_capture(),
            pipeline_id=pipeline_id,
            event_type=EventType.CONSENSUS_FAILURE,
        )
        assert any(e.data.get("type") == "timeout_critical" for e in failures)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.metadata["consensus_timeout_minutes"] == 20
        assert alert.metadata["priority"] == "high"
        assert "reviewer_code" in alert.metadata["blocking_agents"]

    def test_idle_advisory_reviewer_fires_notification(self, event_capture, filter_events) -> None:
        """Idle advisory reviewer routes to the advisory-only branch, not silence.

        Pairs with the critical case to lock in that the
        pre-populated PENDING edges respect the reviewer's
        criticality — advisory-only stays silent at the alert layer
        but still emits ``CONSENSUS_TIMEOUT``.
        """
        pipeline_id = "issue-2653-timeout-idle-advisory"
        pipeline = _make_pipeline(
            pipeline_id,
            PipelineConfig(consensus_timeout_minutes_plan=15),
            PipelinePhase.PLAN,
        )
        graph = ReviewGraph([ReviewEdge("reviewer_contract", "coder", ReviewCriticality.ADVISORY)])
        tracker = make_tracker(pipeline_id, graph)
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))

        capture, alerts = _capture_alerts()
        with capture:
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline_id,
                consensus_timeout=15 * 60,
                blocking_agents=["reviewer_contract"],
                store=None,
                slice_id=None,
                active_role_names=["coder", "reviewer_contract"],
            )

        timeouts = filter_events(
            event_capture(),
            pipeline_id=pipeline_id,
            event_type=EventType.CONSENSUS_TIMEOUT,
        )
        assert len(timeouts) == 1
        assert timeouts[0].data["type"] == "timeout_advisory_only"
        assert alerts == []

    def test_consensus_reached_before_timeout_is_no_op(self, event_capture, filter_events) -> None:
        """If consensus reached before timeout fires, handler emits no alert."""
        pipeline_id = "issue-2635-timeout-noop"
        pipeline = _make_pipeline(pipeline_id, PipelineConfig(), PipelinePhase.PLAN)
        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        tracker = make_tracker(pipeline_id, graph)
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))
        tracker.handle_ack("reviewer_code", "coder", {"ack_version": 1, **ack_payload()})
        tracker.handle_confirmed("coder")
        tracker.handle_confirmed("reviewer_code")
        assert tracker.evaluate()["is_complete"] is True

        capture, alerts = _capture_alerts()
        with capture:
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline_id,
                consensus_timeout=60.0,
                blocking_agents=[],
                store=None,
                slice_id=None,
                active_role_names=["coder", "reviewer_code"],
            )

        events = event_capture()
        assert (
            filter_events(events, pipeline_id=pipeline_id, event_type=EventType.CONSENSUS_TIMEOUT)
            == []
        )
        late_failures = [
            e
            for e in filter_events(
                events, pipeline_id=pipeline_id, event_type=EventType.CONSENSUS_FAILURE
            )
            if e.data.get("type", "").startswith("timeout")
        ]
        assert late_failures == []
        assert alerts == []


class TestPhaseAwareTimeoutResolutionPrecedence:
    """Resolver precedence integrates with the handler.

    The unit-tier resolver tests cover the precedence math
    (``test_models.py::TestResolveConsensusTimeoutMinutes``); this
    suite asserts the **handler's alert metadata** matches across
    the three precedence tiers — guards against a future refactor
    where the resolver and the alert metadata fall out of sync.
    """

    @pytest.mark.parametrize(
        ("config_kwargs", "phase", "expected_minutes"),
        [
            ({"consensus_timeout_minutes_plan": 15}, "plan", 15),
            ({"consensus_timeout_minutes": 22}, "plan", 22),
            ({}, "plan", PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN["plan"]),
            ({}, "refine", PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN["refine"]),
            ({}, "implement", PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN["implement"]),
        ],
    )
    def test_resolved_minutes_match_alert_metadata(
        self, config_kwargs, phase, expected_minutes
    ) -> None:
        pipeline_id = f"issue-2635-precedence-{phase}-{expected_minutes}"
        config = PipelineConfig(**config_kwargs)
        assert resolve_consensus_timeout_minutes(config, phase) == expected_minutes

        pipeline = _make_pipeline(pipeline_id, config, PipelinePhase(phase))
        # Critical reviewer NACKs so the handler takes the escalate
        # branch — that's the path that publishes an alert.
        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        tracker = make_tracker(pipeline_id, graph)
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))
        tracker.handle_nack(
            "reviewer_code",
            "coder",
            {"nack_version": 1, **nack_payload(reason="regress")},
        )

        capture, alerts = _capture_alerts()
        with capture:
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline_id,
                consensus_timeout=expected_minutes * 60,
                blocking_agents=["reviewer_code"],
                store=None,
                slice_id=None,
                active_role_names=["coder", "reviewer_code"],
            )

        assert len(alerts) == 1
        assert alerts[0].metadata["consensus_timeout_minutes"] == expected_minutes
        assert alerts[0].metadata["priority"] == "high"
        assert alerts[0].metadata["phase"] == phase
