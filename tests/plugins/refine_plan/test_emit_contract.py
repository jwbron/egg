"""Tests for ``plugins/refine-plan/skills/refine-plan/bin/emit-contract``.

Confirm the emitter produces output that loads cleanly through egg's
canonical ``Contract`` Pydantic model — that's the wire-compatibility
guarantee the SKILL.md "Compatibility" table makes. Also exercise the
edge cases the previous review flagged: case-insensitive slice ids,
arg-parsing precision, and Python-2 syntax regressions.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import textwrap
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2].parent
SCRIPT = REPO_ROOT / "plugins" / "refine-plan" / "skills" / "refine-plan" / "bin" / "emit-contract"


def _load_module():
    # SourceFileLoader works with extensionless scripts; the default
    # spec_from_file_location does not.
    loader = SourceFileLoader("emit_contract", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec, f"could not import {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _write_plan(tmp_path: Path, body: str) -> Path:
    plan = tmp_path / "plan.md"
    plan.write_text(textwrap.dedent(body))
    return plan


def test_script_parses_under_active_interpreter():
    """Guard against Python-2-style syntax regressions (PR #2608 re-review 1)."""
    source = SCRIPT.read_text()
    ast.parse(source, filename=str(SCRIPT))


def test_emits_contract_loadable_by_egg(tmp_path: Path):
    """Round-trip through ``shared/egg_contracts/models.py::Contract``."""
    pytest.importorskip("pydantic")
    from egg_contracts.models import Contract

    mod = _load_module()
    plan = _write_plan(
        tmp_path,
        """
        # Plan

        ```yaml
        # yaml-tasks
        slices:
          - id: 1
            name: First slice
            tasks:
              - id: TASK-1-1
                description: Implement foo
                acceptance: foo works
                role: coder
                files:
                  - src/foo.py
          - id: phase-2
            name: Second slice
            dependencies: [slice-1]
            tasks:
              - id: TASK-2-1
                description: Test foo
                acceptance: tests pass
                role: tester
        pr:
          title: Add foo
          test_plan: pytest
        ```
        """,
    )
    out = tmp_path / "contract.json"
    rc = mod.main([str(SCRIPT), str(plan), str(out), "issue-1"])
    assert rc == 0
    data = json.loads(out.read_text())
    contract = Contract.model_validate(data)
    assert contract.pipeline_id == "issue-1"
    assert [s.id for s in contract.slices] == ["slice-1", "slice-2"]
    assert [t.id for s in contract.slices for t in s.tasks] == [
        "task-1-1",
        "task-2-1",
    ]
    assert contract.slices[0].tasks[0].acceptance_criteria == "foo works"
    assert contract.slices[0].tasks[0].files_affected == ["src/foo.py"]
    assert contract.slices[1].dependencies == ["slice-1"]


def test_slice_id_case_insensitive(tmp_path: Path):
    """Cap-S/cap-P slice ids must normalize to lowercase ``slice-N``."""
    mod = _load_module()
    plan = _write_plan(
        tmp_path,
        """
        ```yaml
        # yaml-tasks
        slices:
          - id: Slice-3
            name: Cap-S
            tasks:
              - id: TASK-3-1
                description: a
                acceptance: b
          - id: Phase-7
            name: Cap-P
            tasks:
              - id: TASK-7-1
                description: c
                acceptance: d
        ```
        """,
    )
    out = tmp_path / "contract.json"
    rc = mod.main([str(SCRIPT), str(plan), str(out), "issue-1"])
    assert rc == 0
    data = json.loads(out.read_text())
    assert [s["id"] for s in data["slices"]] == ["slice-3", "slice-7"]


def test_dep_normalisation(tmp_path: Path):
    mod = _load_module()
    plan = _write_plan(
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
            tasks:
              - id: TASK-2-1
                description: x
                acceptance: y
        ```
        """,
    )
    out = tmp_path / "contract.json"
    rc = mod.main([str(SCRIPT), str(plan), str(out), "issue-1"])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["slices"][1]["dependencies"] == ["slice-1", "slice-1", "slice-1"]


def test_arg_parsing_rejects_dangling_flag(tmp_path: Path, capsys):
    """``--current-phase`` without a value must error, not silently use the default."""
    mod = _load_module()
    plan = _write_plan(tmp_path, "no fence here\n")
    out = tmp_path / "contract.json"
    rc = mod.main([str(SCRIPT), str(plan), str(out), "issue-1", "--current-phase"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "usage" in err.lower()


def test_arg_parsing_rejects_unknown_flag(tmp_path: Path, capsys):
    mod = _load_module()
    plan = _write_plan(tmp_path, "no fence here\n")
    out = tmp_path / "contract.json"
    rc = mod.main([str(SCRIPT), str(plan), str(out), "issue-1", "bogus", "plan"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unexpected flag" in err.lower()


def test_arg_parsing_rejects_invalid_phase(tmp_path: Path, capsys):
    mod = _load_module()
    plan = _write_plan(tmp_path, "no fence here\n")
    out = tmp_path / "contract.json"
    rc = mod.main([str(SCRIPT), str(plan), str(out), "issue-1", "--current-phase", "bogus"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "invalid --current-phase" in err.lower()


def test_arg_parsing_accepts_valid_phase(tmp_path: Path):
    mod = _load_module()
    plan = _write_plan(
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
        ```
        """,
    )
    out = tmp_path / "contract.json"
    rc = mod.main([str(SCRIPT), str(plan), str(out), "issue-1", "--current-phase", "implement"])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["current_phase"] == "implement"


def test_missing_pr_block_yields_null(tmp_path: Path):
    mod = _load_module()
    plan = _write_plan(
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
        ```
        """,
    )
    out = tmp_path / "contract.json"
    rc = mod.main([str(SCRIPT), str(plan), str(out), "issue-1"])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["pr"] is None
