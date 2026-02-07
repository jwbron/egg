"""GitHub issue creation with deduplication.

This module handles creating GitHub issues for detected problems,
with fingerprint-based deduplication to avoid duplicate issues.
"""

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from ..config import ISSUE_LABEL_PREFIX, ISSUE_TITLE_PREFIX
from ..detection.engine import Detection, Severity

# Fingerprint marker in issue body for deduplication
FINGERPRINT_PREFIX = "<!-- fingerprint:"
FINGERPRINT_SUFFIX = " -->"


def generate_fingerprint(detection: Detection) -> str:
    """Generate a unique fingerprint for a detection.

    The fingerprint is based on the rule ID and category, ensuring
    that the same type of issue gets the same fingerprint regardless
    of specific evidence or run IDs.

    Args:
        detection: The detection to fingerprint

    Returns:
        12-character hex string fingerprint
    """
    content = f"{detection.category}:{detection.rule_id}"
    return hashlib.sha256(content.encode()).hexdigest()[:12]


@dataclass
class IssueResult:
    """Result of an issue creation or update operation.

    Attributes:
        success: Whether the operation succeeded
        issue_number: GitHub issue number (new or existing)
        action: What action was taken ("created", "updated", "skipped")
        url: URL to the issue
        error: Error message if operation failed
    """

    success: bool
    issue_number: int | None = None
    action: str = ""
    url: str = ""
    error: str = ""


