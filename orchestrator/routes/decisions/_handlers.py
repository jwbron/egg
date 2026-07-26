"""HITL decision resolution-dispatch hooks (#3312 decomposition).

Pure dispatch logic invoked by ``resolve_decision``: restart-agent handling,
the conditional-ACK 3-way gate router, choice-envelope normalization, and the
executable task-completion hook. The consensus-graph mutations the gate drives
live in ``_graph_mutations``; they are invoked through the package barrel
(``_pkg``) so ``patch("routes.decisions._persist_deferred_actions")`` (etc.)
stays effective after the decomposition.
"""

import json
import re
from pathlib import Path
from typing import Any

import routes.decisions as _pkg
from events import EventType

from . import logger


def _handle_restart_agent(pipeline_id: str, question: str) -> None:
    """Stop and respawn a stalled agent container.

    Parses the agent role from the HITL decision question
    (format: ``"Agent <role> issue: ..."``) and uses the Docker client
    to stop the old container.  A ``CONTAINER_STOPPED`` event is emitted
    so the pipeline orchestration loop can decide whether to respawn.

    Args:
        pipeline_id: Pipeline ID.
        question: The decision question text containing the agent role.
    """
    match = re.match(r"Agent\s+(\S+)\s+issue:", question)
    if not match:
        logger.warning(
            "Could not parse agent role from restart decision",
            pipeline_id=pipeline_id,
            question=question[:120],
        )
        return

    agent_role = match.group(1)
    logger.info(
        "Restarting agent via HITL decision",
        pipeline_id=pipeline_id,
        agent_role=agent_role,
    )

    try:
        from docker_client import get_docker_client

        docker_client = get_docker_client()
        containers = docker_client.list_containers(
            all=False,
            labels={"egg.pipeline.id": pipeline_id, "egg.agent.role": agent_role},
        )
        if not containers:
            logger.warning(
                "No running container found for agent",
                pipeline_id=pipeline_id,
                agent_role=agent_role,
            )
            return

        container = containers[0]
        docker_client.stop_container(container.container_id, timeout=10)
        logger.info(
            "Stopped stalled agent container for restart",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            container_id=container.container_id[:12],
        )

        # Emit event so health monitor / pipeline loop can track the stop
        try:
            _pkg.emit_event(
                EventType.CONTAINER_STOPPED,
                pipeline_id=pipeline_id,
                data={
                    "container_id": container.container_id,
                    "agent_role": agent_role,
                    "reason": "hitl_restart",
                },
            )
        except Exception:
            logger.debug("Failed to emit CONTAINER_STOPPED event", exc_info=True)

    except Exception:
        logger.warning(
            "Failed to restart agent container",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            exc_info=True,
        )


def _normalize_choice_resolution(resolution: str) -> str:
    """Unwrap a structured ``choice`` envelope to its bare option label (#2978).

    The local SDLC HITL CLI resolves a ``choice`` decision by sending
    ``{"action": "select", "selected": "<option>"}`` (see
    ``sandbox/egg_lib/sdlc_hitl.py``); :func:`resolve_decision`
    JSON-serializes that dict into ``decision.resolution``.  Dispatch
    hooks that compare the resolution against bare option labels must
    unwrap the envelope first — otherwise every structured selection
    reads as an unrecognized option.  Bare-string resolutions (legacy /
    direct-API callers) and any other shape pass through unchanged so
    the caller's existing matching still runs.

    Audit-trail note: this is a dispatch-side unwrap only.  The
    persisted ``decision.resolution`` (and the ``DECISION_RESOLVED``
    event / API response payload) still carries the raw envelope JSON
    as the operator sent it.  Only the in-process value routed to the
    Restart-agent / Continue-without / conditional-ACK / hard-reset
    dispatch helpers — and any subsequent log line that echoes it — is
    the normalized form.
    """
    if not resolution:
        return resolution
    try:
        payload = json.loads(resolution)
    except json.JSONDecodeError:
        return resolution
    if isinstance(payload, dict) and payload.get("action") == "select":
        selected = payload.get("selected")
        if isinstance(selected, str):
            return selected
    return resolution


