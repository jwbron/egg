"""Process-wide admission control for implement-phase slice spawns (#2241 gap 1).

The :class:`SliceScheduler` caps slices **per pipeline** at
``EGG_ORCH_MAX_PARALLEL_SLICES``. That cap does not bound the
**total** slice count across all running pipelines in the
orchestrator process — two pipelines kicked off via ``submit_task``
can each fan out their wave concurrently and exceed the host's safe
budget (operationally observed at ~4 slices, each spawning ~8
containers).

This module provides a process-singleton admission counter that
the implement-phase run loop calls before every slice spawn:

  - :func:`try_admit` is non-blocking (matches the run loop's
    poll-every-5s cadence). On success the slice is admitted and
    ``mark_spawned`` proceeds. On failure the slice stays READY
    and re-yields next tick.
  - :func:`release` is idempotent — duplicate calls (e.g. from a
    ``finally`` block plus a ``record_failure`` codepath) are safe.
  - :func:`snapshot` returns the (cap, admitted, waiting) view for
    operator-facing diagnostics.

The admit cap is read lazily from ``orchestrator.env_config`` so
unit tests can monkey-patch the env var. Tests can also call
:func:`reset_for_testing` between cases.

This is an in-process counter. Under HA replicas the semaphore
under-counts globally — see ``docs/architecture/slice-dag.md`` for
the operator-facing caveat.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("orchestrator.global_slice_admit")


class _GlobalSliceAdmit:
    """Process-singleton admission counter for slice spawns.

    Implementation note: a manual counter + ``threading.Lock`` is
    used instead of ``threading.BoundedSemaphore`` so duplicate
    releases don't raise ``ValueError`` (the failure mode would
    crash worker threads). Idempotency is enforced by tracking
    admitted ``(pipeline_id, slice_id)`` keys in a set.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._admitted: set[tuple[str, str]] = set()
        # ``_cap_cache`` lets tests force-resolve the cap; ``None``
        # means "read fresh from env_config on next try_admit".
        self._cap_override: int | None = None

    def _resolve_cap(self) -> int:
        if self._cap_override is not None:
            return self._cap_override
        try:
            from orchestrator import env_config

            return env_config.get_global_max_parallel_slices()
        except Exception:  # noqa: BLE001 — fall back to literal default
            return 4

    def try_admit(self, pipeline_id: str, slice_id: str) -> bool:
        """Admit one slice if the global cap has headroom.

        Idempotent: re-admitting the same ``(pipeline_id, slice_id)``
        key returns ``True`` without consuming an extra slot. This
        mirrors the run loop's "yielding without spawning is safe"
        contract on :meth:`SliceScheduler.iter_ready`.
        """
        key = (pipeline_id, slice_id)
        with self._lock:
            if key in self._admitted:
                return True
            cap = self._resolve_cap()
            if len(self._admitted) >= cap:
                logger.info(
                    "Global slice admit deferred",
                    extra={
                        "pipeline_id": pipeline_id,
                        "slice_id": slice_id,
                        "admitted": len(self._admitted),
                        "cap": cap,
                    },
                )
                return False
            self._admitted.add(key)
            logger.info(
                "Global slice admit granted",
                extra={
                    "pipeline_id": pipeline_id,
                    "slice_id": slice_id,
                    "admitted": len(self._admitted),
                    "cap": cap,
                },
            )
            return True

    def release(self, pipeline_id: str, slice_id: str) -> None:
        """Release a slice's admission slot.

        Idempotent — safe to call from a worker's ``finally`` block
        plus a separate failure codepath. A release with no prior
        admission is a no-op (logged at DEBUG so leaks remain
        grep-able without spamming on cold startup).
        """
        key = (pipeline_id, slice_id)
        with self._lock:
            if key not in self._admitted:
                logger.debug(
                    "Global slice admit release ignored (not admitted)",
                    extra={"pipeline_id": pipeline_id, "slice_id": slice_id},
                )
                return
            self._admitted.discard(key)
            logger.info(
                "Global slice admit released",
                extra={
                    "pipeline_id": pipeline_id,
                    "slice_id": slice_id,
                    "admitted": len(self._admitted),
                },
            )

    def snapshot(self) -> dict[str, object]:
        """Return ``{cap, admitted, admitted_keys}`` for diagnostics.

        ``admitted_keys`` is a list of ``"<pipeline>/<slice>"`` strings
        — primarily for the pipeline-status endpoint so operators can
        see which slices currently hold the budget.
        """
        with self._lock:
            cap = self._resolve_cap()
            keys = sorted(f"{p}/{s}" for p, s in self._admitted)
            return {
                "cap": cap,
                "admitted": len(self._admitted),
                "admitted_keys": keys,
            }

    # --- testing hooks ---

    def _force_cap(self, cap: int | None) -> None:
        """Override the resolved cap (tests only)."""
        with self._lock:
            self._cap_override = cap

    def _reset(self) -> None:
        """Drop all admissions and clear the cap override (tests only)."""
        with self._lock:
            self._admitted.clear()
            self._cap_override = None


_singleton = _GlobalSliceAdmit()


def try_admit(pipeline_id: str, slice_id: str) -> bool:
    """Module-level proxy to the process singleton."""
    return _singleton.try_admit(pipeline_id, slice_id)


def release(pipeline_id: str, slice_id: str) -> None:
    """Module-level proxy to the process singleton."""
    _singleton.release(pipeline_id, slice_id)


def snapshot() -> dict[str, object]:
    """Module-level proxy to the process singleton."""
    return _singleton.snapshot()


def reset_for_testing(*, cap: int | None = None) -> None:
    """Reset the singleton between tests; optionally pin the cap."""
    _singleton._reset()
    if cap is not None:
        _singleton._force_cap(cap)


__all__ = (
    "release",
    "reset_for_testing",
    "snapshot",
    "try_admit",
)
