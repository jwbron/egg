"""
Tester-agent gap tests for issue #647 DinD integration.

Targets gaps identified in code review:
1. DinD cleanup on GatewayError path (ContainerSpawnError from gateway failure)
2. DinD watchdog timer lifecycle (start, cancel on teardown, expiry callback)
3. integration_test_enabled not wired through production Docker spawn path
4. DinD cleanup when session cleanup itself fails
5. DinD manager state after watchdog-triggered teardown
"""

import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Path setup for orchestrator and shared imports
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker SDK before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from container_spawner import (  # noqa: E402
    ContainerSpawner,
    ContainerSpawnError,
)
from docker_client import ContainerOperationError  # noqa: E402
from gateway_client import GatewayError, GatewayHealth, SessionInfo  # noqa: E402
from models import (  # noqa: E402
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from multi_agent import MultiAgentExecutor  # noqa: E402

# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client with default behaviors."""
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.create_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-issue-123-tester",
        status=ContainerStatus.PENDING,
    )
    mock.start_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-issue-123-tester",
        status=ContainerStatus.RUNNING,
        started_at=datetime.utcnow(),
    )
    mock.list_containers.return_value = []
    return mock


@pytest.fixture
def mock_gateway_client():
    """Create a mock Gateway client with default behaviors."""
    mock = MagicMock()
    mock.check_health.return_value = GatewayHealth(healthy=True, status="healthy", version="0.1.0")
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
    """Create a ContainerSpawner with mocked clients."""
    return ContainerSpawner(
        docker_client=mock_docker_client,
        gateway_client=mock_gateway_client,
    )


def _make_mock_dind_manager(healthy=True, daemon_url="tcp://172.17.0.5:2375"):
    """Create a mock DindManager."""
    from dind_manager import DindStatus, DindStatusValue

    mock = MagicMock()
    status = DindStatus(
        status=DindStatusValue.HEALTHY if healthy else DindStatusValue.UNHEALTHY,
        container_id="dind-abc123",
        daemon_url=daemon_url if healthy else "",
    )
    mock.start.return_value = status
    mock.teardown.return_value = None
    return mock


# ── Gap 1: DinD cleanup on GatewayError path ─────────────────────


