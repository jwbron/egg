"""Tests for ``_open_context_pr_at_implement_start`` + ``_persist_context_pr_number`` (#2777).

Slice-1 of #2777 lands the hard-required, idempotent up-front context
PR opener that replaces the soft-fail
``_maybe_open_base_pr_for_plan_to_implement`` wrapper. The opener fires
ONCE at the plan→implement boundary from ``phases.py:advance_phase``;
the four runner-side backstops re-invoke it on resume / auto-advance
paths so a transient gateway failure cannot strand the slice stack on
``/work``.

This file pins:

* Idempotency hit re-persists ``context_pr_number`` (the resume-from-
  orphaned-pipeline path where the contract lost the field mid-run
  must recover, even though the PR is already on GitHub).
* Happy path persists a freshly-created PR number to the contract.
* Each ``ContextPrCreationReason`` (or a representative subset) surfaces
  as a typed ``ContextPrCreationError`` — there is NO soft-fail
  ``return None`` path for gateway / contract failures.
* ``_persist_context_pr_number`` raises the typed error when the contract
  has no ``PRMetadata`` block, and mutates ``contract.pr.context_pr_number``
  on the happy path.

The wider slice-3 (TASK-3-8) test surface adds the runner-side wiring
tests (HITL resume, slice-loop entry, etc.); this file covers the opener
in isolation so the reviewer's "no minimum-viable unit tests" feedback
is closed.
"""

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


from models import Pipeline, PipelinePhase, PipelineStatus  # noqa: E402
from routes.pipelines import (  # noqa: E402
    ContextPrCreationError,
    ContextPrCreationReason,
    _open_context_pr_at_implement_start,
    _persist_context_pr_number,
)

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _make_pipeline(
    *,
    repo: str = "owner/repo",
    base_branch: str | None = "main",
    branch: str = "egg/issue-2777/work",
) -> Pipeline:
    """Pipeline with the fields the opener reads (repo, base_branch, branch)."""
    return Pipeline(
        id="issue-2777",
        issue_number=2777,
        repo=repo,
        branch=branch,
        base_branch=base_branch,
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.PLAN,
    )


def _make_contract(*, with_pr_metadata: bool = True):
    """Build a Contract with (or without) a ``pr`` block."""
    from egg_contracts.models import Contract, IssueInfo, PRMetadata
    from egg_contracts.models import PipelinePhase as _PP

    pr = PRMetadata(title="Add feature X", description="Body") if with_pr_metadata else None
    return Contract(
        issue=IssueInfo(number=2777, title="t", url=""),
        pipeline_id="issue-2777",
        current_phase=_PP.PLAN,
        pr=pr,
    )


@pytest.fixture
def store(tmp_path):
    """Mock state store with ``repo_path`` pointing at ``tmp_path``."""
    s = MagicMock(name="store")
    s.repo_path = tmp_path
    return s


@pytest.fixture
def spawner_factory():
    """Factory that builds a MagicMock spawner whose gateway returns the supplied PR state."""

    def _build(
        *,
        lookup_open_pr_return=None,
        lookup_open_pr_side_effect=None,
        create_pr_return="https://github.com/owner/repo/pull/4242",
        create_pr_side_effect=None,
    ):
        spawner = MagicMock(name="spawner")
        gw = MagicMock(name="gateway")
        # Both PR-idempotency sites now share the control-plane
        # ``lookup_open_pr(head, base)`` primitive (#2934); it returns a
        # clean ``int | None`` rather than a list to filter client-side.
        if lookup_open_pr_side_effect is not None:
            gw.lookup_open_pr.side_effect = lookup_open_pr_side_effect
        else:
            gw.lookup_open_pr.return_value = lookup_open_pr_return
        if create_pr_side_effect is not None:
            gw.create_pr.side_effect = create_pr_side_effect
        else:
            gw.create_pr.return_value = create_pr_return
        spawner.gateway = gw
        return spawner

    return _build


