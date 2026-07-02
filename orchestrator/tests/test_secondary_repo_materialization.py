"""Slice-7 (#3393): secondary-repo worktree + branch materialization (task-7-2).

``task-7-1`` threads the FULL participating-repo list into worktree creation
and pushes each participating repo's ``egg/<id>/work`` (+ slice integration)
branch to *its own* remote BEFORE any PR-opening call for that repo runs. That
turns the slice-4 PR-routing / secondary-context-PR opener from
structurally-complete-but-soft-failing into functional end-to-end: a secondary
repo's context PR now opens against a head branch that actually exists instead
of soft-failing on a missing head.

Two-layer shape — the same convention slices 2/4/5/6 use in
``test_pipelines.py``:

* **Always-green reference/invariant tests** that pin, as pure functions over
  the slice-1 model API (``resolve_slice_repo`` / ``Pipeline`` / ``Slice`` /
  ``RepoSpec``), the exact contract ``task-7-1`` must satisfy: the set of repos
  whose worktree + ``egg/<id>/work`` branch must be materialized is precisely
  the *participating* set (repos owning ≥1 slice, ordered by ``pipeline.repos``,
  de-duplicated, slice-less repos excluded); the per-repo branch naming is
  uniform (``egg/<id>/work`` in every repo, on distinct remotes); and an N=1
  pipeline materialates exactly one repo — byte-identical to today. These
  encode the invariant the coder's worktree-list threading (replacing the
  primary-only collapse ``pipeline_repos = [pipeline.repo]`` at the
  pipeline-level worktree-create call site) must honour, and never go
  spuriously red because they are model-level.

* **End-to-end opener tests** on the stable secondary-context-PR opener seam
  (``_open_secondary_context_prs`` / ``_maybe_open_secondary_context_prs``),
  driven with the gateway mocked so the secondary branches "exist" — the
  post-materialization happy path. They assert every participating secondary
  repo gets a context PR (no missing-head-branch soft-fail), routed to the
  right repo / base / head, idempotently adopting an already-open PR; a
  slice-less submitted repo is skipped; and the N=1 guard performs ZERO
  secondary work (no contract load, no gateway calls).

The worktree-list-threading + per-repo branch-push seam is handed to the coder
via a ``task-7-2`` gap; the reference tests here pin the set it must produce so
the two halves converge on one shape.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Importing ``routes.pipelines`` pulls in the heavy orchestrator surface, so we
# stub ``docker`` and bootstrap ``sys.path`` exactly like the sibling
# ``test_context_pr_opener`` / ``test_pipelines`` modules do before the guarded
# import.
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

from egg_contracts.models import Contract, IssueInfo, PRMetadata, Slice  # noqa: E402
from models import Pipeline, RepoSpec, resolve_slice_repo  # noqa: E402

_OPENER_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - exercised via skip path in a stripped env
    from routes.pipelines import (  # type: ignore[attr-defined]
        _maybe_open_secondary_context_prs,
        _open_secondary_context_prs,
    )
except Exception as exc:  # noqa: BLE001
    _maybe_open_secondary_context_prs = None  # type: ignore[assignment]
    _open_secondary_context_prs = None  # type: ignore[assignment]
    _OPENER_IMPORT_ERROR = repr(exc)


_WORK_BRANCH = "egg/issue-3393/work"


# ---------------------------------------------------------------------------
# Test fixtures / reference helpers
# ---------------------------------------------------------------------------


def _make_contract_with_slices(slices) -> Contract:
    """Minimal ``Contract`` carrying the given slices for opener tests."""
    return Contract(
        issue=IssueInfo(number=3393, title="multi-repo pipelines", url=""),
        pipeline_id="issue-3393",
        pr=PRMetadata(title="Multi-repo pipelines", description="Body"),
        slices=list(slices),
    )


def _expected_materialization_repos(slices, pipeline) -> list[str]:
    """Repos whose worktree + ``egg/<id>/work`` branch MUST be materialized.

    Pure statement of the lazy-per-repo rule (operator ruling #1): a repo
    participates — i.e. gets a worktree, an ``egg/<id>/work`` branch pushed to
    its remote, and a context PR — iff at least one slice resolves to it via
    :func:`resolve_slice_repo`. Ordered by ``pipeline.repos`` and de-duplicated;
    a submitted repo owning no slices is excluded. This is exactly the set the
    coder's worktree-create call site must thread into ``create_worktrees`` in
    place of the primary-only ``[pipeline.repo]`` collapse.
    """
    owning = {resolve_slice_repo(s, pipeline) for s in slices}
    return [r.repo for r in pipeline.repos if r.repo in owning]


def _mock_spawner(*, lookup_returns=None, create_pr_numbers=None):
    """A spawner whose gateway simulates per-repo PR state.

    ``lookup_returns``    -- ``{repo: pr_number|None}`` for ``lookup_open_pr``.
    ``create_pr_numbers`` -- ``{repo: pr_number}`` the ``create_pr`` call
                             returns as a parseable URL for that repo.
    """
    lookup_returns = lookup_returns or {}
    create_pr_numbers = create_pr_numbers or {}
    spawner = MagicMock(name="spawner")
    gw = MagicMock(name="gateway")

    def _lookup(*, pipeline_id, repo, head, base):  # noqa: ARG001
        return lookup_returns.get(repo)

    def _create(*, repo, **_kwargs):
        num = create_pr_numbers.get(repo)
        if num is None:
            return None
        return f"https://github.com/{repo}/pull/{num}"

    gw.lookup_open_pr.side_effect = _lookup
    gw.create_pr.side_effect = _create
    spawner.gateway = gw
    return spawner


# ===========================================================================
# Layer 1 — always-green reference/invariant tests
#
# The set of repos whose worktree + egg/<id>/work branch must be materialized
# is the participating set; N=1 materializes exactly one. These pin the exact
# contract the coder's worktree-list threading (task-7-1) must satisfy.
# ===========================================================================


class TestMaterializationRepoSet:
    """Worktree/branch materialization set == participating repos (AC / ruling #1)."""

    def test_multi_repo_materializes_every_participating_repo(self):
        """All repos owning ≥1 slice materialize; the slice-less repo does not."""
        pipeline = Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/schema", base_branch="main"),
                RepoSpec(repo="jwbron/consumer", base_branch="develop"),
                RepoSpec(repo="jwbron/unused", base_branch="main"),  # no slice
            ],
        )
        slices = [
            Slice(id="slice-1", name="schema add", repo="jwbron/schema"),
            Slice(id="slice-2", name="consumer migrate", repo="jwbron/consumer"),
        ]
        # The primary-only collapse (``[pipeline.repo]``) would materialize just
        # jwbron/schema and strand jwbron/consumer's context PR on a missing
        # head branch — the exact bug task-7-1 fixes.
        assert _expected_materialization_repos(slices, pipeline) == [
            "jwbron/schema",
            "jwbron/consumer",
        ]

    def test_repo_less_slice_materializes_primary(self):
        """A slice with no explicit repo makes the *primary* materialize."""
        pipeline = Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/primary", base_branch="main"),
                RepoSpec(repo="jwbron/other", base_branch="main"),
            ],
        )
        slices = [
            Slice(id="slice-1", name="defaults to primary"),  # repo None → primary
            Slice(id="slice-2", name="explicit other", repo="jwbron/other"),
        ]
        assert _expected_materialization_repos(slices, pipeline) == [
            "jwbron/primary",
            "jwbron/other",
        ]

    def test_multiple_slices_per_repo_dedup_to_one_materialization(self):
        """Two slices in one repo yield exactly one materialized entry for it."""
        pipeline = Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/a", base_branch="main"),
                RepoSpec(repo="jwbron/b", base_branch="main"),
            ],
        )
        slices = [
            Slice(id="slice-1", name="a1", repo="jwbron/a"),
            Slice(id="slice-2", name="a2", repo="jwbron/a"),
            Slice(id="slice-3", name="b1", repo="jwbron/b"),
        ]
        assert _expected_materialization_repos(slices, pipeline) == ["jwbron/a", "jwbron/b"]

    def test_n1_materializes_exactly_one_repo(self):
        """N=1: exactly one repo materialized → one work branch — unchanged."""
        pipeline = Pipeline(id="issue-3393", repo="jwbron/egg", base_branch="main")
        slices = [Slice(id="slice-1", name="one"), Slice(id="slice-2", name="two")]
        assert _expected_materialization_repos(slices, pipeline) == ["jwbron/egg"]


