"""Tests for the plan→implement context-PR transition wiring (#2593).

#2548 added ``_open_context_pr_for_pipeline`` and wired it into the
inline ``_run_pipeline`` auto-advance path.  #2593 found that the
hook was missing from the other plan→implement transition paths
(``advance_phase`` REST/MCP and the HITL-approval recovery in
``start_pipeline``), so operators clearing the plan gate via those
paths got a slice stack rooted on ``/work`` with no PR to ``main``.

This file pins:

* ``_maybe_open_base_pr_for_plan_to_implement`` wraps the inner hook
  with the CUSTOM-mode guard, the swallow-all-exceptions semantics,
  and the post-hook message-bus emission;
* The wrapper passes ``source`` through to the inner hook so logs
  identify the call site that fired;
* The "hook entered" log line is emitted on every call, even on
  idempotent short-circuit, so operators can confirm the hook ran
  without grepping for per-short-circuit strings;
* A ``context_pr.skipped`` / ``context_pr.failed`` message is appended
  to the pipeline message bus when the hook returned without opening
  a PR on a pipeline that should have one;
* The four call sites (autoadvance, advance_phase REST, HITL resume,
  implement-entry backstop) all route through the same helper.
"""

import logging
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


from models import Pipeline, PipelineMode, PipelinePhase, PipelineStatus  # noqa: E402
from routes import pipelines as _pipelines_mod  # noqa: E402
from routes.pipelines import (  # noqa: E402
    _maybe_open_base_pr_for_plan_to_implement,
)


@pytest.fixture
def issue_pipeline():
    return Pipeline(
        id="issue-2593",
        issue_number=2593,
        repo="owner/repo",
        branch="egg/issue-2593/work",
        base_branch="main",
        mode=PipelineMode.ISSUE,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.PLAN,
    )


@pytest.fixture
def custom_pipeline():
    return Pipeline(
        id="issue-2593-custom",
        issue_number=2593,
        repo="owner/repo",
        branch="egg/issue-2593-custom/work",
        base_branch="main",
        mode=PipelineMode.CUSTOM,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.PLAN,
    )


@pytest.fixture
def spawner():
    s = MagicMock(name="spawner")
    s.gateway = MagicMock(name="gateway")
    return s


@pytest.fixture
def propagate_orchestrator_logs():
    """``egg_logging`` configures ``orchestrator.pipelines`` with
    ``propagate=False`` so structured records don't bubble up to the
    root logger.  pytest's ``caplog`` attaches to the root logger, so
    without re-enabling propagation the test would never see the
    records we're asserting on.  Restore the original setting on
    teardown.
    """
    pl_logger = logging.getLogger("orchestrator.pipelines")
    prior = pl_logger.propagate
    pl_logger.propagate = True
    try:
        yield
    finally:
        pl_logger.propagate = prior


@pytest.fixture(autouse=True)
def reset_context_pr_dedupe():
    """The wrapper dedupes ``context_pr.skipped`` / ``context_pr.failed``
    via a module-level set keyed on ``pipeline_id`` (#2593 review issue
    2).  Clear the set per test so tests that re-use the same pipeline
    fixture do not see the dedupe from a sibling test's first call.
    """
    _pipelines_mod._context_pr_events_emitted.clear()
    yield
    _pipelines_mod._context_pr_events_emitted.clear()


# ---------------------------------------------------------------------------
# CUSTOM-mode guard
# ---------------------------------------------------------------------------


