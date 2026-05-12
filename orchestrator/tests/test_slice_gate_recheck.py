"""Defensive slice-gate recheck — #2337 regression guard.

When ``contract.slices`` is empty at implement-phase entry but the on-disk
plan draft parses to N>1 slices, ``_slice_gate_block_monolithic_demotion``
must return a non-None failure message so the implement phase fails
loudly instead of silently routing through the monolithic path.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from routes.pipelines import _slice_gate_block_monolithic_demotion


def _write_plan(
    tmp_path: Path,
    pipeline_id: str,
    plan_text: str,
    *,
    issue_number: int | None = None,
) -> None:
    """Write a plan draft at the path the helper expects.

    The path is namespaced by issue_number when supplied (matching
    _pipeline_identifier's preference), else by pipeline_id.
    """
    prefix: str | int = issue_number if issue_number is not None else pipeline_id
    drafts_dir = tmp_path / ".egg-state" / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    (drafts_dir / f"{prefix}-plan.md").write_text(plan_text)


_MULTI_SLICE_PLAN = textwrap.dedent(
    """\
    # Plan: multi-slice test

    ## Implementation

    ### Phase 1: Adopt pattern
    Body.

    ### Phase 2: Apply pattern A
    Body.

    ### Phase 3: Apply pattern B
    Body.

    ```yaml
    # yaml-tasks
    pr:
      title: "Multi-slice test"
      description: "test"
    slices:
      - id: 1
        name: Adopt pattern
        goal: scaffold
        dependencies: ""
        tasks:
          - id: TASK-1-1
            description: "scaffold"
            acceptance: "done"
            files:
              - shared/pattern.py
      - id: 2
        name: Apply pattern A
        goal: apply A
        dependencies: "slice-1"
        tasks:
          - id: TASK-2-1
            description: "apply A"
            acceptance: "done"
            files:
              - src/a.py
      - id: 3
        name: Apply pattern B
        goal: apply B
        dependencies: "slice-1"
        tasks:
          - id: TASK-3-1
            description: "apply B"
            acceptance: "done"
            files:
              - src/b.py
    ```
    """
)


_SINGLE_SLICE_PLAN = textwrap.dedent(
    """\
    # Plan: single-slice test

    ## Implementation

    ### Phase 1: Implement
    Body.

    ```yaml
    # yaml-tasks
    pr:
      title: "Single-slice"
      description: "test"
    phases:
      - id: 1
        name: Implement
        goal: do thing
        tasks:
          - id: TASK-1-1
            description: "do"
            acceptance: "done"
            files:
              - src/x.py
    ```
    """
)


class TestSliceGateBlockMonolithicDemotion:
    def test_returns_failure_when_plan_has_multiple_slices(self, tmp_path):
        from routes.pipelines import SliceGateMonolithicBlock

        pipeline_id = "issue-2261"
        _write_plan(tmp_path, pipeline_id, _MULTI_SLICE_PLAN, issue_number=2261)
        result = _slice_gate_block_monolithic_demotion(tmp_path, pipeline_id, issue_number=2261)
        assert result is not None
        assert isinstance(result, SliceGateMonolithicBlock)
        assert "monolithic" in result.message
        assert "#2337" in result.message
        # The structured count is what the dedicated HITL payload reads,
        # so a regression in the count would silently mis-attribute the
        # divergence to the operator.  _MULTI_SLICE_PLAN has three slices.
        assert result.draft_slice_count == 3

    def test_returns_none_for_single_slice_plan(self, tmp_path):
        pipeline_id = "issue-2261-single"
        _write_plan(tmp_path, pipeline_id, _SINGLE_SLICE_PLAN)
        result = _slice_gate_block_monolithic_demotion(tmp_path, pipeline_id, issue_number=None)
        assert result is None

    def test_returns_none_when_draft_missing(self, tmp_path):
        # No draft file written.
        result = _slice_gate_block_monolithic_demotion(tmp_path, "issue-no-draft", issue_number=999)
        assert result is None

    def test_returns_none_when_parser_fails(self, tmp_path):
        pipeline_id = "issue-broken-plan"
        _write_plan(tmp_path, pipeline_id, "not a valid plan, no yaml-tasks block")
        result = _slice_gate_block_monolithic_demotion(tmp_path, pipeline_id, issue_number=None)
        # Parser failure shouldn't block the legacy monolithic path —
        # only an unambiguous "plan parses to N>1 slices" should.
        assert result is None
