"""
Unit tests for :mod:`probe_listener`.

Covers:

- ``/api/v1/live`` always returns 200 with ``{"alive": true}``.
- ``/api/v1/ready`` flips between 200 and 503 based on the cached
  state-store snapshot, mirroring the Flask route's semantics.
- The listener is responsive when waitress would be saturated — the
  whole point of #2414 is that probe latency is *not* coupled to the
  Flask request path, so a synthetic load on Flask routes does not
  affect the probe listener (we model this by simply not running
  Flask at all and confirming the listener answers on its own port).
- HEAD requests are answered with the same status code as GET.
- Unknown paths return 404 instead of throwing.
"""

from __future__ import annotations

import http.client
import socket
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

# Add orchestrator + shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


def _free_port() -> int:
    """Return a currently-unused TCP port. Race-y, but good enough
    for unit tests where ports are bound immediately after."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http_get(port: int, path: str, *, method: str = "GET", timeout: float = 2.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        return resp.status, body
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _reset_state_store_probe():
    """Reset the state-store probe singleton before/after each test so
    that ``ready_payload`` reads from a known empty cache."""
    from state_store_probe import reset_state_store_probe_for_test

    reset_state_store_probe_for_test()
    try:
        yield
    finally:
        reset_state_store_probe_for_test()


@pytest.fixture
def listener():
    """Start a fresh probe listener on a free port for the test, then
    tear it down. Skips the module-level singleton entirely — tests
    construct their own ProbeListener so the singleton stays clean
    for production code paths."""
    from probe_listener import ProbeListener

    instance = ProbeListener(host="127.0.0.1", port=_free_port())
    instance.start()
    try:
        yield instance
    finally:
        instance.stop(timeout=2.0)


class TestLive:
    def test_get_returns_200(self, listener):
        status, body = _http_get(listener.address[1], "/api/v1/live")
        assert status == 200
        assert '"alive": true' in body

    def test_head_returns_200(self, listener):
        status, body = _http_get(listener.address[1], "/api/v1/live", method="HEAD")
        assert status == 200
        # HEAD has no body
        assert body == ""

    def test_does_not_consult_state_store(self, listener):
        """Liveness must not read the state-store cache: kubelet uses
        liveness to decide whether to *restart* the pod, and a
        state-store wedge does not justify a restart (matches the
        Flask route's documented behaviour)."""
        from state_store_probe import get_state_store_probe

        # Force the state-store probe singleton into an unhealthy,
        # not-fresh state. /live should still return 200.
        probe = get_state_store_probe()
        with probe._lock:  # type: ignore[attr-defined]
            probe._healthy = False  # type: ignore[attr-defined]
            probe._message = "wedged"  # type: ignore[attr-defined]
            probe._last_check_monotonic = None  # type: ignore[attr-defined]

        status, _ = _http_get(listener.address[1], "/api/v1/live")
        assert status == 200


class TestReady:
    def test_returns_503_before_first_probe(self, listener):
        """Before the BG state-store probe has run, the cache is empty
        and ``snapshot()`` reports ``fresh=False, healthy=False`` →
        ``/ready`` must be 503 so kubelet pulls the pod from rotation."""
        status, body = _http_get(listener.address[1], "/api/v1/ready")
        assert status == 503
        assert '"ready": false' in body

    def test_returns_200_when_healthy(self, listener):
        """A healthy fresh probe → 200."""
        from state_store_probe import get_state_store_probe

        probe = get_state_store_probe()
        # Run the probe synchronously once. With EGG_REPO_PATH unset
        # the probe records "probe-skipped: EGG_REPO_PATH not set" with
        # healthy=True, fresh=True, which is exactly the
        # state-store-disabled production case.
        probe.probe_now()

        status, body = _http_get(listener.address[1], "/api/v1/ready")
        assert status == 200
        assert '"ready": true' in body

    def test_returns_503_when_cache_stale(self, listener):
        """A wedged BG thread should flip /ready to 503 even if the
        last cached observation was healthy — covered by the staleness
        check in ``StateStoreProbe.snapshot``."""
        from state_store_probe import get_state_store_probe

        probe = get_state_store_probe()
        # Manually populate the cache with a healthy observation, then
        # rewind the timestamp past the staleness window.
        with probe._lock:  # type: ignore[attr-defined]
            probe._healthy = True  # type: ignore[attr-defined]
            probe._message = "ok"  # type: ignore[attr-defined]
            probe._last_check_monotonic = 0.0  # type: ignore[attr-defined]

        status, body = _http_get(listener.address[1], "/api/v1/ready")
        assert status == 503
        assert '"ready": false' in body
        assert "stale" in body  # snapshot prepends "stale (age=...)" to the message

    def test_handler_exception_returns_503(self, listener):
        """A defensive 503 covers the case where the cache read itself
        raises — the listener must stay up rather than crashing the
        kubelet's probe path."""
        with patch(
            "probe_listener.ready_payload",
            side_effect=RuntimeError("boom"),
        ):
            status, body = _http_get(listener.address[1], "/api/v1/ready")
        assert status == 503
        assert '"ready": false' in body
        assert "boom" in body


class TestUnknownPath:
    def test_returns_404(self, listener):
        status, body = _http_get(listener.address[1], "/nope")
        assert status == 404
        assert '"error": "not found"' in body


class TestPortBinding:
    def test_port_default_matches_env_helper(self):
        """The default in ``env_config`` must agree with the comment
        in the k8s manifest, otherwise an operator overriding via the
        env var won't get what they expect."""
        from env_config import DEFAULT_PROBE_LISTENER_PORT, get_probe_listener_port

        assert DEFAULT_PROBE_LISTENER_PORT == 9851
        assert get_probe_listener_port() == 9851

    def test_env_override(self, monkeypatch):
        from env_config import get_probe_listener_port

        monkeypatch.setenv("EGG_ORCH_PROBE_LISTENER_PORT", "9999")
        assert get_probe_listener_port() == 9999

    def test_env_garbage_falls_back(self, monkeypatch):
        from env_config import DEFAULT_PROBE_LISTENER_PORT, get_probe_listener_port

        monkeypatch.setenv("EGG_ORCH_PROBE_LISTENER_PORT", "not-a-port")
        assert get_probe_listener_port() == DEFAULT_PROBE_LISTENER_PORT

    def test_env_out_of_range_falls_back(self, monkeypatch):
        from env_config import DEFAULT_PROBE_LISTENER_PORT, get_probe_listener_port

        monkeypatch.setenv("EGG_ORCH_PROBE_LISTENER_PORT", "70000")
        assert get_probe_listener_port() == DEFAULT_PROBE_LISTENER_PORT


class TestThreadingHTTPServerNonBlocking:
    """The point of the standalone listener is that probe traffic does
    not contend for waitress's worker pool. We can't realistically pin
    waitress saturation in a unit test, but we *can* prove the listener
    handles concurrent requests on its own — i.e., that no internal
    serialisation has been accidentally introduced.
    """

    def test_concurrent_live_requests(self, listener):
        statuses: list[int] = []
        lock = threading.Lock()

        def hit():
            status, _ = _http_get(listener.address[1], "/api/v1/live")
            with lock:
                statuses.append(status)

        threads = [threading.Thread(target=hit) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(statuses) == 16
        assert all(s == 200 for s in statuses)


class TestStartHelper:
    """``start_probe_listener`` is a thin singleton wrapper. Cover the
    idempotent-second-call path so we don't accidentally bind twice."""

    def test_idempotent(self):
        from probe_listener import (
            reset_probe_listener_for_test,
            start_probe_listener,
        )

        port = _free_port()
        try:
            first = start_probe_listener(port=port, host="127.0.0.1")
            second = start_probe_listener(port=port, host="127.0.0.1")
            assert first is second
            # The listener is bound and responsive.
            status, _ = _http_get(port, "/api/v1/live")
            assert status == 200
        finally:
            reset_probe_listener_for_test()
