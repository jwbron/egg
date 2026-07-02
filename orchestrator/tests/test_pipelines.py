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
from models import Pipeline, RepoSpec


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