class TestDindCleanupOnGatewayError:
    """Verify DinD sidecar is cleaned up when GatewayError triggers ContainerSpawnError.

    Code review issue #1: When gateway.register_session() fails after DinD
    has been provisioned, the ContainerSpawnError must trigger DinD teardown.
    """

    @patch("container_spawner.DindManager")
    def test_dind_torn_down_on_gateway_session_failure(
        self, MockDindManager, spawner, mock_gateway_client
    ):
        """DinD is torn down when gateway.register_session() raises GatewayError."""
        mock_dind = _make_mock_dind_manager()
        MockDindManager.return_value = mock_dind

        # Gateway session registration fails AFTER DinD is provisioned
        mock_gateway_client.register_session.side_effect = GatewayError(
            "session registration failed"
        )

        with pytest.raises(ContainerSpawnError, match="Failed to register gateway session"):
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.TESTER,
                issue_number=123,
                integration_test_enabled=True,
            )

        # DinD must have been provisioned then torn down
        mock_dind.start.assert_called_once()
        mock_dind.teardown.assert_called_once()

    @patch("container_spawner.DindManager")
    def test_dind_not_tracked_after_gateway_failure(
        self, MockDindManager, spawner, mock_gateway_client
    ):
        """DinD manager is NOT stored in _dind_managers after GatewayError cleanup."""
        mock_dind = _make_mock_dind_manager()
        MockDindManager.return_value = mock_dind

        mock_gateway_client.register_session.side_effect = GatewayError("fail")

        with pytest.raises(ContainerSpawnError):
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.TESTER,
                issue_number=123,
                integration_test_enabled=True,
            )

        assert "issue-123" not in spawner._dind_managers

    @patch("container_spawner.DindManager")
    def test_dind_teardown_failure_during_gateway_error_is_non_fatal(
        self, MockDindManager, spawner, mock_gateway_client
    ):
        """DinD teardown error during GatewayError handling doesn't mask original error."""
        mock_dind = _make_mock_dind_manager()
        mock_dind.teardown.side_effect = Exception("DinD teardown exploded")
        MockDindManager.return_value = mock_dind

        mock_gateway_client.register_session.side_effect = GatewayError("session fail")

        # The original ContainerSpawnError from GatewayError should propagate,
        # not the DinD teardown error
        with pytest.raises(ContainerSpawnError, match="Failed to register gateway session"):
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.TESTER,
                issue_number=123,
                integration_test_enabled=True,
            )

    @patch("container_spawner.DindManager")
    def test_dind_torn_down_on_container_create_failure(
        self, MockDindManager, spawner, mock_docker_client
    ):
        """DinD is torn down when docker.create_container() fails (not just GatewayError)."""
        mock_dind = _make_mock_dind_manager()
        MockDindManager.return_value = mock_dind

        mock_docker_client.create_container.side_effect = Exception("OOM kill")

        with pytest.raises(ContainerSpawnError, match="Failed to spawn container"):
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.TESTER,
                issue_number=123,
                integration_test_enabled=True,
            )

        mock_dind.teardown.assert_called_once()

    @patch("container_spawner.DindManager")
    def test_dind_torn_down_on_container_start_failure(
        self, MockDindManager, spawner, mock_docker_client
    ):
        """DinD is torn down when docker.start_container() fails."""
        mock_dind = _make_mock_dind_manager()
        MockDindManager.return_value = mock_dind

        mock_docker_client.start_container.side_effect = Exception("failed to start")

        with pytest.raises(ContainerSpawnError, match="Failed to spawn container"):
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.TESTER,
                issue_number=123,
                integration_test_enabled=True,
            )

        mock_dind.teardown.assert_called_once()

    @patch("container_spawner.DindManager")
    def test_session_cleanup_attempted_on_gateway_error_after_dind(
        self, MockDindManager, spawner, mock_docker_client, mock_gateway_client
    ):
        """When container creation fails after session registration, session is cleaned up."""
        mock_dind = _make_mock_dind_manager()
        MockDindManager.return_value = mock_dind

        # Session registers successfully, but container creation fails
        mock_docker_client.create_container.side_effect = Exception("Docker error")

        with pytest.raises(ContainerSpawnError):
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.TESTER,
                issue_number=123,
                integration_test_enabled=True,
            )

        # Both DinD and session should be cleaned up
        mock_dind.teardown.assert_called_once()
        mock_gateway_client.delete_session.assert_called_once_with("test-token-12345")


# ── Gap 2: DinD watchdog timer lifecycle ──────────────────────────


