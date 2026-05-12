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
# Observability sinks (#2611): emissions must reach the message store AND
# the event bus, not just the (handler-less) StatusReporter chain.
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_message_store(monkeypatch):
    """Pin an in-memory ``MessageStore`` singleton for the test so the
    wrapper's emission lands in a fresh store the test can read back
    without depending on a Redis backend (#2611).

    The explicit-instance ``monkeypatch.setattr`` bypasses
    ``_create_message_store`` (the only consumer of the
    ``EGG_MESSAGE_STORE_BACKEND`` env var), so we do not also set the
    env var — it would be redundant.
    """
    import message_store as _ms

    monkeypatch.setattr(_ms, "_message_store", _ms.MessageStore())
    return _ms.get_message_store()


class TestObservabilitySinks:
    """The wrapper must surface ``context_pr.*`` events on three sinks
    so operators have parity with the in-code docstring claim:

    * ``message_store.add_message`` — ``recent_messages`` /
      ``/pipelines/<id>/messages``.
    * ``_emit_pipeline_event`` — ``/status/wait`` + SSE.
    * ``report_pipeline_status`` — legacy ``StatusReporter`` handlers.

    Prior to #2611 only the third sink was wired, and no production
    handler was registered, so ``recent_messages`` and ``wait-status``
    never observed the event.  These tests pin all three sinks so a
    future refactor that drops one fails loudly.
    """

    def _make_contract_without_pr(self, raised: bool):
        """Build a contract whose post-hook ``context_pr_number`` is
        ``None`` — the precondition for the emit branch firing.  When
        ``raised`` is True the wrapper should pick ``context_pr.failed``;
        otherwise ``context_pr.skipped``.
        """
        from egg_contracts.models import (
            Contract,
            IssueInfo,
            PRMetadata,
        )
        from egg_contracts.models import (
            PipelinePhase as ContractPhase,
        )

        return Contract(
            issue=IssueInfo(number=2593, title="t", url=""),
            pipeline_id="issue-2593",
            current_phase=ContractPhase.PLAN,
            pr=PRMetadata(title="t") if raised else None,
        )

    def test_message_store_receives_context_pr_failed_entry(
        self, tmp_path, issue_pipeline, spawner, fresh_message_store
    ):
        """When the inner hook raises and the post-hook contract still
        has no context PR, ``recent_messages`` must include a
        ``CONTEXT_PR_FAILED`` entry tagged with the source."""
        contract = self._make_contract_without_pr(raised=True)

        def _fake_load(identifier, repo_root):
            return contract

        with (
            patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch("egg_contracts.loader.load_contract", _fake_load),
        ):
            inner.side_effect = RuntimeError("gateway down")
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                source="advance_phase_rest",
            )

        messages = fresh_message_store.get_messages("issue-2593")
        failed = [m for m in messages if m.message_type == "CONTEXT_PR_FAILED"]
        assert len(failed) == 1, (
            f"expected exactly one CONTEXT_PR_FAILED message in the "
            f"store; got message_types={[m.message_type for m in messages]!r}"
        )
        assert "advance_phase_rest" in failed[0].body
        # Metadata exposes the source/reason/error for downstream UIs.
        assert failed[0].metadata.get("source") == "advance_phase_rest"
        assert failed[0].metadata.get("reason") == "raised"
        assert "gateway down" in (failed[0].metadata.get("error") or "")

    def test_message_store_receives_context_pr_skipped_entry(
        self, tmp_path, issue_pipeline, spawner, fresh_message_store
    ):
        """Silent short-circuit must surface as a ``CONTEXT_PR_SKIPPED``
        message-store entry (not just a logger.info)."""
        contract = self._make_contract_without_pr(raised=False)

        def _fake_load(identifier, repo_root):
            return contract

        with (
            patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch("egg_contracts.loader.load_contract", _fake_load),
        ):
            inner.return_value = None
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                source="hitl_resume",
            )

        messages = fresh_message_store.get_messages("issue-2593")
        skipped = [m for m in messages if m.message_type == "CONTEXT_PR_SKIPPED"]
        assert len(skipped) == 1, (
            f"expected exactly one CONTEXT_PR_SKIPPED message in the "
            f"store; got message_types={[m.message_type for m in messages]!r}"
        )
        assert "hitl_resume" in skipped[0].body
        assert skipped[0].metadata.get("reason") == "skipped"

    def test_event_bus_receives_context_pr_event(
        self, tmp_path, issue_pipeline, spawner, fresh_message_store
    ):
        """``_emit_pipeline_event`` must be called with the event-type
        string so ``/status/wait`` waiters wake."""
        contract = self._make_contract_without_pr(raised=True)

        def _fake_load(identifier, repo_root):
            return contract

        emitted: list[tuple] = []

        def _fake_emit(pipeline, event_type_str):
            emitted.append((pipeline.id, event_type_str))

        with (
            patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch.object(_pipelines_mod, "_emit_pipeline_event", _fake_emit),
            patch("egg_contracts.loader.load_contract", _fake_load),
        ):
            inner.side_effect = RuntimeError("gateway down")
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                source="advance_phase_rest",
            )

        assert ("issue-2593", "context_pr.failed") in emitted, (
            f"expected _emit_pipeline_event to fire with 'context_pr.failed'; got {emitted!r}"
        )

    def test_event_bus_dispatch_reaches_real_eventbus(
        self, tmp_path, issue_pipeline, spawner, fresh_message_store, monkeypatch
    ):
        """Subscribe a handler to the real ``EventBus`` singleton and
        verify a ``CONTEXT_PR_FAILED`` event lands with the right
        ``EventType``/pipeline-id (#2611 review item 4).

        ``test_event_bus_receives_context_pr_event`` above patches
        ``_emit_pipeline_event`` itself, so it only proves the wrapper
        *calls* the function — not that the call reaches
        ``EventBus.publish`` with the right typed event.  Pair that
        with ``test_event_type_string_maps_to_typed_eventtype`` (which
        checks the dict mapping in isolation) and the wiring is
        covered piece-by-piece but not as a chain.  This test closes
        the gap by exercising the wrapper → ``_emit_pipeline_event``
        → ``emit_event`` → ``EventBus.publish`` → subscriber path
        end-to-end.

        The default singleton uses ``async_delivery=True``, which would
        force the test to poll the bus history; swap in a sync
        ``EventBus`` for the duration of the test instead so the
        subscriber fires synchronously inside ``publish()``.
        """
        import events as _events_mod

        sync_bus = _events_mod.EventBus(async_delivery=False)
        monkeypatch.setattr(_events_mod, "_event_bus", sync_bus)

        received: list[_events_mod.Event] = []
        sync_bus.subscribe(_events_mod.EventType.CONTEXT_PR_FAILED, received.append)

        contract = self._make_contract_without_pr(raised=True)

        def _fake_load(identifier, repo_root):
            return contract

        with (
            patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch("egg_contracts.loader.load_contract", _fake_load),
        ):
            inner.side_effect = RuntimeError("gateway down")
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                source="advance_phase_rest",
            )

        assert len(received) == 1, (
            f"expected exactly one CONTEXT_PR_FAILED event on the bus; "
            f"got {[(e.event_type, e.pipeline_id) for e in received]!r}"
        )
        assert received[0].event_type == _events_mod.EventType.CONTEXT_PR_FAILED
        assert received[0].pipeline_id == "issue-2593"

    def test_event_type_string_maps_to_typed_eventtype(self):
        """The event-bus mapping must know ``context_pr.skipped`` and
        ``context_pr.failed`` — otherwise ``_emit_pipeline_event``
        no-ops via ``mapped is None`` and the bus emission is silently
        dropped before reaching ``/status/wait`` (regression class
        #2611).
        """
        assert "context_pr.skipped" in _pipelines_mod._EVENT_TYPE_MAP
        assert "context_pr.failed" in _pipelines_mod._EVENT_TYPE_MAP

    def test_status_wait_allowlists_include_context_pr(self):
        """``/status/wait`` filters wake-ups via two allowlists.  Both
        must accept the new sinks so a long-poller is unblocked by
        either source (message store or event bus)."""
        assert "context_pr.skipped" in _pipelines_mod._STATUS_WAIT_EVENT_TYPES
        assert "context_pr.failed" in _pipelines_mod._STATUS_WAIT_EVENT_TYPES
        assert "CONTEXT_PR_SKIPPED" in _pipelines_mod._STATUS_WAIT_MESSAGE_TYPES
        assert "CONTEXT_PR_FAILED" in _pipelines_mod._STATUS_WAIT_MESSAGE_TYPES

    def test_repeated_invocations_dedupe_all_three_sinks(
        self, tmp_path, issue_pipeline, spawner, fresh_message_store
    ):
        """The dedupe set guarding the emit branch must cover all three
        sinks — otherwise a second wrapper invocation would double up
        ``recent_messages`` or wake ``wait-status`` twice on the same
        underlying failure."""
        contract = self._make_contract_without_pr(raised=True)

        def _fake_load(identifier, repo_root):
            return contract

        emitted: list[tuple] = []
        reports: list[tuple] = []

        def _fake_emit(pipeline, event_type_str):
            emitted.append((pipeline.id, event_type_str))

        def _fake_report(pipeline, event_type=None, message=None):
            reports.append((event_type, message))

        with (
            patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch.object(_pipelines_mod, "_emit_pipeline_event", _fake_emit),
            patch.object(_pipelines_mod, "report_pipeline_status", _fake_report),
            patch("egg_contracts.loader.load_contract", _fake_load),
        ):
            inner.side_effect = RuntimeError("gateway down")
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

        # All three sinks must respect the dedupe.
        store_msgs = [
            m
            for m in fresh_message_store.get_messages("issue-2593")
            if m.message_type == "CONTEXT_PR_FAILED"
        ]
        assert len(store_msgs) == 1, (
            f"dedupe failure: expected 1 CONTEXT_PR_FAILED message in "
            f"the store across two wrapper invocations; got "
            f"{len(store_msgs)}"
        )
        assert emitted.count(("issue-2593", "context_pr.failed")) == 1, (
            f"dedupe failure: expected 1 _emit_pipeline_event call "
            f"with context_pr.failed; got {emitted!r}"
        )
        assert len([r for r in reports if r[0] == "context_pr.failed"]) == 1, (
            f"dedupe failure: expected 1 report_pipeline_status call "
            f"with context_pr.failed; got {reports!r}"
        )

    def test_message_store_failure_does_not_strand_transition(
        self, tmp_path, issue_pipeline, spawner
    ):
        """The wrapper's swallow-all contract (#2548 decision-3) extends
        to the message-store sink: if ``add_message`` blows up, the
        wrapper must still return cleanly.  Otherwise an observability
        outage would strand the plan→implement transition.

        Also pins that sink 1 (``report_pipeline_status``) and sink 3
        (``_emit_pipeline_event``) still fire when sink 2 raises —
        the three sinks are independently isolated by their own
        ``try/except`` blocks, and a future refactor that collapses
        them into a single try/except would silently regress this
        property.  Without these assertions the wrapper could
        accidentally short-circuit out of sink 3 on any sink-2
        failure without breaking the no-raise contract above.
        """
        contract = self._make_contract_without_pr(raised=True)

        def _fake_load(identifier, repo_root):
            return contract

        import message_store as _ms

        emitted: list[tuple] = []
        reports: list[tuple] = []

        def _fake_emit(pipeline, event_type_str):
            emitted.append((pipeline.id, event_type_str))

        def _fake_report(pipeline, event_type=None, message=None):
            reports.append((event_type, message))

        with (
            patch.object(_pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch.object(_pipelines_mod, "_emit_pipeline_event", _fake_emit),
            patch.object(_pipelines_mod, "report_pipeline_status", _fake_report),
            patch.object(_ms.MessageStore, "add_message", side_effect=RuntimeError("store down")),
            patch("egg_contracts.loader.load_contract", _fake_load),
        ):
            inner.side_effect = RuntimeError("gateway down")
            # Must not raise — observability emission is best-effort.
            _maybe_open_base_pr_for_plan_to_implement(
                issue_pipeline,
                spawner,
                tmp_path,
                source="advance_phase_rest",
            )

        # Sink 1 must still fire even though sink 2 raised.
        assert any(r[0] == "context_pr.failed" for r in reports), (
            f"sink isolation regression: report_pipeline_status was not called "
            f"with context_pr.failed after add_message raised; got reports={reports!r}"
        )
        # Sink 3 must still fire even though sink 2 raised.
        assert ("issue-2593", "context_pr.failed") in emitted, (
            f"sink isolation regression: _emit_pipeline_event was not called "
            f"with context_pr.failed after add_message raised; got emitted={emitted!r}"
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
