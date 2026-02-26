"""Tests for sandbox/egg_lib/runtime.py - Container execution helpers."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add shared module to path for imports
shared_path = Path(__file__).parent.parent.parent / "shared"
sys.path.insert(0, str(shared_path))
sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_config import TEST_GATEWAY_PORT, TEST_GATEWAY_PROXY_PORT
from egg_lib.runtime import (
    VALID_REPO_MODES,
    _allocate_container_ip,
    _cleanup_session,
    _cleanup_worktrees,
    _get_container_network_config,
    _get_repo_owner_name,
    _get_reserved_ips,
    _validate_repo_mode,
)


def _mock_context(**overrides):
    """Create a mock context with sensible defaults.

    Uses TEST_GATEWAY_PORT (1234) to make it obvious when tests
    accidentally connect to real services.
    """
    ctx = MagicMock()
    ctx.isolated_network = "egg-isolated"
    ctx.external_network = "egg-external"
    ctx.isolated_subnet = "172.32.0.0/24"
    ctx.external_subnet = "172.33.0.0/24"
    ctx.gateway_isolated_ip = "172.32.0.2"
    ctx.gateway_external_ip = "172.33.0.2"
    ctx.gateway_container_name = "egg-gateway"
    ctx.gateway_port = TEST_GATEWAY_PORT
    ctx.gateway_proxy_port = TEST_GATEWAY_PROXY_PORT
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


class TestValidRepoModes:
    """Tests for VALID_REPO_MODES constant."""

    def test_contains_expected_modes(self):
        """VALID_REPO_MODES contains private and public."""
        assert "private" in VALID_REPO_MODES
        assert "public" in VALID_REPO_MODES


class TestGetReservedIps:
    """Tests for _get_reserved_ips."""

    def test_basic_subnet(self):
        """Returns reserved IPs for a subnet (base.1 and gateway)."""
        reserved = _get_reserved_ips("172.32.0.0/24", "172.32.0.1")
        assert isinstance(reserved, set)
        assert "172.32.0.1" in reserved  # both docker gateway (.1) and the provided gateway_ip

    def test_includes_gateway(self):
        """Gateway IP is included in reserved set."""
        reserved = _get_reserved_ips("172.32.0.0/24", "172.32.0.2")
        assert "172.32.0.2" in reserved
        assert "172.32.0.1" in reserved  # Docker default gateway

    def test_only_two_reserved(self):
        """Only reserves base.1 and the provided gateway IP."""
        reserved = _get_reserved_ips("172.32.0.0/24", "172.32.0.5")
        assert reserved == {"172.32.0.1", "172.32.0.5"}


class TestValidateRepoMode:
    """Tests for _validate_repo_mode."""

    def test_valid_modes(self):
        """Valid modes do not raise."""
        _validate_repo_mode("private")
        _validate_repo_mode("public")
        _validate_repo_mode(None)  # None is valid (auto-detect)

    def test_invalid_mode(self):
        """Invalid mode raises ValueError."""
        with pytest.raises(ValueError, match="Invalid repo_mode"):
            _validate_repo_mode("invalid")


class TestGetContainerNetworkConfig:
    """Tests for _get_container_network_config."""

    def test_returns_config_for_public(self):
        """Returns external network config for public mode."""
        ctx = _mock_context()
        with patch("egg_lib.runtime.get_context", return_value=ctx):
            config = _get_container_network_config("public")
            assert config is not None
            assert config.network_name == "egg-external"
            assert config.repo_mode == "public"

    def test_returns_config_for_private(self):
        """Returns isolated network config for private mode."""
        ctx = _mock_context()
        with patch("egg_lib.runtime.get_context", return_value=ctx):
            config = _get_container_network_config("private")
            assert config is not None
            assert config.network_name == "egg-isolated"
            assert config.repo_mode == "private"

    def test_returns_config_for_none(self):
        """Returns external (public) config when mode is None."""
        ctx = _mock_context()
        with patch("egg_lib.runtime.get_context", return_value=ctx):
            config = _get_container_network_config(None)
            assert config is not None
            assert config.repo_mode == "public"


class TestGetRepoOwnerName:
    """Tests for _get_repo_owner_name."""

    def test_extracts_from_https(self, tmp_path):
        """Extracts owner/repo from HTTPS remote."""
        # _get_repo_owner_name uses 'git remote get-url origin' with check=True
        mock_result = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo.git\n",
        )
        with patch("egg_lib.runtime.subprocess.run", return_value=mock_result):
            result = _get_repo_owner_name(tmp_path)
            assert result == "owner/repo"

    def test_extracts_from_ssh(self, tmp_path):
        """Extracts owner/repo from SSH remote."""
        mock_result = MagicMock(
            returncode=0,
            stdout="git@github.com:owner/repo.git\n",
        )
        with patch("egg_lib.runtime.subprocess.run", return_value=mock_result):
            result = _get_repo_owner_name(tmp_path)
            assert result == "owner/repo"

    def test_returns_none_on_failure(self, tmp_path):
        """Returns None when git remote fails (CalledProcessError)."""
        with patch(
            "egg_lib.runtime.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "git"),
        ):
            result = _get_repo_owner_name(tmp_path)
            assert result is None

    def test_returns_none_on_exception(self, tmp_path):
        """Returns None on IndexError."""
        with patch(
            "egg_lib.runtime.subprocess.run",
            side_effect=IndexError("boom"),
        ):
            result = _get_repo_owner_name(tmp_path)
            assert result is None


class TestAllocateContainerIp:
    """Tests for _allocate_container_ip."""

    def test_defaults_to_isolated_network(self):
        """Defaults to ctx.isolated_network when network is None."""
        ctx = _mock_context()
        with patch("egg_lib.runtime.get_context", return_value=ctx):
            with patch("egg_lib.runtime.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="{}")
                result = _allocate_container_ip(None)
                # Should allocate an IP from isolated subnet (not return None)
                if result:
                    assert result.startswith("172.32.0.")

    def test_allocates_ip(self):
        """Allocates an IP from the subnet."""
        ctx = _mock_context()
        with patch("egg_lib.runtime.get_context", return_value=ctx):
            with patch("egg_lib.runtime.subprocess.run") as mock_run:
                # No existing containers
                mock_run.return_value = MagicMock(returncode=0, stdout="{}")
                result = _allocate_container_ip("egg-isolated")
                assert result is not None
                assert result.startswith("172.32.0.")
                # Should skip .1 (docker gw) and .2 (gateway_isolated_ip)
                assert result not in {"172.32.0.1", "172.32.0.2"}

    def test_skips_assigned_ips(self):
        """Skips IPs already assigned to containers."""
        ctx = _mock_context()
        containers_json = json.dumps(
            {
                "abc123": {"IPv4Address": "172.32.0.3/24"},
                "def456": {"IPv4Address": "172.32.0.4/24"},
            }
        )
        with patch("egg_lib.runtime.get_context", return_value=ctx):
            with patch("egg_lib.runtime.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout=containers_json)
                result = _allocate_container_ip("egg-isolated")
                assert result is not None
                # Should skip .1, .2 (reserved), .3, .4 (assigned)
                assert result not in {"172.32.0.1", "172.32.0.2", "172.32.0.3", "172.32.0.4"}

    def test_handles_docker_failure(self):
        """Returns None when docker inspect fails."""
        ctx = _mock_context()
        with patch("egg_lib.runtime.get_context", return_value=ctx):
            with patch(
                "egg_lib.runtime.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "docker"),
            ):
                result = _allocate_container_ip("egg-isolated")
                assert result is None


class TestCleanupWorktrees:
    """Tests for _cleanup_worktrees."""

    def test_calls_delete_worktrees(self):
        """Calls delete_worktrees with container ID."""
        with patch("egg_lib.runtime.delete_worktrees", return_value=(True, [], [])):
            _cleanup_worktrees("container-1")

    def test_handles_failure(self):
        """Handles delete_worktrees failure gracefully."""
        with patch("egg_lib.runtime.delete_worktrees", return_value=(False, [], ["error"])):
            _cleanup_worktrees("container-1")  # Should not raise

    def test_handles_exception(self):
        """Handles exceptions gracefully."""
        with patch("egg_lib.runtime.delete_worktrees", side_effect=Exception("boom")):
            _cleanup_worktrees("container-1")  # Should not raise


class TestCleanupSession:
    """Tests for _cleanup_session."""

    def test_deletes_session_and_worktrees(self):
        """Deletes session and cleans up worktrees."""
        with patch("egg_lib.runtime.delete_session", return_value=(True, None)):
            with patch("egg_lib.runtime.delete_worktrees", return_value=(True, [], [])):
                _cleanup_session("tok-123", "container-1")

    def test_handles_no_session_token(self):
        """Handles None session token."""
        with patch("egg_lib.runtime.delete_worktrees", return_value=(True, [], [])):
            _cleanup_session(None, "container-1")

    def test_handles_session_delete_failure(self):
        """Handles session deletion failure gracefully."""
        with patch("egg_lib.runtime.delete_session", return_value=(False, "error")):
            with patch("egg_lib.runtime.time.sleep"):
                with patch("egg_lib.runtime.delete_worktrees", return_value=(True, [], [])):
                    _cleanup_session("tok-123", "container-1")  # Should not raise

    def test_retries_on_failure(self):
        """Retries delete_session on transient failures with backoff."""
        with patch(
            "egg_lib.runtime.delete_session", return_value=(False, "gateway timeout")
        ) as mock_delete:
            with patch("egg_lib.runtime.time.sleep") as mock_sleep:
                with patch("egg_lib.runtime.delete_worktrees", return_value=(True, [], [])):
                    _cleanup_session("tok-123", "container-1")
                    assert mock_delete.call_count == 3
                    # Backoff: 1s after first failure, 2s after second
                    assert mock_sleep.call_args_list == [
                        ((1,),),  # 1 << 0
                        ((2,),),  # 1 << 1
                    ]

    def test_succeeds_on_second_attempt(self):
        """Retry recovers from first failure."""
        with patch(
            "egg_lib.runtime.delete_session",
            side_effect=[
                (False, "gateway timeout"),
                (True, None),
            ],
        ) as mock_delete:
            with patch("egg_lib.runtime.time.sleep") as mock_sleep:
                _cleanup_session("tok-123", "container-1")
                assert mock_delete.call_count == 2
                mock_sleep.assert_called_once_with(1)  # 1 << 0

    def test_retries_on_exception(self):
        """Retries on exceptions with backoff."""
        with patch(
            "egg_lib.runtime.delete_session",
            side_effect=[
                ConnectionError("refused"),
                ConnectionError("refused"),
                (True, None),
            ],
        ) as mock_delete:
            with patch("egg_lib.runtime.time.sleep") as mock_sleep:
                _cleanup_session("tok-123", "container-1")
                assert mock_delete.call_count == 3
                assert mock_sleep.call_args_list == [
                    ((1,),),
                    ((2,),),
                ]

    def test_no_retry_on_success(self):
        """No unnecessary retries when first attempt succeeds."""
        with patch("egg_lib.runtime.delete_session", return_value=(True, None)) as mock_delete:
            with patch("egg_lib.runtime.time.sleep") as mock_sleep:
                _cleanup_session("tok-123", "container-1")
                assert mock_delete.call_count == 1
                mock_sleep.assert_not_called()

    def test_worktree_cleanup_after_retry_exhaustion(self):
        """Falls back to local worktree cleanup when all retries fail."""
        with patch("egg_lib.runtime.delete_session", return_value=(False, "gateway unreachable")):
            with patch("egg_lib.runtime.time.sleep"):
                with patch(
                    "egg_lib.runtime.delete_worktrees", return_value=(True, [], [])
                ) as mock_wt:
                    _cleanup_session("tok-123", "container-1")
                    mock_wt.assert_called_once()

    def test_worktree_cleanup_after_exception_exhaustion(self):
        """Falls back to local worktree cleanup when all retries raise exceptions."""
        with patch(
            "egg_lib.runtime.delete_session",
            side_effect=ConnectionError("refused"),
        ):
            with patch("egg_lib.runtime.time.sleep"):
                with patch(
                    "egg_lib.runtime.delete_worktrees", return_value=(True, [], [])
                ) as mock_wt:
                    _cleanup_session("tok-123", "container-1")
                    mock_wt.assert_called_once()
