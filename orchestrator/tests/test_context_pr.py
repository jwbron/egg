"""Tests for ``_open_context_pr_for_pipeline`` (#2548 task-1-2 / task-1-3).

The orchestrator hook runs after plan_gate approval and before slice-1
provisioning.  It creates ``egg/<pipeline_id>/context``, copies the
refine + plan artifacts onto a temp worktree on that branch, commits +
pushes, opens the doc-only context PR via ``gh pr create``, and
persists ``contract.pr.context_branch`` / ``context_pr_number``.

This file pins:

* Happy path: PR is opened against ``pipeline.base_branch`` with
  ``head = egg/<pipeline_id>/context``; title/body fall through to
  ``pr.context_*`` first then ``pr.title`` / ``pr.description``.
* Idempotency: ``contract.pr.context_pr_number`` already populated
  short-circuits the hook (re-run safe).
* Short-circuits: missing ``pipeline.repo``, missing
  ``pipeline.base_branch``, missing ``contract.pr``, and missing
  refine/plan artifacts each early-return ``None``.
* Failure paths are *fail-soft* (decision-3 / D3): every branch /
  worktree / commit / push / PR-create error path returns ``None``
  without raising, so the plan→implement transition is never stranded.
* The artifacts copied include analysis.md, plan.md, refine + plan BRC
  json/md, and refine + plan agent transcripts (Q3 of the contract
  feedback).
* The hook is wired in *after* plan_gate approval and *before* slice-1
  provisioning at the call site, with the call site catching every
  exception so the hook can never block phase advance (D3).

Adversarial probes the coder might have missed:

* Empty ``context_title`` / ``title`` → no PR is opened (must not call
  ``create_pr``); contract is not mutated.
* PR URL without ``/pull/<n>`` → ``context_pr_number`` stays ``None``
  but ``context_branch`` is still persisted.
* Non-``main`` ``base_branch`` is honored end-to-end (gateway primitive
  AND ``create_pr`` base parameter).
* The hook calls ``_gather_context_pr_files`` against the WORK worktree
  (not the temp context worktree) — files are copied FROM work TO
  context.
"""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock heavy dependencies before importing routes.pipelines.
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))
_shared_path = _orchestrator_path.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


from gateway_client import PushResult  # noqa: E402
from models import Pipeline, PipelinePhase, PipelineStatus  # noqa: E402
from routes.pipelines import (  # noqa: E402
    _STATIC_CONTEXT_PR_FILE_GLOBS,
    _gather_context_pr_files,
    _open_context_pr_for_pipeline,
)

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def pipeline():
    """Pipeline with all the fields the hook reads."""
    return Pipeline(
        id="issue-2548",
        issue_number=2548,
        repo="owner/repo",
        branch="egg/issue-2548/work",
        base_branch="main",
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.PLAN,
    )


@pytest.fixture
def make_spawner():
    """Factory: build a MagicMock spawner.gateway with sensible defaults."""

    def _build(*, pr_url: str | None = "https://github.com/owner/repo/pull/4242"):
        spawner = MagicMock(name="spawner")
        gw = MagicMock(name="gateway")
        gw.create_context_branch.return_value = True
        gw.fetch_branch.return_value = True
        gw.push_worktree_branch.return_value = PushResult(ok=True)
        gw.create_pr.return_value = pr_url
        # Default to empty list so the post-#2548 recovery path
        # (#2548 review issue 1) finds no existing PR and the original
        # create_pr failure mode is preserved.  Tests that exercise the
        # recovery path override this explicitly.
        gw.list_open_prs.return_value = []
        spawner.gateway = gw
        return spawner

    return _build


@pytest.fixture(autouse=True)
def neutralise_git(monkeypatch):
    """Make subprocess.run + _commit_statefiles_to_worktree no-ops.

    The hook shells out to ``git worktree add / remove`` and calls the
    sibling ``_commit_statefiles_to_worktree`` helper, both of which
    require a real git repo at ``worktree_repo_path``.  The container
    sandbox blocks ``git init``, so we patch both surfaces to return
    success without touching the filesystem state created by
    ``_seed_repo``.
    """
    import subprocess

    def _fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", _fake_run)
    # Default to ``True`` so the post-#2548 contract-commit branch (and
    # any future call site that depends on the bool return) treats the
    # neutralised helper as if a commit was made — preserves existing
    # tests that assert a follow-up push happens.
    monkeypatch.setattr("routes.pipelines._commit_statefiles_to_worktree", lambda *a, **kw: True)


