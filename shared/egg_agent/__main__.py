"""CLI entry point for running a Claude agent inside a container.

Usage::

    python3 -m egg_agent "Your prompt here"
    python3 -m egg_agent --model sonnet --max-turns 1 "Say hello"
    echo "prompt" | python3 -m egg_agent --model opus

SIGTERM handling (#3023 slice-3 TASK-3-3)
-----------------------------------------

Post-#3023 the pod entrypoint is exactly ``python3 -m egg_agent`` — no
wrapper bash, no heartbeat subshell. The kubelet's SIGTERM-on-pod-delete
contract therefore lands directly on this Python process. The signal
trap now lives here so an operator's ``kubectl logs`` after the pod has
been terminated continues to show why the agent stopped, with an audit-
log shape that mirrors the prior in-pod cleanup.

The handler is installed via :func:`install_signal_handlers` (called
from :func:`main` before the agent's event loop starts). Both
``SIGTERM`` and ``SIGINT`` are trapped so a manual ``kubectl delete pod``
and a developer's ``Ctrl-C`` produce the same clean shutdown shape.

Concurrency note (reviewer_concurrency v1): the trap MUST be installed
on the main thread (``signal.signal`` raises ``ValueError`` on every
other thread). The handler emits a structured final-state log line and
then exits with status 0 — it does NOT call into the asyncio event loop
from the interrupt context, which would risk a re-entrancy hazard
against the SDK's own signal handling. The agent's in-flight work for
the current event has already been committed (or not) before the
SIGTERM arrives; a clean process exit is the kubelet contract.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from types import FrameType

from egg_agent.client import run_agent

# Track whether a final-state log line has already been emitted so that
# a SIGTERM followed by a SIGINT (or vice versa) doesn't double-log.
_shutdown_logged = False
_shutdown_lock = threading.Lock()


def _stream_to_stdout(text: str) -> None:
    """Write text to stdout immediately, flushing to avoid buffering delays."""
    sys.stdout.write(text)
    sys.stdout.flush()


def _signal_name(signum: int) -> str:
    """Return the human-readable name for a signal number, falling back to
    the integer if the name lookup fails (e.g. on a non-POSIX kernel)."""
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal-{signum}"


def _emit_final_state_log(signum: int, reason: str) -> None:
    """Emit a single structured log line describing the shutdown reason.

    The audit shape:

      * a one-line summary noting the signal and the role that received it;
      * the per-role identifier so an operator's ``kubectl logs`` after the
        pod has been terminated can correlate the shutdown back to the BRC
        matrix without correlating against the orchestrator's audit log.

    Written to stderr (not stdout) so the message survives even if stdout
    is being piped into a tool that buffers on signal. We use ``print``
    rather than the structured logger to avoid pulling in any logger
    initialisation that could deadlock from inside an interrupt context.
    """
    global _shutdown_logged
    with _shutdown_lock:
        if _shutdown_logged:
            return
        _shutdown_logged = True

    role = os.environ.get("EGG_AGENT_ROLE", "unknown")
    pipeline_id = os.environ.get("EGG_PIPELINE_ID", "unknown")
    slice_id = os.environ.get("EGG_SLICE_ID", "none")
    sig_name = _signal_name(signum)

    # Single-line shutdown audit entry. Operators grep for
    # ``[egg-agent] shutting down`` and the keys below.
    print(
        f"[egg-agent] shutting down on {sig_name} "
        f"role={role} pipeline_id={pipeline_id} slice_id={slice_id} "
        f"reason={reason!r}",
        file=sys.stderr,
        flush=True,
    )


def handle_sigterm(signum: int, frame: FrameType | None) -> None:
    """Signal handler for SIGTERM / SIGINT.

    The handler runs in the main thread's interrupt context. The work it
    triggers (a single stderr write + ``sys.exit``) is deliberately
    minimal so it does not interact with the asyncio event loop the
    Agent SDK runs under — the SDK's own signal handling installs a
    default ``KeyboardInterrupt`` raise on SIGINT, which we override
    here with a clean exit code 0 to match the kubelet's expectation
    that a SIGTERM-on-delete pod exits with a non-error status.

    Args:
        signum: Signal number that fired.
        frame: Current stack frame (unused; required by ``signal.signal``).

    Returns:
        Never (calls ``sys.exit(0)``).
    """
    del frame  # unused
    reason = "kubelet SIGTERM" if signum == signal.SIGTERM else "user interrupt"
    _emit_final_state_log(signum, reason)
    # Exit with status 0. The kubelet contract is: SIGTERM-on-pod-delete
    # must produce a non-error exit so the Job's success/failure status
    # reflects the agent's work, not the lifecycle terminator.
    sys.exit(0)


def install_signal_handlers() -> None:
    """Install :func:`handle_sigterm` on SIGTERM and SIGINT.

    MUST be called from the main thread before any worker thread starts.
    ``signal.signal`` raises ``ValueError: signal only works in main thread``
    if invoked from a non-main thread, which would silently break the
    kubelet's SIGTERM contract — every spawned pod would then hang on
    delete until k8s force-kills it with SIGKILL after the grace period.

    Idempotent: re-installing the same handler on the same signal is a
    no-op (Python replaces the previous handler), so callers that wrap
    :func:`main` (e.g. test harnesses) do not need a separate cleanup.
    """
    # ``signal.signal`` raises ValueError when invoked outside the main
    # thread. We surface the failure with the operator-actionable
    # context rather than letting the original ValueError escape.
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "install_signal_handlers must be called from the main thread; "
            "signal.signal() raises ValueError on every other thread, which "
            "would break the kubelet's SIGTERM-on-pod-delete contract."
        )
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="egg_agent",
        description="Run a Claude agent via the Agent SDK.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="Prompt text. Reads from stdin if omitted.",
    )
    parser.add_argument("--model", default="opus[1m]", help="Model alias or ID.")
    parser.add_argument("--max-turns", type=int, default=None, help="Max conversation turns.")
    parser.add_argument("--system-prompt", default=None, help="System prompt override.")
    parser.add_argument("--timeout", type=int, default=7200, help="Timeout in seconds.")

    args = parser.parse_args()

    # Install signal handlers before any agent work starts so a SIGTERM
    # received during prompt read / Agent SDK init still produces a
    # clean shutdown rather than the kubelet's SIGKILL after the grace
    # period. Best-effort: if we are running in a context where
    # ``signal.signal`` is unavailable (unlikely under CPython, but the
    # standard library doesn't guarantee it on all platforms), we log
    # the failure and continue — the pre-#3023 wrapper had no signal
    # trap either when the bash inside the pod crashed early.
    try:
        install_signal_handlers()
    except (RuntimeError, ValueError) as install_err:
        print(
            f"[egg-agent] WARN: install_signal_handlers failed: {install_err!r}; "
            "kubelet SIGTERM will rely on the default Python handler",
            file=sys.stderr,
            flush=True,
        )

    prompt = args.prompt
    if prompt is None:
        if sys.stdin.isatty():
            print("Error: no prompt provided and stdin is a terminal", file=sys.stderr)
            return 1
        prompt = sys.stdin.read().strip()
        if not prompt:
            print("Error: empty prompt from stdin", file=sys.stderr)
            return 1

    result = run_agent(
        prompt,
        model=args.model,
        max_turns=args.max_turns,
        system_prompt=args.system_prompt,
        timeout=args.timeout,
        on_output=_stream_to_stdout,
    )

    if result.stderr:
        print(result.stderr, file=sys.stderr)

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
