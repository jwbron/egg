"""Tests for .github/scripts/checks/ check scripts."""

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add paths for imports
_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "shared"))
sys.path.insert(0, str(_REPO_ROOT / ".github" / "scripts"))

from egg_contracts import CheckResult, CheckStatus, Contract, IssueInfo


def _import_check_module(module_name: str):
    """Import a module from .github/scripts/checks/, handling namespace collisions.

    When pytest collects from orchestrator/tests/, it adds orchestrator/routes/
    to sys.path, which contains a checks.py file that shadows our checks package.
    This helper ensures we import from the correct location.
    """
    full_name = f"checks.{module_name}" if module_name != "checks" else "checks"

    # Remove stale checks modules that may point to the wrong location
    checks_mod = sys.modules.get("checks")
    if checks_mod and not hasattr(checks_mod, "__path__"):
        # It's a flat file (e.g. orchestrator/routes/checks.py), not our package
        stale = [k for k in sys.modules if k == "checks" or k.startswith("checks.")]
        for k in stale:
            del sys.modules[k]

    # Ensure .github/scripts is at the front of sys.path
    scripts_path = str(_REPO_ROOT / ".github" / "scripts")
    if sys.path[0] != scripts_path:
        sys.path.insert(0, scripts_path)

    return importlib.import_module(full_name)


class TestCheckRunnerBase:
    """Tests for the CheckRunner base class."""

    def test_check_runner_create_result(self):
        """Test CheckRunner.create_result helper."""
        CheckRunner = _import_check_module("base").CheckRunner

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
        CheckRunner = _import_check_module("base").CheckRunner

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
        MergeConflictCheck = _import_check_module("merge_conflict_check").MergeConflictCheck

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
        MergeConflictCheck = _import_check_module("merge_conflict_check").MergeConflictCheck

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
        DraftValidationCheck = _import_check_module("draft_validation_check").DraftValidationCheck

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
        DraftValidationCheck = _import_check_module("draft_validation_check").DraftValidationCheck

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
        DraftValidationCheck = _import_check_module("draft_validation_check").DraftValidationCheck

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
        PlanYamlCheck = _import_check_module("plan_yaml_check").PlanYamlCheck

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
        PlanYamlCheck = _import_check_module("plan_yaml_check").PlanYamlCheck

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
        PlanYamlCheck = _import_check_module("plan_yaml_check").PlanYamlCheck

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
        PlanYamlCheck = _import_check_module("plan_yaml_check").PlanYamlCheck

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

    def test_missing_slices_fails(self, tmp_path):
        """Test that YAML without slices/phases field fails."""
        PlanYamlCheck = _import_check_module("plan_yaml_check").PlanYamlCheck

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
        assert "Missing 'slices' (or legacy 'phases') field" in result.details["errors"]

    def test_valid_slices_plan_passes(self, tmp_path):
        """``slices:`` is the canonical key (#2137); the check must
        accept it the same as legacy ``phases:``.
        """
        PlanYamlCheck = _import_check_module("plan_yaml_check").PlanYamlCheck

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)
        plan_path = drafts_dir / "123-plan.md"
        plan_path.write_text("""# Plan

```yaml
# yaml-tasks
slices:
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
        assert result.details["slices_count"] == 1


class TestLintCheck:
    """Tests for lint_check.py."""

    def test_no_makefile_skips(self, tmp_path):
        """Test that missing Makefile causes skip."""
        LintCheck = _import_check_module("lint_check").LintCheck

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = LintCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.SKIP
        assert "No lint target" in result.message

    def test_no_lint_target_skips(self, tmp_path):
        """Test that Makefile without lint target causes skip."""
        LintCheck = _import_check_module("lint_check").LintCheck

        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = LintCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.SKIP

    def test_lint_passes(self, tmp_path):
        """Test that passing lint check succeeds."""
        LintCheck = _import_check_module("lint_check").LintCheck

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
        LintCheck = _import_check_module("lint_check").LintCheck

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
        TestCheck = _import_check_module("test_check").TestCheck

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = TestCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.SKIP

    def test_tests_pass(self, tmp_path):
        """Test that passing tests succeed."""
        TestCheck = _import_check_module("test_check").TestCheck

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
        TestCheck = _import_check_module("test_check").TestCheck

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
        CheckFixer = _import_check_module("check_fixer").CheckFixer

        contract = Contract(
            issue=IssueInfo(number=123, title="Test", url="https://example.com"),
        )
        check = CheckFixer(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.SKIP

    def test_fix_applies_changes(self, tmp_path):
        """Test that fix applies changes successfully."""
        CheckFixer = _import_check_module("check_fixer").CheckFixer

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
        CheckFixer = _import_check_module("check_fixer").CheckFixer

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
