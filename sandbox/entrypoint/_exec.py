"""Subprocess exec path with stderr capture and CWD handling."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from ._config import Config, Logger
from ._core import _SUBPROCESS_STDERR_LOG, _read_subprocess_stderr_tail
from ._timing import _startup_timer


def _tee_stderr_to_file(
    process: subprocess.Popen[bytes],
    log_path: Path,
    max_lines: int = 500,
) -> None:
    """Tee subprocess stderr to both sys.stderr and a bounded log file.

    Runs in a background thread. Reads from process.stderr (PIPE) and
    writes each line to the container's stderr in real time.  Only the
    last *max_lines* lines are kept in memory and flushed to *log_path*
    when the stream ends, preventing unbounded file growth.
    """
    from collections import deque

    try:
        stderr_out: Any = getattr(sys.stderr, "buffer", sys.stderr)
        ring: deque[bytes] = deque(maxlen=max_lines)
        if process.stderr is None:
            return
        while True:
            line = process.stderr.readline()
            if not line:
                break
            stderr_out.write(line)
            stderr_out.flush()
            ring.append(line)
        # Write the bounded tail to disk for _read_subprocess_stderr_tail()
        # This is best-effort — stderr was already forwarded in real time.
        # During container shutdown, filesystems may become read-only.
        try:
            with open(log_path, "wb") as log_file:
                for saved_line in ring:
                    log_file.write(saved_line)
        except OSError:
            pass
    except Exception as exc:
        # Best-effort diagnostic — log so failures aren't completely silent.
        try:
            print(
                f"[DEBUG] _tee_stderr_to_file failed: {exc}",
                file=sys.stderr,
            )
            sys.stderr.flush()
        except Exception:
            pass


def _run_with_stderr_capture(
    cmd: list[str],
    env: dict[str, str],
    logger: Logger,
) -> int:
    """Run a subprocess, capturing stderr to a log file while passing it through.

    Returns the subprocess exit code. Stderr is tee'd to both the container's
    stderr (for docker logs) and _SUBPROCESS_STDERR_LOG (for error signals).
    """
    process = subprocess.Popen(
        cmd,
        env=env,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=subprocess.PIPE,
    )

    tee_thread = threading.Thread(
        target=_tee_stderr_to_file,
        args=(process, _SUBPROCESS_STDERR_LOG),
        daemon=True,
    )
    tee_thread.start()
    process.wait()
    tee_thread.join(timeout=5)

    exit_code = process.returncode

    if exit_code != 0:
        # Log error with subprocess stderr context so it's visible in docker logs
        stderr_tail = _read_subprocess_stderr_tail(30)
        if stderr_tail:
            logger.error(f"Subprocess failed (exit code {exit_code}). Last stderr:\n{stderr_tail}")
        else:
            logger.error(f"Subprocess failed (exit code {exit_code}) with no stderr output")

    return exit_code


def _exclude_from_git(file_path: Path) -> None:
    """Add a file to .git/info/exclude so git ignores it without modifying .gitignore.

    Walks up from file_path to find the nearest .git entry (directory or
    worktree file). If found, appends the relative path to the git metadata
    directory's info/exclude file (a repo-local ignore mechanism that is not
    committed).

    Handles both regular repos (.git is a directory) and worktrees (.git is
    a file containing ``gitdir: <path>``).

    Note: In production containers, .git is typically shadowed with /dev/null
    (orchestrator mode) or a tmpfs (CLI mode), so this function may be a no-op.
    The authoritative symlink filter is in gateway/post_agent_commit.py which
    runs on the host side. This function provides defense-in-depth for cases
    where .git is accessible (e.g., testing, future environments).

    Silently does nothing if no .git entry is found or if .git is not a
    directory or worktree file (e.g., /dev/null bind mount).
    """
    parent = file_path.parent
    while parent != parent.parent:
        git_entry = parent / ".git"
        git_meta_dir: Path | None = None

        if git_entry.is_dir():
            git_meta_dir = git_entry
        elif git_entry.is_file():
            # Worktree: .git is a file containing "gitdir: <path>"
            try:
                content = git_entry.read_text().strip()
                if content.startswith("gitdir:"):
                    gitdir_path = content[len("gitdir:") :].strip()
                    resolved = Path(gitdir_path)
                    if not resolved.is_absolute():
                        resolved = (parent / resolved).resolve()
                    if resolved.is_dir():
                        git_meta_dir = resolved
            except OSError, ValueError:
                pass

        if git_meta_dir is not None:
            exclude_file = git_meta_dir / "info" / "exclude"
            exclude_file.parent.mkdir(parents=True, exist_ok=True)
            rel_path = str(file_path.relative_to(parent))
            # Check if already excluded
            if exclude_file.exists():
                existing = exclude_file.read_text()
                if rel_path in existing.splitlines():
                    return
            with open(exclude_file, "a") as f:
                f.write(f"\n{rel_path}\n")
            return
        parent = parent.parent


def _chdir_to_single_repo(config: Config) -> None:
    """Change to repos directory, entering the single repo if exactly one exists.

    If repos_dir contains exactly one git repository, chdir into it and set
    EGG_REPO_PATH so git/gh commands auto-detect the repository context.
    Falls back to user home if repos_dir doesn't exist.

    After setting CWD, creates project-level ``CLAUDE.md`` and ``AGENTS.md``
    symlinks pointing to the global ``~/.claude/CLAUDE.md`` so Claude Code
    detects the rules in the working directory (suppresses the "Run /init"
    welcome message) and AGENTS.md-aware tools find the same content.

    The symlinks are container-local artifacts and must not be committed to
    user repositories — the target is an absolute container path
    (``/home/egg/.claude/CLAUDE.md``) that would be broken on any host
    checkout. ``_exclude_from_git()`` writes both names to
    ``.git/info/exclude`` so ``git status`` ignores them.

    Known limitation: nothing prevents an agent from explicitly committing
    one of these symlinks via ``git add CLAUDE.md`` / ``git add AGENTS.md``
    followed by ``git commit``. ``gateway/post_agent_commit.py`` no longer
    auto-commits (it has been a logged no-op since #1481, when per-agent
    worktrees made auto-commit unnecessary), and the gateway's commit-time
    phase validation does not check for symlink content. Risk is low —
    agent instructions consistently use ``git add <files>``, not
    ``git add -A`` — but nonzero. Protection here is the per-agent
    cleanup convention plus ``.git/info/exclude``.

    Cleanup in ``setup_agent_rules()`` is target-aware in repo subdirs: it
    only unlinks symlinks that resolve to the global ``~/.claude/CLAUDE.md``,
    so a repo that legitimately commits ``AGENTS.md`` as a relative symlink
    to its own ``CLAUDE.md`` is preserved.
    """
    if config.repos_dir.exists():
        os.chdir(config.repos_dir)
        # If there's exactly one repo, cd into it so git/gh commands work
        subdirs = [d for d in config.repos_dir.iterdir() if d.is_dir() and (d / ".git").exists()]
        if len(subdirs) == 1:
            os.chdir(subdirs[0])
            os.environ["EGG_REPO_PATH"] = str(subdirs[0])
    else:
        os.chdir(config.user_home)

    # Create project-level CLAUDE.md / AGENTS.md symlinks so Claude Code
    # (and other AGENTS.md-aware tools) detect the rules in the working
    # directory (and suppresses Claude's "Run /init" welcome message).
    # The actual rules live in ~/.claude/CLAUDE.md (global config).
    #
    # Note: setup_agent_rules() cleans up stale CLAUDE.md/AGENTS.md files from
    # previous container runs earlier in startup. These symlinks are re-created
    # fresh each time the session starts — the cleanup and creation are
    # intentionally separate phases.
    global_claude_md = config.claude_dir / "CLAUDE.md"
    if global_claude_md.exists():
        for name in ("CLAUDE.md", "AGENTS.md"):
            cwd_link = Path.cwd() / name
            if not cwd_link.exists() and not cwd_link.is_symlink():
                cwd_link.symlink_to(global_claude_md)
                # If CWD is inside a git repo, exclude the symlink from git
                # tracking to prevent it from being committed (the symlink
                # target is a container-local absolute path that would be
                # broken elsewhere).
                _exclude_from_git(cwd_link)


def run_exec(config: Config, logger: Logger, args: list[str]) -> int:
    """Run a command in exec mode.

    Uses subprocess.Popen() to maintain control after process exits,
    enabling completion signaling back to orchestrator.

    Returns:
        Exit code from the subprocess
    """
    # Change to repos directory (same as interactive mode) so that tools
    # like `gh repo view` can auto-detect the repository context.
    _chdir_to_single_repo(config)

    env = os.environ.copy()
    # Remove launcher secret — privileged credential not for Claude's use
    env.pop("EGG_LAUNCHER_SECRET", None)

    # Bypass the BASH_COMMAND_TIMEOUT wrapper for the top-level command.
    #
    # setup_command_timeout() replaces /bin/bash with a wrapper that kills
    # "bash -c ..." invocations after BASH_COMMAND_TIMEOUT seconds (default
    # 300).  This is correct for individual commands Claude runs via the
    # Bash tool, but the top-level exec command (e.g. the consensus wrapper
    # script) is a long-running process that must not be killed.  Using
    # bash.real here bypasses the per-command timeout for the top-level
    # invocation only — Claude's internal bash commands still go through
    # /bin/bash and remain subject to the timeout.
    real_bash = Path("/bin/bash.real")
    if real_bash.exists() and args and args[0] in ("bash", "/bin/bash"):
        args = [str(real_bash)] + args[1:]

    # Print timing summary before exec
    _startup_timer.print_summary()

    # Launch via gosu, capturing stderr to log file for error reporting
    return _run_with_stderr_capture(
        ["gosu", f"{config.runtime_uid}:{config.runtime_gid}"] + args,
        env=env,
        logger=logger,
    )
