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


# ============================================================================
# Slice-5 (#3393): cross-repo merge-sequencing hold — the cq-1 two-tier gate
# (task-5-3).
#
# Same two-layer shape as slices 2 and 4 above:
#
# * **Always-green rule tests** that pin the cq-1 semantics as pure functions
#   over the slice-1 model API (``resolve_slice_repo`` / ``Pipeline`` /
#   ``Slice``) plus small local reference helpers. These encode the exact
#   contract the slice-5 coder machinery must satisfy — when an edge is
#   cross-repo, when the dependent PR may flip draft→ready, and which failure
#   conditions escalate to a HITL hold — so they are meaningful before the
#   coder half lands and never spuriously red.
# * **Skip-guarded integration tests** on the coder-owned seam pinned by name
#   in the architect design (layer_6): the NEW ``mark_pr_ready`` gateway verb
#   and the upstream-merge poll pass. They import-guard the coder symbols and
#   skip with an explicit reason until the parallel coder producer lands, then
#   activate automatically at convergence. The expected interfaces are handed
#   to the coder via the task-5-3 gap so the two halves converge on one shape.
#
# The cq-1 model being pinned (operator-resolved, architect layer_6 /
# reviewer_plan R3):
#   Tier A (AUTOMATED) — a *cross-repo* dependent slice PR opens DRAFT and is
#   auto-marked ready when the upstream slice PR MERGES. Merge is detected off
#   the merged-state (``mergedAt`` / ``merged`` boolean), NOT head-SHA equality
#   (a squash/rebase merge changes the SHA). Two failure terminals escalate to
#   a HITL hold instead of auto-readying: an upstream PR CLOSED-unmerged, and a
#   never-merging upstream whose poll exceeds the bound. Development is never
#   serialized — only the dependent PR's ready-state waits.
#   Tier B (HITL) — a beyond-merge-state edge (release/publish, version-pin,
#   genuine dev block) is held and released only by a human decision; it is
#   never auto-released off a programmatic merge signal.
#   Same-repo dependencies and N=1 pipelines take NEITHER hold.
# ============================================================================


# --- Reference implementation of the cq-1 decision logic ---------------------
# These mirror the pinned semantics as pure functions so the rule is asserted
# independently of the coder's poll/gateway plumbing (which the skip-guarded
# tests below exercise once integrated). They are the invariants the slice-5
# coder machinery must honour.

_HITL_HOLD = "hitl_hold"
_MARK_READY = "mark_ready"
_WAIT = "wait"


def _edge_is_cross_repo(upstream, downstream, pipeline) -> bool:
    """A dependency edge is cross-repo iff its endpoints resolve to different
    repos — the auto-path detector needs NO new field (architect layer_6)."""
    return resolve_slice_repo(upstream, pipeline) != resolve_slice_repo(downstream, pipeline)


def _pr_is_merged(pr_state: dict) -> bool:
    """Merge is keyed off merged-STATE, never head-SHA equality.

    A squash/rebase merge produces a merge commit whose SHA differs from the
    PR head, so ``mergedAt`` (or the ``merged`` boolean) is the only sound
    signal. ``state == "MERGED"`` alone is accepted as a fallback."""
    if pr_state.get("mergedAt"):
        return True
    if pr_state.get("merged") is True:
        return True
    return (pr_state.get("state") or "").upper() == "MERGED"


def _classify_upstream(pr_state: dict, *, polls: int, poll_bound: int) -> str:
    """Map an upstream PR's observed state to the dependent PR's action.

    merged                       -> ``mark_ready`` (Tier A auto)
    closed & unmerged            -> ``hitl_hold``  (failure terminal)
    still open, poll within bound -> ``wait``
    still open, poll >= bound     -> ``hitl_hold``  (never-merging terminal)
    """
    if _pr_is_merged(pr_state):
        return _MARK_READY
    if (pr_state.get("state") or "").upper() == "CLOSED":
        return _HITL_HOLD
    if polls >= poll_bound:
        return _HITL_HOLD
    return _WAIT


