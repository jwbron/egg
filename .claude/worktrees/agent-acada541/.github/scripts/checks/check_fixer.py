"""
Check that attempts to auto-fix issues marked as fixable.
"""

import subprocess

from .base import CheckResult, CheckRunner, CheckStatus


class CheckFixer(CheckRunner):
    """Attempt to auto-fix issues using make fix or similar."""

    @property
    def check_id(self) -> str:
        return "check-fixer"

    def run(self) -> CheckResult:
        """Run the auto-fix check.

        Returns:
            CheckResult indicating whether fixes were applied.
        """
        # Check if Makefile exists with fix target
        makefile_path = self.repo_root / "Makefile"
        has_makefile = makefile_path.exists()

        if has_makefile:
            # Check if fix target exists
            try:
                content = makefile_path.read_text()
                has_fix_target = "fix:" in content or "fix :" in content
            except Exception:
                has_fix_target = False
        else:
            has_fix_target = False

        if not has_fix_target:
            return self.create_result(
                status=CheckStatus.SKIP,
                message="No fix target found in Makefile",
                details={"hint": "Add a 'fix' target to your Makefile for auto-fixing"},
            )

        # Run make fix
        try:
            result = subprocess.run(
                ["make", "fix"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
            )

            if result.returncode == 0:
                # Check if there are any changes
                git_status = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                )
                has_changes = bool(git_status.stdout.strip())

                if has_changes:
                    return self.create_result(
                        status=CheckStatus.PASS,
                        message="Auto-fix applied changes",
                        details={
                            "command": "make fix",
                            "changes_made": True,
                        },
                    )
                else:
                    return self.create_result(
                        status=CheckStatus.PASS,
                        message="Auto-fix ran successfully (no changes needed)",
                        details={
                            "command": "make fix",
                            "changes_made": False,
                        },
                    )
            else:
                return self.create_result(
                    status=CheckStatus.FAIL,
                    message="Auto-fix failed",
                    details={
                        "command": "make fix",
                        "stdout": result.stdout[-2000:] if result.stdout else "",
                        "stderr": result.stderr[-2000:] if result.stderr else "",
                    },
                    fixable=False,
                )

        except subprocess.TimeoutExpired:
            return self.create_result(
                status=CheckStatus.FAIL,
                message="Auto-fix timed out after 5 minutes",
                details={"command": "make fix"},
                fixable=False,
            )
        except Exception as e:
            return self.create_result(
                status=CheckStatus.FAIL,
                message=f"Failed to run auto-fix: {e}",
                fixable=False,
            )
