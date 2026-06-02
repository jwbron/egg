"""Event-driven message handlers (wait, wait_loop, heartbeat).

These back the ``egg-orch message wait``, ``egg-orch message wait-loop``,
and ``egg-orch message heartbeat`` CLI subcommands introduced in
issue #1897, and the ``mcp__brc__wait_for_event`` /
``mcp__brc__wait_loop`` / ``mcp__brc__send_heartbeat`` MCP tools that
expose the same primitives to SDK agents.

The handler functions return structured dicts.  The CLI shim in
``sandbox/egg_lib/orch_cli.py`` maps responses and exceptions onto the
0/1/2/3 exit-code contract #1897 documented (0=match, 1=timeout/no
match, 2=transient error, 3=permanent error).  MCP callers receive the
structured dict directly and classify ``matched``/``ok`` themselves.
"""

from __future__ import annotations

import time as _time
from typing import Any

from egg_agent_tools.handlers._gateway import (
    get_agent_role,
    get_pipeline_id,
    orchestrator_request,
)
from egg_agent_tools.handlers._gateway import maybe_attach_slice_id as _maybe_attach_slice_id
from egg_agent_tools.handlers.errors import GatewayError, HandlerError

_HEARTBEAT_STATES = {
    "WORKING",
    "WAITING_ON_ROLE",
    "WAITING_FOR_EVENT",
    "PROPOSED",
    "IDLE",
}

# Slice-4 task-4-2 deleted the agent-side ``message_wait_loop`` heartbeat /
# gateway-session keep-alive path (``_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS``,
# ``_default_emit_wait_loop_heartbeat``, ``_start_wait_loop_heartbeat``,
# and the per-iteration emit_hb invocations inside ``message_wait_loop``).
# The event-pump wrapper now owns both responsibilities:
#
#   * heartbeat liveness (#2036): the wrapper's
#     ``start_background_heartbeat`` subshell in
#     ``orchestrator/consensus_wrapper.py:_EVENT_PUMP_WRAPPER_TEMPLATE``
#     emits ``egg-orch message heartbeat`` every 30 s while blocking on
#     ``egg-orch message wait-loop``.
#   * gateway-session keep-alive (#2451): the same heartbeat carries
#     ``slice_id`` (sourced from ``$EGG_SLICE_ID``), so the orchestrator's
#     ``_maybe_attach_slice_id`` fan-out refreshes the slice-scoped
#     container session as a side effect of every wrapper heartbeat.
#
# ``message_heartbeat`` (the explicit handler below) is unchanged — it
# is still the path the wrapper bash invokes via
# ``egg-orch message heartbeat`` and that callers like the overseer
# self-test still exercise directly.


def _require_pipeline_id(req: dict[str, Any]) -> str:
    pid = req.get("pipeline_id") or get_pipeline_id()
    if not pid:
        raise HandlerError("pipeline_id required. Set EGG_PIPELINE_ID or pass 'pipeline_id'.")
    return pid


def _role_or_env(req: dict[str, Any]) -> str | None:
    return req.get("role") or get_agent_role()


def _require_role(req: dict[str, Any]) -> str:
    role = _role_or_env(req)
    if not role:
        raise HandlerError("role required. Set EGG_AGENT_ROLE or pass 'role'.")
    return role


def _coerce_for_types(req: dict[str, Any]) -> list[str]:
    """Accept either ``for`` or ``for_types`` (list of strings)."""
    raw = req.get("for_types") if req.get("for_types") is not None else req.get("for")
    if isinstance(raw, str):
        types = [raw]
    else:
        types = list(raw or [])
    types = [t for t in types if t]
    if not types:
        raise HandlerError("at least one message type is required ('for' / 'for_types')")
    return types


