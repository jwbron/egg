"""Tests for the consensus wrapper module.

Slice-4 task-4-2 deleted the legacy capped-restart template; the
``TestBuildConsensusWrappedCommand`` / ``TestConsensusWrapperBehavior``
/ ``TestBufferOverflowDetection`` / ``TestEventDrivenWait`` /
``TestSSESigtermGrace`` classes that pinned its surface went with it,
along with the ``_force_legacy_template`` fixture they shared. The
buffer-overflow / transient-crash / startup-failure shell classifier
helpers were preserved (relocated into ``_EVENT_PUMP_WRAPPER_TEMPLATE``);
their coverage is folded into the event-pump test classes below.
"""

import os
import shlex
import subprocess
import sys

from consensus_wrapper import build_consensus_wrapped_command

# Sentinel event types the event-pump wait filter must always cover.
# Plan TASK-2-1 line 797-799 enumerates these six explicitly.
_EXPECTED_EVENT_PUMP_WAIT_FILTERS = (
    "CONSENSUS_PROPOSE",
    "CONSENSUS_ACK",
    "CONSENSUS_NACK",
    "STATUS",
    "CONSENSUS_RE_REVIEW",
    "OVERSEER_ALERT",
)


class TestEventPumpTemplateSelection:
    """(i) Template selection. Post slice-4 task-4-2 there is only one
    template — the event-pump. The ``EGG_BRC_EVENT_PUMP`` env flag is
    no longer read; any value (including ``false`` / ``0`` / ``no`` /
    ``off``) is silently inert. The class survives task-4-2 so the
    snapshot regression on the event-pump template + the wait-filter
    composition keeps a dedicated home.
    """

    def test_flag_unset_emits_event_pump_template_by_default(self, monkeypatch):
        """Slice-4 task-4-1 flipped the unset-env default to event-pump;
        slice-4 task-4-2 then deleted the legacy template entirely. The
        event-pump is the only production path; unset env must emit it.
        """
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Event-pump template markers.
        assert "Event-pump wrapper (#2908 slice-2)" in script
        # Legacy markers MUST NOT appear — they were deleted by task-4-2.
        assert "MAX_RESTARTS=" not in script
        assert "BRC Consensus Recovery" not in script

    def test_flag_false_is_silently_inert_after_task_4_2(self, monkeypatch):
        """Slice-4 task-4-2 deleted the legacy template and the
        ``EGG_BRC_EVENT_PUMP`` env-flag read along with it. Any value
        (truthy or falsy) is silently ignored; operators with the var
        lingering in k8s manifests can leave it set to ``false`` and
        still get the event-pump template.

        Pins the inertness so a future regression that re-introduces
        a legacy-template branch trips this test.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "false")
        cmd_false = build_consensus_wrapped_command("Prompt")
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd_unset = build_consensus_wrapped_command("Prompt")
        assert cmd_false[2] == cmd_unset[2], (
            "EGG_BRC_EVENT_PUMP=false must be silently inert post "
            "slice-4 task-4-2 — both must emit the event-pump template."
        )
        # Same check for the other falsy tokens.
        for falsy in ("0", "no", "off", "False", "OFF"):
            monkeypatch.setenv("EGG_BRC_EVENT_PUMP", falsy)
            assert build_consensus_wrapped_command("Prompt")[2] == cmd_unset[2], (
                f"EGG_BRC_EVENT_PUMP={falsy!r} must be silently inert."
            )

    def test_flag_unset_and_flag_true_emit_identical_scripts(self, monkeypatch):
        """Sanity: unset and explicit-true cases emit the same script
        post task-4-1. Pinned so a future regression that splits them
        apart trips the test rather than silently bifurcating the
        production path.
        """
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd_unset = build_consensus_wrapped_command("Prompt")
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd_true = build_consensus_wrapped_command("Prompt")
        assert cmd_unset[2] == cmd_true[2], (
            "Unset env and explicit EGG_BRC_EVENT_PUMP=true must emit "
            "the same script after the task-4-1 default flip — "
            "otherwise the production path is bifurcated."
        )

    def test_flag_on_emits_event_pump_template(self, monkeypatch):
        """With ``EGG_BRC_EVENT_PUMP=true``, the new event-pump template
        is emitted in place of the legacy capped-restart template.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The new template MUST contain a wait-loop primitive over the
        # six event types and handle the ``role_complete`` signal from
        # ``brc next-action`` (plan line 783-792).
        assert "egg-orch message wait-loop" in script
        # The role_complete signal arrives from ``brc next-action`` as the
        # action value ``complete``. The wrapper must branch on it (case
        # arm or equivalent). Accept any of: literal ``role_complete``
        # token (variable name), the ``complete)`` case arm in the action
        # switch, or a ``ROLE_CONFIRMED`` boolean derived from
        # ``brc get-state``.
        assert any(
            marker in script for marker in ("role_complete", "complete)", "ROLE_CONFIRMED")
        ), (
            "event-pump must check role_complete from brc get-state / "
            "next-action (plan line 783-792); neither role_complete nor "
            "ROLE_CONFIRMED nor a complete) case arm found in script."
        )
        # New template must call ``brc next-action`` (plan line 785).
        assert "brc next-action" in script

    def test_flag_on_wait_filter_contains_six_required_events(self, monkeypatch):
        """(i) The flag-on path's wait-filter set must include all six event
        types the plan enumerates (line 797-799).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        for event_type in _EXPECTED_EVENT_PUMP_WAIT_FILTERS:
            assert f"--for {event_type}" in script, (
                f"event-pump wait filter missing --for {event_type}; "
                f"plan TASK-2-1 line 797-799 requires all six."
            )


class TestEventPumpHeartbeatCadence:
    """(ii)+(iii) Wrapper-side heartbeat emission migrated out of
    ``sandbox/egg_agent_tools/handlers/message.py:267-429`` and into the
    event-pump bash. The payload must include ``slice_id`` sourced from
    ``EGG_SLICE_ID`` so a regression in slice_id propagation (risk_analyst
    R9) is caught directly.

    Cadence is verified with a mock subprocess fast-forward — we inspect
    the generated script for the configured 30-second loop interval and
    the existence of a backgrounded heartbeat subshell, rather than
    sleeping in real wall-clock to keep the test deterministic.
    """

    def test_flag_on_emits_heartbeat_subshell(self, monkeypatch):
        """The event-pump template must contain ``egg-orch message
        heartbeat`` invoked in a backgrounded subshell while the
        wait-loop is blocking (plan TASK-2-2 description, line 828-829).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "egg-orch message heartbeat" in script
        # Background subshell shape: `( ... ) &` or a backgrounded
        # subshell variant — flexible enough to match either.
        # Heartbeat must run alongside the wait-loop, not block it.
        # We pin the presence of the heartbeat command + the ``wait-loop``
        # primitive in the same script so the migration cannot regress
        # to agent-side-only heartbeating.
        assert "wait-loop" in script

    def test_flag_on_heartbeat_cadence_is_30_seconds(self, monkeypatch):
        """The plan (TASK-2-2 description, line 828) names a 30-second
        cadence. Pin the default in the script so a regression to a
        different cadence is caught by this test. Accept either a
        literal ``sleep 30`` or an env-var indirection that defaults to
        30 (e.g. ``${EGG_BRC_HEARTBEAT_INTERVAL_SECS:-30}``).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Accept either form: literal ``sleep 30`` or an env-var
        # default of 30 (``:-30}"`` etc.).
        cadence_markers = (
            "sleep 30",
            ":-30}",
            "INTERVAL_SECS=30",
            "INTERVAL_SECS:-30",
        )
        assert any(m in script for m in cadence_markers), (
            "wrapper-side heartbeat must default to a 30s cadence per "
            "plan TASK-2-2 line 828; neither literal `sleep 30` nor an "
            "env-var default of 30 found in the rendered bash."
        )

    def test_flag_on_heartbeat_payload_threads_slice_id_from_env(self, monkeypatch):
        """(iii) The heartbeat payload MUST source ``slice_id`` from the
        ``EGG_SLICE_ID`` env var. Plan TASK-2-2 line 831-834 names this
        invariant directly: "The heartbeat payload MUST include
        ``slice_id == os.environ['EGG_SLICE_ID']`` (or the equivalent
        shell substitution ``${EGG_SLICE_ID:-}`` passed through the CLI)
        so a regression in slice_id propagation is caught directly."

        We assert the script references ``EGG_SLICE_ID`` adjacent to the
        ``egg-orch message heartbeat`` invocation. A shell substitution
        of the env var into the heartbeat command line satisfies the
        "passed through the CLI" route from the plan.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The script must reference EGG_SLICE_ID; the message heartbeat
        # CLI takes ``--slice-id`` or threads it through the request.
        assert "EGG_SLICE_ID" in script, (
            "heartbeat payload must source slice_id from EGG_SLICE_ID; "
            "regression in slice_id propagation will not be caught "
            "without this wiring (risk_analyst R9)."
        )

    # Note: slice-2's ``test_flag_off_heartbeat_path_unchanged`` was
    # deleted by slice-4 task-4-2. The legacy template (and its
    # "no wrapper-side heartbeat under flag-off" invariant) no longer
    # exists; the agent-side ``handlers/message.py:_default_emit_wait_loop_heartbeat``
    # was deleted alongside the legacy template, so the double-heartbeat
    # bus-spam scenario that test guarded against is also gone. The
    # wrapper now unconditionally emits the wrapper-owned heartbeat
    # subshell (see ``test_flag_on_emits_heartbeat_subshell`` /
    # ``test_flag_on_emits_heartbeat_subshell_template_marker`` for
    # the post-deletion invariant).


