"""OverseerMonitor consensus-stall detection (post-consensus #1911 + incomplete #1471).

Decomposed from the pre-split ``overseer/monitor.py`` (#3312, slice-8).
``_get_state_store`` is reached through the package barrel (``_pkg``) so
``patch("overseer.monitor._get_state_store")`` keeps injecting a fake store.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import overseer.monitor as _pkg

from . import logger


def _load_pipeline_for_transition_check(self):
    """Load the current Pipeline model for the post-consensus-stall
    short-circuit check (#1911).

    Uses ``state_store.get_state_store`` keyed on ``EGG_REPO_PATH``
    so a missing env var or missing state-store module degrades
    gracefully to ``None`` — the detector then falls through to the
    existing grace-period logic (fail open).  Tests patch
    ``overseer.monitor._get_state_store`` to inject a fake store.
    """
    try:
        if _pkg._get_state_store is None:
            return None
        repo_path = os.environ.get("EGG_REPO_PATH")
        if not repo_path:
            return None
        store = _pkg._get_state_store(repo_path)
        return store.load_pipeline(self.pipeline_id)
    except Exception:
        # Fail open — see the caller's docstring.  Any failure to
        # load the pipeline must not suppress a real stall alert.
        logger.debug(
            "Post-consensus stall: failed to load pipeline for short-circuit check",
            exc_info=True,
        )
        return None


async def _check_post_consensus_stall(self, consensus: dict, pipeline_status_str: str) -> None:
    """Detect and escalate when consensus is complete but phase hasn't transitioned.

    Includes deduplication (only fires once) and a grace period of 3 poll
    cycles to avoid false positives during normal phase transitions.

    Args:
        consensus: Current consensus status dict.
        pipeline_status_str: Current pipeline status string (e.g. "running").
    """
    if not consensus.get("is_complete"):
        # Consensus not complete — reset tracking state
        self._post_consensus_stall_reported = False
        self._post_consensus_stall_first_seen = None
        return
    if pipeline_status_str != "running":
        return

    # Short-circuit when ``current_phase`` has advanced past implement (#1911).
    # The "post-consensus-push-stall" detector fires during a
    # phase-transition window and previously mis-classified a
    # legitimate transition as a stall. The original short-circuit
    # also checked ``pipeline.pr_number`` / the (now-removed)
    # ``phases["pr"].artifacts["pr_url"]`` arm because, pre-#2777,
    # both signals only flipped at the end of a finished
    # implement→PR transition.
    #
    # Under #2777 (cq-4 / TASK-2-2) the PR phase was removed,
    # IMPLEMENT is terminal, and ``pipeline.pr_number`` is now
    # populated up-front by ``_open_context_pr_at_implement_start``
    # at implement-start — so it is *not* a transition-completion
    # signal anymore and gating on it would silently suppress
    # detection for the entire bug window this detector exists to
    # catch. We deliberately drop that arm. The legacy
    # ``current_phase_value != "implement"`` arm is sufficient: if
    # consensus completes during implement and the post-consensus
    # transition succeeds, ``current_phase`` advances and we
    # short-circuit; if that transition itself hangs,
    # ``current_phase`` stays on ``implement`` and the detector
    # *should* fire after the grace period — that is the bug it was
    # designed to catch.
    #
    # Fall open on *any* exception so we never mask a genuine stall
    # on a bug in the short-circuit.
    pipeline = self._load_pipeline_for_transition_check()
    if pipeline is not None:
        try:
            current_phase_value = getattr(getattr(pipeline, "current_phase", None), "value", None)
            if current_phase_value and current_phase_value != "implement":
                # Reset first-seen so a genuinely subsequent stall
                # still gets its own grace period.
                self._post_consensus_stall_first_seen = None
                return
        except Exception:
            # Fail open — don't suppress alerts on a bug in the short-circuit.
            logger.debug(
                "Post-consensus stall short-circuit check raised; falling through",
                exc_info=True,
            )

    # Already escalated — don't spam
    if self._post_consensus_stall_reported:
        return

    # Grace period: wait 3 poll cycles before escalating to allow
    # normal phase transition to complete
    poll_interval = getattr(self.config, "overseer_poll_interval_seconds", 30)
    grace_seconds = poll_interval * 3
    now = time.time()

    if self._post_consensus_stall_first_seen is None:
        self._post_consensus_stall_first_seen = now
        return

    if (now - self._post_consensus_stall_first_seen) < grace_seconds:
        return

    logger.warning(
        "Post-consensus stall detected for pipeline %s: "
        "consensus complete but pipeline still running",
        self.pipeline_id,
    )

    message = (
        "All agents confirmed consensus but the pipeline phase has not "
        "transitioned. Possible orchestrator transition failure. "
        f"Pipeline: {self.pipeline_id}"
    )

    # Broadcast alert so /sdlc monitoring session can surface it
    await self._broadcast_alert("post_consensus_stall", "orchestrator", message, "high")

    # Create HITL decision for human attention
    await self._create_hitl_decision("orchestrator", message)

    # Also send Slack notification for visibility
    await self._send_slack_notification("orchestrator", message)

    self._post_consensus_stall_reported = True

    self._log_oversight_event(
        {
            "event": "post_consensus_stall",
            "consensus": consensus,
            "pipeline_status": pipeline_status_str,
        }
    )


def _blocking_agents_are_active(self, blocking_agents: list[str]) -> bool:
    """Check if any blocking agents have recent progress events."""
    try:
        from progress_store import get_progress_store

        store = get_progress_store()
        window = getattr(self.config, "active_agent_stall_extension_seconds", 120)
        cutoff = datetime.now(UTC) - timedelta(seconds=window)
        for agent in blocking_agents:
            events = store.get_events(self.pipeline_id, agent_role=agent, since=cutoff)
            if events:
                return True
    except Exception:
        logger.debug("Failed to check agent activity", exc_info=True)
    return False


def _get_recent_proposal_age(self) -> float | None:
    """Return seconds since the most recent CONSENSUS_PROPOSE, or None."""
    try:
        from peer_consensus import get_peer_consensus_tracker

        tracker = get_peer_consensus_tracker(self.pipeline_id)
        if tracker is not None:
            latest = tracker.get_latest_proposal_timestamp()
            if isinstance(latest, datetime):
                return (datetime.now(UTC) - latest).total_seconds()
    except Exception:
        logger.debug("Failed to query proposal age", exc_info=True)
    return None


async def _check_incomplete_consensus_stall(
    self, consensus: dict, pipeline_status_str: str
) -> None:
    """Detect and nudge when consensus is incomplete with stuck blocking agents.

    When one or more agents have not confirmed for an extended period while
    other agents have, this method sends a targeted nudge to the blocking
    agents. If the nudge doesn't resolve the stall, escalates to HITL.

    Args:
        consensus: Current consensus status dict from ``_query_consensus_status()``.
        pipeline_status_str: Current pipeline status string (e.g. "running").
    """
    if pipeline_status_str != "running":
        return

    # If consensus is complete or empty, reset and skip
    if not consensus or consensus.get("is_complete"):
        self._reset_incomplete_consensus_tracking()
        return

    blocking_agents = consensus.get("blocking_agents", [])
    if not blocking_agents:
        self._reset_incomplete_consensus_tracking()
        return

    current_blocking = frozenset(blocking_agents)
    now = time.time()

    # If blocking set changed, reset tracking
    if current_blocking != self._incomplete_consensus_blocking:
        self._reset_incomplete_consensus_tracking()
        self._incomplete_consensus_blocking = current_blocking
        self._incomplete_consensus_first_seen = now
        self._incomplete_consensus_absolute_start = now
        return

    if self._incomplete_consensus_first_seen is None:
        self._incomplete_consensus_first_seen = now
        self._incomplete_consensus_absolute_start = now
        return

    # Post-proposal grace: reset if a recent proposal arrived (#1609)
    post_proposal_grace = getattr(self.config, "post_proposal_grace_seconds", 300)
    proposal_age = self._get_recent_proposal_age()
    if proposal_age is not None and proposal_age < post_proposal_grace:
        logger.debug(
            "Recent CONSENSUS_PROPOSE (%.0fs ago) — deferring incomplete consensus check",
            proposal_age,
        )
        self._reset_incomplete_consensus_tracking()
        self._incomplete_consensus_blocking = current_blocking
        self._incomplete_consensus_first_seen = now
        self._incomplete_consensus_absolute_start = now  # restart deferral cap
        return

    elapsed = now - self._incomplete_consensus_first_seen
    poll_interval = getattr(self.config, "overseer_poll_interval_seconds", 30)

    # Nudge threshold: 10 poll cycles (~5 min at 30s interval)
    nudge_threshold = poll_interval * 10
    # HITL threshold: 20 poll cycles (~10 min at 30s interval)
    hitl_threshold = poll_interval * 20

    if elapsed >= hitl_threshold and not self._incomplete_consensus_hitl_created:
        # Activity check: skip HITL escalation if agents are active (#1609)
        # Cap total deferral at 2x HITL threshold to prevent indefinite suppression
        absolute_elapsed = now - (self._incomplete_consensus_absolute_start or now)
        max_deferral = hitl_threshold * 2
        if absolute_elapsed < max_deferral and self._blocking_agents_are_active(
            sorted(blocking_agents)
        ):
            logger.info(
                "Incomplete consensus: blocking agents still active, deferring HITL"
                " (pipeline=%s, blocking=%s, absolute_elapsed=%ds, max_deferral=%ds)",
                self.pipeline_id,
                sorted(blocking_agents),
                round(absolute_elapsed),
                round(max_deferral),
            )
            return

        # Nudge didn't resolve — escalate to HITL
        logger.warning(
            "Incomplete consensus stall persists after nudge — escalating to HITL"
            " (pipeline=%s, blocking=%s, elapsed=%ds)",
            self.pipeline_id,
            sorted(blocking_agents),
            round(elapsed),
        )
        message = (
            f"Consensus incomplete for {round(elapsed)}s. "
            f"Blocking agents: {', '.join(sorted(blocking_agents))}. "
            f"These agents were nudged but have not re-confirmed. "
            f"Pipeline: {self.pipeline_id}"
        )
        await self._create_hitl_decision("orchestrator", message)
        await self._send_slack_notification("orchestrator", message)
        self._incomplete_consensus_hitl_created = True

        self._log_oversight_event(
            {
                "event": "incomplete_consensus_hitl",
                "blocking_agents": sorted(blocking_agents),
                "elapsed_seconds": round(elapsed),
            }
        )

    elif elapsed >= nudge_threshold and not self._incomplete_consensus_nudged:
        # Activity check: defer nudge if agents are active (#1609)
        # Cap nudge deferrals: don't defer past HITL threshold from absolute start
        absolute_elapsed = now - (self._incomplete_consensus_absolute_start or now)
        if absolute_elapsed < hitl_threshold and self._blocking_agents_are_active(
            sorted(blocking_agents)
        ):
            logger.info(
                "Incomplete consensus: blocking agents are active, deferring nudge"
                " (pipeline=%s, blocking=%s, absolute_elapsed=%ds)",
                self.pipeline_id,
                sorted(blocking_agents),
                round(absolute_elapsed),
            )
            # Reset first_seen to extend the window
            self._incomplete_consensus_first_seen = now
            self._log_oversight_event(
                {
                    "event": "incomplete_consensus_activity_extension",
                    "blocking_agents": sorted(blocking_agents),
                }
            )
            return

        # Send targeted nudge to each blocking agent
        logger.info(
            "Incomplete consensus stall detected — nudging blocking agents"
            " (pipeline=%s, blocking=%s, elapsed=%ds)",
            self.pipeline_id,
            sorted(blocking_agents),
            round(elapsed),
        )
        for agent_role in sorted(blocking_agents):
            nudge_message = (
                f"You are blocking consensus for pipeline {self.pipeline_id}. "
                f"Your confirmed status may have been cleared after a re-review "
                f"cycle. Please re-confirm via `egg-orch consensus confirmed`, "
                f"or if you are a reviewer of a re-proposing producer, re-review "
                f"and ACK/NACK the latest proposal then confirm."
            )
            await self._send_message(agent_role, nudge_message)
            self.self_monitor.record_message_sent()

        await self._broadcast_alert(
            "incomplete_consensus_stall",
            ", ".join(sorted(blocking_agents)),
            f"Consensus incomplete for {round(elapsed)}s. "
            f"Blocking agents: {', '.join(sorted(blocking_agents))}. "
            f"Nudge sent.",
            "medium",
        )
        self._incomplete_consensus_nudged = True

        self._log_oversight_event(
            {
                "event": "incomplete_consensus_nudge",
                "blocking_agents": sorted(blocking_agents),
                "elapsed_seconds": round(elapsed),
            }
        )


def _reset_incomplete_consensus_tracking(self) -> None:
    """Reset all incomplete-consensus stall tracking state."""
    self._incomplete_consensus_first_seen = None
    self._incomplete_consensus_blocking = None
    self._incomplete_consensus_nudged = False
    self._incomplete_consensus_hitl_created = False
    self._incomplete_consensus_absolute_start = None