def message_wait(req: dict[str, Any]) -> dict[str, Any]:
    """Event-driven block until a typed message arrives.

    Request:
        for_types / for (list[str]): message types to wait for — required.
        role (str): filter for this role (defaults to EGG_AGENT_ROLE).
        from_role / from (str): filter by sender role.
        since (str): return messages after this ID.
        limit (int): max messages to return.
        timeout (int): server-side block timeout (default 60).
        pipeline_id: override.

    Response:
        { ok: True, matched: bool, messages: list, cursor: str | None,
          role: str | None, for_types: list[str], raw: <server response> }

    ``cursor`` threads through the wait-endpoint's stream cursor so
    callers can chain successive waits without losing events that arrive
    between calls (issue #1995). On match it is the ID of the last
    delivered message; on timeout it is the stream tip at server
    response time; ``None`` only when the stream is empty.

    Raises:
        HandlerError: invalid arguments.
        GatewayError: server error, timeout, or 4xx — the CLI shim maps
            ``status_code`` onto transient (2) / permanent (3) rcs.
    """
    pid = _require_pipeline_id(req)
    role = _role_or_env(req)
    for_types = _coerce_for_types(req)

    timeout = req.get("timeout")
    if timeout is None:
        timeout = 60
    try:
        timeout_i = int(timeout)
    except (TypeError, ValueError) as exc:
        raise HandlerError("'timeout' must be an integer number of seconds") from exc

    params: list[tuple[str, str]] = [("for", t) for t in for_types]
    if role:
        params.append(("role", role))
    from_role = req.get("from_role") or req.get("from")
    if from_role:
        params.append(("from", str(from_role)))
    # #2725: producer allowlist — repeatable set form of ``from`` so a
    # reviewer can name every producer it reviews (and the system senders
    # ``overseer`` / ``orchestrator``) in one call.
    from_roles = req.get("from_roles")
    if from_roles and not from_role:
        for r in from_roles:
            if r:
                params.append(("from_producer", str(r)))
    # #2725: slice scope — only match messages whose metadata.slice_id
    # equals this OR is null (pipeline-level passthrough).
    slice_id = req.get("slice_id")
    if slice_id:
        params.append(("slice", str(slice_id)))
    if req.get("since"):
        params.append(("since_id", str(req["since"])))
    if req.get("limit"):
        params.append(("limit", str(req["limit"])))
    params.append(("timeout", str(timeout_i)))

    from urllib.parse import urlencode

    endpoint = f"/api/v1/pipelines/{pid}/messages/wait?{urlencode(params)}"
    # Add a generous buffer over the server-side timeout so the socket
    # doesn't expire before the long-poll returns.
    client_timeout = timeout_i + 10
    result = orchestrator_request(endpoint, timeout=client_timeout)

    data = result.get("data", {}) if isinstance(result, dict) else {}
    messages = list(data.get("messages", []) or [])
    matched = bool(data.get("matched")) or bool(messages)
    cursor = data.get("cursor")
    # Issue #2464: server signals when ``since_id`` resolved to no
    # known message in the store (typically because a phase-boundary
    # ``clear`` wiped the cursor). The CLI uses this to drop its on-disk
    # cursor file so the next wait re-snaps to tip instead of feeding
    # the dead cursor back forever.
    since_id_stale = bool(data.get("since_id_stale"))
    return {
        "ok": True,
        "matched": matched,
        "messages": messages,
        "cursor": cursor,
        "since_id_stale": since_id_stale,
        "role": role,
        "for_types": for_types,
        "raw": result,
    }


