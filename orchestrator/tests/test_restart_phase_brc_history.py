"""Slice-7 (#3200): the in-flight BRC message record must survive a mid-phase restart.

``_write_brc_history`` persists BRC consensus transcripts only at PHASE
TRANSITIONS today (``complete_phase`` / ``advance_phase``, via
``_persist_phase_brc_history`` — #1827).  ``restart_phase`` tears down a
phase's containers + per-agent worktrees *mid-phase* and does **not**
clear the message store, so the live Redis stream survives a bare phase
restart.  But the durable on-disk transcript (the restart-STABLE
``.egg-state/brc-history/`` artifact the architect's slice calls for) is
never written mid-phase: per-slice ``CONSENSUS_*`` records only land on
disk at slice-PR creation (``_commit_slice_brc_history_to_integration_branch``).
A reseeded post-restart session that re-pulls the *queryable environment*
from disk therefore cannot reconstruct the in-flight proposals / verdicts
/ open NACKs it needs to re-derive the #3189 phase-3 anchors.

These tests pin task-7-2:

* **Data integrity** — the persisted artifact preserves the in-flight
  record (proposals, verdicts, open NACKs) intact and re-readable across
  the restart boundary (``TestInFlightBrcRecordSurvivesToDisk``).
* **Restart wiring** — ``restart_phase`` fires that persistence for the
  in-flight phase, before the destructive worktree/container teardown,
  best-effort (``TestRestartPhasePersistsInFlightBrcHistory``).  These pin
  the *requirement*; the coder (task-7-1) owns the mechanism the architect
  confirms.  A HANDOFF to ``coder`` declares the assumed seam
  (``_persist_phase_brc_history``) so we converge in-cycle.
* **No false record** — absence of any consensus message never fabricates
  a corrupt/empty transcript file ("never a wrong resume").
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from message_store import Message, MessageType  # noqa: E402
from models import (  # noqa: E402
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    PhaseExecution,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from redis_message_store import RedisMessageStore  # noqa: E402

try:
    from agent_salvage import AgentWorktree
    from flask import Flask
    from routes.pipelines import pipelines_bp

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False


# The slice this pipeline restart happens inside of.  task-7-2 cares about
# the per-slice CONSENSUS records carrying ``metadata['slice_id']`` (#2548).
_IN_FLIGHT_SLICE_ID = "slice-7"
_PIPELINE_ID = "issue-3200"
_ISSUE_NUMBER = 3200


def _make_brc_message(
    *,
    from_role,
    message_type,
    subject,
    body="",
    phase="implement",
    slice_id=_IN_FLIGHT_SLICE_ID,
    timestamp=None,
    metadata=None,
):
    """Build a slice-attributed implement-phase BRC ``Message`` for tests."""
    md = dict(metadata or {})
    if slice_id is not None:
        md.setdefault("slice_id", slice_id)
    return Message(
        pipeline_id=_PIPELINE_ID,
        from_role=from_role,
        to_role="all",
        message_type=message_type,
        subject=subject,
        body=body,
        phase=phase,
        timestamp=timestamp or datetime(2026, 6, 25, 7, 33, 0, tzinfo=UTC),
        metadata=md,
    )


def _in_flight_consensus_record():
    """The in-flight slice-7 consensus record: a proposal, an ACK verdict,
    and one still-open NACK — exactly the shape a reseed must re-derive
    the #3189 anchors from (last verdicts + open NACK obligations)."""
    return [
        _make_brc_message(
            from_role="coder",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="task-7-1 mid-phase BRC persistence",
            body="Persist the in-flight record before restart teardown.",
            timestamp=datetime(2026, 6, 25, 7, 33, 0, tzinfo=UTC),
        ),
        _make_brc_message(
            from_role="reviewer_code",
            message_type=MessageType.CONSENSUS_ACK,
            subject="ACK coder",
            body="LGTM",
            timestamp=datetime(2026, 6, 25, 7, 34, 0, tzinfo=UTC),
        ),
        _make_brc_message(
            from_role="reviewer_security",
            message_type=MessageType.CONSENSUS_NACK,
            subject="NACK coder: unvalidated slice_id in restart path",
            body="Validate metadata.slice_id before interpolating into the filename.",
            timestamp=datetime(2026, 6, 25, 7, 35, 0, tzinfo=UTC),
        ),
    ]


