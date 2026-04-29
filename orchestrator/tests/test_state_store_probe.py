"""
Unit tests for :mod:`state_store_probe`.

Covers the cache lifecycle, watchdog staleness, exception isolation,
and clean shutdown semantics introduced in #2191. Does not exercise
the live state-store probe — that's covered transitively by
``test_state_store_wedge_propagation.py`` and by integration tests
against a real worktree.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add orchestrator + shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


@pytest.fixture(autouse=True)
def _reset_singleton():
    from state_store_probe import reset_state_store_probe_for_test

    reset_state_store_probe_for_test()
    try:
        yield
    finally:
        reset_state_store_probe_for_test()


@pytest.fixture
def with_repo_path(monkeypatch):
    """Set ``EGG_REPO_PATH`` to a sentinel so ``probe_now`` takes the
    probe path (callers patch ``probe_state_store_at`` to control the
    return value). The path itself is never read because the patch
    intercepts before any filesystem access."""
    monkeypatch.setenv("EGG_REPO_PATH", "/sentinel/repo/path")
    yield


class TestCacheLifecycle:
    """The cache starts empty, populates on the first probe, and
    reflects subsequent observations."""

    def test_snapshot_before_first_probe_reports_starting(self):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe()
        snap = probe.snapshot()
        assert snap["healthy"] is False
        assert snap["fresh"] is False
        assert snap["age_seconds"] is None
        assert "starting" in snap["message"]

    def test_probe_now_populates_cache(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe()
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok", {"/sentinel/repo/path": {"status": "ok"}}),
        ):
            probe.probe_now()
        snap = probe.snapshot()
        assert snap["healthy"] is True
        assert snap["fresh"] is True
        assert snap["message"] == "ok"
        assert snap["age_seconds"] is not None
        assert snap["age_seconds"] >= 0

    def test_probe_now_reflects_unhealthy_observation(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe()
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(
                False,
                "1/1 repos wedged: /sentinel/repo/path",
                {
                    "/sentinel/repo/path": {
                        "status": "error",
                        "error": "GitOperationError: wedged",
                    }
                },
            ),
        ):
            probe.probe_now()
        snap = probe.snapshot()
        assert snap["healthy"] is False
        assert "wedged" in snap["message"]
        assert snap["repos"]["/sentinel/repo/path"]["status"] == "error"
        assert "wedged" in snap["repos"]["/sentinel/repo/path"]["error"]

    def test_probe_now_skipped_when_egg_repo_path_unset(self, monkeypatch):
        from state_store_probe import StateStoreProbe

        monkeypatch.delenv("EGG_REPO_PATH", raising=False)
        probe = StateStoreProbe()
        healthy, message = probe.probe_now()
        assert healthy is True
        assert "probe-skipped" in message


class TestWatchdog:
    """If the cache goes stale (``age > interval * stale_multiplier``),
    ``snapshot()`` reports ``healthy=False`` regardless of the last
    observation. Covers the case where the BG thread itself wedges."""

    def test_fresh_observation_within_window(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe(interval=10.0, stale_multiplier=2.0)
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok", {"/sentinel/repo/path": {"status": "ok"}}),
        ):
            probe.probe_now()
        snap = probe.snapshot()
        assert snap["fresh"] is True
        assert snap["healthy"] is True

    def test_stale_observation_flips_healthy_to_false(self, with_repo_path):
        """Drive the cache by hand and advance ``time.monotonic`` past
        the staleness threshold. Healthy observation must be ignored."""
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe(interval=1.0, stale_multiplier=2.0)
        # Manually populate the cache with a "healthy" observation, then
        # rewind ``last_check_monotonic`` so the snapshot sees it as stale.
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok", {"/sentinel/repo/path": {"status": "ok"}}),
        ):
            probe.probe_now()

        # Force the cached timestamp into the past by more than 2*interval.
        with probe._lock:  # type: ignore[attr-defined]
            probe._last_check_monotonic -= 10.0  # type: ignore[attr-defined,operator]

        snap = probe.snapshot()
        assert snap["fresh"] is False
        assert snap["healthy"] is False
        assert "stale" in snap["message"]


class TestExceptionIsolation:
    """Exceptions in the underlying probe must not crash the BG thread
    or propagate to ``probe_now`` callers."""

    def test_probe_now_swallows_underlying_exception(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe()
        with patch(
            "state_store_probe.probe_state_store_at",
            side_effect=RuntimeError("boom"),
        ):
            healthy, message = probe.probe_now()
        assert healthy is False
        assert "probe-error" in message
        assert "boom" in message

    def test_bg_thread_survives_probe_exception(self, monkeypatch):
        """The thread must keep running even after the probe raises —
        otherwise a single transient git failure would silently kill
        self-heal for the rest of the pod's lifetime."""
        from state_store_probe import StateStoreProbe

        # Monkeypatch EGG_REPO_PATH so the probe path is taken.
        monkeypatch.setenv("EGG_REPO_PATH", "/nonexistent/path/probably")

        call_count = {"n": 0}

        def flaky_probe(_path: Path) -> tuple[bool, str, dict[str, dict[str, str]]]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient failure")
            return True, "ok", {str(_path): {"status": "ok"}}

        with patch("state_store_probe.probe_state_store_at", side_effect=flaky_probe):
            probe = StateStoreProbe(interval=0.05)
            probe.start()
            try:
                # Wait long enough for at least 2 ticks (the failing one
                # plus a recovering one).
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if call_count["n"] >= 2:
                        break
                    time.sleep(0.01)
            finally:
                probe.stop(timeout=1.0)

        assert call_count["n"] >= 2, (
            "BG thread did not recover after a probe exception; "
            "this would silently disable state-store self-heal."
        )


