"""Changed-files detection for a push range.

Extracted verbatim from the pre-split ``gateway/git_client.py``
(#3312 slice-11). AST-identical to the originals — pure refactor.
"""

import re

from egg_logging import get_logger

from ._remote import git_cmd

logger = get_logger("gateway.git-client")


# =============================================================================
# Changed Files Detection
# =============================================================================


# Matches a single git SHA line (7–64 lowercase hex). Used to reject
# multi-line / garbled stdout from a misbehaving git wrapper before treating
# the value as a commit identifier. Shared by both ``get_changed_files_in_push``
# and ``_enumerate_push_commits`` for fork-point and rev-list validation.
_SHA_LINE_RE = re.compile(r"^[0-9a-f]{7,64}$")


def _parse_sha_lines(text: str) -> list[str] | None:
    """Parse rev-list stdout into a list of SHAs, or ``None`` if any line is garbled.

    Mirrors the validation in ``_enumerate_push_commits`` and applies it to
    both fallback and primary rev-list output in ``get_changed_files_in_push``
    so SHA-from-stdout discipline is uniform across every path that pipes a
    line of git stdout into ``diff-tree``'s positional argv. A misbehaving
    git wrapper that smuggled ``--ext-diff=…`` or another diff-tree flag as
    a "SHA" would otherwise be passed through; failing closed on parse error
    keeps the caller's fail-closed semantics for security checks intact.
    """
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if not _SHA_LINE_RE.match(s):
            return None
        out.append(s)
    return out


