"""
Standalone HTTP listener for kubelet liveness/readiness probes.

The Flask routes at ``/api/v1/live`` and ``/api/v1/ready`` (see
``routes/health.py``) serve the same content, but they share waitress's
worker pool with the rest of the API. Under burst load — heartbeat
fan-in, host-side ``/status/wait`` polls, dozens of agent
``/messages/wait`` long-polls — every waitress thread can be occupied,
and the kubelet's 3 s probe timeout is not enough to clear the queue
even though the probe handler itself is O(dict-read). Three consecutive
misses → kubelet SIGKILL, which is what surfaced in #2413.

#2191 already decoupled the probes from state-store I/O (the BG probe
caches results so handlers never run ``git`` on the request path); this
module finishes the decoupling at the transport layer. The listener
runs in a daemon thread on its own port, so probe latency is bounded
by the Python interpreter's scheduling, not by waitress's worker pool.
A real process wedge (deadlock, OOM, kernel-level death) still trips
the probe — the listener shares the orchestrator's process and crash
domain.

The Flask routes are kept for in-cluster clients (dashboards,
``mcp__egg__check_health``, the orchestrator CLI's ``health`` command)
that come in via the API port.

Public surface:

- :class:`ProbeListener` — encapsulates the server lifecycle.
- :func:`start_probe_listener` — module-level convenience used by
  :func:`orchestrator.cli.cmd_serve`.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Add shared directory to path so ``egg_logging`` resolves the same way as
# the rest of the orchestrator process.
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover — fallback for ad-hoc test contexts
    import logging

    def get_logger(name: str, **_: Any) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.probe_listener")

LIVE_PATH = "/api/v1/live"
READY_PATH = "/api/v1/ready"


def live_payload() -> tuple[int, dict[str, Any]]:
    """Liveness response — pure JSON, no I/O.

    Mirrors the Flask route at ``routes/health.py:liveness_check``: the
    kubelet uses this to decide whether to *restart* the pod, so it must
    not flap on transient subsystem trouble. State-store wedges flip
    readiness via :func:`ready_payload`; a wedge that warrants restart
    has to take the whole process down.
    """
    return 200, {"alive": True}


def ready_payload() -> tuple[int, dict[str, Any]]:
    """Readiness response — reads the cached state-store snapshot.

    Mirrors the Flask route at ``routes/health.py:readiness_check`` so
    the kubelet sees identical semantics regardless of which port the
    probe targets. Returns 503 when the cache is stale (BG probe wedged)
    or the most recent observation was unhealthy.
    """
    # Imported lazily so this module is importable in test contexts that
    # do not bring up the orchestrator's full state-store wiring.
    from state_store_probe import get_state_store_probe

    snap = get_state_store_probe().snapshot()
    ready = bool(snap["healthy"]) and bool(snap["fresh"])
    body = {
        "ready": ready,
        "state_store": snap["repos"],
        "state_store_summary": snap["message"],
        "fresh": snap["fresh"],
        "age_seconds": snap["age_seconds"],
    }
    return (200 if ready else 503), body


class _ProbeHandler(BaseHTTPRequestHandler):
    """Tiny GET-only handler for ``/api/v1/live`` and ``/api/v1/ready``."""

    # Suppress the default per-request stderr line — the orchestrator emits
    # its own structured logs on the API path, and the kubelet probes a few
    # times per pod-second, so the default access log is just noise.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        path = self.path.split("?", 1)[0]
        if path == LIVE_PATH:
            status, body = live_payload()
        elif path == READY_PATH:
            try:
                status, body = ready_payload()
            except Exception as exc:
                # Defensive: a probe-cache exception must not take the
                # listener down. Surface as 503 so the kubelet pulls the
                # pod from rotation but does not restart it.
                logger.warning("Probe ready handler raised", error=str(exc))
                status, body = 503, {"ready": False, "error": str(exc)}
        else:
            status, body = 404, {"error": "not found", "path": path}

        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib signature
        # Some probe configurations issue HEAD; respond with the same
        # status as GET but no body.
        path = self.path.split("?", 1)[0]
        if path == LIVE_PATH:
            status = 200
        elif path == READY_PATH:
            try:
                status, _ = ready_payload()
            except Exception:
                status = 503
        else:
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.end_headers()


class ProbeListener:
    """Daemon-thread driver for the probe HTTP server.

    The server uses :class:`http.server.ThreadingHTTPServer` so each
    request runs on a fresh thread; with two trivial handlers the cost
    of ``threading.Thread`` per request is negligible and concurrent
    kubelet probes (liveness + readiness + startup) can all run without
    queuing.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9851) -> None:
        self._host = host
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def address(self) -> tuple[str, int]:
        return self._host, self._port

    def start(self) -> None:
        """Bind and serve in a daemon thread. Idempotent."""
        with self._lock:
            if self._server is not None:
                return
            self._server = ThreadingHTTPServer((self._host, self._port), _ProbeHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="probe-listener",
                daemon=True,
            )
            self._thread.start()
        logger.info(
            "Probe listener started",
            host=self._host,
            port=self._port,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the server and join the thread. Used by tests."""
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=timeout)


_LISTENER: ProbeListener | None = None
_LISTENER_LOCK = threading.Lock()


def start_probe_listener(port: int, host: str = "0.0.0.0") -> ProbeListener:
    """Start the process-wide probe listener. Idempotent.

    Subsequent calls return the existing instance — useful when the
    serve loop is restarted in-process during tests, but production
    only calls this once from :func:`orchestrator.cli.cmd_serve`.
    """
    global _LISTENER
    with _LISTENER_LOCK:
        if _LISTENER is None:
            _LISTENER = ProbeListener(host=host, port=port)
            _LISTENER.start()
        return _LISTENER


def reset_probe_listener_for_test() -> None:
    """Test hook: stop and drop the singleton."""
    global _LISTENER
    with _LISTENER_LOCK:
        if _LISTENER is not None:
            _LISTENER.stop(timeout=1.0)
        _LISTENER = None