class TestEventPumpKeepAliveCadence:
    """(iv) Wrapper-side gateway-session keep-alive (#2451) migrated out
    of ``sandbox/egg_agent_tools/handlers/message.py`` and into the
    event-pump bash. The wrapper performs the same lifecycle-secret-gated
    session refresh as a background subshell alongside the heartbeat
    emitter from TASK-2-2.
    """

    def test_flag_on_emits_keep_alive_subshell(self, monkeypatch):
        """The event-pump template must perform a gateway-session
        refresh while the wait-loop is blocking (plan TASK-2-4 line
        875-881).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The keep-alive ping refreshes the lifecycle-secret-gated
        # session. Either an explicit ``keep-alive``/``keepalive``
        # subcommand call or a session-refresh marker must appear.
        assert "keep-alive" in script or "keepalive" in script or "session" in script.lower(), (
            "event-pump template must perform gateway-session keep-alive "
            "(plan TASK-2-4); without it, long waits will lose their "
            "lifecycle-secret-gated session and the next CLI call will "
            "401."
        )

    # Note: slice-2's ``test_flag_off_keep_alive_remains_agent_side`` was
    # deleted by slice-4 task-4-2. The legacy template is gone and the
    # agent-side keep-alive (which lived inside ``message_wait_loop``'s
    # heartbeat path) was deleted along with the legacy template, so
    # the "old path unchanged" invariant no longer applies.


class TestEventPumpIdleBudgetAlert:
    """(v) Idle / no-progress safety budget driven by env
    ``EGG_BRC_IDLE_BUDGET_MIN`` (default 30). When no actionable event
    has arrived for the budget duration, the wrapper emits
    ``mcp__progress__overseer_alert`` (anomaly
    ``stuck-phase-transition``, priority ``high``) and continues
    blocking. The legacy template that owned the historical restart
    cap was deleted in slice-4 task-4-2; the idle budget is now the
    only liveness ceiling in the wrapper.

    NOTE: Per scope update on #2908 issue body and contract cq-3, the
    durable server-side ``Pipeline.no_progress_budget`` is the binding
    primary mechanism. The in-wrapper env-var budget tested here is the
    slice-2 implementation gate; it must work AND must NOT replace the
    durable server-side path (which lands in slice-1's orchestrator
    route work, not here).
    """

    def test_flag_on_contains_idle_budget_alert(self, monkeypatch):
        """Idle budget threshold triggers an overseer alert at the
        configured duration (plan TASK-2-3 acceptance line 863-868).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Default budget: 30 minutes (plan line 854).
        assert "EGG_BRC_IDLE_BUDGET_MIN" in script
        # Alert payload (plan line 857-858) — overseer alert with the
        # right anomaly + priority.
        assert "overseer alert" in script or "overseer_alert" in script
        assert "stuck-phase-transition" in script
        # Priority "high" must be passed somewhere (either as
        # ``--priority high`` literal, ``--priority "$priority"`` with
        # ``"high"`` passed in, or an env-var default of "high"). We
        # require both the ``--priority`` flag AND the ``high`` token
        # appear in the script.
        assert "--priority" in script, (
            "overseer alert payload must include --priority flag (plan "
            "TASK-2-3 line 857-858 — `priority high`)."
        )
        assert "high" in script, (
            "overseer alert priority must be `high` per plan TASK-2-3 line 857-858."
        )

    def test_flag_on_idle_budget_default_30_minutes(self, monkeypatch):
        """Default ``EGG_BRC_IDLE_BUDGET_MIN`` is 30 minutes per plan
        line 853-854 (well above the WS7-observed 10-13 min idle ceiling).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Default value must appear as a literal in the rendered bash so
        # an operator override flows through.
        assert "${EGG_BRC_IDLE_BUDGET_MIN:-30}" in script or "EGG_BRC_IDLE_BUDGET_MIN=30" in script

    def test_flag_on_idle_budget_continues_blocking_after_alert(self, monkeypatch):
        """After the alert fires, the loop continues blocking (NOT exit
        1 → FAILED). Plan line 867-868: "loop continues blocking after
        alert (not exit 1 → FAILED)."
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The alert dispatch must NOT be immediately followed by an
        # ``exit 1``. We assert the pattern by checking that the script
        # re-enters the wait-loop after the alert (continue / loop /
        # re-block). The simplest pin: there is no immediate ``exit 1``
        # adjacent to the ``stuck-phase-transition`` keyword.
        alert_idx = script.find("stuck-phase-transition")
        if alert_idx == -1:
            # If the test for `test_flag_on_contains_idle_budget_alert`
            # already failed, this test would also fail — but its scope
            # is the continue-not-exit invariant, so we skip cleanly
            # rather than double-report.
            import pytest as _pytest

            _pytest.skip(
                "stuck-phase-transition keyword absent; covered by the "
                "TestEventPumpIdleBudgetAlert.test_flag_on_contains_idle_budget_alert "
                "failure"
            )
        # Look at the next 200 characters after the alert keyword — no
        # adjacent ``exit 1`` must appear (the loop continues).
        nearby = script[alert_idx : alert_idx + 200]
        assert "exit 1" not in nearby, (
            "idle-budget alert must NOT be followed by exit 1; the loop "
            "MUST continue blocking (plan line 867-868)."
        )

    # Note: slice-2's ``test_flag_off_idle_budget_not_used`` was deleted
    # by slice-4 task-4-2. The legacy template is gone; the unset-env
    # path now emits the event-pump template, which DOES reference
    # ``EGG_BRC_IDLE_BUDGET_MIN``. The "legacy template keeps the
    # 3-restart cap verbatim" invariant no longer applies — see
    # ``test_idle_budget_default_30_min_in_script`` for the post-deletion
    # invariant.


class TestEventPumpStaleVersionRefetch:
    """(vi) 409 ``stale_version`` from ``brc next-action`` is an
    event-pump signal (re-fetch state, re-invoke), NOT a transient crash
    to retry with backoff.

    Plan TASK-2-1 line 793-796: "Wrapper handles 409 ``stale_version``
    and 409 aggregated-NACK from ``brc next-action`` as event-pump
    signals (re-fetch state, re-invoke), NOT as transient crashes to
    retry with backoff."
    """

    def test_flag_on_handles_409_stale_version_as_refetch(self, monkeypatch):
        """The event-pump bash MUST treat HTTP 409 from ``brc
        next-action`` as a re-fetch trigger — call ``brc get-state``
        again and re-invoke, NOT retry the same call with backoff.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The script must explicitly match the 409 status code and
        # call ``brc get-state`` again. Pin the literal "409" so a
        # blanket "any error → retry" path is caught by this test.
        assert "409" in script
        # Re-fetch primitive must be present.
        assert "brc get-state" in script

    def test_flag_on_409_does_not_apply_backoff(self, monkeypatch):
        """On 409, the wrapper must NOT enter the ``CRASH_BACKOFF`` /
        sleep-then-retry path. This is the negative invariant of (vi).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The event-pump template should not be using CRASH_BACKOFF
        # variables at all (those belong to the legacy template).
        # If they DO leak in via copy-paste, the 409 handler might
        # accidentally land in the wrong branch.
        # Allow CRASH_BACKOFF only if it is decisively scoped away
        # from the 409 handler.
        idx = script.find("409")
        if idx != -1:
            nearby = script[max(0, idx - 200) : idx + 400]
            assert "CRASH_BACKOFF" not in nearby, (
                "409 stale_version handler must not be co-located with "
                "CRASH_BACKOFF backoff — it is a re-fetch signal, NOT a "
                "transient crash (plan TASK-2-1 line 793-796)."
            )


class TestEventPumpRoleCompleteConfirm:
    """(vii) + (vii.b) ``role_complete=true`` path calls ``egg-orch
    consensus confirmed`` and exits 0; the wrapper does NOT also call
    ``egg-orch progress complete`` (defensive guard against the
    pseudocode-typo the architect corrected — plan line 932-934).
    """

    def test_flag_on_role_complete_calls_consensus_confirmed(self, monkeypatch):
        """On ``role_complete=true`` the event-pump bash calls
        ``egg-orch consensus confirmed`` to mark consensus and exits 0.
        Plan TASK-2-1 line 791-793: "the wrapper calls ``egg-orch
        consensus confirmed`` (existing CLI at orch_cli.py:2753) — NOT a
        new ``progress complete`` command — to mark the role's consensus
        and exit 0."
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "egg-orch consensus confirmed" in script
        # Clean exit 0 must appear in the role_complete branch.
        # We check by locating the consensus-confirmed call and asserting
        # an ``exit 0`` follows somewhere in the rest of that branch.
        idx = script.find("egg-orch consensus confirmed")
        assert idx >= 0
        tail = script[idx:]
        assert "exit 0" in tail, (
            "the role_complete branch that calls `egg-orch consensus "
            "confirmed` must exit 0 (plan TASK-2-1 line 791-793)."
        )

    def test_flag_on_does_not_call_progress_complete(self, monkeypatch):
        """(vii.b) Defensive guard: the wrapper template must NOT contain
        ``progress complete`` — that would be the pseudocode-typo the
        architect corrected and is NOT a valid CLI subcommand for marking
        BRC consensus.

        Plan TASK-2-6 acceptance line 949-950: "test (vii.b) asserts
        ``rg 'progress complete'`` against the emitted bash returns
        zero matches."
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "progress complete" not in script, (
            "wrapper must NOT call `egg-orch progress complete` — the "
            "correct CLI is `egg-orch consensus confirmed`. This guard "
            "catches the pseudocode-typo the architect corrected."
        )

    # Note: slice-2's ``test_flag_off_legacy_path_does_not_auto_call_consensus_confirmed``
    # was deleted by slice-4 task-4-2. The legacy template is gone; the
    # event-pump template DOES invoke ``egg-orch consensus confirmed``
    # under the ``confirm`` / ``complete`` arms of the action loop (driven
    # by ``brc next-action``, not auto-invoked on agent exit). The
    # symmetry-guard concern (the wrapper auto-confirming on behalf of
    # the agent) is now structurally impossible — the only invocations
    # happen inside the ``case "$ACTION"`` arms which require an
    # orchestrator-side derivation, not a wrapper-side timer or exit
    # condition.


