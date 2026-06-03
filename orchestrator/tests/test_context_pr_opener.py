"""Tests for ``_open_context_pr_at_implement_start`` (#2777, cq-4 / TASK-1-2).

Replaces the legacy ``test_context_pr.py`` (deleted in slice-3 TASK-3-11)
which exercised ``_open_context_pr_for_pipeline`` — the pre-#2777 opener
that created a dedicated ``egg/<id>/context`` branch, copied refine/plan
artifacts onto it, and pushed/opened a PR. The new opener replaces that
entire scaffold with a single up-front PR for ``egg/<id>/work → main``,
opened at the plan→implement boundary and stacked on by slice PRs.

This file pins the three paths task-3-8 calls out:

1. **Happy path** — pipeline has remote config + no existing PR matches;
   the opener calls ``GatewayClient.create_pr`` with the
   ``contract.pr.title`` / ``contract.pr.description`` payload and the
   pipeline's ``branch`` (head) + ``base_branch`` (base), persists the
   parsed PR number via ``_persist_context_pr_number``, and returns it.
2. **Idempotent path** — ``lookup_open_pr`` returns the open PR number
   for ``head=branch base=base_branch``; the opener returns it
   WITHOUT invoking ``create_pr`` (negative-assert), and
   ``_persist_context_pr_number`` IS still called (the contract may have
   lost the field mid-run; idempotency must repair).
3. **Hard-required-raises paths** — the soft-fail ``return None`` swallow
   that the legacy wrapper used is gone. Each of the cq-4 hard-required
   failure modes raises a typed :class:`ContextPrCreationError` with a
   structured ``reason`` value. The local-mode (no repo, no base_branch)
   short-circuit is the ONLY ``return None`` path; the partial-config
   asymmetric case (only one of ``repo`` / ``base_branch`` set) raises.

The naming follows the orchestrator-tests feature-split convention
(``test_<feature>_<topic>.py``) — there is no monolithic
``test_pipelines.py`` to extend.
"""

from __future__ import annotations

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


from egg_contracts.models import Contract, IssueInfo, PRMetadata  # noqa: E402
from models import Pipeline, PipelinePhase, PipelineStatus  # noqa: E402
from routes.pipelines import (  # noqa: E402
    ContextPrCreationError,
    ContextPrCreationReason,
    _open_context_pr_at_implement_start,
)


def _make_pipeline(
    *,
    pipeline_id: str = "issue-2777",
    repo: str = "owner/repo",
    branch: str = "egg/issue-2777/work",
    base_branch: str = "main",
    issue_number: int | None = 2777,
) -> Pipeline:
    """Build a Pipeline with the remote-mode fields set by default.

    Tests override ``repo`` / ``base_branch`` to exercise the local-mode
    short-circuit and the partial-config asymmetric raise.
    """
    return Pipeline(
        id=pipeline_id,
        issue_number=issue_number,
        repo=repo,
        branch=branch,
        base_branch=base_branch,
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )


def _make_contract(
    *,
    title: str = "Slice-3 cleanup: drop umbrella + collapse context-PR scaffold",
    description: str = "Cleanup-only slice; no behavior change to runtime topology.",
    pipeline_id: str = "issue-2777",
    issue_number: int = 2777,
) -> Contract:
    """Build a Contract whose ``pr`` field has the canonical metadata.

    Tests that need to drop the metadata (the ``missing_pr_metadata``
    raise) mutate ``contract.pr`` after this constructor returns.
    """
    return Contract(
        schemaVersion="1.2",
        pipeline_id=pipeline_id,
        issue=IssueInfo(
            number=issue_number,
            title="placeholder",
            url=f"https://example.test/{issue_number}",
        ),
        pr=PRMetadata(
            title=title,
            description=description,
        ),
    )


# ---------------------------------------------------------------------------
# Happy path — opens a new PR via the gateway and persists the number
# ---------------------------------------------------------------------------