def _seed_repo(repo_root: Path, identifier: int | str) -> dict[str, Path]:
    """Seed the curated ``.egg-state/`` tree the hook reads.

    No real git init: the test sandbox blocks ``git init``, and the hook
    only calls subprocess.run via patched-helper paths anyway.  We just
    populate the artifact files so ``_gather_context_pr_files`` finds
    them, then patch subprocess.run + ``_commit_statefiles_to_worktree``
    in each test to neutralise the git operations.

    Returns a map of relative-name → absolute path so individual tests
    can introspect what's on disk before / after the hook runs.
    """
    state = repo_root / ".egg-state"
    drafts = state / "drafts"
    brc = state / "brc-history"
    outputs = state / "agent-outputs"
    for d in (drafts, brc, outputs):
        d.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}
    paths["analysis"] = drafts / f"{identifier}-analysis.md"
    paths["analysis"].write_text("# Analysis\n")
    paths["plan"] = drafts / f"{identifier}-plan.md"
    paths["plan"].write_text("# Plan\n")
    paths["refine_json"] = brc / f"{identifier}-refine.json"
    paths["refine_json"].write_text("{}\n")
    paths["refine_md"] = brc / f"{identifier}-refine.md"
    paths["refine_md"].write_text("# refine BRC\n")
    paths["plan_json"] = brc / f"{identifier}-plan.json"
    paths["plan_json"].write_text("{}\n")
    paths["plan_md"] = brc / f"{identifier}-plan.md"
    paths["plan_md"].write_text("# plan BRC\n")
    # Canonical agent-output filenames the orchestrator emits (per
    # `save_agent_output` shape in shared/egg_contracts/orchestrator.py:386):
    # `<id>-<role>-output.{md,json}`.  v2 of #2548 derives the glob
    # set from `get_roles_for_phase("refine")` /
    # `get_roles_for_phase("plan")` so the filenames must match real
    # production roles (refiner / architect / etc.) rather than the
    # phase-prefix shape that silently matched nothing.
    paths["refine_transcript"] = outputs / f"{identifier}-refiner-output.md"
    paths["refine_transcript"].write_text("refiner transcript\n")
    paths["plan_transcript"] = outputs / f"{identifier}-architect-output.md"
    paths["plan_transcript"].write_text("architect transcript\n")
    paths["refine_transcript_json"] = outputs / f"{identifier}-refiner-output.json"
    paths["refine_transcript_json"].write_text("{}\n")
    return paths


def _stub_load_save_contract(*, contract_pr_factory, missing: bool = False):
    """Return a (load_contract, save_contract, saved) triple of mocks.

    The first call to load_contract returns a fresh contract built by
    ``contract_pr_factory``.  Subsequent calls return whatever the most
    recent ``save_contract`` payload was — that lets us assert the
    persistence step actually wrote ``context_branch`` /
    ``context_pr_number``.
    """
    saved: list = []

    state: dict = {"current": None}

    def _load(identifier, repo_root):
        if missing:
            from egg_contracts.loader import ContractNotFoundError

            raise ContractNotFoundError(identifier, Path(repo_root))
        if state["current"] is None:
            state["current"] = contract_pr_factory()
        return state["current"]

    def _save(contract, repo_root):
        saved.append(contract)
        state["current"] = contract

    return _load, _save, saved


_UNSET = object()


def _make_contract(
    *,
    pr=_UNSET,
):
    """Build a Contract pointed at our test fixture defaults.

    Pass ``pr=None`` to build a contract whose ``pr`` block is
    explicitly missing (used by the short-circuit tests).  Omit the
    keyword to use the default ``PRMetadata`` fixture.
    """
    from egg_contracts.models import Contract, IssueInfo, PipelinePhase, PRMetadata

    if pr is _UNSET:
        pr = PRMetadata(title="Add context PR (#2548)")
    return Contract(
        issue=IssueInfo(number=2548, title="t", url=""),
        pipeline_id="issue-2548",
        current_phase=PipelinePhase.PLAN,
        pr=pr,
    )


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


class TestOpenContextPRHappyPath:
    def test_opens_pr_with_correct_base_head_title_body(self, tmp_path, pipeline, make_spawner):
        """The PR is opened against ``pipeline.base_branch`` with
        ``head = egg/<pipeline_id>/context``, title from
        ``pr.context_title`` (preferred) and body from
        ``pr.context_description`` (preferred)."""
        from egg_contracts.models import PRMetadata

        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, _saved = _stub_load_save_contract(
            contract_pr_factory=lambda: _make_contract(
                pr=PRMetadata(
                    title="Slice title",
                    description="Slice body",
                    context_title="Strategic plan for #2548",
                    context_description="Strategic narrative body",
                )
            )
        )
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result == "egg/issue-2548/context"
        spawner.gateway.create_context_branch.assert_called_once()
        kwargs = spawner.gateway.create_context_branch.call_args.kwargs
        assert kwargs["base_branch"] == "main"
        spawner.gateway.create_pr.assert_called_once()
        pr_kwargs = spawner.gateway.create_pr.call_args.kwargs
        assert pr_kwargs["base"] == "main"
        assert pr_kwargs["head"] == "egg/issue-2548/context"
        assert pr_kwargs["title"] == "Strategic plan for #2548"
        assert pr_kwargs["body"] == "Strategic narrative body"
        assert pr_kwargs["repo"] == "owner/repo"

    def test_falls_back_to_pr_title_when_context_title_missing(
        self, tmp_path, pipeline, make_spawner
    ):
        """Per #2548 contract feedback Q2: ``context_title`` is optional;
        when omitted the orchestrator falls back to ``pr.title``."""
        from egg_contracts.models import PRMetadata

        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(
            contract_pr_factory=lambda: _make_contract(
                pr=PRMetadata(title="Fallback title", description="Fallback body")
            )
        )
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        pr_kwargs = spawner.gateway.create_pr.call_args.kwargs
        assert pr_kwargs["title"] == "Fallback title"
        assert pr_kwargs["body"] == "Fallback body"

    def test_persists_context_branch_and_pr_number(self, tmp_path, pipeline, make_spawner):
        """Step 5: ``context_branch`` and ``context_pr_number`` are written
        to ``contract.pr`` after the PR is opened."""
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, saved = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert saved, "save_contract must be called after the PR opens"
        last = saved[-1]
        assert last.pr is not None
        assert last.pr.context_branch == "egg/issue-2548/context"
        assert last.pr.context_pr_number == 4242

    def test_pushes_branch_via_push_worktree_branch(self, tmp_path, pipeline, make_spawner):
        """The temp worktree is pushed via the orchestrator-trusted
        launcher-auth path (``push_worktree_branch``), with the right
        branch and base_branch.

        Two pushes are expected post-#2548 review issue 5: the context
        branch (carrying the artifacts) and the work branch (carrying
        the contract update for restart durability).  This test pins
        that the *context-branch* push happens with the right kwargs;
        the work-branch push is exercised by ``TestOpenContextPRDurability``.
        """
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        ctx_pushes = [
            c
            for c in spawner.gateway.push_worktree_branch.call_args_list
            if c.kwargs.get("branch") == "egg/issue-2548/context"
        ]
        assert len(ctx_pushes) == 1, (
            f"context-branch push must happen exactly once, got: "
            f"{spawner.gateway.push_worktree_branch.call_args_list}"
        )
        push_kwargs = ctx_pushes[0].kwargs
        assert push_kwargs["base_branch"] == "main"
        assert push_kwargs["pipeline_id"] == "issue-2548"

    def test_create_context_branch_called_before_create_pr(self, tmp_path, pipeline, make_spawner):
        """Pin ordering: branch must exist on origin before the PR is opened."""
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        order: list[str] = []
        spawner.gateway.create_context_branch.side_effect = lambda *a, **kw: (
            order.append("create_context_branch") or True
        )
        spawner.gateway.create_pr.side_effect = lambda *a, **kw: (
            order.append("create_pr") or "https://github.com/owner/repo/pull/4242"
        )
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert order == ["create_context_branch", "create_pr"], (
            "branch must exist on origin before the PR is opened"
        )


