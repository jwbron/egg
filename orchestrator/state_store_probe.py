"""
Background state-store probe for the orchestrator.

Decouples the curative state-store self-heal (introduced in #2167) from
kubelet probe traffic. A single daemon thread runs ``probe_state_store_at``
on a fixed cadence and caches the result; ``/api/v1/health``,
``/api/v1/ready`` and ``/api/v1/live`` read the cache instead of running
the probe inline. This keeps probe-path latency O(dict-read) regardless
of waitress thread-pool pressure (#2191).

The probe still has the curative side effect — ``_ensure_worktree`` will
``shutil.rmtree`` a stale admin dir and retry ``git worktree add`` when
it detects a wedge — but that work now happens on its own cadence,
independent of how often the kubelet polls.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover
    import logging

    def get_logger(name: str, **_: Any) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.state_store_probe")

DEFAULT_PROBE_INTERVAL_SECONDS = 15.0
DEFAULT_STALE_MULTIPLIER = 2.0


def probe_state_store_at(base_path: Path) -> tuple[bool, str]:
    """Probe whether the state-store worktree(s) under ``base_path`` are loadable.

    Pure function — no Flask request context required. Walks the path
    via :func:`state_store.discover_repo_paths` and accesses
    ``store.worktree`` on each. Reports degraded if any probe raises.

    Returns:
        ``(healthy, message)``. ``message`` is ``"ok"`` on success or a
        human-readable error string (``"<ExceptionType>: <message>"``)
        on failure. The same curative side effect documented on
        :class:`state_store.StateStore` applies — wedged repos may be
        self-healed as a side effect of this call.
    """
    from state_store import discover_repo_paths, get_state_store

    if not base_path.exists():
        return True, f"probe-skipped: base_path does not exist: {base_path}"

    if (base_path / ".git").exists():
        repos = [base_path]
    else:
        repos = list(discover_repo_paths(base_path))
        if not repos:
            return True, "probe-skipped: no git repos under base_path"

    for repo_path in repos:
        try:
            store = get_state_store(repo_path)
            _ = store.worktree
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    return True, "ok"


def _resolve_base_path() -> Path | None:
    """Resolve ``EGG_REPO_PATH`` for the BG thread (no request context)."""
    raw = os.environ.get("EGG_REPO_PATH", "").strip()
    if not raw:
        return None
    return Path(raw)


class StateStoreProbe:
    """Daemon-thread driver that periodically probes the state store.

    Holds a thread-safe cache of the most recent probe result. Consumers
    call :meth:`snapshot` to read the cache without blocking on I/O.

    The cache also exposes a freshness flag: if the most recent probe is
    older than ``interval * stale_multiplier``, ``snapshot()`` reports
    ``fresh=False`` and ``healthy=False`` regardless of the last
    observation. That covers the case where the probe thread itself
    wedges (e.g., on a hung ``git`` subprocess) — readiness flips so
    kubelet routes traffic away.
    """

    def __init__(
        self,
        *,
        interval: float = DEFAULT_PROBE_INTERVAL_SECONDS,
        stale_multiplier: float = DEFAULT_STALE_MULTIPLIER,
    ) -> None:
        self._interval = interval
        self._stale_multiplier = stale_multiplier
        self._lock = threading.Lock()
        self._healthy: bool | None = None
        self._message: str = "starting"
        self._last_check_monotonic: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def interval(self) -> float:
        return self._interval

    def probe_now(self) -> tuple[bool, str]:
        """Run a single probe synchronously and update the cache.

        Useful for priming the cache at startup (so the first
        ``/api/v1/ready`` after boot doesn't return 503) and for tests.
        Exceptions in the underlying probe are caught and surface as a
        cached unhealthy result rather than propagating.
        """
        base_path = _resolve_base_path()
        if base_path is None:
            healthy, message = True, "probe-skipped: EGG_REPO_PATH not set"
        else:
            try:
                healthy, message = probe_state_store_at(base_path)
            except Exception as exc:  # pragma: no cover — defensive
                healthy = False
                message = f"probe-error: {type(exc).__name__}: {exc}"
                logger.warning("State-store probe raised", error=str(exc), exc_info=True)

        with self._lock:
            self._healthy = healthy
            self._message = message
            self._last_check_monotonic = time.monotonic()
        return healthy, message

    def snapshot(self) -> dict[str, Any]:
        """Return the current cached probe result.

        Returns a dict with:

        - ``healthy``: bool — most recent observation, forced to ``False``
          if the cache is stale.
        - ``message``: str — human-readable probe message.
        - ``fresh``: bool — whether the most recent probe is within the
          staleness window (``interval * stale_multiplier``).
        - ``age_seconds``: float | None — seconds since the most recent
          probe, ``None`` if the BG thread has not run yet.
        """
        with self._lock:
            healthy = self._healthy
            message = self._message
            last = self._last_check_monotonic

        if last is None:
            return {
                "healthy": False,
                "message": message,
                "fresh": False,
                "age_seconds": None,
            }

        age = time.monotonic() - last
        fresh = age <= self._interval * self._stale_multiplier
        return {
            "healthy": bool(healthy) if fresh else False,
            "message": message if fresh else f"stale (age={age:.1f}s): {message}",
            "fresh": fresh,
            "age_seconds": age,
        }

    def start(self) -> None:
        """Start the BG thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="state-store-probe")
        self._thread.start()
        logger.info(
            "State-store probe started",
            interval_seconds=self._interval,
            stale_multiplier=self._stale_multiplier,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the BG thread to exit and wait briefly for it to join."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        # Run an initial probe immediately so the cache is populated for
        # the first /api/v1/ready hit.
        try:
            self.probe_now()
        except Exception:  # pragma: no cover — probe_now catches its own
            logger.exception("State-store probe initial run failed")

        while not self._stop_event.wait(self._interval):
            try:
                self.probe_now()
            except Exception:  # pragma: no cover
                logger.exception("State-store probe iteration failed")


# Module-level singleton. Tests construct fresh instances directly.
_PROBE: StateStoreProbe | None = None
_PROBE_LOCK = threading.Lock()


def get_state_store_probe() -> StateStoreProbe:
    """Return the process-wide :class:`StateStoreProbe` singleton."""
    global _PROBE
    with _PROBE_LOCK:
        if _PROBE is None:
            _PROBE = StateStoreProbe()
        return _PROBE


def reset_state_store_probe_for_test() -> None:
    """Test hook: drop the singleton so the next ``get_state_store_probe``
    call returns a fresh instance. Should not be used outside tests."""
    global _PROBE
    with _PROBE_LOCK:
        if _PROBE is not None:
            _PROBE.stop(timeout=1.0)
        _PROBE = None
