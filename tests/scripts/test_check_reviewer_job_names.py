"""Tests for scripts/check-reviewer-job-names.py."""

from __future__ import annotations

import importlib.util
import textwrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_reviewer_job_names",
    Path(__file__).resolve().parents[2] / "scripts" / "check-reviewer-job-names.py",
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

check_workflow = _mod.check_workflow
REQUIRED_PREFIX = _mod.REQUIRED_PREFIX


def _write_workflow(tmp_path: Path, content: str, name: str = "test.yml") -> Path:
    """Write a workflow YAML file and return its path."""
    p = tmp_path / name
    p.write_text(textwrap.dedent(content))
    return p


class TestReviewerJobNaming:
    def test_correct_naming_passes(self, tmp_path: Path) -> None:
        """Job with correct prefix should not be flagged."""
        f = _write_workflow(
            tmp_path,
            """\
            name: My Workflow
            on: push
            jobs:
              review:
                name: egg-review / Code
                uses: ./.github/workflows/reusable-review.yml
                with:
                  pr_number: 1
                  bot_name: code
            """,
        )
        violations = check_workflow(f)
        assert len(violations) == 0

    def test_missing_prefix_fails(self, tmp_path: Path) -> None:
        """Job without prefix should be flagged."""
        f = _write_workflow(
            tmp_path,
            """\
            name: My Workflow
            on: push
            jobs:
              review:
                name: AI Code Review
                uses: ./.github/workflows/reusable-review.yml
                with:
                  pr_number: 1
                  bot_name: code
            """,
        )
        violations = check_workflow(f)
        assert len(violations) == 1
        assert "AI Code Review" in violations[0]
        assert REQUIRED_PREFIX in violations[0]

    def test_missing_name_field_fails(self, tmp_path: Path) -> None:
        """Job with no name field should be flagged."""
        f = _write_workflow(
            tmp_path,
            """\
            name: My Workflow
            on: push
            jobs:
              review:
                uses: ./.github/workflows/reusable-review.yml
                with:
                  pr_number: 1
                  bot_name: code
            """,
        )
        violations = check_workflow(f)
        assert len(violations) == 1
        assert "no 'name:' field" in violations[0]

    def test_non_reviewer_jobs_ignored(self, tmp_path: Path) -> None:
        """Jobs that don't use reusable-review.yml should not be checked."""
        f = _write_workflow(
            tmp_path,
            """\
            name: My Workflow
            on: push
            jobs:
              lint:
                name: Lint All Files
                runs-on: ubuntu-latest
                steps:
                  - run: echo "linting"
              build:
                name: Build Project
                uses: ./.github/workflows/other-workflow.yml
            """,
        )
        violations = check_workflow(f)
        assert len(violations) == 0

    def test_multiple_reviewer_jobs(self, tmp_path: Path) -> None:
        """Multiple reviewer jobs should all be checked."""
        f = _write_workflow(
            tmp_path,
            """\
            name: My Workflow
            on: push
            jobs:
              review1:
                name: egg-review / Code
                uses: ./.github/workflows/reusable-review.yml
                with:
                  bot_name: code
              review2:
                name: Bad Review Name
                uses: ./.github/workflows/reusable-review.yml
                with:
                  bot_name: bad
              review3:
                name: egg-review / Design
                uses: ./.github/workflows/reusable-review.yml
                with:
                  bot_name: design
            """,
        )
        violations = check_workflow(f)
        assert len(violations) == 1
        assert "Bad Review Name" in violations[0]

    def test_contract_verification_style(self, tmp_path: Path) -> None:
        """Contract verification style naming should pass."""
        f = _write_workflow(
            tmp_path,
            """\
            name: "egg: Contract Verification"
            on: pull_request
            jobs:
              verify:
                name: egg-review / Contract Verification
                uses: ./.github/workflows/reusable-review.yml
                with:
                  bot_name: contract-verification
            """,
        )
        violations = check_workflow(f)
        assert len(violations) == 0

    def test_empty_workflow(self, tmp_path: Path) -> None:
        """Empty workflow should not cause errors."""
        f = _write_workflow(
            tmp_path,
            """\
            name: Empty
            on: push
            """,
        )
        violations = check_workflow(f)
        assert len(violations) == 0

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        """Invalid YAML should not crash the checker."""
        f = _write_workflow(
            tmp_path,
            """\
            name: Broken
            on: push
            jobs:
              bad: [[[invalid
            """,
        )
        violations = check_workflow(f)
        assert len(violations) == 0  # Returns empty on parse error


class TestMain:
    """Tests for the main() entry point."""

    def test_main_with_valid_workflows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() should find and check all workflow files."""
        # Create mock directory structure
        workflows_dir = tmp_path / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)

        # Valid workflow
        (workflows_dir / "valid-reviewer.yml").write_text(
            textwrap.dedent("""\
            name: Valid Reviewer
            on: push
            jobs:
              review:
                name: egg-review / Code
                uses: ./.github/workflows/reusable-review.yml
            """)
        )

        # Non-reviewer workflow (should be ignored)
        (workflows_dir / "lint.yml").write_text(
            textwrap.dedent("""\
            name: Lint
            on: push
            jobs:
              lint:
                runs-on: ubuntu-latest
                steps:
                  - run: echo lint
            """)
        )

        # Reusable workflow itself (should be skipped)
        (workflows_dir / "reusable-review.yml").write_text(
            textwrap.dedent("""\
            name: Reusable Review
            on: workflow_call
            jobs:
              review:
                runs-on: ubuntu-latest
                steps:
                  - run: echo review
            """)
        )

        # Patch the script's path resolution to use our temp directory
        monkeypatch.setattr(_mod, "main", lambda: _run_main_with_repo_root(tmp_path))

        result = _mod.main()
        assert result == 0

    def test_main_with_violations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() should return 1 when violations are found."""
        workflows_dir = tmp_path / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)

        # Invalid workflow - missing prefix
        (workflows_dir / "bad-reviewer.yml").write_text(
            textwrap.dedent("""\
            name: Bad Reviewer
            on: push
            jobs:
              review:
                name: Wrong Name
                uses: ./.github/workflows/reusable-review.yml
            """)
        )

        monkeypatch.setattr(_mod, "main", lambda: _run_main_with_repo_root(tmp_path))

        result = _mod.main()
        assert result == 1

    def test_main_with_no_workflows_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() should return 0 with warning when workflows dir doesn't exist."""
        # Don't create the workflows directory
        monkeypatch.setattr(_mod, "main", lambda: _run_main_with_repo_root(tmp_path))

        result = _mod.main()
        assert result == 0


def _run_main_with_repo_root(repo_root: Path) -> int:
    """Run main() logic with a custom repo root."""
    workflows_dir = repo_root / ".github" / "workflows"

    if not workflows_dir.exists():
        print("Warning: .github/workflows/ directory not found")
        return 0

    workflow_files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))

    if not workflow_files:
        print("Warning: No workflow files found")
        return 0

    all_violations: list[str] = []
    for wf in workflow_files:
        if wf.name == "reusable-review.yml":
            continue
        all_violations.extend(check_workflow(wf))

    if all_violations:
        print("ERROR: Found reviewer jobs without required naming prefix!\n")
        for v in all_violations:
            print(v)
        return 1
    else:
        print("OK: All reviewer jobs use the required naming prefix")
        return 0