# ----------------------------------------------------------------------
# Idempotency / short-circuits
# ----------------------------------------------------------------------


class TestOpenContextPRShortCircuits:
    def test_skips_when_pipeline_has_no_repo(self, tmp_path, pipeline, make_spawner):
        pipeline.repo = None
        spawner = make_spawner()
        result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)
        assert result is None
        spawner.gateway.create_context_branch.assert_not_called()
        spawner.gateway.create_pr.assert_not_called()

    def test_skips_when_pipeline_has_no_base_branch(self, tmp_path, pipeline, make_spawner):
        pipeline.base_branch = None
        spawner = make_spawner()
        result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)
        assert result is None
        spawner.gateway.create_context_branch.assert_not_called()
        spawner.gateway.create_pr.assert_not_called()

    def test_idempotent_when_context_pr_already_opened(self, tmp_path, pipeline, make_spawner):
        """If ``context_pr_number`` is already populated, the hook returns
        the existing context branch unchanged WITHOUT touching the gateway
        or the contract again — exactly what protects respawned
        ``_run_pipeline`` threads from double-opening."""
        from egg_contracts.models import PRMetadata

        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, saved = _stub_load_save_contract(
            contract_pr_factory=lambda: _make_contract(
                pr=PRMetadata(
                    title="t",
                    context_branch="egg/issue-2548/context",
                    context_pr_number=4242,
                )
            )
        )
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result == "egg/issue-2548/context"
        spawner.gateway.create_context_branch.assert_not_called()
        spawner.gateway.create_pr.assert_not_called()
        assert saved == [], "must not re-write contract on idempotent skip"

    def test_skips_when_contract_pr_block_missing(self, tmp_path, pipeline, make_spawner):
        """A contract without a ``pr`` block has no title/description to
        author the PR with — short-circuit cleanly rather than guess."""
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(
            contract_pr_factory=lambda: _make_contract(pr=None)
        )
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result is None
        spawner.gateway.create_context_branch.assert_not_called()

    def test_skips_when_contract_not_found(self, tmp_path, pipeline, make_spawner):
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(
            contract_pr_factory=lambda: _make_contract(),
            missing=True,
        )
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result is None
        spawner.gateway.create_context_branch.assert_not_called()

    def test_skips_when_no_refine_or_plan_artifacts_present(self, tmp_path, pipeline, make_spawner):
        """An empty ``.egg-state/`` tree on the work worktree is a real
        possibility on babysit / custom mode pipelines that skipped the
        refine/plan phases.  The hook must short-circuit cleanly rather
        than open an empty PR."""
        # Don't seed any drafts / brc-history / agent-outputs files —
        # just rely on the autouse ``neutralise_git`` fixture so the
        # subprocess shells become no-ops.
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result is None
        # Branch creation may still have happened (it's pre-files-check),
        # but the PR must not be opened on an empty diff.
        spawner.gateway.create_pr.assert_not_called()

    def test_skips_when_title_and_context_title_both_empty(self, tmp_path, pipeline, make_spawner):
        """Adversarial probe: an empty title would let ``gh pr create``
        derive its own title from the commit message — losing the
        planner's framing.  The hook must refuse to open an empty-titled
        PR rather than silently fall back to gh's default."""
        from egg_contracts.models import PRMetadata

        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        # ``PRMetadata.title`` has min_length=1, so the only way to reach
        # the empty-title branch is with whitespace-only context_title +
        # whitespace-stripped title.  Whitespace-only context_title is
        # treated as "no override" by ``or`` semantics in the hook.
        load, save, _ = _stub_load_save_contract(
            contract_pr_factory=lambda: _make_contract(
                pr=PRMetadata(title="   ", context_title=None)
            )
        )
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result is None
        spawner.gateway.create_pr.assert_not_called()


