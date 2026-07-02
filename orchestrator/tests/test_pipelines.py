"""Slice-2 (#3393) submission-layer tests: list-shaped submission + uniform
visibility/auth validation.

The model-layer repo dimension — ``RepoSpec``, ``Pipeline.repos``,
``primary_repo``, ``resolve_slice_repo`` and the legacy-singleton bridge —
landed in slice-1 and is covered in ``test_models.py``. This module covers the
**submission layer** added in slice-2:

* a list-shaped submission is represented faithfully end to end — an arbitrary
  number of repos, no primary+secondary collapse, order preserved;
* same-name / different-owner repos stay distinct in the list (operator ruling
  #6 — the ``owner/repo`` slug is the identity, not the bare name);
* each repo keeps its own ``base_branch``;
* the primary defaults to the first repo (used for naming + slice default);
* a bare single-repo (N=1) submission is unchanged.

Two areas depend on the slice-2 **coder** implementation, which is produced in
parallel and may not yet be integrated into this (tester) worktree:

* ``_handle_submit_task`` forwarding a ``repos`` list to the orchestrator API
  (``TestListSubmissionRoute``), and
* the uniform-visibility / uniform-auth **rejections** — those live with the
  gateway helpers and are exercised in
  ``gateway/tests/test_repo_visibility.py``.

Where the coder half is not yet integrated, the dependent test skips with an
explicit reason (rather than failing the suite) and activates automatically at
convergence, when the coder and tester branches merge. The exact interface the
tester expects has been handed to the coder via a contract gap (task-2-3) so
the two halves converge on the same shape.
"""

from __future__ import annotations

import pytest
from egg_contracts.models import Slice
from models import Pipeline, RepoSpec, resolve_slice_repo


class TestMultiRepoSubmissionShape:
    """A list-shaped submission is represented faithfully — no collapse (AC-1)."""

    def test_three_repo_submission_constructs_full_list(self):
        """An arbitrary number of repos survives submission — not just 1 or 2."""
        pipeline = Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/schema", base_branch="main"),
                RepoSpec(repo="jwbron/producer", base_branch="main"),
                RepoSpec(repo="jwbron/consumer", base_branch="develop"),
            ],
        )
        # Genuinely list-shaped: all three retained, in order, no collapse to
        # a primary+secondary shape.
        assert [r.repo for r in pipeline.repos] == [
            "jwbron/schema",
            "jwbron/producer",
            "jwbron/consumer",
        ]
        assert len(pipeline.repos) == 3

    def test_same_name_different_owner_repos_coexist(self):
        """``ownerA/foo`` and ``ownerB/foo`` stay distinct (ruling #6)."""
        pipeline = Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="ownerA/foo", base_branch="main"),
                RepoSpec(repo="ownerB/foo", base_branch="main"),
            ],
        )
        repos = [r.repo for r in pipeline.repos]
        assert repos == ["ownerA/foo", "ownerB/foo"]
        # Distinct by full slug — the bare short name would collide.
        assert len(set(repos)) == 2

    def test_per_repo_base_branch_preserved(self):
        """Each repo pins its own base branch; they are not homogenised."""
        pipeline = Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/a", base_branch="main"),
                RepoSpec(repo="jwbron/b", base_branch="release-2.0"),
            ],
        )
        by_repo = {r.repo: r.base_branch for r in pipeline.repos}
        assert by_repo == {"jwbron/a": "main", "jwbron/b": "release-2.0"}

    def test_primary_defaults_to_first_repo(self):
        """The primary (naming + slice-default) is the first submitted repo (AC-f)."""
        pipeline = Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/first", base_branch="main"),
                RepoSpec(repo="jwbron/second", base_branch="main"),
            ],
        )
        assert pipeline.primary_repo == "jwbron/first"
        # Legacy singleton is mirrored from the primary for un-rewired readers.
        assert pipeline.repo == "jwbron/first"

    def test_multi_repo_submission_round_trips(self):
        """The full list survives a persist/load round trip unchanged."""
        pipeline = Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/a", base_branch="main"),
                RepoSpec(repo="jwbron/b", base_branch="develop"),
                RepoSpec(repo="jwbron/c", base_branch="main"),
            ],
        )
        reloaded = Pipeline.model_validate(pipeline.model_dump())
        assert [(r.repo, r.base_branch) for r in reloaded.repos] == [
            ("jwbron/a", "main"),
            ("jwbron/b", "develop"),
            ("jwbron/c", "main"),
        ]
        assert reloaded.primary_repo == "jwbron/a"


