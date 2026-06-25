"""Slice-7 (#3200): the in-flight BRC message record must survive a mid-phase restart.

**Converged mechanism (task-7-1, commit 6360107b1 — option (a)).**  The
durable BRC record that a reseeded post-restart session re-pulls is the
**live Redis message store** (``pipeline:{id}:messages``).  ``restart_phase``
resets the *peer consensus tracker* (ephemeral ACK/NACK bookkeeping) but
deliberately leaves the message store untouched — the store is cleared only
at phase transitions (``_clear_concurrent_state``) and pipeline
create/delete, never on restart (see ``routes/pipelines.py`` restart_phase,
"Do NOT add ``get_message_store().clear()`` here").  A reseeded session
re-pulls the surviving record via ``/brc-transcript`` + ``read_peer_artifact``
and re-derives the #3189 anchors from it.

``restart_phase`` also makes a **best-effort, non-load-bearing** call to
``_persist_phase_brc_history`` before teardown (cold-start hardening against
a full Redis loss / orchestrator pod death — task-6-1's case).  That persist
uses ``write_per_slice=False`` (it must not duplicate the per-slice files
owned by the slice integration branch — #2755), so it does **not** capture
the in-flight *per-slice* CONSENSUS records.  Survival of the in-flight
slice record is therefore carried by Redis (option (a)), **not** by the
disk-persist.  These tests assert the load-bearing mechanism (live-store
survival) and the seam that backs the cold-start hardening; they do not pin
the disk-persist as the slice record's survival path.

Coverage map (task-7-2):

* **Live-store survival (the AC)** — a real mid-phase ``restart_phase`` leaves
  the in-flight consensus record (proposal + ACK verdict + open NACK +
  ``proposal_commit_sha`` metadata) retrievable from the live message store
  afterwards (``TestRestartPhasePreservesLiveBrcRecord``).  This is the
  ``restart_phase`` counterpart to the coder's ``restart_agent`` coverage in
  ``test_restart_brc_record_survival.py`` (complementary, not duplicate).
* **Persisted-artifact shape** — when a transcript *is* written, it preserves
  the record (proposals, verdicts, open NACKs) intact and re-readable
  (``TestInFlightBrcRecordSurvivesToDisk``, the ``_write_brc_history`` seam
  regression guard).
* **No false record** — absence of any consensus message never fabricates a
  corrupt/empty transcript file ("never a wrong resume").
* **Best-effort guard** — a persist hiccup never blocks the restart's job of
  recovering a wedged phase (``TestRestartPhasePersistGuardIsNonFatal``).
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

from message_store import Message, MessageType, get_message_store  # noqa: E402
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
    to_role="all",
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
        to_role=to_role,
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
    the #3189 anchors from (last verdicts + open NACK obligations).

    The proposal carries ``proposal_commit_sha`` so the
    last-reviewed-SHA-per-producer anchor is re-derivable from the survivors.
    """
    return [
        _make_brc_message(
            from_role="coder",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="task-7-1 mid-phase BRC persistence",
            body="Persist the in-flight record before restart teardown.",
            metadata={"proposal_commit_sha": "abc1234"},
            timestamp=datetime(2026, 6, 25, 7, 33, 0, tzinfo=UTC),
        ),
        _make_brc_message(
            from_role="reviewer_code",
            message_type=MessageType.CONSENSUS_ACK,
            subject="ACK coder",
            body="LGTM",
            to_role="coder",
            metadata={"ack_version": 1},
            timestamp=datetime(2026, 6, 25, 7, 34, 0, tzinfo=UTC),
        ),
        _make_brc_message(
            from_role="reviewer_security",
            message_type=MessageType.CONSENSUS_NACK,
            subject="NACK coder: unvalidated slice_id in restart path",
            body="Validate metadata.slice_id before interpolating into the filename.",
            to_role="coder",
            metadata={"nack_version": 1},
            timestamp=datetime(2026, 6, 25, 7, 35, 0, tzinfo=UTC),
        ),
    ]


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


