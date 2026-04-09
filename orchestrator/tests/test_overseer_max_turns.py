"""
Tests for overseer max_turns configuration and respawn broadcast visibility (issue #1562).

Covers:
- PipelineConfig.overseer_max_turns default value and validation bounds
- spawn_overseer_container max_turns parameter passthrough to build_agent_command
- _check_and_respawn_overseer broadcasts OVERSEER_ALERT on successful respawn
- OVERSEER_ALERT metadata contains required fields (exit_code, old/new container IDs,
  log_tail, respawn_attempt, max_respawns)
- Broadcast is best-effort: skipped gracefully when message_store unavailable
- Broadcast is best-effort: skipped gracefully when broadcast raises exception
- Log tail captured from old container before respawn
- Log tail gracefully falls back to "unavailable" when capture fails
- max_turns from pipeline config passed through at both spawn sites

Related: issue #1562
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup (matches existing test conventions)
# ---------------------------------------------------------------------------
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

# ---------------------------------------------------------------------------
# Conditional imports — skip if modules not yet available
# ---------------------------------------------------------------------------
try:
    from container_spawner import ContainerSpawner, SpawnedContainer
    from docker_client import ContainerNotFoundError
    from gateway_client import GatewayHealth, SessionInfo
    from models import (
        AgentRole,
        ContainerInfo,
        ContainerStatus,
        Pipeline,
        PipelineConfig,
        PipelineStatus,
    )
    from routes.pipelines import _check_and_respawn_overseer
except ImportError as exc:
    pytest.skip(
        f"Required orchestrator modules not available: {exc}",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client."""
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.CONTAINER_PREFIX = "egg-sandbox-"

    mock.create_container.return_value = ContainerInfo(
        container_id="overseer123def456",
        container_name="egg-issue-1562-overseer",
        status=ContainerStatus.PENDING,
    )
    mock.start_container.return_value = ContainerInfo(
        container_id="overseer123def456",
        container_name="egg-issue-1562-overseer",
        status=ContainerStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    mock.stop_container.return_value = ContainerInfo(
        container_id="overseer123def456",
        container_name="egg-issue-1562-overseer",
        status=ContainerStatus.EXITED,
    )
    mock.list_containers.return_value = []
    # Default: log capture returns sample log text
    mock.get_container_logs.return_value = (
        "2026-04-09T13:00:00Z [INFO] monitoring cycle 48\n"
        "2026-04-09T13:00:30Z [INFO] monitoring cycle 49\n"
        "2026-04-09T13:01:00Z [WARN] max_turns approaching limit\n"
    )
    return mock


@pytest.fixture
def mock_gateway_client():
    """Create a mock Gateway client."""
    mock = MagicMock()
    mock.check_health.return_value = GatewayHealth(healthy=True, status="healthy", version="0.1.0")
    mock.register_session.return_value = SessionInfo(
        session_token="overseer-token-1562",
        container_id="overseer123def456",
        container_ip="172.32.0.60",
        mode="public",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    return mock


@pytest.fixture
def spawner(mock_docker_client, mock_gateway_client):
    """Create a ContainerSpawner with mocked clients."""
    return ContainerSpawner(
        docker_client=mock_docker_client,
        gateway_client=mock_gateway_client,
    )


@pytest.fixture
def mock_spawner(mock_docker_client):
    """Mock spawner for _check_and_respawn_overseer tests."""
    mock = MagicMock()
    mock.docker = mock_docker_client
    respawned_id = "overseer-respawned-1562"
    mock.spawn_overseer_container.return_value = SpawnedContainer(
        container_info=ContainerInfo(
            container_id=respawned_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.RUNNING,
            started_at=datetime.now(UTC),
        ),
        session_info=None,
        agent_role=AgentRole.OVERSEER,
        pipeline_id="issue-1562",
        environment={},
    )
    return mock


@pytest.fixture
def mock_store():
    """Mock state store returning a RUNNING pipeline."""
    store = MagicMock()
    store.load_pipeline.return_value = Pipeline(
        id="issue-1562",
        issue_number=1562,
        status=PipelineStatus.RUNNING,
    )
    return store


@pytest.fixture
def running_pipeline():
    """Create a Pipeline in RUNNING state with overseer enabled and max_turns configured."""
    return Pipeline(
        id="issue-1562",
        issue_number=1562,
        status=PipelineStatus.RUNNING,
        config=PipelineConfig(
            overseer_enabled=True,
            overseer_max_respawns=3,
            overseer_max_turns=2000,
        ),
    )


def _make_config(**overrides) -> PipelineConfig:
    """Build a PipelineConfig with test defaults."""
    defaults = {
        "overseer_enabled": True,
        "overseer_poll_interval_seconds": 30,
        "overseer_decision_maker_model": "sonnet",
        "overseer_max_respawns": 3,
        "overseer_max_turns": 2000,
    }
    defaults.update(overrides)
    return PipelineConfig(**defaults)


# ---------------------------------------------------------------------------
# Scenario 1: PipelineConfig.overseer_max_turns defaults and bounds
# ---------------------------------------------------------------------------


class TestOverseerMaxTurnsConfig:
    """Verify overseer_max_turns field on PipelineConfig."""

    def test_default_value_is_2000(self):
        """Default overseer_max_turns is 2000."""
        config = PipelineConfig()
        assert config.overseer_max_turns == 2000

    def test_custom_value_accepted(self):
        """Custom overseer_max_turns within bounds is accepted."""
        config = PipelineConfig(overseer_max_turns=5000)
        assert config.overseer_max_turns == 5000

    def test_minimum_bound_100(self):
        """overseer_max_turns=100 is the minimum allowed value."""
        config = PipelineConfig(overseer_max_turns=100)
        assert config.overseer_max_turns == 100

    def test_maximum_bound_10000(self):
        """overseer_max_turns=10000 is the maximum allowed value."""
        config = PipelineConfig(overseer_max_turns=10000)
        assert config.overseer_max_turns == 10000

    def test_rejects_below_minimum(self):
        """Values below 100 are rejected by validation."""
        with pytest.raises(ValueError):
            PipelineConfig(overseer_max_turns=99)

    def test_rejects_above_maximum(self):
        """Values above 10000 are rejected by validation."""
        with pytest.raises(ValueError):
            PipelineConfig(overseer_max_turns=10001)

    def test_rejects_zero(self):
        """Zero is below the minimum and rejected."""
        with pytest.raises(ValueError):
            PipelineConfig(overseer_max_turns=0)

    def test_rejects_negative(self):
        """Negative values are rejected."""
        with pytest.raises(ValueError):
            PipelineConfig(overseer_max_turns=-1)

    def test_field_coexists_with_other_overseer_fields(self):
        """overseer_max_turns does not interfere with existing overseer config fields."""
        config = PipelineConfig(
            overseer_enabled=True,
            overseer_max_turns=3000,
            overseer_max_respawns=5,
            overseer_poll_interval_seconds=15,
            overseer_decision_maker_model="opus",
        )
        assert config.overseer_max_turns == 3000
        assert config.overseer_max_respawns == 5
        assert config.overseer_poll_interval_seconds == 15
        assert config.overseer_decision_maker_model == "opus"


# ---------------------------------------------------------------------------
# Scenario 2: spawn_overseer_container max_turns passthrough
# ---------------------------------------------------------------------------


class TestSpawnOverseerMaxTurns:
    """Verify spawn_overseer_container passes max_turns to build_agent_command."""

    def test_default_max_turns_in_command(self, spawner, mock_docker_client):
        """Default max_turns=2000 appears in the agent command."""
        spawner.spawn_overseer_container(
            pipeline_id="issue-1562",
            issue_number=1562,
        )

        create_call = mock_docker_client.create_container.call_args
        command = create_call.kwargs.get("command", [])
        # Command should contain --max-turns 2000
        max_turns_idx = command.index("--max-turns")
        assert command[max_turns_idx + 1] == "2000", (
            f"Expected --max-turns 2000, got --max-turns {command[max_turns_idx + 1]}"
        )

    def test_custom_max_turns_in_command(self, spawner, mock_docker_client):
        """Custom max_turns value appears in the agent command."""
        spawner.spawn_overseer_container(
            pipeline_id="issue-1562",
            issue_number=1562,
            max_turns=5000,
        )

        create_call = mock_docker_client.create_container.call_args
        command = create_call.kwargs.get("command", [])
        max_turns_idx = command.index("--max-turns")
        assert command[max_turns_idx + 1] == "5000", (
            f"Expected --max-turns 5000, got --max-turns {command[max_turns_idx + 1]}"
        )

    def test_max_turns_no_longer_hardcoded_500(self, spawner, mock_docker_client):
        """The old hardcoded 500 value is no longer used by default."""
        spawner.spawn_overseer_container(
            pipeline_id="issue-1562-no500",
            issue_number=1562,
        )

        create_call = mock_docker_client.create_container.call_args
        command = create_call.kwargs.get("command", [])
        max_turns_idx = command.index("--max-turns")
        assert command[max_turns_idx + 1] != "500", "max_turns should no longer default to 500"

    def test_max_turns_100_minimum(self, spawner, mock_docker_client):
        """Passing max_turns=100 (minimum config value) works in spawn."""
        spawner.spawn_overseer_container(
            pipeline_id="issue-1562-min",
            issue_number=1562,
            max_turns=100,
        )

        create_call = mock_docker_client.create_container.call_args
        command = create_call.kwargs.get("command", [])
        max_turns_idx = command.index("--max-turns")
        assert command[max_turns_idx + 1] == "100"


# ---------------------------------------------------------------------------
# Scenario 3: _check_and_respawn_overseer passes max_turns from config
# ---------------------------------------------------------------------------


class TestRespawnPassesMaxTurns:
    """Verify _check_and_respawn_overseer passes max_turns from pipeline config."""

    def test_respawn_uses_config_max_turns(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """Respawn call includes max_turns from pipeline.config.overseer_max_turns."""
        original_id = "overseer-original-1562"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1562",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        call_kwargs = mock_spawner.spawn_overseer_container.call_args.kwargs
        assert call_kwargs["max_turns"] == 2000, (
            "spawn_overseer_container should receive max_turns from pipeline config"
        )

    def test_respawn_uses_custom_max_turns(self, mock_spawner, mock_docker_client, mock_store):
        """Respawn uses a non-default max_turns value from pipeline config."""
        pipeline = Pipeline(
            id="issue-1562",
            issue_number=1562,
            status=PipelineStatus.RUNNING,
            config=PipelineConfig(
                overseer_max_turns=8000,
                overseer_max_respawns=3,
            ),
        )
        original_id = "overseer-custom-1562"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1562",
            pipeline=pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        call_kwargs = mock_spawner.spawn_overseer_container.call_args.kwargs
        assert call_kwargs["max_turns"] == 8000


# ---------------------------------------------------------------------------
# Scenario 4: OVERSEER_ALERT broadcast on respawn
# ---------------------------------------------------------------------------


class TestOverseerAlertBroadcast:
    """Verify OVERSEER_ALERT is broadcast with correct metadata on respawn."""

    def test_broadcast_on_successful_respawn(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """OVERSEER_ALERT message is broadcast after successful respawn."""
        original_id = "overseer-original-bcast"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        mock_msg_store = MagicMock()
        mock_store_fn = MagicMock(return_value=mock_msg_store)

        with patch("routes.pipelines._get_message_store", return_value=mock_store_fn):
            _check_and_respawn_overseer(
                spawner=mock_spawner,
                store=mock_store,
                pipeline_id="issue-1562",
                pipeline=running_pipeline,
                overseer_container_id=original_id,
                overseer_respawn_count=0,
                max_overseer_respawns=3,
                gateway_mode="public",
                pipeline_repos=None,
                certs_volume=None,
            )

        mock_msg_store.add_message.assert_called_once()
        msg = mock_msg_store.add_message.call_args[0][0]
        assert msg.message_type == "OVERSEER_ALERT"
        assert msg.pipeline_id == "issue-1562"
        assert msg.from_role == "orchestrator"
        assert msg.to_role == "all"
        assert "overseer_restart" in msg.subject

    def test_broadcast_metadata_contains_required_fields(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """OVERSEER_ALERT metadata contains all required diagnostic fields."""
        original_id = "overseer-meta-check"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=137,
        )

        mock_msg_store = MagicMock()
        mock_store_fn = MagicMock(return_value=mock_msg_store)

        with patch("routes.pipelines._get_message_store", return_value=mock_store_fn):
            _check_and_respawn_overseer(
                spawner=mock_spawner,
                store=mock_store,
                pipeline_id="issue-1562",
                pipeline=running_pipeline,
                overseer_container_id=original_id,
                overseer_respawn_count=0,
                max_overseer_respawns=3,
                gateway_mode="public",
                pipeline_repos=None,
                certs_volume=None,
            )

        msg = mock_msg_store.add_message.call_args[0][0]
        metadata = msg.metadata

        # Required metadata fields per task-1-4
        assert "exit_code" in metadata
        assert metadata["exit_code"] == 137
        assert "old_container_id" in metadata
        assert metadata["old_container_id"] == original_id
        assert "new_container_id" in metadata
        assert metadata["new_container_id"] == "overseer-respawned-1562"
        assert "log_tail" in metadata
        assert "respawn_attempt" in metadata
        assert metadata["respawn_attempt"] == 1
        assert "max_respawns" in metadata
        assert metadata["max_respawns"] == 3

    def test_broadcast_includes_log_tail(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """OVERSEER_ALERT metadata includes captured log tail from old container."""
        original_id = "overseer-logs-check"
        expected_logs = "2026-04-09T13:00:00Z [INFO] last log line\n"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )
        mock_docker_client.get_container_logs.return_value = expected_logs

        mock_msg_store = MagicMock()
        mock_store_fn = MagicMock(return_value=mock_msg_store)

        with patch("routes.pipelines._get_message_store", return_value=mock_store_fn):
            _check_and_respawn_overseer(
                spawner=mock_spawner,
                store=mock_store,
                pipeline_id="issue-1562",
                pipeline=running_pipeline,
                overseer_container_id=original_id,
                overseer_respawn_count=0,
                max_overseer_respawns=3,
                gateway_mode="public",
                pipeline_repos=None,
                certs_volume=None,
            )

        msg = mock_msg_store.add_message.call_args[0][0]
        assert msg.metadata["log_tail"] == expected_logs

    def test_broadcast_includes_phase(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """OVERSEER_ALERT message includes the pipeline's current phase."""
        original_id = "overseer-phase-check"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        mock_msg_store = MagicMock()
        mock_store_fn = MagicMock(return_value=mock_msg_store)

        with patch("routes.pipelines._get_message_store", return_value=mock_store_fn):
            _check_and_respawn_overseer(
                spawner=mock_spawner,
                store=mock_store,
                pipeline_id="issue-1562",
                pipeline=running_pipeline,
                overseer_container_id=original_id,
                overseer_respawn_count=0,
                max_overseer_respawns=3,
                gateway_mode="public",
                pipeline_repos=None,
                certs_volume=None,
            )

        msg = mock_msg_store.add_message.call_args[0][0]
        assert msg.phase is not None, "Message should include pipeline phase"

    def test_broadcast_exit_code_none_for_vanished_container(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """When container vanishes (ContainerNotFoundError), exit_code is None in metadata."""
        original_id = "overseer-vanished-bcast"
        mock_docker_client.get_container_info.side_effect = ContainerNotFoundError(
            f"Container {original_id} not found"
        )

        mock_msg_store = MagicMock()
        mock_store_fn = MagicMock(return_value=mock_msg_store)

        with patch("routes.pipelines._get_message_store", return_value=mock_store_fn):
            _check_and_respawn_overseer(
                spawner=mock_spawner,
                store=mock_store,
                pipeline_id="issue-1562",
                pipeline=running_pipeline,
                overseer_container_id=original_id,
                overseer_respawn_count=0,
                max_overseer_respawns=3,
                gateway_mode="public",
                pipeline_repos=None,
                certs_volume=None,
            )

        msg = mock_msg_store.add_message.call_args[0][0]
        assert msg.metadata["exit_code"] is None, (
            "exit_code should be None for ContainerNotFoundError"
        )

    def test_respawn_still_succeeds_when_broadcast_fails(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """Respawn succeeds and returns new container ID even when broadcast raises."""
        original_id = "overseer-bcast-fail"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        # Make _get_message_store raise an exception
        with patch(
            "routes.pipelines._get_message_store",
            side_effect=RuntimeError("import failed"),
        ):
            new_id, new_count = _check_and_respawn_overseer(
                spawner=mock_spawner,
                store=mock_store,
                pipeline_id="issue-1562",
                pipeline=running_pipeline,
                overseer_container_id=original_id,
                overseer_respawn_count=0,
                max_overseer_respawns=3,
                gateway_mode="public",
                pipeline_repos=None,
                certs_volume=None,
            )

        # Respawn should still succeed
        assert new_id == "overseer-respawned-1562", "Respawn must succeed even when broadcast fails"
        assert new_count == 1


# ---------------------------------------------------------------------------
# Scenario 5: Broadcast graceful fallback — message_store unavailable
# ---------------------------------------------------------------------------


class TestBroadcastGracefulFallback:
    """Verify broadcast is skipped gracefully when message_store is unavailable."""

    def test_broadcast_skipped_when_store_returns_none(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """When _get_message_store() returns None, broadcast is silently skipped."""
        original_id = "overseer-no-store"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        with patch("routes.pipelines._get_message_store", return_value=None):
            new_id, new_count = _check_and_respawn_overseer(
                spawner=mock_spawner,
                store=mock_store,
                pipeline_id="issue-1562",
                pipeline=running_pipeline,
                overseer_container_id=original_id,
                overseer_respawn_count=0,
                max_overseer_respawns=3,
                gateway_mode="public",
                pipeline_repos=None,
                certs_volume=None,
            )

        # Respawn still succeeds
        assert new_id == "overseer-respawned-1562"
        assert new_count == 1

    def test_broadcast_skipped_when_add_message_raises(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """Respawn succeeds even when add_message raises an exception."""
        original_id = "overseer-msg-fail"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        mock_msg_store = MagicMock()
        mock_msg_store.add_message.side_effect = RuntimeError("Redis connection failed")
        mock_store_fn = MagicMock(return_value=mock_msg_store)

        with patch("routes.pipelines._get_message_store", return_value=mock_store_fn):
            new_id, new_count = _check_and_respawn_overseer(
                spawner=mock_spawner,
                store=mock_store,
                pipeline_id="issue-1562",
                pipeline=running_pipeline,
                overseer_container_id=original_id,
                overseer_respawn_count=0,
                max_overseer_respawns=3,
                gateway_mode="public",
                pipeline_repos=None,
                certs_volume=None,
            )

        # Respawn must still succeed
        assert new_id == "overseer-respawned-1562"
        assert new_count == 1


# ---------------------------------------------------------------------------
# Scenario 6: Log tail capture
# ---------------------------------------------------------------------------


class TestLogTailCapture:
    """Verify log tail is captured from old container before respawn."""

    def test_log_tail_captured_with_tail_20(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """get_container_logs is called with tail=20 on the old container."""
        original_id = "overseer-log-capture"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1562",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        mock_docker_client.get_container_logs.assert_called_once_with(original_id, tail=20)

    def test_log_tail_fallback_on_exception(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """Log tail falls back to 'unavailable' when get_container_logs raises."""
        original_id = "overseer-log-fail"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )
        mock_docker_client.get_container_logs.side_effect = ContainerNotFoundError(
            "Container purged"
        )

        mock_msg_store = MagicMock()
        mock_store_fn = MagicMock(return_value=mock_msg_store)

        with patch("routes.pipelines._get_message_store", return_value=mock_store_fn):
            new_id, new_count = _check_and_respawn_overseer(
                spawner=mock_spawner,
                store=mock_store,
                pipeline_id="issue-1562",
                pipeline=running_pipeline,
                overseer_container_id=original_id,
                overseer_respawn_count=0,
                max_overseer_respawns=3,
                gateway_mode="public",
                pipeline_repos=None,
                certs_volume=None,
            )

        # Respawn still succeeds
        assert new_id == "overseer-respawned-1562"
        assert new_count == 1
        # Log tail should be "unavailable" in metadata
        msg = mock_msg_store.add_message.call_args[0][0]
        assert msg.metadata["log_tail"] == "unavailable"

    def test_log_tail_fallback_on_generic_exception(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """Log tail falls back to 'unavailable' on any exception, not just ContainerNotFoundError."""
        original_id = "overseer-log-generic-fail"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )
        mock_docker_client.get_container_logs.side_effect = RuntimeError("Docker daemon error")

        mock_msg_store = MagicMock()
        mock_store_fn = MagicMock(return_value=mock_msg_store)

        with patch("routes.pipelines._get_message_store", return_value=mock_store_fn):
            new_id, new_count = _check_and_respawn_overseer(
                spawner=mock_spawner,
                store=mock_store,
                pipeline_id="issue-1562",
                pipeline=running_pipeline,
                overseer_container_id=original_id,
                overseer_respawn_count=0,
                max_overseer_respawns=3,
                gateway_mode="public",
                pipeline_repos=None,
                certs_volume=None,
            )

        assert new_id == "overseer-respawned-1562"
        msg = mock_msg_store.add_message.call_args[0][0]
        assert msg.metadata["log_tail"] == "unavailable"

    def test_log_capture_does_not_block_respawn(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """Even if log capture fails, respawn proceeds without delay."""
        original_id = "overseer-log-nonblocking"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=1,
        )
        mock_docker_client.get_container_logs.side_effect = Exception("timeout")

        new_id, new_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1562",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert new_id == "overseer-respawned-1562", "Respawn must succeed despite log failure"
        assert new_count == 1
        mock_spawner.spawn_overseer_container.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario 7: No broadcast when respawn not needed
# ---------------------------------------------------------------------------


class TestNoBroadcastWithoutRespawn:
    """Verify no OVERSEER_ALERT is broadcast when respawn is not triggered."""

    def test_no_broadcast_when_container_running(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """No broadcast when overseer container is still running."""
        original_id = "overseer-running-ok"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.RUNNING,
            started_at=datetime.now(UTC),
        )

        mock_msg_store = MagicMock()
        mock_store_fn = MagicMock(return_value=mock_msg_store)

        with patch("routes.pipelines._get_message_store", return_value=mock_store_fn):
            _check_and_respawn_overseer(
                spawner=mock_spawner,
                store=mock_store,
                pipeline_id="issue-1562",
                pipeline=running_pipeline,
                overseer_container_id=original_id,
                overseer_respawn_count=0,
                max_overseer_respawns=3,
                gateway_mode="public",
                pipeline_repos=None,
                certs_volume=None,
            )

        mock_msg_store.add_message.assert_not_called()

    def test_no_broadcast_when_pipeline_terminal(
        self, mock_spawner, mock_docker_client, running_pipeline
    ):
        """No broadcast when pipeline is in terminal state (no respawn happens)."""
        original_id = "overseer-terminal-nobcast"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        terminal_store = MagicMock()
        terminal_store.load_pipeline.return_value = Pipeline(
            id="issue-1562",
            issue_number=1562,
            status=PipelineStatus.COMPLETE,
        )

        mock_msg_store = MagicMock()
        mock_store_fn = MagicMock(return_value=mock_msg_store)

        with patch("routes.pipelines._get_message_store", return_value=mock_store_fn):
            _check_and_respawn_overseer(
                spawner=mock_spawner,
                store=terminal_store,
                pipeline_id="issue-1562",
                pipeline=running_pipeline,
                overseer_container_id=original_id,
                overseer_respawn_count=0,
                max_overseer_respawns=3,
                gateway_mode="public",
                pipeline_repos=None,
                certs_volume=None,
            )

        mock_msg_store.add_message.assert_not_called()

    def test_no_log_capture_when_container_running(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """No log capture when container is still running (no respawn needed)."""
        original_id = "overseer-running-nologs"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.RUNNING,
            started_at=datetime.now(UTC),
        )

        _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1562",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        mock_docker_client.get_container_logs.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 8: Existing tests still pass with new parameter
# ---------------------------------------------------------------------------


class TestBackwardsCompatibility:
    """Verify existing respawn behaviour is unchanged with new parameters."""

    def test_existing_respawn_call_works_without_max_turns(
        self, mock_spawner, mock_docker_client, mock_store
    ):
        """_check_and_respawn_overseer works with pipeline that has default max_turns."""
        # Pipeline with all defaults (overseer_max_turns=2000 by default)
        pipeline = Pipeline(
            id="issue-1562",
            issue_number=1562,
            status=PipelineStatus.RUNNING,
            config=PipelineConfig(),
        )
        original_id = "overseer-compat"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1562-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        new_id, new_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1562",
            pipeline=pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert new_id == "overseer-respawned-1562"
        assert new_count == 1
        # Verify max_turns=2000 is passed (default from config)
        call_kwargs = mock_spawner.spawn_overseer_container.call_args.kwargs
        assert call_kwargs["max_turns"] == 2000

    def test_spawn_overseer_still_works_without_explicit_max_turns(
        self, spawner, mock_docker_client
    ):
        """spawn_overseer_container works without explicit max_turns (uses default)."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-1562-compat",
            issue_number=1562,
        )
        assert isinstance(result, SpawnedContainer)
        assert result.agent_role == AgentRole.OVERSEER

        # Verify default max_turns=2000 in command
        create_call = mock_docker_client.create_container.call_args
        command = create_call.kwargs.get("command", [])
        max_turns_idx = command.index("--max-turns")
        assert command[max_turns_idx + 1] == "2000"