# ----------------------------------------------------------------------
# _open_context_pr_at_implement_start
# ----------------------------------------------------------------------


class TestOpenContextPRAtImplementStartIdempotency:
    """Calling the opener twice for the same pipeline is safe — the
    second call sees the already-open PR via ``lookup_open_pr`` and
    re-persists the number without invoking ``create_pr``.
    """

    def test_idempotent_hit_re_persists_pr_number(
        self, tmp_path, monkeypatch, store, spawner_factory
    ):
        """When ``lookup_open_pr`` already returns our head→base PR
        number, the opener returns it, does NOT call ``create_pr``, AND
        still calls ``_persist_context_pr_number``
        (resume-from-orphaned-pipeline recovery path)."""
        pipeline = _make_pipeline()
        spawner = spawner_factory(lookup_open_pr_return=4242)
        contract = _make_contract()

        save_calls: list = []

        def _fake_save(c, _root):
            # Mirror the real save_contract side effect for the
            # idempotent re-persist: ``context_pr_number`` is written.
            save_calls.append(c.pr.context_pr_number)

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("routes.resolve_worktree_path", return_value=tmp_path),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract", side_effect=_fake_save),
            # The persist helper mirrors pr_url/pr_number onto the pipeline
            # record via a second state-store load/save; mock it so the
            # non-git tmp_path does not trip StateStore creation (#2777).
            patch("state_store.get_state_store", return_value=store),
        ):
            store.load_pipeline.return_value = MagicMock(repo="owner/repo")
            result = _open_context_pr_at_implement_start("issue-2777")

        assert result == 4242
        # The opener delegates head/base discrimination to the primitive,
        # so assert it forwarded the pipeline's work branch + base.
        spawner.gateway.lookup_open_pr.assert_called_once()
        lookup_kwargs = spawner.gateway.lookup_open_pr.call_args.kwargs
        assert lookup_kwargs["head"] == "egg/issue-2777/work"
        assert lookup_kwargs["base"] == "main"
        spawner.gateway.create_pr.assert_not_called()
        # Persistence DOES fire on the idempotent path so an orphaned
        # contract recovers ``context_pr_number``.
        assert save_calls == [4242]


class TestOpenContextPRAtImplementStartHappyPath:
    def test_creates_pr_and_persists_number(self, tmp_path, monkeypatch, store, spawner_factory):
        """No existing PR → ``create_pr`` fires, the returned URL is
        parsed for the PR number, and the number is persisted."""
        pipeline = _make_pipeline()
        spawner = spawner_factory(
            lookup_open_pr_return=None,
            create_pr_return="https://github.com/owner/repo/pull/9001",
        )
        contract = _make_contract()
        save_calls: list = []

        def _fake_save(c, _root):
            save_calls.append(c.pr.context_pr_number)

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("routes.resolve_worktree_path", return_value=tmp_path),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract", side_effect=_fake_save),
            # See the idempotent test: mock the pipeline-record mirror's
            # state-store load/save so the non-git tmp_path is fine (#2777).
            patch("state_store.get_state_store", return_value=store),
        ):
            store.load_pipeline.return_value = MagicMock(repo="owner/repo")
            result = _open_context_pr_at_implement_start("issue-2777")

        assert result == 9001
        spawner.gateway.create_pr.assert_called_once()
        pr_kwargs = spawner.gateway.create_pr.call_args.kwargs
        assert pr_kwargs["head"] == "egg/issue-2777/work"
        assert pr_kwargs["base"] == "main"
        assert pr_kwargs["title"] == "Add feature X"
        assert pr_kwargs["body"] == "Body"
        assert save_calls == [9001]


