"""Commit a timed-out agent's working tree before the process exits (#3658).

When the session budget expires the SDK call is cancelled and the CLI returns;
nothing commits, stashes, or flushes the tree on the way out. #3644 makes the
*next* respawn survivable (its worktree re-attach snapshots the dirty state
before resetting), which turns the boundary from destruction into recovery — but
recovery through a machine snapshot taken minutes later by a different process,
with no marker of where the agent actually stopped.

This module closes the gap at the source: at the moment the clock runs out, in
the pod, with the worktree exactly as the agent left it, take the same
``[salvage]`` snapshot #3644 takes on re-attach. The boundary becomes *clean*
rather than merely recoverable, and the commit lands with the agent's own
timestamp so a human reading the branch can see when work stopped.

Deliberately dependency-free (``subprocess`` + ``pathlib`` only) — it runs in
the sandbox container, which has no orchestrator packages — and best-effort
throughout: every failure returns ``None`` and is logged, because a checkpoint
that raises would replace "the agent ran out of time" with "the agent crashed"
in the exit code the orchestrator reads.

The commit is purely local. Nothing here pushes, so it never touches the gateway
policy surface; the commit rides out on the next invocation's normal push.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from egg_agent._logging import resolve_logger

logger = resolve_logger("egg_agent.checkpoint", __name__)

# Identity + message for the synthetic snapshot. The ``[salvage]`` prefix is the
# repo-wide convention (``agent_salvage._UNCOMMITTED_SALVAGE_MESSAGE``, the
# #3639 re-attach snapshot): one grep finds every machine-made working-tree
# commit regardless of which path took it. The values are duplicated rather than
# imported because those constants live in the orchestrator package, which is
# not installed in the sandbox image.
_CHECKPOINT_COMMIT_NAME = "egg-salvage"
_CHECKPOINT_COMMIT_EMAIL = "egg-salvage@localhost"
_CHECKPOINT_MESSAGE = (
    "[salvage] session-timeout working-tree state (#3658)\n"
    "\n"
    "Snapshot taken in-pod by the agent CLI when the session's wall-clock\n"
    "budget expired, capturing the tree exactly as the agent left it. This is\n"
    "a machine commit, not the agent's own: it marks where the session\n"
    "boundary fell, and its contents are mid-edit by construction. Review it\n"
    "before building on it; amend or reset it freely.\n"
)
_CHECKPOINT_PARTIAL_SUFFIX = (
    "\nNOTE: `git add -A` reported errors, so this snapshot may be PARTIAL.\n"
)

# The checkpoint is on unless explicitly disabled (same OFF-switch semantics as
# the deadline banner: a garbled value must not silently drop the snapshot).
CHECKPOINT_ENV = "EGG_SESSION_TIMEOUT_CHECKPOINT"

_GIT_TIMEOUT_SECONDS = 30


def is_checkpoint_disabled() -> bool:
    """Return True iff the timeout checkpoint is switched off."""
    return os.environ.get(CHECKPOINT_ENV, "").strip().lower() in {"0", "false", "no", "off"}


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one git command in *cwd*, capturing output, never raising on rc.

    The three pinned config values mirror ``agent_salvage._run_git``, for the
    same reasons:

    * ``core.hooksPath=/dev/null`` — this is a machine snapshot of a tree the
      agent may have left mid-edit; a repo pre-commit hook run against it would
      fail or mutate it, and either outcome loses the work.
    * ``commit.gpgsign=false`` — there is no signing key in the sandbox, so a
      worktree that inherited ``commit.gpgsign=true`` from its clone would fail
      every checkpoint commit.
    * ``core.quotePath=true`` plus ``errors="replace"`` on the decode — ``git
      add`` echoes raw paths in stderr messages ``quotePath`` does not cover, and
      under a strict decode a filename whose bytes are not valid UTF-8 raises
      ``UnicodeDecodeError`` from inside :func:`subprocess.run` itself. That is
      the #3639 shape: one hostile filename losing the whole tree.
    """
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.quotePath=true",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _resolve_repo_path(repo_path: str | os.PathLike[str] | None) -> Path:
    """Resolve the worktree to snapshot: explicit arg > EGG_REPO_PATH > cwd.

    The same precedence ``run_agent`` uses to resolve the SDK's cwd, so the
    checkpoint always lands in the tree the agent was actually editing.
    """
    if repo_path is not None:
        return Path(repo_path)
    return Path(os.environ.get("EGG_REPO_PATH") or os.getcwd())


