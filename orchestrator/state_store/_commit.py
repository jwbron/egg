"""Git-commit helpers for ``StateStore`` (#3312).

Method bodies extracted verbatim from the pre-split ``state_store.py`` as
module-level functions taking ``self`` explicitly. The barrel binds these onto
the ``StateStore`` class.
"""

from models import Pipeline

from . import logger
from ._errors import GitOperationError


def _commit_state(self, pipeline: Pipeline, message: str | None = None) -> str:
    """Commit pipeline state to the state branch.

    Commits directly in the persistent worktree.

    Args:
        pipeline: Pipeline being saved
        message: Optional commit message

    Returns:
        Commit SHA (on the state branch)

    Raises:
        GitOperationError: If commit fails for non-benign reasons (e.g., index
            corruption, disk full). Benign "nothing to commit" errors are caught
            and the current HEAD SHA is returned instead.
    """
    if not message:
        message = self._generate_commit_message(pipeline)

    path = self._get_pipeline_path(pipeline.id)
    rel_path = str(path.relative_to(self.worktree))

    wt = self.worktree
    # Hold lock for entire add→diff→commit sequence so concurrent
    # operations cannot interleave and stage into the wrong commit.
    with self._git_op():
        self._run_git("add", rel_path, cwd=wt)

        result = self._run_git("diff", "--cached", "--quiet", cwd=wt, check=False)
        if result.returncode == 0:
            # No changes staged - return current HEAD or empty string for unborn branch
            head_result = self._run_git("rev-parse", "HEAD", cwd=wt, check=False)
            return head_result.stdout.strip() if head_result.returncode == 0 else ""

        try:
            self._run_git("commit", "--no-verify", "-m", message, cwd=wt)
        except GitOperationError as e:
            err_msg = str(e).lower()
            if "nothing to commit" in err_msg or "no changes added" in err_msg:
                logger.warning(
                    "Benign commit failure (nothing to commit) for %s, returning current HEAD: %s",
                    pipeline.id,
                    e,
                )
                head_result = self._run_git("rev-parse", "HEAD", cwd=wt, check=False)
                return head_result.stdout.strip() if head_result.returncode == 0 else ""
            raise
        sha = self._run_git("rev-parse", "HEAD", cwd=wt).stdout.strip()

        # Best-effort async push to remote after every commit
        self._sync_to_remote_async()

        return sha


def _get_current_commit(self) -> str:
    """Get the current HEAD commit SHA."""
    result = self._run_git("rev-parse", "HEAD")
    return result.stdout.strip()


def _generate_commit_message(self, pipeline: Pipeline) -> str:
    """Generate a commit message for pipeline state update."""
    return f"Update pipeline state: {pipeline.id} ({pipeline.status.value})"
