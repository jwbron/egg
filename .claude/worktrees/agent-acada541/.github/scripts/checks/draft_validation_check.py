"""
Check that draft files exist and have required sections.
"""

import re

from .base import CheckResult, CheckRunner, CheckStatus


class DraftValidationCheck(CheckRunner):
    """Validate that draft analysis files exist and have required content."""

    @property
    def check_id(self) -> str:
        return "check-draft-validation"

    def run(self) -> CheckResult:
        """Run the draft validation check.

        Returns:
            CheckResult indicating whether the draft file is valid.
        """
        issue_number = self.contract.issue.number
        draft_path = self.repo_root / ".egg-state" / "drafts" / f"{issue_number}-analysis.md"

        # Check if draft exists
        if not draft_path.exists():
            return self.create_result(
                status=CheckStatus.FAIL,
                message=f"Draft file not found: {draft_path.relative_to(self.repo_root)}",
                details={"expected_path": str(draft_path.relative_to(self.repo_root))},
                fixable=False,
            )

        # Read draft content
        try:
            content = draft_path.read_text()
        except Exception as e:
            return self.create_result(
                status=CheckStatus.FAIL,
                message=f"Failed to read draft file: {e}",
                fixable=False,
            )

        # Check for required sections
        # Draft files should have at least a summary section
        required_patterns = [
            (r"^#\s+", "heading (starts with #)"),
            (
                r"(?:summary|overview|analysis)",
                "summary/overview/analysis section (case-insensitive)",
            ),
        ]

        missing_sections: list[str] = []
        for pattern, description in required_patterns:
            if not re.search(pattern, content, re.IGNORECASE | re.MULTILINE):
                missing_sections.append(description)

        if missing_sections:
            return self.create_result(
                status=CheckStatus.FAIL,
                message=f"Draft file missing required sections: {', '.join(missing_sections)}",
                details={"missing": missing_sections},
                fixable=False,
            )

        # Check minimum content length
        if len(content.strip()) < 100:
            return self.create_result(
                status=CheckStatus.FAIL,
                message="Draft file is too short (minimum 100 characters)",
                details={"length": len(content.strip())},
                fixable=False,
            )

        return self.create_result(
            status=CheckStatus.PASS,
            message="Draft file is valid",
            details={"path": str(draft_path.relative_to(self.repo_root))},
        )