class TestDindWatchdogTimer:
    """Verify DinD watchdog auto-kill timer behavior.

    Code review issue #2: DinD container must have a maximum lifetime timeout
    to prevent indefinite execution of privileged containers.
    """

    def test_watchdog_starts_on_healthy_start(self):
        """Watchdog timer is started when DinD becomes healthy."""
        import dind_manager as dind_mod

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.id = "abc123"
        mock_container.attrs = {
            "NetworkSettings": {
                "Networks": {"bridge": {"IPAddress": "172.17.0.5"}},
            }
        }
        mock_client.containers.get.side_effect = [
            type("NotFound", (Exception,), {})("not found"),
            mock_container,
            mock_container,
        ]
        mock_client.containers.run.return_value = mock_container

        with patch.object(dind_mod, "docker", object()):
            from dind_manager import DindManager

            manager = DindManager(
                pipeline_id="test-watchdog",
                docker_client=mock_client,
                max_lifetime_seconds=600,
            )

            with patch.object(manager, "_wait_for_healthy", return_value=True):
                with patch.object(manager, "_start_watchdog") as mock_start_wd:
                    manager.start()

            mock_start_wd.assert_called_once()

    def test_watchdog_not_started_when_unhealthy(self):
        """Watchdog is NOT started when DinD fails health check."""
        import dind_manager as dind_mod

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.id = "abc123"
        mock_client.containers.get.side_effect = [
            type("NotFound", (Exception,), {})("not found"),
        ]
        mock_client.containers.run.return_value = mock_container

        with patch.object(dind_mod, "docker", object()):
            from dind_manager import DindManager

            manager = DindManager(
                pipeline_id="test-watchdog",
                docker_client=mock_client,
                max_lifetime_seconds=600,
            )

            with patch.object(manager, "_wait_for_healthy", return_value=False):
                with patch.object(manager, "_start_watchdog") as mock_start_wd:
                    manager.start()

            mock_start_wd.assert_not_called()

    def test_watchdog_cancelled_on_teardown(self):
        """Watchdog timer is cancelled when teardown() is called."""
        import dind_manager as dind_mod

        with patch.object(dind_mod, "docker", object()):
            from dind_manager import DindManager

            manager = DindManager(
                pipeline_id="test-watchdog",
                docker_client=MagicMock(),
            )

            mock_timer = MagicMock(spec=threading.Timer)
            manager._watchdog = mock_timer
            manager._container_id = "abc123"
            manager._started = True

            manager.teardown()

            mock_timer.cancel.assert_called_once()
            assert manager._watchdog is None

    def test_watchdog_disabled_when_max_lifetime_zero(self):
        """Watchdog is not started when max_lifetime_seconds=0."""
        import dind_manager as dind_mod

        with patch.object(dind_mod, "docker", object()):
            from dind_manager import DindManager

            manager = DindManager(
                pipeline_id="test-watchdog",
                docker_client=MagicMock(),
                max_lifetime_seconds=0,
            )

            with patch("threading.Timer") as MockTimer:
                manager._start_watchdog()
                MockTimer.assert_not_called()

    def test_watchdog_timer_uses_correct_timeout(self):
        """Watchdog timer fires after max_lifetime_seconds."""
        import dind_manager as dind_mod

        with patch.object(dind_mod, "docker", object()):
            from dind_manager import DindManager

            manager = DindManager(
                pipeline_id="test-watchdog",
                docker_client=MagicMock(),
                max_lifetime_seconds=300,
            )

            with patch("threading.Timer") as MockTimer:
                mock_timer = MagicMock()
                MockTimer.return_value = mock_timer

                manager._start_watchdog()

                MockTimer.assert_called_once_with(300, manager._watchdog_expired)
                mock_timer.start.assert_called_once()
                assert mock_timer.daemon is True

    def test_watchdog_expired_calls_teardown(self):
        """_watchdog_expired() calls teardown to remove the DinD container."""
        import dind_manager as dind_mod

        with patch.object(dind_mod, "docker", object()):
            from dind_manager import DindManager

            manager = DindManager(
                pipeline_id="test-watchdog",
                docker_client=MagicMock(),
            )
            manager._container_id = "abc123"
            manager._started = True

            with patch.object(manager, "teardown") as mock_teardown:
                manager._watchdog_expired()
                mock_teardown.assert_called_once()

    def test_watchdog_expired_swallows_teardown_error(self):
        """_watchdog_expired() does not raise if teardown() fails."""
        import dind_manager as dind_mod

        with patch.object(dind_mod, "docker", object()):
            from dind_manager import DindManager

            manager = DindManager(
                pipeline_id="test-watchdog",
                docker_client=MagicMock(),
            )
            manager._container_id = "abc123"
            manager._started = True

            with patch.object(manager, "teardown", side_effect=Exception("teardown boom")):
                # Should NOT raise
                manager._watchdog_expired()

    def test_default_max_lifetime_is_600_seconds(self):
        """Default max_lifetime_seconds is 600 (10 minutes)."""
        import dind_manager as dind_mod

        with patch.object(dind_mod, "docker", object()):
            from dind_manager import DIND_MAX_LIFETIME_SECONDS, DindManager

            manager = DindManager(
                pipeline_id="test",
                docker_client=MagicMock(),
            )
            assert manager.max_lifetime_seconds == DIND_MAX_LIFETIME_SECONDS
            assert manager.max_lifetime_seconds == 600


