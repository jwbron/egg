"""``rebuild_and_rollout`` + progress-stream plumbing (#3312).

Kicks off ``make redeploy`` in a background thread, streams its line/phase
events into a per-stream buffer with a retention reaper, and exposes the
buffer via ``rebuild_stream_read``. The mutable rollout/stream state
(``_REBUILD_IN_PROGRESS``, ``_REBUILD_ACTIVE_STREAM_ID``, the ``_STREAM_*``
buffers and locks) is REBOUND by both the route tests (``dep_mod.X = ...``)
and this module, so its single canonical home is the barrel
(``routes.deployment``); every access here goes through ``_pkg`` so the
package attribute the tests read/write is the same object this code mutates.
The thread target is ``_pkg._run_redeploy_subprocess`` so the
``patch("routes.deployment._run_redeploy_subprocess")`` seam stays effective.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import routes.deployment as _pkg
from flask import Response, jsonify, request

from ._runtime import _not_available_on_runtime, _runtime_detection_failed


def _stream_append(stream_id: str, event: dict[str, Any]) -> None:
    with _pkg._STREAM_LOCK:
        buf = _pkg._STREAM_BUFFERS.setdefault(stream_id, deque())
        buf.append(event)


def _stream_mark_done(stream_id: str) -> None:
    import time as _time

    with _pkg._STREAM_LOCK:
        _pkg._STREAM_TERMINATED.add(stream_id)
        _pkg._STREAM_TERMINATION_TS[stream_id] = _time.monotonic()
        _reap_stale_streams_locked()


def _reap_stale_streams_locked() -> None:
    """Evict terminated streams beyond the retention cap. Lock-held."""
    if len(_pkg._STREAM_TERMINATION_TS) <= _pkg._STREAM_RETENTION:
        return
    # Oldest first.
    ordered = sorted(_pkg._STREAM_TERMINATION_TS.items(), key=lambda kv: kv[1])
    overflow = len(ordered) - _pkg._STREAM_RETENTION
    for stream_id, _ts in ordered[:overflow]:
        _pkg._STREAM_BUFFERS.pop(stream_id, None)
        _pkg._STREAM_TERMINATED.discard(stream_id)
        _pkg._STREAM_TERMINATION_TS.pop(stream_id, None)


def _stream_is_done(stream_id: str) -> bool:
    with _pkg._STREAM_LOCK:
        return stream_id in _pkg._STREAM_TERMINATED


def _stream_snapshot(stream_id: str, since: int = 0) -> tuple[list[dict[str, Any]], bool]:
    with _pkg._STREAM_LOCK:
        buf = _pkg._STREAM_BUFFERS.get(stream_id)
        if buf is None:
            return [], stream_id in _pkg._STREAM_TERMINATED
        events = list(buf)[since:]
        done = stream_id in _pkg._STREAM_TERMINATED
        return events, done


def _run_redeploy_subprocess(
    stream_id: str,
    cwd: str,
    *,
    runner: Any = None,
    timeout_sec: int | None = None,
) -> None:
    """Execute ``make redeploy`` and pipe progress events to the stream.

    Emits events of shape::

        {"ts": "<isoformat>", "phase": "line", "line": "..."}

    and terminates with a ``{"phase": "done", "exit_code": N,
    "rolled_out_images": {...}}`` record.

    A watchdog kills the subprocess after *timeout_sec* seconds (default
    :data:`_REDEPLOY_SUBPROCESS_TIMEOUT_SEC`) so a wedged
    ``make redeploy`` never leaves ``_REBUILD_IN_PROGRESS`` pinned true
    — review MEDIUM-1 in #1759.

    *runner* may be overridden for testing — any callable with the
    signature of :func:`subprocess.Popen`.
    """
    from datetime import UTC, datetime

    popen = runner or subprocess.Popen
    effective_timeout = (
        timeout_sec if timeout_sec is not None else _pkg._REDEPLOY_SUBPROCESS_TIMEOUT_SEC
    )
    deadline = time.monotonic() + effective_timeout
    exit_code = -1
    rolled_out: dict[str, str] = {}
    timed_out = False
    proc: Any = None

    def _watchdog() -> None:
        # Sleep until the deadline, then kill the process if still alive.
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        if proc is None or proc.poll() is not None:
            return
        nonlocal timed_out
        timed_out = True
        _stream_append(
            stream_id,
            {
                "ts": datetime.now(UTC).isoformat(),
                "phase": "timeout",
                "message": (f"make redeploy exceeded {effective_timeout}s, killing subprocess"),
            },
        )
        try:
            proc.kill()
        except Exception:  # pragma: no cover - defensive
            pass

    watchdog_thread: threading.Thread | None = None

    try:
        proc = popen(
            ["make", "redeploy"],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None

        watchdog_thread = threading.Thread(
            target=_watchdog,
            daemon=True,
            name=f"rebuild-watchdog-{stream_id}",
        )
        watchdog_thread.start()

        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            _stream_append(
                stream_id,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "phase": "line",
                    "line": line,
                },
            )
            # Detect image import lines for the final summary. Matches
            # lines emitted by `k3s ctr images import`.
            if "unpacking" in line and "sha256:" in line:
                # best-effort: parse "unpacking docker.io/library/egg-xxx:tag"
                parts = line.split()
                for p in parts:
                    if ":" in p and ("egg-" in p or "egg:" in p):
                        rolled_out[p] = "imported"
                        break

        exit_code = proc.wait()
    except Exception as exc:  # pragma: no cover - very defensive
        _stream_append(
            stream_id,
            {
                "ts": datetime.now(UTC).isoformat(),
                "phase": "error",
                "message": str(exc),
            },
        )
    finally:
        _stream_append(
            stream_id,
            {
                "ts": datetime.now(UTC).isoformat(),
                "phase": "done",
                "exit_code": exit_code,
                "rolled_out_images": rolled_out,
                "timed_out": timed_out,
            },
        )
        _stream_mark_done(stream_id)
        with _pkg._REBUILD_LOCK:
            _pkg._REBUILD_IN_PROGRESS = False
            _pkg._REBUILD_ACTIVE_STREAM_ID = None


def rebuild_and_rollout() -> tuple[Response, int]:
    """Kick off ``make redeploy`` asynchronously and return a stream handle.

    Safeties:
    - Gated on ``EGG_RUNTIME=kubernetes`` (docker returns ``not_available_on_runtime``).
    - Refuses with ``runtime_detection_failed`` when the process claims
      kubernetes but can't reach the apiserver — kicking off
      ``make redeploy`` against a nonexistent cluster just wastes cycles
      and produces confusing output (#1850).
    - Rejects concurrent invocations while a rollout is live
      (returns 409 with the existing stream id).
    - Actual subprocess runs in a background thread so the HTTP
      request returns immediately; the MCP tool call stays inside
      FastMCP's ~60 s budget.
    """
    if _pkg._current_runtime() != "kubernetes":
        return _not_available_on_runtime()

    reachable, reason = _pkg._probe_kubernetes_reachable()
    if not reachable:
        return _runtime_detection_failed(reason or "apiserver unreachable")

    cwd = os.environ.get("EGG_REPO_PATH") or "/home/egg/repos/egg"
    if not Path(cwd).exists():
        return (
            jsonify({"success": False, "message": f"EGG_REPO_PATH not found: {cwd}"}),
            500,
        )

    with _pkg._REBUILD_LOCK:
        if _pkg._REBUILD_IN_PROGRESS:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "rollout_already_in_progress",
                        "data": {
                            "error": "rollout_already_in_progress",
                            "progress_stream_id": _pkg._REBUILD_ACTIVE_STREAM_ID,
                        },
                    }
                ),
                409,
            )
        stream_id = uuid.uuid4().hex[:16]
        _pkg._REBUILD_IN_PROGRESS = True
        _pkg._REBUILD_ACTIVE_STREAM_ID = stream_id

    # Start the worker thread. The subprocess runs in the orchestrator
    # container which owns the repo bind mount.
    thread = threading.Thread(
        target=_pkg._run_redeploy_subprocess,
        args=(stream_id, cwd),
        daemon=True,
        name=f"rebuild-{stream_id}",
    )
    thread.start()

    return (
        jsonify(
            {
                "success": True,
                "data": {
                    "progress_stream_id": stream_id,
                    "started_at": time.time(),
                },
            }
        ),
        202,
    )


def rebuild_stream_read(stream_id: str) -> tuple[Response, int]:
    """Return buffered progress events for *stream_id*.

    Query ``since`` (integer index, default 0) lets callers fetch only
    new events.  The ``done`` flag tells the caller whether the worker
    has terminated — useful for the MCP ``wait=true`` mode.
    """
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0

    events, done = _stream_snapshot(stream_id, since=since)
    if not events and not done and stream_id not in _pkg._STREAM_BUFFERS:
        return jsonify({"success": False, "message": "stream not found"}), 404

    return (
        jsonify(
            {
                "success": True,
                "data": {
                    "stream_id": stream_id,
                    "events": events,
                    "next_since": since + len(events),
                    "done": done,
                },
            }
        ),
        200,
    )
