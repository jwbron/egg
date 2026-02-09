"""Tests for .github/scripts/checks/ check scripts."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / ".github" / "scripts"))

from egg_contracts import CheckResult, CheckStatus, Contract, IssueInfo


class TestCheckRunnerBase:
    """Tests for the CheckRunner base class."""

    def test_check_runner_create_result(self):
        """Test CheckRunner.create_result helper."""
        from checks.base import CheckRunner

        # Create a concrete implementation for testing
        class TestCheck(CheckRunner):
            @property
            def check_id(self) -> str:
                return "check-test"

            def run(self) -> CheckResult:
                return self.create_result(CheckStatus.PASS, "Test passed")

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = TestCheck(contract, Path("/tmp"))
        result = check.run()

        assert result.check_id == "check-test"
        assert result.status == CheckStatus.PASS
        assert result.message == "Test passed"

    def test_check_runner_output_result(self, capsys):
        """Test CheckRunner.output_result outputs valid JSON."""
        from checks.base import CheckRunner

        class TestCheck(CheckRunner):
            @property
            def check_id(self) -> str:
                return "check-test"

            def run(self) -> CheckResult:
                return self.create_result(
                    CheckStatus.FAIL,
                    "Test failed",
                    details={"error": "reason"},
                    fixable=True,
                )

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = TestCheck(contract, Path("/tmp"))
        result = check.run()
        check.output_result(result)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["check_id"] == "check-test"
        assert output["status"] == "fail"
        assert output["message"] == "Test failed"
        assert output["details"] == {"error": "reason"}
        assert output["fixable"] is True


class TestMergeConflictCheck:
    """Tests for merge_conflict_check.py."""

    def test_no_conflicts_passes(self, tmp_path):
        """Test that a repo without conflicts passes."""
        from checks.merge_conflict_check import MergeConflictCheck

        # Create a fake git repo structure
        (tmp_path / ".git").mkdir()
        (tmp_path / "file.py").write_text("print('hello')")

        # Mock git ls-files to return our test file
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="file.py\n",
                stderr="",
            )

            contract = Contract(
                issue=IssueInfo(number=123, title="Test", url="https://example.com"),
            )
            check = MergeConflictCheck(contract, tmp_path)
            result = check.run()

        assert result.status == CheckStatus.PASS
        assert "No merge conflict markers" in result.message

    def test_with_conflicts_fails(self, tmp_path):
        """Test that a file with conflict markers fails."""
        from checks.merge_conflict_check import MergeConflictCheck

        # Create a file with conflict markers
        conflict_content = """
def hello():
<<<<<<< HEAD
    print("hello from main")
=======
    print("hello from feature")
>>>>>>> feature
"""
        (tmp_path / "conflict.py").write_text(conflict_content)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="conflict.py\n",
                stderr="",
            )

            contract = Contract(
                issue=IssueInfo(number=123, title="Test", url="https://example.com"),
            )
            check = MergeConflictCheck(contract, tmp_path)
            result = check.run()

        assert result.status == CheckStatus.FAIL
        assert "conflict.py" in result.details["files"]
        assert result.fixable is False


class TestDraftValidationCheck:
    """Tests for draft_validation_check.py."""

    def test_missing_draft_fails(self, tmp_path):
        """Test that missing draft file fails."""
        from checks.draft_validation_check import DraftValidationCheck

        (tmp_path / ".egg-state" / "drafts").mkdir(parents=True)

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = DraftValidationCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.FAIL
        assert "not found" in result.message

    def test_valid_draft_passes(self, tmp_path):
        """Test that a valid draft file passes."""
        from checks.draft_validation_check import DraftValidationCheck

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)
        draft_path = drafts_dir / "123-analysis.md"
        draft_path.write_text("""# Summary

This is an overview of the issue. We need to implement a new feature
that will improve the user experience significantly. The analysis shows
that the current implementation has some limitations that we can address.
""")

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = DraftValidationCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.PASS

    def test_too_short_draft_fails(self, tmp_path):
        """Test that a draft that's too short fails."""
        from checks.draft_validation_check import DraftValidationCheck

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)
        draft_path = drafts_dir / "123-analysis.md"
        draft_path.write_text("# Summary\n\nShort")

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = DraftValidationCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.FAIL
        assert "too short" in result.message


