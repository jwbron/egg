"""Periodic cleanup of recovery refs created by :mod:`agent_salvage` (#2446).

The salvage hook (#2429) writes recovery refs to
``egg/recovered/<pipeline_id>/<scope>/<short_sha>`` whenever a per-agent
worktree is about to be deleted with unpushed work. By design those
refs outlive the pipeline they belong to so operators can replay the
work later — but on a busy cluster they accumulate without bound.

This module runs a periodic sweep that:

1. Lists every ``egg/recovered/*`` ref on origin via ``git ls-remote``.
2. Reads each ref tip's committer date to decide staleness.
3. Skips deletion for refs reachable from any non-recovered remote
   branch — defensive against a pending replay that's been pushed back
   to origin.
4. Deletes refs older than the configured TTL via the gateway's
   launcher-auth ``delete_remote_branch`` path.

Cadence and TTL are configurable via :mod:`env_config`. The sweep is
best-effort: any per-ref failure is logged and never aborts the loop.
"""

from __future__ import annotations

import fnmatch
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from agent_salvage import RECOVERY_BRANCH_PREFIX
from egg_logging import get_logger

if TYPE_CHECKING:
    from gateway_client import GatewayClient

logger = get_logger("orchestrator.agent_salvage_cleanup")


# Pseudo-pipeline-id used for the synthetic gateway session that backs
# the cleanup sweep. The salvage hook never sees this name — it only
# shows up in audit logs and session tables.
CLEANUP_PIPELINE_ID = "salvage-cleanup"


@dataclass(frozen=True)
class RecoveryRef:
    """A single ``egg/recovered/...`` ref observed on origin."""

    name: str
    """Full branch name, e.g. ``egg/recovered/issue-99/coder/abc123def456``."""

    sha: str
    """Commit SHA at the ref tip (from ``git ls-remote``)."""

    committed_at: datetime | None
    """Committer date of the tip commit. ``None`` when the commit isn't
    available locally (fetch failed) — such refs are skipped from
    deletion so we never delete a ref whose age we can't determine.
    """

    @property
    def short_sha(self) -> str:
        return self.sha[:12]


@dataclass
class CleanupReport:
    """Per-sweep metrics surfaced to logs and tests."""

    refs_inspected: int = 0
    refs_deleted: int = 0
    refs_skipped_recent: int = 0
    refs_skipped_reachable: int = 0
    refs_skipped_unknown_age: int = 0
    refs_skipped_error: int = 0
    deleted_refs: list[str] = field(default_factory=list)
    oldest_remaining_age_days: float | None = None
    error: str | None = None


