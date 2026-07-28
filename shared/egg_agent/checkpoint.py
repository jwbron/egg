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

**Every git call here goes through the gateway.** This module is a near port of
``orchestrator/agent_salvage.py``, but the two run on opposite sides of the
sandbox boundary and that changes what "run git" means. In the pod the real
binary is relocated to ``/opt/.egg-internal/git`` and ``git`` resolves to
``sandbox/scripts/git``, the policy wrapper — so "the commit is local, it never
pushes" does *not* imply "it never touches policy". Three constraints follow,
and each one silently no-op'd the first cut of this module:

* **Repo discovery is useless in-pod.** ``rev-parse --is-inside-work-tree`` (and
  every other discovery flag) is routed *around* the gateway to the real binary,
  which sees a ``.git`` shadowed by a tmpfs mount and reports "not a git
  repository". ``git status --porcelain`` goes through the gateway and answers
  correctly, so it is the repo check here — one command that also answers "is
  the tree dirty".
* **Flags are allowlisted per subcommand** (``gateway/git_client/_policy.py``).
  Anything outside the list is a hard rejection, not a warning.
* **Global ``-c key=value`` overrides are dropped by the wrapper** along with
  their values, so identity, hooks, and signing cannot be pinned that way from
  in-pod. Identity rides on ``commit --author=`` (allowlisted) plus the
  ``GIT_*`` env vars; see :func:`_run_git`.