class TestOpenContextPrHappyPath:
    """The opener creates a new PR when ``lookup_open_pr`` does not
    surface a head/base match, persists the parsed PR number, and returns it."""

    def test_opens_new_pr_and_persists_number(self, tmp_path):
        pipeline = _make_pipeline()
        contract = _make_contract()

        # Stub the spawner + gateway: no existing PR (lookup miss),
        # create_pr returns a parseable URL.
        spawner = MagicMock()
        spawner.gateway.lookup_open_pr.return_value = None
        spawner.gateway.create_pr.return_value = f"https://github.com/{pipeline.repo}/pull/4242"

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(MagicMock(repo_path=tmp_path), pipeline),
            ),
            patch(
                "routes.resolve_worktree_path",
                return_value=tmp_path,
            ),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch(
                "routes.pipelines._compute_gateway_mode",
                return_value=("public", "public"),
            ),
            patch(
                "egg_contracts.loader.load_contract",
                return_value=contract,
            ),
            patch("routes.pipelines._persist_context_pr_number") as mock_persist,
        ):
            result = _open_context_pr_at_implement_start(pipeline.id)

        assert result == 4242
        # ``create_pr`` MUST be invoked with the canonical
        # title/description/head/base on the happy path.
        spawner.gateway.create_pr.assert_called_once()
        kwargs = spawner.gateway.create_pr.call_args.kwargs
        assert contract.pr is not None  # narrow for the asserts below
        assert kwargs["title"] == contract.pr.title
        assert kwargs["body"] == contract.pr.description
        assert kwargs["head"] == pipeline.branch
        assert kwargs["base"] == pipeline.base_branch
        # Number must be persisted so the slice loop sees it on the next
        # contract read.
        mock_persist.assert_called_once()
        persist_args = mock_persist.call_args
        # First positional is pipeline_id; second positional is the
        # PR number.
        assert persist_args.args[0] == pipeline.id
        assert persist_args.args[1] == 4242

    def test_honors_non_main_base_branch(self, tmp_path):
        """``base_branch`` is passed through end-to-end (#2777 cq-4 keeps
        the legacy honor-non-main behavior)."""
        pipeline = _make_pipeline(base_branch="release/v2")
        contract = _make_contract()
        spawner = MagicMock()
        spawner.gateway.lookup_open_pr.return_value = None
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/777"

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(MagicMock(repo_path=tmp_path), pipeline),
            ),
            patch(
                "routes.resolve_worktree_path",
                return_value=tmp_path,
            ),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch(
                "routes.pipelines._compute_gateway_mode",
                return_value=("public", "public"),
            ),
            patch(
                "egg_contracts.loader.load_contract",
                return_value=contract,
            ),
            patch("routes.pipelines._persist_context_pr_number"),
        ):
            _open_context_pr_at_implement_start(pipeline.id)

        assert spawner.gateway.create_pr.call_args.kwargs["base"] == "release/v2"
        # ``lookup_open_pr`` is called for the idempotency pre-flight with
        # the pipeline's head + base so the gateway's server-side
        # ``--head --base`` filter discriminates correctly.
        spawner.gateway.lookup_open_pr.assert_called_once()
        lookup_kwargs = spawner.gateway.lookup_open_pr.call_args.kwargs
        assert lookup_kwargs["head"] == pipeline.branch
        assert lookup_kwargs["base"] == "release/v2"


# ---------------------------------------------------------------------------
# Idempotent path — existing PR is returned without a create_pr call
# ---------------------------------------------------------------------------


class TestOpenContextPrIdempotent:
    """When ``lookup_open_pr`` surfaces a PR matching the pipeline's
    head/base, the opener returns its number without invoking
    ``create_pr`` — but still persists the number (resume-from-orphan
    case)."""

    def test_idempotent_hit_returns_existing_pr_no_create_call(self, tmp_path):
        pipeline = _make_pipeline()
        contract = _make_contract()
        existing_pr_number = 9999

        spawner = MagicMock()
        spawner.gateway.lookup_open_pr.return_value = existing_pr_number

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(MagicMock(repo_path=tmp_path), pipeline),
            ),
            patch(
                "routes.resolve_worktree_path",
                return_value=tmp_path,
            ),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch(
                "routes.pipelines._compute_gateway_mode",
                return_value=("public", "public"),
            ),
            patch(
                "egg_contracts.loader.load_contract",
                return_value=contract,
            ),
            patch("routes.pipelines._persist_context_pr_number") as mock_persist,
        ):
            result = _open_context_pr_at_implement_start(pipeline.id)

        assert result == existing_pr_number
        # The whole point of the idempotency pre-flight is to skip
        # ``create_pr`` when the open PR already exists.
        spawner.gateway.create_pr.assert_not_called()
        # Persistence still fires so a contract that lost the field
        # mid-run is repaired.
        mock_persist.assert_called_once()
        assert mock_persist.call_args.args[1] == existing_pr_number

    def test_lookup_forwards_head_and_base_then_create_on_miss(self, tmp_path):
        """Head/base discrimination now lives server-side in
        ``lookup_open_pr``'s ``gh pr list --head --base`` filter (a
        wrong-base PR can never come back as a hit). The opener's job is
        to forward the pipeline's exact head + base; on a miss (``None``)
        it falls through to ``create_pr``."""
        pipeline = _make_pipeline(branch="egg/issue-2777/work", base_branch="main")
        contract = _make_contract()

        spawner = MagicMock()
        spawner.gateway.lookup_open_pr.return_value = None
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/3"

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(MagicMock(repo_path=tmp_path), pipeline),
            ),
            patch(
                "routes.resolve_worktree_path",
                return_value=tmp_path,
            ),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch(
                "routes.pipelines._compute_gateway_mode",
                return_value=("public", "public"),
            ),
            patch(
                "egg_contracts.loader.load_contract",
                return_value=contract,
            ),
            patch("routes.pipelines._persist_context_pr_number"),
        ):
            result = _open_context_pr_at_implement_start(pipeline.id)

        # The opener forwarded the exact head + base for the server-side
        # filter to discriminate on.
        lookup_kwargs = spawner.gateway.lookup_open_pr.call_args.kwargs
        assert lookup_kwargs["head"] == "egg/issue-2777/work"
        assert lookup_kwargs["base"] == "main"
        # No hit — fell through to create_pr.
        spawner.gateway.create_pr.assert_called_once()
        assert result == 3


