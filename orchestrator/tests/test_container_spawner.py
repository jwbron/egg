"""
Tests for container spawner with gateway integration.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from container_spawner import (
    ContainerSpawner,
    ContainerSpawnError,
    SpawnedContainer,
    _compute_allowed_files,
    _host_to_local_volumes,
    get_container_spawner,
)
from docker_client import ContainerOperationError
from gateway_client import GatewayError, GatewayHealth, SessionInfo
from models import AgentRole, ContainerInfo, ContainerStatus


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client."""
    mock = MagicMock()
    mock.is_connected.return_value = True

    # Default create_container behavior
    mock.create_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-issue-123-coder",
        status=ContainerStatus.PENDING,
    )

    # Default start_container behavior
    mock.start_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-issue-123-coder",
        status=ContainerStatus.RUNNING,
        started_at=datetime.utcnow(),
    )

    # Default list_containers behavior
    mock.list_containers.return_value = []

    return mock


@pytest.fixture
def mock_gateway_client():
    """Create a mock Gateway client."""
    mock = MagicMock()

    # Default health check
    mock.check_health.return_value = GatewayHealth(
        healthy=True,
        status="healthy",
        version="0.1.0",
    )

    # Default session registration
    mock.register_session.return_value = SessionInfo(
        session_token="test-token-12345",
        container_id="abc123def456",
        container_ip="172.32.0.50",
        mode="public",
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )

    return mock


@pytest.fixture
def spawner(mock_docker_client, mock_gateway_client):
    """Create a container spawner with mocked clients."""
    return ContainerSpawner(
        docker_client=mock_docker_client,
        gateway_client=mock_gateway_client,
    )


class TestContainerSpawnerBasics:
    """Basic spawner tests."""

    def test_lazy_client_initialization(self):
        """Test that clients are lazily initialized."""
        spawner = ContainerSpawner()
        # Clients should not be initialized yet
        assert spawner._docker is None
        assert spawner._gateway is None

    def test_explicit_client_initialization(self, mock_docker_client, mock_gateway_client):
        """Test explicit client initialization."""
        spawner = ContainerSpawner(
            docker_client=mock_docker_client,
            gateway_client=mock_gateway_client,
        )

        assert spawner.docker is mock_docker_client
        assert spawner.gateway is mock_gateway_client