# ----------------------------------------------------------------------
# Failure paths (D3 — fail soft, never strand the pipeline)
# ----------------------------------------------------------------------


class TestOpenContextPRFailSoft:
    """Per decision-3 of #2548 the context PR is doc-only auto-open: any
    failure here MUST be logged-and-swallowed so the plan→implement
    transition advances even when the context-PR mechanism itself is
    broken (gateway down, PR-author rate-limited, branch already
    diverged from a stale prior run, etc.)."""

    def test_returns_none_when_create_context_branch_raises(self, tmp_path, pipeline, make_spawner):
        from gateway_client import GatewayError

        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        spawner.gateway.create_context_branch.side_effect = GatewayError("kaboom")
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)
        assert result is None
        spawner.gateway.create_pr.assert_not_called()

    def test_returns_none_when_push_fails(self, tmp_path, pipeline, make_spawner):
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        spawner.gateway.push_worktree_branch.return_value = PushResult(
            ok=False, category="non_fast_forward", detail="rejected"
        )
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)
        assert result is None
        spawner.gateway.create_pr.assert_not_called()

    def test_returns_none_when_push_raises(self, tmp_path, pipeline, make_spawner):
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        spawner.gateway.push_worktree_branch.side_effect = RuntimeError("net down")
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)
        assert result is None
        spawner.gateway.create_pr.assert_not_called()

    def test_returns_none_when_create_pr_returns_none(self, tmp_path, pipeline, make_spawner):
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner(pr_url=None)
        load, save, saved = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)
        assert result is None
        # Contract must NOT be mutated when no PR was opened — otherwise
        # a subsequent re-entry would short-circuit on the ghost
        # context_pr_number and never retry.
        assert saved == [] or all(getattr(c.pr, "context_pr_number", None) is None for c in saved)

    def test_returns_none_when_create_pr_raises(self, tmp_path, pipeline, make_spawner):
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        spawner.gateway.create_pr.side_effect = RuntimeError("gh api 502")
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)
        assert result is None


# ----------------------------------------------------------------------
# Convergent idempotency: recover an existing PR after a prior tick's
# save_contract failed (#2548 review issue 1)
# ----------------------------------------------------------------------


class TestOpenContextPRRecoverExistingPR:
    """If a prior ``_run_pipeline`` tick opened the context PR but failed
    to persist (orchestrator restart, disk full, lock contention), the
    contract still says ``context_pr_number is None`` so the next tick
    re-enters the hook.  ``gh pr create`` then fails (duplicate
    head→base PR), and without recovery the contract stays out of sync
    with GitHub forever.  These tests pin that the recovery path
    queries ``list_open_prs`` and salvages the existing PR's number /
    URL into the contract."""

    def test_recovers_when_create_pr_raises_with_existing_pr(
        self, tmp_path, pipeline, make_spawner
    ):
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        spawner.gateway.create_pr.side_effect = RuntimeError(
            "gh: a pull request for branch 'egg/issue-2548/context' already exists"
        )
        spawner.gateway.list_open_prs.return_value = [
            {
                "number": 4242,
                "head_ref": "egg/issue-2548/context",
                "base_ref": "main",
            }
        ]
        load, save, saved = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result == "egg/issue-2548/context", (
            "recovery must surface the branch so the operator can correlate "
            "the GitHub PR with the pipeline"
        )
        spawner.gateway.list_open_prs.assert_called_once()
        assert saved, "contract must be persisted after recovery"
        last = saved[-1]
        assert last.pr.context_branch == "egg/issue-2548/context"
        assert last.pr.context_pr_number == 4242, (
            "recovery must populate the contract from the existing PR's number"
        )

    def test_recovers_when_create_pr_returns_no_url_with_existing_pr(
        self, tmp_path, pipeline, make_spawner
    ):
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner(pr_url=None)
        spawner.gateway.list_open_prs.return_value = [
            {
                "number": 4242,
                "head_ref": "egg/issue-2548/context",
                "base_ref": "main",
            }
        ]
        load, save, saved = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result == "egg/issue-2548/context"
        assert saved
        assert saved[-1].pr.context_pr_number == 4242

    def test_no_recovery_when_create_pr_raises_and_no_existing_pr(
        self, tmp_path, pipeline, make_spawner
    ):
        """When ``list_open_prs`` returns no matching PR, the original
        create_pr failure mode is preserved — return None, leave the
        contract untouched."""
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        spawner.gateway.create_pr.side_effect = RuntimeError("gh api 502")
        spawner.gateway.list_open_prs.return_value = []
        load, save, saved = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result is None
        # No save_contract calls — must not synthesize a phantom number
        # when no existing PR was found.
        assert saved == []

    def test_recovery_ignores_prs_with_mismatched_base_ref(self, tmp_path, pipeline, make_spawner):
        """A stale PR pointing at a different base branch must NOT be
        recovered — the branch shape is the same but the target diverges,
        so the operator's intent (PR against ``pipeline.base_branch``) is
        violated."""
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        spawner.gateway.create_pr.side_effect = RuntimeError("kaboom")
        spawner.gateway.list_open_prs.return_value = [
            {
                "number": 99,
                "head_ref": "egg/issue-2548/context",
                # Wrong base — must not match.
                "base_ref": "develop",
            }
        ]
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result is None

    def test_recovery_swallows_list_open_prs_failure(self, tmp_path, pipeline, make_spawner):
        """A best-effort recovery: a gateway failure during
        ``list_open_prs`` falls back to the original error path
        (return None) rather than propagating."""
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        spawner.gateway.create_pr.side_effect = RuntimeError("kaboom")
        spawner.gateway.list_open_prs.side_effect = RuntimeError("gateway 503")
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result is None