# ---------------------------------------------------------------------------
# Live-store survival (the AC): a real mid-phase ``restart_phase`` leaves the
# in-flight BRC record intact in the live message store.  This is the
# load-bearing option-(a) mechanism the reseed depends on.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FLASK, reason="orchestrator deps not available")
class TestRestartPhasePreservesLiveBrcRecord:
    """``restart_phase`` must NOT drop the live BRC message record (#3200 slice-7).

    Drives a real mid-phase phase-restart and asserts the proposal, the ACK
    verdict, the open NACK, and the proposal's ``proposal_commit_sha`` are all
    still retrievable from the live message store afterwards — so the reseeded
    session can re-pull them and re-derive the #3189 anchors.  The
    ``restart_agent`` counterpart lives in ``test_restart_brc_record_survival.py``
    (coder, task-7-1); this covers the phase-restart path specifically.
    """

    def _seed_live_record(self):
        store = get_message_store()
        seeded = _in_flight_consensus_record()
        for m in seeded:
            store.add_message(m)
        # Guard the precondition: the live store actually holds the record.
        assert len(store.get_messages(_PIPELINE_ID, limit=100)) >= len(seeded)
        return store, seeded

    @patch("routes.pipelines.agent_salvage.enumerate_agent_worktrees")
    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines._persist_phase_brc_history")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_preserves_live_brc_record(
        self,
        mock_repo,
        mock_resolve,
        mock_spawner_fn,
        mock_persist,
        mock_thread,
        mock_enumerate,
        client,
    ):
        # ``_persist_phase_brc_history`` is mocked out: option (a) survival does
        # not depend on the (best-effort, non-load-bearing) disk-persist, and
        # mocking it keeps the test off the filesystem/git path.  The assertion
        # is purely that restart_phase leaves the *live store* intact.
        mock_repo.return_value = "/repo"
        mock_enumerate.return_value = []
        pipeline = _make_pipeline_with_slice_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)
        mock_spawner_fn.return_value = MagicMock()

        live_store, seeded = self._seed_live_record()

        response = client.post(
            "/api/v1/pipelines/issue-3200/phases/implement/restart",
            json={"reason": "mid-phase restart"},
        )
        assert response.status_code == 200

        # The live BRC record must survive the restart boundary unchanged: the
        # seeded message types are a subset of the survivors.
        after = live_store.get_messages(_PIPELINE_ID, limit=100)
        types_after = {m.message_type for m in after}
        for seeded_msg in seeded:
            assert seeded_msg.message_type in types_after
        assert MessageType.CONSENSUS_PROPOSE in types_after
        assert MessageType.CONSENSUS_ACK in types_after
        assert MessageType.CONSENSUS_NACK in types_after

        # The open NACK obligation (a phase-3 anchor) survives verbatim.
        nack = next(m for m in after if m.message_type == MessageType.CONSENSUS_NACK)
        assert nack.body.strip(), "open-NACK body must be preserved intact"

        # The proposal's reviewed-SHA survives, so the #3189
        # last-reviewed-SHA-per-producer anchor is re-derivable.
        propose = next(m for m in after if m.message_type == MessageType.CONSENSUS_PROPOSE)
        assert propose.metadata.get("proposal_commit_sha") == "abc1234"

    @patch("routes.pipelines.agent_salvage.enumerate_agent_worktrees")
    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines._persist_phase_brc_history")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_does_not_clear_message_store(
        self,
        mock_repo,
        mock_resolve,
        mock_spawner_fn,
        mock_persist,
        mock_thread,
        mock_enumerate,
        client,
    ):
        """The restart path must never call ``get_message_store().clear()``.

        The store is wiped only at phase transitions / pipeline create+delete;
        a regression that added a ``clear`` to the restart route would silently
        destroy the in-flight record the reseed depends on.
        """
        mock_repo.return_value = "/repo"
        mock_enumerate.return_value = []
        pipeline = _make_pipeline_with_slice_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)
        mock_spawner_fn.return_value = MagicMock()

        live_store, seeded = self._seed_live_record()

        with patch.object(type(live_store), "clear", autospec=True) as mock_clear:
            response = client.post(
                "/api/v1/pipelines/issue-3200/phases/implement/restart",
                json={},
            )
            assert response.status_code == 200
            mock_clear.assert_not_called()

        assert len(live_store.get_messages(_PIPELINE_ID, limit=100)) >= len(seeded)


# ---------------------------------------------------------------------------
# Persisted-artifact shape: when a transcript IS written (the cold-start
# hardening seam, or any phase-transition persist), it preserves the in-flight
# record intact.  These exercise the existing ``_write_brc_history`` seam and
# pass today; they lock down WHAT "the message record is intact afterwards"
# means so a future refactor of the persister cannot silently thin the
# transcript.  They do NOT assert restart_phase wires the persist as the
# survival path — option (a) (live store) carries that.
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
# Best-effort guard: the cold-start persist that ``restart_phase`` fires
# before teardown is wrapped so a persist failure never blocks recovery of a
# wedged phase.  This exercises the REAL wired call site (restart_phase ->
# _persist_phase_brc_history, guarded by try/except) — it does not assert the
# persist is the record-survival mechanism (option (a) / the live store is).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FLASK, reason="orchestrator deps not available")
class TestRestartPhasePersistGuardIsNonFatal:
    """A persist hiccup must not abort the restart (best-effort, like salvage)."""

    @patch("routes.pipelines.agent_salvage.enumerate_agent_worktrees")
    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines._persist_phase_brc_history")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_persist_failure_is_nonfatal(
        self,
        mock_repo,
        mock_resolve,
        mock_spawner_fn,
        mock_persist,
        mock_thread,
        mock_enumerate,
        client,
    ):
        """The restart's job is to recover a wedged phase; a transcript-write
        hiccup in the best-effort cold-start persist must be logged and
        swallowed, not propagated.  ``restart_phase`` invokes
        ``_persist_phase_brc_history`` inside a try/except before teardown —
        making it raise must still yield a 200."""
        mock_repo.return_value = "/repo"
        mock_enumerate.return_value = []
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
        # The guarded persist was actually reached (else the test is vacuous).
        mock_persist.assert_called_once()