def _handle_conditional_ack_gate(
    pipeline_id: str,
    context: str,
    resolution: str,
    repo_path: Path,
) -> None:
    """Dispatch the 3-way conditional-ACK HITL gate resolution (#2004).

    ``context`` is the decision's raw context field, prefixed with
    ``CONDITIONAL_ACK_GATE_MARKER`` and followed by a JSON payload whose
    ``conditions`` entry is a list of ``{reviewer, producer, condition,
    version}`` dicts. ``resolution`` is the human's choice — one of the
    three option strings defined in ``routes.phases``.

    Dispatch:

    - **approve+accept**: write one line per condition to
      ``contract.pr.deferred_actions`` so obligations survive tracker
      teardown between phase close and PR creation (#2003 shipped the
      tracker-backed PR render; this is the durable path).
    - **reject**: call ``tracker.handle_nack`` on each (reviewer, producer)
      edge carrying a condition. Producer returns to WORKING; the caller
      must restart the phase to re-run consensus.
    - **address-in-pipeline**: call ``matrix.invalidate_ack`` on each
      conditioning edge. The ACK drops back to PENDING; the producer
      must re-propose before the phase can complete.

    Silently returns on malformed context or unknown resolution — the
    resolve_decision endpoint still records the resolution so the human's
    intent is preserved. Recovery relies on ``_ensure_conditional_ack_gate``
    re-queuing a new gate on the next ``complete_phase`` call when the
    tracker still has live conditions (the unresolved-decisions guard only
    checks ``PENDING`` decisions, so a resolved-but-failed gate would not
    be caught by that guard alone).
    """
    from routes.phases import (  # local import — avoid circular
        CONDITIONAL_ACK_ADDRESS,
        CONDITIONAL_ACK_APPROVE,
        CONDITIONAL_ACK_GATE_MARKER,
        CONDITIONAL_ACK_REJECT,
    )

    # #2978: defense-in-depth — unwrap the ``choice`` envelope so a future
    # direct caller bypassing ``resolve_decision``'s dispatch-boundary
    # normalization still sees the bare option label below.  Idempotent on
    # already-unwrapped strings.
    resolution = _pkg._normalize_choice_resolution(resolution)

    if not context.startswith(CONDITIONAL_ACK_GATE_MARKER):
        return
    payload_str = context[len(CONDITIONAL_ACK_GATE_MARKER) :]
    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        logger.warning(
            "Conditional-ACK gate context is not valid JSON",
            pipeline_id=pipeline_id,
        )
        return

    conditions = payload.get("conditions") or []
    if not isinstance(conditions, list):
        return

    if resolution == CONDITIONAL_ACK_APPROVE:
        _pkg._persist_deferred_actions(pipeline_id, conditions, repo_path)
    elif resolution == CONDITIONAL_ACK_REJECT:
        _pkg._force_nack_conditional_edges(pipeline_id, conditions)
    elif resolution == CONDITIONAL_ACK_ADDRESS:
        _pkg._invalidate_conditional_acks(pipeline_id, conditions)
    else:
        logger.info(
            "Conditional-ACK gate resolved with unrecognized option",
            pipeline_id=pipeline_id,
            resolution=resolution[:80],
        )


# Canonical executable task-completion resolution (#3124). Any decision
# whose chosen option (or free-form reply) matches this shape EXECUTES
# the completion when resolved — instead of recording a choice and
# leaving the operator to ``kubectl exec`` into an agent pod and
# impersonate its role. Decision creators that want an executable
# completion option should emit a label of the form
# ``Mark task <task-id> complete`` (backticks around the id tolerated);
# free-form replies may append ``commit <sha>`` to link evidence.
#
# Pattern is anchored to the start of the resolution (optional leading
# whitespace) and matched with ``re.match``: an unanchored ``re.search``
# would fire on prose like "I do **not** want to mark task X complete",
# triggering an audited, durable status mutation buried in a free-form
# "Other (explain in reply)" reply. The documented free-form flow ("Mark
# task <id> complete, commit <sha>") still matches; only buried
# substrings are rejected.
#
# Optional commit evidence is captured ONLY when it immediately follows
# the completion clause, separated by whitespace or punctuation with no
# intervening prose. This prevents replies like "Mark task X complete;
# the prior commit abc1234 was wrong" from attaching ``abc1234`` as
# evidence when the operator was referring to an unrelated commit. The
# documented forms ("Mark task X complete, commit <sha>" / "Mark task X
# complete commit <sha>") still capture; only commits gated behind
# intervening words are rejected.
_COMPLETE_TASK_RESOLUTION_RE = re.compile(
    r"\s*Mark task\s+`{0,2}([A-Za-z0-9._\-]+)`{0,2}\s+complete"
    r"(?:[\s,;:.]+\bcommit[\s:=]+([0-9a-fA-F]{7,40})\b)?",
    re.IGNORECASE,
)


def _maybe_complete_task_from_resolution(
    pipeline_id: str,
    decision_id: str,
    resolution: str | None,
) -> dict[str, Any] | None:
    """Execute an operator ``Mark task <id> complete`` resolution (#3124).

    Returns the executed-action payload (merged into the resolve
    response so the operator sees the completion happened), or ``None``
    when the resolution is not a task-completion choice. Execution
    failure is logged AND surfaced in the returned payload — the
    decision is already marked resolved by the time dispatch runs, so a
    silent failure here would recreate the records-a-choice-but-
    executes-nothing gap this hook closes.
    """
    if not resolution:
        return None
    match = _COMPLETE_TASK_RESOLUTION_RE.match(resolution)
    if not match:
        return None
    task_id = match.group(1)
    commit = match.group(2)

    from operator_actions import OperatorActionError, complete_task_as_operator

    try:
        result = complete_task_as_operator(
            pipeline_id,
            task_id,
            commit=commit,
            reason=f"HITL decision {decision_id} resolved with task-completion option",
            actor=f"operator:decision:{decision_id}",
        )
    except OperatorActionError as exc:
        logger.error(
            "Task-completion resolution failed; task remains incomplete",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            task_id=task_id,
            error=exc.message,
        )
        return {
            "action": "complete_task",
            "task_id": task_id,
            "success": False,
            "error": exc.message,
        }
    except Exception as exc:  # pragma: no cover — defensive
        logger.error(
            "Task-completion resolution raised unexpectedly",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            task_id=task_id,
            error=str(exc),
            exc_info=True,
        )
        return {
            "action": "complete_task",
            "task_id": task_id,
            "success": False,
            "error": str(exc),
        }

    logger.info(
        "Executed task completion from decision resolution",
        pipeline_id=pipeline_id,
        decision_id=decision_id,
        task_id=task_id,
        commit=commit,
    )
    return {"action": "complete_task", "success": True, **result}