class TestThreadLifecycle:
    """``start()`` is idempotent and ``stop()`` shuts the thread down
    cleanly on SIGTERM-equivalent code paths."""

    def test_start_is_idempotent(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe(interval=10.0)
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok", {"/sentinel/repo/path": {"status": "ok"}}),
        ):
            probe.start()
            first_thread = probe._thread  # type: ignore[attr-defined]
            probe.start()  # No-op
            second_thread = probe._thread  # type: ignore[attr-defined]
        try:
            assert first_thread is second_thread
        finally:
            probe.stop(timeout=1.0)

    def test_stop_joins_thread(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe(interval=0.1)
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok", {"/sentinel/repo/path": {"status": "ok"}}),
        ):
            probe.start()
            time.sleep(0.05)
            probe.stop(timeout=1.0)

        thread: threading.Thread | None = probe._thread  # type: ignore[attr-defined]
        assert thread is not None
        assert not thread.is_alive(), "BG thread did not exit after stop(); shutdown is broken."


class TestSingleton:
    """The module-level singleton accessor must return a stable instance
    until reset."""

    def test_get_state_store_probe_returns_same_instance(self):
        from state_store_probe import get_state_store_probe

        a = get_state_store_probe()
        b = get_state_store_probe()
        assert a is b

    def test_reset_drops_singleton(self):
        from state_store_probe import (
            get_state_store_probe,
            reset_state_store_probe_for_test,
        )

        a = get_state_store_probe()
        reset_state_store_probe_for_test()
        b = get_state_store_probe()
        assert a is not b

    def test_singleton_reads_interval_env_var(self, monkeypatch):
        """``EGG_ORCH_STATE_STORE_PROBE_INTERVAL`` overrides the default
        cadence. Operators can tune cadence without a code change
        (#2191 review item 6)."""
        from state_store_probe import (
            get_state_store_probe,
            reset_state_store_probe_for_test,
        )

        monkeypatch.setenv("EGG_ORCH_STATE_STORE_PROBE_INTERVAL", "3.5")
        reset_state_store_probe_for_test()
        probe = get_state_store_probe()
        assert probe.interval == 3.5

    def test_singleton_falls_back_on_invalid_interval(self, monkeypatch):
        """Malformed/non-positive ``EGG_ORCH_STATE_STORE_PROBE_INTERVAL``
        falls back to the default rather than crashing the singleton."""
        from state_store_probe import (
            DEFAULT_PROBE_INTERVAL_SECONDS,
            get_state_store_probe,
            reset_state_store_probe_for_test,
        )

        monkeypatch.setenv("EGG_ORCH_STATE_STORE_PROBE_INTERVAL", "not-a-number")
        reset_state_store_probe_for_test()
        probe = get_state_store_probe()
        assert probe.interval == DEFAULT_PROBE_INTERVAL_SECONDS

        monkeypatch.setenv("EGG_ORCH_STATE_STORE_PROBE_INTERVAL", "-5")
        reset_state_store_probe_for_test()
        probe = get_state_store_probe()
        assert probe.interval == DEFAULT_PROBE_INTERVAL_SECONDS


class TestOnObservationCallback:
    """The on_observation callback fires after every cache update so
    consumers (notably ``routes.health._health_tracker``) see every
    wedge cycle observed by the BG thread, not just events between
    sporadic ``/api/v1/health`` hits (#2191 review item 1)."""

    def test_callback_fires_on_each_probe(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        observations: list[bool] = []
        probe = StateStoreProbe(on_observation=observations.append)

        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok", {"/sentinel/repo/path": {"status": "ok"}}),
        ):
            probe.probe_now()

        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(
                False,
                "1/1 repos wedged: /sentinel/repo/path",
                {
                    "/sentinel/repo/path": {
                        "status": "error",
                        "error": "GitOperationError: wedged",
                    }
                },
            ),
        ):
            probe.probe_now()

        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok", {"/sentinel/repo/path": {"status": "ok"}}),
        ):
            probe.probe_now()

        assert observations == [True, False, True], (
            "Callback must observe every transition, including the "
            "wedge cycle between healthy → unhealthy → healthy."
        )

    def test_callback_can_be_replaced_via_setter(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        observations_a: list[bool] = []
        observations_b: list[bool] = []
        probe = StateStoreProbe(on_observation=observations_a.append)

        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok", {"/sentinel/repo/path": {"status": "ok"}}),
        ):
            probe.probe_now()

        probe.set_on_observation(observations_b.append)

        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(
                False,
                "1/1 repos wedged: /sentinel/repo/path",
                {
                    "/sentinel/repo/path": {
                        "status": "error",
                        "error": "wedged",
                    }
                },
            ),
        ):
            probe.probe_now()

        assert observations_a == [True]
        assert observations_b == [False]

    def test_callback_exception_does_not_break_probe(self, with_repo_path):
        """A misbehaving callback must not propagate — otherwise a bug
        in the health tracker would silently disable the BG probe."""
        from state_store_probe import StateStoreProbe

        def boom(_healthy: bool) -> None:
            raise RuntimeError("callback exploded")

        probe = StateStoreProbe(on_observation=boom)
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok", {"/sentinel/repo/path": {"status": "ok"}}),
        ):
            healthy, message = probe.probe_now()

        assert healthy is True
        assert message == "ok"
        # Cache populated despite callback exception:
        snap = probe.snapshot()
        assert snap["healthy"] is True
        assert snap["fresh"] is True