def _fetch_base_branch_best_effort(
    repo_path: str, remote: str, base_branch: str, timeout: int = 15
) -> None:
    """Best-effort fetch of ``origin/<base_branch>`` for the new-branch fallback.

    A single ``git_push`` request calls both ``get_changed_files_in_push`` and
    ``_enumerate_push_commits`` and both run this fallback on the first push
    (before ``origin/<branch>`` exists). Without coordination they each pay
    the full fetch timeout — up to 60 s of added latency when the remote is
    reachable-but-slow. This helper checks for the ref locally first via a
    cheap ``rev-parse --verify --quiet``: on the second call the ref is
    already present (the first call just fetched it) and the redundant network
    round-trip is skipped. Fetch timeout is 15 s here vs. 30 s for the primary
    branch fetch, because the base ref is a fallback, not the critical path.

    Best-effort throughout: both rev-parse and fetch errors are swallowed —
    if the fetch fails, the merge-base loop just falls through to the next
    candidate (``main`` / ``master``) and ultimately fails closed if nothing
    resolves.
    """
    import subprocess

    try:
        check = subprocess.run(
            git_cmd("rev-parse", "--verify", "--quiet", f"{remote}/{base_branch}"),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if check.returncode == 0 and (check.stdout or "").strip():
            return
    except Exception:
        pass  # treat as not-yet-fetched and try the network fetch

    try:
        subprocess.run(
            git_cmd("fetch", remote, base_branch),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        pass  # merge-base will fall through to the next candidate


def _fallback_base_candidates(base_branch: str | None) -> list[str]:
    """Ordered, de-duplicated diff-base candidates for new-branch pushes.

    Used only by the merge-base fallback that fires when ``origin/<branch>``
    does not yet exist (a pipeline's first push). The pipeline's configured
    ``base_branch`` is tried first: a branch forked from a non-trunk base
    carries that base's content, and diffing against trunk (``main``/
    ``master``) instead would attribute every file inherited unchanged from
    the base to the pushing role — falsely blocking non-documenter roles on
    bases that legitimately carry documenter-owned files (#3024). ``main``
    and ``master`` remain as trailing candidates so sessions with no
    ``base_branch`` (legacy / non-pipeline) keep today's behavior, and so the
    merge-base still resolves if the configured base ref can't be fetched.
    """
    candidates: list[str] = []
    if base_branch and base_branch != "HEAD":
        candidates.append(base_branch)
    for trunk in ("main", "master"):
        if trunk not in candidates:
            candidates.append(trunk)
    return candidates


def get_changed_files_in_push(
    repo_path: str, remote: str, branch: str, base_branch: str | None = None
) -> tuple[list[str], str | None]:
    """
    Get the list of files that would be changed by a push.

    Compares local branch to remote tracking branch to determine what
    files are being modified in the commits being pushed.

    Per-commit detection (#1539): Uses rev-list + diff-tree to inspect
    each commit individually, avoiding tree-level diffs that would
    include files from other agents' prior pushes to the same branch.

    SECURITY: This function is used for security-critical file restriction checks.
    If it returns an error, the caller MUST treat it as a security failure and
    block the push. Never fail open on git diff errors.

    Args:
        repo_path: Path to the git repository
        remote: Remote name (e.g., "origin")
        branch: Branch name being pushed
        base_branch: The pipeline's configured base branch (PR base). Used as
            the preferred diff base for the new-branch merge-base fallback so a
            branch forked from a non-trunk base is not blamed for files it
            inherited unchanged from that base (#3024). ``None`` (legacy /
            non-pipeline sessions) falls back to ``main``/``master`` as before.

    Returns:
        Tuple of (changed_files, error_message)
        - changed_files: List of file paths that are changed
        - error_message: Error string if the check failed, None on success
    """
    import subprocess

    # Fetch the target branch so we have the latest remote ref.
    # The orchestrator may have pushed contract init from a different worktree,
    # so the agent's local repo may not know about origin/<branch> yet (#1431).
    try:
        subprocess.run(
            git_cmd("fetch", remote, branch),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        pass  # Best-effort; primary diff path will fall through to fallback if needed

    # Determine which files are changed in the new commits being pushed.
    #
    # IMPORTANT: We use per-commit diff-tree detection (rev-list + diff-tree)
    # instead of `git diff origin/branch..HEAD` because the latter is a
    # tree-level comparison that shows ALL differences between two tree states.
    # In multi-agent pipelines where agents push to the same branch from
    # separate worktrees, a tree diff would include files from other agents'
    # commits, causing false-positive push rejections. (Bug #1535)
    #
    # Per-commit diff-tree only reports files actually modified in each commit,
    # so it correctly scopes the check to the pushing agent's own changes.
    try:
        # Primary path: list commits on HEAD that are not on the remote branch,
        # then inspect each commit individually with diff-tree.
        rev_list_result = subprocess.run(
            git_cmd("rev-list", f"{remote}/{branch}..HEAD"),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        # Validate every rev-list line is a SHA before feeding it to diff-tree's
        # positional argv (parity with _enumerate_push_commits, which goes through
        # _parse_sha_lines on both its primary and fallback paths). On parse
        # failure (garbled or multi-line stdout from a misbehaving git wrapper),
        # treat the primary path as a miss and fall through to the merge-base
        # fallback — same effect as a non-zero rev-list returncode.
        primary_shas = (
            _parse_sha_lines(rev_list_result.stdout) if rev_list_result.returncode == 0 else None
        )
        if primary_shas is None and rev_list_result.returncode == 0:
            logger.error(
                "rev-list stdout contained a non-SHA line - falling through to fallback",
                repo_path=repo_path,
                remote=remote,
                branch=branch,
            )

        if primary_shas is not None:
            all_files: set[str] = set()
            commits_found = 0
            commits_inspected = 0
            diff_tree_errors: list[str] = []
            for sha in primary_shas:
                commits_found += 1
                # NOTE: On merge commits, `diff-tree -r` uses combined diff
                # format, which only shows files differing from *all* parents
                # (i.e., conflict resolutions). Clean merges produce empty
                # output — this is correct here because a clean merge didn't
                # introduce new changes.
                dt_result = subprocess.run(
                    git_cmd("diff-tree", "--no-commit-id", "--name-only", "-r", sha),
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if dt_result.returncode == 0:
                    commits_inspected += 1
                    for f in dt_result.stdout.strip().split("\n"):
                        f = f.strip()
                        if f:
                            all_files.add(f)
                else:
                    diff_tree_errors.append(
                        f"sha={sha} rc={dt_result.returncode} stderr={dt_result.stderr.strip()}"
                    )

            if commits_found > 0 and commits_inspected < commits_found:
                # Some diff-tree calls failed — fail closed
                logger.error(
                    "Some diff-tree calls failed during per-commit file detection - failing closed",
                    repo_path=repo_path,
                    remote=remote,
                    branch=branch,
                    commits_found=commits_found,
                    commits_inspected=commits_inspected,
                    errors=diff_tree_errors,
                )
                # Fall through to merge-base fallback
            else:
                return sorted(all_files), None

        # If remote branch doesn't exist yet, use per-commit file detection via
        # merge-base + diff-tree. This avoids false positives from inherited
        # differences between worktree branches and origin/main (Bug #1239).
        #
        # Diff against the pipeline's configured base_branch first (#3024): a
        # branch forked from a non-trunk base carries that base's content, and
        # diffing against trunk would mis-attribute those inherited files to the
        # pushing role. main/master remain trailing fallbacks.
        fallback_bases = _fallback_base_candidates(base_branch)
        if base_branch and base_branch != "HEAD":
            # Best-effort fetch so origin/<base_branch> resolves for the
            # merge-base below — the top-of-function fetch only refreshed the
            # pushed branch. main/master are assumed already present locally.
            # The helper short-circuits on the second call of the same push
            # (when _enumerate_push_commits ran first or vice-versa) via a
            # local rev-parse check, avoiding a redundant network fetch.
            _fetch_base_branch_best_effort(repo_path, remote, base_branch)
        for default_branch in fallback_bases:
            merge_base_result = subprocess.run(
                git_cmd("merge-base", f"{remote}/{default_branch}", "HEAD"),
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if merge_base_result.returncode != 0:
                continue

            # Validate the merge-base output is a single SHA line (mirrors
            # _enumerate_push_commits): a misbehaving git wrapper or multi-line
            # stdout (e.g. parent-pair output that would surface if --all were
            # ever added) must not leak through as a fork point.
            fork_point = (merge_base_result.stdout or "").strip()
            if not _SHA_LINE_RE.match(fork_point):
                continue

            # Get files changed in each commit between fork point and HEAD
            log_result = subprocess.run(
                git_cmd("rev-list", f"{fork_point}..HEAD"),
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if log_result.returncode != 0:
                continue

            # Validate every SHA line before piping it to diff-tree argv.
            # On parse failure, continue to the next candidate trunk — the
            # caller's fail-closed loop still kicks in if every candidate
            # fails (mirrors the per-line validation in _enumerate_push_commits).
            fallback_shas = _parse_sha_lines(log_result.stdout)
            if fallback_shas is None:
                continue

            all_files = set()
            commits_found = 0
            commits_inspected = 0
            diff_tree_errors = []
            for sha in fallback_shas:
                commits_found += 1
                dt_result = subprocess.run(
                    git_cmd("diff-tree", "--no-commit-id", "--name-only", "-r", sha),
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if dt_result.returncode == 0:
                    commits_inspected += 1
                    for f in dt_result.stdout.strip().split("\n"):
                        f = f.strip()
                        if f:
                            all_files.add(f)
                else:
                    diff_tree_errors.append(
                        f"sha={sha} rc={dt_result.returncode} stderr={dt_result.stderr.strip()}"
                    )

            if commits_found > 0 and commits_inspected < commits_found:
                # Some or all diff-tree calls failed — fail closed to prevent
                # an attacker from hiding restricted files in failing commits
                logger.error(
                    "Some diff-tree calls failed during per-commit file detection - failing closed",
                    repo_path=repo_path,
                    remote=remote,
                    branch=branch,
                    commits_found=commits_found,
                    commits_inspected=commits_inspected,
                    errors=diff_tree_errors,
                )
                continue  # try next default branch, or fall through to security error

            return sorted(all_files), None

        # SECURITY: If we cannot determine what files are being pushed, we MUST
        # fail closed to prevent bypass of file restrictions. An attacker could
        # intentionally cause git diff to fail (e.g., corrupt refs, timeout) to
        # bypass protection. This follows the codebase's fail-closed security pattern.
        logger.error(
            "Could not determine changed files in push - failing closed for security",
            repo_path=repo_path,
            remote=remote,
            branch=branch,
        )
        return [], "Could not determine changed files - push blocked for security"

    except subprocess.TimeoutExpired:
        return [], "Timeout determining changed files"
    except Exception as e:
        return [], f"Error determining changed files: {e}"