class TestCustomModeGuard:
    def test_skips_custom_mode_pipelines(self, tmp_path, custom_pipeline, spawner):
        """CUSTOM-mode pipelines run a single phase and terminate (#1762).
        Opening a context PR for them would orphan a PR with no slices.
        The wrapper must short-circuit before invoking the inner hook."""
        with patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner:
            _maybe_open_base_pr_for_plan_to_implement(
                custom_pipeline,
                spawner,
                tmp_path,
                source="run_pipeline_autoadvance",
            )
        inner.assert_not_called()

    def test_emits_skip_log_line_for_custom_mode(
        self,
        tmp_path,
        custom_pipeline,
        spawner,
        caplog,
        propagate_orchestrator_logs,
    ):
        """#2593 review issue 10 — CUSTOM-mode pipelines short-circuit
        before the inner hook is invoked, but the wrapper still emits
        a "hook skipped (CUSTOM mode)" log line so operators tracing
        transition paths via log greps see one record per call site
        (not silence)."""
        with caplog.at_level(logging.INFO):
            _maybe_open_base_pr_for_plan_to_implement(
                custom_pipeline,
                spawner,
                tmp_path,
                source="run_pipeline_autoadvance",
            )
        skipped = [
            rec
            for rec in caplog.records
            if "Context PR hook skipped (CUSTOM mode)" in rec.getMessage()
        ]
        assert skipped, "expected a 'Context PR hook skipped (CUSTOM mode)' log line"


# ---------------------------------------------------------------------------
# Source propagation + exception swallow
# ---------------------------------------------------------------------------


class TestSourcePropagation:
    def test_passes_source_through_to_inner_hook(self, tmp_path, issue_pipeline, spawner):
        with patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner:
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                gateway_mode="public",
                source="advance_phase_rest",
            )
        inner.assert_called_once()
        kwargs = inner.call_args.kwargs
        assert kwargs["source"] == "advance_phase_rest"
        assert kwargs["gateway_mode"] == "public"


class TestSwallowExceptions:
    def test_inner_exception_is_logged_and_swallowed(
        self,
        tmp_path,
        issue_pipeline,
        spawner,
        caplog,
        propagate_orchestrator_logs,
    ):
        """A transient infra problem inside the hook must not strand the
        plan→implement transition.  The wrapper logs the error and
        returns normally."""
        with (
            patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            caplog.at_level(logging.WARNING),
        ):
            inner.side_effect = RuntimeError("gateway down")
            # Must not raise.
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                source="implement_entry_backstop",
            )
        # The structured log message survives the swallow.
        assert any(
            "Context PR hook raised at plan→implement transition" in rec.getMessage()
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# "Hook entered" log line on every invocation
# ---------------------------------------------------------------------------


class TestHookEnteredLog:
    def test_log_line_emitted_on_idempotent_skip(
        self,
        tmp_path,
        issue_pipeline,
        spawner,
        caplog,
        propagate_orchestrator_logs,
    ):
        """The "hook entered" line must be emitted even when the inner
        function short-circuits — that's the whole point of the gap
        detector added in #2593.  Use a contract whose
        ``context_pr_number`` is already set so the inner short-circuits
        cleanly without touching the gateway."""
        from egg_contracts.models import (
            Contract,
            IssueInfo,
            PRMetadata,
        )
        from egg_contracts.models import (
            PipelinePhase as ContractPhase,
        )

        contract = Contract(
            issue=IssueInfo(number=2593, title="t", url=""),
            pipeline_id="issue-2593",
            current_phase=ContractPhase.PLAN,
            pr=PRMetadata(
                title="t",
                context_branch="egg/issue-2593/context",
                context_pr_number=42,
            ),
        )

        def _fake_load(identifier, repo_root):
            return contract

        with (
            patch("egg_contracts.loader.load_contract", _fake_load),
            caplog.at_level(logging.INFO),
        ):
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                source="run_pipeline_autoadvance",
            )

        # Look for the structured "Context PR hook entered" log message.
        # egg_logging emits structured records; either the message or a
        # source key on the record will carry the marker.
        entered = [rec for rec in caplog.records if "Context PR hook entered" in rec.getMessage()]
        assert entered, "expected a 'Context PR hook entered' log line"


# ---------------------------------------------------------------------------
# Message-bus emission when hook fails to open a PR
# ---------------------------------------------------------------------------


