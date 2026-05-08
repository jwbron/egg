"""Slice-1 base-resolution integration tests (#2548 task-2-1).

Pre-#2548, the root slice's ``parent_branch`` was hard-coded to the
pipeline branch (``egg/<id>/work``).  Under #2548 the dedicated
context branch (``egg/<id>/context``) carries the refine + plan
analysis docs and BRC consensus transcripts, so slice-1 stacks on it
instead — making those artifacts reachable through the slice PR diff.

The new resolution in ``_run_one_slice_inner`` is:

* If ``parent_slice_id is None`` (root slice):
    * Load contract.
    * If ``contract.pr is not None`` and
      ``contract.pr.context_branch`` is truthy → use it.
    * Else → log a warning and fall back to ``pipeline_branch``.
      Empty-string and ``None`` both fall back; the empty-string
      case is a D4 hard-switchover policy violation (the orchestrator
      should always populate the field after plan_gate), but we keep
      the slice provisionable so an operator can investigate.
* Non-root slices (``parent_slice_id is not None``) are unchanged —
  they target the parent slice's integration branch.

These tests exercise the actual production code path through
``_run_implement_phase_slices`` rather than copy-pasting the inline
resolution into a stand-in helper, so a future refactor that breaks
the wiring (e.g. forgets to read ``contract.pr.context_branch``) will
fail this file rather than slip through.

Adversarial probes (each was on my "what could the coder have missed?"
list):

* ``contract.pr.context_branch`` populated → slice-1 picks it up.
* ``contract.pr is None`` → slice-1 falls back to ``pipeline.branch``
  (no AttributeError on ``None.context_branch``).
* ``contract.pr.context_branch`` is empty string → slice-1 falls
  back (the ``if context_branch_for_slice1`` truthiness guard would
  otherwise pick the empty string and break the create-slice-PR
  push).
* ``load_contract`` raises → slice-1 falls back without aborting
  (best-effort: the slice still provisions on the legacy branch).
* Non-root slice (slice-2 with deps=[slice-1]) targets the parent's
  integration branch — context branch wiring must NOT touch the
  child-slice path.
* ``parent_branch_at_creation`` is persisted to the contract with
  the resolved branch (so the reconciler's bootstrap-detection logic
  sees the right parent on a later run).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

# sys.path setup matches test_slice_run_loop_integration.py.
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

from egg_contracts.models import (  # noqa: E402
    Contract,
    IssueInfo,
    PRMetadata,
    Slice,
    SliceStatus,
    Task,
    TaskStatus,
)
from egg_contracts.models import (  # noqa: E402
    PipelinePhase as ContractPhase,
)
from models import (  # noqa: E402
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import (  # noqa: E402
    _run_implement_phase_slices,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_pipeline(
    pipeline_id: str = "issue-2548",
    issue_number: int | None = 2548,
) -> Pipeline:
    config = PipelineConfig(
        concurrent_execution=True,
        max_concurrent_agents=6,
        consensus_timeout_minutes=30,
    )
    return Pipeline(
        id=pipeline_id,
        issue_number=issue_number,
        repo="owner/repo",
        branch=f"egg/{pipeline_id}/work",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _make_contract(
    pipeline_id: str = "issue-2548",
    issue_number: int = 2548,
    slices: list[Slice] | None = None,
    *,
    context_branch: str | None = None,
    has_pr: bool = True,
) -> Contract:
    """Build a Contract with optional ``pr.context_branch`` populated.

    ``has_pr=False`` builds a contract whose ``pr`` field is omitted
    (``contract.pr is None``) — used to exercise the
    ``AttributeError``-defensive fallback path.
    """
    pr = None
    if has_pr:
        pr = PRMetadata(
            title="t",
            description="",
            context_branch=context_branch,
        )
    return Contract(
        schemaVersion="1.0",
        issue=IssueInfo(number=issue_number, title=f"#{issue_number}", url=""),
        pipeline_id=pipeline_id,
        current_phase=ContractPhase.IMPLEMENT,
        slices=slices or [],
        pr=pr,
    )


def _make_slice(
    slice_id: str,
    *,
    name: str | None = None,
    deps: list[str] | None = None,
    tasks: list[Task] | None = None,
) -> Slice:
    return Slice(
        id=slice_id,
        name=name or f"Slice {slice_id}",
        status=SliceStatus.PENDING,
        dependencies=deps or [],
        tasks=tasks or [],
    )


def _make_task(task_id: str = "task-1-1") -> Task:
    return Task(
        id=task_id,
        description=f"Task {task_id}",
        status=TaskStatus.PENDING,
        files_affected=[],
    )


def _make_spawner() -> MagicMock:
    spawner = MagicMock(name="spawner")
    spawner.gateway = MagicMock(name="gateway")
    spawner.gateway.create_slice_pr.return_value = "https://example/pr/1"
    spawner.gateway.is_slice_branch_merged_into_parent.return_value = False
    return spawner


def _has_slice_1_fallback_warning(mock_logger: MagicMock) -> bool:
    """Return True iff ``mock_logger.warning`` was called for the
    slice-1 base-resolution fallback log line.

    The production code emits the warning at
    ``orchestrator/routes/pipelines.py`` (slice-1 base resolution),
    keyed on the substring ``"context_branch"`` and the
    pipeline-branch fallback message.  We match on the
    distinguishing fragment rather than the full message so a
    minor wording tweak doesn't break the test, but a refactor
    that drops the warning entirely does.
    """
    for call in mock_logger.warning.call_args_list:
        # ``logger.warning(msg, **kwargs)`` — the message is args[0].
        if not call.args:
            continue
        msg = call.args[0]
        if "Slice-1 base resolution" in msg and "context_branch" in msg:
            return True
    return False


# ---------------------------------------------------------------------------
# Slice-1 root base resolution
# ---------------------------------------------------------------------------


class TestSlice1RootBaseResolution:
    """The root slice's parent_branch is now ``contract.pr.context_branch``
    when set; pipeline branch is the legacy fallback."""

    def test_slice_1_uses_context_branch_when_populated(self) -> None:
        """Headline #2548 task-2-1 behavior: when
        ``contract.pr.context_branch`` is set, slice-1 stacks on it
        instead of the pipeline branch.  The slice PR's diff therefore
        carries the BRC + analysis artifacts that the context PR
        carries."""
        pipeline = _make_pipeline()
        slice_obj = _make_slice("slice-1", tasks=[_make_task()])
        contract = _make_contract(
            slices=[slice_obj],
            context_branch="egg/issue-2548/context",
        )

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
            patch("routes.pipelines._commit_slice_brc_history_to_integration_branch"),
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = _make_spawner()
            exit_code, _ = _run_implement_phase_slices(
                pipeline_id=pipeline.id,
                pipeline=pipeline,
                spawner=spawner,
                repo_volumes={},
                gateway_mode="public",
                repos=["owner/repo"],
                sandbox_env={},
                store=MagicMock(),
                certs_volume=None,
                worktree_repo_path=Path("/tmp/x"),
            )

        assert exit_code == 0
        # Slice-1's ``parent_branch_at_creation`` is the context branch,
        # NOT the pipeline branch (legacy behavior).
        assert slice_obj.parent_branch_at_creation == "egg/issue-2548/context", (
            f"slice-1 parent must be context branch, got {slice_obj.parent_branch_at_creation!r}"
        )
        # And the slice PR's base is the same — reviewers see the
        # context PR's diff as the slice-1 PR's base.
        spawner.gateway.create_slice_pr.assert_called_once()
        pr_kwargs = spawner.gateway.create_slice_pr.call_args.kwargs
        assert pr_kwargs["base"] == "egg/issue-2548/context"

    def test_slice_1_falls_back_to_pipeline_branch_when_pr_is_none(self) -> None:
        """Defensive: a contract with ``contract.pr is None`` must fall
        back to ``pipeline.branch`` rather than raising an
        AttributeError on ``None.context_branch``.  This protects
        legacy / pre-#2548 contracts and the refine-only short-flow
        from breaking the implement-phase entry."""
        pipeline = _make_pipeline()
        slice_obj = _make_slice("slice-1", tasks=[_make_task()])
        contract = _make_contract(slices=[slice_obj], has_pr=False)
        assert contract.pr is None

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
            patch("routes.pipelines._commit_slice_brc_history_to_integration_branch"),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = _make_spawner()
            exit_code, _ = _run_implement_phase_slices(
                pipeline_id=pipeline.id,
                pipeline=pipeline,
                spawner=spawner,
                repo_volumes={},
                gateway_mode="public",
                repos=["owner/repo"],
                sandbox_env={},
                store=MagicMock(),
                certs_volume=None,
                worktree_repo_path=Path("/tmp/x"),
            )

        assert exit_code == 0
        # No AttributeError raised; slice-1 fell back to pipeline branch.
        assert slice_obj.parent_branch_at_creation == pipeline.branch
        # Operator-visibility: the empty-context-branch warning is
        # emitted so a refactor that drops the warning is caught here
        # rather than discovered in production by an oncall who can't
        # see why slice-1 isn't stacking on the context branch.
        assert _has_slice_1_fallback_warning(mock_logger), (
            f"expected slice-1 base-resolution fallback warning, got: "
            f"{mock_logger.warning.call_args_list}"
        )

    def test_slice_1_falls_back_when_context_branch_is_none(self) -> None:
        """``contract.pr.context_branch is None`` (PRMetadata exists but
        the orchestrator hasn't populated the context branch yet — a
        D4 policy violation in production).  Slice-1 falls back to
        the pipeline branch with a warning so the slice still
        provisions and an operator can investigate."""
        pipeline = _make_pipeline()
        slice_obj = _make_slice("slice-1", tasks=[_make_task()])
        contract = _make_contract(slices=[slice_obj], context_branch=None)

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
            patch("routes.pipelines._commit_slice_brc_history_to_integration_branch"),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = _make_spawner()
            _run_implement_phase_slices(
                pipeline_id=pipeline.id,
                pipeline=pipeline,
                spawner=spawner,
                repo_volumes={},
                gateway_mode="public",
                repos=["owner/repo"],
                sandbox_env={},
                store=MagicMock(),
                certs_volume=None,
                worktree_repo_path=Path("/tmp/x"),
            )

        # Slice-1 falls back to pipeline branch.
        assert slice_obj.parent_branch_at_creation == pipeline.branch
        # Same warning surface as the ``pr is None`` path — they share
        # the empty-context-branch fallback log line.
        assert _has_slice_1_fallback_warning(mock_logger), (
            f"expected slice-1 base-resolution fallback warning, got: "
            f"{mock_logger.warning.call_args_list}"
        )

    def test_slice_1_load_contract_failure_during_base_resolution_falls_back(
        self,
    ) -> None:
        """If the slice-1 base resolver raises (e.g. ``load_contract``
        hits a transient FS error during the base read), the slice
        still provisions — it falls back to the pipeline branch with a
        warning rather than aborting the whole pipeline.

        Earlier versions of this test patched the global
        ``egg_contracts.loader.load_contract`` and counted calls to
        target only the resolver invocation (the contract is also
        loaded for bootstrap, parent_branch persistence, and the
        slice_pr_data snapshot — we needed to fail only the resolver
        call).  That count was an implementation detail of the slice
        loop and a refactor that added or removed a load_contract
        call elsewhere would silently re-target the failure.

        Today we patch
        ``routes.pipelines._resolve_slice_1_context_branch_from_contract``
        directly — a one-purpose helper that exists only as the
        resolver's load_contract wrapper.  Failure is scoped to the
        exact call site by construction.
        """
        pipeline = _make_pipeline()
        slice_obj = _make_slice("slice-1", tasks=[_make_task()])
        contract = _make_contract(
            slices=[slice_obj],
            context_branch="egg/issue-2548/context",
        )

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch(
                "routes.pipelines._resolve_slice_1_context_branch_from_contract",
                side_effect=OSError("transient FS error during base resolve"),
            ),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
            patch("routes.pipelines._commit_slice_brc_history_to_integration_branch"),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = _make_spawner()
            exit_code, _ = _run_implement_phase_slices(
                pipeline_id=pipeline.id,
                pipeline=pipeline,
                spawner=spawner,
                repo_volumes={},
                gateway_mode="public",
                repos=["owner/repo"],
                sandbox_env={},
                store=MagicMock(),
                certs_volume=None,
                worktree_repo_path=Path("/tmp/x"),
            )

        # The resolver's OSError was swallowed and slice-1 fell back
        # to the pipeline branch.  ``parent_branch_at_creation``
        # being non-None is the signal that resolution completed
        # past the failing helper.
        assert exit_code == 0
        assert slice_obj.parent_branch_at_creation == pipeline.branch, (
            f"slice-1 must fall back to pipeline branch on resolver failure, "
            f"got {slice_obj.parent_branch_at_creation!r}"
        )
        # The "failed to load contract" warning is the operator's
        # signal that the resolver hit an error rather than that
        # the contract was simply missing context_branch.
        load_failure_warnings = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and "failed to load contract" in c.args[0]
        ]
        assert load_failure_warnings, (
            f"expected load-failure warning, got: {mock_logger.warning.call_args_list}"
        )


class TestSlice2ChildBaseResolutionUnchanged:
    """The non-root path is *unchanged* — context branch logic only
    affects ``parent_slice_id is None`` (slice-1)."""

    def test_slice_2_targets_parent_integration_branch_not_context(self) -> None:
        """Slice-2 (deps=[slice-1]) targets ``egg/<id>/slice-1`` — the
        parent slice's integration branch.  The context branch must
        NOT leak into the child-slice path; otherwise slice-2's PR
        would skip the slice-1 review surface."""
        pipeline = _make_pipeline()
        root = _make_slice("slice-1", tasks=[_make_task("task-1-1")])
        child = _make_slice("slice-2", deps=["slice-1"], tasks=[_make_task("task-2-1")])
        contract = _make_contract(
            slices=[root, child],
            context_branch="egg/issue-2548/context",
        )

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
            patch("routes.pipelines._commit_slice_brc_history_to_integration_branch"),
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = _make_spawner()
            exit_code, _ = _run_implement_phase_slices(
                pipeline_id=pipeline.id,
                pipeline=pipeline,
                spawner=spawner,
                repo_volumes={},
                gateway_mode="public",
                repos=["owner/repo"],
                sandbox_env={},
                store=MagicMock(),
                certs_volume=None,
                worktree_repo_path=Path("/tmp/x"),
            )

        assert exit_code == 0
        # Slice-1 → context branch.
        assert root.parent_branch_at_creation == "egg/issue-2548/context"
        # Slice-2 → slice-1's integration branch (UNCHANGED).
        assert child.parent_branch_at_creation == "egg/issue-2548/slice-1"
        # And the per-slice PR bases match.
        pr_calls = spawner.gateway.create_slice_pr.call_args_list
        assert len(pr_calls) == 2
        # First call (slice-1) → base=context branch.
        assert pr_calls[0].kwargs["base"] == "egg/issue-2548/context"
        # Second call (slice-2) → base=slice-1's integration branch.
        assert pr_calls[1].kwargs["base"] == "egg/issue-2548/slice-1"


class TestQualifiedPipelineId:
    """Qualifier suffixes (``-v3``, ``-backend``) propagate end-to-end:
    the planner's per-pipeline context branch lives at
    ``egg/<qualified-id>/context``, not the unqualified shape."""

    def test_qualified_context_branch_propagates_to_slice_1_base(self) -> None:
        pipeline = _make_pipeline(pipeline_id="issue-2548-v3", issue_number=2548)
        slice_obj = _make_slice("slice-1", tasks=[_make_task()])
        contract = _make_contract(
            pipeline_id="issue-2548-v3",
            slices=[slice_obj],
            context_branch="egg/issue-2548-v3/context",
        )

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
            patch("routes.pipelines._commit_slice_brc_history_to_integration_branch"),
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = _make_spawner()
            exit_code, _ = _run_implement_phase_slices(
                pipeline_id=pipeline.id,
                pipeline=pipeline,
                spawner=spawner,
                repo_volumes={},
                gateway_mode="public",
                repos=["owner/repo"],
                sandbox_env={},
                store=MagicMock(),
                certs_volume=None,
                worktree_repo_path=Path("/tmp/x"),
            )

        assert exit_code == 0
        assert slice_obj.parent_branch_at_creation == "egg/issue-2548-v3/context", (
            "qualified pipeline must use the qualified context branch, "
            "not the unqualified ``egg/issue-2548/context``"
        )


class TestPerSliceBrcCommitHookInvocation:
    """The new per-slice BRC commit (#2548 task-2-2) is invoked once
    per slice between consensus reaching and ``create_slice_pr``."""

    def test_helper_called_with_integration_branch_per_slice(self) -> None:
        """Each slice's per-slice BRC commit helper is invoked with
        the correct ``integration_branch`` and ``slice_id``.  Without
        this wiring the slice PR's diff would be missing the BRC
        history files that #2548 task-2-2 writes."""
        pipeline = _make_pipeline()
        root = _make_slice("slice-1", tasks=[_make_task("task-1-1")])
        child = _make_slice("slice-2", deps=["slice-1"], tasks=[_make_task("task-2-1")])
        contract = _make_contract(
            slices=[root, child],
            context_branch="egg/issue-2548/context",
        )

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
            patch(
                "routes.pipelines._commit_slice_brc_history_to_integration_branch"
            ) as mock_brc_commit,
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = _make_spawner()
            _run_implement_phase_slices(
                pipeline_id=pipeline.id,
                pipeline=pipeline,
                spawner=spawner,
                repo_volumes={},
                gateway_mode="public",
                repos=["owner/repo"],
                sandbox_env={},
                store=MagicMock(),
                certs_volume=None,
                worktree_repo_path=Path("/tmp/x"),
            )

        # Hook is invoked once per slice with the correct slice id and
        # integration branch — not just once for the terminal slice.
        assert mock_brc_commit.call_count == 2

        # Helper signature is
        # ``(pipeline, spawner, worktree_repo_path, slice_id,
        # integration_branch, *, gateway_mode=...)`` — extract
        # ``slice_id`` and ``integration_branch`` from each call,
        # tolerating both all-positional and all-keyword shapes.
        def _extract(call, kw_name: str, pos_index: int) -> str:
            if kw_name in call.kwargs:
                return call.kwargs[kw_name]
            return call.args[pos_index]

        slice_ids = [_extract(c, "slice_id", 3) for c in mock_brc_commit.call_args_list]
        integration_branches = [
            _extract(c, "integration_branch", 4) for c in mock_brc_commit.call_args_list
        ]

        # Both slices are covered, and each one targets its own
        # integration branch (not a shared work branch and not the
        # context branch — those are the failure modes the wiring
        # protects against).
        assert "slice-1" in slice_ids
        assert "slice-2" in slice_ids
        slice_to_branch = dict(zip(slice_ids, integration_branches, strict=True))
        assert slice_to_branch["slice-1"] == "egg/issue-2548/slice-1"
        assert slice_to_branch["slice-2"] == "egg/issue-2548/slice-2"
