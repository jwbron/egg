"""
Check for merge conflict markers in tracked files.
"""

import subprocess

from .base import CheckResult, CheckRunner, CheckStatus


class MergeConflictCheck(CheckRunner):
    """Check for merge conflict markers in the repository."""

    @property
    def check_id(self) -> str:
        return "check-merge-conflict"

    def run(self) -> CheckResult:
        """Run the merge conflict check.

        Returns:
            CheckResult indicating whether merge conflict markers were found.
        """
        # Get list of tracked files
        try:
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
            tracked_files = result.stdout.strip().split("\n")
        except subprocess.CalledProcessError as e:
            return self.create_result(
                status=CheckStatus.FAIL,
                message=f"Failed to get tracked files: {e}",
                details={"stderr": e.stderr},
                fixable=False,
            )

        # Search for conflict markers
        # Only use <<<<<<< as the definitive marker - it's the most distinctive
        # indicator of an actual merge conflict. The ======= marker can appear
        # legitimately in markdown horizontal rules, documentation, ASCII art, etc.
        files_with_conflicts: list[str] = []

        for file_path in tracked_files:
            if not file_path:
                continue

            full_path = self.repo_root / file_path
            if not full_path.exists() or not full_path.is_file():
                continue

            try:
                content = full_path.read_text(errors="ignore")
                if "<<<<<<<" in content:
                    files_with_conflicts.append(file_path)
            except Exception:
                # Skip files that can't be read (binary files, etc.)
                continue

        if files_with_conflicts:
            return self.create_result(
                status=CheckStatus.FAIL,
                message=f"Found merge conflict markers in {len(files_with_conflicts)} file(s)",
                details={"files": files_with_conflicts},
                fixable=False,
            )

        return self.create_result(
            status=CheckStatus.PASS,
            message="No merge conflict markers found",
        )