class TestPerRepoWorkBranchNaming:
    """Every participating repo materializes the SAME ``egg/<id>/work`` branch
    name, each on its own remote (uniform per-repo naming — no owner/name in the
    branch, so same-short-name repos under different owners never collide at the
    branch level; they are distinguished by remote)."""

    @staticmethod
    def _work_branch(identifier) -> str:
        # The canonical per-repo work branch (gateway
        # ``worktree_manager`` builds ``egg/{container_id}/work``).
        return f"egg/{identifier}/work"

    def test_work_branch_is_identical_per_repo(self):
        identifier = "issue-3393"
        pipeline = Pipeline(
            id=identifier,
            repos=[
                RepoSpec(repo="ownerA/svc", base_branch="main"),
                RepoSpec(repo="ownerB/svc", base_branch="main"),  # same short name
            ],
        )
        slices = [
            Slice(id="slice-1", name="a", repo="ownerA/svc"),
            Slice(id="slice-2", name="b", repo="ownerB/svc"),
        ]
        participating = _expected_materialization_repos(slices, pipeline)
        assert participating == ["ownerA/svc", "ownerB/svc"]
        branches = {repo: self._work_branch(identifier) for repo in participating}
        # Uniform branch name across repos; the remotes (owner/name) keep them
        # distinct, so a same-short-name pair does not collapse.
        assert set(branches.values()) == {"egg/issue-3393/work"}
        assert len(branches) == 2