The commit is still purely local — nothing here pushes — so it rides out on the
next invocation's normal push.
"""

from __future__ import annotations

import os
import re
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
_CHECKPOINT_AUTHOR = f"{_CHECKPOINT_COMMIT_NAME} <{_CHECKPOINT_COMMIT_EMAIL}>"
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
_CHECKPOINT_PHASE_SUFFIX = (
    "\nNOTE: the gateway's phase restrictions rejected some of the staged\n"
    "paths, so this snapshot is PARTIAL — they were unstaged and are still\n"
    "uncommitted in the working tree. See the checkpoint's warning log for\n"
    "the list.\n"
)

# The gateway's phase-restriction 403 for ``git commit``
# (``gateway/gateway/_git_execute.py``) names the offending paths in its message,
# which the wrapper prints to stderr verbatim. That message is the only channel
# through which the blocked set reaches the pod — the wrapper surfaces the HTTP
# body as text, not structure — so the checkpoint parses it to retry with those
# paths unstaged. Parsing another process's prose is fragile by nature, and it is
# made safe by construction here: the parsed names are intersected with the paths
# we ourselves staged (:func:`_phase_blocked_paths`), so a message drift can only
# ever cost us the retry, never unstage something unexpected.
_PHASE_BLOCK_PATTERN = re.compile(r"cannot modify:\s*(.+?)\.\s+Unstage the blocked files")

# The checkpoint is on unless explicitly disabled (same OFF-switch semantics as
# the deadline banner: a garbled value must not silently drop the snapshot).
CHECKPOINT_ENV = "EGG_SESSION_TIMEOUT_CHECKPOINT"

_GIT_TIMEOUT_SECONDS = 30


def is_checkpoint_disabled() -> bool:
    """Return True iff the timeout checkpoint is switched off."""
    return os.environ.get(CHECKPOINT_ENV, "").strip().lower() in {"0", "false", "no", "off"}


def _git_env() -> dict[str, str]:
    """Return the subprocess environment carrying the snapshot's identity.

    ``-c user.name=`` / ``-c user.email=`` cannot be used: the sandbox wrapper
    strips every global ``-c`` and its value before forwarding, so in-pod they
    reach nothing. ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` set the same identity
    for a *direct* git (this module runs unwrapped outside the sandbox too, and
    in the tests), and ``commit --author=`` carries the author across the gateway
    for the in-pod path. The committer of a gateway-executed commit is whatever
    ambient identity the gateway's git resolves — unavoidable from this side, and
    harmless: ``[salvage]`` in the message and ``egg-salvage`` in the author are
    what the recovery greps key on.
    """
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = _CHECKPOINT_COMMIT_NAME
    env["GIT_AUTHOR_EMAIL"] = _CHECKPOINT_COMMIT_EMAIL
    env["GIT_COMMITTER_NAME"] = _CHECKPOINT_COMMIT_NAME
    env["GIT_COMMITTER_EMAIL"] = _CHECKPOINT_COMMIT_EMAIL
    return env


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one git command in *cwd*, capturing output, never raising on rc.

    The pinned config values mirror ``agent_salvage._run_git``. In the sandbox
    the wrapper drops them (see the module docstring) and the gateway pins the
    equivalents itself — ``core.hooksPath=/dev/null`` in
    ``gateway/git_client/_remote.py::git_cmd`` and a forced ``--no-verify`` on
    ``commit`` in ``_git_execute.py`` — so in-pod they are inert rather than
    load-bearing. They are kept because this module also runs against a direct
    git (outside the sandbox, and in the tests), where they are the only thing
    providing:

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
      the #3639 shape: one hostile filename losing the whole tree. The decode
      argument is ours, not git's, so it survives the wrapper.
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
        env=_git_env(),
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


def _staged_paths(repo: Path) -> list[str]:
    """Return the paths currently in the index, or ``[]`` if none/unreadable."""
    staged = _run_git("diff", "--cached", "--name-only", cwd=repo)
    if staged.returncode != 0:
        return []
    return [line for line in (staged.stdout or "").splitlines() if line.strip()]


def _phase_blocked_paths(stderr: str, staged: list[str]) -> list[str]:
    """Return the staged paths the gateway's phase 403 named, else ``[]``.

    Intersecting the parsed names with what we actually staged is what makes
    reading another process's message safe: if the gateway's wording drifts, or a
    path with a comma in it splits wrong, the intersection shrinks and the retry
    is skipped — it can never unstage a path this checkpoint did not stage.
    """
    match = _PHASE_BLOCK_PATTERN.search(stderr)
    if not match:
        return []
    named = {part.strip() for part in match.group(1).split(",") if part.strip()}
    return [path for path in staged if path in named]


def _commit(
    repo: Path, *, partial: bool, phase_partial: bool = False
) -> subprocess.CompletedProcess[str]:
    """Commit the index as the ``[salvage]`` snapshot.

    ``--author=`` rather than ``-c user.name=`` / ``-c user.email=``: the sandbox
    wrapper drops global ``-c`` overrides, and ``--author`` is on the gateway's
    ``commit`` flag allowlist, so this is the one form that survives both paths.
    """
    message = _CHECKPOINT_MESSAGE
    if partial:
        message += _CHECKPOINT_PARTIAL_SUFFIX
    if phase_partial:
        message += _CHECKPOINT_PHASE_SUFFIX
    return _run_git(
        "commit",
        f"--author={_CHECKPOINT_AUTHOR}",
        "-m",
        message,
        cwd=repo,
    )


def checkpoint_working_tree(repo_path: str | os.PathLike[str] | None = None) -> str | None:
    """Commit the working tree as a ``[salvage]`` snapshot; return its SHA.

    Returns ``None`` — never raises — when the checkpoint is disabled, the path
    is not a git worktree, the tree is clean, nothing reached the index, or any
    git command fails. A clean tree is the common case for a well-behaved agent
    that committed before the deadline, and it is a no-op, not a failure.

    Every declining path other than "clean tree" logs at ``warning``: a silent
    ``debug`` is how a checkpoint that never ran once in production hid behind a
    green test suite, so an operator must be able to see the snapshot decline.
    """
    if is_checkpoint_disabled():
        return None
    repo = _resolve_repo_path(repo_path)
    try:
        # Doubles as the repo check. ``rev-parse --is-inside-work-tree`` cannot
        # do this job in the pod (module docstring); ``status`` routes through
        # the gateway and returns non-zero for a non-repo, so a single call
        # answers both "is this a worktree" and "is there anything to snapshot".
        status = _run_git("status", "--porcelain", cwd=repo)
        if status.returncode != 0:
            logger.warning(
                "Timeout checkpoint: cannot read the working tree; skipping the snapshot",
                repo_path=str(repo),
                stderr=(status.stderr or "").strip(),
            )
            return None
        if not (status.stdout or "").strip():
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
                "retrying without --ignore-errors and committing whatever reaches the index",
                repo_path=str(repo),
                stderr=(add.stderr or "").strip(),
            )
            # A gateway older than the allowlist entry for ``--ignore-errors``
            # rejects the flag outright and stages nothing. The plain form is on
            # every version of the allowlist, so this retry keeps the checkpoint
            # working across a sandbox/gateway version skew; on a genuinely
            # hostile tree it fails too and ``partial`` stays true, with the
            # first call's index writes already in place.
            partial = _run_git("add", "-A", cwd=repo).returncode != 0

        staged = _staged_paths(repo)
        if not staged:
            # Separates "the add staged nothing" (ignored files, submodule-only
            # dirt) from "the commit failed", which otherwise present to an
            # operator as the same confusing `commit failed: nothing to commit`.
            logger.warning(
                "Timeout checkpoint: nothing staged; skipping the snapshot",
                repo_path=str(repo),
                add_failed=partial,
            )
            return None

        commit = _commit(repo, partial=partial)
        if commit.returncode != 0:
            blocked = _phase_blocked_paths(commit.stderr or "", staged)
            if not blocked:
                logger.warning(
                    "Timeout checkpoint: commit failed",
                    repo_path=str(repo),
                    stderr=(commit.stderr or "").strip(),
                )
                return None
            # `git add -A` stages everything the agent touched, including scratch
            # files and paths outside the pipeline phase's allowlist; the gateway
            # validates the WHOLE staged set and 403s the commit if any single
            # path violates it. Dropping the snapshot there would lose the tree in
            # exactly the pipeline sessions this exists to protect, so unstage
            # what it named and take the rest.
            logger.warning(
                "Timeout checkpoint: phase restrictions rejected staged paths; "
                "unstaging them and retrying with the rest",
                repo_path=str(repo),
                blocked_paths=blocked,
                staged_count=len(staged),
                stderr=(commit.stderr or "").strip(),
            )
            reset = _run_git("reset", "--quiet", "--", *blocked, cwd=repo)
            if reset.returncode != 0:
                logger.warning(
                    "Timeout checkpoint: could not unstage the blocked paths; "
                    "skipping the snapshot",
                    repo_path=str(repo),
                    stderr=(reset.stderr or "").strip(),
                )
                return None
            if not _staged_paths(repo):
                logger.warning(
                    "Timeout checkpoint: every staged path was out of phase; "
                    "skipping the snapshot",
                    repo_path=str(repo),
                    blocked_paths=blocked,
                )
                return None
            commit = _commit(repo, partial=partial, phase_partial=True)
            if commit.returncode != 0:
                logger.warning(
                    "Timeout checkpoint: commit failed after unstaging the blocked paths",
                    repo_path=str(repo),
                    stderr=(commit.stderr or "").strip(),
                )
                return None
            partial = True

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
