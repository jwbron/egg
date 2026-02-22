"""
Additional unit tests for DindManager edge cases and uncovered branches.

Fills gaps in the coder's original test_dind_manager.py:
- _get_container_ip edge cases (no IP available, no networks)
- preload_images with empty list
- preload_images timeout handling
- start() when docker module is None
- DindStatus serialization with error_message
- _wait_for_healthy socket cleanup edge cases
- Container name with special characters in pipeline_id
- Resource limit configuration verification
"""

import subprocess
from unittest.mock import MagicMock, patch

import dind_manager as dind_mod
import pytest
from dind_manager import (
    DIND_IMAGE,
    DindError,
    DindManager,
    DindStartupError,
    DindStatus,
    DindStatusValue,
    DockerNotFound,
)


@pytest.fixture(autouse=True)
def _mock_docker_module():
    """Ensure dind_manager.docker is truthy so the SDK guard passes."""
    sentinel = object()
    with patch.object(dind_mod, "docker", sentinel):
        yield


def _make_manager(
    pipeline_id="issue-647",
    docker_client=None,
) -> DindManager:
    """Create a DindManager with mocked Docker client."""
    return DindManager(
        pipeline_id=pipeline_id,
        docker_client=docker_client if docker_client is not None else MagicMock(),
    )


# ── _get_container_ip edge cases ─────────────────────────────────


class TestGetContainerIpEdgeCases:
    """Tests for _get_container_ip boundary conditions."""

    def test_no_networks_uses_global_ip(self):
        """Falls back to global IPAddress when Networks dict has no IPs."""
        manager = _make_manager()
        manager._container_id = "abc123"

        mock_container = MagicMock()
        mock_container.attrs = {
            "NetworkSettings": {
                "IPAddress": "172.17.0.99",
                "Networks": {
                    "bridge": {"IPAddress": ""},
                },
            }
        }
        manager.docker_client.containers.get.return_value = mock_container

        ip = manager._get_container_ip()
        assert ip == "172.17.0.99"

    def test_no_ip_anywhere_raises(self):
        """Raises DindStartupError when no IP is available anywhere."""
        manager = _make_manager()
        manager._container_id = "abc123"

        mock_container = MagicMock()
        mock_container.attrs = {
            "NetworkSettings": {
                "IPAddress": "",
                "Networks": {
                    "bridge": {"IPAddress": ""},
                },
            }
        }
        manager.docker_client.containers.get.return_value = mock_container

        with pytest.raises(DindStartupError, match="no IP address"):
            manager._get_container_ip()

    def test_empty_networks_dict_uses_global_ip(self):
        """Falls back to global IPAddress when Networks is empty."""
        manager = _make_manager()
        manager._container_id = "abc123"

        mock_container = MagicMock()
        mock_container.attrs = {
            "NetworkSettings": {
                "IPAddress": "172.17.0.42",
                "Networks": {},
            }
        }
        manager.docker_client.containers.get.return_value = mock_container

        ip = manager._get_container_ip()
        assert ip == "172.17.0.42"

    def test_docker_get_raises_wraps_in_startup_error(self):
        """Wraps Docker client errors in DindStartupError."""
        manager = _make_manager()
        manager._container_id = "abc123"
        manager.docker_client.containers.get.side_effect = Exception("connection refused")

        with pytest.raises(DindStartupError, match="Failed to get DinD container IP"):
            manager._get_container_ip()

    def test_multiple_networks_returns_first_with_ip(self):
        """Returns the first network that has a non-empty IP."""
        manager = _make_manager()
        manager._container_id = "abc123"

        mock_container = MagicMock()
        mock_container.attrs = {
            "NetworkSettings": {
                "Networks": {
                    "empty-net": {"IPAddress": ""},
                    "real-net": {"IPAddress": "10.0.0.5"},
                    "other-net": {"IPAddress": "10.0.0.6"},
                },
            }
        }
        manager.docker_client.containers.get.return_value = mock_container

        ip = manager._get_container_ip()
        assert ip == "10.0.0.5"


# ── preload_images edge cases ────────────────────────────────────


