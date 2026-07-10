"""Patch-id rescue for the slice-close evidence-reachability gate (#3572).

Pipeline ``issue-3364`` reached full slice consensus, but every close
attempt failed the #3125 evidence gate: the coder completed its tasks
citing its local HEAD SHA, and the final push then went through the
gateway push reconciliation (``push_worktree_branch`` →
``_reconcile_and_retry_push`` → rebase), which rewrote the commits
before they landed on the integration branch. The contract kept the
pre-rebase SHA (a commit that afterwards exists nowhere), so a
consensus-complete slice could never close without operator surgery.

The rescue applies the same content-based identity the authorship
registry already relies on (#2932): a rebase mints a new SHA but keeps
the commit's ``git patch-id --stable``. For each cited-but-unreachable
SHA this module resolves a patch-id (from the local object database
when the pre-rebase object still exists, else from the
commit-authorship registry, which durably recorded the patch-id at
commit time) and matches it against the patch-ids of the
integration branch's recent commits. An identical patch on the branch
proves the deliverable landed, so the gate treats the record as
satisfied and logs the old → new mapping instead of blocking the close.

Failure posture: the rescue can only *narrow* the gate's unreachable
set, never widen it, so every internal failure (git missing, ref
unresolvable, registry unreadable, subprocess timeout) degrades to
"no rescue" and the gate's original verdict stands.
``EGG_EVIDENCE_PATCH_ID_RESCUE`` is the operator kill switch.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from egg_logging import get_logger

logger = get_logger("orchestrator.evidence_rescue")

# Operator kill switch. Default on; set to "off" (or 0/false/no) to fall
# back to the strict #3125 exact-SHA verdict without a redeploy.
RESCUE_ENV_VAR = "EGG_EVIDENCE_PATCH_ID_RESCUE"

_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})

# How many tip-most integration-branch commits participate in the
# patch-id match. Rebase-rewritten deliverables land at (or very near)
# the tip the reconciled push produced, so a bounded scan keeps the
# ``git log -p`` cost flat while covering every realistic rewrite.
BRANCH_SCAN_LIMIT = 200

_GIT_TIMEOUT_SECONDS = 60


def rescue_enabled() -> bool:
    """Return True unless the operator kill switch disables the rescue."""
    return os.environ.get(RESCUE_ENV_VAR, "on").strip().lower() not in _DISABLED_VALUES


def _run_git(
    repo_path: Path | str,
    args: list[str],
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Run git in ``repo_path``; return None on spawn failure or timeout."""
    try:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_path), *args],  # noqa: S607
            input=input_text,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "evidence_rescue_git_failed",
            repo_path=str(repo_path),
            args=args[:2],
            error=str(exc),
        )
        return None


def patch_id_for_commit(repo_path: Path | str, sha: str) -> str | None:
    """Return ``git patch-id --stable`` for ``sha``, or ``None``.

    Mirrors the gateway's ``commit_observer.patch_id_for_commit``: feed
    ``git show`` through ``git patch-id --stable`` so the commit header
    is ignored and root commits work. Merge commits, empty commits, and
    rename/mode-only commits yield no patch-id → ``None``. A SHA whose
    object no longer exists in the odb also returns ``None``; the
    caller falls back to the authorship registry's recorded patch-id.
    """
    sha_s = (sha or "").strip()
    if not sha_s:
        return None
    show = _run_git(repo_path, ["show", "--no-color", sha_s])
    if show is None or show.returncode != 0 or not show.stdout:
        return None
    patch = _run_git(repo_path, ["patch-id", "--stable"], input_text=show.stdout)
    if patch is None or patch.returncode != 0:
        return None
    parts = (patch.stdout or "").split()
    return parts[0] if parts else None


