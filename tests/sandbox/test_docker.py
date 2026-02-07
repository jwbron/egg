"""Tests for sandbox/egg_lib/docker.py - Docker image management."""

import hashlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.docker import (
    _create_network,
    _hash_directory,
    _hash_file,
    build_image,
    check_claude_update,
    check_docker,
    check_docker_permissions,
    compute_build_hash,
    ensure_egg_network,
    ensure_gateway_networks,
    get_image_build_hash,
    get_installed_claude_version,
    get_latest_claude_version,
    image_exists,
    set_force_rebuild,
    should_rebuild_image,
    teardown_networks,
)


def _mock_context(**overrides):
    """Create a mock context with sensible defaults."""
    ctx = MagicMock()
    ctx.sandbox_image = "egg-sandbox:latest"
    ctx.skip_build = False
    ctx.isolated_network = "egg-isolated"
    ctx.external_network = "egg-external"
    ctx.isolated_subnet = "172.32.0.0/24"
    ctx.external_subnet = "172.33.0.0/24"
    ctx.gateway_isolated_ip = "172.32.0.2"
    ctx.gateway_external_ip = "172.33.0.2"
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


class TestCheckDockerPermissions:
    """Tests for check_docker_permissions."""

    def test_success(self):
        """Returns True when docker ps succeeds."""
        mock_result = MagicMock(returncode=0, stderr="")
        with patch("egg_lib.docker.subprocess.run", return_value=mock_result):
            assert check_docker_permissions() is True

    def test_permission_denied(self):
        """Returns False with permission denied error."""
        mock_result = MagicMock(returncode=1, stderr="permission denied")
        with patch("egg_lib.docker.subprocess.run", return_value=mock_result):
            assert check_docker_permissions() is False

    def test_other_failure(self):
        """Returns False on other failures."""
        mock_result = MagicMock(returncode=1, stderr="unknown error")
        with patch("egg_lib.docker.subprocess.run", return_value=mock_result):
            assert check_docker_permissions() is False


