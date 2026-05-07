"""Tests for ``plan_parser.validate_task_role_alignment`` (#2527).

The plan-phase ``task_planner`` can assign tasks to producer roles
(``coder`` / ``tester`` / ``documenter``) whose
``shared/egg_restrictions/patterns.py`` blocklist forbids the listed
files. The mismatch is otherwise only caught at push time by the
gateway, after a producer has been spawned and burned tokens. The
validator runs the same blocked-pattern check at plan time so the plan
reviewer can NACK before any producer cycle starts.

These tests cover:

* Clean assignments (each producer role pushing files within its scope)
  return no errors.
* Coder assigned to test files — the dominant misassignment in the
  #2530 audit (24 of 25 cases) — is flagged with ``tester`` as the
  single eligible role.
* Coder assigned to ``**/conftest.py`` is flagged (separate fixture
  pattern from ``test_*.py``).
* Coder assigned to ``.github/`` files — no producer role can push
  these — surfaces the ``.github-staging/`` (#2508) remediation hint.
* Coder assigned to a markdown file is flagged with ``documenter`` as
  the single eligible role.
* Tasks without an explicit ``role`` or without
  ``files_affected`` are skipped (no error, no false positive).
* The actual ``files`` from issue #2527's evidence table parse to the
  expected structured errors.
"""

from __future__ import annotations

from egg_contracts.models import Slice, Task
from egg_contracts.plan_parser import validate_task_role_alignment


def _slice(slice_id: str, tasks: list[Task]) -> Slice:
    return Slice(id=slice_id, name=f"slice {slice_id}", tasks=tasks)


def _task(
    task_id: str,
    files: list[str],
    role: str | None,
    description: str = "task",
) -> Task:
    return Task(
        id=task_id,
        description=description,
        acceptance_criteria="acc",
        files_affected=files,
        role=role,
    )


class TestCleanAssignments:
    """Properly assigned tasks should produce zero errors."""

    def test_empty_input(self) -> None:
        assert validate_task_role_alignment([]) == []

    def test_coder_with_source_file(self) -> None:
        slices = [_slice("slice-1", [_task("task-1-1", ["src/foo.py"], "coder")])]
        assert validate_task_role_alignment(slices) == []

    def test_tester_with_test_file(self) -> None:
        slices = [_slice("slice-1", [_task("task-1-1", ["tests/test_foo.py"], "tester")])]
        assert validate_task_role_alignment(slices) == []

    def test_tester_with_conftest(self) -> None:
        slices = [
            _slice(
                "slice-1",
                [_task("task-1-1", ["integration_tests/conftest.py"], "tester")],
            )
        ]
        assert validate_task_role_alignment(slices) == []

    def test_documenter_with_markdown(self) -> None:
        slices = [_slice("slice-1", [_task("task-1-1", ["docs/guide.md"], "documenter")])]
        assert validate_task_role_alignment(slices) == []


class TestSkippedTasks:
    """Tasks that the validator must intentionally pass over."""

    def test_no_role_is_skipped(self) -> None:
        # Tasks without an explicit role default downstream; the
        # validator's job is catching mis-assignments, not enforcing
        # role declaration (#2527 scope).
        slices = [
            _slice(
                "slice-1",
                [_task("task-1-1", ["integration_tests/conftest.py"], None)],
            )
        ]
        assert validate_task_role_alignment(slices) == []

    def test_empty_files_is_skipped(self) -> None:
        # Prose/research tasks legitimately omit files_affected —
        # nothing to check.
        slices = [_slice("slice-1", [_task("task-1-1", [], "coder")])]
        assert validate_task_role_alignment(slices) == []

    def test_role_without_files_is_skipped(self) -> None:
        slices = [_slice("slice-1", [_task("task-1-1", [], "documenter")])]
        assert validate_task_role_alignment(slices) == []


class TestSingleEligibleRoleHint:
    """When exactly one producer role can push every file in the task,
    the validator must name that role in its hint."""

    def test_coder_with_conftest_suggests_tester(self) -> None:
        slices = [
            _slice(
                "slice-1",
                [_task("task-1-1", ["integration_tests/conftest.py"], "coder")],
            )
        ]
        errors = validate_task_role_alignment(slices)
        assert len(errors) == 1
        msg = errors[0]
        assert "task-1-1" in msg
        assert "slice-1" in msg
        assert "'coder'" in msg
        assert "integration_tests/conftest.py" in msg
        assert "Reassign to role 'tester'" in msg

    def test_coder_with_test_py_suggests_tester(self) -> None:
        # 24 of 25 misassignments in the #2530 audit were coder
        # tasks containing test_*.py files.
        slices = [_slice("slice-1", [_task("task-1-1", ["tests/test_foo.py"], "coder")])]
        errors = validate_task_role_alignment(slices)
        assert len(errors) == 1
        assert "Reassign to role 'tester'" in errors[0]

    def test_coder_with_markdown_suggests_documenter(self) -> None:
        slices = [_slice("slice-1", [_task("task-1-1", ["docs/guide.md"], "coder")])]
        errors = validate_task_role_alignment(slices)
        assert len(errors) == 1
        assert "Reassign to role 'documenter'" in errors[0]


