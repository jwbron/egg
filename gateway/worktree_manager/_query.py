"""WorktreeManager lookup / listing method bodies (#3312 slice-12).

Single-worktree lookup and the filesystem-scanning list operations. Extracted verbatim
and bound onto ``WorktreeManager`` in the barrel; they take ``self`` explicitly.
"""

import re
from pathlib import Path
from typing import Any

from ._common import (
    WorktreeInfo,
    validate_identifier,
)


def lookup_worktree(
    self,
    repo_name: str,
    container_id: str,
) -> WorktreeInfo:
    """Return info for an existing worktree without creating one.

    Used when the caller already created the worktree in a prior step
    and just needs its paths (e.g. session_create reusing a worktree
    that create_worktrees already made — see #1857).  Creating a second
    worktree for the same agent races on ``.git/config.lock`` in the
    bare repo and intermittently fails concurrent spawns.

    Args:
        repo_name: Name of the repository.
        container_id: Container id under which the worktree was created.

    Returns:
        WorktreeInfo describing the existing worktree.

    Raises:
        ValueError: If inputs are invalid, the repo doesn't exist, or
            no valid worktree is present at the expected path.

    Note:
        Unlike ``create_worktree``'s reuse path, this method deliberately
        skips ``_chown_recursive`` and ``_configure_push_upstream``.  In
        the current flow both ``create_worktrees`` and ``register_session``
        run with the same ``host_uid``/``host_gid``, and push upstream was
        already configured by the original ``create_worktree`` call.
    """
    validate_identifier(container_id, "container_id")
    validate_identifier(repo_name, "repo_name")

    main_repo = self.repos_base / repo_name
    if not main_repo.exists():
        raise ValueError(f"Repository not found: {repo_name}")

    worktree_path = self.worktree_base / container_id / repo_name
    git_file = worktree_path / ".git"
    if not (
        worktree_path.exists()
        and git_file.is_file()
        and git_file.read_text().strip().startswith("gitdir:")
    ):
        raise ValueError(
            f"Worktree not found for container_id={container_id} "
            f"repo={repo_name} at {worktree_path}"
        )

    return WorktreeInfo(
        container_id=container_id,
        repo_name=repo_name,
        branch=f"egg/{container_id}/work",
        worktree_path=worktree_path,
        git_dir=self._find_worktree_git_dir(main_repo, worktree_path),
    )


def list_worktrees(self) -> list[dict[str, Any]]:
    """
    List all active worktrees.

    Returns:
        List of worktree information dictionaries
    """
    worktrees: list[dict[str, Any]] = []

    if not self.worktree_base.exists():
        return worktrees

    for container_dir in self.worktree_base.iterdir():
        if not container_dir.is_dir():
            continue

        container_id = container_dir.name
        repos = []

        for repo_dir in container_dir.iterdir():
            if repo_dir.is_dir():
                # Get branch info if possible
                branch = None
                git_file = repo_dir / ".git"
                if git_file.exists():
                    try:
                        # Read gitdir from .git file
                        gitdir_content = git_file.read_text().strip()
                        if gitdir_content.startswith("gitdir: "):
                            gitdir_path = Path(gitdir_content[8:])
                            if not gitdir_path.is_absolute():
                                gitdir_path = (repo_dir / gitdir_path).resolve()
                            head_file = gitdir_path / "HEAD"
                            if head_file.exists():
                                head_content = head_file.read_text().strip()
                                if head_content.startswith("ref: refs/heads/"):
                                    branch = head_content[16:]
                    except Exception:
                        pass

                repos.append(
                    {
                        "name": repo_dir.name,
                        "path": str(repo_dir),
                        "branch": branch,
                    }
                )

        if repos:
            worktrees.append(
                {
                    "container_id": container_id,
                    "repos": repos,
                }
            )

    return worktrees


def list_worktrees_for_pipeline(self, pipeline_id: str) -> list[WorktreeInfo]:
    """List all worktrees for a pipeline (all agent roles).

    Per-agent worktrees (#1481) use IDs of the form
    '{pipeline_id}-{role}', so we scan for all container directories
    matching this prefix as well as the base pipeline worktree.

    Args:
        pipeline_id: Pipeline identifier to scan for.

    Returns:
        List of WorktreeInfo for every repo worktree belonging to
        this pipeline (including both the shared orchestrator
        worktree and per-agent worktrees).
    """
    results: list[WorktreeInfo] = []
    if not self.worktree_base.exists():
        return results

    # Only match the pipeline-level worktree or "{pipeline_id}-{role}"
    # where {role} is shaped like an AgentRole value (lower-case
    # letters and underscores, no hyphens).  A naive `startswith`
    # match collides when one pipeline ID is a prefix of another —
    # e.g. `issue-1758` would spuriously match
    # `issue-1758-worktree-fix-tester` (#1865).
    #
    # Assumes AgentRole values match [a-z_]+ — update if the enum
    # gains values with digits or other characters.
    per_agent = re.compile(rf"{re.escape(pipeline_id)}-[a-z_]+")
    for entry in self.worktree_base.iterdir():
        if not entry.is_dir():
            continue
        if entry.name != pipeline_id and not per_agent.fullmatch(entry.name):
            continue
        for repo_dir in entry.iterdir():
            if not repo_dir.is_dir():
                continue
            # Try to read branch from .git file
            branch = ""
            git_file = repo_dir / ".git"
            if git_file.exists() and git_file.is_file():
                try:
                    gitdir_content = git_file.read_text().strip()
                    if gitdir_content.startswith("gitdir: "):
                        gitdir_path = Path(gitdir_content[8:])
                        if not gitdir_path.is_absolute():
                            gitdir_path = (repo_dir / gitdir_path).resolve()
                        head_file = gitdir_path / "HEAD"
                        if head_file.exists():
                            head_content = head_file.read_text().strip()
                            if head_content.startswith("ref: refs/heads/"):
                                branch = head_content[16:]
                except OSError:
                    pass

            results.append(
                WorktreeInfo(
                    container_id=entry.name,
                    repo_name=repo_dir.name,
                    branch=branch,
                    worktree_path=repo_dir,
                    git_dir=None,
                )
            )

    return results