class TestCheckDocker:
    """Tests for check_docker."""

    def test_docker_installed_and_working(self):
        """Returns True when docker is installed and permissions OK."""
        with patch("egg_lib.docker.subprocess.run") as mock_run:
            # which docker succeeds
            mock_run.side_effect = [
                MagicMock(returncode=0),  # which docker
                MagicMock(returncode=0, stderr=""),  # docker ps
            ]
            assert check_docker() is True

    def test_docker_not_installed_macos(self):
        """Returns False on macOS when docker not installed."""
        with patch("egg_lib.docker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)  # which docker fails
            with patch("egg_lib.config.get_platform", return_value="macos"):
                assert check_docker() is False


class TestSetForceRebuild:
    """Tests for set_force_rebuild."""

    def test_sets_flag(self):
        """Sets the global flag."""
        import egg_lib.docker as docker_mod

        original = docker_mod._force_rebuild
        try:
            set_force_rebuild(True)
            assert docker_mod._force_rebuild is True
            set_force_rebuild(False)
            assert docker_mod._force_rebuild is False
        finally:
            docker_mod._force_rebuild = original


class TestImageExists:
    """Tests for image_exists."""

    def test_exists(self):
        """Returns True when image exists."""
        ctx = _mock_context()
        with patch("egg_lib.docker.get_context", return_value=ctx):
            with patch("egg_lib.docker.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                assert image_exists() is True

    def test_not_exists(self):
        """Returns False when image doesn't exist."""
        ctx = _mock_context()
        with patch("egg_lib.docker.get_context", return_value=ctx):
            with patch("egg_lib.docker.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1)
                assert image_exists() is False


class TestGetInstalledClaudeVersion:
    """Tests for get_installed_claude_version."""

    def test_returns_version(self):
        """Returns version string from image."""
        with patch("egg_lib.docker.image_exists", return_value=True):
            with patch("egg_lib.docker.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="claude 2.1.7\n")
                result = get_installed_claude_version()
                assert result == "2.1.7"

    def test_returns_none_no_image(self):
        """Returns None when image doesn't exist."""
        with patch("egg_lib.docker.image_exists", return_value=False):
            assert get_installed_claude_version() is None

    def test_returns_none_on_failure(self):
        """Returns None on subprocess failure."""
        with patch("egg_lib.docker.image_exists", return_value=True):
            with patch("egg_lib.docker.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout="")
                assert get_installed_claude_version() is None

    def test_returns_none_on_exception(self):
        """Returns None on exception."""
        with patch("egg_lib.docker.image_exists", return_value=True):
            with patch("egg_lib.docker.subprocess.run", side_effect=Exception("error")):
                assert get_installed_claude_version() is None


class TestGetLatestClaudeVersion:
    """Tests for get_latest_claude_version."""

    def test_returns_version(self):
        """Returns version from npm registry."""
        import json

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"version": "2.1.17"}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = get_latest_claude_version()
            assert result == "2.1.17"

    def test_returns_none_on_error(self):
        """Returns None on network error."""
        with patch("urllib.request.urlopen", side_effect=Exception("network error")):
            assert get_latest_claude_version() is None


class TestCheckClaudeUpdate:
    """Tests for check_claude_update."""

    def test_update_available(self):
        """Returns new version when update available."""
        with patch("egg_lib.docker.get_installed_claude_version", return_value="2.1.7"):
            with patch("egg_lib.docker.get_latest_claude_version", return_value="2.1.17"):
                with patch("egg_lib.docker.get_quiet_mode", return_value=True):
                    result = check_claude_update()
                    assert result == "2.1.17"

    def test_no_update(self):
        """Returns None when versions match."""
        with patch("egg_lib.docker.get_installed_claude_version", return_value="2.1.17"):
            with patch("egg_lib.docker.get_latest_claude_version", return_value="2.1.17"):
                result = check_claude_update()
                assert result is None

    def test_no_installed_version(self):
        """Returns latest when no version installed."""
        with patch("egg_lib.docker.get_installed_claude_version", return_value=None):
            with patch("egg_lib.docker.get_latest_claude_version", return_value="2.1.17"):
                result = check_claude_update()
                assert result == "2.1.17"

    def test_check_fails(self):
        """Returns None when version check fails."""
        with patch("egg_lib.docker.get_installed_claude_version", return_value="2.1.7"):
            with patch("egg_lib.docker.get_latest_claude_version", return_value=None):
                result = check_claude_update()
                assert result is None


class TestHashFile:
    """Tests for _hash_file."""

    def test_hashes_content(self, tmp_path):
        """Adds file content to hasher."""
        f = tmp_path / "test.txt"
        f.write_text("hello")
        h = hashlib.sha256()
        _hash_file(f, h)
        assert h.hexdigest() != hashlib.sha256().hexdigest()

    def test_handles_missing_file(self, tmp_path):
        """Handles missing file gracefully."""
        h = hashlib.sha256()
        _hash_file(tmp_path / "nonexistent.txt", h)
        assert h.hexdigest() == hashlib.sha256().hexdigest()


class TestHashDirectory:
    """Tests for _hash_directory."""

    def test_hashes_directory(self, tmp_path):
        """Hashes files in directory."""
        (tmp_path / "a.py").write_text("code")
        h = hashlib.sha256()
        _hash_directory(tmp_path, h)
        assert h.hexdigest() != hashlib.sha256().hexdigest()


class TestComputeBuildHash:
    """Tests for compute_build_hash."""

    def test_returns_hex_string(self):
        """Returns a valid hex hash string."""
        result = compute_build_hash()
        assert isinstance(result, str)
        assert len(result) == 64
        int(result, 16)  # Valid hex


class TestGetImageBuildHash:
    """Tests for get_image_build_hash."""

    def test_returns_hash(self):
        """Returns hash from image label."""
        with patch("egg_lib.docker.image_exists", return_value=True):
            with patch("egg_lib.docker.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="abc123def456\n")
                result = get_image_build_hash()
                assert result == "abc123def456"

    def test_returns_none_no_image(self):
        """Returns None when image doesn't exist."""
        with patch("egg_lib.docker.image_exists", return_value=False):
            assert get_image_build_hash() is None

    def test_returns_none_no_label(self):
        """Returns None when label is not set."""
        with patch("egg_lib.docker.image_exists", return_value=True):
            with patch("egg_lib.docker.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="<no value>\n")
                assert get_image_build_hash() is None

    def test_returns_none_on_exception(self):
        """Returns None on exception."""
        with patch("egg_lib.docker.image_exists", return_value=True):
            with patch("egg_lib.docker.subprocess.run", side_effect=Exception("error")):
                assert get_image_build_hash() is None


class TestShouldRebuildImage:
    """Tests for should_rebuild_image."""

    def test_force_rebuild(self):
        """Returns True when force rebuild flag is set."""
        import egg_lib.docker as docker_mod

        original = docker_mod._force_rebuild
        try:
            docker_mod._force_rebuild = True
            rebuild, reason = should_rebuild_image()
            assert rebuild is True
            assert "forced" in reason.lower()
        finally:
            docker_mod._force_rebuild = original

    def test_no_image(self):
        """Returns True when image doesn't exist."""
        import egg_lib.docker as docker_mod

        original = docker_mod._force_rebuild
        try:
            docker_mod._force_rebuild = False
            with patch("egg_lib.docker.image_exists", return_value=False):
                rebuild, reason = should_rebuild_image()
                assert rebuild is True
        finally:
            docker_mod._force_rebuild = original

    def test_hash_matches(self):
        """Returns False when hash matches."""
        import egg_lib.docker as docker_mod

        original = docker_mod._force_rebuild
        try:
            docker_mod._force_rebuild = False
            hash_val = "abc123" * 10 + "abcd"
            with patch("egg_lib.docker.image_exists", return_value=True):
                with patch("egg_lib.docker.compute_build_hash", return_value=hash_val):
                    with patch("egg_lib.docker.get_image_build_hash", return_value=hash_val):
                        with patch("egg_lib.docker.check_claude_update", return_value=None):
                            rebuild, reason = should_rebuild_image()
                            assert rebuild is False
        finally:
            docker_mod._force_rebuild = original

    def test_hash_changed(self):
        """Returns True when hash doesn't match."""
        import egg_lib.docker as docker_mod

        original = docker_mod._force_rebuild
        try:
            docker_mod._force_rebuild = False
            with patch("egg_lib.docker.image_exists", return_value=True):
                with patch("egg_lib.docker.compute_build_hash", return_value="new-hash"):
                    with patch("egg_lib.docker.get_image_build_hash", return_value="old-hash"):
                        rebuild, reason = should_rebuild_image()
                        assert rebuild is True
        finally:
            docker_mod._force_rebuild = original


class TestBuildImage:
    """Tests for build_image."""

    def test_skip_build(self):
        """Returns True when skip_build is set."""
        ctx = _mock_context(skip_build=True)
        with patch("egg_lib.docker.get_context", return_value=ctx):
            with patch("egg_lib.docker.get_quiet_mode", return_value=True):
                assert build_image() is True

    def test_no_rebuild_needed(self):
        """Returns True when no rebuild needed."""
        ctx = _mock_context(skip_build=False)
        with patch("egg_lib.docker.get_context", return_value=ctx):
            with patch("egg_lib.docker.get_quiet_mode", return_value=True):
                with patch(
                    "egg_lib.docker.should_rebuild_image", return_value=(False, "up to date")
                ):
                    assert build_image() is True


class TestEnsureEggNetwork:
    """Tests for ensure_egg_network."""

    def test_network_exists(self):
        """Returns True when network already exists."""
        ctx = _mock_context()
        with patch("egg_lib.docker.get_context", return_value=ctx):
            with patch("egg_lib.docker.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                assert ensure_egg_network() is True

    def test_network_created(self):
        """Returns True when network is created."""
        ctx = _mock_context()
        with patch("egg_lib.docker.get_context", return_value=ctx):
            with patch("egg_lib.docker.subprocess.run") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=1, stderr="not found"),  # inspect fails
                    MagicMock(returncode=0),  # create succeeds
                ]
                assert ensure_egg_network() is True

    def test_network_creation_fails(self):
        """Returns False when network creation fails."""
        ctx = _mock_context()
        with patch("egg_lib.docker.get_context", return_value=ctx):
            with patch("egg_lib.docker.subprocess.run") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=1, stderr="not found"),  # inspect fails
                    MagicMock(returncode=1, stderr="error"),  # create fails
                ]
                assert ensure_egg_network() is False


class TestCreateNetwork:
    """Tests for _create_network."""

    def test_network_exists(self):
        """Returns True when network already exists."""
        with patch("egg_lib.docker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert _create_network("test-net", "172.32.0.0/24") is True

    def test_creates_network(self):
        """Creates network when it doesn't exist."""
        with patch("egg_lib.docker.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1, stderr="not found"),  # inspect
                MagicMock(returncode=0),  # create
            ]
            assert _create_network("test-net", "172.32.0.0/24") is True

    def test_creates_internal_network(self):
        """Creates internal network with --internal flag."""
        with patch("egg_lib.docker.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1, stderr="not found"),  # inspect
                MagicMock(returncode=0),  # create
            ]
            assert _create_network("test-net", "172.32.0.0/24", internal=True) is True
            # Verify --internal was in the command
            create_call = mock_run.call_args_list[1]
            cmd = create_call[0][0]
            assert "--internal" in cmd

    def test_creation_fails(self):
        """Returns False when creation fails."""
        with patch("egg_lib.docker.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1, stderr="not found"),  # inspect
                MagicMock(returncode=1, stderr="error"),  # create fails
            ]
            assert _create_network("test-net", "172.32.0.0/24") is False


class TestEnsureGatewayNetworks:
    """Tests for ensure_gateway_networks."""

    def test_both_networks_created(self):
        """Returns True when both networks created."""
        ctx = _mock_context()
        with patch("egg_lib.docker.get_context", return_value=ctx):
            with patch("egg_lib.docker._create_network", return_value=True):
                assert ensure_gateway_networks() is True

    def test_isolated_network_fails(self):
        """Returns False when isolated network creation fails."""
        ctx = _mock_context()
        with patch("egg_lib.docker.get_context", return_value=ctx):
            with patch("egg_lib.docker._create_network", return_value=False):
                assert ensure_gateway_networks() is False

    def test_external_network_fails(self):
        """Returns False when external network creation fails."""
        ctx = _mock_context()
        with patch("egg_lib.docker.get_context", return_value=ctx):
            with patch("egg_lib.docker._create_network") as mock_create:
                mock_create.side_effect = [True, False]  # isolated ok, external fails
                assert ensure_gateway_networks() is False


class TestIsDangerousDir:
    """Tests for is_dangerous_dir."""

    def test_safe_directory(self, tmp_path):
        """Returns False for safe directory."""
        from egg_lib.docker import is_dangerous_dir

        safe_dir = tmp_path / "safe"
        safe_dir.mkdir()
        assert is_dangerous_dir(safe_dir) is False

    def test_dangerous_directory(self):
        """Returns True for dangerous directory."""
        # Use actual Config.DANGEROUS_DIRS
        from egg_lib.config import Config
        from egg_lib.docker import is_dangerous_dir

        if Config.DANGEROUS_DIRS:
            # Test that an actual dangerous dir is detected
            dangerous = Config.DANGEROUS_DIRS[0]
            if dangerous.exists():
                assert is_dangerous_dir(dangerous) is True


class TestCopyDirectoryAtomic:
    """Tests for _copy_directory_atomic."""

    def test_successful_copy(self, tmp_path):
        """Copies directory atomically."""
        from egg_lib.docker import _copy_directory_atomic

        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("content")

        dest = tmp_path / "dest"

        result = _copy_directory_atomic(src, dest, "Test", quiet=True)
        assert result is True
        assert (dest / "file.txt").exists()

    def test_overwrites_existing(self, tmp_path):
        """Overwrites existing destination."""
        from egg_lib.docker import _copy_directory_atomic

        src = tmp_path / "src"
        src.mkdir()
        (src / "new_file.txt").write_text("new content")

        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "old_file.txt").write_text("old content")

        result = _copy_directory_atomic(src, dest, "Test", quiet=True)
        assert result is True
        assert (dest / "new_file.txt").exists()


class TestAllocateDynamicSubnet:
    """Tests for _allocate_dynamic_subnet."""

    def test_allocates_subnet(self):
        """Allocates an unused subnet."""
        from egg_lib.docker import _allocate_dynamic_subnet

        with patch("egg_lib.docker.subprocess.run") as mock_run:
            # No existing networks
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = _allocate_dynamic_subnet()
            assert result.startswith("172.")
            assert result.endswith(".0/24")


class TestTeardownNetworks:
    """Tests for teardown_networks."""

    def test_removes_both_networks(self):
        """Removes both networks."""
        ctx = _mock_context()
        with patch("egg_lib.docker.get_context", return_value=ctx):
            with patch("egg_lib.docker.subprocess.run") as mock_run:
                teardown_networks()
                assert mock_run.call_count == 2