class TestMessageBusEmission:
    def test_emits_context_pr_failed_on_inner_exception(self, tmp_path, issue_pipeline, spawner):
        """When the hook raises (gateway down etc.), and the pipeline
        still has no ``context_pr_number`` afterwards, surface that on
        the message bus so operators using ``wait-status`` see it."""
        reports: list[tuple[str | None, str | None]] = []

        def _fake_report(pipeline, event_type=None, message=None):
            reports.append((event_type, message))

        # Simulate "post-hook contract still has no context PR number".
        from egg_contracts.models import (
            Contract,
            IssueInfo,
            PRMetadata,
        )
        from egg_contracts.models import (
            PipelinePhase as ContractPhase,
        )

        contract = Contract(
            issue=IssueInfo(number=2593, title="t", url=""),
            pipeline_id="issue-2593",
            current_phase=ContractPhase.PLAN,
            pr=PRMetadata(title="t"),  # context_pr_number left as None
        )

        def _fake_load(identifier, repo_root):
            return contract

        with (
            patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch.object(_pipelines_mod, "report_pipeline_status", _fake_report),
            patch("egg_contracts.loader.load_contract", _fake_load),
        ):
            inner.side_effect = RuntimeError("gateway down")
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                source="advance_phase_rest",
            )

        failed_events = [r for r in reports if r[0] == "context_pr.failed"]
        assert failed_events, (
            f"expected a context_pr.failed status message on inner exception; got {reports!r}"
        )
        # The status message should include the source so the operator
        # can tell which transition path missed it.
        assert "advance_phase_rest" in (failed_events[0][1] or "")

    def test_emits_context_pr_skipped_on_silent_short_circuit(
        self, tmp_path, issue_pipeline, spawner
    ):
        """When the inner hook short-circuits silently (e.g. contract.pr
        missing) and the contract still has no context_pr_number, emit
        ``context_pr.skipped`` so the operator knows nothing was
        opened."""
        reports: list[tuple[str | None, str | None]] = []

        def _fake_report(pipeline, event_type=None, message=None):
            reports.append((event_type, message))

        from egg_contracts.models import Contract, IssueInfo
        from egg_contracts.models import (
            PipelinePhase as ContractPhase,
        )

        contract = Contract(
            issue=IssueInfo(number=2593, title="t", url=""),
            pipeline_id="issue-2593",
            current_phase=ContractPhase.PLAN,
            pr=None,
        )

        def _fake_load(identifier, repo_root):
            return contract

        with (
            patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch.object(_pipelines_mod, "report_pipeline_status", _fake_report),
            patch("egg_contracts.loader.load_contract", _fake_load),
        ):
            # Inner returns None silently (no exception).
            inner.return_value = None
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                source="hitl_resume",
            )

        skipped_events = [r for r in reports if r[0] == "context_pr.skipped"]
        assert skipped_events, (
            "expected a context_pr.skipped status message when inner "
            "returned without opening a PR; "
            f"got {reports!r}"
        )
        assert "hitl_resume" in (skipped_events[0][1] or "")

    def test_does_not_emit_for_local_mode_pipeline(self, tmp_path, issue_pipeline, spawner):
        """A pipeline without a remote (``pipeline.repo is None``) is
        local-mode by configuration — it can't have a PR and should not
        appear on the bus as a failure.  The wrapper must skip the
        emission entirely for these."""
        reports: list = []

        def _fake_report(pipeline, event_type=None, message=None):
            reports.append((event_type, message))

        issue_pipeline.repo = None
        with (
            patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch.object(_pipelines_mod, "report_pipeline_status", _fake_report),
        ):
            inner.return_value = None
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                source="run_pipeline_autoadvance",
            )
        assert reports == [], "must not emit context_pr.* on local-mode pipelines (no remote)"

    def test_repeated_invocations_dedupe_emitted_event(self, tmp_path, issue_pipeline, spawner):
        """#2593 review issue 2 — the wrapper can run multiple times
        for the same pipeline (auto-advance + implement-entry backstop,
        HITL recovery + backstop).  The inner hook's idempotency
        short-circuits the PR-creation work, but the bus emission
        would otherwise fire on each invocation.  The wrapper must
        dedupe so a single failure produces one ``context_pr.failed``
        event, not one per call site."""
        reports: list[tuple[str | None, str | None]] = []

        def _fake_report(pipeline, event_type=None, message=None):
            reports.append((event_type, message))

        from egg_contracts.models import (
            Contract,
            IssueInfo,
            PRMetadata,
        )
        from egg_contracts.models import (
            PipelinePhase as ContractPhase,
        )

        contract = Contract(
            issue=IssueInfo(number=2593, title="t", url=""),
            pipeline_id="issue-2593",
            current_phase=ContractPhase.PLAN,
            pr=PRMetadata(title="t"),
        )

        def _fake_load(identifier, repo_root):
            return contract

        with (
            patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch.object(_pipelines_mod, "report_pipeline_status", _fake_report),
            patch("egg_contracts.loader.load_contract", _fake_load),
        ):
            inner.side_effect = RuntimeError("gateway down")
            # Simulate auto-advance then implement-entry backstop.
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                source="run_pipeline_autoadvance",
            )
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                source="implement_entry_backstop",
            )

        failed_events = [r for r in reports if r[0] == "context_pr.failed"]
        assert len(failed_events) == 1, (
            f"expected exactly one context_pr.failed event across two "
            f"wrapper invocations for the same pipeline; got {reports!r}"
        )