class TestProbeNowConcurrency:
    """``probe_now()`` is invoked from both ``cmd_serve`` and ``_loop``;
    the BG thread can fire while a manual call is in flight. The
    in-flight sentinel guarantees the second caller short-circuits
    rather than racing on the cache write (#2191 review item 4)."""

    def test_concurrent_probe_now_short_circuits_second_caller(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe()
        first_started = threading.Event()
        release_first = threading.Event()
        call_count = {"n": 0}

        def slow_probe(_path: Path) -> tuple[bool, str, dict[str, dict[str, str]]]:
            call_count["n"] += 1
            first_started.set()
            release_first.wait(timeout=2.0)
            return True, "ok-slow", {str(_path): {"status": "ok"}}

        with patch("state_store_probe.probe_state_store_at", side_effect=slow_probe):
            t1 = threading.Thread(target=probe.probe_now)
            t1.start()
            assert first_started.wait(timeout=1.0), "first probe never started"

            # While the first is in flight, second call should
            # short-circuit and NOT increment call_count.
            healthy, message = probe.probe_now()
            assert call_count["n"] == 1, (
                "Concurrent probe_now() must not launch a second probe while one is in flight."
            )
            # Cache hasn't been populated yet (first probe still in flight),
            # so the short-circuit returns the starting state.
            assert healthy is False
            assert "starting" in message

            release_first.set()
            t1.join(timeout=2.0)

        # First probe completed normally.
        assert call_count["n"] == 1
        assert probe.snapshot()["message"] == "ok-slow"


class TestProbeStateStoreAtMultiRepo:
    """Regression coverage for #2176 — ``probe_state_store_at`` must
    walk every repo, not bail on the first failure. In multi-repo
    deployments a wedge on repo A used to hide an independent wedge on
    repo B from ``/api/v1/health`` until A was healed."""

    def test_one_wedged_one_healthy_surfaces_both(self, tmp_path):
        """Two repos under a parent dir, repo A wedged + repo B healthy.
        The probe must return both entries, mark the aggregate as
        unhealthy, and name the wedged repo in the summary."""
        from state_store_probe import probe_state_store_at

        repo_a = tmp_path / "repo-a"
        repo_a.mkdir()
        (repo_a / ".git").mkdir()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()

        healthy_store = MagicMock()
        healthy_store.worktree = "/some/worktree/path"

        def fake_get_state_store(repo_path: Path):
            if repo_path == repo_a:
                raise RuntimeError("wedged: worktree contention")
            return healthy_store

        with patch(
            "state_store.get_state_store",
            side_effect=fake_get_state_store,
        ):
            healthy, summary, repos = probe_state_store_at(tmp_path)

        assert healthy is False
        assert str(repo_a) in repos
        assert str(repo_b) in repos
        assert repos[str(repo_a)]["status"] == "error"
        assert "wedged" in repos[str(repo_a)]["error"]
        assert repos[str(repo_b)] == {"status": "ok"}
        # Summary names the wedged repo so an operator scanning the
        # /health response sees which repo is the problem.
        assert str(repo_a) in summary
        assert "1/2" in summary

    def test_all_healthy_returns_ok(self, tmp_path):
        """All repos load cleanly → healthy=True, summary='ok', and
        every repo is recorded."""
        from state_store_probe import probe_state_store_at

        repo_a = tmp_path / "repo-a"
        repo_a.mkdir()
        (repo_a / ".git").mkdir()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()

        store = MagicMock()
        store.worktree = "/some/worktree/path"
        with patch("state_store.get_state_store", return_value=store):
            healthy, summary, repos = probe_state_store_at(tmp_path)

        assert healthy is True
        assert summary == "ok"
        assert repos == {
            str(repo_a): {"status": "ok"},
            str(repo_b): {"status": "ok"},
        }

    def test_no_repos_under_base_path_skips(self, tmp_path):
        """An empty base dir is treated as a config issue, not a wedge.
        Healthy=True, repos empty so /health stays 'healthy' rather
        than flapping degraded."""
        from state_store_probe import probe_state_store_at

        healthy, summary, repos = probe_state_store_at(tmp_path)

        assert healthy is True
        assert "probe-skipped" in summary
        assert repos == {}