# ---------------------------------------------------------------------------
# Consensus-timeout HITL executable retry (#3421)
# ---------------------------------------------------------------------------
#
# The incomplete-consensus HITL (``_persist_hitl_decision`` in
# ``routes/pipelines.py``) is written to pipeline state moments before the
# driver marks the pipeline FAILED and exits — nothing ever waits on it, so
# resolving it was record-only: "Retry phase" was a silent no-op.  The #3233
# revive doesn't cover it either (it is gated on ``phase_gate`` decisions and
# AWAITING_HUMAN status; this decision is a ``choice`` on a ``failed``
# pipeline).  Dispatch is keyed on the decision's context discriminator —
# ``_CONSENSUS_TIMEOUT_HITL_CONTEXT`` in ``routes/pipelines.py`` — not the
# prose question text, mirroring the ``failed_role:`` pattern.
CONSENSUS_TIMEOUT_RETRY_OPTION = "Retry phase"
CONSENSUS_TIMEOUT_ACCEPT_OPTION = "Accept current state"
CONSENSUS_TIMEOUT_ABORT_OPTION = "Abort phase"


def _maybe_dispatch_consensus_timeout_resolution(
    pipeline_id: str,
    decision: Any,
    resolution_label: str | None,
) -> dict[str, Any] | None:
    """Execute a consensus-timeout HITL resolution (#3421).

    Keys on the decision's ``consensus_timeout_incomplete`` context:

    - **Retry phase** → call the ``restart_phase`` route in-process (the
      documented manual workaround), which tears down the failed phase,
      flips the pipeline back to RUNNING, and spawns a fresh driver.
      ``restart_phase`` is lifecycle-secret guarded, but so is the
      resolve-decision route invoking this hook, so the in-request call
      passes the guard — same precedent as the #3233 revive reusing
      ``start_pipeline``.
    - **Abort phase** → no action: the driver already failed the pipeline
      when it escalated this decision, so the state matches the intent.
      The payload says so instead of resolving silently.
    - **Accept current state** → not automated (force-advancing past a
      non-converged phase is an operator judgment); the payload names the
      manual follow-up instead of resolving silently.

    Returns the executed-action payload merged into the resolve response,
    or ``None`` when the decision is not a consensus-timeout HITL (or the
    resolution is a free-form reply).  Failure is logged AND surfaced in
    the payload — the decision is already resolved by the time dispatch
    runs, so a silent failure would recreate the gap this hook closes.
    """
    # Lazy import — single source of truth for the context string;
    # routes.pipelines is too heavy to bind at module import time.
    from routes.pipelines import _CONSENSUS_TIMEOUT_HITL_CONTEXT

    if getattr(decision, "context", "") != _CONSENSUS_TIMEOUT_HITL_CONTEXT:
        return None

    label = (resolution_label or "").strip()

    if label == CONSENSUS_TIMEOUT_ABORT_OPTION:
        return {
            "action": "consensus_timeout_abort",
            "success": True,
            "note": (
                "No action taken: the phase was already marked failed when "
                "this decision was escalated, which is the aborted state."
            ),
        }

    if label == CONSENSUS_TIMEOUT_ACCEPT_OPTION:
        return {
            "action": "consensus_timeout_accept",
            "success": True,
            "note": (
                "Recorded only — accepting a non-converged phase is not "
                "automated. Use advance_phase to move past the failed phase, "
                "or restart_phase to re-run it."
            ),
        }

    if label != CONSENSUS_TIMEOUT_RETRY_OPTION:
        return None

    return _execute_restart_phase(
        pipeline_id, decision, log_label="Consensus-timeout 'Retry phase'"
    )


def _execute_restart_phase(
    pipeline_id: str,
    decision: Any,
    *,
    log_label: str,
) -> dict[str, Any]:
    """Restart the decision's phase in-process and report the outcome.

    Shared executor for HITL resolutions that map to a phase restart (the
    consensus-timeout "Retry phase" — #3421 — and the arms-exhausted
    "Restart phase" — #3496). ``restart_phase`` is lifecycle-secret guarded,
    but so is the resolve-decision route invoking this, so the in-request
    call passes the guard. ``log_label`` names the invoking resolution in
    the log lines.
    """
    phase = getattr(decision, "phase", None)
    phase_val = getattr(phase, "value", phase)
    if not phase_val:
        # Older decisions may lack a pinned phase; restart the pipeline's
        # current phase (restart_phase rejects anything else anyway).
        try:
            store, _ = _pkg.get_state_store_for_pipeline(pipeline_id)
            pipeline = store.load_pipeline(pipeline_id)
            phase_val = pipeline.current_phase.value
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "%s resolution: could not determine phase to restart",
                log_label,
                pipeline_id=pipeline_id,
                decision_id=getattr(decision, "id", "?"),
                error=str(exc),
                exc_info=True,
            )
            return {
                "action": "restart_phase",
                "success": False,
                "error": f"could not determine phase to restart: {exc}",
            }

    try:
        from routes.pipelines import restart_phase

        resp, status_code = restart_phase(pipeline_id, phase_val)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "%s resolution raised; phase not restarted",
            log_label,
            pipeline_id=pipeline_id,
            decision_id=getattr(decision, "id", "?"),
            phase=phase_val,
            error=str(exc),
            exc_info=True,
        )
        return {
            "action": "restart_phase",
            "phase": phase_val,
            "success": False,
            "error": str(exc),
        }

    if status_code != 200:
        try:
            payload = resp.get_json(silent=True) or {}
            error = payload.get("message") or f"restart_phase returned HTTP {status_code}"
        except Exception:  # noqa: BLE001
            error = f"restart_phase returned HTTP {status_code}"
        logger.error(
            "%s resolution could not restart the phase",
            log_label,
            pipeline_id=pipeline_id,
            decision_id=getattr(decision, "id", "?"),
            phase=phase_val,
            status_code=status_code,
            error=error,
        )
        return {
            "action": "restart_phase",
            "phase": phase_val,
            "success": False,
            "error": error,
        }

    logger.info(
        "%s resolution restarted the phase",
        log_label,
        pipeline_id=pipeline_id,
        decision_id=getattr(decision, "id", "?"),
        phase=phase_val,
    )
    return {"action": "restart_phase", "phase": phase_val, "success": True}


