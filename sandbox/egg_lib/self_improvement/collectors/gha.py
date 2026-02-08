"""GitHub Actions log collector.

This module collects logs from GitHub Actions workflow runs using the gh CLI.
Logs are fetched via `gh run view --log` which returns plain-text output.
"""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ..config import EGG_WORKFLOWS
from .base import LogCollector, RunLog


class GHALogCollector(LogCollector):
    """Collects logs from GitHub Actions via gh CLI.

    Uses the gh CLI to fetch workflow runs and retrieve logs via
    `gh run view --log` which returns plain-text output directly.
    """

    def __init__(self, repo: str | None = None) -> None:
        """Initialize the collector.

        Args:
            repo: Repository in "owner/repo" format. If not provided,
                  will be detected from the current git repository.
        """
        self.repo = repo or self._get_repo()

    def _get_repo(self) -> str:
        """Get the current repository from gh CLI."""
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def collect(self, since: datetime) -> list[RunLog]:
        """Collect logs from GitHub Actions runs since the given time.

        Args:
            since: Only collect logs from runs that started after this time

        Returns:
            List of RunLog instances for matching runs
        """
        runs = self._fetch_workflow_runs(since)
        result = []
        for run in runs:
            run_log = self._process_run(run)
            if run_log:
                result.append(run_log)
        return result

    def _fetch_workflow_runs(self, since: datetime) -> list[dict[str, str | int]]:
        """Fetch recent workflow runs via gh api.

        Args:
            since: Only include runs created after this time

        Returns:
            List of workflow run dictionaries from the GitHub API
        """
        # Ensure since has timezone info for comparison
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)

        result = subprocess.run(
            [
                "gh",
                "api",
                f"/repos/{self.repo}/actions/runs",
                "--paginate",
                "--jq",
                ".workflow_runs",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            return []

        # Parse the JSON output (may be multiple arrays from pagination)
        # Each page returns a separate JSON array, so we need to parse each line
        runs: list[dict[str, str | int]] = []
        for line_num, line in enumerate(result.stdout.strip().split("\n"), 1):
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, list):
                    runs.extend(parsed)
                # Note: If a single page is malformed, we skip it and continue
                # with other pages to maximize data collection
            except json.JSONDecodeError as e:
                # Log warning but continue processing remaining pages
                import sys

                print(
                    f"Warning: Failed to parse page {line_num} of workflow runs: {e}",
                    file=sys.stderr,
                )
                continue

        # Filter to egg-triggered workflows and runs after since
        filtered_runs = []
        for run in runs:
            # Check if this is an egg workflow
            workflow_path = str(run.get("path", ""))
            workflow_name = Path(workflow_path).name if workflow_path else ""
            if workflow_name not in EGG_WORKFLOWS:
                continue

            # Check if run is after since
            created_at_str = str(run.get("created_at", ""))
            if created_at_str:
                created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                if created_at > since:
                    filtered_runs.append(run)

        return filtered_runs

    def _process_run(self, run: dict[str, str | int]) -> RunLog | None:
        """Fetch logs for a single run via gh run view --log.

        Args:
            run: Workflow run dictionary from the GitHub API

        Returns:
            RunLog instance for this run, or None if processing failed
        """
        run_id = run.get("id")
        if not run_id:
            return None

        # Fetch logs via gh run view --log (returns plain text)
        log_result = subprocess.run(
            [
                "gh",
                "run",
                "view",
                str(run_id),
                "--repo",
                self.repo,
                "--log",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if log_result.returncode != 0:
            # Logs may not be available (e.g., run still in progress)
            return None

        logs = log_result.stdout

        # Parse timestamps
        created_at_str = str(run.get("created_at", ""))
        updated_at_str = str(run.get("updated_at", ""))

        try:
            started_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        except ValueError:
            # Skip runs with malformed created_at timestamps
            return None

        completed_at = None
        if updated_at_str:
            try:
                completed_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            except ValueError:
                # If updated_at is malformed, just leave completed_at as None
                pass

        # Determine status
        conclusion = run.get("conclusion")
        status_val = run.get("status")
        if conclusion == "success":
            status = "success"
        elif conclusion == "failure":
            status = "failure"
        elif conclusion == "cancelled":
            status = "cancelled"
        elif status_val == "in_progress" or status_val == "queued":
            status = "running"
        else:
            status = "failure"  # Default to failure for unknown states

        return RunLog(
            run_id=str(run_id),
            source="gha",
            started_at=started_at,
            completed_at=completed_at,
            status=status,  # type: ignore[arg-type]
            trigger=str(run.get("event", "unknown")),
            logs=logs,
            metadata={
                "workflow": str(run.get("name", "")),
                "head_branch": str(run.get("head_branch", "")),
                "run_number": int(run.get("run_number", 0)),
                "html_url": str(run.get("html_url", "")),
                "workflow_path": str(run.get("path", "")),
            },
        )
