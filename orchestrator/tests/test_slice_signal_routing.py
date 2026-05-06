"""Slice-aware spawn env + signal routing (#2403).

Pins the wire shape that lets per-slice agents reach the orchestrator:

  * The slice-spawn path leaves ``EGG_PIPELINE_ID`` as the bare
    pipeline id (passes ``state_store.PIPELINE_ID_PATTERN``) and
    exposes the slice via ``EGG_SLICE_ID``. An earlier shape jammed
    ``{pipeline_id}/{slice_id}`` into ``EGG_PIPELINE_ID`` itself,
    which 4xx'd every agent → orchestrator round-trip (the validator
    rejects ``/`` and Flask's URL converter doesn't allow it either).
  * Consensus signal handlers read ``slice_id`` from the request body
    and route the tracker lookup to ``get_peer_consensus_tracker(
    pipeline_id, slice_id)`` so per-slice CONSENSUS_* lands on the
    slice's tracker, not the pipeline-level one.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    PhaseExecution,
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import _run_concurrent_phase
from state_store import PIPELINE_ID_PATTERN


def _make_pipeline() -> Pipeline:
    config = PipelineConfig()
    config.concurrent_execution = True
    config.max_concurrent_agents = 4
    config.consensus_timeout_minutes = 30
    return Pipeline(
        id="issue-2403",
        issue_number=2403,
        repo="owner/repo",
        branch="egg/issue-2403/work",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _make_execution(role: AgentRole, container_id: str) -> AgentExecution:
    return AgentExecution(
        role=role,
        status=AgentExecutionStatus.RUNNING,
        container_id=container_id,
        started_at=datetime.now(UTC),
    )


_CALL_ARGS = {
    "repo_volumes": {},
    "gateway_mode": "public",
    "repos": ["owner/repo"],
    "certs_volume": None,
    "worktree_repo_path": Path("/tmp/test-repo"),
}


def _setup_spawn(executions: list[AgentExecution]):
    pipeline = _make_pipeline()
    phase_exec = PhaseExecution(
        phase=PipelinePhase.IMPLEMENT,
        status=PipelineStatus.RUNNING,
    )
    mock_store = MagicMock()
    mock_pipeline_state = MagicMock()
    mock_pipeline_state.get_phase_execution.return_value = phase_exec
    mock_store.load_pipeline.return_value = mock_pipeline_state

    mock_docker = MagicMock()
    mock_docker.get_container_info.side_effect = lambda cid: ContainerInfo(
        container_id=cid,
        container_name=cid,
        status=ContainerStatus.EXITED,
        exit_code=0,
        exited_at=datetime.now(UTC),
    )
    mock_spawner = MagicMock()
    mock_spawner.backend = mock_docker
    mock_spawner.docker = mock_docker
    mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()
    return pipeline, mock_store, mock_spawner


# ---------------------------------------------------------------------------
# call_args helpers — colocated above all consumers so a future
# ``get_peer_consensus_tracker`` signature change has one place to update.
# ---------------------------------------------------------------------------


def _slice_arg_from_call(call) -> str | None:
    """Extract ``slice_id`` from ``get_peer_consensus_tracker``'s call_args.

    The call site uses positional args (``pipeline_id, slice_id``) but
    we accept a kwarg too so future refactors don't break the test.
    """
    if "slice_id" in call.kwargs:
        return call.kwargs["slice_id"]
    return call.args[1] if len(call.args) >= 2 else None


def _pipeline_arg_from_call(call) -> str | None:
    """Extract ``pipeline_id`` from ``get_peer_consensus_tracker``'s call_args.

    Symmetric with ``_slice_arg_from_call`` so tests don't break if a
    future refactor moves ``pipeline_id`` from a positional arg to a
    kwarg.
    """
    if "pipeline_id" in call.kwargs:
        return call.kwargs["pipeline_id"]
    return call.args[0] if call.args else None


class TestSliceSpawnEnvShape:
    """``EGG_PIPELINE_ID`` stays canonical; slice scope rides on ``EGG_SLICE_ID``.

    Single-source-of-truth (#2410 v2 review): ``EGG_SLICE_ID`` is injected
    by ``KubernetesSpawner.spawn_agent_job`` from the ``slice_id`` parameter
    that already drives Job naming and worktree id, and the key is in
    ``_PROTECTED_ENV_KEYS`` so ``extra_env`` cannot smuggle a mismatched
    value. These tests pin the wrapper-side contract: ``slice_id`` must
    flow through ``create_concurrent_spawn_fn`` as a kwarg, and
    ``sandbox_env`` must not carry ``EGG_SLICE_ID`` (a duplicate setter
    here would just trip the protected-key warning every spawn).
    ``test_kubernetes_spawner.py`` covers the spawner-side env injection.
    """

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic", return_value=0.0)
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_slice_scope_forwards_slice_id_and_keeps_egg_pipeline_id_bare(
        self, MockExecutor, mock_prompt, mock_lock, _mono, _sleep
    ):
        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        pipeline, mock_store, mock_spawner = _setup_spawn(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance
        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        _run_concurrent_phase(
            pipeline_id="issue-2403",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            sandbox_env={"PRESERVED": "yes"},
            slice_id="slice-2",
            **_CALL_ARGS,
        )

        # ``create_concurrent_spawn_fn`` is the seam where the slice scope
        # is frozen for the spawn closure.
        kwargs = mock_spawner.create_concurrent_spawn_fn.call_args.kwargs
        assert kwargs["pipeline_id"] == "issue-2403"
        # Slice scope rides on the ``slice_id`` kwarg, not on
        # ``sandbox_env``. The spawner sets ``EGG_SLICE_ID`` itself from
        # this parameter (single source of truth).
        assert kwargs["slice_id"] == "slice-2"
        env = kwargs["sandbox_env"]
        # ``sandbox_env`` must NOT carry ``EGG_SLICE_ID`` — the key is
        # protected and a duplicate would log the override every spawn.
        assert "EGG_SLICE_ID" not in env
        # Agent CLIs read EGG_PIPELINE_ID via ``get_pipeline_id`` — only
        # set if the caller seeded it. We assert here that the function
        # didn't smuggle a slashed value into it.
        if "EGG_PIPELINE_ID" in env:
            assert PIPELINE_ID_PATTERN.match(env["EGG_PIPELINE_ID"]) is not None
        # Pre-existing keys must survive the slice-aware path.
        assert env["PRESERVED"] == "yes"

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic", return_value=0.0)
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_no_slice_scope_does_not_set_egg_slice_id(
        self, MockExecutor, mock_prompt, mock_lock, _mono, _sleep
    ):
        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        pipeline, mock_store, mock_spawner = _setup_spawn(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance
        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        _run_concurrent_phase(
            pipeline_id="issue-2403",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            sandbox_env={},
            slice_id=None,
            **_CALL_ARGS,
        )

        kwargs = mock_spawner.create_concurrent_spawn_fn.call_args.kwargs
        assert kwargs.get("slice_id") is None
        env = kwargs["sandbox_env"]
        assert "EGG_SLICE_ID" not in env


class TestConsensusSignalSliceRouting:
    """``handle_consensus_*`` look up the slice tracker when ``slice_id`` is supplied."""

    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_propose_routes_to_slice_tracker(self, mock_get_tracker, app):
        from routes.signals import handle_consensus_propose_signal

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            handle_consensus_propose_signal(
                "issue-2403",
                {
                    "agent_role": "coder",
                    "slice_id": "slice-2",
                    "payload": {
                        "summary": (
                            "Implemented slice-2 work with thorough commit "
                            "message and substantive description over fifty chars"
                        ),
                        "artifacts": ["src/a.py"],
                    },
                },
                Path("/tmp/repo"),
            )

        # The tracker lookup MUST forward slice_id so consensus messages
        # land on the per-slice tracker (#2403).
        call = mock_get_tracker.call_args
        assert _pipeline_arg_from_call(call) == "issue-2403"
        assert _slice_arg_from_call(call) == "slice-2"

    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_propose_without_slice_falls_back_to_pipeline_tracker(self, mock_get_tracker, app):
        from routes.signals import handle_consensus_propose_signal

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            handle_consensus_propose_signal(
                "issue-2403",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": (
                            "Implemented work with substantive description "
                            "over fifty chars to satisfy the validator"
                        ),
                        "artifacts": ["src/a.py"],
                    },
                },
                Path("/tmp/repo"),
            )

        # Pipeline-level callers (no slice_id) keep the bare-tracker
        # semantics — ``get_peer_consensus_tracker(pipeline_id, None)``
        # is the same key as the legacy single-arg lookup.
        call = mock_get_tracker.call_args
        assert _pipeline_arg_from_call(call) == "issue-2403"
        assert _slice_arg_from_call(call) is None

    def test_propose_rejects_malformed_slice_id(self, app):
        from routes.signals import handle_consensus_propose_signal

        with app.app_context():
            response, status = handle_consensus_propose_signal(
                "issue-2403",
                {
                    "agent_role": "coder",
                    "slice_id": "../etc/passwd",
                    "payload": {
                        "summary": (
                            "Implemented work with substantive description "
                            "over fifty chars to satisfy the validator"
                        ),
                        "artifacts": ["src/a.py"],
                    },
                },
                Path("/tmp/repo"),
            )
        assert status == 400
        assert "slice_id" in response.get_json()["message"]


class TestAllConsensusHandlersRouteToSliceTracker:
    """Every consensus signal handler forwards ``slice_id`` to the tracker lookup.

    The first review (#2402) noted that ``test_propose_routes_to_slice_tracker``
    only pinned the ``propose`` path even though all eight CONSENSUS_*
    handlers got the same paste-and-modify treatment. These tests pin the
    other seven (ack, nack, withdraw, confirmed, excuse_producer,
    resolve_obligation, producer_push) so the tracker-lookup wiring
    cannot regress silently.
    """

    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_ack_routes_to_slice_tracker(self, mock_get_tracker, app):
        from routes.signals import handle_consensus_ack_signal

        mock_tracker = MagicMock()
        mock_tracker.handle_ack.return_value = {
            "version": 1,
            "newly_ready": [],
        }
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            handle_consensus_ack_signal(
                "issue-2403",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "slice_id": "slice-2",
                    "payload": {
                        "reason": (
                            "Reviewed slice-2 work and the diff matches the "
                            "proposal summary; tests cover the new path."
                        ),
                    },
                },
                Path("/tmp/repo"),
            )

        call = mock_get_tracker.call_args
        assert _pipeline_arg_from_call(call) == "issue-2403"
        assert _slice_arg_from_call(call) == "slice-2"

    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_nack_routes_to_slice_tracker(self, mock_get_tracker, app):
        from routes.signals import handle_consensus_nack_signal

        mock_tracker = MagicMock()
        mock_tracker.handle_nack.return_value = {
            "version": 1,
            "reason": "needs more tests",
            "revision_count": 1,
        }
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            handle_consensus_nack_signal(
                "issue-2403",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "slice_id": "slice-2",
                    "payload": {
                        "reason": (
                            "Slice-2 NACK: the diff is missing test coverage "
                            "for the new branch in the orchestrator's strip site."
                        ),
                    },
                },
                Path("/tmp/repo"),
            )

        call = mock_get_tracker.call_args
        assert _pipeline_arg_from_call(call) == "issue-2403"
        assert _slice_arg_from_call(call) == "slice-2"

    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_withdraw_routes_to_slice_tracker(self, mock_get_tracker, app):
        from routes.signals import handle_consensus_withdraw_signal

        mock_tracker = MagicMock()
        mock_tracker.handle_withdraw.return_value = {"version": 2}
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            handle_consensus_withdraw_signal(
                "issue-2403",
                {
                    "agent_role": "coder",
                    "slice_id": "slice-2",
                    "reason": (
                        "Withdrawing slice-2 proposal: a reviewer flagged "
                        "an interaction with the sibling slice integration."
                    ),
                },
                Path("/tmp/repo"),
            )

        call = mock_get_tracker.call_args
        assert _pipeline_arg_from_call(call) == "issue-2403"
        assert _slice_arg_from_call(call) == "slice-2"

    @patch("routes.signals._write_consensus_confirmed_marker")
    @patch("routes.signals._resolve_pipeline_phase", return_value="implement")
    @patch("routes.signals._existing_confirmed_for_role", return_value=(False, False))
    @patch("message_store.get_message_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_confirmed_routes_to_slice_tracker(
        self,
        mock_get_tracker,
        mock_get_store,
        _mock_existing,
        _mock_phase,
        _mock_marker,
        app,
    ):
        # Mock message_store.get_message_store and
        # _existing_confirmed_for_role so the handler's "Final CONFIRMED"
        # branch doesn't read or write the live in-memory message store
        # (test hermeticity — repeated runs in the same process must not
        # observe each other's CONSENSUS_CONFIRMED writes).
        #
        # The ``message_store.get_message_store`` patch reaches the
        # handler because ``signals.py`` imports the symbol *inside* the
        # function body (``from message_store import get_message_store``
        # at the call sites). If anyone moves that to a module-level
        # ``from message_store import get_message_store`` at the top of
        # ``signals.py``, this patch silently stops intercepting and the
        # hermeticity guarantee breaks. Switch the patch target to
        # ``routes.signals.get_message_store`` if that refactor lands.
        from routes.signals import handle_consensus_confirmed_signal

        mock_tracker = MagicMock()
        mock_tracker.handle_confirmed.return_value = {
            "status": "confirmed",
            "version": 1,
        }
        mock_get_tracker.return_value = mock_tracker
        mock_get_store.return_value = MagicMock()

        with app.app_context():
            handle_consensus_confirmed_signal(
                "issue-2403",
                {
                    "agent_role": "coder",
                    "slice_id": "slice-2",
                },
                Path("/tmp/repo"),
            )

        call = mock_get_tracker.call_args
        assert _pipeline_arg_from_call(call) == "issue-2403"
        assert _slice_arg_from_call(call) == "slice-2"

    @patch("peer_consensus.get_peer_consensus_tracker")
    @patch("decision_queue.get_decision_queue")
    def test_excuse_producer_routes_to_slice_tracker(self, mock_get_queue, mock_get_tracker, app):
        from models import DecisionStatus
        from routes.signals import handle_consensus_excuse_producer_signal

        # The excuse_producer handler is HITL-gated: short-circuit the
        # decision-queue lookup with a RESOLVED decision scoped to the
        # producer being excused.
        mock_decision = MagicMock()
        mock_decision.status = DecisionStatus.RESOLVED
        mock_decision.context = "failed_role:coder"
        mock_queue = MagicMock()
        mock_queue.get_decision.return_value = mock_decision
        mock_get_queue.return_value = mock_queue

        mock_tracker = MagicMock()
        mock_tracker.excuse_producer.return_value = {"affected_reviewers": []}
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            handle_consensus_excuse_producer_signal(
                "issue-2403",
                {
                    "producer_role": "coder",
                    "slice_id": "slice-2",
                    "decision_id": "decision-1",
                    "reason": "Producer unresponsive after 30m heartbeat gap.",
                },
                Path("/tmp/repo"),
            )

        call = mock_get_tracker.call_args
        assert _pipeline_arg_from_call(call) == "issue-2403"
        assert _slice_arg_from_call(call) == "slice-2"

    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_resolve_obligation_routes_to_slice_tracker(self, mock_get_tracker, app):
        from routes.signals import handle_consensus_resolve_obligation_signal

        mock_tracker = MagicMock()
        mock_tracker.handle_resolve_obligation.return_value = {
            "version": 1,
            "condition": "add coverage",
        }
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            handle_consensus_resolve_obligation_signal(
                "issue-2403",
                {
                    "agent_role": "tester",
                    "reviewer_role": "reviewer_code",
                    "producer_role": "coder",
                    "slice_id": "slice-2",
                    "commit_sha": "deadbee",
                    "note": "Cherry-picked test that covers the strip site.",
                },
                Path("/tmp/repo"),
            )

        call = mock_get_tracker.call_args
        assert _pipeline_arg_from_call(call) == "issue-2403"
        assert _slice_arg_from_call(call) == "slice-2"

    @patch("message_store.get_message_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_producer_push_routes_to_slice_tracker(self, mock_get_tracker, mock_get_store, app):
        # Mock message_store.get_message_store for hermeticity: today the
        # handler's auto re-propose branch is gated by ``auto_re_propose:
        # False`` so the message-bus write is skipped, but the mock pins
        # the test against a future regression where the gate moves.
        from routes.signals import handle_consensus_producer_push_signal

        mock_tracker = MagicMock()
        mock_tracker.handle_producer_push.return_value = {
            "auto_re_propose": False,
            "version": 1,
        }
        mock_get_tracker.return_value = mock_tracker
        mock_get_store.return_value = MagicMock()

        with app.app_context():
            handle_consensus_producer_push_signal(
                "issue-2403",
                {
                    "agent_role": "coder",
                    "slice_id": "slice-2",
                    "commit_sha": "deadbee",
                    "changed_files": ["src/a.py"],
                },
                Path("/tmp/repo"),
            )

        call = mock_get_tracker.call_args
        assert _pipeline_arg_from_call(call) == "issue-2403"
        assert _slice_arg_from_call(call) == "slice-2"


class TestAllConsensusHandlersRejectMalformedSliceId:
    """Defense-in-depth: every handler rejects a malformed ``slice_id`` at the boundary.

    The boundary check lives in ``_extract_slice_id`` — if any handler
    forgets to call it, a path-separator-bearing ``slice_id`` would
    flow through to ``get_peer_consensus_tracker``'s registry key.
    These tests pin every handler against that regression.
    """

    def test_ack_rejects_malformed_slice_id(self, app):
        from routes.signals import handle_consensus_ack_signal

        with app.app_context():
            response, status = handle_consensus_ack_signal(
                "issue-2403",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "slice_id": "../etc/passwd",
                    "payload": {
                        "reason": (
                            "Reviewed work; the diff matches the proposal "
                            "summary and tests cover the new path."
                        ),
                    },
                },
                Path("/tmp/repo"),
            )
        assert status == 400
        assert "slice_id" in response.get_json()["message"]

    def test_nack_rejects_malformed_slice_id(self, app):
        from routes.signals import handle_consensus_nack_signal

        with app.app_context():
            response, status = handle_consensus_nack_signal(
                "issue-2403",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "slice_id": "phase-2",  # legacy shape rejected at signal boundary
                    "payload": {
                        "reason": (
                            "NACK: the diff is missing test coverage for "
                            "the new branch in the orchestrator strip site."
                        ),
                    },
                },
                Path("/tmp/repo"),
            )
        assert status == 400
        assert "slice_id" in response.get_json()["message"]

    def test_withdraw_rejects_malformed_slice_id(self, app):
        from routes.signals import handle_consensus_withdraw_signal

        with app.app_context():
            response, status = handle_consensus_withdraw_signal(
                "issue-2403",
                {
                    "agent_role": "coder",
                    "slice_id": "slice-2/extra",
                    "reason": (
                        "Withdrawing proposal: a reviewer flagged an "
                        "interaction with the sibling slice integration."
                    ),
                },
                Path("/tmp/repo"),
            )
        assert status == 400
        assert "slice_id" in response.get_json()["message"]

    def test_confirmed_rejects_malformed_slice_id(self, app):
        from routes.signals import handle_consensus_confirmed_signal

        with app.app_context():
            response, status = handle_consensus_confirmed_signal(
                "issue-2403",
                {
                    "agent_role": "coder",
                    "slice_id": "../etc",
                },
                Path("/tmp/repo"),
            )
        assert status == 400
        assert "slice_id" in response.get_json()["message"]

    @patch("decision_queue.get_decision_queue")
    def test_excuse_producer_rejects_malformed_slice_id(self, mock_get_queue, app):
        from models import DecisionStatus
        from routes.signals import handle_consensus_excuse_producer_signal

        # The handler validates the HITL gate before slice_id, so a
        # RESOLVED decision still has to short-circuit cleanly to reach
        # the slice_id rejection branch.
        mock_decision = MagicMock()
        mock_decision.status = DecisionStatus.RESOLVED
        mock_decision.context = "failed_role:coder"
        mock_queue = MagicMock()
        mock_queue.get_decision.return_value = mock_decision
        mock_get_queue.return_value = mock_queue

        with app.app_context():
            response, status = handle_consensus_excuse_producer_signal(
                "issue-2403",
                {
                    "producer_role": "coder",
                    "slice_id": "../etc",
                    "decision_id": "decision-1",
                    "reason": "Producer unresponsive after heartbeat gap.",
                },
                Path("/tmp/repo"),
            )
        assert status == 400
        assert "slice_id" in response.get_json()["message"]

    def test_resolve_obligation_rejects_malformed_slice_id(self, app):
        from routes.signals import handle_consensus_resolve_obligation_signal

        with app.app_context():
            response, status = handle_consensus_resolve_obligation_signal(
                "issue-2403",
                {
                    "agent_role": "tester",
                    "reviewer_role": "reviewer_code",
                    "producer_role": "coder",
                    "slice_id": "../etc",
                    "commit_sha": "deadbee",
                    "note": "Cherry-picked test that covers the strip site.",
                },
                Path("/tmp/repo"),
            )
        assert status == 400
        assert "slice_id" in response.get_json()["message"]

    def test_producer_push_rejects_malformed_slice_id(self, app):
        from routes.signals import handle_consensus_producer_push_signal

        with app.app_context():
            response, status = handle_consensus_producer_push_signal(
                "issue-2403",
                {
                    "agent_role": "coder",
                    "slice_id": "slice-2/extra",
                    "commit_sha": "deadbee",
                    "changed_files": ["src/a.py"],
                },
                Path("/tmp/repo"),
            )
        assert status == 400
        assert "slice_id" in response.get_json()["message"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


import pytest  # noqa: E402


@pytest.fixture
def app():
    from flask import Flask
    from routes.signals import signals_bp

    app = Flask(__name__)
    app.register_blueprint(signals_bp)
    return app