# ----------------------------------------------------------------------
# Durability of the contract update (#2548 review issue 5)
# ----------------------------------------------------------------------


class TestOpenContextPRDurability:
    """``save_contract`` only writes to disk — without a follow-up commit
    + push to the work branch, an orchestrator restart between
    ``save_contract`` and the next phase's commit cycle silently loses
    the context-PR linkage.  These tests pin that the hook commits +
    pushes the contract update so the linkage survives a restart."""

    def test_contract_update_is_committed_and_pushed_after_pr_open(
        self, tmp_path, pipeline, make_spawner, monkeypatch
    ):
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        commits: list[tuple[Path, str]] = []

        def _record_commit(worktree, message, *_, **__):
            commits.append((worktree, message))
            # Return True so the post-#2548 commit-then-push branch in
            # the hook treats the recorded commit as a real one and
            # follows through with the work-branch push.
            return True

        monkeypatch.setattr("routes.pipelines._commit_statefiles_to_worktree", _record_commit)
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        # The hook must commit twice: once on the temp context worktree
        # for the artifact files, once on the work worktree for the
        # contract update.  At minimum, one commit must target the work
        # worktree path with a "context PR linkage" message.
        contract_commits = [
            (wt, msg) for wt, msg in commits if wt == tmp_path and "context PR linkage" in msg
        ]
        assert contract_commits, (
            "contract update must be committed to the work worktree so "
            "an orchestrator restart does not lose the context-PR linkage"
        )

        # And the work-branch push must include the contract update.
        push_calls = spawner.gateway.push_worktree_branch.call_args_list
        work_branch_pushes = [
            c
            for c in push_calls
            if c.kwargs.get("branch") == pipeline.branch
            and c.kwargs.get("repo_path") == str(tmp_path)
        ]
        assert work_branch_pushes, (
            "contract update must be pushed to the work branch so the "
            "next tick (or a fresh orchestrator) sees the linkage on origin"
        )

    def test_contract_commit_failure_does_not_re_raise(
        self, tmp_path, pipeline, make_spawner, monkeypatch
    ):
        """If the commit/push fails, the hook still returns the branch
        name — recovery on the next tick is the safety net."""
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())

        # Make the contract commit raise (the artifact-files commit
        # already ran inside the patched-helper neutralise_git fixture).
        commit_calls: dict[str, int] = {"n": 0}

        def _commit_raises(worktree, message, *_, **__):
            commit_calls["n"] += 1
            if "context PR linkage" in message:
                raise RuntimeError("disk full mid-commit")

        monkeypatch.setattr("routes.pipelines._commit_statefiles_to_worktree", _commit_raises)
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)
        assert result == "egg/issue-2548/context"

    def test_contract_push_failure_does_not_re_raise(
        self, tmp_path, pipeline, make_spawner, monkeypatch
    ):
        """Symmetric coverage to ``test_contract_commit_failure_does_not_re_raise``:
        the commit-and-push pair must both swallow exceptions so a
        push raise does not strand the plan→implement transition
        (#2548 review suggestion E).  The PR is already open and the
        contract is already on disk; recovery on the next tick is the
        safety net."""
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        # The artifact-files push (push #1) succeeds; the work-branch
        # push (push #2 — the contract update) raises.  Iterate side
        # effects so successive calls hit the right branch.
        spawner.gateway.push_worktree_branch.side_effect = [
            PushResult(ok=True),  # context-branch artifact push
            RuntimeError("network blip mid-push"),  # work-branch contract push
        ]
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)
        assert result == "egg/issue-2548/context"

    def test_contract_push_skipped_when_commit_was_a_noop(
        self, tmp_path, pipeline, make_spawner, monkeypatch
    ):
        """When ``_commit_statefiles_to_worktree`` returns ``False``
        (no-op — nothing staged), the hook must NOT push.  Without
        this, every re-entry through the hook on a contract that
        already has the linkage would burn one fast-forward-no-op
        network round-trip per tick (#2548 review suggestion D)."""
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())

        # First call (artifact-files commit on the temp context
        # worktree) returns True so the artifact-files push fires;
        # second call (contract commit on the work worktree) returns
        # False to simulate a no-op idempotent re-entry.
        commit_returns: list[bool] = [True, False]

        def _commit_helper(*_, **__):
            return commit_returns.pop(0) if commit_returns else False

        monkeypatch.setattr("routes.pipelines._commit_statefiles_to_worktree", _commit_helper)
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        # Exactly one push call total — the artifact push to the
        # context branch.  The work-branch push must NOT have fired
        # because the contract commit was a no-op.
        push_calls = spawner.gateway.push_worktree_branch.call_args_list
        work_branch_pushes = [c for c in push_calls if c.kwargs.get("branch") == pipeline.branch]
        assert not work_branch_pushes, (
            "work-branch push must be skipped when the contract commit "
            "was a no-op — otherwise idempotent re-entries burn one "
            "fast-forward push per tick"
        )
        # Pin the positive case so a future regression that skipped
        # both pushes (e.g. by gating both on the same ``committed``
        # var or by a typo in the gate) would still fail this test
        # (#2548 review suggestion G).
        context_branch_pushes = [
            c for c in push_calls if c.kwargs.get("branch") == "egg/issue-2548/context"
        ]
        assert context_branch_pushes, (
            "artifact-files push to the context branch must fire when "
            "the artifact commit was non-empty — without this assert, "
            "a regression skipping both pushes would still pass"
        )

    def test_artifact_push_skipped_when_artifact_commit_was_a_noop(
        self, tmp_path, pipeline, make_spawner, monkeypatch
    ):
        """Symmetric to ``test_contract_push_skipped_when_commit_was_a_noop``.
        When the **artifact-files** commit is a no-op (idempotent
        re-entry: the temp worktree's HEAD already matches origin so
        the staged-vs-HEAD diff is empty), the artifact-files push
        must be skipped too — origin already carries the same content
        and the push would be a fast-forward no-op (#2548 review
        suggestion F)."""
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())

        # First call (artifact-files commit on the temp context
        # worktree) returns False — the no-op re-entry case.  The
        # second call (contract commit on the work worktree) is
        # irrelevant for this test but returning True keeps the test
        # exercising the rest of the hook to make sure the push gate
        # is the only thing that changed.
        commit_returns: list[bool] = [False, True]

        def _commit_helper(*_, **__):
            return commit_returns.pop(0) if commit_returns else False

        monkeypatch.setattr("routes.pipelines._commit_statefiles_to_worktree", _commit_helper)
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        push_calls = spawner.gateway.push_worktree_branch.call_args_list
        context_branch_pushes = [
            c for c in push_calls if c.kwargs.get("branch") == "egg/issue-2548/context"
        ]
        assert not context_branch_pushes, (
            "artifact-files push must be skipped when the artifact "
            "commit was a no-op — otherwise idempotent re-entries burn "
            "one fast-forward push per tick"
        )