class TestPreloadImagesEdgeCases:
    """Tests for preload_images boundary conditions."""

    def test_preload_empty_list_returns_empty(self):
        """preload_images with empty list returns empty without error."""
        manager = _make_manager()
        manager._started = True
        manager._status.daemon_url = "tcp://172.17.0.5:2375"

        # Empty list should not raise DindImageLoadError (guard: `not loaded and image_names`)
        loaded = manager.preload_images([])
        assert loaded == []

    def test_preload_timeout_continues_to_next(self):
        """Timeout on one image doesn't prevent loading others."""
        manager = _make_manager()
        manager._started = True
        manager._status.daemon_url = "tcp://172.17.0.5:2375"

        call_count = 0

        def popen_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            mock.stdout = b"data"
            if call_count == 1:
                mock.wait.side_effect = subprocess.TimeoutExpired(cmd="docker save", timeout=300)
            else:
                mock.returncode = 0
                mock.wait.return_value = 0
            return mock

        with patch("subprocess.Popen", side_effect=popen_side_effect), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Loaded", stderr="")
            loaded = manager.preload_images(["slow-image", "fast-image"])

        assert "fast-image" in loaded

    def test_preload_generic_exception_continues(self):
        """Generic exception on one image continues to next."""
        manager = _make_manager()
        manager._started = True
        manager._status.daemon_url = "tcp://172.17.0.5:2375"

        call_count = 0

        def popen_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("permission denied")
            mock = MagicMock()
            mock.stdout = b"data"
            mock.returncode = 0
            mock.wait.return_value = 0
            return mock

        with patch("subprocess.Popen", side_effect=popen_side_effect), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Loaded", stderr="")
            loaded = manager.preload_images(["bad-image", "good-image"])

        assert loaded == ["good-image"]


# ── start() edge cases ──────────────────────────────────────────


class TestStartEdgeCases:
    """Tests for DindManager.start() edge cases."""

    def test_start_raises_when_docker_is_none(self):
        """start() raises DindError when docker SDK is unavailable."""
        with patch.object(dind_mod, "docker", None):
            manager = DindManager(
                pipeline_id="test",
                docker_client=MagicMock(),
            )
            with pytest.raises(DindError, match="docker SDK"):
                manager.start()

    def test_start_sets_error_status_on_dind_error(self):
        """start() sets ERROR status when a DindError subclass is raised."""
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = DockerNotFound("not found")
        mock_client.containers.run.side_effect = DindStartupError("custom error")

        manager = _make_manager(docker_client=mock_client)

        with pytest.raises(DindStartupError, match="custom error"):
            manager.start()

        assert manager.status.status == DindStatusValue.ERROR

    def test_start_container_kwargs_includes_resource_limits(self):
        """start() sets CPU and memory limits on the DinD container."""
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
            manager.start()

        run_kwargs = mock_client.containers.run.call_args[1]
        assert run_kwargs["privileged"] is True
        assert run_kwargs["mem_limit"] == "2g"
        assert run_kwargs["cpu_quota"] == 200000  # 2.0 * 100000
        assert run_kwargs["cpu_period"] == 100000

    def test_start_sets_correct_labels(self):
        """start() sets egg.dind and egg.pipeline.id labels."""
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

        manager = _make_manager(pipeline_id="issue-42", docker_client=mock_client)

        with patch.object(manager, "_wait_for_healthy", return_value=True):
            manager.start()

        run_kwargs = mock_client.containers.run.call_args[1]
        assert run_kwargs["labels"]["egg.dind"] == "true"
        assert run_kwargs["labels"]["egg.pipeline.id"] == "issue-42"

    def test_start_disables_tls(self):
        """start() sets DOCKER_TLS_CERTDIR='' to disable TLS."""
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
            manager.start()

        run_kwargs = mock_client.containers.run.call_args[1]
        assert run_kwargs["environment"]["DOCKER_TLS_CERTDIR"] == ""

    def test_start_uses_correct_image(self):
        """start() uses the DIND_IMAGE constant."""
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
            manager.start()

        run_kwargs = mock_client.containers.run.call_args[1]
        assert run_kwargs["image"] == DIND_IMAGE


# ── Container naming ─────────────────────────────────────────────


class TestContainerNaming:
    """Tests for DinD container naming with various pipeline IDs."""

    def test_container_name_with_hyphens(self):
        """Pipeline IDs with hyphens produce valid container names."""
        manager = _make_manager(pipeline_id="issue-123-retry-2")
        assert manager.container_name == "egg-dind-issue-123-retry-2"

    def test_container_name_with_underscores(self):
        """Pipeline IDs with underscores produce valid container names."""
        manager = _make_manager(pipeline_id="my_pipeline_v2")
        assert manager.container_name == "egg-dind-my_pipeline_v2"


