"""Tests for sandbox/egg_lib/gateway.py - Gateway sidecar management."""

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

# Add shared module to path for imports
shared_path = Path(__file__).parent.parent.parent / "shared"
sys.path.insert(0, str(shared_path))
sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_config import TEST_GATEWAY_PORT, TEST_GATEWAY_PROXY_PORT

from egg_lib.gateway import (
    _get_user_git_config,
    _hash_directory,
    _hash_file,
    _load_secrets,
    _parse_git_mounts,
    compute_gateway_build_hash,
    create_session,
    create_worktrees,
    delete_session,
    delete_session_by_container,
    delete_worktrees,
    gateway_image_exists,
    get_launcher_secret,
    get_repo_visibilities,
    is_gateway_running,
    launcher_api_call,
    should_rebuild_gateway,
)


def _mock_context(**overrides):
    """Create a mock context with sensible defaults.

    Uses TEST_GATEWAY_PORT (1234) to make it obvious when tests
    accidentally connect to real services.
    """
    ctx = MagicMock()
    ctx.config_dir = Path("/tmp/test-config")
    ctx.launcher_secret = None
    ctx.publish_ports = True
    ctx.gateway_port = TEST_GATEWAY_PORT
    ctx.gateway_isolated_ip = "172.32.0.2"
    ctx.gateway_container_name = "egg-gateway"
    ctx.gateway_image = "egg-gateway:latest"
    ctx.gateway_proxy_port = TEST_GATEWAY_PROXY_PORT
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


class TestHashFile:
    """Tests for _hash_file."""

    def test_hashes_file_content(self, tmp_path):
        """Adds file content to hasher."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")
        hasher = hashlib.sha256()
        _hash_file(test_file, hasher)
        assert hasher.hexdigest() != hashlib.sha256().hexdigest()

    def test_same_content_same_hash(self, tmp_path):
        """Same content produces same hash regardless of filename."""
        f1 = tmp_path / "file1.txt"
        f2 = tmp_path / "file2.txt"
        f1.write_text("same content")
        f2.write_text("same content")
        h1 = hashlib.sha256()
        h2 = hashlib.sha256()
        _hash_file(f1, h1)
        _hash_file(f2, h2)
        # _hash_file only hashes content, not filename
        assert h1.hexdigest() == h2.hexdigest()

    def test_handles_missing_file(self, tmp_path):
        """Handles missing file gracefully (OSError caught)."""
        hasher = hashlib.sha256()
        _hash_file(tmp_path / "nonexistent.txt", hasher)
        # Hash should remain unchanged (no content added)
        assert hasher.hexdigest() == hashlib.sha256().hexdigest()


class TestHashDirectory:
    """Tests for _hash_directory."""

    def test_hashes_directory(self, tmp_path):
        """Hashes all files in directory."""
        (tmp_path / "a.py").write_text("code")
        (tmp_path / "b.py").write_text("more code")
        hasher = hashlib.sha256()
        _hash_directory(tmp_path, hasher)
        assert hasher.hexdigest() != hashlib.sha256().hexdigest()

    def test_excludes_tests(self, tmp_path):
        """Excludes test files when exclude_tests is True."""
        (tmp_path / "main.py").write_text("code")
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        (test_dir / "test_main.py").write_text("test code")

        h1 = hashlib.sha256()
        _hash_directory(tmp_path, h1, exclude_tests=False)

        h2 = hashlib.sha256()
        _hash_directory(tmp_path, h2, exclude_tests=True)

        assert h1.hexdigest() != h2.hexdigest()

    def test_empty_directory(self, tmp_path):
        """Empty directory produces clean hash."""
        hasher = hashlib.sha256()
        _hash_directory(tmp_path, hasher)
        # Should still have a valid hash (just no file content added)
        assert len(hasher.hexdigest()) == 64


class TestComputeGatewayBuildHash:
    """Tests for compute_gateway_build_hash."""

    def test_returns_hex_string(self):
        """Returns a valid hex hash string."""
        result = compute_gateway_build_hash()
        assert isinstance(result, str)
        assert len(result) == 64  # SHA256 hex
        int(result, 16)  # Should be valid hex


class TestShouldRebuildGateway:
    """Tests for should_rebuild_gateway."""

    def test_rebuild_when_no_image(self):
        """Should rebuild when no image exists."""
        with patch("egg_lib.gateway.gateway_image_exists", return_value=False):
            rebuild, reason = should_rebuild_gateway()
            assert rebuild is True
            assert "not found" in reason.lower() or "no" in reason.lower()

    def test_rebuild_when_hash_changed(self):
        """Should rebuild when hash doesn't match."""
        with patch("egg_lib.gateway.gateway_image_exists", return_value=True):
            with patch("egg_lib.gateway.get_gateway_image_hash", return_value="old-hash"):
                with patch("egg_lib.gateway.compute_gateway_build_hash", return_value="new-hash"):
                    rebuild, reason = should_rebuild_gateway()
                    assert rebuild is True

    def test_no_rebuild_when_hash_matches(self):
        """Should not rebuild when hash matches."""
        hash_val = "abcd1234" * 8
        with patch("egg_lib.gateway.gateway_image_exists", return_value=True):
            with patch("egg_lib.gateway.get_gateway_image_hash", return_value=hash_val):
                with patch("egg_lib.gateway.compute_gateway_build_hash", return_value=hash_val):
                    rebuild, reason = should_rebuild_gateway()
                    assert rebuild is False


