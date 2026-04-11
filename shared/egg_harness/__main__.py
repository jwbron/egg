"""CLI entry point for ``python3 -m egg_harness``.

Parses command-line arguments and dispatches to either the single-shot
:func:`~egg_harness.client.run_agent_async` runner or the interactive
multi-turn REPL.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the egg harness CLI."""
    parser = argparse.ArgumentParser(
        prog="egg_harness",
        description="Run an agent session via the egg harness.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help=("The prompt to send to the agent.  If omitted, reads from stdin."),
    )
    parser.add_argument(
        "--model",
        default="opus[1m]",
        help="Model specification (default: opus[1m]).",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Maximum number of conversation turns.",
    )
    parser.add_argument(
        "--system-prompt",
        default=None,
        help="Optional system prompt override.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=7200,
        help="Maximum execution time in seconds (default: 7200).",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Launch multi-turn interactive REPL mode.",
    )
    return parser


def main() -> int:
    """Parse arguments and run the agent or interactive session.

    Returns:
        Exit code: 0 on success, non-zero on failure.
    """
    parser = _build_parser()
    args = parser.parse_args()

    # -- Interactive mode ----------------------------------------------
    if args.interactive:
        from egg_harness.interactive import run_interactive

        return asyncio.run(
            run_interactive(
                model=args.model,
                system_prompt=args.system_prompt,
                timeout=args.timeout,
            )
        )

    # -- Single-shot mode ----------------------------------------------
    prompt: str | None = args.prompt
    if prompt is None:
        if sys.stdin.isatty():
            parser.error(
                "No prompt provided.  Pass a prompt argument or pipe "
                "input via stdin, or use --interactive."
            )
        prompt = sys.stdin.read().strip()
        if not prompt:
            parser.error("Empty prompt received from stdin.")

    from egg_harness.client import run_agent_async

    def _on_output(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    result = asyncio.run(
        run_agent_async(
            prompt,
            model=args.model,
            max_turns=args.max_turns,
            system_prompt=args.system_prompt,
            timeout=args.timeout,
            on_output=_on_output,
        )
    )

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