class TestOpenContextPRAtImplementStartTypedErrors:
    """Each closed ``ContextPrCreationReason`` (or a representative
    subset thereof) surfaces as a typed ``ContextPrCreationError``.
    There is NO soft-fail ``return None`` for gateway / contract
    failures — the failure must reach the BRC NACK / 422 surface.
    """

    def test_local_mode_returns_none_without_raising(self, store, spawner_factory):
        """No repo AND no base_branch is the local-mode short-circuit:
        return ``None`` quietly (no error)."""
        pipeline = _make_pipeline(repo="", base_branch="")
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
        ):
            assert _open_context_pr_at_implement_start("issue-2777") is None

    def test_base_branch_unset_resolves_default_branch(self, tmp_path, store, spawner_factory):
        """Repo set but base_branch unset is the NORMAL auto-detect state
        (#3031), NOT a misconfiguration. ``Pipeline.base_branch`` defaults
        to ``None`` and the standard ``submit_task`` path never populates
        it, so the opener MUST resolve the repo's default branch via
        ``_detect_default_branch`` and open the context PR against it —
        rather than hard-raising ``missing_base_branch`` and stranding the
        slice stack on ``/work`` (the #2777 regression #3031 fixes). A
        non-``main`` default proves the base is genuinely resolved rather
        than hardcoded, and both the idempotency lookup and ``create_pr``
        must receive the resolved value."""
        pipeline = _make_pipeline(repo="owner/repo", base_branch=None)
        spawner = spawner_factory(
            lookup_open_pr_return=None,
            create_pr_return="https://github.com/owner/repo/pull/7777",
        )
        contract = _make_contract()
        save_calls: list = []

        def _fake_save(c, _root):
            save_calls.append(c.pr.context_pr_number)

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("routes.resolve_worktree_path", return_value=tmp_path),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch(
                "routes.pipelines._detect_default_branch",
                return_value="develop",
            ) as detect,
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract", side_effect=_fake_save),
            patch("state_store.get_state_store", return_value=store),
        ):
            store.load_pipeline.return_value = MagicMock(repo="owner/repo")
            result = _open_context_pr_at_implement_start("issue-2777")

        assert result == 7777
        detect.assert_called_once_with(tmp_path)
        assert spawner.gateway.lookup_open_pr.call_args.kwargs["base"] == "develop"
        assert spawner.gateway.create_pr.call_args.kwargs["base"] == "develop"
        assert save_calls == [7777]

    def test_asymmetric_missing_repo_raises(self, store, spawner_factory):
        """Base branch set but repo empty → typed error."""
        pipeline = _make_pipeline(repo="", base_branch="main")
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
        ):
            with pytest.raises(ContextPrCreationError) as exc_info:
                _open_context_pr_at_implement_start("issue-2777")
            assert exc_info.value.reason == ContextPrCreationReason.MISSING_REPO.value

    def test_missing_branch_raises(self, store, spawner_factory):
        """Remote pipeline with no work branch → typed error."""
        pipeline = _make_pipeline(branch="")
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
        ):
            with pytest.raises(ContextPrCreationError) as exc_info:
                _open_context_pr_at_implement_start("issue-2777")
            assert exc_info.value.reason == ContextPrCreationReason.MISSING_BRANCH.value

    def test_pipeline_load_failed_raises(self):
        """Failure in ``get_state_store_for_pipeline`` → typed error."""
        with patch(
            "routes.get_state_store_for_pipeline",
            side_effect=RuntimeError("store unavailable"),
        ):
            with pytest.raises(ContextPrCreationError) as exc_info:
                _open_context_pr_at_implement_start("issue-2777")
            assert exc_info.value.reason == ContextPrCreationReason.PIPELINE_LOAD_FAILED.value

    def test_lookup_open_pr_failure_raises_lookup_failed(self, tmp_path, store, spawner_factory):
        """An unexpected ``lookup_open_pr`` raise → typed ``lookup_failed``.

        The primitive itself soft-fails a transient gateway/parse error
        to ``None`` (matching the slice path), so in production this only
        fires on a programming error; the opener's ``try`` is the
        typed-error backstop that keeps a raw exception from escaping the
        cq-4 no-raw-exception contract.
        """
        pipeline = _make_pipeline()
        spawner = spawner_factory(
            lookup_open_pr_side_effect=RuntimeError("gateway down"),
        )
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("routes.resolve_worktree_path", return_value=tmp_path),
            patch("routes.pipelines._get_spawner", return_value=spawner),
        ):
            with pytest.raises(ContextPrCreationError) as exc_info:
                _open_context_pr_at_implement_start("issue-2777")
            assert exc_info.value.reason == ContextPrCreationReason.LOOKUP_FAILED.value

    def test_missing_pr_block_raises_missing_pr_metadata(self, tmp_path, store, spawner_factory):
        """No PR block on the contract → typed ``missing_pr_metadata``.

        The opener guards on ``contract.pr is None or not title.strip()``;
        an empty title can't be constructed (Pydantic rejects it), so
        the ``pr=None`` branch is the reachable one.
        """
        pipeline = _make_pipeline()
        spawner = spawner_factory(lookup_open_pr_return=None)
        contract = _make_contract(with_pr_metadata=False)
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("routes.resolve_worktree_path", return_value=tmp_path),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch("egg_contracts.loader.load_contract", return_value=contract),
        ):
            with pytest.raises(ContextPrCreationError) as exc_info:
                _open_context_pr_at_implement_start("issue-2777")
            assert exc_info.value.reason == ContextPrCreationReason.MISSING_PR_METADATA.value

    def test_create_pr_failure_raises_gateway_error(self, tmp_path, store, spawner_factory):
        """Gateway ``create_pr`` raising → typed ``gateway_error``."""
        pipeline = _make_pipeline()
        spawner = spawner_factory(
            lookup_open_pr_return=None,
            create_pr_side_effect=RuntimeError("gh failed"),
        )
        contract = _make_contract()
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("routes.resolve_worktree_path", return_value=tmp_path),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch("egg_contracts.loader.load_contract", return_value=contract),
        ):
            with pytest.raises(ContextPrCreationError) as exc_info:
                _open_context_pr_at_implement_start("issue-2777")
            assert exc_info.value.reason == ContextPrCreationReason.GATEWAY_ERROR.value

    def test_create_pr_no_url_raises_gateway_no_url(self, tmp_path, store, spawner_factory):
        """Gateway returns empty URL → typed ``gateway_no_url``."""
        pipeline = _make_pipeline()
        spawner = spawner_factory(
            lookup_open_pr_return=None,
            create_pr_return="",
        )
        contract = _make_contract()
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("routes.resolve_worktree_path", return_value=tmp_path),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch("egg_contracts.loader.load_contract", return_value=contract),
        ):
            with pytest.raises(ContextPrCreationError) as exc_info:
                _open_context_pr_at_implement_start("issue-2777")
            assert exc_info.value.reason == ContextPrCreationReason.GATEWAY_NO_URL.value

    def test_unparseable_pr_url_raises_gateway_bad_url(self, tmp_path, store, spawner_factory):
        """Gateway returns a URL without ``/pull/<n>`` → typed ``gateway_bad_url``."""
        pipeline = _make_pipeline()
        spawner = spawner_factory(
            lookup_open_pr_return=None,
            create_pr_return="https://github.com/owner/repo/not-a-pr-url",
        )
        contract = _make_contract()
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("routes.resolve_worktree_path", return_value=tmp_path),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch("egg_contracts.loader.load_contract", return_value=contract),
        ):
            with pytest.raises(ContextPrCreationError) as exc_info:
                _open_context_pr_at_implement_start("issue-2777")
            assert exc_info.value.reason == ContextPrCreationReason.GATEWAY_BAD_URL.value


