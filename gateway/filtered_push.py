"""Per-commit auto-filter for the gateway push handler.

This is the algorithm described in the architect's output for #1882:
walk the unpushed commit range topologically, keep pulled cross-role
commits bitwise-unchanged, and rewrite own-role commits by removing
blocked paths from their tree.  Own commits whose tree becomes empty
after filtering are dropped.  Every error path restores HEAD so the
agent's worktree looks exactly as it did before the push attempt.

The gateway's ``git_push`` handler calls ``execute_filtered_push`` when
the partition ``partition_files_by_role(push_role, own_files)`` finds
*some* (but not all) own-authored files to be blocked.  The all-blocked
path is handled separately by the caller — it short-circuits with
``nothing_to_push=true`` and never invokes this module.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from git_client import AttributedFile

try:
    from egg_logging import get_logger  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover

    def get_logger(name: str, **kwargs: Any):  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("gateway.filtered_push")


@dataclass
class FilteredPushResult:
    """Result of ``execute_filtered_push``.

    Carries everything the push handler needs to build its 200 response
    (when ``success`` is true) or audit-log the failure (when it's
    false and ``error`` is populated).
    """

    success: bool
    new_tip: str | None = None
    excluded_files: list[str] = field(default_factory=list)
    pushed_files: list[str] = field(default_factory=list)
    pushed_commits: list[str] = field(default_factory=list)
    dropped_commits: list[str] = field(default_factory=list)
    pulled_commits: list[dict[str, Any]] = field(default_factory=list)
    rewritten_commits: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None


def _git(
    exec_path: str, *args: str, env: dict[str, str] | None = None, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    """Run a git subprocess with our standard safe.directory + hooks config."""
    cmd = [
        "/usr/bin/git",
        "-c",
        "safe.directory=*",
        "-c",
        "core.hooksPath=/dev/null",
        *args,
    ]
    return subprocess.run(
        cmd,
        cwd=exec_path,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _commit_metadata(exec_path: str, sha: str) -> dict[str, str] | None:
    """Fetch author/committer name, email, date, and full message for ``sha``.

    Returns ``None`` on error; callers propagate as fail-closed.
    """
    # %an / %ae / %ad / %cn / %ce / %cd / %B — raw, NUL-separated so
    # messages with newlines don't split into multiple fields.
    fmt = "%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%P%x00%B"
    result = _git(exec_path, "log", "-1", f"--format={fmt}", sha)
    if result.returncode != 0 or not result.stdout:
        return None
    # show includes a trailing newline; strip once.
    raw = result.stdout
    parts = raw.split("\x00", 7)
    if len(parts) < 8:
        return None
    (
        author_name,
        author_email,
        author_date,
        committer_name,
        committer_email,
        committer_date,
        parents_raw,
        message_with_trailing,
    ) = parts
    # Trailing newline from the formatter — remove exactly one.
    message = message_with_trailing
    if message.endswith("\n"):
        message = message[:-1]
    return {
        "author_name": author_name,
        "author_email": author_email,
        "author_date": author_date,
        "committer_name": committer_name,
        "committer_email": committer_email,
        "committer_date": committer_date,
        "parents": parents_raw.strip(),
        "message": message,
    }


def _tree_of(exec_path: str, sha: str) -> str | None:
    """Return the tree SHA for commit ``sha``."""
    result = _git(exec_path, "rev-parse", f"{sha}^{{tree}}")
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _filter_tree(
    exec_path: str, base_tree: str, blocked_paths: list[str]
) -> tuple[str | None, str | None]:
    """Produce a new tree equal to ``base_tree`` minus ``blocked_paths``.

    Uses a temp index so the caller's index is untouched.  Returns
    ``(new_tree_sha, error)``.  A returned tree may equal ``base_tree``
    if none of the blocked paths existed in it — the caller treats that
    as the commit needing no rewrite.
    """
    if not blocked_paths:
        return base_tree, None

    index_fd = None
    index_path = None
    try:
        import tempfile

        index_fd, index_path = tempfile.mkstemp(suffix=".idx", dir=exec_path)
        os.close(index_fd)
        index_fd = None
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = index_path

        r = _git(exec_path, "read-tree", base_tree, env=env)
        if r.returncode != 0:
            return None, f"read-tree failed: {(r.stderr or '').strip()}"

        # Remove each blocked path (ignore "not in index" errors — only
        # some commits may have touched a given path).
        for p in blocked_paths:
            _git(exec_path, "update-index", "--remove", "--", p, env=env)

        wt = _git(exec_path, "write-tree", env=env)
        if wt.returncode != 0:
            return None, f"write-tree failed: {(wt.stderr or '').strip()}"
        return (wt.stdout or "").strip() or None, None
    finally:
        if index_path and os.path.exists(index_path):
            try:
                os.unlink(index_path)
            except OSError:
                pass


def _commit_tree(
    exec_path: str,
    tree_sha: str,
    parent_sha: str | None,
    meta: dict[str, str],
    message: str,
) -> tuple[str | None, str | None]:
    """Create a new commit from ``tree_sha`` with ``meta`` env. Returns (sha, error)."""
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = meta["author_name"]
    env["GIT_AUTHOR_EMAIL"] = meta["author_email"]
    env["GIT_AUTHOR_DATE"] = meta["author_date"]
    env["GIT_COMMITTER_NAME"] = meta["committer_name"]
    env["GIT_COMMITTER_EMAIL"] = meta["committer_email"]
    env["GIT_COMMITTER_DATE"] = meta["committer_date"]

    args = ["commit-tree", tree_sha]
    if parent_sha:
        args.extend(["-p", parent_sha])
    args.extend(["-m", message])
    result = _git(exec_path, *args, env=env)
    if result.returncode != 0:
        return None, f"commit-tree failed: {(result.stderr or '').strip()}"
    return (result.stdout or "").strip() or None, None


def execute_filtered_push(
    exec_path: str,
    *,
    push_role: str,
    branch: str,
    attributed_commits: list[str],
    attributed_files: list[AttributedFile],
    blocked_own_files: set[str],
    push_fn,  # type: ignore[no-untyped-def]
    registry_register,  # type: ignore[no-untyped-def]
    pipeline_id: str | None = None,
    repo: str | None = None,
    auto_filter_suffix: str = " [auto-filtered]",
) -> FilteredPushResult:
    """Rewrite and push the commit range.

    Args:
        exec_path: Worktree path.
        push_role: The role performing the push.
        branch: The branch we're pushing.
        attributed_commits: Ordered (oldest-first) list of SHAs in the
            push range.
        attributed_files: Per-file attribution records.
        blocked_own_files: The set of paths the role cannot write; any
            own-role file matching this set is stripped from each commit
            it appears in.
        push_fn: Callable ``push_fn() -> (ok: bool, error: str | None)``
            that performs ``git push`` with whatever refspec / options
            the caller wants.  Invoked after HEAD has been advanced to
            the rewritten tip.
        registry_register: Callable ``(sha, role, pipeline_id, repo,
            branch) -> bool`` that registers a rewritten own-commit
            with the authorship registry.  Best-effort; failures are
            swallowed.

    Returns:
        FilteredPushResult — success or rollback diagnostics.
    """
    # Snapshot HEAD for rollback.
    head_result = _git(exec_path, "rev-parse", "HEAD")
    if head_result.returncode != 0:
        return FilteredPushResult(success=False, error="Could not resolve HEAD")
    original_head = (head_result.stdout or "").strip()
    if not original_head:
        return FilteredPushResult(success=False, error="Empty HEAD")

    # Build a per-commit index of files so we know which blocked paths
    # to strip from each commit's tree.  Only apply to own-authored
    # (or unregistered-and-treated-as-own) commits; pulled commits pass
    # through verbatim.
    files_by_sha: dict[str, list[AttributedFile]] = {}
    for f in attributed_files:
        files_by_sha.setdefault(f.commit_sha, []).append(f)

    # Walk oldest → newest.
    parent_lookup: dict[str, str] = {}  # original_sha -> new_sha (or same)
    new_parent = original_head
    # Find the merge base with the intended chain start.  Our new_parent
    # must begin at the commit *before* attributed_commits[0] so the
    # first rewritten commit has a sane parent.  We resolve the first
    # parent of attributed_commits[0] (if any).
    if attributed_commits:
        first_parent_result = _git(
            exec_path, "rev-parse", f"{attributed_commits[0]}^@"
        )
        if first_parent_result.returncode == 0:
            first_parents = [
                line.strip()
                for line in (first_parent_result.stdout or "").splitlines()
                if line.strip()
            ]
            if first_parents:
                new_parent = first_parents[0]
            else:
                # Root commit — no parent.
                new_parent = ""

    pulled_commits: list[dict[str, Any]] = []
    rewritten_commits: list[dict[str, str]] = []
    dropped_commits: list[str] = []
    pushed_commits: list[str] = []
    excluded_paths: set[str] = set()
    pushed_paths: set[str] = set()

    for sha in attributed_commits:
        commit_files = files_by_sha.get(sha, [])
        author_role = None
        for f in commit_files:
            if f.authored_by is not None:
                author_role = f.authored_by
                break

        is_own = author_role is None or author_role == push_role

        meta = _commit_metadata(exec_path, sha)
        if meta is None:
            _rollback(exec_path, original_head, branch)
            return FilteredPushResult(
                success=False,
                error=f"Could not read metadata for {sha}",
            )

        if not is_own:
            # Pulled cross-role commit: re-parent onto the running chain
            # but preserve its tree and message.  If re-parenting would
            # produce the same SHA (parents unchanged), skip.
            tree_sha = _tree_of(exec_path, sha)
            if tree_sha is None:
                _rollback(exec_path, original_head, branch)
                return FilteredPushResult(
                    success=False, error=f"Missing tree for {sha}"
                )
            parents_orig = meta.get("parents", "")
            orig_parent_list = [p for p in parents_orig.split() if p]
            if orig_parent_list and orig_parent_list[0] == new_parent:
                new_sha = sha
            else:
                new_sha, err = _commit_tree(
                    exec_path, tree_sha, new_parent or None, meta, meta["message"]
                )
                if err or new_sha is None:
                    _rollback(exec_path, original_head, branch)
                    return FilteredPushResult(
                        success=False, error=err or "commit-tree returned empty"
                    )
            parent_lookup[sha] = new_sha
            new_parent = new_sha
            pushed_commits.append(new_sha)
            for f in commit_files:
                pushed_paths.add(f.path)
            pulled_commits.append(
                {
                    "sha": sha,
                    "author_role": author_role,
                    "rewritten_sha": new_sha if new_sha != sha else None,
                }
            )
            continue

        # Own commit: strip blocked paths.
        blocked_here = [f.path for f in commit_files if f.path in blocked_own_files]
        allowed_here = [f.path for f in commit_files if f.path not in blocked_own_files]
        for p in blocked_here:
            excluded_paths.add(p)
        for p in allowed_here:
            pushed_paths.add(p)
        if not blocked_here:
            # No filtering needed — commit passes through.  Still
            # re-parent if the chain shifted underneath.
            tree_sha = _tree_of(exec_path, sha)
            if tree_sha is None:
                _rollback(exec_path, original_head, branch)
                return FilteredPushResult(
                    success=False, error=f"Missing tree for {sha}"
                )
            parents_orig = meta.get("parents", "")
            orig_parent_list = [p for p in parents_orig.split() if p]
            if orig_parent_list and orig_parent_list[0] == new_parent:
                new_sha = sha
            else:
                new_sha, err = _commit_tree(
                    exec_path, tree_sha, new_parent or None, meta, meta["message"]
                )
                if err or new_sha is None:
                    _rollback(exec_path, original_head, branch)
                    return FilteredPushResult(
                        success=False, error=err or "commit-tree returned empty"
                    )
            parent_lookup[sha] = new_sha
            new_parent = new_sha
            pushed_commits.append(new_sha)
            continue

        base_tree = _tree_of(exec_path, sha)
        if base_tree is None:
            _rollback(exec_path, original_head, branch)
            return FilteredPushResult(
                success=False, error=f"Missing base tree for {sha}"
            )
        new_tree, err = _filter_tree(exec_path, base_tree, blocked_here)
        if err:
            _rollback(exec_path, original_head, branch)
            return FilteredPushResult(success=False, error=err)
        # Did filtering leave anything different from the parent's tree?
        parent_tree: str | None = None
        if new_parent:
            parent_tree = _tree_of(exec_path, new_parent)
        if parent_tree is not None and new_tree == parent_tree:
            # Empty after filter → drop commit; parent chain continues
            # at the existing new_parent.
            dropped_commits.append(sha)
            parent_lookup[sha] = new_parent  # points to upstream anchor
            continue
        # Build new commit with suffix.
        new_message = meta["message"].rstrip() + auto_filter_suffix
        new_sha, err = _commit_tree(
            exec_path,
            new_tree or base_tree,
            new_parent or None,
            meta,
            new_message,
        )
        if err or new_sha is None:
            _rollback(exec_path, original_head, branch)
            return FilteredPushResult(
                success=False, error=err or "commit-tree returned empty"
            )
        parent_lookup[sha] = new_sha
        new_parent = new_sha
        pushed_commits.append(new_sha)
        rewritten_commits.append({"original_sha": sha, "new_sha": new_sha})

    final_tip = new_parent

    # Advance the local branch ref to the new tip.
    if not final_tip:
        # Every commit in the range got dropped — equivalent to the
        # "all blocked" path; caller should have intercepted, but we
        # return a no-op success defensively.
        return FilteredPushResult(
            success=True,
            new_tip=original_head,
            excluded_files=sorted(excluded_paths),
            pushed_files=sorted(pushed_paths),
            pushed_commits=[],
            dropped_commits=dropped_commits,
            pulled_commits=pulled_commits,
            rewritten_commits=rewritten_commits,
        )

    # update-ref so the local branch matches our rewrite.
    ur = _git(exec_path, "update-ref", f"refs/heads/{branch}", final_tip)
    if ur.returncode != 0:
        _rollback(exec_path, original_head, branch)
        return FilteredPushResult(
            success=False,
            error=f"update-ref refs/heads/{branch} failed: {(ur.stderr or '').strip()}",
        )

    # Now push.  If it fails, roll HEAD + the branch ref back and
    # restore the worktree so the caller sees the pre-attempt state.
    try:
        ok, push_err = push_fn()
    except Exception as exc:  # pragma: no cover - defensive
        _rollback(exec_path, original_head, branch)
        return FilteredPushResult(success=False, error=f"push raised: {exc}")

    if not ok:
        _rollback(exec_path, original_head, branch)
        return FilteredPushResult(success=False, error=push_err or "Push failed")

    # Post-success: fast-forward the worktree + index to the new tip,
    # then re-stage the excluded files so the next role sees them as
    # uncommitted changes (decision-6).
    rt = _git(exec_path, "read-tree", "--reset", "-u", final_tip)
    if rt.returncode != 0:
        # The push succeeded but the worktree can't be synced.  Log and
        # return success — the agent's local state is degraded but
        # origin is now the source of truth.  Callers can re-fetch.
        logger.warning(
            "filtered_push_worktree_resync_failed",
            branch=branch,
            error=(rt.stderr or "").strip(),
        )
    else:
        if excluded_paths:
            _restage_blocked_files(
                exec_path,
                blocked_paths=sorted(excluded_paths),
                source_sha=original_head,
            )

    # Best-effort: register rewritten own-commits with the registry so a
    # subsequent cross-role push attributes them correctly.
    for mapping in rewritten_commits:
        new_sha = mapping.get("new_sha")
        if not new_sha:
            continue
        try:
            registry_register(
                sha=new_sha,
                role=push_role,
                pipeline_id=pipeline_id,
                repo=repo,
                branch=branch,
            )
        except Exception:
            logger.debug(
                "filtered_push_register_rewritten_swallowed",
                new_sha=new_sha,
                exc_info=True,
            )

    return FilteredPushResult(
        success=True,
        new_tip=final_tip,
        excluded_files=sorted(excluded_paths),
        pushed_files=sorted(pushed_paths),
        pushed_commits=pushed_commits,
        dropped_commits=dropped_commits,
        pulled_commits=pulled_commits,
        rewritten_commits=rewritten_commits,
    )


def _rollback(exec_path: str, original_head: str, branch: str) -> None:
    """Restore HEAD, the branch ref, the index, and the worktree.

    The caller's error path is free to report whatever message it
    needs; this helper just ensures a bad filtered-push leaves no
    footprint in the agent's worktree.
    """
    try:
        _git(exec_path, "update-ref", f"refs/heads/{branch}", original_head)
    except Exception:  # pragma: no cover
        logger.warning(
            "filtered_push_rollback_update_ref_failed",
            branch=branch,
            exc_info=True,
        )
    try:
        _git(exec_path, "reset", "--hard", original_head)
    except Exception:  # pragma: no cover
        logger.warning(
            "filtered_push_rollback_reset_failed",
            branch=branch,
            exc_info=True,
        )


def _restage_blocked_files(
    exec_path: str, blocked_paths: list[str], source_sha: str
) -> None:
    """Re-introduce the blocked files into the worktree as *uncommitted* changes.

    After a filtered push the origin tip lacks the blocked paths.  Most
    agents will want them back as staged changes so the next role can
    pick them up (HITL decision-6).  Best-effort: any failure here is
    logged at WARNING and swallowed — the push itself already
    succeeded.
    """
    if not blocked_paths:
        return
    try:
        for path in blocked_paths:
            # Fetch the blob from the pre-filter tip.
            show = _git(exec_path, "show", f"{source_sha}:{path}")
            if show.returncode != 0:
                logger.debug(
                    "filtered_push_restage_missing_blob",
                    path=path,
                    source_sha=source_sha,
                )
                continue
            content = show.stdout
            # Write the file content back to the worktree.
            full_path = os.path.join(exec_path, path)
            os.makedirs(os.path.dirname(full_path) or exec_path, exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
            _git(exec_path, "add", "--intent-to-add", "--", path)
    except Exception:  # pragma: no cover
        logger.warning("filtered_push_restage_failed", exc_info=True)


__all__ = [
    "AttributedFile",
    "FilteredPushResult",
    "execute_filtered_push",
]
