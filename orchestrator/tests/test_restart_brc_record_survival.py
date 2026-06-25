"""Regression tests for #3200 task-7-1 — mid-phase BRC message-record survival.

The #3200 context discipline reseeds/resumes an event-pump agent's session at
re-invocation. After a mid-phase restart, the reseeded session reconstructs its
"queryable environment" by re-pulling the BRC message record (via
``GET /<pipeline_id>/brc-transcript`` + ``read_peer_artifact``) and re-deriving
the #3189 deterministic anchors from it. Both readers serve the **live Redis
message store** (``pipeline:{id}:messages``), so the record must survive the
restart boundary *in Redis* for the re-pull to work.

Mechanism (Option (a) — read the live Redis stream across the restart): the
store is wiped only at phase transitions (``_clear_concurrent_state``) and at
pipeline create/delete (``_clear_pipeline_runtime_state``). The restart handlers
(``restart_agent`` / ``restart_phase``) reset the *peer consensus tracker* (the
ephemeral ACK/NACK bookkeeping) but deliberately leave the message store
untouched. These tests lock that invariant in so a future change that adds a
``get_message_store().clear()`` to the restart path fails loudly.

AC: after a simulated mid-phase restart, the phase's BRC message record
(proposals, verdicts, open NACKs, conditional-ACK obligations) is retrievable;
no message loss across the restart boundary.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# Mock heavy module-level imports pulled in by routes.pipelines (mirrors
# test_brc_history.py / test_restart_agent.py).
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from container_spawner import SpawnedContainer
from message_store import Message, MessageType, get_message_store
from models import (
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

try:
    from flask import Flask
    from routes.pipelines import pipelines_bp

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False

_PIPELINE_ID = "issue-100"
_SLICE_ID = "slice-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_brc_record(store, pipeline_id=_PIPELINE_ID):
    """Seed a realistic in-flight BRC record: a proposal, an ACK verdict, and
    an open NACK with a blocking reason. Returns the messages added."""
    msgs = [
        Message(
            pipeline_id=pipeline_id,
            from_role="coder",
            to_role="all",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="Proposal from coder",
            body="slice-1 implementation",
            phase="implement",
            metadata={"slice_id": _SLICE_ID, "proposal_commit_sha": "abc123"},
        ),
        Message(
            pipeline_id=pipeline_id,
            from_role="reviewer_code",
            to_role="coder",
            message_type=MessageType.CONSENSUS_ACK,
            subject="ACK coder",
            body="looks good",
            phase="implement",
            metadata={"slice_id": _SLICE_ID, "ack_version": 1},
        ),
        Message(
            pipeline_id=pipeline_id,
            from_role="reviewer_contract",
            to_role="coder",
            message_type=MessageType.CONSENSUS_NACK,
            subject="NACK coder",
            body="missing test for the open obligation",
            phase="implement",
            metadata={"slice_id": _SLICE_ID, "nack_version": 1},
        ),
    ]
    for m in msgs:
        store.add_message(m)
    return msgs


def _make_pipeline_with_running_agent(pipeline_id=_PIPELINE_ID, agent_role=AgentRole.CODER):
    """A RUNNING pipeline mid-implement with one running agent (mirrors the
    fixture in test_restart_agent.py)."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=100,
        repo="owner/repo",
        branch="egg/issue-100",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )
    pipeline.phases = {
        "implement": PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            containers=[
                ContainerInfo(
                    container_id="container-abc",
                    container_name=f"egg-issue-100-{agent_role.value}",
                    agent_role=agent_role,
                    status=ContainerStatus.RUNNING,
                ),
            ],
            agents=[
                AgentExecution(
                    role=agent_role,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="container-abc",
                ),
            ],
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
# Route-level guard: the restart endpoint must not lose the message record
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestRestartAgentPreservesBrcRecord:
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_preserves_brc_message_record(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """A mid-phase agent restart leaves the Redis BRC record intact: the
        proposal, the ACK verdict, and the open NACK are all still retrievable
        afterwards, so the reseeded session can re-pull and re-derive anchors."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.restart_agent_container.return_value = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id=_PIPELINE_ID,
            environment={},
        )
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        # Seed the live (fakeredis-backed) message store *before* the restart.
        msg_store = get_message_store()
        seeded = _seed_brc_record(msg_store)
        assert len(msg_store.get_messages(_PIPELINE_ID, limit=100)) == len(seeded)

        response = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/agents/coder/restart",
            json={"reason": "Agent stalled mid-phase"},
        )
        assert response.status_code == 200
        assert response.get_json().get("success") is True

        # The durable record must survive the restart boundary unchanged.
        after = msg_store.get_messages(_PIPELINE_ID, limit=100)
        types_after = sorted(m.message_type for m in after)
        assert types_after == sorted(m.message_type for m in seeded)
        assert MessageType.CONSENSUS_PROPOSE in types_after
        assert MessageType.CONSENSUS_ACK in types_after
        assert MessageType.CONSENSUS_NACK in types_after
        # The open NACK reason (the blocking obligation) is preserved verbatim.
        nack = next(m for m in after if m.message_type == MessageType.CONSENSUS_NACK)
        assert nack.body == "missing test for the open obligation"
        # The last-reviewed SHA carried in the proposal metadata survives, so
        # the #3189 last-reviewed-SHA-per-producer anchor can be re-derived.
        propose = next(m for m in after if m.message_type == MessageType.CONSENSUS_PROPOSE)
        assert propose.metadata.get("proposal_commit_sha") == "abc123"


# ---------------------------------------------------------------------------
# Harness-independent invariant: the consensus-tracker reset that BOTH
# restart_agent and restart_phase perform does not touch the message store.
# ---------------------------------------------------------------------------


class TestConsensusResetPreservesMessageStore:
    def test_clearing_consensus_tracker_leaves_message_store_intact(self):
        """The restart handlers reset the peer consensus tracker (ephemeral
        ACK/NACK bookkeeping) — a store distinct from the Redis message record.
        Clearing the tracker must not drop any BRC messages."""
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:  # pragma: no cover - import shape varies
            pytest.skip("peer_consensus not importable")

        store = get_message_store()
        seeded = _seed_brc_record(store, pipeline_id="issue-777")
        assert len(store.get_messages("issue-777", limit=100)) == len(seeded)

        # Mirror the restart handlers' reset step.
        tracker = get_peer_consensus_tracker("issue-777")
        if tracker is not None:
            tracker.clear()

        after = store.get_messages("issue-777", limit=100)
        assert len(after) == len(seeded)
        assert sorted(m.message_type for m in after) == sorted(m.message_type for m in seeded)