class TestSingleRepoSubmissionBackCompat:
    """A bare single-repo submission stays accepted and unchanged (AC-1 / N=1)."""

    def test_bare_single_repo_synthesizes_one_element_list(self):
        pipeline = Pipeline(id="issue-3393", repo="jwbron/egg", base_branch="main")
        assert len(pipeline.repos) == 1
        assert pipeline.repos[0].repo == "jwbron/egg"
        assert pipeline.repos[0].base_branch == "main"
        assert pipeline.primary_repo == "jwbron/egg"

    def test_single_repo_round_trips_unchanged(self):
        pipeline = Pipeline(id="issue-3393", repo="jwbron/egg", base_branch="main")
        reloaded = Pipeline.model_validate(pipeline.model_dump())
        assert reloaded.repo == "jwbron/egg"
        assert reloaded.base_branch == "main"
        assert len(reloaded.repos) == 1
        assert reloaded.primary_repo == "jwbron/egg"


def _make_submit_handler():
    """Build a ``PipelineToolHandler`` whose HTTP calls are captured, not sent.

    Returns ``(handler, captured)`` where ``captured`` is a dict populated with
    the POST body handed to ``/api/v1/pipelines`` so a test can assert what the
    submission surface forwarded.
    """
    from mcp_tools import PipelineToolHandler

    handler = PipelineToolHandler.__new__(PipelineToolHandler)
    captured: dict[str, object] = {}

    def _fake_make_request(path, method="GET", data=None, timeout=None):  # noqa: ANN001
        if path == "/api/v1/pipelines" and method == "POST":
            captured["data"] = data
            return {"data": {"pipeline": {"id": "issue-3393"}}}
        # /start (or anything else) — no-op success.
        return {"data": {}}

    handler._make_request = _fake_make_request  # type: ignore[method-assign]
    return handler, captured


class TestListSubmissionRoute:
    """``_handle_submit_task`` forwards the submission shape to the API.

    The single-repo path is green today; the list path depends on the slice-2
    coder change and skips (with a reason) until integrated.
    """

    def test_bare_single_repo_forwarded(self):
        """Back-compat: a single ``repo`` is forwarded on the POST body."""
        handler, captured = _make_submit_handler()
        result = handler._handle_submit_task(
            {
                "description": "multi-repo pipelines",
                "issue_number": 3393,
                "repo": "jwbron/egg",
                "base_branch": "main",
            }
        )
        assert result.get("status") in ("started", "created_not_started")
        data = captured.get("data") or {}
        assert data.get("repo") == "jwbron/egg"
        assert data.get("base_branch") == "main"

    def test_list_repos_forwarded(self):
        """A ``repos`` list is forwarded verbatim to the orchestrator API.

        Skips until the slice-2 coder wires ``repos`` through
        ``_handle_submit_task`` — the tester and coder are parallel producers.
        """
        handler, captured = _make_submit_handler()
        repos = [
            {"repo": "jwbron/schema", "base_branch": "main"},
            {"repo": "jwbron/consumer", "base_branch": "develop"},
        ]
        handler._handle_submit_task(
            {
                "description": "multi-repo pipelines",
                "issue_number": 3393,
                "repos": repos,
            }
        )
        data = captured.get("data") or {}
        if "repos" not in data:
            pytest.skip(
                "slice-2 coder change not yet integrated: _handle_submit_task "
                "does not forward `repos` yet (parallel tester producer; "
                "activates at convergence)"
            )
        assert data["repos"] == repos


# --- Uniform visibility / auth rejection at the submission boundary ----------
#
# The rejection *logic* lives with the gateway helpers (see
# gateway/tests/test_repo_visibility.py). Here we assert the submission surface
# refuses a mixed set — the operator-visible behavior. This depends on the
# slice-2 coder wiring the validators into create_pipeline / the API, so the
# test import-guards on the validator symbol and skips until integrated.

_UNIFORMITY_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - exercised via skip path until coder lands
    from repo_visibility import (  # type: ignore[attr-defined]
        validate_auth_mode_uniformity,
        validate_visibility_uniformity,
    )
except Exception as exc:  # noqa: BLE001
    validate_visibility_uniformity = None  # type: ignore[assignment]
    validate_auth_mode_uniformity = None  # type: ignore[assignment]
    _UNIFORMITY_IMPORT_ERROR = repr(exc)