# ── Gap 3: integration_test_enabled not wired in production ───────


class TestIntegrationTestEnabledProductionWiring:
    """Verify integration_test_enabled is NOT passed through the production Docker path.

    Code review issue #4: The parameter only propagates through the spawn_fn
    callback path. The production callers in pipelines.py never pass it.
    This test documents the intentional deferral to Phase 2.
    """

    def test_spawn_agent_container_defaults_integration_test_disabled(self):
        """spawn_agent_container defaults integration_test_enabled to False."""
        import inspect

        from container_spawner import ContainerSpawner

        sig = inspect.signature(ContainerSpawner.spawn_agent_container)
        param = sig.parameters["integration_test_enabled"]
        assert param.default is False

    def test_multi_agent_executor_defaults_integration_test_disabled(self):
        """MultiAgentExecutor defaults integration_test_enabled to False."""
        import inspect

        sig = inspect.signature(MultiAgentExecutor.__init__)
        param = sig.parameters["integration_test_enabled"]
        assert param.default is False

    def test_production_spawn_call_does_not_pass_integration_test_enabled(self):
        """The production spawn call in pipelines.py does NOT include integration_test_enabled.

        This verifies the documented gap: DinD is not wired in production yet.
        We check that the source code of the production spawn call site does not
        include the integration_test_enabled kwarg.
        """
        import ast

        pipelines_path = Path(__file__).parent.parent / "routes" / "pipelines.py"
        if not pipelines_path.exists():
            pytest.skip("pipelines.py not found at expected location")

        source = pipelines_path.read_text()

        # Find all spawn_agent_container call sites
        tree = ast.parse(source)
        spawn_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Check if this is a call to spawn_agent_container
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "spawn_agent_container":
                    kwarg_names = [kw.arg for kw in node.keywords if kw.arg is not None]
                    spawn_calls.append(kwarg_names)

        assert len(spawn_calls) > 0, "Expected at least one spawn_agent_container call"

        # None of the production spawn calls should pass integration_test_enabled
        for kwarg_names in spawn_calls:
            assert "integration_test_enabled" not in kwarg_names, (
                "Production spawn call passes integration_test_enabled — "
                "Phase 2 wiring may have been added. Update this test if intentional."
            )

    def test_spawn_fn_path_propagates_integration_test_env(self):
        """The spawn_fn path correctly propagates EGG_INTEGRATION_TEST_ENABLED."""
        pipeline = Pipeline(
            id="issue-99",
            issue_number=99,
            repo="owner/repo",
            branch="egg/issue-99",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )

        mock_dispatcher = MagicMock()
        mock_dispatcher.save_contract = MagicMock()
        mock_dispatcher.get_agents_to_run.side_effect = [[AgentRole.TESTER], []]
        mock_dispatcher.get_next_dispatch.return_value = MagicMock(wave_number=1)
        mock_dispatcher.get_handoff_data.return_value = {}

        captured_envs = []

        def spy_spawn(role, prompt, extra_env):
            captured_envs.append(dict(extra_env or {}))
            return (0, "ok")

        executor = MultiAgentExecutor(
            pipeline=pipeline,
            repo_path=Path("/repo"),
            dispatcher=mock_dispatcher,
            spawn_fn=spy_spawn,
            integration_test_enabled=True,
        )

        executor.execute_all_waves(
            agent_prompts={AgentRole.TESTER: "run tests"},
            max_waves=1,
        )

        assert len(captured_envs) == 1
        assert captured_envs[0].get("EGG_INTEGRATION_TEST_ENABLED") == "true"