def branch_patch_ids(
    repo_path: Path | str,
    ref: str,
    *,
    limit: int = BRANCH_SCAN_LIMIT,
) -> dict[str, str]:
    """Map patch-id → commit SHA for the ``limit`` tip-most commits of ``ref``.

    One ``git log -p | git patch-id --stable`` pipeline rather than one
    subprocess pair per commit (same batching as the gateway's
    ``commit_observer.patch_ids_for_commits``). Merge commits emit no
    patch and simply don't participate. When two branch commits carry an
    identical patch (e.g. a revert-and-reland), the tip-most one wins;
    the mapping is only used to name a satisfying commit in logs.
    """
    log = _run_git(
        repo_path,
        ["log", "--no-color", "--no-merges", "-p", "-n", str(limit), ref],
    )
    if log is None or log.returncode != 0 or not log.stdout:
        return {}
    patch = _run_git(repo_path, ["patch-id", "--stable"], input_text=log.stdout)
    if patch is None or patch.returncode != 0:
        return {}
    result: dict[str, str] = {}
    for line in (patch.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        pid, commit_sha = parts[0], parts[1]
        if pid and pid not in result:
            result[pid] = commit_sha
    return result


def _resolve_branch_ref(repo_path: Path | str, integration_branch: str) -> str | None:
    """Return a locally-resolvable ref for the integration branch tip.

    The #3125 gate fetched ``refs/remotes/origin/<branch>`` immediately
    before flagging the SHAs, so that tracking ref is the canonical
    candidate; a local ``refs/heads/<branch>`` covers worktrees that
    check the integration branch out directly.
    """
    for ref in (
        f"refs/remotes/origin/{integration_branch}",
        f"refs/heads/{integration_branch}",
    ):
        probe = _run_git(repo_path, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
        if probe is not None and probe.returncode == 0 and probe.stdout.strip():
            return ref
    return None


def _recorded_patch_ids(shas: list[str]) -> dict[str, str | None]:
    """Look up patch-ids the authorship registry recorded at commit time.

    Covers the fully-lost case: the pre-rebase object was pruned from
    every odb, but the gateway's commit observer registered its
    ``patch_id`` when the agent created the commit (#2932). Degrades to
    an empty mapping when the registry is unavailable.
    """
    try:
        from commit_authorship_store import get_store

        return get_store().lookup_patch_ids(shas)
    except Exception as exc:  # noqa: BLE001
        logger.debug("evidence_rescue_registry_lookup_failed", error=str(exc))
        return {}


def rescue_unreachable_commits(
    pipeline_id: str,
    repo_path: Path | str,
    *,
    unreachable_shas: list[str],
    integration_branch: str,
) -> dict[str, str]:
    """Return ``{cited_sha: branch_sha}`` for rewrites provably on the branch.

    For each SHA the #3125 gate flagged as unreachable, resolve its
    patch-id (odb first, authorship registry second) and match it
    against the patch-ids of the integration branch's recent commits.
    A match means a rebase (typically the gateway push reconciliation,
    #3572) rewrote the commit after the task record cited it; the
    deliverable is on the branch under a new SHA, so the record is
    satisfied. SHAs with no resolvable patch-id or no match are simply
    absent from the result and keep the gate's original verdict.
    """
    if not unreachable_shas or not integration_branch or not rescue_enabled():
        return {}

    ref = _resolve_branch_ref(repo_path, integration_branch)
    if ref is None:
        logger.warning(
            "Evidence patch-id rescue skipped: integration branch not resolvable locally (#3572)",
            pipeline_id=pipeline_id,
            integration_branch=integration_branch,
        )
        return {}

    branch_map = branch_patch_ids(repo_path, ref)
    if not branch_map:
        return {}

    rescued: dict[str, str] = {}
    odb_misses: list[str] = []
    # First pass: the odb-computed patch-id is authoritative when the
    # pre-rebase object still exists. Defer SHAs whose objects are gone.
    for sha in unreachable_shas:
        pid = patch_id_for_commit(repo_path, sha)
        if pid is None:
            odb_misses.append(sha)
            continue
        match = branch_map.get(pid)
        if match:
            rescued[sha] = match

    # Second pass: consult the authorship registry only for the odb
    # misses. ``recorded`` is keyed by ``_validate_sha``-normalized SHAs
    # (stripped + lowercased), so normalize the lookup key to match.
    if odb_misses:
        recorded = _recorded_patch_ids(odb_misses)
        for sha in odb_misses:
            pid = recorded.get(sha.strip().lower())
            if not pid:
                continue
            match = branch_map.get(pid)
            if match:
                rescued[sha] = match
    return rescued
