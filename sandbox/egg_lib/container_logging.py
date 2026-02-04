"""Container log persistence and correlation for jib.

This module handles saving container logs, correlation tracking,
and log index management.
"""

import contextlib
import fcntl
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .output import get_quiet_mode, info, warn

# Default log directory for container logs
CONTAINER_LOGS_DIR = Path.home() / ".egg-sharing" / "container-logs"


def generate_container_id() -> str:
    """Generate unique container ID based on timestamp and process ID"""
    import time

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    pid = os.getpid()
    return f"jib-{timestamp}-{pid}"


def get_docker_log_config(container_id: str, task_id: str | None = None) -> list:
    """Generate Docker logging configuration arguments.

    Uses the json-file logging driver with:
    - Log rotation (max 10MB per file, 5 files)
    - Correlation labels for easy searching
    - Timestamps included

    Args:
        container_id: Unique container identifier
        task_id: Optional task ID for correlation (e.g., "task-20251129-222239")

    Returns:
        List of Docker command arguments for logging configuration
    """
    # Ensure container logs directory exists
    CONTAINER_LOGS_DIR.mkdir(parents=True, exist_ok=True)

    log_args = [
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=5",
        # Add labels for correlation - these appear in docker inspect
        "--label",
        f"jib.container_id={container_id}",
    ]

    if task_id:
        log_args.extend(["--label", f"jib.task_id={task_id}"])

    return log_args


def extract_task_id_from_command(command: list[str]) -> str | None:
    """Extract task ID from the command if it's processing a task file.

    Looks for patterns like:
    - incoming-processor.py /path/to/task-20251129-222239.md
    - task-20251129-222239

    Args:
        command: Command list passed to jib --exec

    Returns:
        Task ID if found, None otherwise
    """
    task_pattern = r"(task-\d{8}-\d{6})"

    for arg in command:
        match = re.search(task_pattern, arg)
        if match:
            return match.group(1)

    return None


def extract_thread_ts_from_task_file(task_file_path: str) -> str | None:
    """Extract thread_ts from a task file's YAML frontmatter.

    Task files contain YAML frontmatter like:
        ---
        task_id: "task-20251129-222239"
        thread_ts: "1764483758.159619"
        ---

    Args:
        task_file_path: Path to the task file (may be container path)

    Returns:
        Thread timestamp if found, None otherwise
    """
    # Convert container path back to host path
    host_path = task_file_path
    if "/sharing/" in task_file_path:
        # Container path like /home/user/sharing/incoming/task.md
        # -> Host path like ~/.egg-sharing/incoming/task.md
        parts = task_file_path.split("/sharing/", 1)
        if len(parts) == 2:
            host_path = str(Path.home() / ".egg-sharing" / parts[1])

    try:
        path = Path(host_path)
        if path.exists():
            content = path.read_text()
            # Look for thread_ts in YAML frontmatter
            match = re.search(r'thread_ts:\s*["\']?(\d+\.\d+)["\']?', content)
            if match:
                return match.group(1)
    except Exception:
        pass

    return None


