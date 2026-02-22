"""
Unit tests for DindManager.

Tests DinD sidecar lifecycle: initialization, startup, health check polling,
image pre-load command construction, and cleanup paths.
All Docker SDK calls are mocked.
"""

from unittest.mock import MagicMock, patch

import dind_manager as dind_mod
import pytest
from dind_manager import (
    DIND_PORT,
    DindError,
    DindImageLoadError,
    DindManager,
    DindStartupError,
    DindStatusValue,
    DockerNotFound,
)


@pytest.fixture(autouse=True)
def _mock_docker_module():
    """Ensure dind_manager.docker is truthy so the SDK guard passes.

    The real docker SDK may not be installed in the test environment.
    Tests provide their own mock docker_client; this fixture only ensures
    the ``if docker is None`` guard in start() is bypassed.
    """
    sentinel = object()
    with patch.object(dind_mod, "docker", sentinel):
        yield


def _make_manager(
    pipeline_id: str = "issue-647",
    docker_client: any = None,
) -> DindManager:
    """Create a DindManager with mocked Docker client."""
    return DindManager(
        pipeline_id=pipeline_id,
        docker_client=docker_client if docker_client is not None else MagicMock(),
    )


# ── Initialization Tests ────────────────────────────────────────


class TestInit:
    """Tests for DindManager initialization."""

    def test_init_sets_pipeline_id(self):
        manager = _make_manager(pipeline_id="issue-123")
        assert manager.pipeline_id == "issue-123"

    def test_init_sets_container_name(self):
        manager = _make_manager(pipeline_id="issue-123")
        assert manager.container_name == "egg-dind-issue-123"

    def test_init_status_is_stopped(self):
        manager = _make_manager()
        assert manager.status.status == DindStatusValue.STOPPED

    def test_init_daemon_url_empty(self):
        manager = _make_manager()
        assert manager.daemon_url == ""


# ── Start Tests ────────────────────────────────────────────────


class TestStart:
    """Tests for DindManager.start()."""

    def test_start_creates_container(self):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.attrs = {
            "NetworkSettings": {
                "IPAddress": "172.17.0.5",
                "Networks": {"bridge": {"IPAddress": "172.17.0.5"}},
            }
        }
        mock_client.containers.run.return_value = mock_container
        mock_client.containers.get.side_effect = [
            DockerNotFound("not found"),
            mock_container,
            mock_container,
        ]

        manager = _make_manager(docker_client=mock_client)

        with patch.object(manager, "_wait_for_healthy", return_value=True):
            status = manager.start()

        assert status.status == DindStatusValue.HEALTHY
        assert status.container_id == "abc123def456"
        assert "172.17.0.5" in status.daemon_url
        mock_client.containers.run.assert_called_once()

    def test_start_with_network(self):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.attrs = {
            "NetworkSettings": {
                "Networks": {"test-net": {"IPAddress": "172.18.0.5"}},
            }
        }
        mock_client.containers.run.return_value = mock_container
        mock_client.containers.get.side_effect = [
            DockerNotFound("not found"),
            mock_container,
            mock_container,
        ]

        manager = _make_manager(docker_client=mock_client)

        with patch.object(manager, "_wait_for_healthy", return_value=True):
            manager.start(network_name="test-net")

        run_kwargs = mock_client.containers.run.call_args
        assert run_kwargs[1]["network"] == "test-net"

    def test_start_removes_stale_container(self):
        mock_client = MagicMock()
        mock_existing = MagicMock()
        mock_new = MagicMock()
        mock_new.id = "new123"
        mock_new.attrs = {
            "NetworkSettings": {
                "Networks": {"bridge": {"IPAddress": "172.17.0.5"}},
            }
        }
        mock_client.containers.get.side_effect = [
            mock_existing,  # Found stale container
            mock_new,  # After start, for IP lookup
            mock_new,  # For daemon URL
        ]
        mock_client.containers.run.return_value = mock_new

        manager = _make_manager(docker_client=mock_client)

        with patch.object(manager, "_wait_for_healthy", return_value=True):
            manager.start()

        mock_existing.remove.assert_called_once_with(force=True)

    def test_start_unhealthy_on_timeout(self):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.id = "abc123"

        mock_client.containers.get.side_effect = [
            DockerNotFound("not found"),
        ]
        mock_client.containers.run.return_value = mock_container

        manager = _make_manager(docker_client=mock_client)

        with patch.object(manager, "_wait_for_healthy", return_value=False):
            status = manager.start()

        assert status.status == DindStatusValue.UNHEALTHY
        assert "healthy" in status.error_message.lower()

    def test_start_idempotent(self):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.id = "abc123"
        mock_container.attrs = {
            "NetworkSettings": {
                "Networks": {"bridge": {"IPAddress": "172.17.0.5"}},
            }
        }

        mock_client.containers.get.side_effect = [
            DockerNotFound("not found"),
            mock_container,
            mock_container,
        ]
        mock_client.containers.run.return_value = mock_container

        manager = _make_manager(docker_client=mock_client)

        with patch.object(manager, "_wait_for_healthy", return_value=True):
            status1 = manager.start()
            status2 = manager.start()

        assert status1 is status2
        assert mock_client.containers.run.call_count == 1

    def test_start_docker_failure_raises(self):
        mock_client = MagicMock()

        mock_client.containers.get.side_effect = DockerNotFound("nope")
        mock_client.containers.run.side_effect = Exception("Docker daemon unavailable")

        manager = _make_manager(docker_client=mock_client)

        with pytest.raises(DindStartupError, match="Docker daemon unavailable"):
            manager.start()

        assert manager.status.status == DindStatusValue.ERROR


