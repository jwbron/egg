"""status-wait cursor + host-wait tracking helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _track_host_wait_start() -> None:
    if _pkg._inflight_host_waits is not None:
        try:
            _pkg._inflight_host_waits.inc()
        except Exception:  # pragma: no cover
            pass


def _track_host_wait_end() -> None:
    if _pkg._inflight_host_waits is not None:
        try:
            _pkg._inflight_host_waits.dec()
        except Exception:  # pragma: no cover
            pass


def _parse_status_wait_cursor(
    raw: str | None,
) -> tuple[bool, str | None, int | None]:
    """Parse a ``/status/wait`` cursor.

    Returns ``(ok, msg_since_id, event_since_seq)`` where either half
    may be ``None`` (meaning "snap to tip on this source").  ``ok``
    is False only for a syntactically malformed cursor — the route
    returns 400 in that case.  An empty / missing cursor is treated
    as "snap to tip on both sources" (``ok=True, None, None``).
    """
    if raw is None or raw == "":
        return True, None, None
    match = _pkg._STATUS_WAIT_CURSOR_RE.match(raw)
    if not match:
        return False, None, None
    msg_part = match.group(1)
    evt_part = match.group(2)
    msg_since_id = msg_part if msg_part else None
    event_since_seq: int | None = None
    if evt_part:
        try:
            event_since_seq = int(evt_part)
        except ValueError:  # pragma: no cover — the regex guarantees digits/-
            event_since_seq = None
    return True, msg_since_id, event_since_seq


def _build_status_wait_cursor(
    msg_tip_id: str | None,
    event_tip_seq: int,
) -> str:
    """Format a cursor for a ``/status/wait`` response.

    Both halves are emitted — the consumer treats empty halves as
    "snap to tip" on the next call, matching ``_parse_status_wait_cursor``.
    """
    msg_part = msg_tip_id or ""
    return f"msg:{msg_part}|evt:{event_tip_seq}"


def _message_store_tip_id(pipeline_id: str) -> str | None:
    """Best-effort read of the message-store tip ID for a pipeline.

    Used to build the initial / terminal cursor when the route
    returns without matching a message.  Returns ``None`` when the
    store has no messages yet — the caller formats this as the
    empty ``msg:`` half of the compound cursor.

    Three distinct conditions all collapse to ``None`` here and
    callers cannot distinguish between them:

    1. **Store import failure** — the message-store module is not
       loadable in this process (test harness without Redis,
       packaging skew). Pre-PR / post-#2464: same behavior.
    2. **Transient ``get_latest_id`` failure** — e.g.,
       :class:`redis.RedisError` from ``XREVRANGE`` on a connection
       blip. ``RedisMessageStore.get_latest_id`` already catches
       this and returns ``None``, so we see "no tip". This conflates
       a transient error with a genuinely empty store; #2464's fix
       at the call site (``_message_store_tip_id() or msg_since_id``
       removal) drops the consumer's cursor on this transient as
       well, which is a small behavioral regression vs. pre-PR
       graceful-degradation behavior. Acceptable in practice
       because transient Redis errors degrade many other paths
       simultaneously, but worth knowing.
    3. **Empty store** — the ``/status/wait`` post-clear case the
       PR is fixing. Returning ``None`` lets the route emit an
       empty ``msg:`` half so the consumer doesn't re-feed the
       dead cursor.
    """
    try:
        store = _pkg._get_message_store()()
    except Exception:  # pragma: no cover — store may not be importable
        return None
    try:
        return store.get_latest_id(pipeline_id)
    except Exception:
        return None


def _build_minimal_status_envelope(
    pipeline: _pkg.Pipeline,
    cursor: str,
) -> dict[str, _pkg.Any]:
    """Compute the small envelope used on both wait paths.

    Ships ``current_phase`` / ``status`` / ``phase_elapsed_seconds``
    so dashboards can refresh cheaply on a timeout without paying
    for a second round-trip.  ``concurrent.consensus`` is also
    included (R5 mitigation from the refine phase) so the host
    does not miss a BRC state change during a quiet interval.
    """
    phase_key = pipeline.current_phase.value if pipeline.current_phase else ""
    phase_data = pipeline.phases.get(phase_key, None)
    envelope: dict[str, _pkg.Any] = {
        "current_phase": phase_key,
        "status": pipeline.status.value if pipeline.status else "",
        "cursor": cursor,
    }
    if phase_data is not None:
        started_at = getattr(phase_data, "started_at", None)
        if started_at:
            try:
                if isinstance(started_at, str):
                    started_dt = _pkg.datetime.fromisoformat(started_at)
                else:
                    started_dt = started_at
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=_pkg.UTC)
                elapsed = int((_pkg.datetime.now(_pkg.UTC) - started_dt).total_seconds())
                envelope["phase_elapsed_seconds"] = max(0, elapsed)
            except ValueError, TypeError, AttributeError:
                pass

    concurrent_data = _pkg._get_concurrent_status(pipeline)
    if concurrent_data and "consensus" in concurrent_data:
        envelope["concurrent"] = {"consensus": concurrent_data["consensus"]}
    return envelope