class IssueCreator:
    """Creates GitHub issues for detected problems with deduplication.

    The IssueCreator uses fingerprints embedded in issue bodies to
    detect existing issues for the same problem, preventing duplicates.
    """

    def __init__(self, repo: str | None = None, dry_run: bool = False) -> None:
        """Initialize the issue creator.

        Args:
            repo: Repository in owner/repo format. Auto-detected if not provided.
            dry_run: If True, don't actually create issues, just report what would happen.
        """
        self.repo = repo or self._detect_repo()
        self.dry_run = dry_run

    def _detect_repo(self) -> str:
        """Detect the repository from git remote.

        Returns:
            Repository in owner/repo format
        """
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to detect repository: {result.stderr}")
        return result.stdout.strip()

    def create_or_update(self, detection: Detection) -> IssueResult:
        """Create a new issue or update an existing one for a detection.

        Args:
            detection: The detection to create an issue for

        Returns:
            IssueResult describing what happened
        """
        fingerprint = generate_fingerprint(detection)

        # Check for existing issue with this fingerprint
        existing = self._find_existing_issue(fingerprint)

        if existing:
            return self._update_issue(existing, detection, fingerprint)
        else:
            return self._create_issue(detection, fingerprint)

    def _find_existing_issue(self, fingerprint: str) -> dict[str, Any] | None:
        """Search for an existing open issue with the given fingerprint.

        Args:
            fingerprint: The fingerprint to search for

        Returns:
            Issue data dict if found, None otherwise
        """
        try:
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "list",
                    "--repo",
                    self.repo,
                    "--label",
                    ISSUE_LABEL_PREFIX,
                    "--state",
                    "open",
                    "--json",
                    "number,body,url",
                    "--limit",
                    "100",
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                return None

            issues: list[dict[str, Any]] = json.loads(result.stdout)
            fingerprint_marker = f"{FINGERPRINT_PREFIX}{fingerprint}{FINGERPRINT_SUFFIX}"

            for issue in issues:
                if fingerprint_marker in issue.get("body", ""):
                    return issue

        except (subprocess.SubprocessError, json.JSONDecodeError) as e:
            print(f"Warning: Error searching for existing issues: {e}", file=sys.stderr)

        return None

    def _create_issue(self, detection: Detection, fingerprint: str) -> IssueResult:
        """Create a new GitHub issue for a detection.

        Args:
            detection: The detection to create an issue for
            fingerprint: The fingerprint to embed

        Returns:
            IssueResult describing the outcome
        """
        title = f"{ISSUE_TITLE_PREFIX} {detection.title}"
        body = self._format_issue_body(detection, fingerprint)
        labels = self._get_labels(detection)

        if self.dry_run:
            print(f"[DRY RUN] Would create issue: {title}", file=sys.stderr)
            return IssueResult(
                success=True,
                action="skipped",
                error="Dry run - issue not created",
            )

        try:
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "create",
                    "--repo",
                    self.repo,
                    "--title",
                    title,
                    "--body",
                    body,
                    "--label",
                    ",".join(labels),
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                return IssueResult(
                    success=False,
                    action="failed",
                    error=result.stderr.strip(),
                )

            # Parse the issue URL from output
            url = result.stdout.strip()
            # Extract issue number from URL (e.g., .../issues/123)
            issue_number = int(url.split("/")[-1]) if "/" in url else None

            return IssueResult(
                success=True,
                issue_number=issue_number,
                action="created",
                url=url,
            )

        except subprocess.SubprocessError as e:
            return IssueResult(
                success=False,
                action="failed",
                error=str(e),
            )

    def _update_issue(
        self, existing: dict[str, Any], detection: Detection, fingerprint: str
    ) -> IssueResult:
        """Update an existing issue with new occurrence information.

        Args:
            existing: The existing issue data
            detection: The new detection data
            fingerprint: The fingerprint

        Returns:
            IssueResult describing the outcome
        """
        issue_number = existing["number"]
        url = existing.get("url", "")

        if self.dry_run:
            print(f"[DRY RUN] Would update issue #{issue_number}", file=sys.stderr)
            return IssueResult(
                success=True,
                issue_number=issue_number,
                action="skipped",
                url=url,
                error="Dry run - issue not updated",
            )

        # Add a comment with the new occurrence information
        comment = self._format_update_comment(detection)

        try:
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "comment",
                    str(issue_number),
                    "--repo",
                    self.repo,
                    "--body",
                    comment,
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                return IssueResult(
                    success=False,
                    issue_number=issue_number,
                    action="failed",
                    url=url,
                    error=result.stderr.strip(),
                )

            return IssueResult(
                success=True,
                issue_number=issue_number,
                action="updated",
                url=url,
            )

        except subprocess.SubprocessError as e:
            return IssueResult(
                success=False,
                issue_number=issue_number,
                action="failed",
                url=url,
                error=str(e),
            )

    def _format_issue_body(self, detection: Detection, fingerprint: str) -> str:
        """Format the issue body for a new issue.

        Args:
            detection: The detection data
            fingerprint: The fingerprint to embed

        Returns:
            Formatted issue body
        """
        evidence_text = "\n".join(f"- `{e}`" for e in detection.evidence[:5])
        if not evidence_text:
            evidence_text = "_No specific log evidence captured_"

        runs_text = "\n".join(f"- {rid}" for rid in detection.run_ids[:10])
        if not runs_text:
            runs_text = "_No specific runs identified_"

        recommendation = detection.recommendation or "Investigate and address the root cause."

        return f"""{FINGERPRINT_PREFIX}{fingerprint}{FINGERPRINT_SUFFIX}

## Summary

{detection.description}

## Detection Details

| Field | Value |
|-------|-------|
| Category | `{detection.category}` |
| Severity | **{detection.severity.value.upper()}** |
| Rule ID | `{detection.rule_id}` |
| Occurrences | {detection.occurrence_count} |
| Fingerprint | `{fingerprint}` |

## Evidence

{evidence_text}

## Affected Runs

{runs_text}

## Recommended Actions

{recommendation}

---

*This issue was automatically created by the egg self-improvement analyzer.*

Authored-by: egg
"""

    def _format_update_comment(self, detection: Detection) -> str:
        """Format a comment for updating an existing issue.

        Args:
            detection: The new detection data

        Returns:
            Formatted comment body
        """
        runs_text = ", ".join(detection.run_ids[:5])
        if len(detection.run_ids) > 5:
            runs_text += f", ... (+{len(detection.run_ids) - 5} more)"

        return f"""## New Occurrences Detected

**Date:** {self._get_current_date()}
**New occurrences:** {detection.occurrence_count}
**Affected runs:** {runs_text}

### Recent Evidence

```
{chr(10).join(detection.evidence[:3])}
```

---

*Updated by egg self-improvement analyzer*

Authored-by: egg
"""

    def _get_labels(self, detection: Detection) -> list[str]:
        """Get labels for an issue based on detection severity.

        Args:
            detection: The detection

        Returns:
            List of label strings
        """
        labels = [ISSUE_LABEL_PREFIX]

        severity_label = f"{ISSUE_LABEL_PREFIX}:{detection.severity.value}"
        labels.append(severity_label)

        return labels

    def _get_current_date(self) -> str:
        """Get current date in ISO format.

        Returns:
            Current date string
        """
        from datetime import UTC, datetime

        return datetime.now(UTC).strftime("%Y-%m-%d")

    def create_issues_for_detections(
        self,
        detections: list[Detection],
        min_severity: Severity = Severity.HIGH,
    ) -> list[IssueResult]:
        """Create issues for multiple detections.

        Args:
            detections: List of detections to process
            min_severity: Minimum severity level to create issues for

        Returns:
            List of IssueResult for each detection processed
        """
        severity_order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
        min_order = severity_order[min_severity]

        results = []
        for detection in detections:
            if severity_order[detection.severity] <= min_order:
                result = self.create_or_update(detection)
                results.append(result)
                if result.success:
                    print(
                        f"  {result.action.capitalize()}: {detection.title} "
                        f"(#{result.issue_number or 'N/A'})",
                        file=sys.stderr,
                    )
                else:
                    print(f"  Failed: {detection.title} - {result.error}", file=sys.stderr)

        return results
