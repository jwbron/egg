"""CLI entry point for running a Claude agent inside a container.

Usage::

    python3 -m egg_agent "Your prompt here"
    python3 -m egg_agent --model sonnet --max-turns 1 "Say hello"
    echo "prompt" | python3 -m egg_agent --model opus
"""

from __future__ import annotations

import argparse
import sys

from egg_agent.client import run_agent
from egg_agent.session import write_session_state


def _stream_to_stdout(text: str) -> None:
    """Write text to stdout immediately, flushing to avoid buffering delays."""
    sys.stdout.write(text)
    sys.stdout.flush()


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
    parser.add_argument(
        "--effort",
        default=None,
        choices=["low", "medium", "high", "max"],
        help=("Reasoning effort for the session. Omit to inherit Claude Code's per-model default."),
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="SESSION_ID",
        help=(
            "Claude session_id to re-enter (warm resume, #3200 slice-6). Opt-in "
            "and default OFF: only resumes when EGG_SESSION_RESUME is enabled; an "
            "absent/stale session cold-starts from the protected root. The "
            "resume-vs-reseed decision lives in the slice-8 gate."
        ),
    )
    parser.add_argument(
        "--session-state-file",
        default=None,
        metavar="PATH",
        help=(
            "Where to persist this run's session_id + window occupancy so a later "
            "event-pump invocation can resume it (defaults to $EGG_SESSION_STATE_FILE; "
            "no-op when neither is set). The round-trip's read+decide side is the "
            "slice-8 gate."
        ),
    )

    args = parser.parse_args()

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
        effort=args.effort,
        resume=args.resume,
    )

    # Write side of the session-state round-trip (#3200 slice-6): persist this
    # run's session_id + window occupancy so a later event-pump invocation can
    # re-enter (or, per the slice-8 gate, reseed). Best-effort and inert when no
    # path is configured; a persistence failure never changes the exit code.
    write_session_state(
        result.session_id,
        result.window_occupancy,
        path=args.session_state_file,
    )

    if result.stderr:
        print(result.stderr, file=sys.stderr)

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
