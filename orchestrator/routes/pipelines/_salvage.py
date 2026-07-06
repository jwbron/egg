"""salvage worktree filters + serializers helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _filter_salvage_worktrees(
    worktrees: list[_pkg.Any],
    *,
    agent_role: str | None,
    slice_id: str | None,
) -> list[_pkg.Any]:
    """Filter ``enumerate_agent_worktrees`` output by role / slice scope.

    ``agent_role`` and ``slice_id`` may both be ``None`` (return all) or
    set together to scope down to one specific worktree. ``agent_role``
    set with ``slice_id=None`` matches non-slice per-agent worktrees.
    The pipeline-level worktree (``agent_role=None`` on the worktree)
    is included only when the caller did not specify ``agent_role``.
    """
    out = []
    for wt in worktrees:
        if agent_role is not None and wt.agent_role != agent_role:
            continue
        if slice_id is not None and wt.slice_id != slice_id:
            continue
        out.append(wt)
    return out


def _serialize_commit_report(report: _pkg.Any) -> dict[str, _pkg.Any]:
    """Convert a ``WorktreeCommitReport`` to a JSON-safe dict."""
    return {
        "worktree_id": report.worktree.worktree_id,
        "agent_role": report.worktree.agent_role,
        "slice_id": report.worktree.slice_id,
        "local_branch": report.worktree.local_branch,
        "assigned_branch": report.assigned_branch,
        "anchor_ref": report.anchor_ref,
        "commits": [
            {
                "sha": c.sha,
                "summary": c.summary,
                "author": c.author,
                "authored_at": c.authored_at,
                "files_changed": c.files_changed,
            }
            for c in report.commits
        ],
        "error": report.error,
    }


def _serialize_salvage_result(result: _pkg.Any) -> dict[str, _pkg.Any]:
    """Convert a ``SalvageResult`` to a JSON-safe dict."""
    return {
        "worktree_id": result.worktree_id,
        "agent_role": result.agent_role,
        "slice_id": result.slice_id,
        "recovery_ref": result.recovery_ref,
        "head_sha": result.head_sha,
        "n_commits": result.n_commits,
        "ok": result.ok,
        "error": result.error,
    }