# ---------------------------------------------------------------------------
# Arms-exhausted HITL executable resolutions (#3496)
# ---------------------------------------------------------------------------
#
# The event loop escalates an all-arms-exhausted livelock (every spawn arm
# blocked on an exhausted dedupe key, nothing in flight) as a persisted HITL
# decision — see ``ConcurrentPhaseExecutor._handle_arms_exhausted``. Dispatch
# keys on the decision's ``event_arms_exhausted`` context (imported lazily
# from ``concurrent_executor``, its single source). "Retry arms" is the
# in-band recovery the incident lacked: it clears the exhausted keys on the
# pipeline's live event loop(s) so the blocked arms respawn with fresh
# budgets — no phase teardown, nothing in flight is lost.


def _maybe_dispatch_arms_exhausted_resolution(
    pipeline_id: str,
    decision: Any,
    resolution_label: str | None,
) -> dict[str, Any] | None:
    """Execute an arms-exhausted HITL resolution (#3496).

    - **Retry arms (reset spawn budgets)** → clear the exhausted keys on
      every live event loop registered for this pipeline (the live-loop
      registry in ``event_loop``), giving the blocked arms fresh spawn
      budgets. Reports the cleared keys per slice. Fails informatively when
      no live loop exists (the orchestrator restarted — supervision state is
      per-process and already reset, so a still-wedged pipeline needs
      "Restart phase" instead).
    - **Restart phase** → tear down and re-run the decision's phase
      in-process (shared ``_execute_restart_phase`` executor).
    - **Abort (manual — recorded only)** → not automated (stopping the
      pipeline is a distinct operator action); the payload names
      ``cancel_task`` instead of resolving silently.

    Returns the executed-action payload, or ``None`` when the decision is
    not an arms-exhausted HITL (or the resolution is a free-form reply).
    """
    # Lazy import — single source of truth for the context/option strings;
    # concurrent_executor is not needed at module import time.
    from concurrent_executor import (
        ARMS_EXHAUSTED_ABORT_OPTION,
        ARMS_EXHAUSTED_HITL_CONTEXT,
        ARMS_EXHAUSTED_RESTART_OPTION,
        ARMS_EXHAUSTED_RETRY_OPTION,
    )

    if getattr(decision, "context", "") != ARMS_EXHAUSTED_HITL_CONTEXT:
        return None

    label = (resolution_label or "").strip()

    if label == ARMS_EXHAUSTED_ABORT_OPTION:
        return {
            "action": "arms_exhausted_abort",
            "success": True,
            "note": (
                "Recorded only — aborting is not automated from this "
                "decision. Use cancel_task to stop the pipeline, or resolve "
                "again with 'Restart phase' to re-run it."
            ),
        }

    if label == ARMS_EXHAUSTED_RESTART_OPTION:
        return _execute_restart_phase(
            pipeline_id, decision, log_label="Arms-exhausted 'Restart phase'"
        )

    if label != ARMS_EXHAUSTED_RETRY_OPTION:
        return None

    from event_loop import get_live_event_loops

    loops = get_live_event_loops(pipeline_id)
    if not loops:
        logger.warning(
            "Arms-exhausted 'Retry arms' resolution found no live event loop",
            pipeline_id=pipeline_id,
            decision_id=getattr(decision, "id", "?"),
        )
        return {
            "action": "reset_exhausted_arms",
            "success": False,
            "error": (
                "no live event loop for this pipeline — the orchestrator "
                "likely restarted since the decision was raised (supervision "
                "state is per-process and resets on restart). If the "
                "pipeline is still wedged, resolve with 'Restart phase'."
            ),
        }

    cleared_by_slice: dict[str, list[str]] = {}
    for loop in loops:
        cleared = loop.reset_exhausted_arms()
        if cleared:
            cleared_by_slice[loop.slice_id or "pipeline"] = cleared
    total = sum(len(keys) for keys in cleared_by_slice.values())
    logger.info(
        "Arms-exhausted 'Retry arms' resolution cleared exhausted keys",
        pipeline_id=pipeline_id,
        decision_id=getattr(decision, "id", "?"),
        cleared=total,
        slices=sorted(cleared_by_slice),
    )
    return {
        "action": "reset_exhausted_arms",
        "success": True,
        "cleared_total": total,
        "cleared_by_slice": cleared_by_slice,
        "note": (
            "Exhausted spawn budgets were reset; the blocked arms respawn on "
            "the next event-loop poll. If the underlying failure persists "
            "the arms will re-exhaust and this decision will re-fire."
            if total
            else "No exhausted keys were held by the live event loop(s); nothing to clear."
        ),
    }