# ---------------------------------------------------------------------------
# Data integrity: the persisted artifact preserves the in-flight record.
# These exercise the existing _write_brc_history seam and pass today; they
# lock down WHAT "the message record is intact afterwards" means so a future
# refactor of the restart-time persister cannot silently thin the transcript.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FLASK, reason="orchestrator deps not available")
class TestInFlightBrcRecordSurvivesToDisk:
    """The on-disk transcript must carry the full in-flight consensus record."""

    def _write(self, tmp_path, messages):
        from routes.pipelines import _write_brc_history

        mock_store = MagicMock(spec=RedisMessageStore)
        mock_store.get_messages.return_value = messages
        with patch("message_store.get_message_store", return_value=mock_store):
            # write_per_slice=True so the slice-attributed CONSENSUS records
            # are bucketed to the per-slice file (the artifact a reseeded
            # session re-pulls for THIS slice).
            _write_brc_history(
                tmp_path,
                _PIPELINE_ID,
                "implement",
                _ISSUE_NUMBER,
                write_per_slice=True,
            )
        return (
            tmp_path
            / ".egg-state"
            / "brc-history"
            / f"{_ISSUE_NUMBER}-implement-{_IN_FLIGHT_SLICE_ID}.json"
        )

    def test_consensus_record_round_trips_intact(self, tmp_path):
        """Proposal + ACK + NACK all survive to the JSON companion, re-readable."""
        json_file = self._write(tmp_path, _in_flight_consensus_record())

        assert json_file.exists(), (
            "per-slice BRC JSON companion must be written for the in-flight slice"
        )
        records = json.loads(json_file.read_text())
        by_type = {(r["message_type"], r["from_role"]) for r in records}

        assert ("CONSENSUS_PROPOSE", "coder") in by_type
        assert ("CONSENSUS_ACK", "reviewer_code") in by_type
        assert ("CONSENSUS_NACK", "reviewer_security") in by_type
        # Every record is scoped to the in-flight slice — no cross-slice bleed.
        assert all(r["metadata"].get("slice_id") == _IN_FLIGHT_SLICE_ID for r in records)

    def test_open_nack_obligation_preserved(self, tmp_path):
        """The open NACK (a phase-3 anchor) must be recoverable verbatim."""
        json_file = self._write(tmp_path, _in_flight_consensus_record())
        records = json.loads(json_file.read_text())

        nacks = [r for r in records if r["message_type"] == "CONSENSUS_NACK"]
        assert len(nacks) == 1, "the open NACK must survive the restart boundary"
        assert "slice_id" in nacks[0]["metadata"]
        # The blocking reason body is what a reseed re-derives the obligation
        # from — it must not be dropped or truncated to an empty record.
        assert nacks[0]["body"].strip(), "open-NACK body must be preserved intact"

    def test_markdown_transcript_also_written(self, tmp_path):
        """The human-readable markdown sibling is produced alongside the JSON."""
        json_file = self._write(tmp_path, _in_flight_consensus_record())
        md_file = json_file.with_suffix(".md")

        assert md_file.exists()
        content = md_file.read_text()
        assert "coder" in content
        assert "reviewer_security" in content
        assert "CONSENSUS_NACK" in content

    def test_no_record_yields_no_file(self, tmp_path):
        """An empty store must not fabricate a transcript — never a wrong resume.

        A reseeded session that finds no persisted artifact falls back to a
        cold pull; a *corrupt/empty* artifact would be mistaken for ground
        truth.  ``_write_brc_history`` must no-op rather than write a stub.
        """
        from routes.pipelines import _write_brc_history

        mock_store = MagicMock(spec=RedisMessageStore)
        mock_store.get_messages.return_value = []
        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(
                tmp_path,
                _PIPELINE_ID,
                "implement",
                _ISSUE_NUMBER,
                write_per_slice=True,
            )

        history_dir = tmp_path / ".egg-state" / "brc-history"
        if history_dir.exists():
            assert list(history_dir.iterdir()) == [], (
                "no transcript file should be written when the record is empty"
            )


# ---------------------------------------------------------------------------
# Restart wiring: restart_phase must persist the in-flight phase's record
# before the destructive teardown.  These pin task-7-1's requirement and
# FAIL until the coder wires the persist step (TDD contract pin).  The
# assumed seam (_persist_phase_brc_history, the #1827 helper) is declared to
# `coder` via HANDOFF so we converge in-cycle; if the architect's confirmed
# mechanism differs, these flip to the agreed seam.
# ---------------------------------------------------------------------------