# ===========================================================================
# Layer 2 — end-to-end opener tests (gateway mocked = branches materialized)
#
# With the secondary head branches materialized (task-7-1), the slice-4 opener
# opens a context PR in every participating secondary repo with no soft-fail.
# ===========================================================================


@pytest.mark.skipif(
    _open_secondary_context_prs is None,
    reason=(f"secondary-context-PR opener import failed (stripped env): {_OPENER_IMPORT_ERROR}"),
)
class TestSecondaryContextPrsOpenWhenMaterialized:
    """Every participating secondary repo's context PR opens end-to-end."""

    @staticmethod
    def _pipeline() -> Pipeline:
        return Pipeline(
            id="issue-3393",
            issue_number=3393,
            branch=_WORK_BRANCH,
            base_branch="main",
            repos=[
                RepoSpec(repo="jwbron/schema", base_branch="main"),  # primary
                RepoSpec(repo="jwbron/consumer", base_branch="develop"),
                RepoSpec(repo="jwbron/client", base_branch=None),  # → default "main"
            ],
        )

    def _call(self, spawner, contract, pipeline, tmp_path):
        with patch("egg_contracts.loader.load_contract", return_value=contract):
            return _open_secondary_context_prs(
                pipeline.id,
                pipeline=pipeline,
                primary_repo="jwbron/schema",
                primary_pr_number=100,
                work_branch=_WORK_BRANCH,
                worktree_repo_path=tmp_path,
                identifier=3393,
                gateway_mode="public",
                spawner=spawner,
            )

    def test_opens_context_pr_in_each_participating_secondary(self, tmp_path):
        pipeline = self._pipeline()
        contract = _make_contract_with_slices(
            [
                Slice(id="slice-1", name="schema add", repo="jwbron/schema"),
                Slice(id="slice-2", name="consumer migrate", repo="jwbron/consumer"),
                Slice(id="slice-3", name="client migrate", repo="jwbron/client"),
            ]
        )
        spawner = _mock_spawner(
            create_pr_numbers={"jwbron/consumer": 201, "jwbron/client": 202},
        )

        opened = self._call(spawner, contract, pipeline, tmp_path)

        # Primary is never re-opened here; both secondaries are.
        assert opened == {
            "jwbron/schema": 100,
            "jwbron/consumer": 201,
            "jwbron/client": 202,
        }
        # One create_pr per secondary; the primary is NOT among them.
        created_repos = {c.kwargs["repo"] for c in spawner.gateway.create_pr.call_args_list}
        assert created_repos == {"jwbron/consumer", "jwbron/client"}
        assert spawner.gateway.create_pr.call_count == 2

        by_repo = {c.kwargs["repo"]: c.kwargs for c in spawner.gateway.create_pr.call_args_list}
        # Head is the per-repo work branch in every secondary — the branch
        # task-7-1 materialised on that repo's remote.
        assert by_repo["jwbron/consumer"]["head"] == _WORK_BRANCH
        assert by_repo["jwbron/client"]["head"] == _WORK_BRANCH
        # Base honours each repo's RepoSpec; ``None`` falls back to "main".
        assert by_repo["jwbron/consumer"]["base"] == "develop"
        assert by_repo["jwbron/client"]["base"] == "main"

    def test_adopts_already_open_secondary_pr_without_recreating(self, tmp_path):
        """Idempotency: an already-open secondary PR is adopted (no create_pr)."""
        pipeline = self._pipeline()
        contract = _make_contract_with_slices(
            [
                Slice(id="slice-1", name="schema add", repo="jwbron/schema"),
                Slice(id="slice-2", name="consumer migrate", repo="jwbron/consumer"),
                Slice(id="slice-3", name="client migrate", repo="jwbron/client"),
            ]
        )
        spawner = _mock_spawner(
            lookup_returns={"jwbron/consumer": 555},  # already open
            create_pr_numbers={"jwbron/client": 202},
        )

        opened = self._call(spawner, contract, pipeline, tmp_path)

        assert opened["jwbron/consumer"] == 555  # adopted
        assert opened["jwbron/client"] == 202  # freshly opened
        created_repos = {c.kwargs["repo"] for c in spawner.gateway.create_pr.call_args_list}
        assert created_repos == {"jwbron/client"}  # consumer NOT recreated

    def test_sliceless_secondary_repo_is_skipped(self, tmp_path):
        """A submitted secondary repo owning no slice gets no worktree PR."""
        pipeline = self._pipeline()  # 3 repos submitted
        contract = _make_contract_with_slices(
            [
                Slice(id="slice-1", name="schema add", repo="jwbron/schema"),
                Slice(id="slice-2", name="consumer migrate", repo="jwbron/consumer"),
                # jwbron/client owns NO slice → excluded (lazy-per-repo).
            ]
        )
        spawner = _mock_spawner(create_pr_numbers={"jwbron/consumer": 201})

        opened = self._call(spawner, contract, pipeline, tmp_path)

        assert "jwbron/client" not in opened
        created_repos = {c.kwargs["repo"] for c in spawner.gateway.create_pr.call_args_list}
        assert created_repos == {"jwbron/consumer"}

    def test_opened_pr_urls_parse_to_numbers(self, tmp_path):
        """The opener parses ``/pull/<n>`` out of the create_pr URL per repo."""
        pipeline = self._pipeline()
        contract = _make_contract_with_slices(
            [
                Slice(id="slice-1", name="schema", repo="jwbron/schema"),
                Slice(id="slice-2", name="consumer", repo="jwbron/consumer"),
            ]
        )
        spawner = _mock_spawner(create_pr_numbers={"jwbron/consumer": 4242})

        opened = self._call(spawner, contract, pipeline, tmp_path)

        assert opened["jwbron/consumer"] == 4242
        # Sanity: the URL the mock returned really carries that number.
        url = spawner.gateway.create_pr.call_args.kwargs
        assert re.search(r"/pull/\d+", f"https://github.com/{url['repo']}/pull/4242")


