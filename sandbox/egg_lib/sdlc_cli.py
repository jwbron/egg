"""Main CLI module for egg-sdlc.

Replaces the Claude-as-collaborator SDLC pipeline workflow with a rich
terminal CLI that directly handles DAG visualization and HITL checkpoints.

Usage:
    egg-sdlc -r egg -i 659           # Repo dir + issue number
    egg-sdlc -r egg 659              # Short form (positional issue)
    egg-sdlc 659                     # Auto-detect repo
    egg-sdlc --repo owner/repo -i 1  # Explicit owner/repo
    egg-sdlc                         # Local/prompt mode (no issue)
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

from .orch_client import OrchClient, OrchestratorError
from .sdlc_hitl import _parse_egg_repos, handle_hitl_checkpoint

# ANSI escape codes
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

IS_TTY = sys.stdout.isatty()

# Status → color mapping (same as egg-pipeline-watch)
STATUS_COLORS = {
    "running": CYAN,
    "complete": GREEN,
    "failed": RED,
    "pending": DIM,
    "awaiting_human": YELLOW,
    "cancelled": RED,
}


def _write(text: str, file=None) -> None:
    """Write text, stripping ANSI if not a TTY."""
    import re

    file = file or sys.stdout
    if not file.isatty():
        text = re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", text)
    file.write(text)
    file.flush()


def _clear_screen() -> None:
    """Clear the terminal screen."""
    if IS_TTY:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def _resolve_repo_dir(repo_dir: str) -> str | None:
    """Resolve a repo directory name to owner/repo format.

    Looks up the directory name in EGG_REPOS (e.g. "egg" matches "jwbron/egg")
    and changes to the repo directory if it exists.
    """
    from pathlib import Path

    # Find matching entry in EGG_REPOS
    for entry in _parse_egg_repos():
        if entry.split("/")[-1] == repo_dir:
            # Change to the repo directory so git/gh commands work
            repo_path = Path.home() / "repos" / repo_dir
            if repo_path.is_dir():
                os.chdir(repo_path)
            return entry

    # Not in EGG_REPOS — check if the directory exists and try gh detection
    repo_path = Path.home() / "repos" / repo_dir
    if repo_path.is_dir():
        os.chdir(repo_path)
        try:
            result = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass

    return None



# --- SSE Parsing (reused from egg-pipeline-watch) ---


def parse_sse_stream(response):
    """Parse an SSE stream from an HTTP response.

    Yields (event_type, data_dict) tuples.
    """
    event_type = None
    data_lines = []

    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")

        if line.startswith("event: "):
            event_type = line[7:]
        elif line.startswith("data: "):
            data_lines.append(line[6:])
        elif line.startswith(":"):
            pass  # Heartbeat comment
        elif line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"raw": raw}
                yield (event_type or "message", data)
            event_type = None
            data_lines = []


# --- DAG Visualization ---


def render_header(pipeline_id: str, event_type: str | None = None) -> str:
    """Render header line above the DAG."""
    lines = [f"{BOLD}egg-sdlc: {pipeline_id}{RESET}"]
    if event_type:
        lines.append(f"{DIM}Last event: {event_type}{RESET}")
    lines.append("")
    return "\n".join(lines)


def render_event_info(data: dict) -> str:
    """Render event metadata below the DAG."""
    status = data.get("status", "")
    phase = data.get("current_phase", "")
    timestamp = data.get("timestamp", "")
    pending = data.get("pending_decisions", 0)

    color = STATUS_COLORS.get(status, "")
    display_status = "awaiting approval" if status == "awaiting_human" else status

    lines = [""]
    lines.append(f"  Status: {color}{display_status}{RESET}  |  Phase: {phase}")
    if pending:
        lines.append(f"  {YELLOW}({pending} pending decision{'s' if pending != 1 else ''}){RESET}")
    if timestamp:
        time_part = timestamp.split("T")[-1].rstrip("Z")[:8]
        lines.append(f"  {DIM}Updated: {time_part}{RESET}")

    return "\n".join(lines)


def display_visualization(data: dict) -> None:
    """Display the DAG visualization with in-place update."""
    _clear_screen()

    pipeline_id = data.get("pipeline_id", "unknown")
    event_type = data.get("event_type", "")

    output = render_header(pipeline_id, event_type)

    viz = data.get("visualization", {})
    dag = viz.get("dag", "")
    if dag:
        output += dag
    else:
        output += "  Waiting for visualization data..."

    output += render_event_info(data)
    output += f"\n\n{DIM}Press Ctrl+C to stop watching{RESET}\n"

    _write(output)


# --- Watch Loop ---


def watch_pipeline(
    client: OrchClient,
    pipeline_id: str,
    pipeline_mode: str = "issue",
    issue_number: int | None = None,
) -> str:
    """Watch pipeline via SSE stream, handling HITL checkpoints.

    Returns the terminal status: "complete", "failed", "cancelled", or "error".
    """
    max_retries = 20
    retry_delay = 1.0  # seconds, doubles each retry up to max_delay
    max_delay = 30.0
    retries = 0

    while True:
        conn = None
        try:
            conn, response = client.stream_pipeline(pipeline_id)

            # Reset retry state on successful connection
            retries = 0
            retry_delay = 1.0

            last_status = None
            last_pending = 0

            for event_type, data in parse_sse_stream(response):
                if event_type == "error":
                    error = data.get("error", "Unknown error")
                    _write(f"{RED}Error: {error}{RESET}\n", file=sys.stderr)
                    return "error"

                if event_type == "done":
                    reason = data.get("reason", "unknown")
                    if reason in ("completed", "already_terminal"):
                        if last_status in ("failed", "cancelled"):
                            color = RED
                            verb = "failed" if last_status == "failed" else "was cancelled"
                            _write(f"\n{color}Pipeline {pipeline_id} {verb}.{RESET}\n")
                        else:
                            _write(f"\n{GREEN}Pipeline {pipeline_id} completed.{RESET}\n")
                    elif reason == "timeout":
                        _write(f"{YELLOW}Connection timed out. Reconnecting...{RESET}\n")
                        break  # Will reconnect via outer loop
                    else:
                        _write(f"\nStream ended: {reason}\n")
                    return last_status or "complete"

                last_status = data.get("status", last_status)
                if "pending_decisions" in data:
                    last_pending = data["pending_decisions"]
                elif last_pending:
                    data["pending_decisions"] = last_pending

                display_visualization(data)

                # Detect HITL checkpoint
                if last_status == "awaiting_human" and last_pending > 0:
                    # Fetch pending decisions
                    try:
                        decisions = client.list_decisions(pipeline_id, pending_only=True)
                    except OrchestratorError:
                        decisions = []

                    if decisions:
                        decision = decisions[0]
                        result = handle_hitl_checkpoint(
                            client,
                            pipeline_id,
                            decision,
                            pipeline_mode=pipeline_mode,
                            issue_number=issue_number,
                        )
                        if result == "cancelled":
                            return "cancelled"
                        # result == "resolved" → reconnect to SSE
                        break  # Break inner loop, reconnect

        except OrchestratorError as e:
            _write(f"\n{RED}{e}{RESET}\n", file=sys.stderr)
            return "error"
        except KeyboardInterrupt:
            _write(f"\n{DIM}Stopped watching.{RESET}\n")
            return "interrupted"
        except TimeoutError:
            retries += 1
            if retries > max_retries:
                _write(
                    f"{RED}Max reconnection attempts ({max_retries}) reached. Giving up.{RESET}\n",
                    file=sys.stderr,
                )
                return "error"
            _write(f"{YELLOW}Connection timed out. Reconnecting in {retry_delay:.0f}s...{RESET}\n")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_delay)
            continue  # Reconnect
        except ConnectionRefusedError:
            _write(
                f"{RED}Cannot connect to orchestrator at {client.base_url}\n"
                f"Is the orchestrator running?{RESET}\n",
                file=sys.stderr,
            )
            return "error"
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


# --- Local Mode ---


def run_local_mode(client: OrchClient) -> int:
    """Run egg-sdlc in local (prompt-driven) mode."""
    print(f"\n{BOLD}egg-sdlc: Local Mode{RESET}")
    print(f"{DIM}No issue number provided. Running prompt-driven pipeline.{RESET}\n")

    # Prompt for task description
    try:
        task = input(f"{BOLD}What would you like to build or change?{RESET}\n> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1

    if not task:
        _write(f"{RED}No task description provided.{RESET}\n", file=sys.stderr)
        return 1

    # Ask a clarifying question
    try:
        scope = input(
            f"\n{BOLD}Any specific files or areas of the codebase this should touch?{RESET}\n"
            f"{DIM}(press Enter to skip){RESET}\n> "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        scope = ""

    # Build refined prompt
    prompt = task
    if scope:
        prompt += f"\n\nScope: {scope}"

    print(f"\n{DIM}Creating local pipeline...{RESET}")

    try:
        pipeline = client.create_pipeline(mode="local", prompt=prompt)
    except OrchestratorError as e:
        _write(f"{RED}Failed to create pipeline: {e}{RESET}\n", file=sys.stderr)
        return 1

    pipeline_id = pipeline.get("id", pipeline.get("pipeline_id"))
    if not pipeline_id:
        _write(f"{RED}Pipeline created but no ID returned.{RESET}\n", file=sys.stderr)
        return 1

    print(f"  Pipeline: {BOLD}{pipeline_id}{RESET}")

    # Start pipeline
    try:
        client.start_pipeline(pipeline_id)
    except OrchestratorError as e:
        _write(f"{RED}Failed to start pipeline: {e}{RESET}\n", file=sys.stderr)
        return 1

    print(f"  {GREEN}Pipeline started.{RESET}\n")

    # Watch pipeline
    result = watch_pipeline(client, pipeline_id, pipeline_mode="local")
    return 0 if result == "complete" else 1


# --- Issue Mode ---


def run_issue_mode(client: OrchClient, issue_number: int, repo: str | None = None) -> int:
    """Run egg-sdlc in issue mode."""
    if not repo:
        _write(
            f"{RED}Repository is required. Use -r <repo_dir> or --repo <owner/repo>.{RESET}\n",
            file=sys.stderr,
        )
        return 1

    pipeline_id = f"issue-{issue_number}"
    branch = f"egg/issue-{issue_number}"

    print(f"\n{BOLD}egg-sdlc: Issue Mode{RESET}")
    print(f"  Issue:    #{issue_number}")
    print(f"  Repo:     {repo}")
    print(f"  Pipeline: {pipeline_id}")
    print(f"  Branch:   {branch}")

    # Create pipeline
    print(f"\n{DIM}Creating pipeline...{RESET}")
    try:
        client.create_pipeline(
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            mode="issue",
        )
    except OrchestratorError as e:
        if e.status_code == 409:
            # Pipeline already exists — check if it's still active
            print(f"  {YELLOW}Pipeline already exists. Checking status...{RESET}")
            try:
                status_data = client.get_pipeline_status(pipeline_id)
                status = status_data.get("status", "unknown")
                if status in ("complete", "failed", "cancelled"):
                    # Terminal state — delete and re-create
                    print(f"  Pipeline was {status}. Restarting...")
                    client.delete_pipeline(pipeline_id)
                    client.create_pipeline(
                        issue_number=issue_number,
                        repo=repo,
                        branch=branch,
                        mode="issue",
                    )
                    client.start_pipeline(pipeline_id)
                    print(f"  {GREEN}Pipeline restarted.{RESET}")
                else:
                    print(f"  Pipeline status: {status}. Attaching to watch loop...")
            except OrchestratorError as e2:
                _write(f"{RED}Failed to restart pipeline: {e2}{RESET}\n", file=sys.stderr)
                return 1
        else:
            _write(f"{RED}Failed to create pipeline: {e}{RESET}\n", file=sys.stderr)
            return 1
    else:
        # Start pipeline
        try:
            client.start_pipeline(pipeline_id)
            print(f"  {GREEN}Pipeline started.{RESET}")
        except OrchestratorError as e:
            if e.status_code == 409:
                print(f"  {YELLOW}Pipeline already running.{RESET}")
            else:
                _write(f"{RED}Failed to start pipeline: {e}{RESET}\n", file=sys.stderr)
                return 1

    print()

    # Watch pipeline
    result = watch_pipeline(
        client,
        pipeline_id,
        pipeline_mode="issue",
        issue_number=issue_number,
    )
    return 0 if result == "complete" else 1


# --- Entry Point ---


def main() -> None:
    """Main entry point for egg-sdlc CLI."""
    parser = argparse.ArgumentParser(
        description="Interactive SDLC pipeline CLI with DAG visualization and HITL checkpoints",
        prog="egg-sdlc",
    )
    parser.add_argument(
        "issue_number_pos",
        nargs="?",
        type=int,
        help="GitHub issue number (positional, omit for local/prompt mode)",
    )
    parser.add_argument(
        "-i",
        "--issue",
        type=int,
        metavar="NUM",
        help="GitHub issue number",
    )
    parser.add_argument(
        "-r",
        "--repo",
        metavar="NAME",
        help="Repository directory name under ~/repos/ (e.g. 'egg'). "
        "Also accepts owner/repo format for direct specification.",
    )

    args = parser.parse_args()

    # Resolve issue number: -i/--issue takes precedence over positional
    issue_number = args.issue or args.issue_number_pos

    # Handle SIGPIPE gracefully
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    # Resolve repo: if it looks like a directory name (no slash), resolve it
    repo = None
    if args.repo:
        if "/" in args.repo:
            # Already in owner/repo format
            repo = args.repo
        else:
            # Directory name — resolve to owner/repo via EGG_REPOS
            repo = _resolve_repo_dir(args.repo)
            if not repo:
                _write(
                    f"{RED}Cannot resolve repo '{args.repo}'. "
                    f"No matching entry in mounted repos.{RESET}\n",
                    file=sys.stderr,
                )
                sys.exit(1)

    # Create orchestrator client
    client = OrchClient()

    # Verify orchestrator is reachable
    if not client.health_check():
        _write(
            f"{RED}Cannot reach orchestrator at {client.base_url}\n"
            f"Is the orchestrator running? Try: docker-compose up -d orchestrator{RESET}\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Dispatch to mode
    if issue_number:
        exit_code = run_issue_mode(client, issue_number, repo)
    else:
        exit_code = run_local_mode(client)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
