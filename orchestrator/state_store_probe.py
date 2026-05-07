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
from collections.abc import Callable
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


def probe_state_store_at(
    base_path: Path,
) -> tuple[bool, str, dict[str, dict[str, str]]]:
    """Probe whether the state-store worktree(s) under ``base_path`` are loadable.

    Pure function — no Flask request context required. Walks the path
    via :func:`state_store.discover_repo_paths` and accesses
    ``store.worktree`` on each. **All** repos are probed every call —
    a wedge on repo A no longer hides an independent wedge on repo B
    (#2176). The curative ``_ensure_worktree`` self-heal still runs as a
    side effect on each repo, so every wedged repo gets a heal attempt
    per probe interval.

    Returns:
        ``(healthy, summary, repos)``.

        - ``healthy`` is True when every probed repo loaded cleanly (or
          the probe was skipped — no base_path, no repos discovered).
        - ``summary`` is a short human-readable aggregate ("ok",
          "probe-skipped: ...", or "N/M repos wedged: a, b").
        - ``repos`` maps each probed repo path (str) to
          ``{"status": "ok"} | {"status": "error", "error": "..."}``.
          Empty when the probe was skipped.
    """
    from state_store import discover_repo_paths, get_state_store

    if not base_path.exists():
        return True, f"probe-skipped: base_path does not exist: {base_path}", {}

    if (base_path / ".git").exists():
        repos = [base_path]
    else:
        repos = list(discover_repo_paths(base_path))
        if not repos:
            return True, "probe-skipped: no git repos under base_path", {}

    results: dict[str, dict[str, str]] = {}
    failures: list[str] = []
    for repo_path in repos:
        try:
            store = get_state_store(repo_path)
            _ = store.worktree
            results[str(repo_path)] = {"status": "ok"}
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            results[str(repo_path)] = {"status": "error", "error": error_msg}
            failures.append(str(repo_path))

    if not failures:
        return True, "ok", results

    summary = f"{len(failures)}/{len(repos)} repos wedged: " + ", ".join(failures)
    return False, summary, results


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
        on_observation: Callable[[bool], None] | None = None,
    ) -> None:
        self._interval = interval
        self._stale_multiplier = stale_multiplier
        self._lock = threading.Lock()
        self._healthy: bool | None = None
        self._message: str = "starting"
        self._repos: dict[str, dict[str, str]] = {}
        self._last_check_monotonic: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._probe_in_flight = False
        self._probe_started_at_monotonic: float | None = None
        self._on_observation = on_observation

    @property
    def interval(self) -> float:
        return self._interval

    def set_on_observation(self, callback: Callable[[bool], None] | None) -> None:
        """Install/replace the per-observation callback.

        The callback runs after every cache update with the latest
        ``healthy`` bool. Used by the orchestrator to drive
        ``routes.health._health_tracker`` at BG-thread cadence so
        ``healthy_since`` / ``recent_transitions`` reflect every wedge
        cycle, not just events between sporadic ``/api/v1/health``
        hits (#2191 review). Setter avoids a route → probe import cycle.
        """
        with self._lock:
            self._on_observation = callback

    def probe_now(self) -> tuple[bool, str]:
        """Run a single probe synchronously and update the cache.

        Useful for priming the cache at startup (so the first
        ``/api/v1/ready`` after boot doesn't return 503) and for tests.
        Exceptions in the underlying probe are caught and surface as a
        cached unhealthy result rather than propagating.

        Concurrent callers short-circuit: the second caller sees the
        in-flight flag and returns the existing cache without launching
        a duplicate probe (avoids the lock-order race called out in
        the #2191 review).
        """
        with self._lock:
            if self._probe_in_flight:
                # Short-circuit: another caller is already probing.
                # Return the most recent cached result rather than
                # racing on the cache write.
                healthy = bool(self._healthy) if self._healthy is not None else False
                return healthy, self._message
            self._probe_in_flight = True
            self._probe_started_at_monotonic = time.monotonic()

        try:
            base_path = _resolve_base_path()
            repos: dict[str, dict[str, str]] = {}
            if base_path is None:
                healthy, message = True, "probe-skipped: EGG_REPO_PATH not set"
            else:
                try:
                    healthy, message, repos = probe_state_store_at(base_path)
                except Exception as exc:  # pragma: no cover — defensive
                    healthy = False
                    message = f"probe-error: {type(exc).__name__}: {exc}"
                    repos = {}
                    logger.warning("State-store probe raised", error=str(exc), exc_info=True)

            with self._lock:
                self._healthy = healthy
                self._message = message
                self._repos = repos
                self._last_check_monotonic = time.monotonic()
                callback = self._on_observation
        finally:
            with self._lock:
                self._probe_in_flight = False
                self._probe_started_at_monotonic = None

        if callback is not None:
            try:
                callback(healthy)
            except Exception:
                logger.exception("State-store probe on_observation callback failed")
        return healthy, message

    def snapshot(self) -> dict[str, Any]:
        """Return the current cached probe result.

        Returns a dict with:

        - ``healthy``: bool — most recent observation, forced to ``False``
          if the cache is stale.
        - ``message``: str — human-readable aggregate probe message.
        - ``repos``: dict[str, dict] — per-repo probe results keyed by
          path. Each value is ``{"status": "ok"}`` or
          ``{"status": "error", "error": "..."}``. Empty when the probe
          was skipped or has not yet run (#2176).
        - ``fresh``: bool — whether the most recent probe is within the
          staleness window (``interval * stale_multiplier``). When a
          probe is currently in flight the window is extended for the
          duration of that probe (up to one additional staleness window),
          so a single slow-but-completing probe doesn't trigger a
          spurious unhealthy flip on the request path while the BG
          callback later records healthy from the same probe (#2501).
          A wedged probe (in-flight indefinitely) still surfaces as
          stale once its in-flight age exceeds the staleness window.
          Worst-case wedge detection latency is therefore bounded at
          ~``2 * stale_window`` after the last good probe (one window
          for the in-flight grace plus one for the existing cache age),
          versus ~``stale_window`` before #2501. With the 30s default
          this is ~60s of blindness in the pathological case — the
          documented trade-off for eliminating the dual-write flap.
          During the grace, ``fresh=True`` is reported even though
          ``age_seconds`` may exceed ``stale_window``; operators
          inspecting ``/api/v1/health`` mid-grace will see a
          "fresh-but-old" response, which is intentional.
        - ``age_seconds``: float | None — seconds since the most recent
          probe, ``None`` if the BG thread has not run yet.
        """
        with self._lock:
            healthy = self._healthy
            message = self._message
            repos = dict(self._repos)
            last = self._last_check_monotonic
            in_flight = self._probe_in_flight
            started = self._probe_started_at_monotonic

        if last is None:
            return {
                "healthy": False,
                "message": message,
                "repos": repos,
                "fresh": False,
                "age_seconds": None,
            }

        now = time.monotonic()
        age = now - last
        stale_window = self._interval * self._stale_multiplier
        fresh = age <= stale_window
        if not fresh and in_flight and started is not None:
            # Don't penalize the cache for a probe that's still in
            # progress: the BG callback will record the result from this
            # same probe shortly (this is "fix #1" of the #2501 dual-
            # write race — addressing the symptom on the snapshot side
            # so the request path doesn't observe a transient unhealthy
            # state from the same probe the BG thread will record as
            # healthy seconds later). Bound the grace by the staleness
            # window so a wedged probe (in-flight forever) still flips
            # stale — without this bound the cache would become a
            # permanent lie on a real wedge.
            in_flight_age = now - started
            if in_flight_age <= stale_window:
                fresh = True
        return {
            "healthy": bool(healthy) if fresh else False,
            "message": message if fresh else f"stale (age={age:.1f}s): {message}",
            "repos": repos,
            "fresh": fresh,
            "age_seconds": age,
        }

    def start(self) -> None:
        """Start the BG thread. Idempotent and atomic."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="state-store-probe"
            )
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
    """Return the process-wide :class:`StateStoreProbe` singleton.

    The interval is read from ``EGG_ORCH_STATE_STORE_PROBE_INTERVAL``
    on first construction (via :mod:`env_config`); subsequent calls
    return the same instance regardless of env-var changes.
    """
    global _PROBE
    with _PROBE_LOCK:
        if _PROBE is None:
            try:
                from env_config import get_state_store_probe_interval

                interval = get_state_store_probe_interval()
            except ImportError:  # pragma: no cover — defensive
                interval = DEFAULT_PROBE_INTERVAL_SECONDS
            _PROBE = StateStoreProbe(interval=interval)
        return _PROBE


def reset_state_store_probe_for_test() -> None:
    """Test hook: drop the singleton so the next ``get_state_store_probe``
    call returns a fresh instance. Should not be used outside tests."""
    global _PROBE
    with _PROBE_LOCK:
        if _PROBE is not None:
            _PROBE.stop(timeout=1.0)
        _PROBE = None