class TestEventPumpWaitFilterConditional:
    """(viii) Wait-filter construction OMITS ``CONSENSUS_CONFIRMED``
    pre-confirm and INCLUDES it post-confirm.

    Plan TASK-2-1 line 811-816: "the wait-filter set is **constructed
    conditionally from ``consensus_status.is_role_confirmed``** —
    pre-confirm waits OMIT ``CONSENSUS_CONFIRMED`` from the filter (per
    risk_analyst R12 / orchestrator HTTP-400 rejection documented in
    #2064/#2482), post-confirm STAY-ALIVE waits INCLUDE it."

    The HTTP-400 rejection is real: the orchestrator's wait endpoint
    returns 400 if a producer's pre-confirm wait names
    ``CONSENSUS_CONFIRMED`` because its own confirm is what generates
    that signal. So the wrapper bash MUST conditionally include the
    filter or risk wedging every pre-confirm wait.
    """

    def test_flag_on_wait_filter_is_constructed_conditionally(self, monkeypatch):
        """The event-pump bash MUST branch on ``is_role_confirmed`` (or
        an equivalent boolean) when constructing the wait-loop filter
        set so the pre-confirm path omits ``CONSENSUS_CONFIRMED``.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The conditional may take any of several shapes. The plan names
        # ``consensus_status.is_role_confirmed`` as the input — at the
        # bash level this is a boolean variable derived from the
        # ``brc get-state`` response. Any of these markers proves the
        # conditional shape exists:
        markers = (
            "is_role_confirmed",
            "ROLE_CONFIRMED",
            "is_confirmed",
            "role_confirmed",
        )
        assert any(m in script for m in markers), (
            "event-pump wait filter must be constructed conditionally "
            "from a role-confirmed boolean derived from brc get-state; "
            "pre-confirm waits must omit CONSENSUS_CONFIRMED per "
            "risk_analyst R12 / orchestrator HTTP-400 rejection "
            "(#2064/#2482)."
        )

    def test_flag_on_pre_confirm_wait_does_not_always_include_consensus_confirmed(
        self, monkeypatch
    ):
        """Negative invariant: the script must NOT unconditionally pass
        ``--for CONSENSUS_CONFIRMED`` to the wait-loop. If every
        wait-loop invocation hard-codes that filter, the conditional
        shape is missing and pre-confirm waits will wedge with HTTP 400.

        The matching tactic: ensure that ``CONSENSUS_CONFIRMED`` does
        NOT appear in the same line as ``wait-loop`` unconditionally.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Find every line containing ``wait-loop``. If any of them
        # ALSO contains ``--for CONSENSUS_CONFIRMED`` directly,
        # without a conditional gate, that's a regression.
        # We detect this by checking that the script branches on a
        # role-confirmed boolean somewhere near the wait-loop call.
        wait_loop_lines = [
            ln for ln in script.splitlines() if "wait-loop" in ln and "egg-orch" in ln
        ]
        # Allow at most one wait-loop call site, but require the script
        # contains either an ``if`` branch around it or a variable that
        # is conditionally extended.
        if any("CONSENSUS_CONFIRMED" in ln for ln in wait_loop_lines):
            # The literal --for CONSENSUS_CONFIRMED appears on the same
            # line as wait-loop. This is only OK if the line is gated
            # by a conditional; check for a same-region ``if``/``case``
            # construct.
            assert "if " in script and (
                "is_role_confirmed" in script
                or "ROLE_CONFIRMED" in script
                or "is_confirmed" in script
            ), (
                "the wait-loop invocation includes --for "
                "CONSENSUS_CONFIRMED unconditionally; this will wedge "
                "pre-confirm waits with HTTP 400 (risk_analyst R12)."
            )

    def test_flag_on_post_confirm_wait_includes_consensus_confirmed(self, monkeypatch):
        """Positive invariant: post-confirm STAY-ALIVE waits MUST
        include ``CONSENSUS_CONFIRMED`` so the wrapper wakes when peer
        producers confirm. Plan line 815-816: "post-confirm STAY-ALIVE
        waits INCLUDE it."
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The literal ``--for CONSENSUS_CONFIRMED`` MUST appear somewhere
        # in the script (gated by the conditional from the previous
        # test).
        assert "--for CONSENSUS_CONFIRMED" in script, (
            "post-confirm STAY-ALIVE wait must include "
            "--for CONSENSUS_CONFIRMED (plan line 815-816)."
        )


class TestEventPumpSliceIdHeartbeatEdge:
    """(ix) Unset-``EGG_SLICE_ID`` case (plan / refine phase) emits
    either explicit-null or omitted slice_id on the heartbeat payload —
    NOT empty-string.

    Plan TASK-2-6 acceptance line 937-939: "unset-``EGG_SLICE_ID`` case
    (plan/refine phase) emits either explicit-null or omitted slice_id
    on the heartbeat payload (NOT empty-string)."

    Empty-string is a known bug class: the orchestrator's slice scoping
    treats "" as a match for "no slice" but also as a distinct value
    from None, so a heartbeat with ``slice_id=""`` will mismatch
    against a tracker reconstruction keyed on None. This test pins the
    null / omission shape.
    """

    def test_unset_slice_id_does_not_emit_empty_string(self, monkeypatch):
        """When ``EGG_SLICE_ID`` is unset, the rendered bash must NOT
        pass an empty-string ``slice_id`` to the heartbeat CLI.

        We assert by scanning the script for a literal ``--slice-id ""``
        or ``"slice_id":""`` pattern, both of which are bug shapes.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        monkeypatch.delenv("EGG_SLICE_ID", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Neither of these bug patterns may appear in the rendered bash.
        assert '--slice-id ""' not in script, (
            'rendered bash must not emit `--slice-id ""` — plan/refine '
            "phases run without a slice and the heartbeat payload must "
            "omit slice_id (or send null), NOT empty-string."
        )
        assert '"slice_id":""' not in script
        assert '"slice_id": ""' not in script

    def test_slice_id_threaded_via_shell_substitution(self, monkeypatch):
        """The plan names two acceptable threading shapes (TASK-2-2
        line 831-834):

        - ``slice_id == os.environ['EGG_SLICE_ID']`` (Python-side read), OR
        - ``${EGG_SLICE_ID:-}`` shell substitution passed through the CLI.

        For the bash template the substitution form is the natural
        shape. Pin the presence of a ``${EGG_SLICE_ID...}`` substitution
        anywhere in the script — the CLI's ``cmd_message_heartbeat``
        already resolves the env var server-side, so even an
        empty-string default would be filtered by the handler. But the
        rendered bash MUST still reference the env var so the slice
        scope makes it onto the wire.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "EGG_SLICE_ID" in script, (
            "rendered bash must reference EGG_SLICE_ID so slice scoping "
            "propagates onto the heartbeat payload (risk_analyst R9)."
        )


class TestEventPumpIdleBudgetCeiling:
    """Post slice-4 task-4-2 the event-pump template is the only
    template path and the ``EGG_BRC_EVENT_PUMP`` env flag is silently
    inert (the legacy capped-restart template and the env-flag read
    were both deleted). This class pins the surviving invariant: the
    idle budget — not a restart cap — is the liveness ceiling for the
    wrapper. Originally named ``TestEventPumpFlagIsolation`` (slice-2)
    when the slice-2/3/4 split between event-pump and legacy paths
    needed to be policed; renamed and trimmed in slice-4 task-4-2 to
    match the post-deletion single-template world.
    """

    def test_event_pump_relies_on_idle_budget_not_legacy_restart_cap(self, monkeypatch):
        """The event-pump path uses ``EGG_BRC_IDLE_BUDGET_MIN`` as the
        liveness ceiling — the legacy restart cap was deleted by
        slice-4 task-4-2 along with the ``max_restarts`` kwarg on
        ``build_consensus_wrapped_command``.

        Renamed and simplified from the original
        ``test_flag_on_does_not_inherit_legacy_max_restarts``: the
        kwarg is gone and the env flag is silently inert, so the test
        no longer needs to drive either.
        """
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "EGG_BRC_IDLE_BUDGET_MIN" in script, (
            "event-pump path must rely on EGG_BRC_IDLE_BUDGET_MIN as the "
            "liveness ceiling (slice-4 task-4-2 deleted the legacy "
            "restart cap)."
        )

    def test_flag_on_does_not_re_invoke_recovery_system_prompt(self, monkeypatch):
        """The new template does not need the legacy recovery system
        prompt — the per-event invocation contract supplies its own
        memory + delta context. Plan slice-3 owns the per-event prompt
        composer.

        Pin that the legacy ``BRC Consensus Recovery`` header does NOT
        appear in the flag-on template; carrying it forward would
        confuse a one-shot per-event invocation with a recovery from
        crash.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The legacy recovery header text is a strong marker.
        assert "BRC Consensus Recovery" not in script, (
            "flag-on event-pump template must not carry the legacy "
            "'BRC Consensus Recovery' header forward — the per-event "
            "invocation contract supplies its own context."
        )


class TestEventPumpHeartbeatSubshellLifecycle:
    """Adversarial probing: the wrapper-owned heartbeat subshell MUST be
    killable by ``stop_background_heartbeat``. If the subshell
    installs an empty ``trap '' TERM`` then a default ``kill`` (SIGTERM)
    from the parent is IGNORED — the subshell continues forever, and
    the subsequent ``wait $HB_BG_PID`` blocks indefinitely because the
    process never exits. This wedges the entire wrapper loop after the
    first ``wait_for_event`` call returns.

    Issue lineage: this is exactly the bug class #2906 / #2451 were
    trying to fix at the agent-side layer. Re-introducing it at the
    wrapper layer would silently regress slice_id propagation AND wedge
    the deterministic loop.

    Note: behavioural verification of the kill semantics belongs in a
    bash-harness integration test (where we can spawn a subshell with
    the same trap and confirm that ``kill && wait`` does not return).
    These tests pin the *static* invariant against the rendered bash:
    if the subshell traps TERM, the corresponding ``stop`` path MUST
    use a signal that the subshell does not trap (``SIGINT``,
    ``SIGHUP``, or ``SIGKILL``); otherwise the lifecycle is broken.
    """

    def test_flag_on_heartbeat_subshell_can_be_stopped(self, monkeypatch):
        """If the rendered bash installs ``trap '' TERM`` (or any
        ignored-TERM equivalent) in the heartbeat subshell, the
        corresponding ``stop`` path MUST send a non-TERM signal so the
        subshell actually exits. Sending the default ``kill`` (= TERM)
        against a TERM-ignoring trap is a silent no-op — the subshell
        loops forever and the wait blocks indefinitely.

        Verified by ad-hoc bash harness during slice-2 review:

            $ bash -c '( trap "" TERM; while true; do sleep 1; echo tick; done ) &
                        sleep 2; kill $!; wait $! 2>/dev/null'
            tick
            tick
            (hang — never returns)

        Therefore the invariant: if the rendered bash sets
        ``trap '' TERM`` in the heartbeat subshell, the stop primitive
        must NOT rely on a default-signal ``kill``. Use ``kill -INT``,
        ``kill -HUP``, ``kill -KILL``, or do not install the empty
        TERM trap in the first place.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]

        # Strip ``#``-prefixed comment content so a comment that *mentions*
        # the buggy pattern (``# The earlier `trap '' TERM` form ...``)
        # doesn't trip the detector. We use a per-line scan rather than
        # full bash tokenisation — sufficient for the static invariant.
        def _strip_comments(text: str) -> str:
            out_lines = []
            for ln in text.splitlines():
                stripped = ln.lstrip()
                if stripped.startswith("#"):
                    continue
                # Trim inline trailing comments (best-effort: ``#`` outside
                # any quotes). False-positive risk is bounded because the
                # pattern we look for has its own quote shape.
                if " #" in ln:
                    ln = ln.split(" #", 1)[0]
                out_lines.append(ln)
            return "\n".join(out_lines)

        executable = _strip_comments(script)
        # Two failure modes the test guards against:
        #
        #  (A) Heartbeat subshell installs `trap '' TERM` AND the
        #      ``stop`` path issues a default-signal `kill $HB_BG_PID`
        #      (with no explicit signal). This is the silent-wedge bug.
        #
        #  (B) Heartbeat subshell installs `trap '' TERM` AND the
        #      ``stop`` path issues `kill -TERM`/`kill -15`. Same wedge,
        #      different shape.
        #
        # If the subshell does NOT install an ignored-TERM trap (either
        # absent or replaced with ``trap 'exit 0' TERM`` / equivalent
        # handler), this test is automatically satisfied — the wrapper is
        # free to use any kill primitive against a non-trapping (or
        # cleanly-exiting) subshell.
        installs_ignoring_term_trap = "trap '' TERM" in executable or 'trap "" TERM' in executable
        if not installs_ignoring_term_trap:
            # The subshell either does not trap TERM at all, or installs a
            # handler that exits cleanly on TERM (e.g. ``trap 'exit 0'
            # TERM``). Either way the default-signal kill / wait pair
            # works as expected.
            return
        # The subshell installs an ignored-TERM trap. The stop path MUST
        # use a non-TERM signal. Allowed primitives: ``kill -INT``,
        # ``kill -HUP``, ``kill -KILL``, ``kill -9``, or
        # ``kill -SIGINT``/-SIGHUP/-SIGKILL. Scan the script for any of
        # these adjacent to the ``HB_BG_PID`` symbol.
        allowed_stop_primitives = (
            "kill -INT",
            "kill -HUP",
            "kill -KILL",
            "kill -9",
            "kill -SIGINT",
            "kill -SIGHUP",
            "kill -SIGKILL",
        )
        kill_lines = [ln for ln in executable.splitlines() if "kill " in ln and "HB_BG_PID" in ln]
        # Detect a default-signal kill (no explicit -SIG flag).
        default_kill_lines = [
            ln
            for ln in kill_lines
            if not any(prim in ln for prim in allowed_stop_primitives) and "kill -" not in ln
        ]
        assert not default_kill_lines, (
            "the heartbeat subshell installs `trap '' TERM` which "
            "ignores the default SIGTERM signal. The corresponding "
            "stop path uses a default-signal `kill` which is a silent "
            "no-op against that trap — the subshell will never exit, "
            "and the subsequent `wait` blocks indefinitely, wedging "
            "the event-pump loop after the first wait_for_event call.\n"
            "Fix options: (a) remove the `trap '' TERM` from the "
            "subshell, (b) replace it with `trap 'exit 0' TERM` (or any "
            "handler that exits the subshell), or (c) change the stop "
            "path to `kill -INT`, `kill -HUP`, or `kill -KILL` so the "
            "signal is not trapped.\n"
            f"Offending kill line(s): {default_kill_lines}"
        )

    def test_flag_on_heartbeat_subshell_lifecycle_is_bounded(self, monkeypatch):
        """Companion to the trap test: regardless of the trap shape,
        the wrapper MUST have an ``EXIT``-time cleanup that stops the
        background heartbeat so a clean exit doesn't leave a stray
        subshell holding the gateway session open. (The orchestrator's
        ``ScriptedProvider`` ban for E2E means we cannot verify this
        end-to-end; this is the static guard.)
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The cleanup must be wired into bash's EXIT trap so even an
        # unexpected exit path tears the subshell down. Either
        # ``trap cleanup EXIT`` or ``trap '<stop call>' EXIT`` is
        # acceptable.
        assert "trap " in script and "EXIT" in script, (
            "wrapper must install an EXIT trap to clean up the "
            "background heartbeat subshell on any exit path."
        )


class TestEventPumpIdleAlertBrcSnapshot:
    """Adversarial regression for the v2 idle-alert BRC snapshot bug.

    Plan TASK-2-3 acceptance line 866-867: "alert payload includes
    anomaly type, priority, current BRC state". The v2 coder addressed
    this by embedding a ``brc_snapshot`` line in the alert detail
    sourced from ``${{STATE_JSON:-{}}}``. The bash parameter expansion
    is broken: bash's ``${{VAR:-DEFAULT}}`` syntax ends at the FIRST
    ``}}`` after ``${{``, so ``${{STATE_JSON:-{}}}`` is parsed as
    ``${{STATE_JSON:-{}}`` (default ``{``) followed by a literal
    trailing ``}``. When STATE_JSON IS unset the rendered text happens
    to read as ``{}}}`` collapsed to a valid empty-object literal by
    accident, but when STATE_JSON is populated (the common case during
    the event-pump loop) the rendered text appends a STRAY ``}`` to
    the JSON document, and ``json.load`` fails with
    ``json.decoder.JSONDecodeError: Extra data`` — the snapshot falls
    back to ``(unavailable)`` 100% of the time the alert actually has
    state to show.

    Verified end-to-end with the rendered bash (slice-2 v2):

        $ STATE_JSON='{"consensus":{"agents":{...}}}'
        $ echo "${STATE_JSON:-{}}" \
            | python3 -c 'import sys, json; json.load(sys.stdin)'
        json.decoder.JSONDecodeError: Extra data: line 1 column 110 (char 109)

    This is an observability bug, not a correctness wedge: the alert
    still fires, but the BRC-state field always reads "(unavailable)"
    when state IS available. That defeats the entire point of the
    snapshot (tester v1 non-blocker #2).

    Fix: use a temp variable for the default so the bash parser sees
    a balanced ``${{...}}``:

        local state_default='{}'
        echo "${{STATE_JSON:-$state_default}}" | python3 ...
    """

    def test_flag_on_state_json_default_does_not_corrupt_populated_json(self, monkeypatch):
        """The idle-alert BRC snapshot extraction MUST work when
        STATE_JSON is populated. Statically check that the rendered
        bash does NOT use the broken ``${{STATE_JSON:-{}}}`` pattern.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The broken pattern. We pin against the exact literal because
        # the bash parser fails the same way regardless of variable
        # name; if any future code introduces a ``${VAR:-{}}`` it has
        # the same bug.
        assert "${STATE_JSON:-{}}" not in script, (
            "rendered bash contains `${STATE_JSON:-{}}` which bash "
            "parses as `${STATE_JSON:-{}` (default `{`) plus a "
            "trailing `}` — populated STATE_JSON values get a stray "
            "`}` appended, breaking the downstream `json.load`. The "
            "idle-alert BRC-snapshot field will always read "
            "`(unavailable)` in the common case. Fix: use a temp var "
            "for the default, e.g.\n"
            "    local state_default='{}'\n"
            '    echo "${STATE_JSON:-$state_default}" | python3 ...'
        )

    def test_flag_on_state_json_snapshot_round_trips_populated_json(self, monkeypatch, tmp_path):
        """Behavioral round-trip: render the bash, extract the
        ``brc_snapshot=$(echo ... | python3 ...)`` block, drive it
        with a populated STATE_JSON, and assert the output does NOT
        say ``(unavailable)``.
        """
        import os
        import re
        import subprocess

        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Locate the brc_snapshot extraction. The shape evolved across
        # cycles: v2 used ``brc_snapshot=$(echo "${STATE_JSON:-{}}" |
        # python3 ...)`` (the broken form this test originally pinned);
        # v3 uses a separate ``snapshot_input`` variable + explicit
        # empty-string check, then ``printf '%s' "$snapshot_input" |
        # python3 ...``. We extract from the start of the
        # ``raise_idle_alert`` function definition through the
        # ``(snapshot unavailable)`` literal so either shape round-
        # trips through the harness.
        match = re.search(
            r"raise_idle_alert\(\) \{(.*?\(snapshot unavailable\)\"\))",
            script,
            flags=re.DOTALL,
        )
        if match is None:
            import pytest as _pytest

            _pytest.skip(
                "raise_idle_alert / brc_snapshot extraction block not "
                "present in rendered bash; behavioral test does not "
                "apply."
            )
        # The captured group is the function body up through the
        # snapshot extraction; trim the leading ``local`` declarations
        # so the harness can supply its own STATE_JSON without
        # collision.
        snapshot_block = match.group(1)
        # The captured block lives inside a function body; replace
        # ``local`` declarations with plain assignments so the harness
        # can run it at the top level of the wrapper script.
        snapshot_block = re.sub(
            r"^\s*local (\w+)(?: (\w+))?",
            lambda m: " ".join(g for g in (m.group(1), m.group(2)) if g),
            snapshot_block,
            flags=re.MULTILINE,
        )
        # Build a minimal harness: define STATE_JSON, run the block,
        # echo the result.
        harness = (
            'STATE_JSON=\'{"consensus":{"agents":'
            '{"tester":{"confirmed":true,"producer_phase":"WORKING"}},'
            '"blocking_agents":["coder"]}}\'\n'
            + snapshot_block
            + '\necho "RESULT=[$brc_snapshot]"\n'
        )
        env = os.environ.copy()
        env["EGG_AGENT_ROLE"] = "tester"
        result = subprocess.run(
            ["bash", "-c", harness],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        # The snapshot MUST contain the role info from the populated
        # STATE_JSON, NOT the "(unavailable)" fallback.
        assert "RESULT=[" in result.stdout, (
            f"harness did not produce a RESULT line; stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        result_line = next(ln for ln in result.stdout.splitlines() if ln.startswith("RESULT="))
        assert "(unavailable)" not in result_line, (
            "idle-alert BRC snapshot reads `(unavailable)` even when "
            "STATE_JSON IS populated — the `${STATE_JSON:-{}}` bash "
            "parameter expansion corrupts the JSON with a stray `}` "
            "before it reaches `json.load`. The snapshot enhancement "
            "ships broken; operators will never see structured state "
            "in the alert detail. See test docstring for the fix.\n"
            f"Got: {result_line}"
        )
        # And the result should contain the role we set.
        assert "tester" in result_line, (
            f"snapshot does not contain the EGG_AGENT_ROLE; got {result_line!r}"
        )


class TestEventPumpConfirmFailureRaisesIdleAlert:
    """(reviewer §1 lock-in) When ``egg-orch consensus confirmed``
    persistently fails on the ``confirm`` arm, the wrapper must NOT
    tight-retry. The idle-budget overseer alert is the replacement
    for the legacy ``MAX_CONSENSUS_RESTARTS=3`` ceiling -- it MUST
    fire when the underlying CLI keeps returning non-zero.

    Pre-fix bug shape (slice-2 v1): the ``confirm)`` arm called
    ``note_progress`` unconditionally after the CLI returned, which
    reset ``LAST_PROGRESS`` and both ``ALERTED_AT_*`` latches every
    iteration. ``check_idle_budget`` therefore never observed a
    growing idle and the alert never fired -- a tight retry loop
    with zero operator-visible signal.

    Post-fix: ``note_progress`` only fires on rc==0. A persistent
    non-zero rc lets the idle counter accrue; ``check_idle_budget``
    fires the OVERSEER_ALERT at the configured budget.
    """

    def test_persistent_confirm_failure_fires_overseer_alert(self, tmp_path, monkeypatch):
        """End-to-end behavioural test of the §1 + §6.2 lock-in.

        Drive the rendered event-pump bash against stubbed
        ``egg-orch`` / ``python3`` shims:
          - ``brc get-state`` → role unconfirmed
          - ``brc next-action`` → ``{"action":"confirm"}``
          - ``consensus confirmed`` → exit 1 every call (persistent
            failure)
          - ``overseer alert`` → record the call to a log file
          - everything else → noop / exit 0

        With ``EGG_BRC_IDLE_BUDGET_MIN=0`` the very first
        ``check_idle_budget`` after the failing confirm trips the
        ``idle >= 2*budget`` (= 0) branch -- exactly once, post-fix.

        Discrimination: per-iteration count of the alert is the
        differential between the bug and the fix.

          - Pre-fix (note_progress reset on every iteration AND
            ALERTED_AT_DOUBLE reset every iteration -- the combined
            §1 + §6.2 bug): every loop iter resets the latch so
            ``check_idle_budget`` re-fires the 2x alert *every*
            iteration. Within the 8s timeout below, observed count
            is several.
          - Post-fix (rc-gated note_progress AND sticky
            ALERTED_AT_DOUBLE): alert fires once on the first
            iteration; subsequent iterations see the sticky latch
            and do not re-fire.

        The ``count == 1`` assertion locks in the worst-case
        regression where BOTH §1 and §6.2 regress together: the
        action arm calls ``note_progress`` on every iteration AND
        ``note_progress`` resets ``ALERTED_AT_DOUBLE``, so the
        2x-budget alert re-fires every loop iteration, yielding
        ``count >> 1``. The fix yields exactly 1.

        Note: with ``EGG_BRC_IDLE_BUDGET_MIN=0`` either regression
        in isolation still yields ``count == 1`` (a §1-only
        regression rearms ``LAST_PROGRESS`` but ``ALERTED_AT_DOUBLE``
        stays sticky after iter-1; a §6.2-only regression never
        reaches the reset path because rc-gated ``note_progress``
        is never called on persistent failure). The combined
        regression is the alert-flood scenario worth catching here.
        """
        # ``EGG_BRC_EVENT_PUMP`` is silently inert after slice-4 task-4-2
        # (the env-flag read was deleted along with the legacy template);
        # this ``setenv`` is harmlessly retained so a future regression
        # that re-introduces a flag-gated branch trips the test if it
        # depends on the env. ``build_consensus_wrapped_command`` always
        # emits the event-pump template body now.
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        # Stub directory on PATH ahead of the real egg-orch.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        confirm_log = tmp_path / "confirm_calls.log"
        alert_log = tmp_path / "alert_calls.log"
        general_log = tmp_path / "egg_orch.log"

        # Mock egg-orch: route on the first two positional words so we
        # can recognise ``brc get-state`` / ``brc next-action`` /
        # ``consensus confirmed`` / ``overseer alert`` etc.
        mock_orch = bin_dir / "egg-orch"
        mock_orch.write_text(
            "#!/bin/bash\n"
            f'echo "$@" >> {shlex.quote(str(general_log))}\n'
            'sub="$1 $2"\n'
            'case "$sub" in\n'
            '    "brc get-state")\n'
            '        echo \'{"consensus":{"agents":{"coder":{"confirmed":false,'
            '"producer_phase":"WAITING_FOR_REVIEW"}},"is_complete":false}}\'\n'
            "        ;;\n"
            '    "brc next-action")\n'
            '        echo \'{"action":"confirm"}\'\n'
            "        ;;\n"
            '    "consensus confirmed")\n'
            f"        echo confirm_call >> {shlex.quote(str(confirm_log))}\n"
            "        exit 1\n"
            "        ;;\n"
            '    "overseer alert")\n'
            f'        echo "alert: $*" >> {shlex.quote(str(alert_log))}\n'
            "        ;;\n"
            "    *)\n"
            "        # message heartbeat, message wait-loop, etc -- benign no-ops.\n"
            "        ;;\n"
            "esac\n"
            "exit 0\n"
        )
        os.chmod(str(mock_orch), 0o755)  # nosec B103

        # Stub python3 so the agent invocation arm (not exercised here
        # because next-action == ``confirm``) doesn't accidentally run
        # the real Agent SDK if a future regression flips the action.
        # Forward inline ``python3 -c`` invocations (which the wrapper
        # uses for JSON parsing) to the real interpreter.
        real_python = sys.executable
        mock_python = bin_dir / "python3"
        mock_python.write_text(
            "#!/bin/bash\n"
            'if [ "$1" = "-c" ] || [ "$1" = "-" ]; then\n'
            f'    exec {shlex.quote(real_python)} "$@"\n'
            "fi\n"
            "# Agent SDK invocation path -- treat as success no-op.\n"
            "exit 0\n"
        )
        os.chmod(str(mock_python), 0o755)  # nosec B103

        # Build the wrapper with the flag on; idle budget of 0 makes
        # ``check_idle_budget`` fire on the first non-progress
        # iteration.
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["EGG_BRC_EVENT_PUMP"] = "true"
        env["EGG_BRC_IDLE_BUDGET_MIN"] = "0"
        env["EGG_AGENT_ROLE"] = "coder"
        env["EGG_PIPELINE_ID"] = "test-pipeline"
        env["EGG_CONCURRENT_MODE"] = "true"

        cmd = build_consensus_wrapped_command("Prompt")
        # The wrapper loops forever; bound the test with a short
        # timeout. By that time, several confirm attempts and at
        # least one overseer alert must have been recorded if the
        # §1 fix is in place.
        try:
            subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except subprocess.TimeoutExpired:
            pass  # expected — the wrapper loops; we bound it.

        assert confirm_log.exists(), (
            "wrapper did not call `egg-orch consensus confirmed` at "
            "all on the confirm arm -- the event-pump loop may not "
            "have reached the confirm action. egg-orch log:\n"
            + (general_log.read_text() if general_log.exists() else "(empty)")
        )
        confirm_attempts = confirm_log.read_text().count("confirm_call")
        assert confirm_attempts >= 1, f"expected >= 1 confirm attempts, got {confirm_attempts}"

        assert alert_log.exists(), (
            "§1 regression: `egg-orch consensus confirmed` failed "
            f"{confirm_attempts} times but the overseer idle-budget "
            "alert never fired. The pre-fix `note_progress` reset "
            "ran unconditionally on every confirm-arm iteration, "
            "resetting LAST_PROGRESS and the ALERTED_AT_* latches so "
            "`check_idle_budget` never observed a growing idle. "
            "Post-fix: `note_progress` only fires on rc==0; "
            "persistent confirm failure must surface as an "
            "OVERSEER_ALERT. egg-orch log:\n" + general_log.read_text()
        )
        alert_text = alert_log.read_text()
        assert "stuck-phase-transition" in alert_text, (
            f"overseer alert fired but with the wrong anomaly type. Got:\n{alert_text}"
        )
        # Lock-in for the combined §1 + §6.2 regression (reviewer
        # follow-up on PR #2926, commit 022fad4): the bug fires the
        # 2x-budget alert *every* loop iteration because (a) the
        # action arm called ``note_progress`` regardless of rc and
        # (b) ``note_progress`` itself reset ``ALERTED_AT_DOUBLE``.
        # The fix gates ``note_progress`` on rc==0 AND keeps the 2x
        # latch sticky for the loop lifetime -> exactly one alert.
        alert_count = alert_text.count("stuck-phase-transition")
        assert alert_count == 1, (
            "§1 + §6.2 regression: persistent confirm failure produced "
            f"{alert_count} overseer alerts, expected exactly 1. "
            "Pre-fix, the action arm reset both LAST_PROGRESS and the "
            "ALERTED_AT_DOUBLE latch on every iteration, so "
            "`check_idle_budget` re-fired the 2x-budget alert each "
            "loop. Post-fix, `note_progress` is gated on rc==0 (so "
            "persistent failure does not reset state) AND "
            "`ALERTED_AT_DOUBLE` is sticky (so transient progress "
            "later in the loop's life cannot re-arm the page). "
            f"Alert text:\n{alert_text}"
        )


class TestEventPumpInvokesComposer:
    """Pin the wrapper template's ``invoke_agent_for_event`` invocation
    shape (reviewer_contract NACK v1, plan TASK-3-2 acceptance "Wrapper
    template emits expected ``compose_event_prompt`` invocation").

    The wrapper composes the per-event prompt by invoking
    ``orchestrator/routes/event_prompt.py`` via the script CLI, with
    env-var contract: ``EGG_AGENT_ROLE``, ``EGG_BASE_BRANCH``,
    ``EGG_REPO_PATH``, ``EGG_BRC_MEMORY`` (all four explicitly
    re-exported on the ``python3`` invocation per the
    reviewer_holistic v2 follow-up so the script sees a deterministic
    env regardless of which vars the parent shell happens to export).
    These tests fail if a future refactor drops the function, changes
    the script path, breaks the env-var pass-through, or removes the
    ``python3 "$script_path"`` call shape.
    """

    def test_flag_on_template_defines_invoke_agent_for_event(self, monkeypatch) -> None:
        from consensus_wrapper import build_consensus_wrapped_command

        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("hello")
        script = cmd[2]
        assert "invoke_agent_for_event()" in script, (
            "Wrapper template must define `invoke_agent_for_event` so the "
            "per-event prompt composition is in place for slice-3 / "
            "slice-4 wiring."
        )

    def test_flag_on_template_references_event_prompt_script(self, monkeypatch) -> None:
        from consensus_wrapper import build_consensus_wrapped_command

        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("hello")
        script = cmd[2]
        # Either the hard-coded production path OR the override env var
        # must be present so tests can swap the script for fakes.
        assert (
            "/opt/egg-runtime/orchestrator/routes/event_prompt.py" in script
            or "EGG_EVENT_PROMPT_SCRIPT" in script
        ), (
            "Wrapper template must reference the event_prompt CLI script "
            "by path or by env-var indirection."
        )
        # The actual script-path env var name is the documented seam.
        assert "EGG_EVENT_PROMPT_SCRIPT" in script

    def test_flag_on_template_re_exports_memory_env_var(self, monkeypatch) -> None:
        from consensus_wrapper import build_consensus_wrapped_command

        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("hello")
        script = cmd[2]
        # The re-export line must be present so the env-var contract
        # to ``event_prompt.py::_cli`` is locked in. All four documented
        # env vars (``EGG_AGENT_ROLE`` / ``EGG_BASE_BRANCH`` /
        # ``EGG_REPO_PATH`` / ``EGG_BRC_MEMORY``) must be re-exported
        # on the python3 invocation per the reviewer_holistic v2
        # follow-up — the earlier shape relied on ``EGG_REPO_PATH``
        # being exported by the parent shell, which works in
        # production today but breaks symmetry with the in-source
        # comment that lists all four as wrapper-supplied.
        assert "EGG_BRC_MEMORY=" in script
        assert "EGG_AGENT_ROLE=" in script
        assert "EGG_BASE_BRANCH=" in script
        assert "EGG_REPO_PATH=" in script

    def test_flag_on_template_env_prefix_attaches_to_python3_not_printf(self, monkeypatch) -> None:
        """The env-var prefix must attach to ``python3`` (RHS of the
        pipe), not ``printf`` (LHS). The earlier form attached only to
        ``printf`` and ``python3`` inherited from the parent shell — a
        latent bug that worked in production today only because the
        agent-pod shell already exports the vars, and would silently
        break if the parent shell didn't (reviewer_contract NACK v1
        finding #1 + reviewer_code NACK v1 finding #1).
        """
        from consensus_wrapper import build_consensus_wrapped_command

        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("hello")
        script = cmd[2]
        # The pipe must run printf first (LHS) without env-var prefix,
        # then env-vars decorate the python3 invocation (RHS).
        # Pin by checking the textual order of the key tokens.
        printf_idx = script.find("printf '%s' \"$event_payload\"")
        env_role_idx = script.find('EGG_AGENT_ROLE="$role"')
        python3_idx = script.find('python3 "$script_path"')
        assert printf_idx > 0
        assert env_role_idx > 0
        assert python3_idx > 0
        assert printf_idx < env_role_idx < python3_idx, (
            f"Expected order: printf '%s' "
            f"(idx={printf_idx}) | EGG_AGENT_ROLE=...="
            f"(idx={env_role_idx}) python3 (idx={python3_idx}); "
            "this confirms the env-var prefix attaches to python3 "
            "(RHS of the pipe) per the v1 NACK fix."
        )

    def test_flag_on_template_invokes_python3_with_script_path_and_action(
        self, monkeypatch
    ) -> None:
        from consensus_wrapper import build_consensus_wrapped_command

        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("hello")
        script = cmd[2]
        # The call shape: ``python3 "$script_path" "$action"``.
        assert 'python3 "$script_path" "$action"' in script, (
            "Wrapper template must invoke python3 with the script path "
            "and action argument. A future refactor that drops the "
            "action argv would silently break the CLI contract."
        )

    # Note: slice-2's ``test_flag_off_legacy_template_does_not_reference_event_prompt``
    # was deleted by slice-4 task-4-2. The legacy template is gone; the
    # event-pump template now unconditionally references ``event_prompt.py``
    # and ``invoke_agent_for_event``. See
    # ``test_invokes_event_prompt_composer_script`` (above) for the
    # post-deletion positive invariant.


class TestEffortFlag:
    """``effort`` threads into the agent command prefix as ``--effort``.

    The decision's effort (AgentModelDecision.effort — currently pinned
    only for fable-routed agents) must reach the ``python3 -m egg_agent``
    invocation; omitting it must leave the flag off entirely so every
    other model keeps inheriting Claude Code's per-model default.
    """

    def test_effort_appends_flag_to_agent_prefix(self):
        cmd = build_consensus_wrapped_command("Prompt", model="fable", effort="high")
        script = cmd[2]
        assert "--model fable --max-turns 1000 --effort high" in script

    def test_no_effort_omits_flag(self):
        cmd = build_consensus_wrapped_command("Prompt", model="opus")
        script = cmd[2]
        assert "--effort" not in script


class TestSyncToProposals:
    """Wrapper-performed sync-to-proposal on review actions (#3076 /
    #3077 clause 2).

    The designed mid-phase artifact flow used to live as fetch/merge
    prose in spawn prompts the event pump provably discards
    (``del prompt_text``, #3033) — so reviewers that must RUN a
    proposal (tester) never had the producer's commits in their
    worktree. The wrapper now performs that sync deterministically:
    before an ``ack``/``nack`` invocation it merges each pending
    producer's ``proposal_commit_sha`` into the reviewer worktree,
    fail-soft at every step.
    """

    def _script(self, monkeypatch) -> str:
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        return build_consensus_wrapped_command("Prompt")[2]

    def test_template_defines_sync_to_proposals(self, monkeypatch):
        script = self._script(monkeypatch)
        assert "sync_to_proposals() {" in script

    def test_sync_runs_only_for_review_actions(self, monkeypatch):
        """The sync call must be gated on ack/nack — a producer's own
        ``propose`` invocation must NOT merge peers' commits into its
        worktree (R11a: propose own work first, peer state irrelevant).
        """
        script = self._script(monkeypatch)
        guard = 'if [ "$ACTION" = "ack" ] || [ "$ACTION" = "nack" ]; then'
        assert guard in script
        # The call rides inside that guard, with the event payload.
        guarded_block = script.split(guard, 1)[1].split("fi", 1)[0]
        assert 'sync_to_proposals "$EVENT_PAYLOAD"' in guarded_block

    def test_sync_precedes_agent_invocation(self, monkeypatch):
        """Ordering invariant: the worktree must be synced BEFORE the
        one-shot agent runs, or the tester still reviews a stale tree.
        """
        script = self._script(monkeypatch)
        sync_pos = script.index('sync_to_proposals "$EVENT_PAYLOAD"')
        invoke_pos = script.index('invoke_agent_for_event "$ACTION" "$EVENT_PAYLOAD"')
        assert sync_pos < invoke_pos

    def test_sha_extraction_is_hex_validated(self, monkeypatch):
        """The producer-supplied SHA is interpolated into git argv;
        the extractor must hex-validate (7-64 chars) so shell
        metacharacters and non-hex sentinels (RECONSTRUCTED_NO_SHA)
        never reach git.
        """
        script = self._script(monkeypatch)
        assert "[0-9a-fA-F]{7,64}" in script
        assert "fullmatch" in script

    def test_merge_failure_is_fail_soft(self, monkeypatch):
        """A conflicting merge must abort and continue — the per-event
        prompt's ``git show`` reads (#3078) remain the fallback; the
        agent invocation must never be blocked on the sync.
        """
        script = self._script(monkeypatch)
        assert "merge --abort" in script
        # The function never propagates failure into the action arm.
        fn_body = script.split("sync_to_proposals() {", 1)[1]
        # Take through the function's closing `return 0`.
        assert "return 0" in fn_body.split("\n}\n", 1)[0]

    def _extract_sync_harness(self, script: str, repo: str, payload: str) -> str:
        """Build a runnable bash harness: cw_log + sync_to_proposals."""
        import re

        cw_match = re.search(r"cw_log\(\) \{.*?\n\}", script, flags=re.DOTALL)
        assert cw_match is not None
        sync_match = re.search(r"sync_to_proposals\(\) \{.*?\n\}", script, flags=re.DOTALL)
        assert sync_match is not None
        return (
            "#!/bin/bash\nset -uo pipefail\n"
            f"EGG_REPO_PATH={shlex.quote(repo)}\n"
            + cw_match.group(0)
            + "\n"
            + sync_match.group(0)
            + "\nsync_to_proposals "
            + shlex.quote(payload)
            + '\necho "SYNC_RC=$?"\n'
        )

    def test_behavioral_merge_and_metachar_filter(self, tmp_path, monkeypatch):
        """End-to-end: a real proposal SHA on a producer branch is
        merged into the reviewer's checkout (the proposed artifact
        becomes Read-able); a shell-metachar SHA is filtered before
        any git command; the function exits 0 regardless.
        """
        import json as _json

        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args):
            subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t",
                },
            )

        git("init", "-q", "-b", "main")
        (repo / "f.txt").write_text("base\n")
        git("add", ".")
        git("commit", "-qm", "base")
        git("checkout", "-qb", "producer")
        (repo / "plan.md").write_text("the plan\n")
        git("add", ".")
        git("commit", "-qm", "plan draft")
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git("checkout", "-qb", "reviewer", "main")

        payload = _json.dumps(
            {
                "pending_reviews": [
                    {"producer": "architect", "proposal_commit_sha": sha},
                    {"producer": "evil", "proposal_commit_sha": "abc; rm -rf /"},
                    {"producer": "noop", "proposal_commit_sha": ""},
                ]
            }
        )
        script = self._script(monkeypatch)
        harness = self._extract_sync_harness(script, str(repo), payload)
        result = subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        assert "SYNC_RC=0" in result.stdout, (
            f"sync_to_proposals must exit 0; stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        # The proposed artifact is now a real file in the reviewer tree.
        assert (repo / "plan.md").read_text() == "the plan\n"
        # The metachar SHA was filtered, not executed/attempted.
        assert "rm -rf" not in result.stderr

    def test_behavioral_unresolvable_sha_logs_and_continues(self, tmp_path, monkeypatch):
        """A well-formed but unknown SHA logs the git-show fallback and
        exits 0 — never fails the action arm."""
        import json as _json

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        payload = _json.dumps(
            {"pending_reviews": [{"producer": "x", "proposal_commit_sha": "a" * 40}]}
        )
        script = self._script(monkeypatch)
        harness = self._extract_sync_harness(script, str(repo), payload)
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=30)
        assert "SYNC_RC=0" in result.stdout
        assert "unresolvable" in result.stderr


class TestSyncOutcomesAndBanner:
    """R1 non-silent sync banner (#3077 slice-1 TASK-1-3).

    The fail-soft skip points in ``sync_to_proposals()`` — unresolvable
    SHA, conflicting merge — used to log and continue, leaving a
    reviewer whose worktree silently failed to sync to trust a stale
    local diff. Slice-1 closes that silence: ``sync_to_proposals()``
    records a per-SHA outcome (one of ``merged``, ``already-ancestor``,
    ``unresolvable``, ``merge-failed``) and the wrapper prepends a
    "worktree NOT synced to <sha> (<reason>); treat your local diff as
    unreliable — use the ``git show`` commands below." banner to the
    fetched event prompt BEFORE the agent is invoked, on any failure
    outcome. Successful sync paths leave the agent-visible prompt
    byte-identical.

    Sync semantics (fail-soft, exit 0, merge --abort on conflict) are
    unchanged — only reporting is new. These tests pin the new R1
    contract:

    * outcome values are observable at the wrapper level for all four
      branches (``merged``, ``already-ancestor``, ``unresolvable``,
      ``merge-failed``);
    * the agent-visible prompt contains the banner on a failure outcome
      and contains the SHA + reason verbatim;
    * the agent-visible prompt is byte-identical to the composed prompt
      on a successful outcome (no banner).
    """

    # The four outcome tokens the wrapper records at the per-SHA level.
    # Plan slice-1 acceptance (line 342): "All four outcome values
    # covered at the wrapper level."
    _OUTCOMES = ("merged", "already-ancestor", "unresolvable", "merge-failed")

    def _script(self, monkeypatch) -> str:
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        return build_consensus_wrapped_command("Prompt")[2]

    def test_template_records_all_four_outcomes(self, monkeypatch):
        """Each of the four per-SHA outcome tokens (``merged``,
        ``already-ancestor``, ``unresolvable``, ``merge-failed``) is
        present in the wrapper template. A future refactor that drops
        an outcome — e.g. collapses ``already-ancestor`` back into the
        ``merged`` log line — would re-orphan a path the banner needs
        to distinguish.
        """
        script = self._script(monkeypatch)
        missing = [o for o in self._OUTCOMES if o not in script]
        assert not missing, (
            "Slice-1 outcome contract requires all four per-SHA outcome "
            "tokens in the wrapper template; missing: "
            f"{missing}. Plan TASK-1-1 acceptance: "
            "merged, already-ancestor, unresolvable, merge-failed."
        )

    def test_template_emits_not_synced_banner_text(self, monkeypatch):
        """The banner string the agent sees on a failure outcome is
        pinned to the architect's wording ("worktree NOT synced ...").
        A future regression that downgrades the banner to a log-only
        line would re-introduce the silence R1 is closing.
        """
        script = self._script(monkeypatch)
        assert "NOT synced" in script, (
            "Wrapper template must emit the 'worktree NOT synced' "
            "banner so a reviewer whose sync silently failed cannot "
            "trust a stale local diff. Plan slice-1 banner wording: "
            "'worktree NOT synced to `<sha>` (`<reason>`); treat your "
            "local diff as unreliable — use the `git show` commands "
            "below.'"
        )

    def test_template_references_git_show_fallback_in_banner(self, monkeypatch):
        """The banner steers reviewers at the rendered ``git show``
        delta commands (#3078 served reads) — that is the live
        replacement channel R1 is non-silently surfacing. The
        substring ``git show`` must appear inside the banner-bearing
        region so the agent has a next step beyond "sync failed".
        """
        script = self._script(monkeypatch)
        # The banner-bearing region runs from the start of
        # ``invoke_agent_for_event`` (where ``$prompt`` is composed) to
        # the start of ``sync_to_proposals`` (whose closing fence ends
        # before the main loop). The exact placement of the prepend
        # logic — sync function vs invoke function vs a small helper —
        # is the coder's call; we only require both regions, taken
        # together, to carry the banner-and-fallback text.
        invoke_start = script.index("invoke_agent_for_event() {")
        sync_start = script.index("sync_to_proposals() {")
        body_lo = min(invoke_start, sync_start)
        body_hi = script.index("# --- main event-pump loop ---")
        body = script[body_lo:body_hi]
        assert "NOT synced" in body, (
            "Banner text must live inside the sync/invoke region — "
            "where ``$prompt`` is built or where outcomes are recorded "
            "— so a future refactor cannot hide the banner in a "
            "comment-only block far from the producing/consuming code."
        )
        assert "git show" in body, (
            "Slice-1 banner must point reviewers at the rendered "
            "``git show`` fallback (#3078 served reads) — that is "
            "their next step when the local worktree diff is "
            "unreliable. Plan slice-1 banner wording references "
            "'the `git show` commands below.'"
        )

    # ------------------------------------------------------------------
    # End-to-end harness: real bash + real git + a stubbed event_prompt
    # composer + a captured agent invocation. The four scenarios below
    # exercise each per-SHA outcome and assert presence/absence of the
    # banner in the agent-visible prompt — the slice-1 acceptance.
    # ------------------------------------------------------------------

    _STUB_PROMPT_BODY = "STUB_PROMPT_BODY_FOR_SLICE_1_TESTS"

    def _build_harness(
        self, script: str, repo: str, payload: str, capture: str, stub: str
    ) -> str:
        """Compose a runnable bash harness that links the wrapper's
        ``cw_log`` / ``sync_to_proposals`` / ``invoke_agent_for_event``
        functions, stubs the event_prompt composer (``stub``), and
        captures the prompt handed to the agent into ``capture``.

        The wrapper's substituted agent prefix
        (``python3 -m egg_agent --model ... "$prompt"``) is rewritten to
        a single-line capture sink that writes ``$prompt`` to ``capture``
        verbatim so the test can byte-inspect what reached the agent.
        """
        import re as _re

        cw_match = _re.search(r"cw_log\(\) \{.*?\n\}", script, flags=_re.DOTALL)
        sync_match = _re.search(r"sync_to_proposals\(\) \{.*?\n\}", script, flags=_re.DOTALL)
        invoke_match = _re.search(
            r"invoke_agent_for_event\(\) \{.*?\n\}", script, flags=_re.DOTALL
        )
        assert cw_match is not None
        assert sync_match is not None
        assert invoke_match is not None

        # Swap the substituted ``python3 -m egg_agent ... "$prompt"``
        # call for a capture sink. The trailing ``"$prompt"`` is what
        # carries the agent-visible text; printing it byte-for-byte
        # gives the test a faithful surface for the banner-presence
        # assertions.
        invoke_body = invoke_match.group(0)
        agent_call = _re.compile(r'python3 -m egg_agent[^\n]*"\$prompt"')
        new_invoke, n_subs = agent_call.subn(
            f'printf "%s" "$prompt" > {shlex.quote(capture)}',
            invoke_body,
        )
        assert n_subs == 1, (
            "Expected exactly one ``python3 -m egg_agent ... \"$prompt\"`` "
            "call inside ``invoke_agent_for_event``; the test harness "
            "rewrites that call to a capture sink so the agent-visible "
            "prompt can be byte-inspected."
        )

        return (
            "#!/bin/bash\nset -uo pipefail\n"
            f"export EGG_REPO_PATH={shlex.quote(repo)}\n"
            f"export EGG_EVENT_PROMPT_SCRIPT={shlex.quote(stub)}\n"
            "export EGG_AGENT_ROLE=tester\n"
            "export EGG_BASE_BRANCH=main\n"
            "export EGG_BRC_MEMORY=off\n"
            "export EGG_PIPELINE_ID=test-pipeline\n"
            "export EGG_SLICE_ID=slice-1\n"
            + cw_match.group(0)
            + "\n"
            + sync_match.group(0)
            + "\n"
            + new_invoke
            + "\n"
            + 'sync_to_proposals "$1"\n'
            + 'invoke_agent_for_event "ack" "$1"\n'
            + 'echo "HARNESS_RC=$?"\n'
        )

    def _stub_composer(self, tmp_path) -> str:
        """A minimal stand-in for ``orchestrator/routes/event_prompt.py``
        that emits a known-good body so the test can pin banner-vs-no-
        banner byte equality without the full composer's churn.
        """
        stub = tmp_path / "event_prompt_stub.py"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "_ = sys.stdin.read()\n"
            f"sys.stdout.write({self._STUB_PROMPT_BODY!r})\n",
            encoding="utf-8",
        )
        os.chmod(str(stub), 0o755)  # nosec B103 — test fixture
        return str(stub)

    def _init_repo(self, tmp_path):
        """Initialise a tiny git repo with a base commit so producer /
        reviewer branches can diverge. Returns the repo path."""
        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args):
            subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t",
                },
            )

        git("init", "-q", "-b", "main")
        (repo / "f.txt").write_text("base\n")
        git("add", ".")
        git("commit", "-qm", "base")
        return repo, git

    def _run_harness(self, tmp_path, monkeypatch, payload, capture, stub):
        script = self._script(monkeypatch)
        repo, _ = (
            (None, None)
            if not (tmp_path / "repo").exists()
            else (tmp_path / "repo", None)
        )
        harness = self._build_harness(
            script, str(tmp_path / "repo"), payload, str(capture), stub
        )
        result = subprocess.run(
            ["bash", "-c", harness, "harness", payload],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        # Sync function and invoke function must each exit zero; the
        # idle-budget safety net keeps the event-pump moving even on a
        # composer error, and the slice-1 banner mechanism rides on that
        # invariant (banner is a prompt prefix, not a hard failure).
        assert "HARNESS_RC=0" in result.stdout, (
            "Harness must exit 0; the slice-1 banner is a prompt "
            "prefix, not a hard failure. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        return result

    def test_behavioral_merged_outcome_no_banner(self, tmp_path, monkeypatch):
        """Successful merge → ``merged`` outcome recorded, agent-visible
        prompt is byte-identical to the composed prompt (no banner).

        Plan TASK-1-1 acceptance: "``merged`` and ``already-ancestor``
        outcomes produce no banner and a byte-identical prompt."
        """
        import json as _json

        repo, git = self._init_repo(tmp_path)
        # Producer branch with a real commit that merges cleanly into the
        # reviewer's worktree on main (no conflict).
        git("checkout", "-qb", "producer")
        (repo / "plan.md").write_text("the plan\n")
        git("add", ".")
        git("commit", "-qm", "plan draft")
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git("checkout", "-q", "main")

        payload = _json.dumps(
            {"pending_reviews": [{"producer": "coder", "proposal_commit_sha": sha}]}
        )
        capture = tmp_path / "agent_prompt.txt"
        stub = self._stub_composer(tmp_path)
        result = self._run_harness(tmp_path, monkeypatch, payload, capture, stub)

        assert "merged" in result.stderr, (
            "``merged`` outcome must be observable at the wrapper "
            f"level. stderr={result.stderr!r}"
        )
        prompt = capture.read_text(encoding="utf-8")
        assert prompt == self._STUB_PROMPT_BODY, (
            "Successful merge MUST leave the agent-visible prompt "
            "byte-identical to the composer's output — no banner, no "
            f"prefix. Got: {prompt!r}"
        )

    def test_behavioral_already_ancestor_outcome_no_banner(self, tmp_path, monkeypatch):
        """SHA already in HEAD ancestry → ``already-ancestor`` outcome,
        agent-visible prompt unchanged."""
        import json as _json

        repo, git = self._init_repo(tmp_path)
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        payload = _json.dumps(
            {"pending_reviews": [{"producer": "coder", "proposal_commit_sha": sha}]}
        )
        capture = tmp_path / "agent_prompt.txt"
        stub = self._stub_composer(tmp_path)
        result = self._run_harness(tmp_path, monkeypatch, payload, capture, stub)

        assert "already-ancestor" in result.stderr, (
            "``already-ancestor`` outcome must be observable at the "
            f"wrapper level. stderr={result.stderr!r}"
        )
        prompt = capture.read_text(encoding="utf-8")
        assert prompt == self._STUB_PROMPT_BODY, (
            "Already-ancestor sync MUST leave the agent-visible prompt "
            f"byte-identical. Got: {prompt!r}"
        )

    def test_behavioral_unresolvable_outcome_emits_banner(self, tmp_path, monkeypatch):
        """Well-formed but unknown SHA → ``unresolvable`` outcome AND
        the "NOT synced" banner reaches the agent prompt.

        Plan TASK-1-1 acceptance: "An unresolvable SHA yields
        ``unresolvable`` + banner." Plan TASK-1-3 acceptance: "All four
        outcome values covered at the wrapper level."
        """
        import json as _json

        repo, _git = self._init_repo(tmp_path)
        unresolvable_sha = "a" * 40
        payload = _json.dumps(
            {"pending_reviews": [{"producer": "coder", "proposal_commit_sha": unresolvable_sha}]}
        )
        capture = tmp_path / "agent_prompt.txt"
        stub = self._stub_composer(tmp_path)
        result = self._run_harness(tmp_path, monkeypatch, payload, capture, stub)

        assert "unresolvable" in result.stderr, (
            "``unresolvable`` outcome must be observable at the "
            f"wrapper level. stderr={result.stderr!r}"
        )
        prompt = capture.read_text(encoding="utf-8")
        # Banner contract: SHA + reason word + key phrase.
        assert "NOT synced" in prompt, (
            "Banner must be prepended to the agent-visible prompt on "
            "an ``unresolvable`` outcome — that is the slice-1 R1 "
            f"closing-the-silence behaviour. Prompt: {prompt!r}"
        )
        assert unresolvable_sha in prompt, (
            "Banner must carry the failed SHA so the agent can "
            f"correlate. Prompt: {prompt!r}"
        )
        assert "unresolvable" in prompt, (
            "Banner must name the reason (``unresolvable``) so the "
            "agent knows why their worktree is untrustworthy. "
            f"Prompt: {prompt!r}"
        )
        # Composed body still reaches the agent — the banner is a
        # prefix, not a replacement.
        assert self._STUB_PROMPT_BODY in prompt, (
            "Composed prompt body must still reach the agent after the "
            "banner is prepended; the banner is a prefix, not a "
            f"substitution. Prompt: {prompt!r}"
        )

    def test_behavioral_merge_failed_outcome_emits_banner(self, tmp_path, monkeypatch):
        """Conflicting merge → ``merge-failed`` outcome AND the
        "NOT synced" banner reaches the agent prompt.

        Plan TASK-1-1 acceptance: "A conflicting merge yields a
        ``merge-failed`` outcome and the banner (with SHA and reason)
        in the prompt handed to the agent." Plan TASK-1-3 acceptance
        (the R1 acceptance from refine): "Simulated conflicting merge
        ⇒ banner present in the agent-visible prompt."
        """
        import json as _json

        repo, git = self._init_repo(tmp_path)
        # Reviewer side mutates f.txt on main first; producer mutates
        # the same line on their branch. The merge into the (mutated)
        # main HEAD then conflicts.
        (repo / "f.txt").write_text("reviewer-side\n")
        git("add", ".")
        git("commit", "-qm", "reviewer mutation")
        git("checkout", "-qb", "producer", "HEAD~1")
        (repo / "f.txt").write_text("producer-side\n")
        git("add", ".")
        git("commit", "-qm", "producer mutation")
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git("checkout", "-q", "main")

        payload = _json.dumps(
            {"pending_reviews": [{"producer": "coder", "proposal_commit_sha": sha}]}
        )
        capture = tmp_path / "agent_prompt.txt"
        stub = self._stub_composer(tmp_path)
        result = self._run_harness(tmp_path, monkeypatch, payload, capture, stub)

        assert "merge-failed" in result.stderr, (
            "``merge-failed`` outcome must be observable at the "
            f"wrapper level on a conflicting merge. stderr={result.stderr!r}"
        )
        prompt = capture.read_text(encoding="utf-8")
        assert "NOT synced" in prompt, (
            "Banner must be prepended on a ``merge-failed`` outcome — "
            "the slice-1 R1 acceptance from refine. "
            f"Prompt: {prompt!r}"
        )
        assert sha in prompt, (
            "Banner must carry the failed SHA. "
            f"Prompt: {prompt!r}"
        )
        assert "merge-failed" in prompt, (
            "Banner must name the reason (``merge-failed``) so the "
            "agent can distinguish conflict from unresolvable-SHA. "
            f"Prompt: {prompt!r}"
        )
        assert self._STUB_PROMPT_BODY in prompt, (
            "Composed prompt body must still reach the agent. "
            f"Prompt: {prompt!r}"
        )