# ---------------------------------------------------------------------------
# Hard-required raises — every failure mode raises ContextPrCreationError
# ---------------------------------------------------------------------------


class TestOpenContextPrHardRequiredRaises:
    """The legacy soft-fail ``return None`` swallow path is gone. Every
    cq-4 failure mode raises a typed ``ContextPrCreationError`` with a
    structured ``reason`` value."""

    def test_local_mode_returns_none_without_raise(self, tmp_path):
        """The ONLY ``return None`` survivor — both ``repo`` and
        ``base_branch`` empty means local mode, no remote PR to open."""
        pipeline = _make_pipeline(repo="", base_branch="")
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(MagicMock(repo_path=tmp_path), pipeline),
            ),
        ):
            result = _open_context_pr_at_implement_start(pipeline.id)
        assert result is None

    @pytest.mark.parametrize(
        ("repo", "base_branch", "expected_reason"),
        [
            ("owner/repo", "", ContextPrCreationReason.MISSING_BASE_BRANCH.value),
            ("", "main", ContextPrCreationReason.MISSING_REPO.value),
        ],
    )
    def test_partial_remote_config_raises_typed_error(
        self, tmp_path, repo, base_branch, expected_reason
    ):
        """A pipeline with ``repo`` set but no ``base_branch`` (or vice
        versa) is a misconfiguration — the new opener raises rather than
        silently skipping (the legacy wrapper would silently
        ``return None`` and leave the slice stack stranded on /work).
        Reviewer-blocker-3 of slice-1 reviewer_code_holistic pinned
        this asymmetric-config raise."""
        pipeline = _make_pipeline(repo=repo, base_branch=base_branch)
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(MagicMock(repo_path=tmp_path), pipeline),
            ),
            pytest.raises(ContextPrCreationError) as exc_info,
        ):
            _open_context_pr_at_implement_start(pipeline.id)
        assert exc_info.value.reason == expected_reason

    def test_missing_branch_raises(self, tmp_path):
        """A remote pipeline must have ``branch`` set; an empty branch
        on the remote-config path is a structural error and the opener
        must raise — not silently skip — so the operator sees the bug."""
        pipeline = _make_pipeline()
        pipeline.branch = None
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(MagicMock(repo_path=tmp_path), pipeline),
            ),
            pytest.raises(ContextPrCreationError) as exc_info,
        ):
            _open_context_pr_at_implement_start(pipeline.id)
        assert exc_info.value.reason == ContextPrCreationReason.MISSING_BRANCH.value

    def test_missing_pr_metadata_raises(self, tmp_path):
        """``contract.pr.title`` empty after the local-mode / branch
        checks pass must raise ``missing_pr_metadata`` — the opener
        cannot fabricate a title."""
        pipeline = _make_pipeline()
        contract = _make_contract()
        contract.pr = None  # Drop the PR metadata entirely.

        spawner = MagicMock()
        spawner.gateway.lookup_open_pr.return_value = None

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(MagicMock(repo_path=tmp_path), pipeline),
            ),
            patch(
                "routes.resolve_worktree_path",
                return_value=tmp_path,
            ),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch(
                "routes.pipelines._compute_gateway_mode",
                return_value=("public", "public"),
            ),
            patch(
                "egg_contracts.loader.load_contract",
                return_value=contract,
            ),
            pytest.raises(ContextPrCreationError) as exc_info,
        ):
            _open_context_pr_at_implement_start(pipeline.id)
        assert exc_info.value.reason == ContextPrCreationReason.MISSING_PR_METADATA.value
        # ``create_pr`` MUST NOT have been called — the contract guard
        # short-circuits before the gateway request.
        spawner.gateway.create_pr.assert_not_called()

    def test_lookup_open_pr_failure_raises_lookup_failed(self, tmp_path):
        """If ``lookup_open_pr`` raises unexpectedly, the opener wraps it
        in a typed ``lookup_failed`` rather than letting a raw exception
        escape the cq-4 no-raw-exception contract. (The primitive itself
        soft-fails a transient gateway/parse error to ``None``, matching
        the slice path, so this backstop only fires on a programming
        error.)"""
        pipeline = _make_pipeline()
        contract = _make_contract()

        spawner = MagicMock()
        spawner.gateway.lookup_open_pr.side_effect = RuntimeError("gateway down")

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(MagicMock(repo_path=tmp_path), pipeline),
            ),
            patch(
                "routes.resolve_worktree_path",
                return_value=tmp_path,
            ),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch(
                "routes.pipelines._compute_gateway_mode",
                return_value=("public", "public"),
            ),
            patch(
                "egg_contracts.loader.load_contract",
                return_value=contract,
            ),
            pytest.raises(ContextPrCreationError) as exc_info,
        ):
            _open_context_pr_at_implement_start(pipeline.id)
        assert exc_info.value.reason == ContextPrCreationReason.LOOKUP_FAILED.value
        # No duplicate PR creation attempt.
        spawner.gateway.create_pr.assert_not_called()

    def test_create_pr_failure_raises_gateway_error(self, tmp_path):
        """If ``create_pr`` raises (e.g. transient gateway failure), the
        opener must raise ``gateway_error`` rather than silently
        ``return None`` — the slice stack would otherwise be stranded
        on /work without a context PR."""
        pipeline = _make_pipeline()
        contract = _make_contract()

        spawner = MagicMock()
        spawner.gateway.lookup_open_pr.return_value = None
        spawner.gateway.create_pr.side_effect = RuntimeError("HTTP 503")

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(MagicMock(repo_path=tmp_path), pipeline),
            ),
            patch(
                "routes.resolve_worktree_path",
                return_value=tmp_path,
            ),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch(
                "routes.pipelines._compute_gateway_mode",
                return_value=("public", "public"),
            ),
            patch(
                "egg_contracts.loader.load_contract",
                return_value=contract,
            ),
            pytest.raises(ContextPrCreationError) as exc_info,
        ):
            _open_context_pr_at_implement_start(pipeline.id)
        assert exc_info.value.reason == ContextPrCreationReason.GATEWAY_ERROR.value

    def test_create_pr_returns_no_url_raises_gateway_no_url(self, tmp_path):
        """A ``create_pr`` that returns ``None`` (gh succeeded but printed
        nothing) leaves the opener with no parseable PR number; raise so
        the operator sees the gateway misbehavior."""
        pipeline = _make_pipeline()
        contract = _make_contract()

        spawner = MagicMock()
        spawner.gateway.lookup_open_pr.return_value = None
        spawner.gateway.create_pr.return_value = None

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(MagicMock(repo_path=tmp_path), pipeline),
            ),
            patch(
                "routes.resolve_worktree_path",
                return_value=tmp_path,
            ),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch(
                "routes.pipelines._compute_gateway_mode",
                return_value=("public", "public"),
            ),
            patch(
                "egg_contracts.loader.load_contract",
                return_value=contract,
            ),
            pytest.raises(ContextPrCreationError) as exc_info,
        ):
            _open_context_pr_at_implement_start(pipeline.id)
        assert exc_info.value.reason == ContextPrCreationReason.GATEWAY_NO_URL.value

    @pytest.mark.parametrize(
        "bad_url",
        [
            "not-a-url",
            "https://github.com/owner/repo/pulled-files/42",
            "https://github.com/owner/repo/pull/abc",
        ],
    )
    def test_unparseable_create_pr_url_raises_gateway_bad_url(self, tmp_path, bad_url):
        """A URL the regex cannot lift a number out of (or whose suffix
        is not a valid PR path boundary) raises ``gateway_bad_url``. The
        trailing-boundary regex must not let a digit-suffixed slug like
        ``/pulled-files/12345`` smuggle a wrong number through —
        reviewer_concurrency non-blocking #2 on slice-1."""
        pipeline = _make_pipeline()
        contract = _make_contract()

        spawner = MagicMock()
        spawner.gateway.lookup_open_pr.return_value = None
        spawner.gateway.create_pr.return_value = bad_url

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(MagicMock(repo_path=tmp_path), pipeline),
            ),
            patch(
                "routes.resolve_worktree_path",
                return_value=tmp_path,
            ),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch(
                "routes.pipelines._compute_gateway_mode",
                return_value=("public", "public"),
            ),
            patch(
                "egg_contracts.loader.load_contract",
                return_value=contract,
            ),
            pytest.raises(ContextPrCreationError) as exc_info,
        ):
            _open_context_pr_at_implement_start(pipeline.id)
        assert exc_info.value.reason == ContextPrCreationReason.GATEWAY_BAD_URL.value