def _maybe_dispatch_arms_parked_resolution(
    pipeline_id: str,
    decision: Any,
    resolution_label: str | None,
) -> dict[str, Any] | None:
    """Execute an arms-parked HITL resolution (#3548).

    The no-op-park twin of :func:`_maybe_dispatch_arms_exhausted_resolution`:

    - **Retry arms (release no-op parks)** → clear the no-op-parked keys on
      every live event loop registered for this pipeline so the blocked
      arms respawn immediately instead of waiting out the park retry
      heartbeat. Reports the cleared keys per slice; fails informatively
      when no live loop exists.
    - **Restart phase** → tear down and re-run the decision's phase
      (shared ``_execute_restart_phase`` executor).
    - **Abort (manual — recorded only)** → recorded only; points at
      ``cancel_task``.
    """
    from concurrent_executor import (
        ARMS_EXHAUSTED_ABORT_OPTION,
        ARMS_EXHAUSTED_RESTART_OPTION,
        ARMS_PARKED_HITL_CONTEXT,
        ARMS_PARKED_RETRY_OPTION,
    )

    if getattr(decision, "context", "") != ARMS_PARKED_HITL_CONTEXT:
        return None

    label = (resolution_label or "").strip()

    if label == ARMS_EXHAUSTED_ABORT_OPTION:
        return {
            "action": "arms_parked_abort",
            "success": True,
            "note": (
                "Recorded only — aborting is not automated from this "
                "decision. Use cancel_task to stop the pipeline, or resolve "
                "again with 'Restart phase' to re-run it."
            ),
        }

    if label == ARMS_EXHAUSTED_RESTART_OPTION:
        return _execute_restart_phase(
            pipeline_id, decision, log_label="Arms-parked 'Restart phase'"
        )

    if label != ARMS_PARKED_RETRY_OPTION:
        return None

    from event_loop import get_live_event_loops

    loops = get_live_event_loops(pipeline_id)
    if not loops:
        logger.warning(
            "Arms-parked 'Retry arms' resolution found no live event loop",
            pipeline_id=pipeline_id,
            decision_id=getattr(decision, "id", "?"),
        )
        return {
            "action": "reset_parked_arms",
            "success": False,
            "error": (
                "no live event loop for this pipeline — the orchestrator "
                "likely restarted since the decision was raised (supervision "
                "state is per-process and resets on restart). If the "
                "pipeline is still stalled, resolve with 'Restart phase'."
            ),
        }

    cleared_by_slice: dict[str, list[str]] = {}
    for loop in loops:
        cleared = loop.reset_parked_arms()
        if cleared:
            cleared_by_slice[loop.slice_id or "pipeline"] = cleared
    total = sum(len(keys) for keys in cleared_by_slice.values())
    logger.info(
        "Arms-parked 'Retry arms' resolution released no-op parks",
        pipeline_id=pipeline_id,
        decision_id=getattr(decision, "id", "?"),
        cleared=total,
        slices=sorted(cleared_by_slice),
    )
    return {
        "action": "reset_parked_arms",
        "success": True,
        "cleared_total": total,
        "cleared_by_slice": cleared_by_slice,
        "note": (
            "No-op parks were released; the blocked arms respawn on the "
            "next event-loop poll. If the agents keep no-oping the arms "
            "will re-park and this decision will re-fire."
            if total
            else "No parked keys were held by the live event loop(s); nothing to clear."
        ),
    }


# ---------------------------------------------------------------------------
# Driver-liveness HITL executable resolutions (#3540)
# ---------------------------------------------------------------------------
#
# The kubernetes-monitor sweep escalates a dead/hung/no-progress
# ``_run_pipeline`` driver as a persisted HITL decision (see
# ``KubernetesMonitor._handle_driver_liveness_results``). Dispatch keys on
# the decision's ``driver_liveness_stall`` context (imported lazily from
# ``health_checks.tier1.driver_liveness``, its single source). "Retry phase"
# reuses the shared restart-phase executor; the wedged/dead driver cannot
# consume the resolution itself, so without this dispatch the resolution
# would be the silent no-op this decision exists to prevent (#3421 class).


def _maybe_dispatch_driver_liveness_resolution(
    pipeline_id: str,
    decision: Any,
    resolution_label: str | None,
) -> dict[str, Any] | None:
    """Execute a driver-liveness HITL resolution (#3540).

    - **Retry phase** → tear down and re-run the decision's phase
      in-process (shared ``_execute_restart_phase`` executor). This also
      replaces the wedged/dead driver thread: ``restart_phase`` bumps
      ``run_epoch`` and spawns a fresh ``_run_pipeline`` driver.
    - **Dismiss (recorded only)** → not automated; the alert re-fires on
      the monitor's re-alert cadence if the stall persists.

    Returns the executed-action payload, or ``None`` when the decision is
    not a driver-liveness HITL (or the resolution is a free-form reply).
    """
    from health_checks.tier1.driver_liveness import (
        DRIVER_LIVENESS_DISMISS_OPTION,
        DRIVER_LIVENESS_HITL_CONTEXT,
        DRIVER_LIVENESS_RETRY_OPTION,
    )

    if getattr(decision, "context", "") != DRIVER_LIVENESS_HITL_CONTEXT:
        return None

    label = (resolution_label or "").strip()

    if label == DRIVER_LIVENESS_DISMISS_OPTION:
        return {
            "action": "driver_liveness_dismiss",
            "success": True,
            "note": (
                "Recorded only; no automated action. The driver-liveness "
                "alert re-fires if the stall persists; use cancel_task to "
                "stop the pipeline, or resolve a future alert with "
                "'Retry phase' to re-run the phase."
            ),
        }

    if label != DRIVER_LIVENESS_RETRY_OPTION:
        return None

    return _execute_restart_phase(pipeline_id, decision, log_label="Driver-liveness 'Retry phase'")


# ---------------------------------------------------------------------------
# Executable adds_task option (#3428)
# ---------------------------------------------------------------------------
#
# ``register_open_question`` lets agents pose options that mandate a contract
# mutation ("Add a new task/slice to wire X as a dependency"), but no agent
# has a task-add verb — resolving such a decision used to record the choice
# and materialize nothing, so the reviewer that raised the question kept
# withholding ACK and the slice re-deadlocked *after* the human answered.
# Options that mandate the mutation now carry a structured ``adds_task``
# payload (``egg_contracts.models.AddsTaskPayload``, attached at registration
# time); when the operator resolves the decision by selecting that option,
# the hook below executes the mutation via
# ``operator_actions.add_task_as_operator`` — same audited executor family
# as the ``Mark task <id> complete`` option (#3124).