def update_log_index(
    container_id: str,
    task_id: str | None = None,
    thread_ts: str | None = None,
    log_file: str | None = None,
) -> None:
    """Update the log index file with correlation information.

    The log index enables quick lookups:
    - task_id -> container_id
    - thread_ts -> task_id
    - List of all recent container runs

    Uses file locking to prevent race conditions when multiple containers
    finish simultaneously.

    Args:
        container_id: Docker container ID
        task_id: Optional task ID (e.g., "task-20251129-222239")
        thread_ts: Optional Slack thread timestamp
        log_file: Path to the log file
    """
    index_file = CONTAINER_LOGS_DIR / "log-index.json"
    CONTAINER_LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Use file locking to prevent concurrent modifications
    # Open in 'a+' mode to create file if it doesn't exist
    with open(index_file, "a+") as f:
        # Acquire exclusive lock
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            # Seek to beginning to read
            f.seek(0)
            content = f.read()

            # Load existing index
            index = {"task_to_container": {}, "thread_to_task": {}, "entries": []}
            if content:
                with contextlib.suppress(Exception):
                    index = json.loads(content)

            # Update correlation maps
            if task_id:
                index["task_to_container"][task_id] = container_id
            if thread_ts and task_id:
                index["thread_to_task"][thread_ts] = task_id

            # Add entry
            entry = {
                "container_id": container_id,
                "task_id": task_id,
                "thread_ts": thread_ts,
                "log_file": str(log_file) if log_file else None,
                "timestamp": datetime.now().isoformat(),
            }
            index["entries"].append(entry)

            # Keep last 1000 entries
            if len(index["entries"]) > 1000:
                index["entries"] = index["entries"][-1000:]

            # Write updated index
            f.seek(0)
            f.truncate()
            f.write(json.dumps(index, indent=2))
        finally:
            # Release lock
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def save_container_logs(
    container_id: str,
    task_id: str | None = None,
    thread_ts: str | None = None,
) -> Path | None:
    """Save Docker container logs to persistent storage.

    Uses `docker logs` to capture all container output and saves it to
    ~/.egg-sharing/container-logs/{container_id}.log

    Also creates a symlink from task_id.log -> container_id.log for easy lookup,
    and updates the log index for correlation searches.

    Args:
        container_id: Docker container ID/name
        task_id: Optional task ID for symlink creation
        thread_ts: Optional Slack thread timestamp for index

    Returns:
        Path to the saved log file, or None if saving failed
    """
    quiet = get_quiet_mode()
    CONTAINER_LOGS_DIR.mkdir(parents=True, exist_ok=True)

    log_file = CONTAINER_LOGS_DIR / f"{container_id}.log"

    try:
        # Get container logs using docker logs command
        result = subprocess.run(
            ["docker", "logs", container_id],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        # Check log size before writing to prevent disk space exhaustion
        max_log_size = 100 * 1024 * 1024  # 100MB
        total_log_size = len(result.stdout) + len(result.stderr)
        if total_log_size > max_log_size:
            warn(f"Container logs exceed {max_log_size / (1024 * 1024):.0f}MB, truncating...")
            # Truncate stderr/stdout proportionally
            if result.stdout:
                result.stdout = result.stdout[: max_log_size // 2] + "\n\n[... truncated ...]\n"
            if result.stderr:
                result.stderr = result.stderr[: max_log_size // 2] + "\n\n[... truncated ...]\n"

        # Write logs (both stdout and stderr)
        with open(log_file, "w") as f:
            f.write(f"=== Container: {container_id} ===\n")
            f.write(f"=== Saved: {datetime.now().isoformat()} ===\n")
            if task_id:
                f.write(f"=== Task ID: {task_id} ===\n")
            if thread_ts:
                f.write(f"=== Thread TS: {thread_ts} ===\n")
            f.write("=" * 50 + "\n\n")

            if result.stdout:
                f.write("=== STDOUT ===\n")
                f.write(result.stdout)
                f.write("\n")

            if result.stderr:
                f.write("\n=== STDERR ===\n")
                f.write(result.stderr)

        # Create symlink from task_id if provided
        if task_id:
            task_log_link = CONTAINER_LOGS_DIR / f"{task_id}.log"
            # Remove existing symlink if present
            if task_log_link.is_symlink():
                task_log_link.unlink()
            # Create relative symlink
            task_log_link.symlink_to(f"{container_id}.log")

        # Update log index for correlation lookups
        update_log_index(container_id, task_id, thread_ts, str(log_file))

        if not quiet:
            info(f"Container logs saved: {log_file}")
            if task_id:
                info(f"  Symlink: {task_id}.log -> {container_id}.log")

        return log_file

    except subprocess.TimeoutExpired:
        warn("Timed out getting container logs")
    except FileNotFoundError:
        # Container doesn't exist or already removed
        pass
    except Exception as e:
        warn(f"Failed to save container logs: {e}")

    return None
