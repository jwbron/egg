"""Tests for sandbox/egg_lib/docker.py - Docker image management."""

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.docker import (
    _create_network,
    _has_installable_files,
    build_image,
    check_agent_sdk_update,
    check_claude_update,
    check_docker,
    check_docker_permissions,
    compute_build_hash,
    ensure_egg_network,
    ensure_gateway_networks,
    get_image_build_hash,
    get_installed_agent_sdk_version,
    get_installed_claude_version,
    get_latest_agent_sdk_version,
    get_latest_claude_version,
    hash_directory,
    hash_file,
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

    def setup_method(self):
        get_latest_claude_version.cache_clear()

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

    def setup_method(self):
        check_claude_update.cache_clear()

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


class TestGetInstalledAgentSdkVersion:
    """Tests for get_installed_agent_sdk_version."""

    def test_returns_version(self):
        """Returns version from image."""
        with patch("egg_lib.docker.image_exists", return_value=True):
            with patch("egg_lib.docker.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="0.1.5\n")
                result = get_installed_agent_sdk_version()
                assert result == "0.1.5"

    def test_returns_none_no_image(self):
        """Returns None when image doesn't exist."""
        with patch("egg_lib.docker.image_exists", return_value=False):
            assert get_installed_agent_sdk_version() is None

    def test_returns_none_on_failure(self):
        """Returns None when docker run fails."""
        with patch("egg_lib.docker.image_exists", return_value=True):
            with patch("egg_lib.docker.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout="")
                assert get_installed_agent_sdk_version() is None


class TestGetLatestAgentSdkVersion:
    """Tests for get_latest_agent_sdk_version."""

    def setup_method(self):
        get_latest_agent_sdk_version.cache_clear()

    def test_returns_version(self):
        """Returns version from PyPI."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "info": {"version": "0.1.5"},
                "releases": {"0.1.5": [{"filename": "claude_agent_sdk-0.1.5.tar.gz"}]},
            }
        ).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = get_latest_agent_sdk_version()
            assert result == "0.1.5"

    def test_falls_back_for_ghost_version(self):
        """Falls back to newest installable version when reported version has no files."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "info": {"version": "0.1.49"},
                "releases": {"0.1.48": [{"filename": "sdk-0.1.48.tar.gz"}], "0.1.49": []},
            }
        ).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = get_latest_agent_sdk_version()
            assert result == "0.1.48"

    def test_falls_back_when_version_absent_from_releases(self):
        """Falls back when the reported version key is missing from releases dict."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "info": {"version": "0.1.50"},
                "releases": {"0.1.48": [{"filename": "sdk-0.1.48.tar.gz"}]},
            }
        ).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = get_latest_agent_sdk_version()
            assert result == "0.1.48"

    def test_falls_back_for_yanked_version(self):
        """Falls back when the reported version has only yanked files."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "info": {"version": "0.1.49"},
                "releases": {
                    "0.1.48": [{"filename": "sdk-0.1.48.tar.gz"}],
                    "0.1.49": [
                        {"filename": "sdk-0.1.49.tar.gz", "yanked": True},
                    ],
                },
            }
        ).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = get_latest_agent_sdk_version()
            assert result == "0.1.48"

    def test_falls_back_for_macos_only_version(self):
        """Falls back when the reported version only has macOS wheels."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "info": {"version": "0.1.49"},
                "releases": {
                    "0.1.48": [
                        {"filename": "claude_agent_sdk-0.1.48-py3-none-manylinux_2_17_x86_64.whl"},
                        {"filename": "claude_agent_sdk-0.1.48.tar.gz"},
                    ],
                    "0.1.49": [
                        {"filename": "claude_agent_sdk-0.1.49-py3-none-macosx_11_0_arm64.whl"},
                    ],
                },
            }
        ).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = get_latest_agent_sdk_version()
            assert result == "0.1.48"

    def test_returns_none_when_no_installable_releases(self):
        """Returns None when no releases have installable files."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "info": {"version": "0.1.49"},
                "releases": {
                    "0.1.49": [
                        {"filename": "claude_agent_sdk-0.1.49-py3-none-macosx_11_0_arm64.whl"},
                    ],
                },
            }
        ).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = get_latest_agent_sdk_version()
            assert result is None

    def test_falls_back_skipping_prerelease(self):
        """Falls back to newest stable version, skipping pre-releases."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "info": {"version": "0.1.51"},
                "releases": {
                    "0.1.48": [{"filename": "sdk-0.1.48.tar.gz"}],
                    "0.1.49": [{"filename": "sdk-0.1.49.tar.gz"}],
                    "0.1.50a1": [{"filename": "sdk-0.1.50a1.tar.gz"}],
                    "0.1.50rc1": [{"filename": "sdk-0.1.50rc1.tar.gz"}],
                    "0.1.51": [],
                },
            }
        ).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = get_latest_agent_sdk_version()
            assert result == "0.1.49"

    def test_returns_none_on_error(self):
        """Returns None on network error."""
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            assert get_latest_agent_sdk_version() is None


class TestHasInstallableFiles:
    """Tests for _has_installable_files."""

    def test_tar_gz_sdist(self):
        """Source distribution .tar.gz is installable."""
        assert _has_installable_files([{"filename": "pkg-1.0.tar.gz"}]) is True

    def test_zip_sdist(self):
        """Source distribution .zip is installable."""
        assert _has_installable_files([{"filename": "pkg-1.0.zip"}]) is True

    def test_platform_agnostic_wheel(self):
        """Platform-agnostic wheel (py3-none-any) is installable."""
        assert _has_installable_files([{"filename": "pkg-1.0-py3-none-any.whl"}]) is True

    def test_linux_wheel(self):
        """Linux wheel is installable."""
        assert (
            _has_installable_files([{"filename": "pkg-1.0-cp312-cp312-manylinux_2_17_x86_64.whl"}])
            is True
        )

    def test_musllinux_wheel(self):
        """musllinux wheel is installable."""
        assert (
            _has_installable_files([{"filename": "pkg-1.0-cp312-cp312-musllinux_1_2_x86_64.whl"}])
            is True
        )

    def test_macos_only_wheel(self):
        """macOS-only wheel is not installable."""
        assert (
            _has_installable_files([{"filename": "pkg-1.0-cp312-cp312-macosx_11_0_arm64.whl"}])
            is False
        )

    def test_windows_only_wheel(self):
        """Windows-only wheel is not installable."""
        assert _has_installable_files([{"filename": "pkg-1.0-cp312-cp312-win_amd64.whl"}]) is False

    def test_yanked_files_ignored(self):
        """Yanked files are ignored."""
        assert _has_installable_files([{"filename": "pkg-1.0.tar.gz", "yanked": True}]) is False

    def test_mixed_yanked_and_non_yanked(self):
        """Non-yanked file found among yanked files."""
        assert (
            _has_installable_files(
                [
                    {"filename": "pkg-1.0.tar.gz", "yanked": True},
                    {"filename": "pkg-1.0-py3-none-any.whl"},
                ]
            )
            is True
        )

    def test_empty_files_list(self):
        """Empty file list is not installable."""
        assert _has_installable_files([]) is False


class TestCheckAgentSdkUpdate:
    """Tests for check_agent_sdk_update."""

    def setup_method(self):
        check_agent_sdk_update.cache_clear()

    def test_update_available(self):
        """Returns new version when update available."""
        with patch("egg_lib.docker.get_installed_agent_sdk_version", return_value="0.1.3"):
            with patch("egg_lib.docker.get_latest_agent_sdk_version", return_value="0.1.5"):
                with patch("egg_lib.docker.get_quiet_mode", return_value=True):
                    result = check_agent_sdk_update()
                    assert result == "0.1.5"

    def test_no_update(self):
        """Returns None when versions match."""
        with patch("egg_lib.docker.get_installed_agent_sdk_version", return_value="0.1.5"):
            with patch("egg_lib.docker.get_latest_agent_sdk_version", return_value="0.1.5"):
                result = check_agent_sdk_update()
                assert result is None

    def test_no_installed_version(self):
        """Returns latest when no version installed."""
        with patch("egg_lib.docker.get_installed_agent_sdk_version", return_value=None):
            with patch("egg_lib.docker.get_latest_agent_sdk_version", return_value="0.1.5"):
                result = check_agent_sdk_update()
                assert result == "0.1.5"

    def test_check_fails(self):
        """Returns None when version check fails."""
        with patch("egg_lib.docker.get_installed_agent_sdk_version", return_value="0.1.3"):
            with patch("egg_lib.docker.get_latest_agent_sdk_version", return_value=None):
                result = check_agent_sdk_update()
                assert result is None


class TestHashFile:
    """Tests for hash_file."""

    def test_hashes_content(self, tmp_path):
        """Adds file content to hasher."""
        f = tmp_path / "test.txt"
        f.write_text("hello")
        h = hashlib.sha256()
        hash_file(f, h)
        assert h.hexdigest() != hashlib.sha256().hexdigest()

    def test_handles_missing_file(self, tmp_path):
        """Handles missing file gracefully."""
        h = hashlib.sha256()
        hash_file(tmp_path / "nonexistent.txt", h)
        assert h.hexdigest() == hashlib.sha256().hexdigest()


class TestHashDirectory:
    """Tests for hash_directory."""

    def test_hashes_directory(self, tmp_path):
        """Hashes files in directory."""
        (tmp_path / "a.py").write_text("code")
        h = hashlib.sha256()
        hash_directory(tmp_path, h)
        assert h.hexdigest() != hashlib.sha256().hexdigest()

    def test_excludes_pycache(self, tmp_path):
        """__pycache__ directories are excluded from the hash."""
        (tmp_path / "a.py").write_text("code")
        h1 = hashlib.sha256()
        hash_directory(tmp_path, h1)
        hash_without_pycache = h1.hexdigest()

        # Add __pycache__ with .pyc files — hash should not change
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "a.cpython-312.pyc").write_bytes(b"compiled")

        h2 = hashlib.sha256()
        hash_directory(tmp_path, h2)
        assert h2.hexdigest() == hash_without_pycache

    def test_excludes_nested_pycache(self, tmp_path):
        """Nested __pycache__ directories are also excluded."""
        subpkg = tmp_path / "subpackage"
        subpkg.mkdir()
        (subpkg / "mod.py").write_text("code")
        h1 = hashlib.sha256()
        hash_directory(tmp_path, h1)
        hash_without_pycache = h1.hexdigest()

        nested_pycache = subpkg / "__pycache__"
        nested_pycache.mkdir()
        (nested_pycache / "mod.cpython-312.pyc").write_bytes(b"compiled")

        h2 = hashlib.sha256()
        hash_directory(tmp_path, h2)
        assert h2.hexdigest() == hash_without_pycache


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
                            with patch("egg_lib.docker.check_agent_sdk_update", return_value=None):
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

    def test_agent_sdk_update_triggers_rebuild(self):
        """Returns True when agent SDK update is available."""
        import egg_lib.docker as docker_mod

        original = docker_mod._force_rebuild
        try:
            docker_mod._force_rebuild = False
            hash_val = "abc123" * 10 + "abcd"
            with patch("egg_lib.docker.image_exists", return_value=True):
                with patch("egg_lib.docker.compute_build_hash", return_value=hash_val):
                    with patch("egg_lib.docker.get_image_build_hash", return_value=hash_val):
                        with patch("egg_lib.docker.check_claude_update", return_value=None):
                            with patch(
                                "egg_lib.docker.check_agent_sdk_update",
                                return_value="0.2.0",
                            ):
                                rebuild, reason = should_rebuild_image()
                                assert rebuild is True
                                assert "agent-sdk" in reason.lower() or "0.2.0" in reason
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


class TestGetLocalRepoPath:
    """Tests for _get_local_repo_path."""

    def test_finds_repo_by_full_path(self, tmp_path):
        """Matches repo by owner/name at end of path."""
        from egg_lib.docker import _get_local_repo_path

        repo_dir = tmp_path / "org" / "my-app"
        repo_dir.mkdir(parents=True)

        config = {"local_repos": {"paths": [str(repo_dir)]}}

        result = _get_local_repo_path(config, "org/my-app")
        assert result == repo_dir

    def test_finds_repo_by_name_only(self, tmp_path):
        """Falls back to matching just the repo name."""
        from egg_lib.docker import _get_local_repo_path

        repo_dir = tmp_path / "my-app"
        repo_dir.mkdir(parents=True)

        config = {"local_repos": {"paths": [str(repo_dir)]}}

        result = _get_local_repo_path(config, "org/my-app")
        assert result == repo_dir

    def test_returns_none_for_missing_repo(self, tmp_path):
        """Returns None when repo not in local_repos."""
        from egg_lib.docker import _get_local_repo_path

        config = {"local_repos": {"paths": [str(tmp_path / "other-repo")]}}

        result = _get_local_repo_path(config, "org/my-app")
        assert result is None

    def test_returns_none_for_empty_config(self):
        """Returns None with no local_repos config."""
        from egg_lib.docker import _get_local_repo_path

        result = _get_local_repo_path({}, "org/my-app")
        assert result is None


class TestCopyRepoWatchFiles:
    """Tests for _copy_repo_watch_files."""

    def test_copies_watch_files(self, tmp_path):
        """Copies watch files from local repos to build context."""
        from egg_lib.docker import _copy_repo_watch_files

        # Set up local repo with a watch file
        repo_dir = tmp_path / "org" / "web-app"
        repo_dir.mkdir(parents=True)
        (repo_dir / "package-lock.json").write_text('{"lockfileVersion": 3}')

        # Config with build_commands
        config = {
            "repo_settings": {
                "org/web-app": {
                    "build_commands": {
                        "watch_files": ["package-lock.json"],
                        "commands": ["npm ci"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        # Mock the config loading and Config.CONFIG_DIR
        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        # Check the watch file was copied
        dest = build_dir / "repo-deps" / "org--web-app" / "package-lock.json"
        assert dest.exists()
        assert dest.read_text() == '{"lockfileVersion": 3}'

    def test_writes_manifest_json(self, tmp_path):
        """Writes manifest.json with build commands into repo-deps."""
        import json

        from egg_lib.docker import _copy_repo_watch_files

        repo_dir = tmp_path / "org" / "web-app"
        repo_dir.mkdir(parents=True)
        (repo_dir / "package-lock.json").write_text("{}")

        config = {
            "repo_settings": {
                "org/web-app": {
                    "build_commands": {
                        "watch_files": ["package-lock.json"],
                        "commands": ["npm ci"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        build_commands = manifest["build_commands"]
        assert len(build_commands) == 1
        assert build_commands[0]["repo"] == "org/web-app"
        assert build_commands[0]["commands"] == ["npm ci"]
        assert build_commands[0]["watch_files"] == ["package-lock.json"]
        assert manifest["extra_packages"] == {"apt": [], "dnf": []}

    def test_manifest_includes_multiple_repos(self, tmp_path):
        """Manifest includes all repos with build_commands."""
        import json

        from egg_lib.docker import _copy_repo_watch_files

        repo_a = tmp_path / "org" / "app-a"
        repo_a.mkdir(parents=True)
        repo_b = tmp_path / "org" / "app-b"
        repo_b.mkdir(parents=True)

        config = {
            "repo_settings": {
                "org/app-a": {
                    "build_commands": {
                        "commands": ["make deps"],
                    }
                },
                "org/app-b": {
                    "build_commands": {
                        "watch_files": ["go.sum"],
                        "commands": ["go mod download"],
                    }
                },
                "org/no-build": {
                    "checks": [{"name": "test"}],
                },
            },
            "local_repos": {"paths": [str(repo_a), str(repo_b)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        build_commands = manifest["build_commands"]
        assert len(build_commands) == 2
        repos = [m["repo"] for m in build_commands]
        assert "org/app-a" in repos
        assert "org/app-b" in repos

    def test_manifest_includes_extra_packages(self, tmp_path):
        """Manifest includes extra_packages when configured in docker_setup."""
        import json

        from egg_lib.docker import _copy_repo_watch_files

        config = {
            "repo_settings": {},
            "docker_setup": {
                "extra_packages": {
                    "apt": ["golang-go", "nodejs"],
                    "dnf": ["golang", "nodejs"],
                }
            },
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["extra_packages"] == {
            "apt": ["golang-go", "nodejs"],
            "dnf": ["golang", "nodejs"],
        }
        assert manifest["build_commands"] == []

    def test_manifest_includes_generic_packages(self, tmp_path):
        """Generic packages are appended to both apt and dnf lists in manifest."""
        import json

        from egg_lib.docker import _copy_repo_watch_files

        config = {
            "repo_settings": {},
            "docker_setup": {
                "extra_packages": {
                    "apt": ["libssl-dev"],
                    "dnf": ["openssl-devel"],
                    "packages": ["curl", "wget"],
                }
            },
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["extra_packages"]["apt"] == ["libssl-dev", "curl", "wget"]
        assert manifest["extra_packages"]["dnf"] == ["openssl-devel", "curl", "wget"]

    def test_no_manifest_when_no_build_commands_or_extra_packages(self, tmp_path):
        """No manifest.json when no repos have build_commands or extra_packages."""
        from egg_lib.docker import _copy_repo_watch_files

        config = {"repo_settings": {"org/app": {"checks": []}}}

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert not manifest_path.exists()

    def test_skips_when_no_build_commands(self, tmp_path):
        """Does nothing when no repos have build_commands."""
        from egg_lib.docker import _copy_repo_watch_files

        config = {"repo_settings": {"org/app": {"checks": []}}}

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        # repo-deps should have the empty marker
        repo_deps = build_dir / "repo-deps"
        if repo_deps.exists():
            assert (repo_deps / ".empty").exists()


class TestHashBuildCommandWatchFiles:
    """Tests for _hash_build_command_watch_files."""

    def test_hashes_watch_file_contents(self, tmp_path):
        """Watch file content is included in hash."""
        from egg_lib.docker import _hash_build_command_watch_files

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)
        (repo_dir / "requirements.txt").write_text("flask==3.0\n")

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["requirements.txt"],
                        "commands": ["pip install -r requirements.txt"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        h1 = hashlib.sha256()
        with patch("egg_lib.docker._load_repos_config", return_value=config):
            _hash_build_command_watch_files(h1)
        hash_before = h1.hexdigest()

        # Change the watch file
        (repo_dir / "requirements.txt").write_text("flask==3.1\nrequests==2.32\n")

        h2 = hashlib.sha256()
        with patch("egg_lib.docker._load_repos_config", return_value=config):
            _hash_build_command_watch_files(h2)
        hash_after = h2.hexdigest()

        assert hash_before != hash_after

    def test_noop_with_empty_config(self):
        """Does nothing with empty config."""
        from egg_lib.docker import _hash_build_command_watch_files

        h = hashlib.sha256()
        empty_digest = h.hexdigest()

        with patch("egg_lib.docker._load_repos_config", return_value={}):
            _hash_build_command_watch_files(h)

        # Hash should be unchanged
        assert h.hexdigest() == empty_digest

    def test_includes_command_changes(self, tmp_path):
        """Changing commands (not just files) changes the hash."""
        from egg_lib.docker import _hash_build_command_watch_files

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)
        (repo_dir / "requirements.txt").write_text("flask==3.0\n")

        config1 = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["requirements.txt"],
                        "commands": ["pip install -r requirements.txt"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        config2 = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["requirements.txt"],
                        "commands": ["pip install -r requirements.txt --no-deps"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        h1 = hashlib.sha256()
        with patch("egg_lib.docker._load_repos_config", return_value=config1):
            _hash_build_command_watch_files(h1)

        h2 = hashlib.sha256()
        with patch("egg_lib.docker._load_repos_config", return_value=config2):
            _hash_build_command_watch_files(h2)

        assert h1.hexdigest() != h2.hexdigest()


class TestLoadReposConfig:
    """Tests for _load_repos_config."""

    def test_returns_empty_when_file_missing(self, tmp_path):
        """Returns empty dict when repositories.yaml doesn't exist."""
        from egg_lib.docker import _load_repos_config

        with patch("egg_lib.docker.Config") as mock_config:
            mock_config.REPOS_CONFIG_FILE = tmp_path / "nonexistent.yaml"
            result = _load_repos_config()

        assert result == {}

    def test_returns_empty_on_malformed_yaml(self, tmp_path):
        """Returns empty dict when YAML is invalid."""
        from egg_lib.docker import _load_repos_config

        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(": : :\n  invalid: [unclosed")

        with patch("egg_lib.docker.Config") as mock_config:
            mock_config.REPOS_CONFIG_FILE = bad_yaml
            result = _load_repos_config()

        assert result == {}

    def test_returns_empty_on_empty_file(self, tmp_path):
        """Returns empty dict when YAML file is empty."""
        from egg_lib.docker import _load_repos_config

        empty_file = tmp_path / "empty.yaml"
        empty_file.write_text("")

        with patch("egg_lib.docker.Config") as mock_config:
            mock_config.REPOS_CONFIG_FILE = empty_file
            result = _load_repos_config()

        assert result == {}

    def test_loads_valid_config(self, tmp_path):
        """Successfully loads a valid repositories.yaml."""
        from egg_lib.docker import _load_repos_config

        config_file = tmp_path / "repos.yaml"
        config_file.write_text(
            "repo_settings:\n  org/app:\n    build_commands:\n      commands:\n        - make\n"
        )

        with patch("egg_lib.docker.Config") as mock_config:
            mock_config.REPOS_CONFIG_FILE = config_file
            result = _load_repos_config()

        assert "repo_settings" in result
        assert "org/app" in result["repo_settings"]