# ── Gap 4: DinD cleanup edge cases in spawner ────────────────────


class TestDindCleanupEdgeCases:
    """Additional edge cases for DinD cleanup in ContainerSpawner."""

    @patch("container_spawner.DindManager")
    def test_dind_cleanup_when_session_cleanup_also_fails(
        self, MockDindManager, spawner, mock_docker_client, mock_gateway_client
    ):
        """Both DinD teardown and session cleanup fail — error propagates correctly."""
        mock_dind = _make_mock_dind_manager()
        MockDindManager.return_value = mock_dind

        # Container creation fails
        mock_docker_client.create_container.side_effect = Exception("Docker error")
        # Session cleanup also fails
        mock_gateway_client.delete_session.side_effect = GatewayError("session cleanup fail")

        with pytest.raises(ContainerSpawnError, match="Failed to spawn container"):
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.TESTER,
                issue_number=123,
                integration_test_enabled=True,
            )

        # DinD was still torn down despite session cleanup failure
        mock_dind.teardown.assert_called_once()

    @patch("container_spawner.DindManager")
    def test_cleanup_pipeline_with_dind_and_container_removal_error(
        self, MockDindManager, spawner, mock_docker_client, mock_gateway_client
    ):
        """cleanup_pipeline removes DinD even if container removal fails."""
        mock_dind = MagicMock()
        spawner._dind_managers["issue-123"] = mock_dind

        container = ContainerInfo(
            container_id="abc123",
            container_name="egg-issue-123-tester",
            status=ContainerStatus.RUNNING,
        )
        mock_docker_client.list_containers.return_value = [container]
        # cleanup_pipeline catches ContainerNotFoundError and ContainerOperationError
        mock_docker_client.remove_container.side_effect = ContainerOperationError("busy")

        # Should not raise
        spawner.cleanup_pipeline("issue-123")

        # DinD should still be cleaned up even though container removal failed
        mock_dind.teardown.assert_called_once()
        assert "issue-123" not in spawner._dind_managers

    @patch("container_spawner.DindManager")
    def test_dind_not_in_managers_after_all_error_paths(
        self, MockDindManager, spawner, mock_gateway_client
    ):
        """After any error path, DinD manager is never left in _dind_managers."""
        mock_dind = _make_mock_dind_manager()
        MockDindManager.return_value = mock_dind

        # GatewayError path
        mock_gateway_client.register_session.side_effect = GatewayError("fail")

        with pytest.raises(ContainerSpawnError):
            spawner.spawn_agent_container(
                pipeline_id="pipe-1",
                agent_role=AgentRole.TESTER,
                issue_number=1,
                integration_test_enabled=True,
            )

        assert "pipe-1" not in spawner._dind_managers


# ── Gap 5: CI workflow validation ─────────────────────────────────