class TestLoadSecrets:
    """Tests for _load_secrets."""

    def test_loads_from_file(self, tmp_path):
        """Loads secrets from secrets.env file."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "secrets.env").write_text("KEY=value\nSECRET=hidden\n")
        ctx = _mock_context(config_dir=config_dir)
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            result = _load_secrets()
            assert result.get("KEY") == "value"
            assert result.get("SECRET") == "hidden"

    def test_empty_when_no_file(self, tmp_path):
        """Returns empty dict when file doesn't exist."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        ctx = _mock_context(config_dir=config_dir)
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            result = _load_secrets()
            assert result == {}

    def test_strips_quotes(self, tmp_path):
        """Strips surrounding quotes from values."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "secrets.env").write_text("QUOTED=\"quoted_val\"\nSINGLE='single_val'\n")
        ctx = _mock_context(config_dir=config_dir)
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            result = _load_secrets()
            assert result.get("QUOTED") == "quoted_val"
            assert result.get("SINGLE") == "single_val"

    def test_skips_comments_and_empty(self, tmp_path):
        """Skips comment lines and empty lines."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "secrets.env").write_text("# comment\n\nKEY=value\n")
        ctx = _mock_context(config_dir=config_dir)
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            result = _load_secrets()
            assert result == {"KEY": "value"}


class TestParseGitMounts:
    """Tests for _parse_git_mounts."""

    def test_parses_local_repos(self, tmp_path):
        """Parses local_repos paths from config."""
        config_file = tmp_path / "repositories.yaml"
        config_file.write_text(
            "local_repos:\n  paths:\n    - /path/to/repo1\n    - /path/to/repo2\n"
        )
        result = _parse_git_mounts(config_file, "/home/egg")
        assert isinstance(result, list)

    def test_handles_missing_config(self, tmp_path):
        """Handles missing config file gracefully."""
        result = _parse_git_mounts(tmp_path / "nonexistent.yaml", "/home/egg")
        assert result == []