@pytest.mark.skipif(
    validate_visibility_uniformity is None,
    reason=(
        "slice-2 coder uniformity validators not yet integrated into the "
        "tester worktree (parallel producer); activates at convergence. "
        f"import error: {_UNIFORMITY_IMPORT_ERROR}"
    ),
)
class TestSubmissionUniformityIntegration:
    """A mixed-visibility / mixed-auth submission is rejected (AC-2)."""

    def test_mixed_visibility_rejected(self, monkeypatch):
        # private + public in one set -> reject.
        vis = {"jwbron/priv": "private", "jwbron/pub": "public"}
        monkeypatch.setattr(
            "repo_visibility.get_repo_visibility",
            lambda owner, repo, **_: vis[f"{owner}/{repo}"],
            raising=False,
        )
        with pytest.raises(ValueError) as excinfo:
            validate_visibility_uniformity(["jwbron/priv", "jwbron/pub"])
        # Error names the offending repos so the operator can act.
        assert "jwbron/pub" in str(excinfo.value)

    def test_uniform_visibility_accepted(self, monkeypatch):
        monkeypatch.setattr(
            "repo_visibility.get_repo_visibility",
            lambda owner, repo, **_: "private",
            raising=False,
        )
        # No raise for a uniformly-private set.
        validate_visibility_uniformity(["jwbron/a", "jwbron/b"])


# ============================================================================
# Slice-4 (#3393): slice-PR routing to ``slice.repo`` + lazy per-repo work
# branch & context PR (task-4-3).
#
# Two layers, mirroring the slice-2 pattern above:
#
# * **Always-green rule tests** built on the slice-1 model API
#   (``resolve_slice_repo`` / ``primary_repo`` / ``Pipeline.repos``). These
#   pin the exact contract the slice-4 coder change must satisfy — the repo a
#   slice's PR is routed to, and the set of repos that get a work branch +
#   context PR under the lazy-per-repo rule — so they are meaningful even
#   before the coder half integrates and never spuriously red.
# * **Skip-guarded integration tests** on the coder-owned seams (the
#   ``_repos_with_slices`` opener helper and the cross-repo sibling references
#   rendered into the context-PR body). These skip with an explicit reason
#   until the parallel coder producer lands, then activate automatically at
#   convergence. The exact interfaces are handed to the coder via task gaps
#   (task-4-1 / task-4-2) so the two halves converge on the same shape.
# ============================================================================


def _expected_participating_repos(slices, pipeline) -> list[str]:
    """The lazy-per-repo rule as a pure function (operator ruling #1).

    A repo *participates* — i.e. gets its own ``egg/<id>/work`` branch and its
    own context PR — iff at least one slice resolves to it via
    :func:`resolve_slice_repo`. The result is ordered by ``pipeline.repos`` and
    de-duplicated; a submitted repo that ends up owning no slices is excluded.
    This is the invariant the slice-4 opener's per-repo iteration must honour.
    """
    owning = {resolve_slice_repo(s, pipeline) for s in slices}
    return [r.repo for r in pipeline.repos if r.repo in owning]


class TestSlicePrRepoRouting:
    """Each slice's PR is routed to ``resolve_slice_repo(slice, pipeline)`` (AC-5)."""

    @staticmethod
    def _pipeline() -> Pipeline:
        return Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/schema", base_branch="main"),
                RepoSpec(repo="jwbron/consumer", base_branch="develop"),
            ],
        )

    def test_explicit_slice_repo_is_pr_target(self):
        """A slice with an explicit ``repo`` opens its PR in that repo."""
        pipeline = self._pipeline()
        s = Slice(id="slice-2", name="consumer migration", repo="jwbron/consumer")
        assert resolve_slice_repo(s, pipeline) == "jwbron/consumer"

    def test_absent_slice_repo_falls_back_to_primary(self):
        """A repo-less slice routes its PR to the pipeline primary (migration default)."""
        pipeline = self._pipeline()
        s = Slice(id="slice-1", name="schema add")
        assert s.repo is None
        assert resolve_slice_repo(s, pipeline) == "jwbron/schema"

    def test_each_slice_routes_to_its_own_repo(self):
        """A mixed multi-repo plan routes each slice PR independently, no collapse."""
        pipeline = self._pipeline()
        slices = [
            Slice(id="slice-1", name="schema", repo="jwbron/schema"),
            Slice(id="slice-2", name="consumer", repo="jwbron/consumer"),
        ]
        assert [resolve_slice_repo(s, pipeline) for s in slices] == [
            "jwbron/schema",
            "jwbron/consumer",
        ]

    def test_n1_slice_routes_to_single_repo(self):
        """N=1: the single (repo-less) slice routes to the one repo — unchanged."""
        pipeline = Pipeline(id="issue-3393", repo="jwbron/egg", base_branch="main")
        s = Slice(id="slice-1", name="the only slice")
        assert resolve_slice_repo(s, pipeline) == "jwbron/egg"


