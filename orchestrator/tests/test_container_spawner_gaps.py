"""Gap tests for container spawner's _compute_allowed_files() helper.

Covers edge cases not in the coder's initial tests:
- files_affected attribute being None (getattr fallback)
- Empty string entries in files_affected (filtered out)
- Mixed glob and explicit file patterns
- Task with no tasks attribute
- Single task with single file
"""

from __future__ import annotations

from unittest.mock import MagicMock

from container_spawner import _compute_allowed_files


class TestComputeAllowedFilesEdgeCases:
    """Edge cases for _compute_allowed_files()."""

    def _make_phase(self, phase_id: str, tasks: list | None = None):
        """Create a mock Phase object with tasks."""
        phase = MagicMock()
        phase.id = phase_id
        phase.tasks = tasks or []
        return phase

    def _make_task(self, task_id: str, files: list[str] | None = None):
        """Create a mock Task object with files_affected."""
        task = MagicMock()
        task.id = task_id
        task.files_affected = files
        return task

    def test_none_files_affected_returns_none(self):
        """Task with files_affected=None is treated as empty."""
        tasks = [self._make_task("task-1", None)]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is None

    def test_empty_string_entries_filtered(self):
        """Empty string entries in files_affected are ignored."""
        tasks = [self._make_task("task-1", ["src/auth/login.py", "", "  "])]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is not None
        # Only the valid file should produce patterns
        assert "src/auth/login.py" in result
        assert "src/auth/*" in result
        # Empty strings should not be in the result
        assert "" not in result

    def test_mixed_globs_and_explicit_files(self):
        """Mix of glob patterns and explicit files are all handled."""
        tasks = [
            self._make_task(
                "task-1",
                [
                    "src/auth/login.py",  # explicit file -> expands to src/auth/*
                    "tests/**",  # glob pattern -> preserved
                    "Makefile",  # top-level file -> no expansion
                ],
            )
        ]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is not None
        assert "src/auth/login.py" in result
        assert "src/auth/*" in result
        assert "tests/**" in result
        assert "Makefile" in result

    def test_single_task_single_file(self):
        """Simplest case: one task with one file."""
        tasks = [self._make_task("task-1", ["gateway/gateway.py"])]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is not None
        assert "gateway/gateway.py" in result
        assert "gateway/*" in result

    def test_tasks_with_no_tasks_attribute(self):
        """Phase with no tasks attribute returns None."""
        phase = MagicMock()
        phase.id = "phase-1"
        del phase.tasks  # Remove the auto-created attribute

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is None

    def test_deeply_nested_file_expands_parent_only(self):
        """a/b/c/d/file.py expands to a/b/c/d/* (immediate parent dir only)."""
        tasks = [self._make_task("task-1", ["a/b/c/d/file.py"])]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is not None
        assert "a/b/c/d/file.py" in result
        assert "a/b/c/d/*" in result
        # Should NOT expand to a/*, a/b/*, etc.
        assert "a/*" not in result
        assert "a/b/*" not in result
        assert "a/b/c/*" not in result

    def test_file_in_root_with_subdir_has_slash(self):
        """A file like 'config/settings.py' gets 'config/*' expansion."""
        tasks = [self._make_task("task-1", ["config/settings.py"])]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is not None
        assert "config/settings.py" in result
        assert "config/*" in result

    def test_glob_pattern_not_expanded(self):
        """Glob patterns containing * are NOT expanded further."""
        tasks = [self._make_task("task-1", ["src/auth/*"])]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is not None
        assert "src/auth/*" in result
        # Should only contain the one pattern, no duplicate expansion
        assert len(result) == 1

    def test_multiple_phases_selects_correct_one(self):
        """Only tasks from the matching phase are used."""
        tasks_p1 = [self._make_task("task-1", ["src/auth/login.py"])]
        tasks_p2 = [self._make_task("task-2", ["src/payments/pay.py"])]
        phase1 = self._make_phase("phase-1", tasks_p1)
        phase2 = self._make_phase("phase-2", tasks_p2)

        result = _compute_allowed_files([phase1, phase2], "phase-2", "coder")
        assert result is not None
        assert "src/payments/pay.py" in result
        assert "src/payments/*" in result
        assert "src/auth/login.py" not in result
