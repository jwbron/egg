"""
Check that runs the project tests.
"""

import subprocess

from .base import CheckResult, CheckRunner, CheckStatus


class TestCheck(CheckRunner):
    """Run the project tests and report results."""

    @property
    def check_id(self) -> str:
        return "check-test"

    def run(self) -> CheckResult:
        """Run the test check.

        Returns:
            CheckResult indicating whether tests passed.
        """
        # Check if Makefile exists with test target
        makefile_path = self.repo_root / "Makefile"
        has_makefile = makefile_path.exists()

        if has_makefile:
            # Check if test target exists
            try:
                content = makefile_path.read_text()
                has_test_target = "test:" in content or "test :" in content
            except Exception:
                has_test_target = False
        else:
            has_test_target = False

        # Try make test first
        if has_test_target:
            try:
                result = subprocess.run(
                    ["make", "test"],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=600,  # 10 minute timeout
                )

                if result.returncode == 0:
                    return self.create_result(
                        status=CheckStatus.PASS,
                        message="Tests passed",
                        details={"command": "make test"},
                    )
                else:
                    return self.create_result(
                        status=CheckStatus.FAIL,
                        message="Tests failed",
                        details={
                            "command": "make test",
                            "stdout": result.stdout[-2000:] if result.stdout else "",
                            "stderr": result.stderr[-2000:] if result.stderr else "",
                        },
                        fixable=False,  # Test failures require code changes
                    )

            except subprocess.TimeoutExpired:
                return self.create_result(
                    status=CheckStatus.FAIL,
                    message="Tests timed out after 10 minutes",
                    details={"command": "make test"},
                    fixable=False,
                )
            except Exception as e:
                return self.create_result(
                    status=CheckStatus.FAIL,
                    message=f"Failed to run tests: {e}",
                    fixable=False,
                )

        # Check for pytest
        pytest_config = (
            (self.repo_root / "pytest.ini").exists()
            or (self.repo_root / "pyproject.toml").exists()
            or (self.repo_root / "setup.cfg").exists()
        )

        if pytest_config:
            try:
                result = subprocess.run(
                    ["pytest"],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )

                if result.returncode == 0:
                    return self.create_result(
                        status=CheckStatus.PASS,
                        message="Tests passed",
                        details={"command": "pytest"},
                    )
                else:
                    return self.create_result(
                        status=CheckStatus.FAIL,
                        message="Tests failed",
                        details={
                            "command": "pytest",
                            "stdout": result.stdout[-2000:] if result.stdout else "",
                            "stderr": result.stderr[-2000:] if result.stderr else "",
                        },
                        fixable=False,
                    )

            except subprocess.TimeoutExpired:
                return self.create_result(
                    status=CheckStatus.FAIL,
                    message="Tests timed out after 10 minutes",
                    details={"command": "pytest"},
                    fixable=False,
                )
            except FileNotFoundError:
                pass  # pytest not installed, continue to skip
            except Exception as e:
                return self.create_result(
                    status=CheckStatus.FAIL,
                    message=f"Failed to run tests: {e}",
                    fixable=False,
                )

        # No test infrastructure found, skip the check
        return self.create_result(
            status=CheckStatus.SKIP,
            message="No test infrastructure found",
            details={"hint": "Add a 'test' target to your Makefile or configure pytest"},
        )