# ----------------------------------------------------------------------
# Adversarial probes
# ----------------------------------------------------------------------


class TestOpenContextPRAdversarial:
    def test_pr_url_without_pull_n_pattern_persists_branch_only(
        self, tmp_path, pipeline, make_spawner
    ):
        """Adversarial: a malformed PR URL (e.g. an internal helper that
        returned the bare repo URL) should not stash a None ``context_pr_number``
        as 0 or raise — the branch is persisted but the number stays None.

        This pins that the regex on ``/pull/(\\d+)`` is the only source of
        truth and that an empty match doesn't poison the persistence
        step.
        """
        _seed_repo(tmp_path, identifier=2548)
        # URL with no /pull/<n> segment.
        spawner = make_spawner(pr_url="https://github.com/owner/repo")
        load, save, saved = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result == "egg/issue-2548/context"
        assert saved, "context_branch must still be persisted"
        last = saved[-1]
        assert last.pr.context_branch == "egg/issue-2548/context"
        assert last.pr.context_pr_number is None, (
            "must not synthesize a fake PR number when /pull/<n> can't be parsed"
        )

    def test_non_main_base_branch_threads_through_end_to_end(
        self, tmp_path, pipeline, make_spawner
    ):
        """Pin that the base_branch parameter is honoured end-to-end:
        gateway primitive AND ``create_pr`` base, AND ``push_worktree_branch``
        base_branch parameter (used for rebase reconcile)."""
        pipeline.base_branch = "develop"
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert spawner.gateway.create_context_branch.call_args.kwargs["base_branch"] == "develop"
        assert spawner.gateway.create_pr.call_args.kwargs["base"] == "develop"
        assert spawner.gateway.push_worktree_branch.call_args.kwargs["base_branch"] == "develop"

    def test_files_copied_include_all_curated_artifacts(self, tmp_path, pipeline, make_spawner):
        """Q3 of the contract feedback: the context PR must carry analysis,
        plan, refine + plan BRC json/md, AND refine + plan agent transcripts.

        Adversarial intent: pin every glob the coder added so a future
        refactor that drops one of them silently is caught immediately."""
        identifier = 2548
        seeded = _seed_repo(tmp_path, identifier=identifier)
        found = _gather_context_pr_files(tmp_path, identifier)
        rel = {p.relative_to(tmp_path) for p in found}

        # Spot-check every kind of artifact: the seven seeded-paths are
        # the truth set, and every one of them must show up in the gather
        # output.  Names are explicit so a future ``_seed_repo`` change
        # that drops a path is also caught.
        expected = {
            seeded["analysis"].relative_to(tmp_path),
            seeded["plan"].relative_to(tmp_path),
            seeded["refine_json"].relative_to(tmp_path),
            seeded["refine_md"].relative_to(tmp_path),
            seeded["plan_json"].relative_to(tmp_path),
            seeded["plan_md"].relative_to(tmp_path),
            seeded["refine_transcript"].relative_to(tmp_path),
            seeded["plan_transcript"].relative_to(tmp_path),
            seeded["refine_transcript_json"].relative_to(tmp_path),
        }
        missing = expected - rel
        assert not missing, f"gather dropped expected artifacts: {missing}"

    def test_gather_does_not_pick_up_other_pipelines_files(self, tmp_path):
        """Pipeline isolation: a stray draft for ``other-pipeline`` in
        the same ``.egg-state/`` tree must not leak into the context PR
        diff for our pipeline.

        This pins the prefix-anchored glob behavior the coder inherited
        via ``glob.escape(identifier)``.  Without prefix anchoring,
        identifier ``2548`` would substring-match ``25481-plan.md`` and
        adjacent issues would cross-contaminate."""
        # Seed our pipeline (2548) plus an adjacent pipeline whose
        # identifier shares ``2548`` as a substring.
        _seed_repo(tmp_path, identifier=2548)
        other_drafts = tmp_path / ".egg-state" / "drafts"
        # NOTE: 25480 contains 2548 as a substring → catches naive
        # ``identifier in name`` regressions.
        (other_drafts / "25480-plan.md").write_text("# other pipeline\n")
        # And a pre-unification bare-int contract collision.
        (other_drafts / "2548999-plan.md").write_text("# yet another\n")

        found = _gather_context_pr_files(tmp_path, 2548)
        names = {p.name for p in found}
        assert "2548-plan.md" in names
        assert "25480-plan.md" not in names
        assert "2548999-plan.md" not in names

    def test_context_branch_name_is_egg_pipeline_id_context(self, tmp_path, pipeline, make_spawner):
        """Pin the canonical branch shape — ``egg/<pipeline_id>/context`` —
        and that the gateway exemption regex (#2548) accepts it."""
        from gateway.gateway import _CONTEXT_BRANCH_RE

        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result == "egg/issue-2548/context"
        # Gateway exemption regex must accept this exact shape.
        assert _CONTEXT_BRANCH_RE.match("egg/issue-2548/context")
        # And reject obvious near-misses that the orchestrator does NOT
        # produce (multi-segment, missing /context suffix, etc.).
        assert not _CONTEXT_BRANCH_RE.match("egg/issue-2548/context/extra")
        assert not _CONTEXT_BRANCH_RE.match("egg/issue-2548")

    def test_qualified_pipeline_id_context_branch(self, tmp_path, make_spawner):
        """Pipelines like ``issue-2548-v2`` must produce
        ``egg/issue-2548-v2/context`` — single-segment ``<base>``
        branch shape that the gateway regex still accepts.

        Qualified pipelines key per-state files by ``pipeline_id``
        (``issue-2548-v2``) rather than by the bare ``issue_number``
        (#1762 CUSTOM-mode disambiguation in ``_pipeline_identifier``),
        so the test seeds with the qualified identifier shape.
        """
        from gateway.gateway import _CONTEXT_BRANCH_RE

        pipeline = Pipeline(
            id="issue-2548-v2",
            issue_number=2548,
            repo="owner/repo",
            branch="egg/issue-2548-v2/work",
            base_branch="main",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.PLAN,
        )
        # ``_pipeline_identifier`` keys by pipeline_id for qualified
        # pipelines, so seed under that identifier shape.
        _seed_repo(tmp_path, identifier="issue-2548-v2")
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result == "egg/issue-2548-v2/context"
        assert _CONTEXT_BRANCH_RE.match("egg/issue-2548-v2/context")

    def test_gather_rejects_path_traversal_identifier(self, tmp_path):
        """``glob.escape`` does not escape ``..`` or ``/`` — defense-in-depth
        check rejects any identifier that does not match the production
        shape so a future call site that bypasses ``_pipeline_identifier``
        cannot smuggle a traversal payload into ``.egg-state/`` paths
        (#2548 review issue 10)."""
        _seed_repo(tmp_path, identifier=2548)
        # Path traversal attempts.
        assert _gather_context_pr_files(tmp_path, "../../etc/passwd") == []
        assert _gather_context_pr_files(tmp_path, "foo/bar") == []
        # Empty / leading-dot identifiers are also rejected.
        assert _gather_context_pr_files(tmp_path, "") == []
        assert _gather_context_pr_files(tmp_path, ".hidden") == []
        # Valid shapes still work — sanity check the negative tests
        # didn't accidentally break the happy path.
        found = _gather_context_pr_files(tmp_path, 2548)
        assert found, "valid identifier must still gather files"

    def test_agent_output_suffix_set_is_explicit_allowlist(self):
        """``_AGENT_OUTPUT_SUFFIXES`` must be an explicit allowlist, not
        an open-ended wildcard set.  Wildcard suffixes (``-*.json``,
        ``-*.md``) would pick up arbitrary sidecar files an agent
        writes (debug dumps, partial state, raw prompts) onto the
        publicly-reviewable context PR (#2548 review issue 7)."""
        from routes.pipelines import _AGENT_OUTPUT_SUFFIXES

        for suffix in _AGENT_OUTPUT_SUFFIXES:
            assert "*" not in suffix, (
                f"open-ended wildcard suffix {suffix!r} would let agents leak "
                "arbitrary sidecar files onto the public context PR — keep "
                "the suffix set as an explicit allowlist"
            )

    def test_static_glob_inventory_matches_documented_artifact_set(self):
        """Pin the *static* portion of the curated glob set (the part
        that is NOT derived from ``get_roles_for_phase``).  Agent-
        transcript globs live on the dynamic side (#2548 v2: derived
        from refine + plan rosters at runtime so a future role
        addition is auto-picked up).  Future refactors that drop one
        of the static entries — or sneak in an unauthorized one,
        e.g. ``.egg-state/contracts/*.json`` which would leak the
        contract itself onto the context PR — are caught here."""
        expected_static = {
            ".egg-state/drafts/{identifier}-analysis.md",
            ".egg-state/drafts/{identifier}-plan.md",
            ".egg-state/brc-history/{identifier}-refine.json",
            ".egg-state/brc-history/{identifier}-refine.md",
            ".egg-state/brc-history/{identifier}-plan.json",
            ".egg-state/brc-history/{identifier}-plan.md",
        }
        assert set(_STATIC_CONTEXT_PR_FILE_GLOBS) == expected_static, (
            "The static-glob set must match the documented Q3 answer "
            "(analysis + plan + refine/plan BRC); agent transcripts are "
            "added dynamically from the role roster."
        )
        # Defensive: contract files MUST NOT appear in this set.
        for tmpl in _STATIC_CONTEXT_PR_FILE_GLOBS:
            assert "/contracts/" not in tmpl, (
                "context PR must not include .egg-state/contracts/*.json"
            )
            assert "/agent-outputs/" not in tmpl, (
                "agent-outputs must be added dynamically via "
                "_refine_and_plan_role_values, not statically"
            )

    def test_save_contract_failure_does_not_re_raise(self, tmp_path, pipeline, make_spawner):
        """If persistence fails *after* the PR has been opened, the hook
        must log-and-swallow — re-raising would propagate to the caller's
        ``except`` and cascade to the phase advance, undoing the PR open
        but leaving GitHub state ahead of the contract."""
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load_count = {"n": 0}

        def _load(identifier, repo_root):
            load_count["n"] += 1
            return _make_contract()

        def _save_raises(contract, repo_root):
            raise RuntimeError("disk full")

        with (
            patch("egg_contracts.loader.load_contract", _load),
            patch("egg_contracts.loader.save_contract", _save_raises),
        ):
            result = _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        assert result == "egg/issue-2548/context", (
            "PR was opened — hook must return the branch name even when "
            "persistence fails so the operator can correlate the GitHub "
            "PR with the pipeline"
        )
        spawner.gateway.create_pr.assert_called_once()