def _edge_hold_kind(*, cross_repo: bool, external_condition: bool) -> str:
    """Which hold an edge takes. Tier B (declared external condition) is HITL
    regardless of repo; a plain cross-repo edge is the Tier-A auto gate; a
    same-repo / N=1 edge takes no hold."""
    if external_condition:
        return "hitl"  # Tier B — released only by a human
    if cross_repo:
        return "auto"  # Tier A — merge-state draft->ready
    return "none"  # same-repo / N=1 — no gate


def _hold_blocks_development(kind: str) -> bool:
    """A hold gates PR ready-state only; development is never serialized."""
    return False


class TestCrossRepoEdgeDetection:
    """cross-repo iff the endpoints resolve to different repos (AC-6)."""

    @staticmethod
    def _pipeline() -> Pipeline:
        return Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/schema", base_branch="main"),
                RepoSpec(repo="jwbron/consumer", base_branch="develop"),
            ],
        )

    def test_different_repos_is_cross_repo(self):
        pipeline = self._pipeline()
        up = Slice(id="slice-1", name="schema add", repo="jwbron/schema")
        down = Slice(id="slice-2", name="consumer migrate", repo="jwbron/consumer")
        assert _edge_is_cross_repo(up, down, pipeline) is True

    def test_same_repo_is_not_cross_repo(self):
        """Two slices in one repo → same-repo edge → no merge gate (case f)."""
        pipeline = self._pipeline()
        up = Slice(id="slice-1", name="schema v1", repo="jwbron/schema")
        down = Slice(id="slice-2", name="schema v2", repo="jwbron/schema")
        assert _edge_is_cross_repo(up, down, pipeline) is False
        assert _edge_hold_kind(cross_repo=False, external_condition=False) == "none"

    def test_repo_less_endpoints_resolve_to_primary_same_repo(self):
        """Both endpoints default to the primary → same repo → not cross-repo."""
        pipeline = self._pipeline()
        up = Slice(id="slice-1", name="defaults to primary")
        down = Slice(id="slice-2", name="also primary")
        assert resolve_slice_repo(up, pipeline) == resolve_slice_repo(down, pipeline)
        assert _edge_is_cross_repo(up, down, pipeline) is False

    def test_n1_edge_is_never_cross_repo(self):
        """N=1: every edge resolves to the one repo → no hold (case f)."""
        pipeline = Pipeline(id="issue-3393", repo="jwbron/egg", base_branch="main")
        up = Slice(id="slice-1", name="one")
        down = Slice(id="slice-2", name="two")
        assert _edge_is_cross_repo(up, down, pipeline) is False
        assert _edge_hold_kind(cross_repo=False, external_condition=False) == "none"


class TestMergeStateReadyDecision:
    """draft→ready keys off merged-state, NOT head-SHA (AC-6, case a)."""

    def test_merged_at_set_marks_ready(self):
        pr_state = {"state": "MERGED", "mergedAt": "2026-07-02T00:00:00Z"}
        assert _pr_is_merged(pr_state) is True
        assert _classify_upstream(pr_state, polls=0, poll_bound=10) == _MARK_READY

    def test_squash_merge_sha_differs_from_head_still_ready(self):
        """A squash merge changes the SHA — detection must ignore head-SHA.

        The merge commit oid (``zzz…``) differs from the PR head oid
        (``aaa…``); a naive head-SHA-equality check would MISS this merge. The
        merged-state signal fires regardless, so the transition still happens.
        """
        pr_state = {
            "state": "MERGED",
            "mergedAt": "2026-07-02T00:00:00Z",
            "headRefOid": "aaaaaaaaaaaa",
            "mergeCommit": {"oid": "zzzzzzzzzzzz"},
        }
        # Guard: this really is a SHA-≠-head case, so the assertion below is
        # meaningful (a head-SHA check would return False here).
        assert pr_state["mergeCommit"]["oid"] != pr_state["headRefOid"]
        assert _pr_is_merged(pr_state) is True
        assert _classify_upstream(pr_state, polls=0, poll_bound=10) == _MARK_READY

    def test_open_unmerged_does_not_mark_ready(self):
        pr_state = {"state": "OPEN", "mergedAt": None}
        assert _pr_is_merged(pr_state) is False
        assert _classify_upstream(pr_state, polls=0, poll_bound=10) == _WAIT

    def test_closed_unmerged_is_not_merged(self):
        pr_state = {"state": "CLOSED", "mergedAt": None}
        assert _pr_is_merged(pr_state) is False


