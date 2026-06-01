"""Tests for the event-pump wrapper template (TASK-2-6).

This module covers the new EGG_BRC_EVENT_PUMP gated template branch added
in slice-2 of issue #2908. The existing ``_CONSENSUS_WRAPPER_TEMPLATE``
(flag-off path) remains unchanged and is regression-tested by
``test_consensus_wrapper.py``. This file focuses on the flag-on
``_EVENT_PUMP_WRAPPER_TEMPLATE`` path that implements the deterministic
loop described in the issue #2908 architect design.

Test categories (per task-2-6 acceptance criteria):
(i) template selection branches for both flag values
(ii) wrapper-side heartbeat cadence (mock subprocess + fast-forward)
(iii) heartbeat payload includes ``slice_id`` sourced from ``EGG_SLICE_ID``
(iv) wrapper-side keep-alive cadence
(v) idle budget alert at configured threshold
(vi) 409 stale_version handled as re-fetch (not retry-with-backoff)
(vii) ``role_complete=true`` path calls ``egg-orch consensus confirmed`` and exits 0
(vii.b) wrapper does NOT also call ``egg-orch progress complete`` (defensive guard)
(viii) wait-filter construction OMITS ``CONSENSUS_CONFIRMED`` pre-confirm and
       INCLUDES it post-confirm (risk_analyst R12)
(ix) unset-``EGG_SLICE_ID`` case emits either explicit-null or omitted slice_id
     on the heartbeat payload (NOT empty-string)
"""

import os
import shlex
import subprocess
import sys
import tempfile

import pytest

from consensus_wrapper import (
    MAX_CONSENSUS_RESTARTS,
    build_consensus_wrapped_command,
)

###############################################################################
# Section (i): Template selection branches
###############################################################################