# ── Health Check Tests ──────────────────────────────────────────


class TestHealthCheck:
    """Tests for _wait_for_healthy."""

    def test_healthy_on_first_try(self):
        manager = _make_manager()
        manager._container_id = "abc123"

        mock_container = MagicMock()
        mock_container.attrs = {
            "NetworkSettings": {
                "Networks": {"bridge": {"IPAddress": "127.0.0.1"}},
            }
        }
        manager.docker_client.containers.get.return_value = mock_container

        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock

            result = manager._wait_for_healthy(timeout_seconds=5)

        assert result is True
        mock_sock.connect.assert_called_once_with(("127.0.0.1", DIND_PORT))

    def test_unhealthy_on_timeout(self):
        manager = _make_manager()
        manager._container_id = "abc123"

        mock_container = MagicMock()
        mock_container.attrs = {
            "NetworkSettings": {
                "Networks": {"bridge": {"IPAddress": "127.0.0.1"}},
            }
        }
        manager.docker_client.containers.get.return_value = mock_container

        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_sock.connect.side_effect = OSError("Connection refused")
            mock_socket_cls.return_value = mock_sock

            with patch("time.sleep"):
                result = manager._wait_for_healthy(timeout_seconds=0.1)

        assert result is False

    def test_healthy_after_retries(self):
        manager = _make_manager()
        manager._container_id = "abc123"

        mock_container = MagicMock()
        mock_container.attrs = {
            "NetworkSettings": {
                "Networks": {"bridge": {"IPAddress": "127.0.0.1"}},
            }
        }
        manager.docker_client.containers.get.return_value = mock_container

        call_count = 0

        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()

            def connect_side_effect(addr):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise OSError("Connection refused")

            mock_sock.connect.side_effect = connect_side_effect
            mock_socket_cls.return_value = mock_sock

            with patch("time.sleep"):
                result = manager._wait_for_healthy(timeout_seconds=30)

        assert result is True
        assert call_count == 3


# ── Image Pre-load Tests ────────────────────────────────────────


