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

    def _resolve(self, path: str) -> tuple[int, dict[str, Any]]:
        """Compute the (status, body) tuple for a probe request.

        Centralises the GET/HEAD branching so both verbs see the same
        status, the same defensive 503-on-cache-exception path, and the
        same logging. Per RFC 7231 §4.3.2 a HEAD response MUST report
        the same headers (including Content-Length) it would have sent
        on a GET, which means HEAD has to compute the body too — it just
        does not write it.
        """
        if path == LIVE_PATH:
            return live_payload()
        if path == READY_PATH:
            try:
                return ready_payload()
            except Exception as exc:
                # Defensive: a probe-cache exception must not take the
                # listener down. Log with the full traceback so operators
                # can debug after the fact, and return 503 so the kubelet
                # pulls the pod from rotation but does not restart it.
                logger.exception("Probe ready handler raised")
                return 503, {"ready": False, "error": str(exc)}
        return 404, {"error": "not found", "path": path}

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        path = self.path.split("?", 1)[0]
        status, body = self._resolve(path)
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib signature
        # Per RFC 7231 §4.3.2 the server SHOULD send the same header
        # fields in response to a HEAD request as it would have sent
        # on an equivalent GET — including Content-Length. We compute
        # the body the same way GET does and emit its byte length
        # without writing the body itself.
        path = self.path.split("?", 1)[0]
        status, body = self._resolve(path)
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        # No body on HEAD.


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
        """Stop the server and join the thread. Used by tests.

        Holds ``self._lock`` for the entire shutdown so a concurrent
        ``start()`` cannot observe ``self._server is None`` mid-shutdown
        and create a fresh server while the old one is still releasing
        the listening socket — which would race two listeners onto the
        same port. ``serve_forever()`` runs without the lock (it loops
        on a separate thread), so blocking ``shutdown()``/``join()``
        cannot self-deadlock against the lock the request handler
        thread doesn't take.
        """
        with self._lock:
            server = self._server
            thread = self._thread
            if server is not None:
                server.shutdown()
                server.server_close()
            if thread is not None:
                thread.join(timeout=timeout)
            self._server = None
            self._thread = None


_LISTENER: ProbeListener | None = None
_LISTENER_LOCK = threading.Lock()


def start_probe_listener(port: int, host: str = "0.0.0.0") -> ProbeListener:
    """Start the process-wide probe listener. Idempotent.

    Subsequent calls return the existing instance — useful when the
    serve loop is restarted in-process during tests, but production
    only calls this once from :func:`orchestrator.cli.cmd_serve`.

    If a second call passes ``host``/``port`` arguments that disagree
    with the existing instance, the mismatch is logged at WARNING but
    the existing instance is still returned. The caller does NOT get
    a listener bound to the new port — the singleton is bound to its
    original address. This usually indicates a misconfiguration where
    two startup paths are racing to register different ports; the warn
    surfaces it instead of silently dropping the second config.
    """
    global _LISTENER
    with _LISTENER_LOCK:
        if _LISTENER is None:
            _LISTENER = ProbeListener(host=host, port=port)
            _LISTENER.start()
        else:
            existing_host, existing_port = _LISTENER.address
            if (host, port) != (existing_host, existing_port):
                logger.warning(
                    "start_probe_listener called with mismatched address; "
                    "existing singleton retained",
                    requested_host=host,
                    requested_port=port,
                    existing_host=existing_host,
                    existing_port=existing_port,
                )
        return _LISTENER


def reset_probe_listener_for_test() -> None:
    """Test hook: stop and drop the singleton."""
    global _LISTENER
    with _LISTENER_LOCK:
        if _LISTENER is not None:
            _LISTENER.stop(timeout=1.0)
        _LISTENER = None
