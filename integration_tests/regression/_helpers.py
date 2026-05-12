"""Shared helpers for regression integration tests.

The git-plumbing helpers and the ``_build_worktree`` setup live here so
sibling tests in this suite can compose worktrees the same way without
copy-pasting the boilerplate. Salvage-loop tests (``test_unpushed_commit_salvage.py``)
and recovery-invariant tests (``test_recovery_invariants.py``) both
exercise the agent-salvage chain on real on-disk worktrees; the shape
of those worktrees needs to be uniform so the tests document the same
contract.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_salvage import AgentWorktree


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git`` with a regression-test identity, hooks disabled."""
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.email=salvage@test.example",
            "-c",
            "user.name=Salvage Tester",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def make_repo(path: Path, branch_name: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "--initial-branch", branch_name, cwd=path)
    (path / "README.md").write_text("seed\n")
    git("add", "README.md", cwd=path)
    git("commit", "-q", "-m", "seed", cwd=path)
    return git("rev-parse", "HEAD", cwd=path).stdout.strip()


def commit(path: Path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content)
    git("add", filename, cwd=path)
    git("commit", "-q", "-m", message, cwd=path)
    return git("rev-parse", "HEAD", cwd=path).stdout.strip()


def set_assigned_branch(repo: Path, local_branch: str, assigned: str) -> None:
    """Mirror what gateway/worktree_manager writes at worktree create time.

    The agent's per-agent worktree carries ``branch.<local>.merge`` set
    to ``refs/heads/<assigned>``; that's how ``list_unpushed_commits``
    discovers the assigned upstream when computing the anchor cut.
    """
    git(
        "config",
        f"branch.{local_branch}.merge",
        f"refs/heads/{assigned}",
        cwd=repo,
    )


def create_origin_tracking(repo: Path, remote_branch: str, sha: str) -> None:
    """Stand in for ``origin/<branch>`` after a fetch."""
    git("update-ref", f"refs/remotes/origin/{remote_branch}", sha, cwd=repo)


def build_worktree(
    base: Path,
    pipeline_id: str,
    *,
    agent_role: str | None,
    slice_id: str | None,
    assigned_branch: str,
    n_unpushed: int = 1,
) -> tuple[AgentWorktree, str]:
    """Create a per-agent worktree directory with ``n_unpushed`` local commits.

    Returns the ``AgentWorktree`` descriptor and the HEAD SHA (the SHA
    that the salvage path is supposed to push to its recovery ref).
    """
    if agent_role is None:
        worktree_id = pipeline_id
        scope = "pipeline"
    elif slice_id is None:
        worktree_id = f"{pipeline_id}-{agent_role}"
        scope = agent_role
    else:
        worktree_id = f"{pipeline_id}-{slice_id}-{agent_role}"
        scope = f"{slice_id}-{agent_role}"

    local_branch = f"egg/{worktree_id}/work"
    repo = base / worktree_id / "repo"
    anchor = make_repo(repo, local_branch)
    set_assigned_branch(repo, local_branch, assigned_branch)
    # The wedged scenario from #2429: agent pushes were rejected so the
    # assigned-branch tracking ref never advanced past the anchor. Local
    # commits accumulate on the work branch with no remote anchor for
    # them.
    create_origin_tracking(repo, assigned_branch, anchor)

    head = anchor
    for i in range(n_unpushed):
        head = commit(repo, f"unpushed-{i}.txt", f"work {i}\n", f"unpushed change {i}")

    wt = AgentWorktree(
        worktree_id=worktree_id,
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        slice_id=slice_id,
        repo_path=repo,
        local_branch=local_branch,
    )
    assert wt.scope_label == scope  # belt-and-braces against helper drift
    return wt, head
