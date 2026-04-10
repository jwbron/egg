"""CLI entry point for running an agent via the egg harness.

Usage::
    python3 -m egg_harness "Your prompt here"
    python3 -m egg_harness --model sonnet --max-turns 100 "Say hello"
    echo "prompt" | python3 -m egg_harness --model opus
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _stream_to_stdout(text: str) -> None:
    """Write text to stdout immediately."""
    sys.stdout.write(text)
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="egg_harness",
        description="Run an agent via the egg harness.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="Prompt text. Reads from stdin if omitted.",
    )
    parser.add_argument("--model", default="opus", help="Model alias or ID.")
    parser.add_argument("--max-turns", type=int, default=None, help="Max conversation turns.")
    parser.add_argument("--system-prompt", default=None, help="System prompt override.")
    parser.add_argument("--timeout", type=int, default=7200, help="Timeout in seconds.")
    parser.add_argument("--session-file", default=None, help="Path for session persistence.")
    parser.add_argument(
        "--provider",
        default="anthropic",
        choices=["anthropic", "openai-compatible"],
        help="LLM provider.",
    )
    parser.add_argument("--endpoint", default=None, help="Provider endpoint URL.")
    parser.add_argument("--interactive", action="store_true", help="Run in interactive mode.")

    args = parser.parse_args()

    if args.interactive:
        from egg_harness.interactive import run_interactive

        return asyncio.run(
            run_interactive(
                model=args.model,
                provider=args.provider,
                endpoint=args.endpoint,
            )
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

    from egg_harness.client import run_agent

    result = run_agent(
        prompt,
        model=args.model,
        max_turns=args.max_turns,
        system_prompt=args.system_prompt,
        timeout=args.timeout,
        on_output=_stream_to_stdout,
        provider=args.provider,
        endpoint=args.endpoint,
        session_file=args.session_file,
    )

    if result.stderr:
        print(result.stderr, file=sys.stderr)

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