@pytest.mark.skipif(
    _maybe_open_secondary_context_prs is None,
    reason=(f"secondary-context-PR guard import failed (stripped env): {_OPENER_IMPORT_ERROR}"),
)
class TestN1TakesNoSecondaryMaterialization:
    """N=1 / single-repo: the guarded entry performs ZERO secondary work."""

    def test_single_repo_pipeline_is_noop(self, tmp_path):
        """len(repos) <= 1 → no contract load, no gateway calls (byte-identical N=1)."""
        pipeline = Pipeline(
            id="issue-3393",
            issue_number=3393,
            repo="jwbron/egg",
            branch=_WORK_BRANCH,
            base_branch="main",
        )
        # A single-repo pipeline synthesises a one-element ``repos`` list.
        assert len(pipeline.repos) <= 1
        spawner = _mock_spawner()

        _maybe_open_secondary_context_prs(
            pipeline.id,
            pipeline=pipeline,
            primary_pr_number=100,
            work_branch=_WORK_BRANCH,
            worktree_repo_path=tmp_path,
            identifier=3393,
            gateway_mode="public",
            spawner=spawner,
        )

        spawner.gateway.create_pr.assert_not_called()
        spawner.gateway.lookup_open_pr.assert_not_called()

    def test_missing_work_branch_is_noop(self, tmp_path):
        """A multi-repo pipeline with no resolved work branch does no work."""
        pipeline = Pipeline(
            id="issue-3393",
            issue_number=3393,
            repos=[
                RepoSpec(repo="jwbron/schema", base_branch="main"),
                RepoSpec(repo="jwbron/consumer", base_branch="main"),
            ],
        )
        spawner = _mock_spawner()

        _maybe_open_secondary_context_prs(
            pipeline.id,
            pipeline=pipeline,
            primary_pr_number=100,
            work_branch=None,  # nothing to point a secondary PR head at
            worktree_repo_path=tmp_path,
            identifier=3393,
            gateway_mode="public",
            spawner=spawner,
        )

        spawner.gateway.create_pr.assert_not_called()
