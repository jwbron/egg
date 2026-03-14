"""
Check that plan files have valid yaml-tasks blocks.
"""

import re

import yaml

from .base import CheckResult, CheckRunner, CheckStatus


class PlanYamlCheck(CheckRunner):
    """Validate that plan files have valid yaml-tasks blocks."""

    @property
    def check_id(self) -> str:
        return "check-plan-yaml"

    def run(self) -> CheckResult:
        """Run the plan YAML validation check.

        Returns:
            CheckResult indicating whether the plan yaml-tasks block is valid.
        """
        issue_number = self.contract.issue.number
        plan_path = self.repo_root / ".egg-state" / "drafts" / f"{issue_number}-plan.md"

        # Check if plan exists
        if not plan_path.exists():
            return self.create_result(
                status=CheckStatus.FAIL,
                message=f"Plan file not found: {plan_path.relative_to(self.repo_root)}",
                details={"expected_path": str(plan_path.relative_to(self.repo_root))},
                fixable=False,
            )

        # Read plan content
        try:
            content = plan_path.read_text()
        except Exception as e:
            return self.create_result(
                status=CheckStatus.FAIL,
                message=f"Failed to read plan file: {e}",
                fixable=False,
            )

        # Extract yaml-tasks block
        # Pattern: ```yaml followed by # yaml-tasks comment, then content, then ```
        yaml_pattern = r"```yaml\s*\n\s*#\s*yaml-tasks\s*\n(.*?)```"
        match = re.search(yaml_pattern, content, re.DOTALL)

        if not match:
            return self.create_result(
                status=CheckStatus.FAIL,
                message="Plan file missing yaml-tasks block",
                details={"hint": "Plan should contain a ```yaml block with # yaml-tasks comment"},
                fixable=False,
            )

        yaml_content = match.group(1)

        # Parse the YAML
        try:
            parsed = yaml.safe_load(yaml_content)
        except yaml.YAMLError as e:
            return self.create_result(
                status=CheckStatus.FAIL,
                message=f"Invalid YAML in yaml-tasks block: {e}",
                details={"yaml_error": str(e)},
                fixable=False,
            )

        # Validate structure
        errors: list[str] = []

        if not isinstance(parsed, dict):
            errors.append("yaml-tasks must be a dictionary")
        else:
            # Check for required fields
            if "phases" not in parsed:
                errors.append("Missing 'phases' field")
            elif not isinstance(parsed["phases"], list):
                errors.append("'phases' must be a list")
            else:
                for i, phase in enumerate(parsed["phases"]):
                    if not isinstance(phase, dict):
                        errors.append(f"Phase {i + 1} must be a dictionary")
                        continue
                    if "id" not in phase:
                        errors.append(f"Phase {i + 1} missing 'id' field")
                    if "tasks" not in phase:
                        errors.append(f"Phase {i + 1} missing 'tasks' field")
                    elif not isinstance(phase.get("tasks"), list):
                        errors.append(f"Phase {i + 1} 'tasks' must be a list")

        if errors:
            return self.create_result(
                status=CheckStatus.FAIL,
                message=f"Invalid yaml-tasks structure: {errors[0]}",
                details={"errors": errors},
                fixable=False,
            )

        return self.create_result(
            status=CheckStatus.PASS,
            message="Plan yaml-tasks block is valid",
            details={
                "phases_count": len(parsed.get("phases", [])),
                "has_pr_metadata": "pr" in parsed,
            },
        )
