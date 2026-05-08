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
    _CONTEXT_PR_FILE_GLOBS,
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
    monkeypatch.setattr("routes.pipelines._commit_statefiles_to_worktree", lambda *a, **kw: None)


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
    paths["refine_transcript"] = outputs / f"{identifier}-refine-architect.md"
    paths["refine_transcript"].write_text("refine architect transcript\n")
    paths["plan_transcript"] = outputs / f"{identifier}-plan-planner.md"
    paths["plan_transcript"].write_text("plan planner transcript\n")
    paths["refine_transcript_json"] = outputs / f"{identifier}-refine-architect.json"
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
        branch and base_branch."""
        _seed_repo(tmp_path, identifier=2548)
        spawner = make_spawner()
        load, save, _ = _stub_load_save_contract(contract_pr_factory=lambda: _make_contract())
        with (
            patch("egg_contracts.loader.load_contract", load),
            patch("egg_contracts.loader.save_contract", save),
        ):
            _open_context_pr_for_pipeline(pipeline, spawner, tmp_path)

        spawner.gateway.push_worktree_branch.assert_called_once()
        push_kwargs = spawner.gateway.push_worktree_branch.call_args.kwargs
        assert push_kwargs["branch"] == "egg/issue-2548/context"
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

    def test_glob_inventory_matches_documented_artifact_set(self):
        """Pin the exact set of curated globs.  Future refactors that
        drop one (or sneak in an unauthorized one — e.g.
        ``.egg-state/contracts/*.json`` which would leak the contract
        itself onto the context PR) are caught here."""
        expected = {
            ".egg-state/drafts/{identifier}-analysis.md",
            ".egg-state/drafts/{identifier}-plan.md",
            ".egg-state/brc-history/{identifier}-refine.json",
            ".egg-state/brc-history/{identifier}-refine.md",
            ".egg-state/brc-history/{identifier}-plan.json",
            ".egg-state/brc-history/{identifier}-plan.md",
            ".egg-state/agent-outputs/{identifier}-refine-*.md",
            ".egg-state/agent-outputs/{identifier}-refine-*.json",
            ".egg-state/agent-outputs/{identifier}-plan-*.md",
            ".egg-state/agent-outputs/{identifier}-plan-*.json",
        }
        assert set(_CONTEXT_PR_FILE_GLOBS) == expected, (
            "The curated artifact set must match the documented Q3 answer "
            "(analysis + plan + refine/plan BRC + refine/plan transcripts)."
        )
        # Defensive: contract files MUST NOT appear in this set.
        for tmpl in _CONTEXT_PR_FILE_GLOBS:
            assert "/contracts/" not in tmpl, (
                "context PR must not include .egg-state/contracts/*.json"
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
        same code on a different phase MUST NOT re-open the context PR."""
        src = Path(__file__).parent.parent / "routes" / "pipelines.py"
        text = src.read_text()
        # The call site must be inside an ``if current_phase.value == "plan":`` block.
        m = re.search(
            r'if\s+current_phase\.value\s*==\s*"plan"\s*:\s*\n\s*try:\s*\n\s*'
            r"_open_context_pr_for_pipeline\(",
            text,
        )
        assert m is not None, (
            "_open_context_pr_for_pipeline call site must be gated on "
            "current_phase.value == 'plan' AND wrapped in try/except (D3)"
        )

    def test_call_site_swallows_any_exception(self):
        """The call site catches a broad ``except`` so the hook can never
        block the plan→implement transition, even if the helper raises
        on top of its own internal swallows."""
        src = Path(__file__).parent.parent / "routes" / "pipelines.py"
        text = src.read_text()
        # Locate the call and verify the surrounding ``except`` clause
        # logs-and-continues.
        # Allow flexible whitespace + trailing kwargs between the call and
        # the closing paren before the matching except.
        m = re.search(
            r"_open_context_pr_for_pipeline\(.+?\)\s*\n\s*except\s+Exception",
            text,
            flags=re.DOTALL,
        )
        assert m is not None, (
            "call site must be inside ``try: ...; except Exception``: a hook "
            "failure must never escape into _run_pipeline (D3)"
        )