def _run_git(
    *args: str,
    cwd: Path,
    check: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a git command with hooks disabled. Mirrors agent_salvage._run_git."""
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(cwd), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def list_recovery_refs(
    gateway: GatewayClient,
    repo_path: str,
    *,
    mode: str = "public",
) -> dict[str, str]:
    """Return ``{ref_name: sha}`` for every ``egg/recovered/*`` ref on origin.

    Uses :meth:`GatewayClient.list_remote_branches_with_shas` so the
    SHA is captured in the same round-trip as enumeration. Returns
    ``{}`` on gateway error.
    """
    branches = gateway.list_remote_branches_with_shas(
        CLEANUP_PIPELINE_ID,
        repo_path,
        mode=mode,  # type: ignore[arg-type]
    )
    prefix = f"{RECOVERY_BRANCH_PREFIX}/"
    return {name: sha for name, sha in branches.items() if name.startswith(prefix)}


def _fetch_recovery_refs(
    gateway: GatewayClient,
    repo_path: str,
    *,
    mode: str = "public",
) -> bool:
    """Refresh ``refs/remotes/origin/egg/recovered/*`` in the local repo.

    Best-effort: on failure we still attempt to inspect already-cached
    refs but the reachability and committer-date checks may rely on
    stale local state.
    """
    refspec = (
        f"+refs/heads/{RECOVERY_BRANCH_PREFIX}/*:refs/remotes/origin/{RECOVERY_BRANCH_PREFIX}/*"
    )
    return gateway.fetch_branch(
        pipeline_id=CLEANUP_PIPELINE_ID,
        repo_path=repo_path,
        args=["--prune", refspec],
        mode=mode,  # type: ignore[arg-type]
    )


def _committer_date(repo_path: Path, sha: str) -> datetime | None:
    """Read the committer date (``%cI``) of *sha*. Returns ``None`` when
    the commit isn't present locally or git rejects the SHA."""
    try:
        result = _run_git(
            "log",
            "-1",
            "--format=%cI",
            sha,
            cwd=repo_path,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    if not raw:
        return None
    try:
        # ``%cI`` is strict ISO-8601 with timezone (``2026-01-01T12:00:00+00:00``).
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _is_reachable_from_non_recovery(repo_path: Path, sha: str) -> bool:
    """Return True iff any non-``egg/recovered/*`` remote-tracking branch
    contains *sha*.

    Uses ``git for-each-ref --contains=<sha>`` which is the cheapest way
    to ask "what branches contain this commit?" and is bounded by the
    number of remote-tracking refs. False (no other ref contains it) is
    the default when the SHA isn't present locally — we can't prove
    reachability without the commit.
    """
    try:
        result = _run_git(
            "for-each-ref",
            f"--contains={sha}",
            "--format=%(refname)",
            "refs/remotes/origin/",
            cwd=repo_path,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return False
    if result.returncode != 0:
        return False
    excluded_prefix = f"refs/remotes/origin/{RECOVERY_BRANCH_PREFIX}/"
    for line in result.stdout.splitlines():
        ref = line.strip()
        if not ref:
            continue
        if ref.startswith(excluded_prefix):
            continue
        return True
    return False


def _classify(
    name: str,
    sha: str,
    repo_path: Path,
    cutoff: datetime,
) -> tuple[RecoveryRef, str]:
    """Return ``(ref, action)`` where action is one of:

    - ``"delete"``    — older than cutoff and not reachable elsewhere.
    - ``"recent"``    — newer than cutoff, leave alone.
    - ``"reachable"`` — old enough but reachable from a non-recovered branch.
    - ``"unknown"``   — couldn't determine committer date.
    """
    committed_at = _committer_date(repo_path, sha)
    ref = RecoveryRef(name=name, sha=sha, committed_at=committed_at)
    if committed_at is None:
        return ref, "unknown"
    if committed_at >= cutoff:
        return ref, "recent"
    if _is_reachable_from_non_recovery(repo_path, sha):
        return ref, "reachable"
    return ref, "delete"


def sweep_recovery_refs(
    gateway: GatewayClient,
    repo_path: Path | str,
    *,
    ttl_days: int = 90,
    mode: str = "public",
    dry_run: bool = False,
    now: datetime | None = None,
) -> CleanupReport:
    """One sweep across every ``egg/recovered/*`` ref on origin.

    Idempotent — re-running is safe. Errors are logged and surfaced via
    :class:`CleanupReport` rather than raised so the periodic driver
    can keep running on transient failure.

    ``dry_run`` skips the actual delete call but still classifies and
    populates ``CleanupReport.deleted_refs`` with the names that
    *would* have been deleted.
    """
    repo_path = Path(repo_path)
    report = CleanupReport()
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=ttl_days)

    repo_path_str = str(repo_path)

    # Step 1 — bring local recovery-ref state up to date so committer
    # dates and reachability lookups see the truth on origin. Failure is
    # non-fatal: we proceed with whatever local state exists.
    fetched = _fetch_recovery_refs(gateway, repo_path_str, mode=mode)
    if not fetched:
        logger.debug(
            "Recovery-ref fetch failed; sweep will use stale local state",
            repo_path=repo_path_str,
        )

    # Step 2 — list candidate refs on origin.
    try:
        candidates = list_recovery_refs(gateway, repo_path_str, mode=mode)
    except Exception as exc:  # noqa: BLE001
        report.error = f"list_recovery_refs failed: {exc}"
        logger.warning(
            "Recovery-ref cleanup sweep aborted at list step",
            repo_path=repo_path_str,
            error=str(exc),
        )
        return report

    report.refs_inspected = len(candidates)

    # Track the youngest committer date among kept refs so operators can
    # eyeball "what's the oldest thing still hanging around".
    oldest_remaining: datetime | None = None

    for name, sha in sorted(candidates.items()):
        try:
            ref, action = _classify(name, sha, repo_path, cutoff)
        except Exception as exc:  # noqa: BLE001
            report.refs_skipped_error += 1
            logger.warning(
                "Recovery-ref classification failed",
                ref=name,
                error=str(exc),
            )
            continue

        if action == "delete":
            if dry_run:
                report.refs_deleted += 1
                report.deleted_refs.append(name)
                continue
            try:
                push_result = gateway.delete_remote_branch(
                    pipeline_id=CLEANUP_PIPELINE_ID,
                    repo_path=repo_path_str,
                    branch=name,
                    mode=mode,  # type: ignore[arg-type]
                )
            except Exception as exc:  # noqa: BLE001
                report.refs_skipped_error += 1
                logger.warning(
                    "Recovery-ref delete raised",
                    ref=name,
                    error=str(exc),
                )
                continue
            if push_result.ok or push_result.category == "already_deleted":
                report.refs_deleted += 1
                report.deleted_refs.append(name)
                logger.info(
                    "Deleted stale recovery ref",
                    ref=name,
                    head_sha=ref.sha,
                    committed_at=(ref.committed_at.isoformat() if ref.committed_at else None),
                )
            else:
                report.refs_skipped_error += 1
                logger.warning(
                    "Recovery-ref delete failed",
                    ref=name,
                    detail=push_result.describe(),
                )
            continue

        if action == "recent":
            report.refs_skipped_recent += 1
        elif action == "reachable":
            report.refs_skipped_reachable += 1
        elif action == "unknown":
            report.refs_skipped_unknown_age += 1

        if ref.committed_at is not None:
            if oldest_remaining is None or ref.committed_at < oldest_remaining:
                oldest_remaining = ref.committed_at

    if oldest_remaining is not None:
        age_days = (now - oldest_remaining).total_seconds() / 86400.0
        report.oldest_remaining_age_days = round(age_days, 2)

    logger.info(
        "Recovery-ref cleanup sweep complete",
        repo_path=repo_path_str,
        ttl_days=ttl_days,
        dry_run=dry_run,
        refs_inspected=report.refs_inspected,
        refs_deleted=report.refs_deleted,
        refs_skipped_recent=report.refs_skipped_recent,
        refs_skipped_reachable=report.refs_skipped_reachable,
        refs_skipped_unknown_age=report.refs_skipped_unknown_age,
        refs_skipped_error=report.refs_skipped_error,
        oldest_remaining_age_days=report.oldest_remaining_age_days,
    )
    return report


class RecoveryRefCleaner:
    """Background driver that runs :func:`sweep_recovery_refs` on a cadence.

    One instance per repo path the orchestrator manages. Sweeps run in
    a daemon thread so process shutdown is clean. Stop is cooperative
    (the loop checks a flag between sleeps); a pending sweep finishes
    before the thread exits.
    """

    def __init__(
        self,
        gateway: GatewayClient,
        repo_path: Path,
        *,
        ttl_days: int = 90,
        interval_seconds: float = 24 * 3600,
        mode: str = "public",
    ):
        self._gateway = gateway
        self._repo_path = repo_path
        self._ttl_days = ttl_days
        self._interval_seconds = interval_seconds
        self._mode = mode
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"recovery-ref-cleaner:{self._repo_path.name}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Recovery-ref cleanup loop started",
            repo_path=str(self._repo_path),
            ttl_days=self._ttl_days,
            interval_seconds=self._interval_seconds,
        )

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def run_once(self, *, dry_run: bool = False) -> CleanupReport:
        """Synchronous one-shot — exposed for tests and operator CLIs."""
        return sweep_recovery_refs(
            self._gateway,
            self._repo_path,
            ttl_days=self._ttl_days,
            mode=self._mode,
            dry_run=dry_run,
        )

    def _loop(self) -> None:
        # Sleep before the first sweep so multiple replicas don't pile
        # up on the same minute after a coordinated restart.
        if self._stop.wait(self._interval_seconds):
            return
        while not self._stop.is_set():
            try:
                sweep_recovery_refs(
                    self._gateway,
                    self._repo_path,
                    ttl_days=self._ttl_days,
                    mode=self._mode,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Recovery-ref cleanup sweep raised; loop continues",
                    repo_path=str(self._repo_path),
                    error=str(exc),
                )
            if self._stop.wait(self._interval_seconds):
                return


def is_recovery_ref(name: str) -> bool:
    """Public predicate exposed for tests / operator scripts."""
    return fnmatch.fnmatch(name, f"{RECOVERY_BRANCH_PREFIX}/*")