class TestSpawnAgentContainer:
    """Tests for spawning agent containers."""

    def test_spawn_coder_container(self, spawner, mock_docker_client, mock_gateway_client):
        """Test spawning a coder container."""
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            mode="public",
        )

        assert isinstance(result, SpawnedContainer)
        assert result.agent_role == AgentRole.CODER
        assert result.pipeline_id == "issue-123"
        assert result.session_info is not None
        assert result.session_info.session_token == "test-token-12345"

        # Verify Docker client calls
        assert mock_docker_client.create_container.called
        assert mock_docker_client.start_container.called

        # Verify gateway registration
        mock_gateway_client.register_session.assert_called()

        # Verify session is updated with actual Docker container ID
        mock_gateway_client.update_session.assert_called_once()
        update_call = mock_gateway_client.update_session.call_args
        assert update_call.kwargs.get("container_id") == "abc123def456"
        assert update_call.kwargs.get("session_token") == "test-token-12345"

    def test_spawn_with_custom_image(self, spawner, mock_docker_client):
        """Test spawning with custom image."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            image="custom-sandbox:v2",
        )

        # Check that custom image was used
        calls = mock_docker_client.create_container.call_args_list
        # Get the last call (after recreate with env)
        last_call = calls[-1]
        assert last_call.kwargs.get("image") == "custom-sandbox:v2"

    def test_spawn_with_repo_volumes(self, spawner, mock_docker_client):
        """Test spawning with repository volumes."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repo_volumes={"my-repo": "/host/path/to/repo"},
        )

        # Check that volume was configured via mounts list
        calls = mock_docker_client.create_container.call_args_list
        last_call = calls[-1]
        mounts = last_call.kwargs.get("mounts", [])
        repo_mounts = [m for m in mounts if m.get("Source") == "/host/path/to/repo"]
        assert len(repo_mounts) == 1

    def test_spawn_with_extra_env(self, spawner, mock_docker_client, mock_gateway_client):
        """Test spawning with extra environment variables."""
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            extra_env={"CUSTOM_VAR": "custom_value"},
        )

        assert "CUSTOM_VAR" in result.environment
        assert result.environment["CUSTOM_VAR"] == "custom_value"

    def test_spawn_private_mode(self, spawner, mock_gateway_client):
        """Test spawning in private mode."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            mode="private",
        )

        # Verify private mode was passed to gateway
        mock_gateway_client.register_session.assert_called()
        call_kwargs = mock_gateway_client.register_session.call_args.kwargs
        assert call_kwargs.get("mode") == "private"

    def test_spawn_unhealthy_gateway_fails(self, spawner, mock_gateway_client):
        """Test that spawn fails when gateway is unhealthy."""
        mock_gateway_client.check_health.return_value = GatewayHealth(
            healthy=False,
            status="unhealthy",
            error="Connection refused",
        )

        with pytest.raises(ContainerSpawnError) as exc_info:
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.CODER,
                issue_number=123,
            )

        assert "not healthy" in str(exc_info.value).lower()

    def test_spawn_skip_gateway_health_check(self, spawner, mock_gateway_client):
        """Test spawning without gateway health check."""
        mock_gateway_client.check_health.return_value = GatewayHealth(
            healthy=False,
            status="unhealthy",
        )

        # Should not raise when wait_for_gateway=False
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            wait_for_gateway=False,
        )

        assert result is not None

    def test_spawn_raises_on_session_failure(
        self, spawner, mock_gateway_client, mock_docker_client
    ):
        """Test that spawn raises ContainerSpawnError if session registration fails."""
        mock_gateway_client.check_health.return_value = GatewayHealth(
            healthy=True,
            status="healthy",
        )
        mock_gateway_client.register_session.side_effect = GatewayError("Registration failed")

        with pytest.raises(ContainerSpawnError) as exc_info:
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.CODER,
                issue_number=123,
            )

        assert "session" in str(exc_info.value).lower()

    def test_spawn_sets_labels(self, spawner, mock_docker_client):
        """Test that proper labels are set on container."""
        spawner.spawn_agent_container(
            pipeline_id="issue-456",
            agent_role=AgentRole.DOCUMENTER,
            issue_number=456,
        )

        calls = mock_docker_client.create_container.call_args_list
        last_call = calls[-1]
        labels = last_call.kwargs.get("labels", {})

        assert labels.get("egg.pipeline.id") == "issue-456"
        assert labels.get("egg.agent.role") == "documenter"
        assert labels.get("egg.issue.number") == "456"


class TestSpawnBranchPropagation:
    """Tests for branch parameter propagation to gateway registration."""

    def test_branch_passed_to_register_session(self, spawner, mock_gateway_client):
        """Branch param is threaded to gateway register_session."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            branch="egg/fix-auth-bug",
        )

        mock_gateway_client.register_session.assert_called()
        call_kwargs = mock_gateway_client.register_session.call_args.kwargs
        assert call_kwargs.get("branch") == "egg/fix-auth-bug"

    def test_branch_none_by_default(self, spawner, mock_gateway_client):
        """Branch is None when not provided."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
        )

        mock_gateway_client.register_session.assert_called()
        call_kwargs = mock_gateway_client.register_session.call_args.kwargs
        assert call_kwargs.get("branch") is None


class TestStopAgentContainer:
    """Tests for stopping agent containers."""

    def test_stop_container(self, spawner, mock_docker_client, mock_gateway_client):
        """Test stopping a container."""
        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id="abc123",
            container_name="test",
            status=ContainerStatus.EXITED,
        )

        result = spawner.stop_agent_container("abc123")

        assert result.status == ContainerStatus.EXITED
        mock_docker_client.stop_container.assert_called_with("abc123", timeout=10)
        mock_gateway_client.delete_session_by_container.assert_called_with("abc123")

    def test_stop_container_without_session_cleanup(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test stopping without session cleanup."""
        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id="abc123",
            container_name="test",
            status=ContainerStatus.EXITED,
        )

        spawner.stop_agent_container("abc123", cleanup_session=False)

        mock_gateway_client.delete_session_by_container.assert_not_called()

    def test_stop_container_session_cleanup_error(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that session cleanup errors are logged but not raised."""
        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id="abc123",
            container_name="test",
            status=ContainerStatus.EXITED,
        )
        mock_gateway_client.delete_session_by_container.side_effect = GatewayError("Error")

        # Should not raise
        result = spawner.stop_agent_container("abc123")
        assert result is not None


class TestRemoveAgentContainer:
    """Tests for removing agent containers."""

    def test_remove_container(self, spawner, mock_docker_client, mock_gateway_client):
        """Test removing a container."""
        spawner.remove_agent_container("abc123")

        mock_docker_client.remove_container.assert_called_with("abc123", force=False)
        mock_gateway_client.delete_session_by_container.assert_called_with("abc123")

    def test_remove_container_force(self, spawner, mock_docker_client):
        """Test force removing a container."""
        spawner.remove_agent_container("abc123", force=True)

        mock_docker_client.remove_container.assert_called_with("abc123", force=True)

    def test_remove_cleans_up_session_on_error(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that session is cleaned up even if removal fails."""
        mock_docker_client.remove_container.side_effect = ContainerOperationError("Failed")

        with pytest.raises(ContainerOperationError):
            spawner.remove_agent_container("abc123")

        # Session should still be cleaned up
        mock_gateway_client.delete_session_by_container.assert_called_with("abc123")


class TestListPipelineContainers:
    """Tests for listing pipeline containers."""

    def test_list_pipeline_containers(self, spawner, mock_docker_client):
        """Test listing containers for a pipeline."""
        mock_docker_client.list_containers.return_value = [
            ContainerInfo(
                container_id="abc123",
                container_name="egg-issue-123-coder",
                status=ContainerStatus.RUNNING,
            ),
            ContainerInfo(
                container_id="def456",
                container_name="egg-issue-123-tester",
                status=ContainerStatus.RUNNING,
            ),
        ]

        result = spawner.list_pipeline_containers("issue-123")

        assert len(result) == 2
        mock_docker_client.list_containers.assert_called_with(
            labels={"egg.pipeline.id": "issue-123"}
        )


class TestCleanupPipeline:
    """Tests for pipeline cleanup."""

    def test_cleanup_pipeline(self, spawner, mock_docker_client, mock_gateway_client):
        """Test cleaning up all pipeline containers."""
        mock_docker_client.list_containers.return_value = [
            ContainerInfo(
                container_id="abc123",
                container_name="egg-issue-123-coder",
                status=ContainerStatus.EXITED,
            ),
            ContainerInfo(
                container_id="def456",
                container_name="egg-issue-123-tester",
                status=ContainerStatus.EXITED,
            ),
        ]

        removed = spawner.cleanup_pipeline("issue-123")

        assert removed == 2
        assert mock_docker_client.remove_container.call_count == 2
        assert mock_gateway_client.delete_session_by_container.call_count == 2

    def test_cleanup_continues_on_error(self, spawner, mock_docker_client, mock_gateway_client):
        """Test that cleanup continues even if some containers fail."""
        mock_docker_client.list_containers.return_value = [
            ContainerInfo(
                container_id="abc123",
                container_name="test1",
                status=ContainerStatus.EXITED,
            ),
            ContainerInfo(
                container_id="def456",
                container_name="test2",
                status=ContainerStatus.EXITED,
            ),
        ]

        # First removal fails, second succeeds
        mock_docker_client.remove_container.side_effect = [
            ContainerOperationError("Failed"),
            None,
        ]

        removed = spawner.cleanup_pipeline("issue-123")

        # Only the second container was successfully removed
        assert removed == 1


class TestGetContainerIp:
    """Tests for container IP resolution."""

    def test_get_ip_from_docker(self, spawner, mock_docker_client):
        """Test getting IP from Docker network info."""
        mock_container = MagicMock()
        mock_container.attrs = {
            "NetworkSettings": {
                "Networks": {
                    "egg-isolated": {
                        "IPAddress": "172.32.0.42",
                    },
                },
            },
        }
        mock_docker_client.client.containers.get.return_value = mock_container

        ip = spawner._get_container_ip("abc123def456")

        assert ip == "172.32.0.42"

    def test_get_ip_fallback(self, spawner, mock_docker_client):
        """Test fallback IP generation."""
        mock_docker_client.client.containers.get.side_effect = Exception("Not found")

        ip = spawner._get_container_ip("abc123def456")

        # Should return a valid IP in the range
        assert ip.startswith("172.32.0.")


class TestContainerEnvironmentAtCreation:
    """Tests verifying gateway environment is included at container creation time."""

    def test_spawn_includes_session_token_in_container_env(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that session token is passed to create_container, not added after."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            mode="public",
        )

        # Verify the environment passed to create_container includes the session token
        create_call = mock_docker_client.create_container.call_args
        container_env = create_call.kwargs.get("environment", {})

        assert "EGG_SESSION_TOKEN" in container_env, (
            "Session token must be included in container environment at creation time"
        )
        assert container_env["EGG_SESSION_TOKEN"] == "test-token-12345"

    def test_spawn_includes_gateway_url_in_container_env(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that GATEWAY_URL uses hostname, not raw IP."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
        )

        create_call = mock_docker_client.create_container.call_args
        container_env = create_call.kwargs.get("environment", {})

        assert "GATEWAY_URL" in container_env, (
            "Gateway URL must be included in container environment at creation time"
        )
        # GATEWAY_URL should be hostname-based (from shared config builder)
        assert "egg-gateway" in container_env["GATEWAY_URL"]

    def test_spawn_private_mode_includes_proxy_config(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that proxy configuration is set for private mode containers."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            mode="private",
        )

        create_call = mock_docker_client.create_container.call_args
        container_env = create_call.kwargs.get("environment", {})

        assert "HTTP_PROXY" in container_env, "HTTP_PROXY must be included for private mode"
        assert "HTTPS_PROXY" in container_env, "HTTPS_PROXY must be included for private mode"

    def test_spawn_public_mode_no_proxy_config(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that proxy configuration is NOT set for public mode containers."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            mode="public",
        )

        create_call = mock_docker_client.create_container.call_args
        container_env = create_call.kwargs.get("environment", {})

        assert "HTTP_PROXY" not in container_env
        assert "HTTPS_PROXY" not in container_env

    def test_spawn_passes_repos_and_phase_to_register_session(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that repos and phase are passed through to session registration."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repos=["test-owner/test-repo"],
            phase="refine",
        )

        # Verify register_session was called with repos and phase
        mock_gateway_client.register_session.assert_called_once()
        register_call = mock_gateway_client.register_session.call_args
        assert register_call.kwargs.get("repos") == ["test-owner/test-repo"]
        assert register_call.kwargs.get("phase") == "refine"

    def test_spawn_includes_extra_hosts_for_gateway(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that extra_hosts maps gateway hostname to IP."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
        )

        create_call = mock_docker_client.create_container.call_args
        extra_hosts = create_call.kwargs.get("extra_hosts", {})

        assert "egg-gateway" in extra_hosts, (
            "extra_hosts must include gateway hostname for DNS resolution"
        )

    def test_spawn_with_repo_volumes_adds_git_shadows(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that .git shadow mounts are added for repo volumes."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repo_volumes={"my-repo": "/host/repos/my-repo"},
        )

        create_call = mock_docker_client.create_container.call_args
        mounts = create_call.kwargs.get("mounts", [])

        # .git shadow uses /dev/null bind mount (file-over-file for worktrees)
        git_shadow = [m for m in mounts if m["Target"] == "/home/egg/repos/my-repo/.git"]
        assert len(git_shadow) == 1, ".git shadow mount must be added for each repo volume"
        assert git_shadow[0]["Source"] == "/dev/null"
        assert git_shadow[0]["ReadOnly"] is True


class TestHostToLocalVolumes:
    """Tests for _host_to_local_volumes()."""

    def test_translates_host_home_to_container_home(self):
        """HOST_HOME prefix is replaced with /home/egg."""
        repo_volumes = {"repo": "/home/jwies/.egg-worktrees/repo"}
        with patch.dict("os.environ", {"HOST_HOME": "/home/jwies"}):
            result = _host_to_local_volumes(repo_volumes)
        assert result == {"repo": "/home/egg/.egg-worktrees/repo"}

    def test_passthrough_when_host_home_matches_container(self):
        """No translation when HOST_HOME equals container home."""
        repo_volumes = {"repo": "/home/egg/.egg-worktrees/repo"}
        with patch.dict("os.environ", {"HOST_HOME": "/home/egg"}):
            result = _host_to_local_volumes(repo_volumes)
        assert result is repo_volumes

    def test_passthrough_when_host_home_empty(self):
        """No translation when HOST_HOME is not set."""
        repo_volumes = {"repo": "/some/path/repo"}
        with patch.dict("os.environ", {"HOST_HOME": ""}):
            result = _host_to_local_volumes(repo_volumes)
        assert result is repo_volumes

    def test_only_replaces_prefix(self):
        """Only the first occurrence of HOST_HOME at the start is replaced."""
        repo_volumes = {"repo": "/home/jwies/repos/home/jwies/nested"}
        with patch.dict("os.environ", {"HOST_HOME": "/home/jwies"}):
            result = _host_to_local_volumes(repo_volumes)
        assert result == {"repo": "/home/egg/repos/home/jwies/nested"}

    def test_multiple_repos(self):
        """All repos in the mapping are translated."""
        repo_volumes = {
            "a": "/home/jwies/.egg-worktrees/a",
            "b": "/home/jwies/.egg-worktrees/b",
        }
        with patch.dict("os.environ", {"HOST_HOME": "/home/jwies"}):
            result = _host_to_local_volumes(repo_volumes)
        assert result == {
            "a": "/home/egg/.egg-worktrees/a",
            "b": "/home/egg/.egg-worktrees/b",
        }

    def test_non_matching_paths_unchanged(self):
        """Paths not starting with HOST_HOME are left unchanged."""
        repo_volumes = {
            "a": "/home/jwies/.egg-worktrees/a",
            "b": "/other/path/b",
        }
        with patch.dict("os.environ", {"HOST_HOME": "/home/jwies"}):
            result = _host_to_local_volumes(repo_volumes)
        assert result == {
            "a": "/home/egg/.egg-worktrees/a",
            "b": "/other/path/b",
        }

    def test_trailing_slash_on_host_home(self):
        """HOST_HOME with trailing slash does not produce double slashes."""
        repo_volumes = {"repo": "/home/jwies/.egg-worktrees/repo"}
        with patch.dict("os.environ", {"HOST_HOME": "/home/jwies/"}):
            result = _host_to_local_volumes(repo_volumes)
        assert result == {"repo": "/home/egg/.egg-worktrees/repo"}


class TestSingletonSpawner:
    """Tests for singleton spawner."""

    def test_get_container_spawner_returns_singleton(self):
        """Test that get_container_spawner returns the same instance."""
        import container_spawner

        container_spawner._spawner = None

        spawner1 = get_container_spawner()
        spawner2 = get_container_spawner()

        assert spawner1 is spawner2


class TestComputeAllowedFiles:
    """Tests for _compute_allowed_files() helper."""

    def _make_phase(self, phase_id: str, tasks: list | None = None):
        """Create a mock Phase object with tasks."""
        phase = MagicMock()
        phase.id = phase_id
        phase.tasks = tasks or []
        return phase

    def _make_task(self, task_id: str, files: list[str] | None = None):
        """Create a mock Task object with files_affected."""
        task = MagicMock()
        task.id = task_id
        task.files_affected = files or []
        return task

    def test_union_of_files_from_multiple_tasks(self):
        """Collects files from all tasks in the phase."""
        tasks = [
            self._make_task("task-1", ["src/auth/login.py", "src/auth/utils.py"]),
            self._make_task("task-2", ["src/models/user.py"]),
        ]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is not None
        # Should contain all explicit files plus directory expansions
        assert "src/auth/login.py" in result
        assert "src/auth/utils.py" in result
        assert "src/models/user.py" in result
        assert "src/auth/*" in result
        assert "src/models/*" in result

    def test_directory_sibling_expansion(self):
        """dir/foo.py expands to include dir/* (recursive via fnmatch)."""
        tasks = [self._make_task("task-1", ["src/auth/login.py"])]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is not None
        assert "src/auth/*" in result
        assert "src/auth/login.py" in result

    def test_empty_files_returns_none(self):
        """Empty files_affected across all tasks returns None."""
        tasks = [
            self._make_task("task-1", []),
            self._make_task("task-2", []),
        ]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is None

    def test_non_coder_returns_none(self):
        """Non-coder agent roles return None."""
        tasks = [self._make_task("task-1", ["src/foo.py"])]
        phase = self._make_phase("phase-1", tasks)

        assert _compute_allowed_files([phase], "phase-1", "tester") is None
        assert _compute_allowed_files([phase], "phase-1", "documenter") is None
        assert _compute_allowed_files([phase], "phase-1", "reviewer") is None

    def test_missing_plan_phase_id_returns_none(self):
        """Missing plan_phase_id returns None."""
        tasks = [self._make_task("task-1", ["src/foo.py"])]
        phase = self._make_phase("phase-1", tasks)

        assert _compute_allowed_files([phase], None, "coder") is None
        assert _compute_allowed_files([phase], "", "coder") is None

    def test_missing_phases_returns_none(self):
        """Empty phases list returns None."""
        assert _compute_allowed_files([], "phase-1", "coder") is None

    def test_phase_not_found_returns_none(self):
        """Phase ID not found in phases list returns None."""
        tasks = [self._make_task("task-1", ["src/foo.py"])]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-999", "coder")
        assert result is None

    def test_glob_patterns_preserved(self):
        """Glob patterns in files_affected pass through unchanged."""
        tasks = [self._make_task("task-1", ["tests/**", "src/auth/*"])]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is not None
        assert "tests/**" in result
        assert "src/auth/*" in result

    def test_deduplication(self):
        """Duplicate files across tasks are deduplicated."""
        tasks = [
            self._make_task("task-1", ["src/auth/login.py"]),
            self._make_task("task-2", ["src/auth/login.py"]),
        ]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is not None
        # Each entry should appear exactly once
        assert result.count("src/auth/login.py") == 1
        assert result.count("src/auth/*") == 1

    def test_result_is_sorted(self):
        """Result is sorted for deterministic output."""
        tasks = [
            self._make_task("task-1", ["z/file.py", "a/file.py"]),
        ]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is not None
        assert result == sorted(result)

    def test_top_level_file_no_directory_expansion(self):
        """Top-level files (no /) don't get directory expansion."""
        tasks = [self._make_task("task-1", ["Makefile"])]
        phase = self._make_phase("phase-1", tasks)

        result = _compute_allowed_files([phase], "phase-1", "coder")
        assert result is not None
        assert "Makefile" in result
        # No dir/* expansion for top-level files
        assert len(result) == 1


class TestSpawnerPassesAllowedFiles:
    """Tests that spawner passes allowed_files to register_session()."""

    def test_spawn_with_allowed_files(self, spawner, mock_gateway_client, mock_docker_client):
        """spawn_agent_container passes allowed_files to register_session."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            mode="public",
            repos=["owner/repo"],
            phase="implement",
            wait_for_gateway=False,
            allowed_files=["src/auth/*", "tests/*"],
        )

        # Verify register_session was called with allowed_files
        mock_gateway_client.register_session.assert_called_once()
        call_kwargs = mock_gateway_client.register_session.call_args[1]
        assert call_kwargs["allowed_files"] == ["src/auth/*", "tests/*"]

    def test_spawn_without_allowed_files(self, spawner, mock_gateway_client, mock_docker_client):
        """spawn_agent_container passes None when no allowed_files."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            mode="public",
            repos=["owner/repo"],
            phase="implement",
            wait_for_gateway=False,
        )

        mock_gateway_client.register_session.assert_called_once()
        call_kwargs = mock_gateway_client.register_session.call_args[1]
        assert call_kwargs["allowed_files"] is None
