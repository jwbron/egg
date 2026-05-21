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
        # The closing ``` must start at a line boundary (#2743) — otherwise
        # a nested fence inside a YAML block scalar truncates the capture.
        yaml_pattern = (
            r"```yaml\s*\n\s*#\s*yaml-tasks\s*\n"
            r"((?:.*\n)*?)[ ]{0,3}```\s*(?:\n|$)"
        )
        match = re.search(yaml_pattern, content)

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
            # Canonical key is ``slices`` (#2137); ``phases`` is the
            # legacy alias still accepted by every other parser. Accept
            # either; ``slices`` wins when both are present so the check
            # mirrors ``parse_phases_from_yaml``.
            if "slices" in parsed:
                top_key = "slices"
            elif "phases" in parsed:
                top_key = "phases"
            else:
                top_key = None
                errors.append("Missing 'slices' (or legacy 'phases') field")
            if top_key is not None:
                top_value = parsed[top_key]
                if not isinstance(top_value, list):
                    errors.append(f"'{top_key}' must be a list")
                else:
                    for i, slice_entry in enumerate(top_value):
                        if not isinstance(slice_entry, dict):
                            errors.append(f"Slice {i + 1} must be a dictionary")
                            continue
                        if "id" not in slice_entry:
                            errors.append(f"Slice {i + 1} missing 'id' field")
                        if "tasks" not in slice_entry:
                            errors.append(f"Slice {i + 1} missing 'tasks' field")
                        elif not isinstance(slice_entry.get("tasks"), list):
                            errors.append(f"Slice {i + 1} 'tasks' must be a list")

        if errors:
            return self.create_result(
                status=CheckStatus.FAIL,
                message=f"Invalid yaml-tasks structure: {errors[0]}",
                details={"errors": errors},
                fixable=False,
            )

        slices_count = len(parsed.get("slices", parsed.get("phases", [])))
        return self.create_result(
            status=CheckStatus.PASS,
            message="Plan yaml-tasks block is valid",
            details={
                "slices_count": slices_count,
                "has_pr_metadata": "pr" in parsed,
            },
        )