class TestOpenContextPRAtImplementStartImportFailures:
    """ImportError + non-ImportError-load failures inside the opener
    (egg-reviewer slice-3 follow-up): ``ROUTES_UNAVAILABLE``,
    ``LOADER_UNAVAILABLE``, and ``CONTRACT_LOAD_FAILED``.

    The first two branches are awkward because the modules are already
    on ``sys.modules`` by the time the test process reaches them — we
    use ``monkeypatch.setitem(sys.modules, ..., None)`` so the local
    ``from routes import ...`` / ``from egg_contracts.loader import ...``
    inside the function raises ``ImportError`` (Python's import
    machinery raises when ``sys.modules[name] is None``). The third
    branch only needs a fake ``load_contract`` that raises a
    non-``ImportError`` exception.
    """

    def test_routes_import_failure_raises_routes_unavailable(self, monkeypatch):
        """``from routes import ...`` failing inside the opener surfaces
        as ``ContextPrCreationReason.ROUTES_UNAVAILABLE`` rather than
        crashing with the raw ``ImportError`` (which the four
        ``except ContextPrCreationError`` call-site handlers would not
        match)."""
        monkeypatch.setitem(sys.modules, "routes", None)
        with pytest.raises(ContextPrCreationError) as exc_info:
            _open_context_pr_at_implement_start("issue-2777")
        assert exc_info.value.reason == ContextPrCreationReason.ROUTES_UNAVAILABLE.value
        assert isinstance(exc_info.value.cause, ImportError)

    def test_loader_import_failure_raises_loader_unavailable(
        self, tmp_path, store, spawner_factory, monkeypatch
    ):
        """``from egg_contracts.loader import load_contract`` failing on
        the miss path (after ``lookup_open_pr`` returns ``None``) surfaces
        as ``ContextPrCreationReason.LOADER_UNAVAILABLE``."""
        pipeline = _make_pipeline()
        spawner = spawner_factory(lookup_open_pr_return=None)
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("routes.resolve_worktree_path", return_value=tmp_path),
            patch("routes.pipelines._get_spawner", return_value=spawner),
        ):
            # Mask the loader AFTER the patches resolve (the patches
            # themselves don't import ``egg_contracts.loader``).
            monkeypatch.setitem(sys.modules, "egg_contracts.loader", None)
            with pytest.raises(ContextPrCreationError) as exc_info:
                _open_context_pr_at_implement_start("issue-2777")
        assert exc_info.value.reason == ContextPrCreationReason.LOADER_UNAVAILABLE.value
        assert isinstance(exc_info.value.cause, ImportError)

    def test_load_contract_failure_raises_contract_load_failed(
        self, tmp_path, store, spawner_factory
    ):
        """A non-``ImportError`` raised by ``load_contract`` (e.g. the
        contract YAML is malformed on disk) surfaces as
        ``ContextPrCreationReason.CONTRACT_LOAD_FAILED`` — the typed
        422 the BRC NACK / advance_phase handler contracts on."""
        pipeline = _make_pipeline()
        spawner = spawner_factory(lookup_open_pr_return=None)
        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("routes.resolve_worktree_path", return_value=tmp_path),
            patch("routes.pipelines._get_spawner", return_value=spawner),
            patch(
                "egg_contracts.loader.load_contract",
                side_effect=RuntimeError("malformed contract yaml"),
            ),
        ):
            with pytest.raises(ContextPrCreationError) as exc_info:
                _open_context_pr_at_implement_start("issue-2777")
        assert exc_info.value.reason == ContextPrCreationReason.CONTRACT_LOAD_FAILED.value
        assert isinstance(exc_info.value.cause, RuntimeError)