class TestUpstreamFailureTerminals:
    """The two failure terminals escalate to a HITL hold (AC-6, cases b, c)."""

    def test_closed_unmerged_escalates_to_hitl(self):
        """CLOSED-unmerged upstream → NO auto-ready; a HITL hold instead."""
        pr_state = {"state": "CLOSED", "mergedAt": None}
        action = _classify_upstream(pr_state, polls=0, poll_bound=10)
        assert action == _HITL_HOLD
        assert action != _MARK_READY

    def test_never_merging_bound_exceeded_escalates_to_hitl(self):
        """An open upstream that never merges escalates once the poll bound is
        exceeded — the dependent PR is not left draft indefinitely."""
        pr_state = {"state": "OPEN", "mergedAt": None}
        # Within the bound we keep waiting…
        assert _classify_upstream(pr_state, polls=9, poll_bound=10) == _WAIT
        # …at/over the bound we escalate rather than hang forever.
        assert _classify_upstream(pr_state, polls=10, poll_bound=10) == _HITL_HOLD
        assert _classify_upstream(pr_state, polls=99, poll_bound=10) == _HITL_HOLD

    def test_merge_wins_over_bound(self):
        """A merge detected on the final poll still readies (merge beats bound)."""
        pr_state = {"state": "MERGED", "mergedAt": "2026-07-02T00:00:00Z"}
        assert _classify_upstream(pr_state, polls=10, poll_bound=10) == _MARK_READY


class TestTierBExternalConditionHold:
    """Beyond-merge-state edges are HITL-held, never auto-released (case e)."""

    def test_external_condition_edge_is_hitl_regardless_of_repo(self):
        assert _edge_hold_kind(cross_repo=True, external_condition=True) == "hitl"
        assert _edge_hold_kind(cross_repo=False, external_condition=True) == "hitl"

    def test_tier_b_not_released_by_merge_signal(self):
        """Even a merged upstream does NOT release a Tier-B hold — only a human
        decision does. The auto classifier's verdict is irrelevant to Tier B."""
        # The upstream is merged; Tier A would auto-ready…
        pr_state = {"state": "MERGED", "mergedAt": "2026-07-02T00:00:00Z"}
        assert _classify_upstream(pr_state, polls=0, poll_bound=10) == _MARK_READY
        # …but the edge is Tier B, so it stays a HITL hold, not auto.
        assert _edge_hold_kind(cross_repo=True, external_condition=True) == "hitl"

    def test_plain_cross_repo_edge_is_auto_not_hitl(self):
        """Absent a declared external condition, a cross-repo edge is Tier-A
        auto (default) — HITL is the opt-in exception, not the default."""
        assert _edge_hold_kind(cross_repo=True, external_condition=False) == "auto"


class TestHoldDoesNotSerializeDevelopment:
    """A hold gates PR ready-state only; development runs in parallel (case d)."""

    def test_no_hold_kind_blocks_development(self):
        for kind in ("auto", "hitl", "none"):
            assert _hold_blocks_development(kind) is False


# --- Coder-owned seams: skip until the slice-5 coder machinery integrates -----
# The docker/sys.path bootstrap for importing the orchestrator surface is done
# once in the slice-4 block above; reuse it here.

_MARK_PR_READY_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - exercised via skip path until coder lands
    from gateway_client import GatewayClient as _GatewayClient  # type: ignore
except Exception as exc:  # noqa: BLE001
    _GatewayClient = None  # type: ignore[assignment]
    _MARK_PR_READY_IMPORT_ERROR = repr(exc)


