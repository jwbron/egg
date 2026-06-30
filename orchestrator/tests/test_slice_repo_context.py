"""Tests for ``_resolve_slice_repo_context`` (#3393 multi-repo pipelines).

The helper decides, for one slice, which repo its branch / PR / git ops
target and which orchestrator worktree to run them against. Primary-repo
(or unset) slices must resolve to the primary worktree unchanged so the
single-repo flow is byte-for-byte untouched; secondary-repo slices
resolve to their sibling worktree when provisioned and signal
``provisioned=False`` otherwise so the caller can fail loudly.
"""

from __future__ import annotations

from egg_contracts.models import Slice
from models import AdditionalRepo, Pipeline
from routes.pipelines import _resolve_slice_repo_context


def _worktrees(tmp_path, *repo_names):
    """Create a container worktree root with one dir per repo name.

    Returns (primary_worktree_path, repo_volumes) where repo_volumes maps
    repo name → path, mirroring ``WorktreeResult.worktrees``.
    """
    root = tmp_path / "container"
    root.mkdir()
    volumes = {}
    for name in repo_names:
        d = root / name
        d.mkdir()
        volumes[name] = str(d)
    return root / repo_names[0], volumes


def test_primary_slice_resolves_to_primary_worktree(tmp_path):
    primary_wt, volumes = _worktrees(tmp_path, "primary")
    pipeline = Pipeline(id="issue-1", repo="owner/primary")
    slice_obj = Slice(id="slice-1", name="x")  # repo unset → primary
    ctx = _resolve_slice_repo_context(pipeline, slice_obj, primary_wt, volumes)
    assert ctx.repo == "owner/primary"
    assert ctx.worktree_path == primary_wt
    assert ctx.is_secondary is False
    assert ctx.provisioned is True


def test_slice_repo_equal_to_primary_is_not_secondary(tmp_path):
    primary_wt, volumes = _worktrees(tmp_path, "primary")
    pipeline = Pipeline(id="issue-1", repo="owner/primary")
    slice_obj = Slice(id="slice-1", name="x", repo="owner/primary")
    ctx = _resolve_slice_repo_context(pipeline, slice_obj, primary_wt, volumes)
    assert ctx.is_secondary is False
    assert ctx.worktree_path == primary_wt


def test_provisioned_secondary_slice_resolves_to_sibling_worktree(tmp_path):
    primary_wt, volumes = _worktrees(tmp_path, "primary", "schema")
    pipeline = Pipeline(
        id="issue-1",
        repo="owner/primary",
        additional_repos=[AdditionalRepo(repo="owner/schema")],
    )
    slice_obj = Slice(id="slice-2", name="schema bump", repo="owner/schema")
    ctx = _resolve_slice_repo_context(pipeline, slice_obj, primary_wt, volumes)
    assert ctx.repo == "owner/schema"
    assert ctx.worktree_path == primary_wt.parent / "schema"
    assert ctx.is_secondary is True
    assert ctx.provisioned is True


def test_unprovisioned_secondary_slice_flags_not_provisioned(tmp_path):
    # repo_volumes lacks 'schema' → the secondary repo was never provisioned.
    primary_wt, volumes = _worktrees(tmp_path, "primary")
    pipeline = Pipeline(id="issue-1", repo="owner/primary")
    slice_obj = Slice(id="slice-2", name="x", repo="owner/schema")
    ctx = _resolve_slice_repo_context(pipeline, slice_obj, primary_wt, volumes)
    assert ctx.repo == "owner/schema"
    assert ctx.is_secondary is True
    assert ctx.provisioned is False
    assert ctx.worktree_path is None


def test_none_slice_obj_resolves_to_primary(tmp_path):
    primary_wt, volumes = _worktrees(tmp_path, "primary")
    pipeline = Pipeline(id="issue-1", repo="owner/primary")
    ctx = _resolve_slice_repo_context(pipeline, None, primary_wt, volumes)
    assert ctx.repo == "owner/primary"
    assert ctx.is_secondary is False
