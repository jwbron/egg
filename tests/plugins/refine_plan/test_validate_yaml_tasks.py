"""Tests for ``plugins/refine-plan/skills/refine-plan/bin/validate-yaml-tasks``.

These tests guard against divergence between the plugin's portable
validator and the canonical egg parser at ``shared/egg_contracts/
plan_parser.py``. The validator is shipped to end users via the Claude
Code marketplace plugin, so it must run on a wider Python range than
egg's own ``requires-python = ">=3.14"`` floor — the AST-parse test
forces compilation under whichever interpreter pytest is using and
catches Python-2 syntax regressions like the one in PR #2608's first
re-review.

The validate() function is loaded as a module via importlib so we can
exercise it directly without subprocess overhead. It lives in a file
without a .py extension so it's not picked up by the global
test_python_syntax discovery — we own its parse check here.
"""

from __future__ import annotations

import ast
import importlib.util
import textwrap
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2].parent
SCRIPT = (
    REPO_ROOT / "plugins" / "refine-plan" / "skills" / "refine-plan" / "bin" / "validate-yaml-tasks"
)


def _load_module():
    # Use SourceFileLoader directly since the script has no .py extension —
    # importlib.util.spec_from_file_location returns None on extensionless
    # files even when they parse as Python.
    loader = SourceFileLoader("validate_yaml_tasks", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec, f"could not import {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _write(tmp_path: Path, body: str) -> Path:
    plan = tmp_path / "plan.md"
    plan.write_text(textwrap.dedent(body))
    return plan


def test_script_parses_under_active_interpreter():
    """Guard against Python-2-style syntax regressions (PR #2608 re-review 1)."""
    source = SCRIPT.read_text()
    ast.parse(source, filename=str(SCRIPT))


def test_happy_path_minimal_pr_block(tmp_path: Path):
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        # Plan

        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: Only slice
            tasks:
              - id: TASK-1-1
                description: Do the thing
                acceptance: It works
        pr:
          title: Add the thing
        ```
        """,
    )
    code, errors, warnings = mod.validate(str(plan))
    assert code == 0, f"expected OK, got errors={errors}"
    assert "yaml-tasks valid" in errors[0]
    assert any("test_plan" in w for w in warnings), warnings


def test_pr_title_is_only_required_field(tmp_path: Path):
    """Mirrors the canonical egg schema (.egg/schemas/yaml-tasks.schema.json)."""
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: x
            tasks:
              - id: TASK-1-1
                description: d
                acceptance: a
        pr:
          title: t
        ```
        """,
    )
    code, errors, warnings = mod.validate(str(plan))
    assert code == 0
    assert any("test_plan" in w for w in warnings)


def test_pr_block_is_optional(tmp_path: Path):
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: x
            tasks:
              - id: TASK-1-1
                description: d
                acceptance: a
        ```
        """,
    )
    code, errors, _ = mod.validate(str(plan))
    assert code == 0, errors


def test_duplicate_task_id_within_slice(tmp_path: Path):
    """Mirrors plan_parser.py:679-690 (#1988)."""
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: x
            tasks:
              - id: TASK-1-1
                description: a
                acceptance: b
              - id: task-1-1
                description: c
                acceptance: d
        ```
        """,
    )
    code, errors, _ = mod.validate(str(plan))
    assert code == 1
    assert any("duplicate task id" in e for e in errors), errors


def test_duplicate_task_id_across_slices(tmp_path: Path):
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: a
            tasks:
              - id: TASK-1-1
                description: a
                acceptance: b
          - id: 2
            name: b
            tasks:
              - id: TASK-1-1
                description: c
                acceptance: d
        ```
        """,
    )
    code, errors, _ = mod.validate(str(plan))
    assert code == 1
    assert any("duplicate task id" in e for e in errors), errors


def test_duplicate_slice_id(tmp_path: Path):
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: a
            tasks:
              - id: TASK-1-1
                description: a
                acceptance: b
          - id: phase-1
            name: b
            tasks:
              - id: TASK-2-1
                description: c
                acceptance: d
        ```
        """,
    )
    code, errors, _ = mod.validate(str(plan))
    assert code == 1
    assert any("duplicate slice id" in e for e in errors), errors


def test_missing_yaml_fence(tmp_path: Path):
    mod = _load_module()
    plan = _write(tmp_path, "# Plan\n\nNo yaml fence here.\n")
    code, errors, _ = mod.validate(str(plan))
    assert code == 1
    assert any("yaml-tasks" in e for e in errors)


def test_bad_task_id(tmp_path: Path):
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: x
            tasks:
              - id: not-a-task-id
                description: d
                acceptance: a
        ```
        """,
    )
    code, errors, _ = mod.validate(str(plan))
    assert code == 1
    assert any("bad task id" in e for e in errors), errors


def test_bad_role(tmp_path: Path):
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: x
            tasks:
              - id: TASK-1-1
                description: d
                acceptance: a
                role: builder
        ```
        """,
    )
    code, errors, _ = mod.validate(str(plan))
    assert code == 1
    assert any("bad role" in e for e in errors), errors


def test_yaml_parse_error(tmp_path: Path):
    """The #1974 regression case: ``: type`` inside a plain scalar."""
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: x
            tasks:
              - id: TASK-1-1
                description: implement foo: int -> str
                acceptance: works
        ```
        """,
    )
    code, errors, _ = mod.validate(str(plan))
    assert code == 1
    assert any("YAML parse error" in e for e in errors), errors


def test_dep_normalises_known_targets(tmp_path: Path):
    """Mixed-case / phase-prefixed deps that resolve to defined slices pass."""
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: a
            tasks:
              - id: TASK-1-1
                description: x
                acceptance: y
          - id: 2
            name: b
            dependencies: ["Slice-1", "phase-1", 1]
            serialized_chain_order: [slice-1]
            tasks:
              - id: TASK-2-1
                description: x
                acceptance: y
        pr:
          title: t
          test_plan: tp
        ```
        """,
    )
    code, errors, _ = mod.validate(str(plan))
    assert code == 0, errors


def test_dep_rejects_unknown_slice(tmp_path: Path):
    """A dependency pointing at a non-existent slice must fail validation."""
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: a
            tasks:
              - id: TASK-1-1
                description: x
                acceptance: y
          - id: 2
            name: b
            dependencies: [slice-99]
            tasks:
              - id: TASK-2-1
                description: x
                acceptance: y
        ```
        """,
    )
    code, errors, _ = mod.validate(str(plan))
    assert code == 1
    assert any("unknown slice 'slice-99'" in e for e in errors), errors


def test_dep_rejects_unparseable_entry(tmp_path: Path):
    """A non-slice-shaped dependency string must fail validation."""
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: a
            tasks:
              - id: TASK-1-1
                description: x
                acceptance: y
          - id: 2
            name: b
            dependencies: [garbage]
            tasks:
              - id: TASK-2-1
                description: x
                acceptance: y
        ```
        """,
    )
    code, errors, _ = mod.validate(str(plan))
    assert code == 1
    assert any("'garbage' is not a valid slice reference" in e for e in errors), errors


def test_serialized_chain_order_unknown_slice(tmp_path: Path):
    """``serialized_chain_order`` entries are also validated against defined slices."""
    mod = _load_module()
    plan = _write(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: a
            serialized_chain_order: [slice-42]
            tasks:
              - id: TASK-1-1
                description: x
                acceptance: y
        ```
        """,
    )
    code, errors, _ = mod.validate(str(plan))
    assert code == 1
    assert any("serialized_chain_order" in e and "slice-42" in e for e in errors), errors