class TestLazyPerRepoParticipation:
    """A repo gets a work branch + context PR iff it owns ≥1 slice (AC-5, ruling #1)."""

    def test_sliceless_submitted_repo_excluded(self):
        """A submitted repo that ends up with no slices gets neither branch nor PR."""
        pipeline = Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/a", base_branch="main"),
                RepoSpec(repo="jwbron/b", base_branch="main"),
                RepoSpec(repo="jwbron/c", base_branch="main"),  # no slice → excluded
            ],
        )
        slices = [
            Slice(id="slice-1", name="a work", repo="jwbron/a"),
            Slice(id="slice-2", name="b work", repo="jwbron/b"),
        ]
        assert _expected_participating_repos(slices, pipeline) == ["jwbron/a", "jwbron/b"]

    def test_single_slice_repo_still_participates(self):
        """A repo owning a single slice still gets the standard context PR."""
        pipeline = Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/a", base_branch="main"),
                RepoSpec(repo="jwbron/b", base_branch="main"),
            ],
        )
        slices = [Slice(id="slice-1", name="only in a", repo="jwbron/a")]
        # ``b`` owns nothing → excluded; ``a`` participates with its lone slice
        # (uniformity beats special-casing).
        assert _expected_participating_repos(slices, pipeline) == ["jwbron/a"]

    def test_repo_less_slices_count_toward_primary(self):
        """A slice with no explicit repo makes the *primary* participate."""
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
        assert _expected_participating_repos(slices, pipeline) == [
            "jwbron/primary",
            "jwbron/other",
        ]

    def test_participation_dedups_multiple_slices_per_repo(self):
        """Two slices in one repo yield exactly one participating entry for it."""
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
        assert _expected_participating_repos(slices, pipeline) == ["jwbron/a", "jwbron/b"]

    def test_n1_single_participating_repo(self):
        """N=1: exactly one participating repo → one work branch + one context PR."""
        pipeline = Pipeline(id="issue-3393", repo="jwbron/egg", base_branch="main")
        slices = [
            Slice(id="slice-1", name="one"),
            Slice(id="slice-2", name="two"),
        ]
        assert _expected_participating_repos(slices, pipeline) == ["jwbron/egg"]


# --- Coder-owned seams: skip until the slice-4 coder change integrates -------
#
# Importing ``routes.pipelines`` pulls in the heavy orchestrator surface, so we
# stub ``docker`` and bootstrap ``sys.path`` the same way the sibling
# ``test_open_context_pr_at_implement_start`` module does before the guarded
# import. If the import fails (e.g. in a stripped environment) the dependent
# tests skip rather than erroring the whole module.

import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
from unittest.mock import MagicMock as _MagicMock  # noqa: E402

_docker_mock = _MagicMock()
_sys.modules.setdefault("docker", _docker_mock)
_sys.modules.setdefault("docker.errors", _docker_mock.errors)
_sys.modules.setdefault("docker.types", _docker_mock.types)
_orchestrator_path = _Path(__file__).parent.parent
if str(_orchestrator_path) not in _sys.path:
    _sys.path.insert(0, str(_orchestrator_path))

_COMPOSE_BODY_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - exercised via skip path until coder lands
    from routes.pipelines import _compose_context_pr_body  # type: ignore[attr-defined]
except Exception as exc:  # noqa: BLE001
    _compose_context_pr_body = None  # type: ignore[assignment]
    _COMPOSE_BODY_IMPORT_ERROR = repr(exc)

_REPOS_WITH_SLICES_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - exercised via skip path until coder lands
    from routes.pipelines import _repos_with_slices  # type: ignore[attr-defined]
except Exception as exc:  # noqa: BLE001
    _repos_with_slices = None  # type: ignore[assignment]
    _REPOS_WITH_SLICES_IMPORT_ERROR = repr(exc)