def _make_pipeline_with_slice_agents(
    pipeline_id=_PIPELINE_ID,
    phase=PipelinePhase.IMPLEMENT,
    slice_id=_IN_FLIGHT_SLICE_ID,
    roles=(AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER),
):
    """A slice pipeline mid-implement, mirroring restart-time production state."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=_ISSUE_NUMBER,
        repo="owner/repo",
        branch=f"egg/{pipeline_id}",
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=phase,
    )
    containers = []
    agents = []
    for role in roles:
        container_id = f"{role.value}-{slice_id}-container"
        containers.append(
            ContainerInfo(
                container_id=container_id,
                container_name=f"egg-{pipeline_id}-{slice_id}-{role.value}",
                agent_role=role,
                status=ContainerStatus.RUNNING,
            )
        )
        agents.append(
            AgentExecution(
                role=role,
                status=AgentExecutionStatus.RUNNING,
                container_id=container_id,
                slice_id=slice_id,
            )
        )
    pipeline.phases = {
        phase.value: PhaseExecution(
            phase=phase,
            status=PipelineStatus.RUNNING,
            review_cycles=1,
            containers=containers,
            agents=agents,
        ),
    }
    return pipeline


def _make_agent_worktree(worktree_id, *, agent_role, slice_id=_IN_FLIGHT_SLICE_ID):
    return AgentWorktree(
        worktree_id=worktree_id,
        pipeline_id=_PIPELINE_ID,
        agent_role=agent_role,
        slice_id=slice_id,
        repo_path=Path(f"/var/lib/egg/worktrees/{worktree_id}/repo"),
        local_branch=f"egg/{worktree_id}/work",
    )


@pytest.fixture
def app():
    if not _HAS_FLASK:
        pytest.skip("Flask not available")
    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.mark.skipif(not _HAS_FLASK, reason="orchestrator deps not available")
class TestRestartPhasePersistsInFlightBrcHistory:
    """``restart_phase`` must persist the in-flight phase's BRC record (#3200 slice-7).

    Mirrors the #1827 invariant locked down for ``complete_phase`` /
    ``advance_phase`` in ``test_phase_transition_brc_history.py`` — extended
    to the mid-phase restart path, which is the slice-7 gap.
    """

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines._persist_phase_brc_history")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_persists_in_flight_phase_brc_history(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_persist, mock_thread, client
    ):
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_slice_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)
        mock_spawner_fn.return_value = MagicMock()

        response = client.post(
            "/api/v1/pipelines/issue-3200/phases/implement/restart",
            json={"reason": "mid-phase restart"},
        )

        assert response.status_code == 200
        mock_persist.assert_called_once()
        # Persist must target the in-flight ("implement") phase — persisting
        # any other phase would save a transcript with no in-flight record.
        call = mock_persist.call_args
        phase_arg = call.args[2] if len(call.args) >= 3 else call.kwargs.get("phase")
        assert phase_arg == "implement", (
            f"restart must persist the in-flight phase's record, got phase={phase_arg!r}"
        )

    @patch("routes.pipelines.agent_salvage.enumerate_agent_worktrees")
    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines._persist_phase_brc_history")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_persist_runs_before_worktree_teardown(
        self,
        mock_repo,
        mock_resolve,
        mock_spawner_fn,
        mock_persist,
        mock_thread,
        mock_enumerate,
        client,
    ):
        """The record must be captured BEFORE worktrees/containers are torn down.

        Once the per-agent worktrees are deleted the slice's in-flight state
        is gone; the persist must front-run the teardown so the transcript is
        captured from a still-intact phase (the #1827 persist-before-clear
        invariant, applied to restart).
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_slice_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        mock_enumerate.return_value = [
            _make_agent_worktree("issue-3200-slice-7-coder", agent_role="coder"),
        ]

        call_order: list[str] = []
        mock_persist.side_effect = lambda *a, **k: call_order.append("persist")
        mock_spawner.gateway.delete_worktrees.side_effect = lambda *a, **k: call_order.append(
            "delete_worktree"
        )
        mock_spawner.stop_agent_container.side_effect = lambda *a, **k: call_order.append(
            "stop_container"
        )

        response = client.post(
            "/api/v1/pipelines/issue-3200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        assert "persist" in call_order, "restart must persist the in-flight BRC record"
        first_persist = call_order.index("persist")
        # Persist must precede every destructive teardown step.
        for destructive in ("delete_worktree", "stop_container"):
            if destructive in call_order:
                assert first_persist < call_order.index(destructive), (
                    f"BRC persist must run before {destructive}; order={call_order}"
                )

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines._persist_phase_brc_history")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_persist_failure_is_nonfatal(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_persist, mock_thread, client
    ):
        """A persistence failure must not abort the restart (best-effort, like salvage).

        The restart's job is to recover a wedged phase; a transcript-write
        hiccup must not block that recovery.  The call site must guard the
        persist so an exception is logged and swallowed.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_slice_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)
        mock_spawner_fn.return_value = MagicMock()

        mock_persist.side_effect = RuntimeError("brc-history disk full")

        response = client.post(
            "/api/v1/pipelines/issue-3200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200, (
            "a BRC-persist failure must not block the phase restart"
        )