class TestCIWorkflowConfiguration:
    """Validate CI workflow configuration addresses review issues #3 and #5."""

    def test_integration_tests_has_path_filters(self):
        """test-integration.yml has path filters on pull_request trigger."""
        import yaml

        workflow_path = (
            Path(__file__).parent.parent.parent / ".github" / "workflows" / "test-integration.yml"
        )
        if not workflow_path.exists():
            pytest.skip("test-integration.yml not found")

        content = yaml.safe_load(workflow_path.read_text())
        pr_trigger = content.get(True, content.get("on", {})).get("pull_request", {})

        # Must have path filters
        assert "paths" in pr_trigger, (
            "test-integration.yml pull_request trigger must have path filters "
            "to avoid running expensive integration tests on docs-only PRs"
        )
        paths = pr_trigger["paths"]
        assert len(paths) > 0

        # Core paths should be included
        path_set = set(paths)
        for expected in ["gateway/**", "orchestrator/**", "shared/**"]:
            assert expected in path_set, f"Missing path filter: {expected}"

    def test_integration_tests_has_concurrency_group(self):
        """test-integration.yml has a concurrency group to cancel stale runs."""
        import yaml

        workflow_path = (
            Path(__file__).parent.parent.parent / ".github" / "workflows" / "test-integration.yml"
        )
        if not workflow_path.exists():
            pytest.skip("test-integration.yml not found")

        content = yaml.safe_load(workflow_path.read_text())

        assert "concurrency" in content, (
            "test-integration.yml must have a concurrency group "
            "to cancel stale CI runs on rapid pushes"
        )
        concurrency = content["concurrency"]
        assert concurrency.get("cancel-in-progress") is True

    def test_lint_workflow_has_concurrency_group(self):
        """lint.yml has a concurrency group."""
        import yaml

        workflow_path = Path(__file__).parent.parent.parent / ".github" / "workflows" / "lint.yml"
        if not workflow_path.exists():
            pytest.skip("lint.yml not found")

        content = yaml.safe_load(workflow_path.read_text())

        assert "concurrency" in content, "lint.yml must have a concurrency group"
        assert content["concurrency"].get("cancel-in-progress") is True

    def test_test_workflow_has_concurrency_group(self):
        """test.yml has a concurrency group."""
        import yaml

        workflow_path = Path(__file__).parent.parent.parent / ".github" / "workflows" / "test.yml"
        if not workflow_path.exists():
            pytest.skip("test.yml not found")

        content = yaml.safe_load(workflow_path.read_text())

        assert "concurrency" in content, "test.yml must have a concurrency group"
        assert content["concurrency"].get("cancel-in-progress") is True

    def test_autofix_watcher_includes_integration_tests(self):
        """on-check-failure.yml watches 'Integration Tests'.

        PR #882 added Integration Tests to the autofix workflow triggers
        so that the autofixer can attempt to fix integration test failures.
        """
        import yaml

        workflow_path = (
            Path(__file__).parent.parent.parent / ".github" / "workflows" / "on-check-failure.yml"
        )
        if not workflow_path.exists():
            pytest.skip("on-check-failure.yml not found")

        content = yaml.safe_load(workflow_path.read_text())
        workflows_watched = (
            content.get(True, content.get("on", {})).get("workflow_run", {}).get("workflows", [])
        )

        assert "Integration Tests" in workflows_watched, (
            "on-check-failure.yml should watch 'Integration Tests' — "
            "autofix should attempt to fix integration test failures (see PR #882)"
        )

    def test_integration_tests_builds_all_required_images(self):
        """test-integration.yml builds gateway, orchestrator, AND mock-sandbox images."""
        workflow_path = (
            Path(__file__).parent.parent.parent / ".github" / "workflows" / "test-integration.yml"
        )
        if not workflow_path.exists():
            pytest.skip("test-integration.yml not found")

        content = workflow_path.read_text()

        assert "egg-gateway" in content, "CI must build egg-gateway image"
        assert "egg-orchestrator" in content, "CI must build egg-orchestrator image"
        assert "mock-sandbox" in content, "CI must build mock-sandbox image"

    def test_integration_tests_cleanup_both_compose_stacks(self):
        """test-integration.yml cleanup step handles both compose files."""
        workflow_path = (
            Path(__file__).parent.parent.parent / ".github" / "workflows" / "test-integration.yml"
        )
        if not workflow_path.exists():
            pytest.skip("test-integration.yml not found")

        content = workflow_path.read_text()

        # Both compose stacks should be cleaned up
        assert "integration_tests/docker-compose.yml" in content, (
            "CI cleanup must handle the gateway-only compose stack"
        )
        assert "integration_tests/local_pipeline/docker-compose.yml" in content, (
            "CI cleanup must handle the local_pipeline compose stack"
        )