class TestNoEligibleRoleHint:
    """Files no producer role can push — `.github/` is the canonical case
    per #2508 — must surface the `.github-staging/` remediation."""

    def test_coder_with_github_workflow_no_eligible_role(self) -> None:
        slices = [
            _slice(
                "slice-1",
                [_task("task-1-1", [".github/workflows/ci.yml"], "coder")],
            )
        ]
        errors = validate_task_role_alignment(slices)
        assert len(errors) == 1
        msg = errors[0]
        assert ".github/workflows/ci.yml" in msg
        assert "No producer role can push" in msg
        assert ".github-staging/" in msg


class TestMixedRoleFiles:
    """When files cross role boundaries no single role is eligible — the
    validator must say so without listing a specific role to switch to."""

    def test_task_mixing_test_and_doc_files_has_no_eligible_role(self) -> None:
        # tests/test_foo.py is blocked for coder + documenter (the
        # latter via the ``tests/`` directory rule).
        # docs/guide.md is blocked for coder + tester (both via
        # ``**/*.md``). No producer role can push both.
        slices = [
            _slice(
                "slice-1",
                [
                    _task(
                        "task-1-1",
                        ["tests/test_foo.py", "docs/guide.md"],
                        "coder",
                    )
                ],
            )
        ]
        errors = validate_task_role_alignment(slices)
        assert len(errors) == 1
        assert "No producer role can push" in errors[0]


class TestMultipleSlicesAndTasks:
    """The walk must cover every (slice, task) pair and emit one error
    per offender."""

    def test_evidence_from_issue_2527(self) -> None:
        # Reproduces the misassignments listed in the issue's evidence
        # table for pipeline issue-2474-v2 slice-1: TASK-1-1 (conftest),
        # TASK-1-3 (test_*.py + .github/), TASK-1-4 (conftest) all
        # assigned to coder.
        slices = [
            _slice(
                "slice-1",
                [
                    _task(
                        "task-1-1",
                        [
                            "integration_tests/conftest.py",
                            "integration_tests/local_pipeline/conftest.py",
                        ],
                        "coder",
                    ),
                    # task-1-2 is correctly assigned -> no error
                    _task("task-1-2", ["src/foo.py"], "coder"),
                    _task(
                        "task-1-3",
                        [
                            ".github/workflows/test-e2e.yml",
                            "integration_tests/test_e2e.py",
                        ],
                        "coder",
                    ),
                    _task("task-1-4", ["integration_tests/conftest.py"], "coder"),
                ],
            )
        ]
        errors = validate_task_role_alignment(slices)
        assert len(errors) == 3
        offending_ids = {
            tid for err in errors for tid in ("task-1-1", "task-1-3", "task-1-4") if tid in err
        }
        assert offending_ids == {"task-1-1", "task-1-3", "task-1-4"}

    def test_errors_walk_every_slice(self) -> None:
        slices = [
            _slice("slice-1", [_task("task-1-1", ["tests/test_a.py"], "coder")]),
            _slice("slice-2", [_task("task-2-1", ["docs/x.md"], "coder")]),
        ]
        errors = validate_task_role_alignment(slices)
        assert len(errors) == 2
        assert any("slice-1" in e and "task-1-1" in e for e in errors)
        assert any("slice-2" in e and "task-2-1" in e for e in errors)


class TestImportOrderingRegression:
    """Guard against the egg_restrictions ↔ egg_contracts import cycle.

    A clean-interpreter ``import egg_restrictions.patterns`` must succeed
    even when nothing has pre-loaded ``egg_contracts``. Hoisting
    ``AGENT_PATTERNS`` to module scope in ``plan_parser.py`` triggers an
    ``ImportError: cannot import name 'AGENT_PATTERNS' from partially
    initialized module`` because patterns.py imports
    ``egg_contracts.agent_roles``, which runs ``egg_contracts/__init__.py``,
    which loads ``plan_parser`` mid-cycle. The gateway production boot
    path runs ``python3 gateway.py`` (no pytest pre-loader), so this test
    runs the import in a subprocess to mirror that condition. See
    ``shared/egg_restrictions/matchers.py`` docstring for context.
    """

    def test_egg_restrictions_patterns_imports_cleanly(self) -> None:
        import os
        import subprocess
        import sys
        from pathlib import Path

        # Resolve the repo's ``shared/`` directory from this file's location:
        # tests/ → egg_contracts/ → shared/. The gateway runs
        # ``python3 gateway.py`` with ``PYTHONPATH=/app`` (see
        # gateway/Dockerfile:99 and gateway/entrypoint.sh:286) where
        # shared/ modules are copied to /app/ (see
        # gateway/Dockerfile:70-75). ``PYTHONPATH=shared`` mirrors that
        # import surface — no orchestrator/, no gateway/.
        shared_dir = Path(__file__).resolve().parents[2]
        env = {**os.environ, "PYTHONPATH": str(shared_dir)}

        # Fresh interpreter — no pytest pre-loader has run, so the cycle
        # surfaces if AGENT_PATTERNS is hoisted to module scope in
        # plan_parser.py.
        result = subprocess.run(
            [sys.executable, "-c", "import egg_restrictions.patterns"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0, (
            f"import egg_restrictions.patterns failed in clean interpreter:\n"
            f"stderr:\n{result.stderr}"
        )