class TestGetGatewayImageHash:
    """Tests for get_gateway_image_hash."""

    def test_returns_hash(self):
        """Returns hash from image label."""
        from egg_lib.gateway import get_gateway_image_hash

        ctx = _mock_context()
        with patch("egg_lib.gateway.gateway_image_exists", return_value=True):
            with patch("egg_lib.gateway.get_context", return_value=ctx):
                with patch("egg_lib.gateway.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout="abc123\n")
                    result = get_gateway_image_hash()
                    assert result == "abc123"

    def test_returns_none_no_image(self):
        """Returns None when image doesn't exist."""
        from egg_lib.gateway import get_gateway_image_hash

        with patch("egg_lib.gateway.gateway_image_exists", return_value=False):
            assert get_gateway_image_hash() is None

    def test_returns_none_no_label(self):
        """Returns None when label is <no value>."""
        from egg_lib.gateway import get_gateway_image_hash

        ctx = _mock_context()
        with patch("egg_lib.gateway.gateway_image_exists", return_value=True):
            with patch("egg_lib.gateway.get_context", return_value=ctx):
                with patch("egg_lib.gateway.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout="<no value>\n")
                    assert get_gateway_image_hash() is None

    def test_returns_none_on_exception(self):
        """Returns None on exception."""
        from egg_lib.gateway import get_gateway_image_hash

        with patch("egg_lib.gateway.gateway_image_exists", return_value=True):
            with patch("egg_lib.gateway.subprocess.run", side_effect=Exception("error")):
                assert get_gateway_image_hash() is None

    def test_returns_none_on_failure(self):
        """Returns None on subprocess failure."""
        from egg_lib.gateway import get_gateway_image_hash

        ctx = _mock_context()
        with patch("egg_lib.gateway.gateway_image_exists", return_value=True):
            with patch("egg_lib.gateway.get_context", return_value=ctx):
                with patch("egg_lib.gateway.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1, stdout="")
                    assert get_gateway_image_hash() is None


class TestShouldRebuildGatewayExtended:
    """Extended tests for should_rebuild_gateway."""

    def test_no_stored_hash(self):
        """Rebuilds when no stored hash (legacy image)."""
        with patch("egg_lib.gateway.gateway_image_exists", return_value=True):
            with patch("egg_lib.gateway.get_gateway_image_hash", return_value=None):
                with patch("egg_lib.gateway.compute_gateway_build_hash", return_value="abc"):
                    rebuild, reason = should_rebuild_gateway()
                    assert rebuild is True
                    assert "legacy" in reason.lower() or "no" in reason.lower()


class TestGetUserGitConfig:
    """Tests for _get_user_git_config."""

    def test_reads_user_mode_config(self, tmp_path):
        """Reads git user name and email from config."""
        config_file = tmp_path / "repositories.yaml"
        # Actual implementation uses git_name and git_email keys
        config_file.write_text("user_mode:\n  git_name: Test User\n  git_email: test@example.com\n")
        name, email = _get_user_git_config(config_file)
        assert name == "Test User"
        assert email == "test@example.com"

    def test_returns_none_when_missing(self, tmp_path):
        """Returns None when user_mode not configured."""
        config_file = tmp_path / "repositories.yaml"
        config_file.write_text("github_username: test\n")
        name, email = _get_user_git_config(config_file)
        assert name is None
        assert email is None

    def test_handles_missing_file(self, tmp_path):
        """Returns None when file doesn't exist."""
        name, email = _get_user_git_config(tmp_path / "nonexistent.yaml")
        assert name is None
        assert email is None


class TestGetLauncherSecret:
    """Tests for get_launcher_secret."""

    def test_from_context(self):
        """Returns secret from context.launcher_secret."""
        ctx = _mock_context(launcher_secret="ctx-secret")
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            assert get_launcher_secret() == "ctx-secret"

    def test_from_file(self, tmp_path):
        """Returns secret from file when context has no secret."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "launcher-secret").write_text("file-secret")
        ctx = _mock_context(config_dir=config_dir, launcher_secret=None)
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            result = get_launcher_secret()
            assert result == "file-secret"

    def test_generates_new_secret(self, tmp_path):
        """Generates new secret when none exists."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        ctx = _mock_context(config_dir=config_dir, launcher_secret=None)
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            result = get_launcher_secret()
            assert result is not None
            assert len(result) > 0
            # Verify it was written to file
            assert (config_dir / "launcher-secret").exists()


class TestLauncherApiCall:
    """Tests for launcher_api_call."""

    def _make_mock_response(self, data):
        """Create a mock urlopen response."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(data).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        return mock_response

    def test_get_request(self):
        """Makes GET request with auth header."""
        ctx = _mock_context(launcher_secret="test-secret", publish_ports=True, gateway_port=TEST_GATEWAY_PORT)
        response_data = {"success": True, "status": "ok"}
        mock_response = self._make_mock_response(response_data)

        with patch("egg_lib.gateway.get_context", return_value=ctx):
            with patch("egg_lib.gateway.urlopen", return_value=mock_response):
                success, data = launcher_api_call("/api/v1/health")
                assert success is True
                assert data.get("status") == "ok"

    def test_post_request(self):
        """Makes POST request with JSON body."""
        ctx = _mock_context(launcher_secret="test-secret", publish_ports=True, gateway_port=TEST_GATEWAY_PORT)
        response_data = {"success": True, "created": True}
        mock_response = self._make_mock_response(response_data)

        with patch("egg_lib.gateway.get_context", return_value=ctx):
            with patch("egg_lib.gateway.urlopen", return_value=mock_response):
                success, data = launcher_api_call(
                    "/api/v1/sessions", method="POST", data={"key": "val"}
                )
                assert success is True

    def test_returns_false_without_success_key(self):
        """Returns False when response lacks success: true."""
        ctx = _mock_context(launcher_secret="test-secret", publish_ports=True, gateway_port=TEST_GATEWAY_PORT)
        response_data = {"status": "ok"}  # No "success" key
        mock_response = self._make_mock_response(response_data)

        with patch("egg_lib.gateway.get_context", return_value=ctx):
            with patch("egg_lib.gateway.urlopen", return_value=mock_response):
                success, data = launcher_api_call("/api/v1/health")
                assert success is False

    def test_handles_url_error(self):
        """Returns failure on URL error."""
        ctx = _mock_context(launcher_secret="test-secret", publish_ports=True, gateway_port=TEST_GATEWAY_PORT)
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            with patch("egg_lib.gateway.urlopen", side_effect=URLError("connection refused")):
                success, data = launcher_api_call("/api/v1/health")
                assert success is False

    def test_handles_timeout(self):
        """Returns failure on timeout."""
        ctx = _mock_context(launcher_secret="test-secret", publish_ports=True, gateway_port=TEST_GATEWAY_PORT)
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            with patch("egg_lib.gateway.urlopen", side_effect=TimeoutError("timed out")):
                success, data = launcher_api_call("/api/v1/health")
                assert success is False

    def test_uses_container_ip_when_no_publish_ports(self):
        """Uses gateway_isolated_ip when publish_ports is False."""
        ctx = _mock_context(
            launcher_secret="test-secret",
            publish_ports=False,
            gateway_port=TEST_GATEWAY_PORT,
            gateway_isolated_ip="172.32.0.2",
        )
        response_data = {"success": True}
        mock_response = self._make_mock_response(response_data)

        with patch("egg_lib.gateway.get_context", return_value=ctx):
            with patch("egg_lib.gateway.urlopen", return_value=mock_response) as mock_urlopen:
                success, data = launcher_api_call("/api/v1/health")
                assert success is True
                # Verify URL uses container IP
                call_args = mock_urlopen.call_args
                req = call_args[0][0]
                assert "172.32.0.2" in req.full_url


class TestIsGatewayRunning:
    """Tests for is_gateway_running."""

    def test_running(self):
        """Returns True when container is running."""
        ctx = _mock_context()
        mock_result = MagicMock(returncode=0, stdout="true\n")
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            with patch("egg_lib.gateway.subprocess.run", return_value=mock_result):
                assert is_gateway_running() is True

    def test_not_running(self):
        """Returns False when container is not running."""
        ctx = _mock_context()
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            with patch("egg_lib.gateway.subprocess.run", return_value=mock_result):
                assert is_gateway_running() is False

    def test_stdout_false(self):
        """Returns False when stdout is 'false'."""
        ctx = _mock_context()
        mock_result = MagicMock(returncode=0, stdout="false\n")
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            with patch("egg_lib.gateway.subprocess.run", return_value=mock_result):
                assert is_gateway_running() is False


class TestGatewayImageExists:
    """Tests for gateway_image_exists."""

    def test_exists(self):
        """Returns True when image exists."""
        ctx = _mock_context()
        mock_result = MagicMock(returncode=0)
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            with patch("egg_lib.gateway.subprocess.run", return_value=mock_result):
                assert gateway_image_exists() is True

    def test_not_exists(self):
        """Returns False when image doesn't exist."""
        ctx = _mock_context()
        mock_result = MagicMock(returncode=1)
        with patch("egg_lib.gateway.get_context", return_value=ctx):
            with patch("egg_lib.gateway.subprocess.run", return_value=mock_result):
                assert gateway_image_exists() is False


class TestCreateWorktrees:
    """Tests for create_worktrees."""

    def test_success(self):
        """Creates worktrees via API."""
        response = {
            "success": True,
            "data": {
                "worktrees": {"repo": "/path/to/worktree"},
                "errors": [],
            },
        }
        with patch("egg_lib.gateway.launcher_api_call", return_value=(True, response)):
            success, worktrees, errors = create_worktrees("container-1", ["repo"])
            assert success is True
            assert worktrees == {"repo": "/path/to/worktree"}
            assert errors == []

    def test_failure(self):
        """Returns failure on API error."""
        with patch("egg_lib.gateway.launcher_api_call", return_value=(False, {"error": "fail"})):
            success, worktrees, errors = create_worktrees("container-1", ["repo"])
            assert success is False
            assert worktrees == {}
            assert "fail" in errors[0]


class TestDeleteWorktrees:
    """Tests for delete_worktrees."""

    def test_success(self):
        """Deletes worktrees via API."""
        response = {
            "success": True,
            "data": {"deleted": ["repo"], "errors": []},
        }
        with patch("egg_lib.gateway.launcher_api_call", return_value=(True, response)):
            success, deleted, errors = delete_worktrees("container-1")
            assert success is True
            assert deleted == ["repo"]

    def test_failure(self):
        """Returns failure on API error."""
        with patch("egg_lib.gateway.launcher_api_call", return_value=(False, {"error": "fail"})):
            success, deleted, errors = delete_worktrees("container-1")
            assert success is False
            assert deleted == []


class TestCreateSession:
    """Tests for create_session."""

    def test_success(self):
        """Creates session via API."""
        response = {
            "success": True,
            "data": {
                "session_token": "tok-123",
                "worktrees": {"repo": "/path"},
                "filtered_repos": ["repo"],
                "errors": [],
            },
        }
        with patch("egg_lib.gateway.launcher_api_call", return_value=(True, response)):
            success, token, worktrees, repos, errors = create_session(
                "container-1", "10.0.0.1", "public", ["repo"]
            )
            assert success is True
            assert token == "tok-123"
            assert worktrees == {"repo": "/path"}
            assert repos == ["repo"]

    def test_failure(self):
        """Returns failure on API error."""
        with patch("egg_lib.gateway.launcher_api_call", return_value=(False, {"error": "fail"})):
            success, token, worktrees, repos, errors = create_session(
                "container-1", "10.0.0.1", "public", ["repo"]
            )
            assert success is False
            assert token is None


class TestDeleteSession:
    """Tests for delete_session."""

    def test_success(self):
        """Deletes session via API."""
        with patch("egg_lib.gateway.launcher_api_call", return_value=(True, {"success": True})):
            success, error = delete_session("tok-123")
            assert success is True
            assert error is None

    def test_failure(self):
        """Returns failure on API error."""
        with patch("egg_lib.gateway.launcher_api_call", return_value=(False, {"error": "fail"})):
            success, error = delete_session("tok-123")
            assert success is False
            assert error == "fail"


class TestDeleteSessionByContainer:
    """Tests for delete_session_by_container."""

    def test_success(self):
        """Deletes session by container ID."""
        list_response = {
            "success": True,
            "data": {"sessions": []},
        }
        wt_response = (True, ["repo"], [])
        with patch("egg_lib.gateway.launcher_api_call", return_value=(True, list_response)):
            with patch("egg_lib.gateway.delete_worktrees", return_value=wt_response):
                success, error = delete_session_by_container("container-1")
                assert success is True

    def test_failure_on_list(self):
        """Returns failure when listing sessions fails."""
        with patch("egg_lib.gateway.launcher_api_call", return_value=(False, {"error": "fail"})):
            success, error = delete_session_by_container("container-1")
            assert success is False


class TestGetRepoVisibilities:
    """Tests for get_repo_visibilities."""

    def test_success(self):
        """Gets repo visibilities via API."""
        response = {
            "success": True,
            "data": {"visibilities": {"owner/repo": "public"}},
        }
        with patch("egg_lib.gateway.launcher_api_call", return_value=(True, response)):
            success, visibilities, error = get_repo_visibilities(["owner/repo"])
            assert success is True
            assert visibilities == {"owner/repo": "public"}

    def test_failure(self):
        """Returns failure on API error."""
        with patch("egg_lib.gateway.launcher_api_call", return_value=(False, {"error": "fail"})):
            success, visibilities, error = get_repo_visibilities(["owner/repo"])
            assert success is False