# Bridge context fingerprint — must stay in sync with the composer in
# ``_queue_and_await_contract_decisions`` (routes/pipelines.py) and the
# post-gate guard in ``_resolve_contract_decision``.
_BRIDGED_CQ_CONTEXT_RE = re.compile(r"^Open contract question (cq-[0-9]+),")


def _resolution_selects_option(option: Any, resolution: str) -> bool:
    """True when ``resolution`` unambiguously selects ``option``.

    Operators resolve ``choice`` decisions with heterogeneous strings: the
    bare option label (the SDLC HITL CLI envelope is unwrapped upstream by
    ``_normalize_choice_resolution``), the option id (``opt-1``), or a
    positional reference (``option 1`` / ``1``). All are unambiguous
    identifiers of a single option; anything else (free-form prose) does not
    select — an audited contract mutation must never fire on a fuzzy match.
    """
    normalized = (resolution or "").strip().casefold()
    if not normalized:
        return False
    label = (getattr(option, "label", "") or "").strip().casefold()
    if label and normalized == label:
        return True
    option_id = (getattr(option, "id", "") or "").strip().casefold()
    if not option_id:
        return False
    if normalized == option_id:
        return True
    positional = re.match(r"^(?:opt(?:ion)?[\s-]*)?([0-9]+)$", normalized)
    return bool(positional) and option_id == f"opt-{positional.group(1)}"


def _load_contract_decision(pipeline_id: str, decision_id: str) -> Any | None:
    """Load the contract decision ``decision_id`` (``cq-N``) for ``pipeline_id``.

    Best-effort: returns ``None`` (and logs) when the worktree/contract can't
    be loaded or no decision matches. Used by resolution-dispatch hooks on the
    bridged queue path, where the pipeline ``HITLDecision`` carries only bare
    option labels and the structured payload must be recovered from the
    contract.
    """
    import contract_store

    try:
        from egg_contracts import load_contract
        from routes.pipelines import _pipeline_identifier

        store, _ = _pkg.get_state_store_for_pipeline(pipeline_id)
        pipeline = store.load_pipeline(pipeline_id)
        worktree = contract_store.resolve_pipeline_worktree(pipeline_id)
        if worktree is None:
            return None
        identifier = _pipeline_identifier(getattr(pipeline, "issue_number", None), pipeline_id)
        contract = load_contract(identifier, worktree)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not load contract to recover decision for resolution dispatch",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            exc_info=True,
        )
        return None

    return next(
        (d for d in (getattr(contract, "decisions", None) or []) if d.id == decision_id),
        None,
    )


def _maybe_add_task_from_resolution(
    pipeline_id: str,
    decision: Any,
    resolution: str | None,
) -> dict[str, Any] | None:
    """Execute an ``adds_task``-carrying option resolution (#3428).

    ``decision`` is either the contract ``Decision`` (the pre-bridge
    ``_resolve_contract_decision`` path — its ``options`` carry the
    structured payload directly) or the pipeline ``HITLDecision`` (the
    bridged queue path — options are bare labels, so the contract decision
    is recovered via the bridge's context fingerprint).

    Returns the executed-action payload (merged into the resolve response so
    the operator sees the materialization happened), or ``None`` when the
    resolution does not select an ``adds_task`` option. Execution failure is
    logged AND surfaced in the returned payload — the decision is already
    marked resolved by the time dispatch runs, so a silent failure here would
    recreate the records-a-choice-but-executes-nothing gap this hook closes.
    """
    if not resolution:
        return None

    options = list(getattr(decision, "options", None) or [])
    decision_id = getattr(decision, "id", "?")
    if not any(getattr(o, "adds_task", None) for o in options):
        # Bridged queue path (or a decision with no structured payload):
        # recover the contract decision named in the bridge context. Bail
        # early when the context carries no fingerprint — that covers every
        # ordinary queue decision without a contract round-trip.
        context = getattr(decision, "context", "") or ""
        match = _BRIDGED_CQ_CONTEXT_RE.match(context)
        if not match:
            return None
        contract_decision = _load_contract_decision(pipeline_id, match.group(1))
        if contract_decision is None:
            return None
        options = list(contract_decision.options or [])
        if not any(getattr(o, "adds_task", None) for o in options):
            return None

    selected = next((o for o in options if _resolution_selects_option(o, resolution)), None)
    payload = getattr(selected, "adds_task", None) if selected is not None else None
    if payload is None:
        return None

    from operator_actions import OperatorActionError, add_task_as_operator

    try:
        result = add_task_as_operator(
            pipeline_id,
            payload.slice_id,
            payload.description,
            acceptance_criteria=payload.acceptance_criteria,
            files_affected=list(payload.files_affected or []),
            role=payload.role,
            reason=(
                f"HITL decision {decision_id} resolved with adds_task option "
                f"{getattr(selected, 'id', '?')}"
            ),
            actor=f"operator:decision:{decision_id}",
        )
    except OperatorActionError as exc:
        logger.error(
            "adds_task resolution failed; mandated task was NOT materialized",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            slice_id=payload.slice_id,
            error=exc.message,
        )
        return {
            "action": "add_task",
            "slice_id": payload.slice_id,
            "success": False,
            "error": exc.message,
        }
    except Exception as exc:  # pragma: no cover — defensive
        logger.error(
            "adds_task resolution raised unexpectedly",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            slice_id=payload.slice_id,
            error=str(exc),
            exc_info=True,
        )
        return {
            "action": "add_task",
            "slice_id": payload.slice_id,
            "success": False,
            "error": str(exc),
        }

    logger.info(
        "Executed adds_task materialization from decision resolution",
        pipeline_id=pipeline_id,
        decision_id=decision_id,
        task_id=result.get("task_id"),
        slice_id=result.get("slice_id"),
    )
    return {"action": "add_task", "success": True, **result}