# ----------------------------------------------------------------------
# D3 wiring — call site swallows hook failures
# ----------------------------------------------------------------------


class TestOpenContextPRCallSiteWiring:
    """The hook is wired in at the plan→implement transition with a
    try/except so any failure is logged-and-swallowed (decision-3 / D3
    of #2548).  These tests pin the call site itself, not just the
    helper, so a future refactor that hoists the hook out of the
    try/except is caught."""

    def test_call_site_is_gated_on_plan_phase(self):
        """The hook only fires after the plan phase — re-entering the
        same code on a different phase MUST NOT re-open the context PR.

        After #2593 the call site routes through the shared
        ``_maybe_open_base_pr_for_plan_to_implement`` wrapper, so the
        regression check is against the wrapper invocation rather than
        the inner ``_open_context_pr_for_pipeline`` call (the wrapper
        owns the CUSTOM-mode guard and exception swallow now)."""
        src = Path(__file__).parent.parent / "routes" / "pipelines.py"
        text = src.read_text()
        m = re.search(
            r'if\s+current_phase\.value\s*==\s*"plan"[^\n:]*:\s*\n\s*'
            r"_maybe_open_base_pr_for_plan_to_implement\(",
            text,
        )
        assert m is not None, (
            "plan→implement call site must be gated on "
            "current_phase.value == 'plan' and route through "
            "_maybe_open_base_pr_for_plan_to_implement (D3, #2593)"
        )

    def test_call_site_is_gated_on_non_custom_mode(self):
        """CUSTOM-mode pipelines (#1762) terminate after a single phase
        and never advance to implement — opening a context PR for them
        would orphan a PR on GitHub with no slice PRs to stack on
        (#2548 review issue 3).  Under #2593 the CUSTOM-mode skip moved
        from the call site into the
        ``_maybe_open_base_pr_for_plan_to_implement`` wrapper so every
        transition path inherits the same guard.  Pin the guard in the
        wrapper body."""
        src = Path(__file__).parent.parent / "routes" / "pipelines.py"
        text = src.read_text()
        # Look inside the wrapper for the CUSTOM-mode early return.
        # The body between ``if _is_custom_mode:`` and the ``return``
        # may contain a log line (#2593 review issue 10) — match
        # non-greedy across comments/logging so the regression check
        # still catches a missing return.
        m = re.search(
            r"def\s+_maybe_open_base_pr_for_plan_to_implement\b.+?"
            r"_is_custom_mode\s*=\s*getattr\(pipeline,\s*['\"]mode['\"],\s*None\)"
            r"\s*==\s*PipelineMode\.CUSTOM.+?if\s+_is_custom_mode\s*:.+?return\b",
            text,
            flags=re.DOTALL,
        )
        assert m is not None, (
            "_maybe_open_base_pr_for_plan_to_implement must skip CUSTOM-mode "
            "pipelines: they terminate after one phase and would orphan "
            "the context PR (#2548 review issue 3, #2593)"
        )

    def test_call_site_swallows_any_exception(self):
        """The hook can never block the plan→implement transition, even
        if the inner helper raises on top of its own internal swallows
        (D3 of #2548).  Under #2593 the swallow moved from the call
        site into the
        ``_maybe_open_base_pr_for_plan_to_implement`` wrapper so every
        transition path inherits the same protection.  Pin the
        try/except around the inner call inside the wrapper.
        """
        src = Path(__file__).parent.parent / "routes" / "pipelines.py"
        text = src.read_text()
        # The wrapper must wrap the inner _open_context_pr_for_pipeline
        # call in a try/except Exception so any raise becomes a log line.
        m = re.search(
            r"def\s+_maybe_open_base_pr_for_plan_to_implement\b.+?"
            r"try:\s*\n\s*_open_context_pr_for_pipeline\(.+?\)"
            r"\s*\n\s*except\s+Exception",
            text,
            flags=re.DOTALL,
        )
        assert m is not None, (
            "_maybe_open_base_pr_for_plan_to_implement must wrap "
            "_open_context_pr_for_pipeline in try/except Exception so a "
            "hook failure can never escape into the transition path (D3)"
        )