# ── DindStatus serialization edge cases ──────────────────────────


class TestDindStatusEdgeCases:
    """Tests for DindStatus serialization edge cases."""

    def test_to_dict_with_error_message(self):
        """to_dict includes error_message when set."""
        status = DindStatus(
            status=DindStatusValue.ERROR,
            error_message="daemon crashed",
        )
        d = status.to_dict()

        assert d["status"] == "error"
        assert d["error_message"] == "daemon crashed"

    def test_to_dict_stopped_state(self):
        """to_dict for a stopped DinD includes empty fields."""
        status = DindStatus()
        d = status.to_dict()

        assert d["status"] == "stopped"
        assert d["container_id"] == ""
        assert d["daemon_url"] == ""
        assert d["error_message"] == ""
        assert "preloaded_images" not in d

    def test_to_dict_starting_state(self):
        """to_dict for a starting DinD."""
        status = DindStatus(
            status=DindStatusValue.STARTING,
            container_id="abc123",
        )
        d = status.to_dict()

        assert d["status"] == "starting"
        assert d["container_id"] == "abc123"

    def test_to_dict_unhealthy_state(self):
        """to_dict for an unhealthy DinD includes error message."""
        status = DindStatus(
            status=DindStatusValue.UNHEALTHY,
            container_id="abc123",
            error_message="timeout waiting for health",
        )
        d = status.to_dict()

        assert d["status"] == "unhealthy"
        assert d["error_message"] == "timeout waiting for health"


# ── _wait_for_healthy edge cases ─────────────────────────────────


class TestWaitForHealthyEdgeCases:
    """Tests for _wait_for_healthy edge cases."""

    def test_finally_close_error_is_swallowed(self):
        """Exception in the finally block's sock.close() is caught and ignored."""
        manager = _make_manager()
        manager._container_id = "abc123"

        mock_container = MagicMock()
        mock_container.attrs = {
            "NetworkSettings": {
                "Networks": {"bridge": {"IPAddress": "127.0.0.1"}},
            }
        }
        manager.docker_client.containers.get.return_value = mock_container

        # Simulate: connect fails (triggers except), then finally close also fails.
        # On second iteration, connect succeeds and close works.
        # Each iteration creates a fresh socket via socket.socket().
        iteration = [0]

        with patch("socket.socket") as mock_socket_cls:

            def make_sock(*args, **kwargs):
                mock_sock = MagicMock()
                iteration[0] += 1
                if iteration[0] == 1:
                    mock_sock.connect.side_effect = OSError("refused")
                    mock_sock.close.side_effect = OSError("close failed in finally")
                return mock_sock

            mock_socket_cls.side_effect = make_sock

            with patch("time.sleep"):
                result = manager._wait_for_healthy(timeout_seconds=30)

        assert result is True

    def test_os_error_during_connect_retries(self):
        """OSError (not just socket.error) during connect triggers retry."""
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
                if call_count == 1:
                    raise OSError("Network unreachable")
                # Second call succeeds

            mock_sock.connect.side_effect = connect_side_effect
            mock_socket_cls.return_value = mock_sock

            with patch("time.sleep"):
                result = manager._wait_for_healthy(timeout_seconds=30)

        assert result is True
        assert call_count == 2


# ── Teardown edge cases ──────────────────────────────────────────


class TestTeardownEdgeCases:
    """Additional teardown edge cases."""

    def test_teardown_with_not_found_error_is_silent(self):
        """Container not found during teardown doesn't log a warning."""
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = DockerNotFound("not found")

        manager = _make_manager(docker_client=mock_client)
        manager._container_id = "abc123"
        manager._started = True

        # Should not raise
        manager.teardown()

        assert manager.status.status == DindStatusValue.STOPPED
        assert manager._started is False

    def test_teardown_resets_all_state(self):
        """Teardown resets container_id, started flag, and status."""
        manager = _make_manager()
        manager._container_id = "abc123"
        manager._started = True
        manager._status = DindStatus(
            status=DindStatusValue.HEALTHY,
            container_id="abc123",
            daemon_url="tcp://172.17.0.5:2375",
        )

        manager.teardown()

        assert manager._container_id == ""
        assert manager._started is False
        assert manager.status.status == DindStatusValue.STOPPED
        assert manager.daemon_url == ""