# ---------------------------------------------------------------------------
# First-principles redirect accept-path
# ---------------------------------------------------------------------------
#
# The ``first_principles_reviewer`` files a refine-phase HITL decision whose
# options are EXACTLY these labels; the resolve hook below keys on the resolved
# label. SINGLE SOURCE OF TRUTH — the reviewer criteria in
# ``routes/pipelines.py`` interpolate these same strings into the agent's
# prompt, so the label the agent writes and the label this hook matches can
# never drift. The reviewer also carries the proposed new direction on the
# decision's ``redirect_seed`` field (written through the same contract-mutate
# RPC that creates the decision — the one channel proven to reach the shared
# pipeline worktree); ``adopt`` reads it back from there.
FIRST_PRINCIPLES_ADOPT_OPTION = "Adopt the redirect (rewrite the seed and re-run the refine phase)"
FIRST_PRINCIPLES_PROCEED_OPTION = "Proceed as-is (the current direction stands)"
FIRST_PRINCIPLES_CANCEL_OPTION = "Don't build this (cancel the pipeline)"
FIRST_PRINCIPLES_OPTIONS = (
    FIRST_PRINCIPLES_ADOPT_OPTION,
    FIRST_PRINCIPLES_PROCEED_OPTION,
    FIRST_PRINCIPLES_CANCEL_OPTION,
)


def _read_first_principles_redirect(pipeline_id: str, decision: Any) -> str | None:
    """Recover the reviewer's proposed redirect (the new seed) for ``adopt``.

    The ``first_principles_reviewer`` carries its proposed redirect on the
    decision's ``redirect_seed`` field, written through the same
    ``register_open_question`` → contract-mutate RPC that creates the decision.
    That RPC writes **directly into the shared pipeline worktree**, so unlike a
    free-standing file in the reviewer's per-agent worktree (which has no
    commit/push path off it under BRC isolation) the payload actually reaches
    the orchestrator.

    The primary resolve path hands us the contract ``Decision`` itself, so the
    field is read straight off ``decision``. The bridged queue path hands us a
    pipeline ``HITLDecision`` (a ``list[str]`` of bare option labels with no
    structured payload), so we fall back to reloading the contract and reading
    ``redirect_seed`` off the matching ``cq-N`` decision. Returns the stripped
    new seed, or ``None`` when no payload is present.
    """
    seed = getattr(decision, "redirect_seed", None)
    if isinstance(seed, str) and seed.strip():
        return seed.strip()
    # Fallback (bridged queue path): the pipeline HITLDecision doesn't carry
    # ``redirect_seed``; recover it from the contract decision of the same id.
    return _read_redirect_seed_from_contract(pipeline_id, getattr(decision, "id", None))


def _read_redirect_seed_from_contract(pipeline_id: str, decision_id: Any) -> str | None:
    """Read ``redirect_seed`` off the contract decision ``decision_id``.

    Best-effort: returns ``None`` (and logs) when the worktree/contract can't
    be loaded or the decision carries no redirect payload. When ``decision_id``
    doesn't resolve to a contract decision, scans for the single first-
    principles decision carrying a ``redirect_seed`` (the reviewer is the only
    producer of that field, so the match is unambiguous).
    """
    import contract_store

    try:
        from egg_contracts import load_contract
        from routes.pipelines import _pipeline_identifier

        store, _ = _pkg.get_state_store_for_pipeline(pipeline_id)
        pipeline = store.load_pipeline(pipeline_id)
        worktree = contract_store.resolve_pipeline_worktree(pipeline_id)
        if worktree is None:
            return None
        identifier = _pipeline_identifier(getattr(pipeline, "issue_number", None), pipeline_id)
        contract = load_contract(identifier, worktree)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not load contract to recover first-principles redirect seed",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            exc_info=True,
        )
        return None

    decisions = getattr(contract, "decisions", None) or []
    seed_carriers: list[tuple[Any, str]] = []
    for d in decisions:
        seed = getattr(d, "redirect_seed", None)
        if not (isinstance(seed, str) and seed.strip()):
            continue
        did = getattr(d, "id", None)
        if did == decision_id:
            return seed.strip()
        seed_carriers.append((did, seed.strip()))

    if not seed_carriers:
        return None
    # Id miss: fall back to the sole redirect-carrying decision. With more than
    # one candidate the choice is order-dependent (no id matched), so warn —
    # in normal operation the reviewer files exactly one such decision.
    if len(seed_carriers) > 1:
        logger.warning(
            "Ambiguous first-principles redirect fallback: %d decisions carry a "
            "redirect_seed but none match the resolved id; using the last",
            len(seed_carriers),
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            candidate_ids=[did for did, _ in seed_carriers],
        )
    return seed_carriers[-1][1]