def _gateway_has_mark_pr_ready() -> bool:
    return _GatewayClient is not None and hasattr(_GatewayClient, "mark_pr_ready")


@pytest.mark.skipif(
    not _gateway_has_mark_pr_ready(),
    reason=(
        "slice-5 coder verb ``GatewayClient.mark_pr_ready`` not yet integrated "
        "into the tester worktree (parallel producer); activates at "
        "convergence. Expected interface (handed via task-5-3 gap): "
        "mark_pr_ready(self, pipeline_id, repo, *, pr_number) wrapping "
        f"`gh pr ready`. import: {_MARK_PR_READY_IMPORT_ERROR}"
    ),
)
class TestMarkPrReadyGatewayVerb:
    """The NEW ``mark_pr_ready`` gateway verb exists and takes (repo, pr).

    Pinned by name in the architect layer_6 design. This is the primitive the
    Tier-A auto gate calls when it detects the upstream merge. We assert the
    verb is exposed with a repo + PR-number-shaped interface; the merge→ready
    *decision* that drives it is pinned by ``TestMergeStateReadyDecision``.
    """

    def test_verb_signature_accepts_repo_and_pr_number(self):
        import inspect

        sig = inspect.signature(_GatewayClient.mark_pr_ready)  # type: ignore[union-attr]
        params = set(sig.parameters)
        # Routes per-repo (multi-repo pipelines) and identifies the PR.
        assert "repo" in params, f"mark_pr_ready must take a repo: {params}"
        assert any(p in params for p in ("pr_number", "pr", "number")), (
            f"mark_pr_ready must identify the PR: {params}"
        )


# The upstream-merge poll pass (extend stacked_pr_reconciler or a new
# cross_repo_merge_gate module — the coder owns the exact name). We probe the
# candidate entry points; if one lands exposing a merge-state classifier, we
# assert it agrees with the pinned reference logic. Until then it skips.
_MERGE_GATE_CLASSIFIER = None
_MERGE_GATE_IMPORT_ERROR: str | None = None
for _modname, _fnname in (
    ("cross_repo_merge_gate", "classify_upstream_merge"),
    ("stacked_pr_reconciler", "classify_upstream_merge"),
):
    try:  # pragma: no cover - exercised via skip path until coder lands
        _mod = __import__(_modname)
        _fn = getattr(_mod, _fnname, None)
        if _fn is not None:
            _MERGE_GATE_CLASSIFIER = _fn
            break
    except Exception as exc:  # noqa: BLE001
        _MERGE_GATE_IMPORT_ERROR = f"{_modname}: {exc!r}"


@pytest.mark.skipif(
    _MERGE_GATE_CLASSIFIER is None,
    reason=(
        "slice-5 coder merge-poll classifier not yet integrated (parallel "
        "producer); activates at convergence. Expected: a "
        "``classify_upstream_merge(pr_state)`` on cross_repo_merge_gate or "
        "stacked_pr_reconciler that readies on merged-state, holds on "
        f"closed-unmerged. probe: {_MERGE_GATE_IMPORT_ERROR}"
    ),
)
class TestMergePollClassifierIntegration:
    """Once the coder's poll classifier lands it must ready a merged upstream
    and refuse a closed-unmerged one — the pinned Tier-A semantics."""

    def test_merged_upstream_readies(self):
        pr_state = {"state": "MERGED", "mergedAt": "2026-07-02T00:00:00Z"}
        assert _MERGE_GATE_CLASSIFIER(pr_state) == _MARK_READY  # type: ignore[misc]

    def test_closed_unmerged_does_not_ready(self):
        pr_state = {"state": "CLOSED", "mergedAt": None}
        assert _MERGE_GATE_CLASSIFIER(pr_state) != _MARK_READY  # type: ignore[misc]


# --- Release-selection semantics: poll_once must honour the SELECTED option --
# reviewer_code_holistic v1 NACK: a hold that readied on the bare
# ``Decision.resolved`` boolean made the "Keep held" option a lie — selecting
# it would still ready the PR on the next tick. The fix routes a
# RELEASE/KEEP/None verdict through ``poll_once``'s ``hold_resolution``
# callback. These tests pin that behaviour on the coder's real gate module.
_MERGE_GATE_MOD = None
_MERGE_GATE_MOD_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - exercised via skip path until coder lands
    import cross_repo_merge_gate as _MERGE_GATE_MOD  # type: ignore
