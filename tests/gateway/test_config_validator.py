"""Tests for gateway config_validator module."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from config_validator import (
    ConfigError,
    is_private_mode_enabled,
    validate_config,
    validate_network_lockdown_mode,
)


class TestIsPrivateModeEnabled:
    """Tests for is_private_mode_enabled function."""

    def test_default_is_public(self, monkeypatch):
        """Default mode is public (false)."""
        monkeypatch.delenv("PRIVATE_MODE", raising=False)
        assert is_private_mode_enabled() is False

    def test_explicit_false(self, monkeypatch):
        """Explicit false."""
        monkeypatch.setenv("PRIVATE_MODE", "false")
        assert is_private_mode_enabled() is False

    def test_explicit_true(self, monkeypatch):
        """Explicit true enables private mode."""
        monkeypatch.setenv("PRIVATE_MODE", "true")
        assert is_private_mode_enabled() is True

    def test_value_one(self, monkeypatch):
        """Value '1' enables private mode."""
        monkeypatch.setenv("PRIVATE_MODE", "1")
        assert is_private_mode_enabled() is True

    def test_value_yes(self, monkeypatch):
        """Value 'yes' enables private mode."""
        monkeypatch.setenv("PRIVATE_MODE", "yes")
        assert is_private_mode_enabled() is True

    def test_value_True_uppercase(self, monkeypatch):
        """Value 'True' enables private mode (case-insensitive)."""
        monkeypatch.setenv("PRIVATE_MODE", "True")
        assert is_private_mode_enabled() is True

    def test_value_no(self, monkeypatch):
        """Value 'no' is public mode."""
        monkeypatch.setenv("PRIVATE_MODE", "no")
        assert is_private_mode_enabled() is False

    def test_whitespace_handling(self, monkeypatch):
        """Whitespace is stripped."""
        monkeypatch.setenv("PRIVATE_MODE", "  true  ")
        assert is_private_mode_enabled() is True


class TestValidateConfig:
    """Tests for validate_config function."""

    def test_missing_secrets_dir(self, tmp_path):
        """Missing /secrets directory causes error."""
        with patch("config_validator.Path") as mock_path:
            # Make all Path() calls return mock objects
            mock_secrets = mock_path.return_value
            mock_secrets.__truediv__ = lambda s, k: mock_path(str(s) + "/" + k)

            # Simpler approach: just test the function raises ConfigError
            # when the expected paths don't exist
            with pytest.raises(ConfigError):
                validate_config()

    def test_valid_config(self, tmp_path):
        """Valid config passes validation."""
        # Create all required files
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("test-secret")

        squid_dir = tmp_path / "etc" / "squid"
        squid_dir.mkdir(parents=True)
        (squid_dir / "squid.conf").write_text("# config")
        (squid_dir / "squid-allow-all.conf").write_text("# config")
        (squid_dir / "allowed_domains.txt").write_text("api.anthropic.com\n")
        (squid_dir / "squid-ca.pem").write_text("cert")

        # Patch Path to point to our temp dirs
        original_path = Path

        def mock_path_init(path_str):
            if path_str == "/secrets":
                return original_path(secrets_dir)
            if path_str.startswith("/etc/squid/"):
                filename = path_str.split("/")[-1]
                return original_path(squid_dir / filename)
            return original_path(path_str)

        with patch("config_validator.Path", side_effect=mock_path_init):
            # Should not raise
            validate_config()


class TestValidateNetworkLockdownMode:
    """Tests for validate_network_lockdown_mode function."""

    def test_all_present(self, tmp_path):
        """Returns True when all lockdown components present."""
        squid_dir = tmp_path / "etc" / "squid"
        squid_dir.mkdir(parents=True)
        (squid_dir / "squid.conf").write_text("config")
        (squid_dir / "allowed_domains.txt").write_text("api.anthropic.com")
        (squid_dir / "squid-ca.pem").write_text("cert")

        original_path = Path

        def mock_path_init(path_str):
            if path_str.startswith("/etc/squid/"):
                filename = path_str.split("/")[-1]
                return original_path(squid_dir / filename)
            return original_path(path_str)

        with patch("config_validator.Path", side_effect=mock_path_init):
            assert validate_network_lockdown_mode() is True

    def test_missing_squid_conf(self, tmp_path):
        """Returns False when squid.conf missing."""
        squid_dir = tmp_path / "etc" / "squid"
        squid_dir.mkdir(parents=True)
        (squid_dir / "allowed_domains.txt").write_text("api.anthropic.com")
        (squid_dir / "squid-ca.pem").write_text("cert")

        original_path = Path

        def mock_path_init(path_str):
            if path_str.startswith("/etc/squid/"):
                filename = path_str.split("/")[-1]
                return original_path(squid_dir / filename)
            return original_path(path_str)

        with patch("config_validator.Path", side_effect=mock_path_init):
            assert validate_network_lockdown_mode() is False

    def test_missing_domains_file(self, tmp_path):
        """Returns False when allowed_domains.txt missing."""
        squid_dir = tmp_path / "etc" / "squid"
        squid_dir.mkdir(parents=True)
        (squid_dir / "squid.conf").write_text("config")
        (squid_dir / "squid-ca.pem").write_text("cert")

        original_path = Path

        def mock_path_init(path_str):
            if path_str.startswith("/etc/squid/"):
                filename = path_str.split("/")[-1]
                return original_path(squid_dir / filename)
            return original_path(path_str)

        with patch("config_validator.Path", side_effect=mock_path_init):
            assert validate_network_lockdown_mode() is False


class TestConfigError:
    """Tests for ConfigError exception."""

    def test_is_exception(self):
        """ConfigError is an Exception."""
        err = ConfigError("test error")
        assert isinstance(err, Exception)
        assert str(err) == "test error"