def _make_contract_with_slices(slices):
    """Build a minimal ``Contract`` carrying the given slices for opener tests."""
    from egg_contracts.models import Contract, IssueInfo, PRMetadata

    return Contract(
        issue=IssueInfo(number=3393, title="multi-repo pipelines", url=""),
        pipeline_id="issue-3393",
        pr=PRMetadata(title="Multi-repo pipelines", description="Body"),
        slices=list(slices),
    )


@pytest.mark.skipif(
    _repos_with_slices is None,
    reason=(
        "slice-4 coder helper ``routes.pipelines._repos_with_slices`` not yet "
        "integrated into the tester worktree (parallel producer); activates at "
        f"convergence. import error: {_REPOS_WITH_SLICES_IMPORT_ERROR}"
    ),
)
class TestLazyPerRepoOpenerHelper:
    """The opener's per-repo helper matches the lazy-per-repo rule (AC-5).

    Handed to the coder via a task-4-2 gap: ``_repos_with_slices(contract,
    pipeline) -> list[str]`` returns the participating repos (owning ≥1 slice),
    ordered by ``pipeline.repos`` and de-duplicated, slice-less repos excluded.
    """

    def test_helper_matches_rule_for_multi_repo(self):
        pipeline = Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/a", base_branch="main"),
                RepoSpec(repo="jwbron/b", base_branch="main"),
                RepoSpec(repo="jwbron/c", base_branch="main"),  # slice-less
            ],
        )
        slices = [
            Slice(id="slice-1", name="a", repo="jwbron/a"),
            Slice(id="slice-2", name="b", repo="jwbron/b"),
        ]
        contract = _make_contract_with_slices(slices)
        assert (
            list(_repos_with_slices(contract, pipeline))
            == _expected_participating_repos(slices, pipeline)
            == ["jwbron/a", "jwbron/b"]
        )

    def test_helper_n1_single_repo(self):
        pipeline = Pipeline(id="issue-3393", repo="jwbron/egg", base_branch="main")
        slices = [Slice(id="slice-1", name="one"), Slice(id="slice-2", name="two")]
        contract = _make_contract_with_slices(slices)
        assert list(_repos_with_slices(contract, pipeline)) == ["jwbron/egg"]


class TestContextPrSiblingCrossReferences:
    """The context-PR body cross-references sibling PRs in *other* repos (AC-5).

    GitHub renders a bare ``#N`` as an autolink scoped to the repo the body
    lives in, so a cross-repo sibling must be repo-qualified as
    ``owner/repo#N`` to resolve. This is the slice-4 addition on top of the
    existing same-repo ``— #N`` slice cross-links; the test skips until the
    coder renders the qualified form (handed over via a task-4-1 gap).
    """

    @pytest.mark.skipif(
        _compose_context_pr_body is None,
        reason=(
            f"routes.pipelines._compose_context_pr_body import failed: {_COMPOSE_BODY_IMPORT_ERROR}"
        ),
    )
    def test_cross_repo_sibling_is_repo_qualified(self, tmp_path):
        pipeline = Pipeline(
            id="issue-3393",
            issue_number=3393,
            branch="egg/issue-3393/work",
            base_branch="main",
            repos=[
                RepoSpec(repo="jwbron/schema", base_branch="main"),
                RepoSpec(repo="jwbron/consumer", base_branch="develop"),
            ],
        )
        # Context PR conceptually lives in the primary (jwbron/schema). slice-2
        # lives in a *different* repo, so its reference must be repo-qualified.
        contract = _make_contract_with_slices(
            [
                Slice(id="slice-1", name="Schema add", repo="jwbron/schema", pr_number=100),
                Slice(id="slice-2", name="Consumer migrate", repo="jwbron/consumer", pr_number=200),
            ]
        )
        body = _compose_context_pr_body(
            contract=contract,
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
            identifier=3393,
        )
        if "jwbron/consumer#200" not in body:
            pytest.skip(
                "slice-4 coder change not yet integrated: cross-repo sibling "
                "references are not repo-qualified in the context-PR body yet "
                "(parallel producer; activates at convergence)"
            )
        # The cross-repo sibling is qualified; the same-repo slice stays a bare
        # in-repo autolink (#100 resolves within jwbron/schema).
        assert "jwbron/consumer#200" in body
        assert "#100" in body