def message_wait_loop(req: dict[str, Any]) -> dict[str, Any]:
    """Loop ``message_wait`` until a match arrives.

    Blocks through timeouts (keeps re-issuing the wait) and short
    exponential backoff on transient gateway errors (capped at 5s).
    Permanent errors (HandlerError or GatewayError with 4xx non-408)
    propagate to the caller.

    Threads the server-side ``cursor`` through successive iterations via
    ``since_id`` so a message that arrives after one inner wait times
    out (and before the next begins) is still delivered on the next
    iteration. The final ``cursor`` is also surfaced in the response so
    callers can chain successive ``wait_loop`` invocations across tool
    boundaries without reopening the same race at the outer layer
    (issue #1995).

    Request:
        Same as :func:`message_wait` plus:
        max_iterations (int): safety cap on outer iterations.  ``None``
            or non-positive means effectively unbounded (``sys.maxsize``),
            matching the CLI's "loops forever by default" contract.

    Response:
        { ok: True, matched: True, messages, cursor, iterations, ... }
        Or — if the safety cap trips without a match —
        { ok: True, matched: False, cursor, iterations: <cap>, ... }.

    Raises:
        HandlerError / GatewayError on permanent failure (4xx non-408).
    """
    import sys as _sys

    max_iter = req.get("max_iterations")
    if not isinstance(max_iter, int) or max_iter <= 0:
        max_iter = _sys.maxsize

    # Sleep hook is overridable so tests can skip real sleeps.
    sleep = req.get("_sleep", _time.sleep)

    backoff = 1.0
    # The legacy ``_emit_heartbeat`` / ``_heartbeat_interval`` /
    # ``_start_heartbeat`` request overrides were the test hooks for the
    # agent-side heartbeat that slice-4 task-4-2 deleted. Strip them
    # from the inner ``message_wait`` payload so older tests that still
    # pass them don't end up with the hooks leaking through to the
    # wait endpoint (which would 400 on unknown query params).
    inner = {
        k: v
        for k, v in req.items()
        if k
        not in {
            "max_iterations",
            "_sleep",
            "_emit_heartbeat",
            "_heartbeat_interval",
            "_start_heartbeat",
        }
    }
    last_resp: dict[str, Any] = {}
    # Issue #2464 follow-up: the staleness flag from the *server* only
    # appears on iterations whose ``inner["since"]`` is non-None. Once
    # iteration 1 sees ``since_id_stale: True`` we drop ``inner["since"]``,
    # so iteration 2+ have nothing for the server to flag and the response
    # comes back with no flag (or false). That left the CLI's
    # ``if resp.get("since_id_stale"): _delete_cursor_file`` branch
    # depending on the *last* iteration's flag — which would miss the
    # safety-cap exit case where iteration 1 saw staleness but the loop
    # exhausted ``--max-iterations`` before matching. Track "did the loop
    # ever observe staleness" and propagate it on the final response so
    # the CLI's unlink branch fires regardless of which iteration tripped
    # it.
    loop_saw_stale = False
    for i in range(1, max_iter + 1):
        try:
            resp = message_wait(inner)
        except GatewayError as err:
            status = err.status_code
            # 4xx (non-408) is permanent: callers must not retry.
            if status is not None and 400 <= status < 500 and status != 408:
                raise
            # Transient: sleep and retry.
            sleep(min(backoff, 5.0))
            backoff = min(backoff * 2, 5.0)
            continue
        last_resp = resp
        # Issue #2464: if the server flagged the previous ``since``
        # as unresolvable (post-phase-clear cursor) drop it before
        # threading the new cursor — otherwise a server-side tip of
        # ``None`` would let the dead cursor live another iteration.
        if resp.get("since_id_stale"):
            loop_saw_stale = True
            inner.pop("since", None)
        # Thread the server cursor into the next wait's ``since`` so
        # events that arrive between this response and the next call
        # can't slip through the gap (issue #1995). A cursor of ``None``
        # (empty stream) leaves ``inner["since"]`` unchanged so we keep
        # whatever cursor the caller originally passed in, if any.
        next_cursor = resp.get("cursor")
        if next_cursor is not None:
            inner["since"] = next_cursor
        if resp.get("matched"):
            resp_out = dict(resp)
            resp_out["iterations"] = i
            if loop_saw_stale:
                resp_out["since_id_stale"] = True
            return resp_out
        # Timeout with no match — reset backoff and loop.
        backoff = 1.0

    capped = dict(last_resp)
    capped.setdefault("ok", True)
    capped["matched"] = False
    capped["iterations"] = max_iter
    if loop_saw_stale:
        capped["since_id_stale"] = True
    return capped


def message_heartbeat(req: dict[str, Any]) -> dict[str, Any]:
    """Emit a structured HEARTBEAT on the dedicated ``/heartbeat`` endpoint.

    Unlike :func:`progress_heartbeat` (which signals on ``/signal`` with
    ``signal_type=heartbeat``), this posts to the schema-validated,
    per-role-deduped, rate-limited heartbeat endpoint introduced in
    #1897.

    Request:
        state (str): required — one of
            ``WORKING|WAITING_ON_ROLE|WAITING_FOR_EVENT|PROPOSED|IDLE``.
        waiting_on (str): peer role — required when ``state`` is
            ``WAITING_ON_ROLE``.
        since (str): optional ISO-8601 / epoch timestamp of state entry.
        body (str): optional free-form body.
        pipeline_id, role: overrides.

    Response:
        { ok: True, role, state, deduped: bool, signal: <raw response> }

    Raises:
        HandlerError: invalid ``state`` or missing ``waiting_on``.
        GatewayError: HTTP failure.  ``status_code=429`` is a rate-limit
            hit — the CLI shim surfaces this as a permanent error (rc=3)
            per #1897's contract.
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)
    state = req.get("state")
    if state not in _HEARTBEAT_STATES:
        raise HandlerError(
            f"invalid 'state'={state!r}; expected one of {sorted(_HEARTBEAT_STATES)}"
        )
    waiting_on = req.get("waiting_on")
    if state == "WAITING_ON_ROLE" and not waiting_on:
        raise HandlerError("'state'='WAITING_ON_ROLE' requires 'waiting_on' (peer role)")

    body: dict[str, Any] = {
        "from_role": role,
        "state": state,
    }
    if waiting_on:
        body["waiting_on"] = waiting_on
    if req.get("since"):
        body["since"] = req["since"]
    if req.get("body"):
        body["body"] = req["body"]
    _maybe_attach_slice_id(req, body)

    result = orchestrator_request(
        f"/api/v1/pipelines/{pid}/heartbeat",
        method="POST",
        data=body,
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "heartbeat failed"))
    deduped = bool(result.get("data", {}).get("deduped"))
    return {
        "ok": True,
        "role": role,
        "state": state,
        "deduped": deduped,
        "signal": result,
    }