def checkpoint_working_tree(repo_path: str | os.PathLike[str] | None = None) -> str | None:
    """Commit the working tree as a ``[salvage]`` snapshot; return its SHA.

    Returns ``None`` — never raises — when the checkpoint is disabled, the path
    is not a git worktree, the tree is clean, nothing reached the index, or any
    git command fails. A clean tree is the common case for a well-behaved agent
    that committed before the deadline, and it is a no-op, not a failure.
    """
    if is_checkpoint_disabled():
        return None
    repo = _resolve_repo_path(repo_path)
    try:
        inside = _run_git("rev-parse", "--is-inside-work-tree", cwd=repo)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            logger.debug("Timeout checkpoint: not a git worktree; skipping", repo_path=str(repo))
            return None

        status = _run_git("status", "--porcelain", cwd=repo)
        if status.returncode != 0 or not status.stdout.strip():
            logger.debug("Timeout checkpoint: nothing to snapshot", repo_path=str(repo))
            return None

        add = _run_git("add", "-A", "--ignore-errors", cwd=repo)
        partial = add.returncode != 0
        if partial:
            # Not fatal, and deliberately not a bail-out: per ``git-add(1)`` one
            # unindexable entry (an unreadable file, a fifo, a missing filter)
            # exits non-zero even under ``--ignore-errors``, after skipping it.
            # Returning here would discard the other N-1 files this snapshot
            # exists to capture — the same call the #3639 re-attach path makes.
            logger.warning(
                "Timeout checkpoint: git add -A reported errors; "
                "committing whatever reached the index",
                repo_path=str(repo),
                stderr=(add.stderr or "").strip(),
            )

        staged = _run_git("diff", "--cached", "--name-only", cwd=repo)
        if staged.returncode == 0 and not (staged.stdout or "").strip():
            # Separates "the add staged nothing" (ignored files, submodule-only
            # dirt) from "the commit failed", which otherwise present to an
            # operator as the same confusing `commit failed: nothing to commit`.
            logger.warning(
                "Timeout checkpoint: nothing staged; skipping the snapshot",
                repo_path=str(repo),
                add_failed=partial,
            )
            return None

        commit = _run_git(
            "-c",
            f"user.name={_CHECKPOINT_COMMIT_NAME}",
            "-c",
            f"user.email={_CHECKPOINT_COMMIT_EMAIL}",
            "commit",
            "-m",
            _CHECKPOINT_MESSAGE + (_CHECKPOINT_PARTIAL_SUFFIX if partial else ""),
            cwd=repo,
        )
        if commit.returncode != 0:
            logger.warning(
                "Timeout checkpoint: commit failed",
                repo_path=str(repo),
                stderr=(commit.stderr or "").strip(),
            )
            return None

        head = _run_git("rev-parse", "HEAD", cwd=repo)
        sha = (head.stdout or "").strip() if head.returncode == 0 else None
        logger.info(
            "Timeout checkpoint: committed the working tree at the session boundary",
            repo_path=str(repo),
            commit=sha,
            partial=partial,
        )
        return sha
    # Broader than the failure classes above need. The docstring promises this
    # never raises, and what would break that promise is not only a subprocess
    # error: a hostile worktree can surface decode / OS errors from inside
    # ``subprocess.run``. Letting one escape here would turn "ran out of time"
    # into an unclassifiable crash exit code, which is precisely the confusion
    # this work removes.
    except Exception as e:  # noqa: BLE001 — checkpointing is strictly best-effort
        logger.warning(
            "Timeout checkpoint: snapshotting the working tree raised; continuing",
            repo_path=str(repo),
            # The class name is the one field that separates "the worktree was
            # hostile" from "this code is broken".
            error_type=type(e).__name__,
            error=str(e),
        )
        return None
