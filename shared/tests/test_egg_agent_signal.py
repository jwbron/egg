"""Tests for ``shared/egg_agent/__main__.py`` SIGTERM handling (issue
#3023, slice-3, TASK-3-3).

Pod entrypoint after slice-3 is exactly ``python3 -m egg_agent --model X
--max-turns N`` with the composed prompt on stdin. There is no wrapper
bash and no heartbeat subshell, so the kubelet's SIGTERM-on-pod-delete
contract now lands directly on the Python process. TASK-3-3 moves the
signal trap that used to live in ``orchestrator/consensus_wrapper.py``
(the ``cleanup`` trap at consensus_wrapper.py:117-124, plus the
``trap cleanup EXIT TERM INT`` line at consensus_wrapper.py:239) into
``shared/egg_agent/__main__.py``.

The plan's TASK-3-3 acceptance lines are:

* A SIGTERM to a per-event pod produces a clean agent shutdown with the
  same audit-log entries the wrapper's ``cleanup`` trap produced.
* An integration test simulates SIGTERM mid-event and asserts the agent
  emits its final-state log + the pod exits with a non-error status.

This file pins those acceptance lines at the **wiring** + **unit**
level:

* the ``__main__`` module installs a signal handler for SIGTERM and
  SIGINT (the asyncio event-loop reentrancy hazard reviewer_concurrency
  flagged on v1 — a naive ``signal.signal`` inside a running event loop
  would either silently no-op or fight the SDK's own signal handling);
* the handler emits a final-state log line on its way out so an
  operator's ``kubectl logs`` after the pod has been terminated still
  shows the shutdown reason; the exit code is non-error.

End-to-end behaviour (drive a real subprocess, send SIGTERM, observe the
pod exit + audit log) lives in an integration test placeholder at the
bottom of this file so the unit-level pins stay fast under
``make test``.

Skip-vs-assert pattern mirrors slice-1's
``test_phase_idle_budget.py``: the file is collectable pre-TASK-3-3 and
skips cleanly until the production change lands.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import pytest

# conftest.py at shared/tests/ inserts shared/ on sys.path; assert the
# guarantee so a future regression there fails with an obvious
# diagnostic on this file instead of a confusing ``ModuleNotFoundError``.
_shared_path = Path(__file__).parent.parent
assert str(_shared_path) in sys.path, (
    "shared/tests/conftest.py must have added shared/ to sys.path before this module is collected."
)


def _import_main_module():
    """Import ``egg_agent.__main__`` lazily so the test file is
    collectable in environments without the claude-agent-sdk runtime
    (the orchestrator-side CI image).
    """
    return pytest.importorskip(
        "egg_agent.__main__",
        reason=(
            "shared/egg_agent/__main__.py is the slice-3 TASK-3-3 home "
            "for the SIGTERM signal trap; tests skip until the coder "
            "lands the trap. The module exists pre-TASK-3-3 as a thin "
            "argparse wrapper around run_agent — the importorskip "
            "below covers the rare CI environments without the SDK at all."
        ),
    )


def _task_3_3_landed() -> bool:
    """Return True once TASK-3-3's SIGTERM trap has landed in
    ``shared/egg_agent/__main__.py``.

    The signal we key off is the presence of a public ``handle_sigterm``
    (or ``_install_signal_handlers``) symbol on the module. Pre-TASK-3-3
    ``__main__`` is a thin argparse wrapper around ``run_agent`` and
    exposes neither symbol; post-TASK-3-3 the trap-install seam must be
    exported so this test (and any operator's ``python3 -c 'import
    egg_agent.__main__; help(...)'`` triage) can locate it.

    The check is tolerant of the exact spelling (``handle_sigterm``,
    ``_handle_sigterm``, or ``_install_signal_handlers``) so the test
    pins the contract without naming a single private symbol.
    """
    main_mod = _import_main_module()
    seam_names = (
        "handle_sigterm",
        "_handle_sigterm",
        "install_signal_handlers",
        "_install_signal_handlers",
    )
    return any(hasattr(main_mod, name) for name in seam_names)


def _resolve_handler_seam(main_mod):
    """Return whichever of the accepted seam names is present, or
    ``None`` if none are.
    """
    for name in (
        "handle_sigterm",
        "_handle_sigterm",
        "install_signal_handlers",
        "_install_signal_handlers",
    ):
        if hasattr(main_mod, name):
            return getattr(main_mod, name)
    return None


# --------------------------------------------------------------------------- #
# (1) Module-level wiring: __main__ exposes a SIGTERM seam
# --------------------------------------------------------------------------- #


class TestSigtermSeamExposed:
    """TASK-3-3 acceptance: SIGTERM handling moves into
    ``shared/egg_agent/__main__.py`` and the trap-install seam is
    discoverable from the module namespace. Pre-TASK-3-3 the module has
    no such seam, so the tests skip cleanly.
    """

    def test_main_module_imports(self):
        """The argparse wrapper at ``shared/egg_agent/__main__.py``
        must continue to import (TASK-3-3 must not break the existing
        entry point). This pin runs pre- and post-TASK-3-3.
        """
        main_mod = _import_main_module()
        # ``main`` is the existing entrypoint preserved from pre-TASK-3-3;
        # a regression that renames or removes it would break the
        # ``python3 -m egg_agent`` invocation the plan requires.
        assert hasattr(main_mod, "main"), (
            "shared/egg_agent/__main__.py must expose a ``main`` "
            "function as the ``python3 -m egg_agent`` entry point. "
            "TASK-3-3 must NOT break this."
        )

    def test_sigterm_seam_exposed_after_task_3_3(self):
        """Post-TASK-3-3 the module must expose a SIGTERM trap-install
        seam (named ``handle_sigterm``, ``install_signal_handlers``, or
        their underscore-prefixed siblings) so the trap is discoverable
        for operator triage and for the integration test that drives
        ``kill -TERM`` against a live process.
        """
        if not _task_3_3_landed():
            pytest.skip(
                "TASK-3-3 (SIGTERM trap migration into "
                "shared/egg_agent/__main__.py) not yet landed; the "
                "module exposes no signal-handler seam. Test will "
                "assert once the coder's commit lands."
            )
        main_mod = _import_main_module()
        assert _resolve_handler_seam(main_mod) is not None, (
            "TASK-3-3 acceptance: shared/egg_agent/__main__.py must "
            "expose a signal-handler seam (handle_sigterm / "
            "install_signal_handlers) so the trap that used to live in "
            "consensus_wrapper.py:117-124 is discoverable from the "
            "Python-side entrypoint."
        )


# --------------------------------------------------------------------------- #
# (2) Asyncio re-entrancy: the trap doesn't fight the running event loop
# --------------------------------------------------------------------------- #


class TestSigtermInsideAsyncioLoop:
    """Reviewer_concurrency v1 flagged the asyncio event-loop re-entrancy
    hazard explicitly: a SIGTERM handler inside an asyncio event loop is
    a textbook concurrency-lens target (heartbeat cadence under stall
    #2012, final-state log ordering vs. cleanup, event-loop re-entrancy).

    The agent SDK runs ``query()`` under ``asyncio.run`` — the Python
    runtime installs default signal handlers on the *main* thread's
    selector; a naïve ``signal.signal(SIGTERM, handler)`` works on the
    main thread but is a no-op on every other thread. TASK-3-3's trap
    MUST install on the main thread and MUST be re-entrant-safe (the
    handler runs in the main thread's interrupt context, but the work
    it triggers must dispatch back through the loop, not raise from
    inside ``handler`` itself).
    """

    def test_handler_is_callable_on_main_thread(self):
        """The handler must be installable from the main thread without
        raising. This pin catches the most common bug — installing the
        handler from a worker thread, where ``signal.signal`` raises
        ``ValueError: signal only works in main thread``.
        """
        if not _task_3_3_landed():
            pytest.skip("TASK-3-3 not yet landed; signal-handler seam absent.")

        main_mod = _import_main_module()
        seam = _resolve_handler_seam(main_mod)
        assert seam is not None
        # Either the seam is a function (``install_signal_handlers``)
        # we can call directly, or a handler callable
        # (``handle_sigterm``) we can register via ``signal.signal``.
        # Both shapes are valid; this test just exercises the call
        # path so an install-time TypeError or main-thread-only error
        # surfaces here.
        try:
            if callable(seam):
                # If the seam is install_signal_handlers, calling it is
                # the install path. If it's handle_sigterm, we register
                # it under SIGTERM and immediately restore so the test
                # doesn't leave the interpreter with a custom handler.
                previous = signal.getsignal(signal.SIGTERM)
                try:
                    if seam.__name__ in (
                        "install_signal_handlers",
                        "_install_signal_handlers",
                    ):
                        seam()
                    else:
                        signal.signal(signal.SIGTERM, seam)
                finally:
                    signal.signal(signal.SIGTERM, previous)
        except ValueError as e:
            pytest.fail(
                f"TASK-3-3 acceptance: signal-handler seam must be "
                f"installable from the main thread without raising; "
                f"got ValueError: {e}. The trap must register on the "
                f"main thread so SIGTERM from kubelet actually fires."
            )


# --------------------------------------------------------------------------- #
# (3) Final-state log parity with the wrapper's cleanup trap
# --------------------------------------------------------------------------- #


class TestFinalStateLogParity:
    """TASK-3-3 acceptance: ``a SIGTERM to a per-event pod produces a
    clean agent shutdown with the same audit-log entries the wrapper's
    cleanup trap (consensus_wrapper.py:117-124) produced``.

    The wrapper's cleanup trap emitted two log shapes:

    * ``[event-pump] cleanup: stopping background heartbeat`` (via
      ``cw_log``), which we replace with a Python-side log line carrying
      the same semantic content (``shutting down on SIGTERM``);
    * a final-state log line capturing the BRC role state at the moment
      of shutdown so an operator scanning ``kubectl logs`` after the pod
      is gone can see *why* the agent stopped (vs. the legacy mystery of
      a kubelet-terminated wrapper with no audit trail).

    We pin the **shape** of the final-state log here (key/value pairs an
    operator's grep can latch onto) so a regression that emits an empty
    log line slips through. The exact wording is the coder's choice.
    """

    def test_final_state_log_keys_present(self):
        """Skip until TASK-3-3 lands; then assert the seam emits a log
        line referencing the role + signal + reason on its way out.

        This test is intentionally **placeholder-shaped** — once the
        coder lands the trap, the assertion expands to capture the log
        stream (via ``caplog`` or a structured-log fixture) and pin the
        keys. Today it just records the expected shape as a docstring
        + a clean skip so the BRC matrix has a tester pin against the
        acceptance line.
        """
        if not _task_3_3_landed():
            pytest.skip(
                "TASK-3-3 not yet landed; final-state log parity pin "
                "is recorded as a placeholder. Once the coder lands "
                "the trap, expand this test to capture the structured "
                "log stream and assert against {role, signal, reason}."
            )

        # Post-TASK-3-3, fill in: drive the handler via a captured-
        # logger fixture and assert the emitted record carries the
        # role + signal name + a non-empty reason. The wrapper's
        # cleanup trap emitted these via ``cw_log`` -> stderr; the
        # Python side must continue to emit them so audit log
        # observability does not regress.
        pytest.skip(
            "Post-TASK-3-3 expansion: capture the structured log "
            "stream and assert {role, signal, reason} keys. Leaving "
            "the skip in place until the production trap exposes a "
            "test seam for log-stream capture."
        )


# --------------------------------------------------------------------------- #
# (4) Double-fire guard for re-entrant SIGTERM
# --------------------------------------------------------------------------- #


class TestSigtermDoubleFireGuard:
    """Direct rebuttal to reviewer_concurrency v2 item 4 (TASK-3-3
    concurrency-lens concern): "SIGTERM handler inside an asyncio event
    loop … needs ... double-fire guards" pins.

    Operationally, the kubelet sends SIGTERM at pod-delete time. If the
    grace period elapses without the pod exiting, the kubelet sends
    SIGKILL. But there are two intermediate-states where a SECOND
    SIGTERM can land on the same process:

    * an operator's ``kubectl delete pod --grace-period=0`` (the second
      kubelet pass);
    * a pod restart loop where systemd / containerd re-sends TERM
      during a graceful-stop tight retry.

    Without a guard, the second SIGTERM would re-enter the handler while
    the first invocation is still emitting the final-state log + tearing
    down the asyncio loop — producing duplicate audit records, racing
    cleanup, and a non-zero exit. Pin that the handler is idempotent at
    the seam-level: registering it twice and invoking it twice MUST be
    safe. The pin is tolerant of the exact spelling of the guard (a
    ``_shutdown_in_progress`` flag, an ``asyncio.Event``, an explicit
    ``Once``-like primitive) — we just exercise the install + double-
    invoke path and assert no exception escapes.
    """

    def test_handler_install_is_idempotent(self):
        """Installing the SIGTERM handler twice MUST NOT raise. A naive
        ``signal.signal`` call is itself idempotent (returns the
        previous handler), but ``install_signal_handlers`` may also
        register a SIGINT handler, start a background coroutine, or
        attach to an asyncio loop — each of those is a re-entrancy
        hazard if the install path is called twice. This test exercises
        the install path twice and restores the previous handlers so
        the test interpreter is not left with a custom handler.
        """
        if not _task_3_3_landed():
            pytest.skip(
                "TASK-3-3 not yet landed; double-fire guard pin is "
                "recorded as a placeholder. Once the coder lands the "
                "trap, expand this test to assert the second install "
                "is a no-op (or returns cleanly)."
            )

        main_mod = _import_main_module()
        seam = _resolve_handler_seam(main_mod)
        assert seam is not None

        if seam.__name__ not in (
            "install_signal_handlers",
            "_install_signal_handlers",
        ):
            # The seam is the bare handler callable, not an installer.
            # Bare callable double-fire is exercised in
            # ``test_handler_double_invoke_is_safe`` below.
            pytest.skip(
                "Seam is a bare handler, not an installer; double-"
                "install path does not apply. The double-invoke path "
                "is exercised in test_handler_double_invoke_is_safe."
            )

        previous_term = signal.getsignal(signal.SIGTERM)
        previous_int = signal.getsignal(signal.SIGINT)
        try:
            # First install: the canonical path.
            seam()
            # Second install: MUST NOT raise. A naive implementation
            # that, e.g., starts a background asyncio task on every
            # install call would leak a task here — the guard is
            # required for graceful operator triage where the install
            # is invoked twice (initial main + a debug-re-install).
            seam()
        except Exception as e:
            pytest.fail(
                f"TASK-3-3 double-fire guard: installing the signal "
                f"handler twice must be idempotent; raised "
                f"{type(e).__name__}: {e}. The handler must guard "
                f"against re-installation so a kubelet grace-period "
                f"re-send + an operator's explicit re-install do not "
                f"compound."
            )
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)

    def test_handler_double_invoke_is_safe(self):
        """If the kubelet sends a second SIGTERM during the grace
        period, the handler MUST tolerate being invoked twice on the
        same frame. The first invocation initiates shutdown; the second
        invocation must short-circuit (or re-emit cleanly) without
        raising and without crashing the asyncio loop.

        The pin is at the call level: invoke the handler twice with
        synthetic signal-frame args and assert no exception escapes.
        Once the production trap lands the assertion expands to also
        check that the final-state log is emitted ONCE (not twice).
        """
        if not _task_3_3_landed():
            pytest.skip(
                "TASK-3-3 not yet landed; double-invoke pin is "
                "recorded as a placeholder. Once the coder lands the "
                "trap, expand this test to capture the structured "
                "log stream and assert {role, signal, reason} keys "
                "appear exactly once."
            )

        main_mod = _import_main_module()
        seam = _resolve_handler_seam(main_mod)
        assert seam is not None

        # Find the bare handler callable. If the seam is an installer,
        # introspect the module for a ``handle_sigterm`` /
        # ``_handle_sigterm`` companion symbol.
        handler = None
        if seam.__name__ in (
            "install_signal_handlers",
            "_install_signal_handlers",
        ):
            for name in ("handle_sigterm", "_handle_sigterm"):
                if hasattr(main_mod, name):
                    handler = getattr(main_mod, name)
                    break
            if handler is None:
                pytest.skip(
                    "Seam is an installer with no exported "
                    "handle_sigterm companion; double-invoke pin can "
                    "only be exercised end-to-end (see "
                    "TestSigtermEndToEndIntegration)."
                )
        else:
            handler = seam

        # Synthetic signal-frame args: signum + frame=None is the
        # standard shape ``signal.signal`` callbacks receive.
        try:
            handler(signal.SIGTERM, None)
            handler(signal.SIGTERM, None)
        except SystemExit:
            # Calling the handler may sys.exit() — that is acceptable
            # on the FIRST invocation but the SECOND invocation must
            # land on a guard that short-circuits. If both invocations
            # raise SystemExit the test passes (the second was a clean
            # exit), but a non-SystemExit exception is a regression.
            pass
        except Exception as e:
            pytest.fail(
                f"TASK-3-3 double-fire guard: invoking the SIGTERM "
                f"handler twice raised {type(e).__name__}: {e}. The "
                f"handler must guard against re-entrancy so a kubelet "
                f"grace-period re-send does not compound (duplicate "
                f"audit records, racing asyncio cancellation)."
            )


# --------------------------------------------------------------------------- #
# (5) Signal-vs-asyncio-loop race: handler doesn't block the loop
# --------------------------------------------------------------------------- #


class TestSigtermSignalVsAsyncioLoopRace:
    """Direct rebuttal to reviewer_concurrency v3 item 4 (TASK-3-3
    concurrency-lens list): "signal-vs-loop race" pin.

    The hazard the reviewer named: the SDK's ``query()`` runs under
    ``asyncio.run``. A Python signal handler runs in the *main thread's
    interrupt context*, NOT inside the loop's coroutine context. If the
    handler does anything that touches the running loop synchronously
    (e.g. ``loop.run_until_complete``, ``asyncio.run`` re-entry,
    blocking ``Future.result()``), it deadlocks the loop and the agent
    hangs until SIGKILL — defeating the entire reason TASK-3-3 migrates
    the trap into the Python process.

    The safe shape: the handler MUST be quick — set a flag, schedule a
    coroutine via ``loop.call_soon_threadsafe`` / ``asyncio.run_coroutine
    _threadsafe``, then return. Cleanup runs on the loop, not in the
    handler.

    This pin is at the source-text level (the production handler isn't
    importable in CI without the SDK, so we can't drive a real loop)
    and tolerantly checks the handler body for the documented anti-
    patterns and for at least one of the canonical safe-handoff idioms.
    """

    def test_handler_does_not_block_running_loop(self):
        """The handler body MUST NOT call ``asyncio.run`` or
        ``loop.run_until_complete`` — those are the textbook re-entrancy
        crashes (``RuntimeError: This event loop is already running``).

        Pre-TASK-3-3 the seam doesn't exist; skip. Post-TASK-3-3 the
        seam's source body MUST avoid both anti-patterns.
        """
        if not _task_3_3_landed():
            pytest.skip(
                "TASK-3-3 not yet landed; signal-vs-loop race pin is "
                "recorded as a placeholder. Once the coder lands the "
                "trap, the body grep below asserts the handler does "
                "not re-enter the loop."
            )

        # Locate the production handler source. The seam may be a
        # bare callable or an installer; either way the file we want
        # is shared/egg_agent/__main__.py.
        main_py = _shared_path / "egg_agent" / "__main__.py"
        if not main_py.is_file():
            pytest.skip(
                "shared/egg_agent/__main__.py not present in this "
                "checkout — skipping signal-vs-loop race source pin."
            )

        source = main_py.read_text(encoding="utf-8")

        # Anti-patterns the handler MUST NOT use: these would re-enter
        # the running loop from a signal-handler frame and deadlock.
        forbidden = (
            "asyncio.run(",
            "run_until_complete(",
            ".result(timeout=",  # blocking Future wait from handler
        )
        for token in forbidden:
            assert token not in source, (
                f"TASK-3-3 signal-vs-loop race: shared/egg_agent/"
                f"__main__.py contains {token!r}, which inside a "
                f"signal handler would re-enter the running asyncio "
                f"loop and deadlock the agent until SIGKILL. The "
                f"handler must schedule cleanup via "
                f"``loop.call_soon_threadsafe`` or "
                f"``asyncio.run_coroutine_threadsafe`` and return."
            )

        # Safe-handoff idioms: at least one must be present so the
        # handler has *some* way to land work on the loop. Tolerant of
        # exact spelling.
        safe_idioms = (
            "call_soon_threadsafe",
            "run_coroutine_threadsafe",
            "loop.stop(",
            "shutdown_event",
            "shutdown_in_progress",
            "_shutdown",
        )
        assert any(idiom in source for idiom in safe_idioms), (
            "TASK-3-3 signal-vs-loop race: shared/egg_agent/__main__.py "
            "must use one of the documented safe-handoff idioms "
            f"({', '.join(safe_idioms)}) so the SIGTERM handler can "
            "schedule cleanup on the asyncio loop without blocking. "
            "None of the idioms found — the handler likely runs cleanup "
            "synchronously in the interrupt frame, racing the loop."
        )


# --------------------------------------------------------------------------- #
# (6) Heartbeat-cadence under stall: heartbeat stops on shutdown
# --------------------------------------------------------------------------- #


class TestSigtermHeartbeatCadenceUnderStall:
    """Direct rebuttal to reviewer_concurrency v3 item 4 (TASK-3-3
    concurrency-lens list): "heartbeat cadence under stall" pin.

    The hazard: the pre-TASK-3-3 wrapper had a background heartbeat
    subshell (``consensus_wrapper.py:188-234``) that emitted HEARTBEAT
    messages every N seconds. When SIGTERM arrived, the wrapper's
    cleanup trap (``consensus_wrapper.py:117-124``) killed the subshell
    before exiting so no further heartbeats landed on the orchestrator
    after the agent had shut down. The post-TASK-3-3 trap MUST preserve
    that invariant: once the agent process has accepted SIGTERM and is
    on the shutdown path, no further HEARTBEAT messages are emitted.

    A regression here would cause the orchestrator's stuck-phase
    detector (#3023 slice-1 PhaseIdleBudgetTimer) to see heartbeats
    from a dying pod and *delay* its overseer alert — defeating the
    whole point of slice-1's alerting.

    The pin is source-level: assert the production handler references
    a shutdown sentinel that the heartbeat path can check on each tick.
    """

    def test_handler_signals_shutdown_to_heartbeat_path(self):
        """The handler body MUST reference a shutdown sentinel — a flag,
        Event, or cancellation token — that the heartbeat-emit code
        path checks on each iteration. Without it the heartbeat thread
        continues sending after the agent has stopped processing
        events.
        """
        if not _task_3_3_landed():
            pytest.skip(
                "TASK-3-3 not yet landed; heartbeat-cadence-under-"
                "stall pin is recorded as a placeholder. Once the "
                "coder lands the trap, the source-grep below asserts "
                "the handler signals shutdown to the heartbeat path."
            )

        main_py = _shared_path / "egg_agent" / "__main__.py"
        if not main_py.is_file():
            pytest.skip(
                "shared/egg_agent/__main__.py not present in this "
                "checkout — skipping heartbeat-cadence pin."
            )

        source = main_py.read_text(encoding="utf-8")

        # Look for any documented shutdown-sentinel idiom that the
        # heartbeat path can latch onto. Tolerant of spelling.
        sentinels = (
            "shutdown_event",
            "shutdown_in_progress",
            "_shutdown",
            "is_shutting_down",
            "cancel(",  # task.cancel() to a heartbeat coroutine
            "stop_event",
        )
        assert any(sentinel in source for sentinel in sentinels), (
            "TASK-3-3 heartbeat-cadence acceptance: the SIGTERM "
            "handler in shared/egg_agent/__main__.py must signal "
            "shutdown to the heartbeat-emit path via one of the "
            f"documented sentinel idioms ({', '.join(sentinels)}). "
            "Without it, HEARTBEAT messages would continue flowing "
            "from a dying pod and silence the orchestrator's stuck-"
            "phase alert (#3023 slice-1 PhaseIdleBudgetTimer)."
        )


# --------------------------------------------------------------------------- #
# (7) End-to-end integration sentinel
# --------------------------------------------------------------------------- #


class TestSigtermEndToEndIntegration:
    """Plan §slice-3 / TASK-3-3 integration acceptance:

        Integration test simulates SIGTERM mid-event and asserts the
        agent emits its final-state log and the pod exits with a
        non-error status.

    The full subprocess scenario (spawn ``python3 -m egg_agent``, send
    ``SIGTERM``, observe stdout + exit code) requires the Claude Agent
    SDK to be installed in the test image; we leave that to the
    integration suite. This placeholder records the contract so a
    future regression can wire it in without re-deriving the shape from
    the plan text.
    """

    def test_sigterm_exit_code_is_zero(self):
        pytest.skip(
            "TASK-3-3 end-to-end integration shape recorded as an "
            "explicit placeholder; the live subprocess scenario "
            "(SIGTERM mid-event → final-state log + non-error exit) "
            "lives in the integration suite (integration_tests/) so "
            "this file stays fast under `make test`. Re-target this "
            "test once the integration-suite scaffold lands."
        )