class TestPlanYamlCheck:
    """Tests for plan_yaml_check.py."""

    def test_missing_plan_fails(self, tmp_path):
        """Test that missing plan file fails."""
        from checks.plan_yaml_check import PlanYamlCheck

        (tmp_path / ".egg-state" / "drafts").mkdir(parents=True)

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = PlanYamlCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.FAIL
        assert "not found" in result.message

    def test_valid_plan_passes(self, tmp_path):
        """Test that a valid plan file passes."""
        from checks.plan_yaml_check import PlanYamlCheck

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)
        plan_path = drafts_dir / "123-plan.md"
        plan_path.write_text("""# Plan

## Implementation

```yaml
# yaml-tasks
phases:
  - id: 1
    name: Setup
    tasks:
      - id: TASK-1-1
        description: First task
```
""")

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = PlanYamlCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.PASS

    def test_missing_yaml_block_fails(self, tmp_path):
        """Test that plan without yaml-tasks block fails."""
        from checks.plan_yaml_check import PlanYamlCheck

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)
        plan_path = drafts_dir / "123-plan.md"
        plan_path.write_text("# Plan\n\nJust text, no yaml block.")

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = PlanYamlCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.FAIL
        assert "missing yaml-tasks block" in result.message

    def test_invalid_yaml_fails(self, tmp_path):
        """Test that invalid YAML fails."""
        from checks.plan_yaml_check import PlanYamlCheck

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)
        plan_path = drafts_dir / "123-plan.md"
        plan_path.write_text("""# Plan

```yaml
# yaml-tasks
phases:
  - id: 1
    tasks:
      - invalid: [unclosed
```
""")

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = PlanYamlCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.FAIL
        assert "Invalid YAML" in result.message

    def test_missing_phases_fails(self, tmp_path):
        """Test that YAML without phases field fails."""
        from checks.plan_yaml_check import PlanYamlCheck

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)
        plan_path = drafts_dir / "123-plan.md"
        plan_path.write_text("""# Plan

```yaml
# yaml-tasks
pr:
  title: Some PR
```
""")

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = PlanYamlCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.FAIL
        assert "Missing 'phases' field" in result.details["errors"]


class TestLintCheck:
    """Tests for lint_check.py."""

    def test_no_makefile_skips(self, tmp_path):
        """Test that missing Makefile causes skip."""
        from checks.lint_check import LintCheck

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = LintCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.SKIP
        assert "No lint target" in result.message

    def test_no_lint_target_skips(self, tmp_path):
        """Test that Makefile without lint target causes skip."""
        from checks.lint_check import LintCheck

        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = LintCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.SKIP

    def test_lint_passes(self, tmp_path):
        """Test that passing lint check succeeds."""
        from checks.lint_check import LintCheck

        (tmp_path / "Makefile").write_text("lint:\n\techo 'ok'\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Linting passed",
                stderr="",
            )

            contract = Contract(
                issue=IssueInfo(number=123, title="Test", url="https://example.com"),
            )
            check = LintCheck(contract, tmp_path)
            result = check.run()

        assert result.status == CheckStatus.PASS

    def test_lint_fails_is_fixable(self, tmp_path):
        """Test that failing lint check is marked as fixable."""
        from checks.lint_check import LintCheck

        (tmp_path / "Makefile").write_text("lint:\n\tflake8 .\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="file.py:1:1: E501 line too long",
                stderr="",
            )

            contract = Contract(
                issue=IssueInfo(number=123, title="Test", url="https://example.com"),
            )
            check = LintCheck(contract, tmp_path)
            result = check.run()

        assert result.status == CheckStatus.FAIL
        assert result.fixable is True


class TestTestCheck:
    """Tests for test_check.py."""

    def test_no_test_infrastructure_skips(self, tmp_path):
        """Test that no test infrastructure causes skip."""
        from checks.test_check import TestCheck

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = TestCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.SKIP

    def test_tests_pass(self, tmp_path):
        """Test that passing tests succeed."""
        from checks.test_check import TestCheck

        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="All tests passed",
                stderr="",
            )

            contract = Contract(
                issue=IssueInfo(number=123, title="Test", url="https://example.com"),
            )
            check = TestCheck(contract, tmp_path)
            result = check.run()

        assert result.status == CheckStatus.PASS

    def test_tests_fail_not_fixable(self, tmp_path):
        """Test that failing tests are not marked as fixable."""
        from checks.test_check import TestCheck

        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="1 test failed",
                stderr="",
            )

            contract = Contract(
                issue=IssueInfo(number=123, title="Test", url="https://example.com"),
            )
            check = TestCheck(contract, tmp_path)
            result = check.run()

        assert result.status == CheckStatus.FAIL
        assert result.fixable is False


class TestCheckFixer:
    """Tests for check_fixer.py."""

    def test_no_fix_target_skips(self, tmp_path):
        """Test that missing fix target causes skip."""
        from checks.check_fixer import CheckFixer

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = CheckFixer(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.SKIP

    def test_fix_applies_changes(self, tmp_path):
        """Test that fix applies changes successfully."""
        from checks.check_fixer import CheckFixer

        (tmp_path / "Makefile").write_text("fix:\n\tblack .\n")

        with patch("subprocess.run") as mock_run:
            # First call: make fix succeeds
            # Second call: git status shows changes
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="M file.py\n", stderr=""),
            ]

            contract = Contract(
                issue=IssueInfo(number=123, title="Test", url="https://example.com"),
            )
            check = CheckFixer(contract, tmp_path)
            result = check.run()

        assert result.status == CheckStatus.PASS
        assert result.details["changes_made"] is True

    def test_fix_no_changes_needed(self, tmp_path):
        """Test that fix with no changes is still a pass."""
        from checks.check_fixer import CheckFixer

        (tmp_path / "Makefile").write_text("fix:\n\tblack .\n")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),  # No changes
            ]

            contract = Contract(
                issue=IssueInfo(number=123, title="Test", url="https://example.com"),
            )
            check = CheckFixer(contract, tmp_path)
            result = check.run()

        assert result.status == CheckStatus.PASS
        assert result.details["changes_made"] is False
