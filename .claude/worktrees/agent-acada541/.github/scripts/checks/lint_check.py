"""
Check that runs the project linter.
"""

import subprocess

from .base import CheckResult, CheckRunner, CheckStatus


class LintCheck(CheckRunner):
    """Run the project linter and report results."""

    @property
    def check_id(self) -> str:
        return "check-lint"

    def run(self) -> CheckResult:
        """Run the lint check.

        Returns:
            CheckResult indicating whether linting passed.
        """
        # Check if Makefile exists with lint target
        makefile_path = self.repo_root / "Makefile"
        has_makefile = makefile_path.exists()

        if has_makefile:
            # Check if lint target exists
            try:
                content = makefile_path.read_text()
                has_lint_target = "lint:" in content or "lint :" in content
            except Exception:
                has_lint_target = False
        else:
            has_lint_target = False

        # Try make lint first
        if has_lint_target:
            try:
                result = subprocess.run(
                    ["make", "lint"],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minute timeout
                )

                if result.returncode == 0:
                    return self.create_result(
                        status=CheckStatus.PASS,
                        message="Linting passed",
                        details={"command": "make lint"},
                    )
                else:
                    return self.create_result(
                        status=CheckStatus.FAIL,
                        message="Linting failed",
                        details={
                            "command": "make lint",
                            "stdout": result.stdout[-2000:] if result.stdout else "",
                            "stderr": result.stderr[-2000:] if result.stderr else "",
                        },
                        fixable=True,  # Lint errors are often auto-fixable
                    )

            except subprocess.TimeoutExpired:
                return self.create_result(
                    status=CheckStatus.FAIL,
                    message="Linting timed out after 5 minutes",
                    details={"command": "make lint"},
                    fixable=False,
                )
            except Exception as e:
                return self.create_result(
                    status=CheckStatus.FAIL,
                    message=f"Failed to run linter: {e}",
                    fixable=False,
                )

        # No lint target found, skip the check
        return self.create_result(
            status=CheckStatus.SKIP,
            message="No lint target found in Makefile",
            details={"hint": "Add a 'lint' target to your Makefile"},
        )