class TestPreloadImages:
    """Tests for preload_images and build_preload_command."""

    def test_build_preload_command(self):
        manager = _make_manager()
        manager._status.daemon_url = "tcp://172.17.0.5:2375"

        save_cmd, load_cmd = manager.build_preload_command("egg-gateway:latest")

        assert save_cmd == ["docker", "save", "egg-gateway:latest"]
        assert load_cmd == ["docker", "-H", "tcp://172.17.0.5:2375", "load"]

    def test_preload_success(self):
        manager = _make_manager()
        manager._started = True
        manager._status.daemon_url = "tcp://172.17.0.5:2375"

        mock_save = MagicMock()
        mock_save.stdout = b"image data"
        mock_save.returncode = 0
        mock_save.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_save), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Loaded image", stderr="")
            loaded = manager.preload_images(["egg-gateway:latest"])

        assert loaded == ["egg-gateway:latest"]
        assert manager.status.preloaded_images == ["egg-gateway:latest"]

    def test_preload_partial_failure(self):
        manager = _make_manager()
        manager._started = True
        manager._status.daemon_url = "tcp://172.17.0.5:2375"

        mock_save = MagicMock()
        mock_save.stdout = b"image data"
        mock_save.returncode = 0
        mock_save.wait.return_value = 0
        mock_save.stderr = MagicMock()
        mock_save.stderr.read.return_value = b""

        call_count = 0

        def run_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock(returncode=1, stdout="", stderr="load failed")
            return MagicMock(returncode=0, stdout="Loaded image", stderr="")

        with patch("subprocess.Popen", return_value=mock_save), \
             patch("subprocess.run", side_effect=run_side_effect):
            loaded = manager.preload_images(["bad-image", "good-image"])

        assert loaded == ["good-image"]

    def test_preload_all_fail_raises(self):
        manager = _make_manager()
        manager._started = True
        manager._status.daemon_url = "tcp://172.17.0.5:2375"

        mock_save = MagicMock()
        mock_save.stdout = b""
        mock_save.returncode = 1
        mock_save.wait.return_value = 1
        mock_save.stderr = MagicMock()
        mock_save.stderr.read.return_value = b"save failed"

        with patch("subprocess.Popen", return_value=mock_save), \
             patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="fail")):
            with pytest.raises(DindImageLoadError, match="Failed to pre-load"):
                manager.preload_images(["egg-gateway:latest"])

    def test_preload_requires_running_sidecar(self):
        manager = _make_manager()

        with pytest.raises(DindError, match="not running"):
            manager.preload_images(["egg-gateway:latest"])


# ── Teardown Tests ──────────────────────────────────────────────


class TestTeardown:
    """Tests for DindManager.teardown()."""

    def test_teardown_removes_container(self):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container

        manager = _make_manager(docker_client=mock_client)
        manager._container_id = "abc123"
        manager._started = True

        manager.teardown()

        mock_container.remove.assert_called_once_with(force=True)
        assert manager.status.status == DindStatusValue.STOPPED
        assert manager._started is False
        assert manager._container_id == ""

    def test_teardown_idempotent(self):
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = DockerNotFound("gone")

        manager = _make_manager(docker_client=mock_client)
        manager._container_id = "abc123"

        # Should not raise
        manager.teardown()
        manager.teardown()

        assert manager.status.status == DindStatusValue.STOPPED

    def test_teardown_handles_removal_error(self):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.remove.side_effect = Exception("removal failed")
        mock_client.containers.get.return_value = mock_container

        manager = _make_manager(docker_client=mock_client)
        manager._container_id = "abc123"
        manager._started = True

        # Should not raise, just log warning
        manager.teardown()

        assert manager.status.status == DindStatusValue.STOPPED
        assert manager._started is False

    def test_teardown_no_container(self):
        manager = _make_manager()

        # Should not raise when nothing to clean up
        manager.teardown()

        assert manager.status.status == DindStatusValue.STOPPED


# ── Status Serialization ────────────────────────────────────────


class TestDindStatus:
    """Tests for DindStatus serialization."""

    def test_to_dict_basic(self):
        from dind_manager import DindStatus

        status = DindStatus(
            status=DindStatusValue.HEALTHY,
            container_id="abc123",
            daemon_url="tcp://172.17.0.5:2375",
        )
        d = status.to_dict()

        assert d["status"] == "healthy"
        assert d["container_id"] == "abc123"
        assert d["daemon_url"] == "tcp://172.17.0.5:2375"
        assert "preloaded_images" not in d  # Empty list omitted

    def test_to_dict_with_images(self):
        from dind_manager import DindStatus

        status = DindStatus(
            status=DindStatusValue.HEALTHY,
            preloaded_images=["egg-gateway", "egg-orchestrator"],
        )
        d = status.to_dict()

        assert d["preloaded_images"] == ["egg-gateway", "egg-orchestrator"]