def _cancel_pipeline_in_process(pipeline_id: str, *, reason: str) -> None:
    """Cancel a pipeline from a resolution hook (status CANCELLED + loop stop).

    Mirrors the inline cancel pattern used elsewhere: flip status under the
    pipeline state lock, stop the pipeline's live BRC event loops, emit
    ``PIPELINE_CANCELLED``, and cancel pending decisions so any
    ``wait_for_decision`` unblocks.

    This is the second place CANCELLED originates (the PATCH route is the
    first), so it needs the same #3633 loop teardown: without it the live
    loops keep deriving arms and requesting one-shot Jobs, and only the
    driver loops' own CANCELLED re-reads — a poll interval later — would
    catch it. Note this hook does NOT tear down containers; the driver's
    cancel bail and the operator's own cleanup own that half.
    """
    from models import PipelineStatus
    from state_store import get_pipeline_state_lock

    store, _ = _pkg.get_state_store_for_pipeline(pipeline_id)
    lock = get_pipeline_state_lock(pipeline_id)
    with lock:
        pipeline = store.load_pipeline(pipeline_id)
        pipeline.status = PipelineStatus.CANCELLED
        pipeline.error = reason
        store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))

    try:
        # Deferred import: routes.pipelines is too heavy to bind at module
        # import time (same reason as the other hooks in this file).
        from routes.pipelines import _stop_pipeline_event_loops

        _stop_pipeline_event_loops(pipeline_id, reason="pipeline_cancelled")
    except Exception:
        logger.warning(
            "Failed to stop BRC event loops after first-principles cancel",
            pipeline_id=pipeline_id,
            exc_info=True,
        )

    try:
        _pkg.emit_event(
            EventType.PIPELINE_CANCELLED,
            pipeline_id=pipeline_id,
            data={"reason": reason},
        )
    except Exception:
        logger.warning(
            "Failed to emit PIPELINE_CANCELLED event after first-principles cancel",
            pipeline_id=pipeline_id,
            exc_info=True,
        )
    try:
        queue = _pkg.get_decision_queue(pipeline_id, store.repo_path)
        for d in queue.get_pending_decisions():
            queue.cancel_decision(d.id)
    except Exception:
        logger.warning(
            "Failed to cancel pending decisions after first-principles cancel",
            pipeline_id=pipeline_id,
            exc_info=True,
        )


def _maybe_apply_first_principles_redirect(
    pipeline_id: str,
    decision: Any,
    resolution_label: str,
    pipeline: Any,
) -> dict[str, Any] | None:
    """Execute a first-principles redirect resolution — the accept-path.

    Keys on the resolved option label (one of ``FIRST_PRINCIPLES_OPTIONS``):

    - **Adopt** → rewrite the seed to the reviewer's proposed redirect and
      re-run the refine phase against it.
    - **Don't build** → cancel the pipeline.
    - **Proceed** → no-op; the current direction stands and the decision is
      simply marked resolved.

    Returns an executed-action payload merged into the resolve response, or
    ``None`` when the resolution is not a first-principles option. Failure is
    logged AND surfaced in the payload — the decision is already resolved by
    the time dispatch runs, so a silent failure would strand the operator's
    intent.
    """
    label = resolution_label or ""
    if label not in FIRST_PRINCIPLES_OPTIONS:
        return None

    # Guard: only act on refine-phase first-principles decisions. The labels
    # are specific enough to be effectively unique, but the phase check keeps a
    # coincidental match outside refine from triggering a seed rewrite. The
    # phase is always populated on both resolve paths (the contract Decision is
    # filed with ``--phase refine``; the bridged HITLDecision auto-infers it
    # from the pipeline's current phase, which is refine while this gate is
    # open), so require an exact match rather than letting ``phase=None``
    # through.
    phase = getattr(decision, "phase", None)
    phase_val = getattr(phase, "value", phase)
    if phase_val != "refine":
        return None

    decision_id = getattr(decision, "id", "?")

    if label == FIRST_PRINCIPLES_PROCEED_OPTION:
        logger.info(
            "First-principles redirect: operator chose proceed-as-is",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
        )
        return {"action": "first_principles_redirect", "outcome": "proceed", "success": True}

    if label == FIRST_PRINCIPLES_CANCEL_OPTION:
        try:
            _cancel_pipeline_in_process(
                pipeline_id,
                reason=f"first-principles redirect: operator chose not to build (decision {decision_id})",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "First-principles cancel failed; pipeline not cancelled",
                pipeline_id=pipeline_id,
                decision_id=decision_id,
                error=str(exc),
                exc_info=True,
            )
            return {
                "action": "first_principles_redirect",
                "outcome": "cancel",
                "success": False,
                "error": str(exc),
            }
        return {"action": "first_principles_redirect", "outcome": "cancelled", "success": True}

    # Adopt: rewrite the seed and re-run refine.
    new_seed = _read_first_principles_redirect(pipeline_id, decision)
    if not new_seed:
        logger.error(
            "First-principles adopt: no proposed redirect found on the "
            "decision's redirect_seed; cannot rewrite the seed",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
        )
        return {
            "action": "first_principles_redirect",
            "outcome": "adopt",
            "success": False,
            "error": "no proposed redirect found on the decision's redirect_seed",
        }
    try:
        from routes.pipelines import apply_first_principles_redirect

        agents = apply_first_principles_redirect(
            pipeline_id, new_seed, reason=f"decision {decision_id} adopted"
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "First-principles adopt failed; seed/refine state may be partially "
            "updated (the decision is still resolved)",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            error=str(exc),
            exc_info=True,
        )
        return {
            "action": "first_principles_redirect",
            "outcome": "adopt",
            "success": False,
            "error": str(exc),
        }
    logger.info(
        "First-principles redirect adopted: seed rewritten, refine re-run",
        pipeline_id=pipeline_id,
        decision_id=decision_id,
        agents_restarted=agents,
    )
    return {
        "action": "first_principles_redirect",
        "outcome": "adopted",
        "success": True,
        "agents_restarted": agents,
    }
