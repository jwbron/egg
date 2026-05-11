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


# ---------------------------------------------------------------------------
# Call-site wiring: the four transition paths route through the helper
# ---------------------------------------------------------------------------


class TestCallSiteWiring:
    """Static / textual assertions that each known transition path
    routes through ``_maybe_open_base_pr_for_plan_to_implement`` and
    passes a recognised ``source`` value.  These guard against future
    refactors silently breaking the wiring (regression class for
    #2593)."""

    def test_run_pipeline_autoadvance_source_present(self):
        src = (_orchestrator_path / "routes" / "pipelines.py").read_text()
        assert 'source="run_pipeline_autoadvance"' in src

    def test_implement_entry_backstop_source_present(self):
        src = (_orchestrator_path / "routes" / "pipelines.py").read_text()
        assert 'source="implement_entry_backstop"' in src

    def test_hitl_resume_source_present(self):
        src = (_orchestrator_path / "routes" / "pipelines.py").read_text()
        assert 'source="hitl_resume"' in src

    def test_advance_phase_rest_source_present(self):
        src = (_orchestrator_path / "routes" / "phases.py").read_text()
        assert 'source="advance_phase_rest"' in src

    def test_helper_called_from_all_four_sites(self):
        """Belt-and-suspenders: count call sites of the helper across
        both files.  Exactly four expected (run_pipeline auto-advance,
        implement-entry backstop, HITL resume in pipelines.py, plus
        advance_phase in phases.py).  An accidental refactor that
        drops one will trip this."""
        pl_src = (_orchestrator_path / "routes" / "pipelines.py").read_text()
        ph_src = (_orchestrator_path / "routes" / "phases.py").read_text()
        # Count call-site invocations, not the definition line.
        pl_calls = pl_src.count("_maybe_open_base_pr_for_plan_to_implement(")
        ph_calls = ph_src.count("_maybe_open_base_pr_for_plan_to_implement(")
        # pipelines.py has 1 (definition) + 3 (autoadvance, backstop,
        # HITL) = 4 occurrences of the bare name.
        # phases.py has 1 (advance_phase REST handler).
        assert pl_calls == 4, (
            f"expected 4 occurrences of "
            f"_maybe_open_base_pr_for_plan_to_implement( in pipelines.py "
            f"(1 def + 3 call sites), got {pl_calls}"
        )
        assert ph_calls == 1, (
            f"expected 1 occurrence of "
            f"_maybe_open_base_pr_for_plan_to_implement( in phases.py "
            f"(advance_phase REST call site), got {ph_calls}"
        )
