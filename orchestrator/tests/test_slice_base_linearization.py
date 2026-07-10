"""Tests for root-slice linearization + the base-ancestry admission gate (#3541).

Pipeline ``issue-3523`` orphaned slice-1's reviewed, consensus-approved
code: slice-2 was declared a parallel root, so its integration branch
forked from the work branch — which only ever advances with bookkeeping
commits during a run — and every downstream slice excluded slice-1's
code while the contract kept marking it complete.

Covers:

* ``routes.pipelines._latest_completed_chain_tip`` — tip selection
  (deepest completed chain, declaration-order tiebreak), the
  ``parent_branch_at_creation`` chain walk, and the liveness probe
  posture (absent tip skipped, raising probe treated as "exists").
* ``routes.pipelines._resolve_slice_base_branch`` — the new root arm:
  linearize onto the completed tip, fall back to ``pipeline_branch``
  when nothing has completed, and keep orphan-reconciler mode
  (``extant_branches``) on the old root behaviour.
* ``contract_completeness.base_ancestry_gate_enabled`` — the
  independent kill switch.
* ``routes.pipelines._check_slice_base_ancestry`` — gate wiring:
  kill switch, no-predecessor / probe-failure degradation, the
  serialized (gate everything COMPLETE) vs concurrent (gate the fork
  chain only) scoping, and the failure string on a definitive miss.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# sys.path setup matches other orchestrator/tests files.
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing routes.pipelines.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

import contract_completeness as cc  # noqa: E402
from egg_contracts.models import (  # noqa: E402
    Contract,
    IssueInfo,
    Slice,
    SliceStatus,
    Task,
    TaskStatus,
)
from egg_contracts.models import (  # noqa: E402
    PipelinePhase as ContractPhase,
)
from routes.pipelines import (  # noqa: E402
    _check_slice_base_ancestry,
    _latest_completed_chain_tip,
    _resolve_slice_base_branch,
)

PIPELINE_ID = "issue-3541"
ISSUE_BRANCH = "egg/issue-3541"
PIPELINE_BRANCH = "egg/issue-3541/work"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _make_slice(
    slice_id: str,
    *,
    deps: list[str] | None = None,
    parent_branch_at_creation: str | None = None,
    status: SliceStatus = SliceStatus.PENDING,
    task_idx: int = 1,
    commit: str | None = None,
) -> Slice:
    """Build a contract Slice with one role-bound task.

    ``commit`` (when given) lands on the task so
    ``contract_completeness.evidence_commits`` picks the row up as
    gated evidence for the slice.
    """
    return Slice(
        id=slice_id,
        name=f"Slice {slice_id}",
        status=status,
        dependencies=deps or [],
        parent_branch_at_creation=parent_branch_at_creation,
        tasks=[
            Task(
                id=f"task-{task_idx}",
                description="t",
                role="coder",
                status=TaskStatus.COMPLETE if commit else TaskStatus.PENDING,
                commit=commit,
                files_affected=[],
            )
        ],
    )


def _make_contract(slices: list[Slice]) -> Contract:
    return Contract(
        schemaVersion="1.2",
        issue=IssueInfo(number=3541, title="#3541", url=""),
        pipeline_id=PIPELINE_ID,
        current_phase=ContractPhase.IMPLEMENT,
        slices=slices,
    )


# ---------------------------------------------------------------------------
# _latest_completed_chain_tip
# ---------------------------------------------------------------------------


class TestLatestCompletedChainTip:
    def test_no_completed_slices_returns_none(self) -> None:
        slices = [
            _make_slice("slice-1"),
            _make_slice("slice-2", task_idx=2),
        ]
        assert (
            _latest_completed_chain_tip(
                slices,
                slice_id="slice-2",
                issue_branch=ISSUE_BRANCH,
                pipeline_id=PIPELINE_ID,
            )
            is None
        )

    def test_single_completed_root_is_the_tip(self) -> None:
        slices = [
            _make_slice("slice-1", status=SliceStatus.COMPLETE),
            _make_slice("slice-2", task_idx=2),
        ]
        probe = MagicMock(return_value=True)
        result = _latest_completed_chain_tip(
            slices,
            slice_id="slice-2",
            issue_branch=ISSUE_BRANCH,
            pipeline_id=PIPELINE_ID,
            branch_exists=probe,
        )
        assert result == f"{ISSUE_BRANCH}/slice-1"
        probe.assert_called_once_with(f"{ISSUE_BRANCH}/slice-1")

    def test_deepest_completed_chain_wins(self) -> None:
        # Chain slice-1 → slice-2 fully complete; slice-3 a completed
        # lone root. The chain's tip (slice-2, depth 2) outranks the
        # lone root (depth 1).
        slices = [
            _make_slice("slice-1", status=SliceStatus.COMPLETE),
            _make_slice(
                "slice-2",
                deps=["slice-1"],
                status=SliceStatus.COMPLETE,
                task_idx=2,
            ),
            _make_slice("slice-3", status=SliceStatus.COMPLETE, task_idx=3),
            _make_slice("slice-4", task_idx=4),
        ]
        result = _latest_completed_chain_tip(
            slices,
            slice_id="slice-4",
            issue_branch=ISSUE_BRANCH,
            pipeline_id=PIPELINE_ID,
        )
        assert result == f"{ISSUE_BRANCH}/slice-2"

    def test_linearized_chain_walked_via_recorded_parent(self) -> None:
        # #3541 shape: slice-2 is a declared parallel root (deps=[])
        # but was linearized onto slice-1's branch at creation. The
        # recorded parent makes slice-1 "referenced", so slice-2 is
        # the single tip with chain depth 2.
        slices = [
            _make_slice("slice-1", status=SliceStatus.COMPLETE),
            _make_slice(
                "slice-2",
                parent_branch_at_creation=f"{ISSUE_BRANCH}/slice-1",
                status=SliceStatus.COMPLETE,
                task_idx=2,
            ),
            _make_slice("slice-3", task_idx=3),
        ]
        result = _latest_completed_chain_tip(
            slices,
            slice_id="slice-3",
            issue_branch=ISSUE_BRANCH,
            pipeline_id=PIPELINE_ID,
        )
        assert result == f"{ISSUE_BRANCH}/slice-2"

    def test_absent_tip_branch_falls_to_next_tip(self) -> None:
        # The deepest tip's branch was merged + cascade-deleted; the
        # next-ranked completed tip is used instead.
        slices = [
            _make_slice("slice-1", status=SliceStatus.COMPLETE),
            _make_slice(
                "slice-2",
                deps=["slice-1"],
                status=SliceStatus.COMPLETE,
                task_idx=2,
            ),
            _make_slice("slice-3", status=SliceStatus.COMPLETE, task_idx=3),
            _make_slice("slice-4", task_idx=4),
        ]
        probe = MagicMock(side_effect=lambda b: b != f"{ISSUE_BRANCH}/slice-2")
        result = _latest_completed_chain_tip(
            slices,
            slice_id="slice-4",
            issue_branch=ISSUE_BRANCH,
            pipeline_id=PIPELINE_ID,
            branch_exists=probe,
        )
        assert result == f"{ISSUE_BRANCH}/slice-3"

    def test_all_tips_absent_returns_none(self) -> None:
        slices = [
            _make_slice("slice-1", status=SliceStatus.COMPLETE),
            _make_slice("slice-2", task_idx=2),
        ]
        probe = MagicMock(return_value=False)
        assert (
            _latest_completed_chain_tip(
                slices,
                slice_id="slice-2",
                issue_branch=ISSUE_BRANCH,
                pipeline_id=PIPELINE_ID,
                branch_exists=probe,
            )
            is None
        )

    def test_raising_probe_is_treated_as_exists(self) -> None:
        # Conservative posture mirrors the resolver's #2928 gate: a
        # flaky gateway must not silently re-route the base; branch
        # creation fails loud later if the tip is genuinely gone.
        slices = [
            _make_slice("slice-1", status=SliceStatus.COMPLETE),
            _make_slice("slice-2", task_idx=2),
        ]
        probe = MagicMock(side_effect=OSError("gateway down"))
        result = _latest_completed_chain_tip(
            slices,
            slice_id="slice-2",
            issue_branch=ISSUE_BRANCH,
            pipeline_id=PIPELINE_ID,
            branch_exists=probe,
        )
        assert result == f"{ISSUE_BRANCH}/slice-1"

    def test_declaration_order_breaks_depth_ties(self) -> None:
        slices = [
            _make_slice("slice-1", status=SliceStatus.COMPLETE),
            _make_slice("slice-2", status=SliceStatus.COMPLETE, task_idx=2),
            _make_slice("slice-3", task_idx=3),
        ]
        result = _latest_completed_chain_tip(
            slices,
            slice_id="slice-3",
            issue_branch=ISSUE_BRANCH,
            pipeline_id=PIPELINE_ID,
        )
        assert result == f"{ISSUE_BRANCH}/slice-2"


# ---------------------------------------------------------------------------
# _resolve_slice_base_branch — the new root arm
# ---------------------------------------------------------------------------


class TestResolveRootSliceLinearization:
    def test_root_linearizes_onto_completed_sibling_chain(self) -> None:
        """#3541 headline regression: a root slice admitted after a
        sibling root completed must fork from the completed sibling's
        integration branch, not from the work branch (which only
        carries bookkeeping commits during the run).
        """
        contract = _make_contract(
            [
                _make_slice("slice-1", status=SliceStatus.COMPLETE),
                _make_slice("slice-2", task_idx=2),
            ]
        )
        probe = MagicMock(return_value=True)
        result = _resolve_slice_base_branch(
            contract,
            "slice-2",
            pipeline_id=PIPELINE_ID,
            pipeline_branch=PIPELINE_BRANCH,
            parent_branch_exists=probe,
        )
        assert result == f"{ISSUE_BRANCH}/slice-1"

    def test_first_root_still_bases_on_pipeline_branch(self) -> None:
        contract = _make_contract(
            [
                _make_slice("slice-1"),
                _make_slice("slice-2", task_idx=2),
            ]
        )
        result = _resolve_slice_base_branch(
            contract,
            "slice-1",
            pipeline_id=PIPELINE_ID,
            pipeline_branch=PIPELINE_BRANCH,
        )
        assert result == PIPELINE_BRANCH

    def test_root_with_all_tips_deleted_falls_back_to_pipeline_branch(self) -> None:
        # Completed tip merged into work + cascade-deleted → its code
        # already lives on the work branch, which is the right base.
        contract = _make_contract(
            [
                _make_slice("slice-1", status=SliceStatus.COMPLETE),
                _make_slice("slice-2", task_idx=2),
            ]
        )
        probe = MagicMock(return_value=False)
        result = _resolve_slice_base_branch(
            contract,
            "slice-2",
            pipeline_id=PIPELINE_ID,
            pipeline_branch=PIPELINE_BRANCH,
            parent_branch_exists=probe,
        )
        assert result == PIPELINE_BRANCH

    def test_orphan_reconciler_mode_keeps_root_on_pipeline_branch(self) -> None:
        # The reconciler retargets orphaned PRs; re-linearizing a root
        # onto an unrelated live chain would rewrite the PR's diff.
        contract = _make_contract(
            [
                _make_slice("slice-1", status=SliceStatus.COMPLETE),
                _make_slice("slice-2", task_idx=2),
            ]
        )
        result = _resolve_slice_base_branch(
            contract,
            "slice-2",
            pipeline_id=PIPELINE_ID,
            pipeline_branch=PIPELINE_BRANCH,
            extant_branches={f"{ISSUE_BRANCH}/slice-1", PIPELINE_BRANCH},
        )
        assert result == PIPELINE_BRANCH

    def test_recorded_parent_still_short_circuits(self) -> None:
        contract = _make_contract(
            [
                _make_slice("slice-1", status=SliceStatus.COMPLETE),
                _make_slice(
                    "slice-2",
                    parent_branch_at_creation=f"{ISSUE_BRANCH}/recorded",
                    task_idx=2,
                ),
            ]
        )
        probe = MagicMock(return_value=True)
        result = _resolve_slice_base_branch(
            contract,
            "slice-2",
            pipeline_id=PIPELINE_ID,
            pipeline_branch=PIPELINE_BRANCH,
            parent_branch_exists=probe,
        )
        assert result == f"{ISSUE_BRANCH}/recorded"
        probe.assert_not_called()

    def test_non_root_still_derives_from_dependency(self) -> None:
        # A dependent slice must keep stacking on its declared parent
        # even when an unrelated sibling chain completed meanwhile.
        contract = _make_contract(
            [
                _make_slice("slice-1", status=SliceStatus.COMPLETE),
                _make_slice("slice-2", status=SliceStatus.IN_PROGRESS, task_idx=2),
                _make_slice("slice-3", deps=["slice-2"], task_idx=3),
            ]
        )
        probe = MagicMock(return_value=True)
        result = _resolve_slice_base_branch(
            contract,
            "slice-3",
            pipeline_id=PIPELINE_ID,
            pipeline_branch=PIPELINE_BRANCH,
            parent_branch_exists=probe,
        )
        assert result == f"{ISSUE_BRANCH}/slice-2"
        probe.assert_called_once_with(f"{ISSUE_BRANCH}/slice-2")


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


class TestBaseAncestryGateEnabled:
    def test_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(cc.BASE_ANCESTRY_GATE_ENV_VAR, raising=False)
        assert cc.base_ancestry_gate_enabled() is True

    @pytest.mark.parametrize("value", ["off", "0", "false", "no", " OFF "])
    def test_kill_switch(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(cc.BASE_ANCESTRY_GATE_ENV_VAR, value)
        assert cc.base_ancestry_gate_enabled() is False

    def test_independent_of_other_gate_switches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(cc.GATE_ENV_VAR, "off")
        monkeypatch.setenv(cc.EVIDENCE_GATE_ENV_VAR, "off")
        monkeypatch.delenv(cc.BASE_ANCESTRY_GATE_ENV_VAR, raising=False)
        assert cc.base_ancestry_gate_enabled() is True


# ---------------------------------------------------------------------------
# _check_slice_base_ancestry
# ---------------------------------------------------------------------------


NEW_SLICE_BRANCH = f"{ISSUE_BRANCH}/slice-9"


def _spawner(unreachable: list[str] | None) -> MagicMock:
    spawner = MagicMock()
    spawner.gateway.find_unreachable_evidence_commits.return_value = unreachable
    return spawner


@pytest.fixture
def gate_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(cc.BASE_ANCESTRY_GATE_ENV_VAR, raising=False)
    return monkeypatch


class TestCheckSliceBaseAncestry:
    def _serial_contract(self) -> Contract:
        return _make_contract(
            [
                _make_slice("slice-1", status=SliceStatus.COMPLETE, commit=SHA_A),
                _make_slice("slice-9", task_idx=2),
            ]
        )

    def test_kill_switch_skips_without_gateway_call(
        self, tmp_path: Path, gate_env: pytest.MonkeyPatch
    ) -> None:
        gate_env.setenv(cc.BASE_ANCESTRY_GATE_ENV_VAR, "off")
        spawner = _spawner([SHA_A])
        result = _check_slice_base_ancestry(
            PIPELINE_ID,
            spawner,
            tmp_path,
            "slice-9",
            NEW_SLICE_BRANCH,
            issue_branch=ISSUE_BRANCH,
            contract=self._serial_contract(),
        )
        assert result is None
        spawner.gateway.find_unreachable_evidence_commits.assert_not_called()

    def test_missing_contract_skips(self, tmp_path: Path, gate_env) -> None:
        spawner = _spawner([SHA_A])
        result = _check_slice_base_ancestry(
            PIPELINE_ID,
            spawner,
            tmp_path,
            "slice-9",
            NEW_SLICE_BRANCH,
            issue_branch=ISSUE_BRANCH,
        )
        assert result is None
        spawner.gateway.find_unreachable_evidence_commits.assert_not_called()

    def test_no_completed_predecessors_skips_probe(self, tmp_path: Path, gate_env) -> None:
        contract = _make_contract(
            [
                _make_slice("slice-1", status=SliceStatus.IN_PROGRESS),
                _make_slice("slice-9", task_idx=2),
            ]
        )
        spawner = _spawner([SHA_A])
        result = _check_slice_base_ancestry(
            PIPELINE_ID,
            spawner,
            tmp_path,
            "slice-9",
            NEW_SLICE_BRANCH,
            issue_branch=ISSUE_BRANCH,
            contract=contract,
        )
        assert result is None
        spawner.gateway.find_unreachable_evidence_commits.assert_not_called()

    def test_all_reachable_passes(self, tmp_path: Path, gate_env) -> None:
        spawner = _spawner([])
        result = _check_slice_base_ancestry(
            PIPELINE_ID,
            spawner,
            tmp_path,
            "slice-9",
            NEW_SLICE_BRANCH,
            issue_branch=ISSUE_BRANCH,
            contract=self._serial_contract(),
        )
        assert result is None
        call_kwargs = spawner.gateway.find_unreachable_evidence_commits.call_args.kwargs
        assert call_kwargs["commit_shas"] == [SHA_A]
        assert call_kwargs["integration_branch"] == NEW_SLICE_BRANCH

    def test_probe_failure_skips(self, tmp_path: Path, gate_env) -> None:
        result = _check_slice_base_ancestry(
            PIPELINE_ID,
            _spawner(None),
            tmp_path,
            "slice-9",
            NEW_SLICE_BRANCH,
            issue_branch=ISSUE_BRANCH,
            contract=self._serial_contract(),
        )
        assert result is None

    def test_orphaned_completed_work_fails_admission(self, tmp_path: Path, gate_env) -> None:
        """#3541 headline regression: a base that excludes a completed
        predecessor's recorded commit must fail admission loudly."""
        result = _check_slice_base_ancestry(
            PIPELINE_ID,
            _spawner([SHA_A]),
            tmp_path,
            "slice-9",
            NEW_SLICE_BRANCH,
            issue_branch=ISSUE_BRANCH,
            contract=self._serial_contract(),
        )
        assert result is not None
        assert "slice-9" in result
        assert SHA_A in result
        assert NEW_SLICE_BRANCH in result
        assert cc.BASE_ANCESTRY_GATE_ENV_VAR in result

    def test_concurrent_mode_ignores_off_chain_siblings(self, tmp_path: Path, gate_env) -> None:
        # cap > 1: a completed sibling chain the new slice does not
        # fork from is legitimately not an ancestor — no gate, no
        # probe.
        contract = _make_contract(
            [
                _make_slice("slice-1", status=SliceStatus.COMPLETE, commit=SHA_A),
                _make_slice("slice-9", task_idx=2),
            ]
        )
        spawner = _spawner([SHA_A])
        result = _check_slice_base_ancestry(
            PIPELINE_ID,
            spawner,
            tmp_path,
            "slice-9",
            NEW_SLICE_BRANCH,
            issue_branch=ISSUE_BRANCH,
            max_parallel_slices=2,
            contract=contract,
        )
        assert result is None
        spawner.gateway.find_unreachable_evidence_commits.assert_not_called()

    def test_concurrent_mode_gates_fork_chain(self, tmp_path: Path, gate_env) -> None:
        # cap > 1: completed ancestors on the slice's own fork chain
        # (declared dependency AND recorded linearized parent) are
        # still gated.
        contract = _make_contract(
            [
                _make_slice("slice-1", status=SliceStatus.COMPLETE, commit=SHA_A),
                _make_slice(
                    "slice-2",
                    status=SliceStatus.COMPLETE,
                    parent_branch_at_creation=f"{ISSUE_BRANCH}/slice-1",
                    commit=SHA_B,
                    task_idx=2,
                ),
                _make_slice(
                    "slice-9",
                    deps=["slice-2"],
                    parent_branch_at_creation=f"{ISSUE_BRANCH}/slice-2",
                    task_idx=3,
                ),
            ]
        )
        spawner = _spawner([SHA_A])
        result = _check_slice_base_ancestry(
            PIPELINE_ID,
            spawner,
            tmp_path,
            "slice-9",
            NEW_SLICE_BRANCH,
            issue_branch=ISSUE_BRANCH,
            max_parallel_slices=2,
            contract=contract,
        )
        assert result is not None
        assert SHA_A in result
        call_kwargs = spawner.gateway.find_unreachable_evidence_commits.call_args.kwargs
        # Both chain ancestors' commits probed (slice-2 via deps,
        # slice-1 via the recorded linearized parent walk).
        assert set(call_kwargs["commit_shas"]) == {SHA_A, SHA_B}