class TestGetLocalRepoPathEdgeCases:
    """Edge case tests for _get_local_repo_path."""

    def test_non_dict_local_repos_returns_none(self):
        """Non-dict local_repos value returns None."""
        from egg_lib.docker import _get_local_repo_path

        config = {"local_repos": "not-a-dict"}
        result = _get_local_repo_path(config, "org/app")
        assert result is None

    def test_non_list_paths_returns_none(self):
        """Non-list paths value returns None."""
        from egg_lib.docker import _get_local_repo_path

        config = {"local_repos": {"paths": "not-a-list"}}
        result = _get_local_repo_path(config, "org/app")
        assert result is None

    def test_case_insensitive_matching(self, tmp_path):
        """Path matching is case-insensitive."""
        from egg_lib.docker import _get_local_repo_path

        repo_dir = tmp_path / "MyOrg" / "MyApp"
        repo_dir.mkdir(parents=True)

        config = {"local_repos": {"paths": [str(repo_dir)]}}
        result = _get_local_repo_path(config, "myorg/myapp")
        assert result == repo_dir

    def test_skips_nonexistent_paths(self, tmp_path):
        """Non-existent paths in the list are skipped."""
        from egg_lib.docker import _get_local_repo_path

        real_dir = tmp_path / "org" / "app"
        real_dir.mkdir(parents=True)

        config = {
            "local_repos": {
                "paths": [
                    str(tmp_path / "nonexistent" / "org" / "app"),
                    str(real_dir),
                ]
            }
        }

        result = _get_local_repo_path(config, "org/app")
        assert result == real_dir

    def test_single_component_repo_name(self, tmp_path):
        """Repo name without owner (no slash) is handled."""
        from egg_lib.docker import _get_local_repo_path

        repo_dir = tmp_path / "myapp"
        repo_dir.mkdir()

        config = {"local_repos": {"paths": [str(repo_dir)]}}
        # Single-component name: no fallback matching (len(repo_parts) == 1)
        result = _get_local_repo_path(config, "myapp")
        assert result == repo_dir