# ---------------------------------------------------------------------------
# Call-site wiring: the four transition paths route through the helper
# ---------------------------------------------------------------------------


def _collect_helper_call_sources(file_path: Path) -> list[str | None]:
    """AST-walk ``file_path`` and return the ``source=`` kwarg literal
    of every call to ``_maybe_open_base_pr_for_plan_to_implement``.

    Returns one entry per call site (function definitions are NOT
    counted — only ``ast.Call`` nodes).  A ``None`` entry means the
    call site was found but its ``source`` kwarg was not a plain
    string literal (a future refactor might dispatch on a variable —
    flag for human review).
    """
    import ast

    tree = ast.parse(file_path.read_text())
    sources: list[str | None] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "_maybe_open_base_pr_for_plan_to_implement":
            literal: str | None = None
            for kw in node.keywords:
                if kw.arg == "source" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        literal = kw.value.value
            sources.append(literal)
    return sources


class TestCallSiteWiring:
    """AST-based assertions that each known transition path routes
    through ``_maybe_open_base_pr_for_plan_to_implement`` and passes
    a recognised ``source`` value.  These guard against future
    refactors silently breaking the wiring (regression class for
    #2593).

    AST-based (#2593 review issue 8) so the count is not perturbed by
    docstring examples that mention the helper name, by import-line
    splits, or by string-literal occurrences in error messages.  The
    function definition itself is an ``ast.FunctionDef`` and is not
    counted.
    """

    def test_pipelines_py_has_expected_call_sites(self):
        """``pipelines.py`` calls the helper from auto-advance,
        implement-entry backstop, and HITL resume — three call sites
        with three distinct, recognised source values.  The wrapper
        definition itself is an ``ast.FunctionDef`` and is excluded
        by the AST filter."""
        pl_path = _orchestrator_path / "routes" / "pipelines.py"
        sources = _collect_helper_call_sources(pl_path)
        assert len(sources) == 3, (
            f"expected 3 call sites in pipelines.py (auto-advance, "
            f"implement-entry backstop, HITL resume), got {len(sources)} "
            f"with sources {sources!r}"
        )
        # No call site should pass a non-literal ``source`` — that
        # would defeat the per-path log tagging.
        assert all(s is not None for s in sources), (
            f"every helper call must pass source= as a string literal; got {sources!r}"
        )
        assert set(sources) == {
            "run_pipeline_autoadvance",
            "implement_entry_backstop",
            "hitl_resume",
        }, f"unexpected source values in pipelines.py: {sources!r}"

    def test_phases_py_has_expected_call_site(self):
        """``phases.py`` calls the helper exactly once, from the
        plan→implement branch of the ``advance_phase`` REST/MCP
        handler, with ``source="advance_phase_rest"``."""
        ph_path = _orchestrator_path / "routes" / "phases.py"
        sources = _collect_helper_call_sources(ph_path)
        assert sources == ["advance_phase_rest"], (
            f"expected 1 call site in phases.py with source='advance_phase_rest'; got {sources!r}"
        )