# ----------------------------------------------------------------------
# _persist_context_pr_number
# ----------------------------------------------------------------------


class TestPersistContextPrNumber:
    """The SOLE writer of ``context_pr_number`` after slice-2 deletes
    the legacy ``_persist_context_pr_linkage_on_contract`` helper. The
    happy path mutates the contract's ``pr.context_pr_number``; the
    no-PR-metadata path raises a typed error.
    """

    def test_happy_path_mutates_contract_pr_context_pr_number(self, tmp_path):
        """Successful load → write the PR number → save_contract is
        called with a contract that has ``context_pr_number`` set."""
        contract = _make_contract()
        save_calls: list = []

        def _fake_save(c, _root):
            save_calls.append(c.pr.context_pr_number)

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract", side_effect=_fake_save),
            # Mock the pipeline-record mirror's state-store load/save so the
            # non-git tmp_path does not trip StateStore creation (#2777).
            patch("state_store.get_state_store") as mock_get_store,
        ):
            mock_get_store.return_value.load_pipeline.return_value = MagicMock(repo="owner/repo")
            _persist_context_pr_number(
                "issue-2777",
                4242,
                worktree_repo_path=tmp_path,
                identifier=2777,
            )

        assert contract.pr.context_pr_number == 4242
        assert save_calls == [4242]

    def test_missing_pr_metadata_raises_typed_error(self, tmp_path):
        """Contract without a ``pr`` block → typed
        ``missing_pr_metadata`` error rather than an ``AttributeError``."""
        contract = _make_contract(with_pr_metadata=False)
        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
        ):
            with pytest.raises(ContextPrCreationError) as exc_info:
                _persist_context_pr_number(
                    "issue-2777",
                    4242,
                    worktree_repo_path=tmp_path,
                    identifier=2777,
                )
            assert exc_info.value.reason == ContextPrCreationReason.MISSING_PR_METADATA.value

    def test_save_failure_raises_save_failed(self, tmp_path):
        """``save_contract`` raising → typed ``save_failed`` error."""
        contract = _make_contract()
        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch(
                "egg_contracts.loader.save_contract",
                side_effect=OSError("disk full"),
            ),
        ):
            with pytest.raises(ContextPrCreationError) as exc_info:
                _persist_context_pr_number(
                    "issue-2777",
                    4242,
                    worktree_repo_path=tmp_path,
                    identifier=2777,
                )
            assert exc_info.value.reason == ContextPrCreationReason.SAVE_FAILED.value

    def test_loader_import_failure_raises_loader_unavailable(self, tmp_path, monkeypatch):
        """``from egg_contracts.loader import load_contract, save_contract``
        failing inside the helper surfaces as
        ``ContextPrCreationReason.LOADER_UNAVAILABLE``. Pairs with the
        opener-side test in
        ``TestOpenContextPRAtImplementStartImportFailures`` — both
        functions have their own local loader import that can
        independently raise."""
        monkeypatch.setitem(sys.modules, "egg_contracts.loader", None)
        with pytest.raises(ContextPrCreationError) as exc_info:
            _persist_context_pr_number(
                "issue-2777",
                4242,
                worktree_repo_path=tmp_path,
                identifier=2777,
            )
        assert exc_info.value.reason == ContextPrCreationReason.LOADER_UNAVAILABLE.value
        assert isinstance(exc_info.value.cause, ImportError)


# ----------------------------------------------------------------------
# ContextPrCreationError typo-fallback (egg-reviewer non-blocking #4)
# ----------------------------------------------------------------------


class TestContextPrCreationErrorTypoFallback:
    """A typo in ``reason=`` would normally raise ``ValueError`` from
    the StrEnum constructor, but the four ``except
    ContextPrCreationError`` handlers at the call sites would not match
    that ``ValueError``, so a typo would surface as a 500 instead of
    the typed 422 the handlers contract on. The fix coerces unknown
    reasons to ``UNKNOWN`` (and logs loudly).
    """

    def test_known_reason_passes_through(self):
        err = ContextPrCreationError("boom", reason="missing_branch")
        assert err.reason == ContextPrCreationReason.MISSING_BRANCH.value

    def test_unknown_reason_coerces_to_unknown(self):
        err = ContextPrCreationError("boom", reason="this_reason_does_not_exist")
        assert err.reason == ContextPrCreationReason.UNKNOWN.value

    def test_enum_reason_passes_through(self):
        err = ContextPrCreationError("boom", reason=ContextPrCreationReason.GATEWAY_ERROR)
        assert err.reason == ContextPrCreationReason.GATEWAY_ERROR.value