class TestCopyRepoWatchFilesEdgeCases:
    """Edge case tests for _copy_repo_watch_files."""

    def test_nested_watch_files_preserve_structure(self, tmp_path):
        """Watch files in subdirectories preserve their directory structure."""
        from egg_lib.docker import _copy_repo_watch_files

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)
        nested_dir = repo_dir / "config"
        nested_dir.mkdir()
        (nested_dir / "settings.json").write_text('{"key": "value"}')

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["config/settings.json"],
                        "commands": ["make deps"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        dest = build_dir / "repo-deps" / "org--app" / "config" / "settings.json"
        assert dest.exists()
        assert dest.read_text() == '{"key": "value"}'

    def test_cleans_up_stale_repo_deps(self, tmp_path):
        """Old repo-deps directory is cleaned before copying new files."""
        from egg_lib.docker import _copy_repo_watch_files

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        # Create stale repo-deps
        stale_dir = build_dir / "repo-deps" / "old--repo"
        stale_dir.mkdir(parents=True)
        (stale_dir / "stale.txt").write_text("old")

        config = {"repo_settings": {"org/app": {"checks": []}}}

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        # Stale directory should be removed
        assert not stale_dir.exists()

    def test_writes_manifest_when_no_watch_files_copied(self, tmp_path):
        """Writes manifest.json even when no watch files are copyable (local path not found)."""
        import json

        from egg_lib.docker import _copy_repo_watch_files

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        # Config with commands but no local repo path match
        config = {
            "repo_settings": {
                "org/unknown-repo": {
                    "build_commands": {
                        "watch_files": ["req.txt"],
                        "commands": ["pip install -r req.txt"],
                    }
                }
            },
            "local_repos": {"paths": []},
        }

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        # Manifest should still be written so build commands execute during Docker build
        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        build_commands = manifest["build_commands"]
        assert len(build_commands) == 1
        assert build_commands[0]["repo"] == "org/unknown-repo"
        assert build_commands[0]["commands"] == ["pip install -r req.txt"]
        # .empty should NOT exist since manifest was written
        assert not (build_dir / "repo-deps" / ".empty").exists()

    def test_multiple_repos_copy_separately(self, tmp_path):
        """Watch files from multiple repos are copied to separate directories."""
        from egg_lib.docker import _copy_repo_watch_files

        repo_a = tmp_path / "org" / "app-a"
        repo_a.mkdir(parents=True)
        (repo_a / "package.json").write_text('{"name": "a"}')

        repo_b = tmp_path / "org" / "app-b"
        repo_b.mkdir(parents=True)
        (repo_b / "requirements.txt").write_text("flask\n")

        config = {
            "repo_settings": {
                "org/app-a": {
                    "build_commands": {
                        "watch_files": ["package.json"],
                        "commands": ["npm ci"],
                    }
                },
                "org/app-b": {
                    "build_commands": {
                        "watch_files": ["requirements.txt"],
                        "commands": ["pip install -r requirements.txt"],
                    }
                },
            },
            "local_repos": {"paths": [str(repo_a), str(repo_b)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        assert (build_dir / "repo-deps" / "org--app-a" / "package.json").exists()
        assert (build_dir / "repo-deps" / "org--app-b" / "requirements.txt").exists()

    def test_missing_watch_file_is_skipped(self, tmp_path):
        """Watch file that doesn't exist in local repo is skipped."""
        import json

        from egg_lib.docker import _copy_repo_watch_files

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)
        # Don't create the watch file

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["nonexistent.json"],
                        "commands": ["npm ci"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        # Dest directory should exist but be empty (no files copied)
        dest_dir = build_dir / "repo-deps" / "org--app"
        assert dest_dir.exists()
        # Manifest should still be written so build commands execute
        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        build_commands = manifest["build_commands"]
        assert len(build_commands) == 1
        assert build_commands[0]["commands"] == ["npm ci"]


class TestHashBuildCommandWatchFilesEdgeCases:
    """Edge case tests for _hash_build_command_watch_files."""

    def test_missing_watch_file_is_skipped(self, tmp_path):
        """Watch file that doesn't exist is simply skipped in hashing."""
        from egg_lib.docker import _hash_build_command_watch_files

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)
        # Don't create the watch file

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["nonexistent.txt"],
                        "commands": ["pip install"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        h = hashlib.sha256()
        with patch("egg_lib.docker._load_repos_config", return_value=config):
            _hash_build_command_watch_files(h)

        # Should still update the hash (repo name + commands), just skip the file
        empty_h = hashlib.sha256()
        assert h.hexdigest() != empty_h.hexdigest()

    def test_repos_without_commands_are_skipped(self, tmp_path):
        """Repos with empty commands don't affect the hash."""
        from egg_lib.docker import _hash_build_command_watch_files

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)
        (repo_dir / "req.txt").write_text("flask\n")

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["req.txt"],
                        "commands": [],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        h = hashlib.sha256()
        empty_digest = h.hexdigest()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            _hash_build_command_watch_files(h)

        # Hash should be unchanged since commands is empty
        assert h.hexdigest() == empty_digest

    def test_deterministic_multi_repo_ordering(self, tmp_path):
        """Multiple repos are hashed in sorted order for determinism."""
        from egg_lib.docker import _hash_build_command_watch_files

        repo_a = tmp_path / "aaa" / "first"
        repo_a.mkdir(parents=True)
        (repo_a / "req.txt").write_text("flask\n")

        repo_b = tmp_path / "zzz" / "second"
        repo_b.mkdir(parents=True)
        (repo_b / "pkg.json").write_text("{}\n")

        config = {
            "repo_settings": {
                "zzz/second": {
                    "build_commands": {
                        "watch_files": ["pkg.json"],
                        "commands": ["npm ci"],
                    }
                },
                "aaa/first": {
                    "build_commands": {
                        "watch_files": ["req.txt"],
                        "commands": ["pip install"],
                    }
                },
            },
            "local_repos": {"paths": [str(repo_a), str(repo_b)]},
        }

        # Hash twice — should be identical (deterministic)
        h1 = hashlib.sha256()
        with patch("egg_lib.docker._load_repos_config", return_value=config):
            _hash_build_command_watch_files(h1)

        h2 = hashlib.sha256()
        with patch("egg_lib.docker._load_repos_config", return_value=config):
            _hash_build_command_watch_files(h2)

        assert h1.hexdigest() == h2.hexdigest()

    def test_missing_local_repo_path_is_skipped(self, tmp_path):
        """Repo without a matching local path is skipped."""
        from egg_lib.docker import _hash_build_command_watch_files

        config = {
            "repo_settings": {
                "org/no-local-path": {
                    "build_commands": {
                        "watch_files": ["req.txt"],
                        "commands": ["pip install"],
                    }
                }
            },
            "local_repos": {"paths": []},
        }

        h = hashlib.sha256()
        empty_digest = h.hexdigest()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            _hash_build_command_watch_files(h)

        # Hash should be unchanged - repo was skipped entirely
        assert h.hexdigest() == empty_digest

    def test_non_dict_settings_are_skipped(self):
        """Non-dict individual settings are silently skipped."""
        from egg_lib.docker import _hash_build_command_watch_files

        config = {
            "repo_settings": {
                "org/broken": "not-a-dict",
            }
        }

        h = hashlib.sha256()
        empty_digest = h.hexdigest()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            _hash_build_command_watch_files(h)

        assert h.hexdigest() == empty_digest


class TestWatchFilePathTraversal:
    """Tests for path traversal validation in watch file handling."""

    def test_copy_rejects_path_traversal(self, tmp_path):
        """Watch files with .. components that escape the repo are rejected."""
        from egg_lib.docker import _copy_repo_watch_files

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)
        (repo_dir / "legit.txt").write_text("ok")

        # Create a file outside the repo that a traversal would reach
        (tmp_path / "secret.txt").write_text("sensitive")

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["../../secret.txt"],
                        "commands": ["make deps"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        # The traversal file should NOT be copied
        repo_deps = build_dir / "repo-deps"
        assert repo_deps.exists(), "repo-deps directory should always be created"
        # Should only have the .empty marker, not the secret file
        all_files = list(repo_deps.rglob("*"))
        file_names = [f.name for f in all_files if f.is_file()]
        assert "secret.txt" not in file_names

    def test_hash_skips_path_traversal(self, tmp_path):
        """Hash function skips watch files with path traversal."""
        from egg_lib.docker import _hash_build_command_watch_files

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)

        # Create a file outside the repo
        (tmp_path / "outside.txt").write_text("outside content")

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["../../outside.txt"],
                        "commands": ["pip install"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        # Hash with traversal path — should only include repo/commands, not file content
        h_traversal = hashlib.sha256()
        with patch("egg_lib.docker._load_repos_config", return_value=config):
            _hash_build_command_watch_files(h_traversal)

        # Hash with a legit path that doesn't exist — should produce same result
        # (both skip the file content, only include repo name + commands)
        config_legit = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["nonexistent.txt"],
                        "commands": ["pip install"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }
        h_legit = hashlib.sha256()
        with patch("egg_lib.docker._load_repos_config", return_value=config_legit):
            _hash_build_command_watch_files(h_legit)

        assert h_traversal.hexdigest() == h_legit.hexdigest()

    def test_copy_rejects_symlink_escaping_repo(self, tmp_path):
        """Symlinks pointing outside the repo boundary are rejected."""
        from egg_lib.docker import _copy_repo_watch_files

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)

        # Create a file outside the repo
        outside_file = tmp_path / "outside-secret.txt"
        outside_file.write_text("sensitive data")

        # Create a symlink inside the repo pointing outside
        symlink = repo_dir / "sneaky-link.txt"
        symlink.symlink_to(outside_file)

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["sneaky-link.txt"],
                        "commands": ["make deps"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            with patch("egg_lib.docker.Config") as mock_config:
                mock_config.CONFIG_DIR = build_dir
                _copy_repo_watch_files(quiet=True)

        # The symlink target should NOT be copied
        repo_deps = build_dir / "repo-deps"
        assert repo_deps.exists(), "repo-deps directory should always be created"
        all_files = list(repo_deps.rglob("*"))
        file_names = [f.name for f in all_files if f.is_file()]
        assert "sneaky-link.txt" not in file_names
