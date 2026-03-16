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
    )

    if result.stderr:
        print(result.stderr, file=sys.stderr)

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
