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

import datetime as _datetime
import re
import threading
import time as _time
from collections.abc import Callable
from typing import Any

from egg_agent_tools.handlers._gateway import (
    get_agent_role,
    get_pipeline_id,
    get_slice_id,
    orchestrator_request,
)
from egg_agent_tools.handlers.errors import GatewayError, HandlerError

_HEARTBEAT_STATES = {
    "WORKING",
    "WAITING_ON_ROLE",
    "WAITING_FOR_EVENT",
    "PROPOSED",
    "IDLE",
}

_SLICE_ID_PATTERN = re.compile(r"^slice-[0-9]+$")


def _maybe_attach_slice_id(req: dict[str, Any], data: dict[str, Any]) -> None:
    """Forward ``slice_id`` from the request or env onto the heartbeat body.

    Mirrors ``brc._maybe_attach_slice_id`` and ``progress._maybe_attach_slice_id``
    so the orchestrator's gateway-session fan-out (#2068) can reconstruct
    the slice-scoped container_id (``egg-agent-{pid}-{slice}-{role}``)
    that ``kubernetes_spawner.JOB_NAME_FORMAT_SLICE`` registered. Without
    this, slice-scoped agents emit heartbeats whose fan-out always 404s
    against the gateway because the orchestrator falls back to the
    pipeline-level shape ``egg-agent-{pid}-{role}``. See #2451.
    """
    slice_id = req.get("slice_id") or get_slice_id()
    if not slice_id:
        return
    if not isinstance(slice_id, str) or not _SLICE_ID_PATTERN.fullmatch(slice_id):
        raise HandlerError(f"Invalid slice_id {slice_id!r}: must match 'slice-<N>'")
    data["slice_id"] = slice_id


# Interval between ``WAITING_FOR_EVENT`` keep-alive heartbeats emitted by
# ``message_wait_loop`` while blocked. Needs to be well under the
# overseer's ``heartbeat_threshold`` (120 s default, 600 s during
# implement phase) so a single missed beat doesn't flip the stall
# detector. See issue #2036.
_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS = 60.0


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
    return {
        "ok": True,
        "matched": matched,
        "messages": messages,
        "cursor": cursor,
        "role": role,
        "for_types": for_types,
        "raw": result,
    }


def _default_emit_wait_loop_heartbeat(
    pipeline_id: str | None,
    role: str | None,
    state: str,
    body: str,
    since: str | None = None,
) -> None:
    """Emit a single liveness heartbeat from ``message_wait_loop``.

    Best-effort: failures are swallowed. ``wait_loop`` heartbeats are a
    liveness signal for the overseer (issue #2036) and must never kill
    the wait itself — in particular, 429 rate-limit responses mean the
    overseer already has plenty of beats for this role and the next tick
    will succeed.

    Short-circuits when ``pipeline_id`` or ``role`` is unset: without
    them the server cannot associate the beat with an agent, and the
    heartbeat endpoint would reject the request anyway.

    ``since`` (optional ISO-8601 timestamp) is included in the payload
    only when truthy. ``message_wait_loop`` captures it once at wait
    entry so every periodic ``WAITING_FOR_EVENT`` beat carries the same
    value, letting the overseer read it as a monotonically aging
    "waiting since" rather than a clock that resets each tick.
    """
    if not pipeline_id or not role:
        return
    payload: dict[str, Any] = {
        "from_role": role,
        "state": state,
        "body": body,
    }
    if since:
        payload["since"] = since
    try:
        orchestrator_request(
            f"/api/v1/pipelines/{pipeline_id}/heartbeat",
            method="POST",
            data=payload,
        )
    except GatewayError:
        pass
    except Exception:
        pass


def _start_wait_loop_heartbeat(
    tick: Callable[[], None],
    interval: float,
) -> Callable[[], None]:
    """Emit ``tick()`` immediately, then every ``interval`` seconds.

    Returns a stop callable. Uses a daemon thread so the emitter dies
    with the interpreter if anything pathological happens; the stop
    callable is called from the outer ``finally`` to halt the thread
    promptly after the wait resolves.

    ``interval <= 0`` disables the periodic tick (entry call only) —
    tests use this to avoid real time.sleep.
    """
    tick()
    if interval <= 0:
        return lambda: None
    stop = threading.Event()

    def _run() -> None:
        while not stop.wait(interval):
            try:
                tick()
            except Exception:
                # Never let a heartbeat failure tear down the thread —
                # the next iteration will try again.
                pass

    t = threading.Thread(target=_run, daemon=True, name="wait_loop_heartbeat")
    t.start()
    return stop.set


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

    # Heartbeat emission (issue #2036). The overseer's stall detector
    # treats "no heartbeat for N seconds" as a liveness signal, but
    # agents blocked in ``wait_loop`` were sending none — so reviewers
    # and downstream producers routinely tripped false-positive stall
    # alerts. Emit ``WAITING_FOR_EVENT`` on entry, every
    # ``_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS`` thereafter, and a final
    # ``WORKING`` on exit so liveness tracks protocol reality.
    pipeline_id_hb = req.get("pipeline_id") or get_pipeline_id()
    role_hb = _role_or_env(req)
    for_types_hb = _coerce_for_types(req)
    from_role_hb = req.get("from_role") or req.get("from")
    emit_hb: Callable[..., None] = req.get("_emit_heartbeat", _default_emit_wait_loop_heartbeat)
    hb_interval = float(req.get("_heartbeat_interval", _WAIT_LOOP_HEARTBEAT_INTERVAL_SECS))
    start_hb: Callable[[Callable[[], None], float], Callable[[], None]] = req.get(
        "_start_heartbeat", _start_wait_loop_heartbeat
    )

    waiting_body = "wait_loop blocked on " + ",".join(for_types_hb)
    if from_role_hb:
        waiting_body += f" from={from_role_hb}"
    # Captured once so every WAITING_FOR_EVENT beat carries the same
    # ``since``: the overseer (and humans tailing the bus) can read it
    # as a monotonically aging "waiting since" rather than a clock that
    # resets every interval. Reviewer suggestion on PR #2041.
    wait_since = _datetime.datetime.now(_datetime.UTC).isoformat()

    def _tick() -> None:
        emit_hb(pipeline_id_hb, role_hb, "WAITING_FOR_EVENT", waiting_body, wait_since)

    stop_hb = start_hb(_tick, hb_interval)

    backoff = 1.0
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
    try:
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
                return resp_out
            # Timeout with no match — reset backoff and loop.
            backoff = 1.0

        capped = dict(last_resp)
        capped.setdefault("ok", True)
        capped["matched"] = False
        capped["iterations"] = max_iter
        return capped
    finally:
        stop_hb()
        # Final transition back to WORKING so the overseer sees the
        # agent leave the wait cleanly. Best-effort; dedup will still
        # collapse a follow-on manual WORKING beat from the caller.
        emit_hb(pipeline_id_hb, role_hb, "WORKING", "wait_loop exited")


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