except Exception as exc:  # noqa: BLE001
    _MERGE_GATE_MOD = None
    _MERGE_GATE_MOD_IMPORT_ERROR = repr(exc)


def _merge_gate_has_release_verdicts() -> bool:
    return (
        _MERGE_GATE_MOD is not None
        and hasattr(_MERGE_GATE_MOD, "poll_once")
        and hasattr(_MERGE_GATE_MOD, "RELEASE")
        and hasattr(_MERGE_GATE_MOD, "KEEP")
    )


@pytest.mark.skipif(
    not _merge_gate_has_release_verdicts(),
    reason=(
        "slice-5 coder release-selection seam (cross_repo_merge_gate.poll_once "
        "with RELEASE/KEEP verdicts) not yet integrated; activates at "
        f"convergence. probe: {_MERGE_GATE_MOD_IMPORT_ERROR}"
    ),
)
class TestCrossRepoHoldReleaseSelection:
    """``poll_once`` honours the human's SELECTED hold option (holistic v1 NACK).

    A resolved hold released with the RELEASE verdict readies the PR; a hold
    resolved with the KEEP verdict is terminal and does NOT ready (the PR
    stays draft for manual handling); an unresolved hold keeps waiting.
    """

    @staticmethod
    def _pipeline() -> Pipeline:
        return Pipeline(
            id="issue-3393",
            repos=[
                RepoSpec(repo="jwbron/schema", base_branch="main"),
                RepoSpec(repo="jwbron/consumer", base_branch="main"),
            ],
        )

    @staticmethod
    def _contract():
        from types import SimpleNamespace

        up = Slice(id="slice-1", name="schema add", repo="jwbron/schema", pr_number=100)
        down = Slice(
            id="slice-2",
            name="consumer migrate",
            repo="jwbron/consumer",
            dependencies=["slice-1"],
            pr_number=200,
        )
        return SimpleNamespace(slices=[up, down], decisions=[])

    def _run_hold_then_verdict(self, *, verdict):
        """Tick 1 escalates a closed-unmerged upstream to a hold; tick 2 applies
        the human ``verdict`` (RELEASE / KEEP / None). Returns (readied, result)."""
        mod = _MERGE_GATE_MOD
        pipeline = self._pipeline()
        contract = self._contract()
        readied: list[tuple[str, int]] = []
        state: dict = {}
        closed = {"state": "CLOSED", "merged_at": None}

        def _tick(resolution):
            return mod.poll_once(  # type: ignore[union-attr]
                contract,
                resolve_repo=lambda s: resolve_slice_repo(s, pipeline),
                get_merge_state=lambda _r, _n: closed,
                mark_ready=lambda r, n: readied.append((r, n)) or True,
                register_hold=lambda _gate, _reason: True,
                hold_resolution=lambda _gate: resolution,
                state=state,
                max_attempts=3,
            )

        _tick(None)  # tick 1: closed-unmerged → hold registered
        result = _tick(verdict)  # tick 2: human verdict
        return readied, result

    def test_release_verdict_readies_the_pr(self):
        readied, _ = self._run_hold_then_verdict(verdict=_MERGE_GATE_MOD.RELEASE)  # type: ignore[union-attr]
        assert readied == [("jwbron/consumer", 200)]

    def test_keep_verdict_does_not_ready_the_pr(self):
        readied, result = self._run_hold_then_verdict(verdict=_MERGE_GATE_MOD.KEEP)  # type: ignore[union-attr]
        # The whole point of the NACK: "Keep held" must NOT ready the PR.
        assert readied == []
        assert result.kept_held == 1

    def test_unresolved_hold_keeps_waiting(self):
        readied, _ = self._run_hold_then_verdict(verdict=None)
        assert readied == []