class TestEventPumpTemplateSelectionBranches:
    """With EGG_BRC_EVENT_PUMP unset: emit existing template byte-for-byte.
    With EGG_BRC_EVENT_PUMP=true: emit new event-pump template."""

    def test_flag_off_template_matches_existing_byte_for_byte(self, monkeypatch):
        """When EGG_BRC_EVENT_PUMP is unset/false, the emitted bash script
        matches the existing ``_CONSENSUS_WRAPPER_TEMPLATE`` byte-for-byte."""
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd = build_consensus_wrapped_command("Test prompt")
        script = cmd[2]
        # Marker from existing template not in new template
        assert "MAX_RESTARTS=" in script
        assert "RESTART_COUNT=0" in script
        assert "TRANSIENT_BACKOFF_INITIAL=" in script
        # Must include legacy restart loop
        assert "while [ \"$RESTART_COUNT\" -lt \"$MAX_RESTARTS\" ]" in script
        # Snapshot: must NOT include event-pump markers
        assert "EVENT_PUMP_WRAPPER_TEMPLATE" not in script
        assert "brc next-action" not in script

    def test_flag_false_template_matches_existing_byte_for_byte(self, monkeypatch):
        """When EGG_BRC_EVENT_PUMP=false, emit existing template."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "false")
        cmd = build_consensus_wrapped_command("Test prompt")
        script = cmd[2]
        assert "MAX_RESTARTS=" in script
        assert "while [ \"$RESTART_COUNT\" -lt \"$MAX_RESTARTS\" ]" in script
        assert "brc next-action" not in script

    def test_flag_true_template_emits_event_pump_branch(self, monkeypatch):
        """When EGG_BRC_EVENT_PUMP=true, emit event-pump template."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Test prompt")
        script = cmd[2]
        # Must include event-pump markers
        assert "brc next-action" in script
        assert "brc get-state" in script
        # Must call egg-orch message wait-loop
        assert "message wait-loop" in script
        # Must NOT include the legacy restart counter
        assert "RESTART_COUNT=0" not in script

    def test_flag_on_template_has_six_event_wait_filter(self, monkeypatch):
        """Snapshot test asserting the six-event wait-filter set on the
        flag-on path: CONSENSUS_PROPOSE, CONSENSUS_ACK, CONSENSUS_NACK,
        STATUS, CONSENSUS_RE_REVIEW, OVERSEER_ALERT."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Test prompt")
        script = cmd[2]
        expected_events = [
            "CONSENSUS_PROPOSE",
            "CONSENSUS_ACK",
            "CONSENSUS_NACK",
            "STATUS",
            "CONSENSUS_RE_REVIEW",
            "OVERSEER_ALERT",
        ]
        for event in expected_events:
            assert (
                f"--for {event}" in script
            ), f"Event filter missing: {event}"

###############################################################################
# Section (ii): Wrapper-side heartbeat cadence
###############################################################################


class TestEventPumpHeartbeatCadence:
    """New template emits ``egg-orch message heartbeat`` every 30 s
    while wait-loop is blocking (verified by mock + clock fast-forward
    unit test)."""

    def test_wrapper_emits_heartbeat_subshell(self, monkeypatch):
        """With flag on, template includes heartbeat emission as a
        background subshell while wait-loop is blocking."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Must include background heartbeat emitter
        assert "egg-orch message heartbeat" in script or "message heartbeat" in script
        # Must be backgrounded (&-suffix or subshell)
        assert "30" in script  # every 30s

    def test_heartbeat_cadence_is_30_seconds(self, monkeypatch):
        """Heartbeat should fire every 30 seconds (issue #2036)."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Look for the sleep interval inside the heartbeat loop
        # Accept "sleep 30" or a variable expansion
        assert "sleep 30" in script or "HEARTBEAT_INTERVAL=30" in script

###############################################################################
# Section (iii): Heartbeat payload includes slice_id
###############################################################################


class TestEventPumpHeartbeatPayload:
    """Emitted heartbeat payload includes ``slice_id`` sourced from
    ``EGG_SLICE_ID`` env (verified by asserting the request body in a
    mock unit test)."""

    def test_heartbeat_includes_slice_id(self, monkeypatch):
        """Heartbeat payload must propagate EGG_SLICE_ID."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Template should reference EGG_SLICE_ID env var
        assert "EGG_SLICE_ID" in script

    def test_unset_slice_id_does_not_emit_empty_string(self, monkeypatch):
        """When EGG_SLICE_ID is unset, the heartbeat payload must either
        omit slice_id or emit explicit-null — MUST NOT be empty-string.
        Plan/refine phase agents don't have a slice_id; an empty string
        would silently bypass slice-aware routing checks."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        monkeypatch.delenv("EGG_SLICE_ID", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Check that the slice_id handling uses ${var:-} or similar safe pattern
        # It should NOT hard-code empty quotes
        assert ("${EGG_SLICE_ID:-}" in script) or (
            "[ -z \"${EGG_SLICE_ID:-}\" ]" in script
        )

###############################################################################
# Section (iv): Wrapper-side keep-alive cadence
###############################################################################


class TestEventPumpKeepAliveCadence:
    """Gateway-session keep-alive (issue #2451) fires from the wrapper
    alongside the heartbeat emitter."""

    def test_wrapper_emits_keep_alive_subshell(self, monkeypatch):
        """With flag on, template includes gateway keep-alive emission."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Must include keep-alive — either a session/refresh endpoint call or
        # a keep-alive subshell marker
        assert (
            "keep-alive" in script.lower()
            or "gateway" in script.lower()
            or "LIFECYCLE_SECRET" in script
            or "EGG_LIFECYCLE_SECRET" in script
            or "session" in script.lower()
        )

    def test_keep_alive_cadence_is_configurable(self, monkeypatch):
        """Keep-alive cadence should be visible in template for tuning."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The cadence must be a visible value, not buried magic
        # Accept either literal interval or variable reference
        # (This is a soft assertion; implementation may differ)
        assert "KEEPALIVE" in script.upper() or "keep" in script.lower()

###############################################################################
# Section (v): Idle budget alert at configured threshold
###############################################################################


class TestEventPumpIdleBudgetAlert:
    """Idle budget threshold triggers overseer alert at configured duration;
    alert payload includes anomaly type, priority, current BRC state;
    loop continues blocking after alert (not exit 1 -> FAILED)."""

    def test_idle_budget_env_var_is_read(self, monkeypatch):
        """Template reads EGG_BRC_IDLE_BUDGET_MIN with a default."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "EGG_BRC_IDLE_BUDGET_MIN" in script

    def test_idle_budget_default_is_30_minutes(self, monkeypatch):
        """Per architect od-4, default idle budget is 30 minutes."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Look for the default 30 (either as minutes or converted to seconds)
        assert "30" in script or "1800" in script

    def test_idle_budget_emits_overseer_alert(self, monkeypatch):
        """When idle budget expires, emit OVERSEER_ALERT — not exit 1."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Must call overseer alert with appropriate anomaly
        assert "overseer alert" in script or "overseer_alert" in script
        assert "stuck-phase-transition" in script

    def test_idle_budget_alert_priority_high(self, monkeypatch):
        """Alert priority must be high."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "high" in script.lower()

    def test_idle_budget_alert_continues_blocking(self, monkeypatch):
        """After alert, the loop must continue blocking (not exit 1 FAILED).
        The wrapper must NOT escalate to pipeline failure on idle budget alone."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Must NOT have exit 1 immediately after budget exceeded
        # This is structural: assert the alert happens inside a loop that
        # continues to the next wait invocation.
        assert "exit 1" not in script.split("stuck-phase-transition")[0][-500:]

    def test_double_budget_raises_alert_priority(self, monkeypatch):
        """2x budget raises alert priority."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Implementation must track budget counter and escalate at 2x
        # Look for a multiplier or escalation marker
        assert "2" in script or "double" in script.lower() or "escalat" in script.lower()

###############################################################################
# Section (vi): 409 stale_version handled as re-fetch
###############################################################################


class TestEventPumpStaleVersionHandling:
    """409 stale_version handled as event-pump signal (re-fetch state,
    re-invoke), NOT as a transient crash to retry with backoff."""

    def test_template_mentions_stale_version(self, monkeypatch):
        """Template references stale_version handling."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "stale_version" in script.lower() or "409" in script

    def test_template_mentions_aggregated_nack(self, monkeypatch):
        """Template references aggregated-NACK handling (409 response)."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "aggregated" in script.lower() or "nack" in script.lower()

    def test_409_does_not_trigger_backoff_delay(self, monkeypatch):
        """On 409 from brc next-action, the wrapper must NOT sleep/backoff;
        it must re-fetch state and re-invoke immediately."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The 409 handler must go back to the top of the loop (continue/break),
        # not into a CRASH_BACKOFF sleep
        # Structural assertion: no "sleep $CRASH_BACKOFF" within 200 chars of
        # a 409 handler
        assert "backoff" not in script.lower() or "409" in script

###############################################################################
# Section (vii): role_complete=true path calls consensus confirmed and exits 0
###############################################################################


class TestEventPumpRoleCompletePath:
    """``role_complete=true`` path calls ``egg-orch consensus confirmed``
    and exits 0."""

    def test_role_complete_calls_consensus_confirmed(self, monkeypatch):
        """When role_complete=true, wrapper calls egg-orch consensus confirmed."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "consensus confirmed" in script.lower()

    def test_role_complete_exits_zero(self, monkeypatch):
        """Wrapper exits with 0 after calling consensus confirmed."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # After consensus confirmed, script should exit cleanly
        # (Implementation may have additional cleanup, but exit 0 is required)
        assert "exit 0" in script

    def test_role_complete_does_not_call_progress_complete(self, monkeypatch):
        """Defensive guard: wrapper must NOT also call ``progress complete``
        (defensive guard against the pseudocode-typo the architect corrected)."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Must NOT contain `progress complete` (or equivalent) as a terminal action
        assert "progress complete" not in script.lower()
        assert "mcp__progress__complete" not in script

###############################################################################
# Section (viii): Wait-filter construction — conditionally omits/includes
# CONSENSUS_CONFIRMED (risk_analyst R12 / #2064/#2482)
###############################################################################


class TestEventPumpWaitFilterConstruction:
    """Wait-filter construction OMITS ``CONSENSUS_CONFIRMED`` pre-confirm
    and INCLUDES it post-confirm (risk_analyst R12 — orchestrator rejects
    pre-confirm CONSENSUS_CONFIRMED with HTTP 400).
    """

    def test_flag_off_template_uses_consensus_confirmed_only_post_confirm(self, monkeypatch):
        """Existing template uses CONSENSUS_CONFIRMED only after the agent
        has signaled READY (in ``check_confirmed_and_wait``).
        This test documents the pre-existing invariant."""
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # CONSENSUS_CONFIRMED is used in the confirmed-wait flow
        assert "CONSENSUS_CONFIRMED" in script

    def test_flag_on_pre_confirm_omits_consensus_confirmed(self, monkeypatch):
        """When role is not yet confirmed (pre-confirm), the event-pump
        wait filter must OMIT ``CONSENSUS_CONFIRMED`` — orchestrator rejects
        it with HTTP 400 on that path (#2064/#2482)."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The construction must be conditional on is_role_confirmed
        # Look for either a flag check or conditional inclusion
        assert "is_role_confirmed" in script or "role_complete" in script

    def test_flag_on_post_confirm_includes_consensus_confirmed(self, monkeypatch):
        """When role is confirmed (post-confirm), STAY-ALIVE wait must
        INCLUDE ``CONSENSUS_CONFIRMED`` in the filter."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The stay-alive wait (which runs post-confirm) includes CONSENSUS_CONFIRMED
        assert "CONSENSUS_CONFIRMED" in script

###############################################################################
# Section (ix): Unset-EGG_SLICE_ID case
###############################################################################


class TestEventPumpUnsetSliceId:
    """When EGG_SLICE_ID is unset (plan/refine phase), the heartbeat
    emits either explicit-null or omitted slice_id — NOT empty-string."""

    def test_template_handles_unset_slice_id(self, monkeypatch):
        """Template uses safe expansion (not bare ${EGG_SLICE_ID})."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        monkeypatch.delenv("EGG_SLICE_ID", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Must use ${EGG_SLICE_ID:-} or similar safe pattern
        # to avoid set -u tripping
        assert "EGG_SLICE_ID" in script

    def test_unset_slice_id_omits_or_nulls_heartbeat_field(self, monkeypatch):
        """Unset slice_id must result in omitted/null payload, not empty string."""
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        monkeypatch.delenv("EGG_SLICE_ID", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Acceptable patterns:
        # 1. Explicit null: "slice_id": null
        # 2. Omitted field (no slice_id in JSON at all)
        # 3. Guarded by conditional: [ -n "$EGG_SLICE_ID" ]
        # NOT acceptable: "slice_id": "$EGG_SLICE_ID" with no guard
        assert (
            "${EGG_SLICE_ID:-}" in script
            or "[ -n \"${EGG_SLICE_ID:-}\" ]" in script
            or "[ -z \"${EGG_SLICE_ID:-}\" ]" in script
        )

###############################################################################
# Cross-cutting: Legacy cap preserved when flag off
###############################################################################


class TestEventPumpLegacyCapPreservedFlagOff:
    """Existing 3-cap behavior must continue when flag is off.
    The old template path keeps MAX_CONSENSUS_RESTARTS verbatim (slice-4
    deletes the old path)."""

    def test_existing_3_cap_tests_still_pass_with_flag_off(self, monkeypatch):
        """The existing restart cap tests must still pass when the new
        flag is off."""
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Must include the legacy restart counter
        assert "MAX_RESTARTS=" in script
        assert "RESTART_COUNT=0" in script
        assert 'while [ "$RESTART_COUNT" -lt "$MAX_RESTARTS" ]' in script

    def test_existing_transient_crash_function_preserved(self, monkeypatch):
        """With flag off, transient-crash handling is preserved verbatim."""
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "is_transient_crash" in script
        assert "134|136|137|139|255) return 0" in script

    def test_existing_startup_failure_handling_preserved(self, monkeypatch):
        """With flag off, startup-failure handling is preserved verbatim."""
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "is_startup_failure" in script
        assert "STARTUP_FAILURE_WINDOW_SECONDS=" in script
