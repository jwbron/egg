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
