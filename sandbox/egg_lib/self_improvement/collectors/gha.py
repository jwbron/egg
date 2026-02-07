"""GitHub Actions log collector.

This module collects logs from GitHub Actions workflow runs using the gh CLI.
Logs are downloaded as ZIP archives, extracted, and parsed into RunLog format.
"""

import json
import os
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from ..config import EGG_WORKFLOWS
from .base import LogCollector, RunLog


class GHALogCollector(LogCollector):
    """Collects logs from GitHub Actions via gh api.

    Uses the gh CLI to fetch workflow runs and download log archives.
    Each run's logs are extracted from ZIP format and aggregated into
    a single RunLog instance.
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
        runs: list[dict[str, str | int]] = []
        for line in result.stdout.strip().split("\n"):
            if line:
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, list):
                        runs.extend(parsed)
                except json.JSONDecodeError:
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
        """Download and extract logs for a single run.

        Args:
            run: Workflow run dictionary from the GitHub API

        Returns:
            RunLog instance for this run, or None if processing failed
        """
        run_id = run.get("id")
        if not run_id:
            return None

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "logs.zip"
            extract_dir = Path(tmpdir) / "logs"

            # Download logs ZIP
            download_result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"/repos/{self.repo}/actions/runs/{run_id}/logs",
                ],
                capture_output=True,
                check=False,
            )

            if download_result.returncode != 0:
                # Logs may not be available (e.g., run still in progress)
                return None

            # Write binary content to file
            zip_path.write_bytes(download_result.stdout)

            # Extract ZIP
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)
            except zipfile.BadZipFile:
                return None

            # Parse and aggregate all job logs
            logs = self._aggregate_job_logs(extract_dir)

            # Parse timestamps
            created_at_str = str(run.get("created_at", ""))
            updated_at_str = str(run.get("updated_at", ""))

            started_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            completed_at = None
            if updated_at_str:
                completed_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))

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

    def _aggregate_job_logs(self, extract_dir: Path) -> str:
        """Combine all job log files into a single string.

        Args:
            extract_dir: Directory containing extracted log files

        Returns:
            Combined log content with job separators
        """
        all_logs: list[str] = []
        for root, _dirs, files in os.walk(extract_dir):
            for file in sorted(files):
                if file.endswith(".txt"):
                    path = Path(root) / file
                    try:
                        content = path.read_text(errors="replace")
                        all_logs.append(f"=== {file} ===\n{content}")
                    except OSError:
                        continue
        return "\n\n".join(all_logs)
