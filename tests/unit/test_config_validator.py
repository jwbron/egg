"""Tests for gateway/config_validator.py."""

import os
from unittest.mock import patch

import pytest

from gateway.config_validator import (
    ConfigError,
    is_private_mode_enabled,
    validate_config,
    validate_network_lockdown_mode,
)


class TestConfigError:
    """Tests for ConfigError exception."""

    def test_config_error_message(self):
        """Test ConfigError contains message."""
        with pytest.raises(ConfigError) as exc_info:
            raise ConfigError("Test error")
        assert "Test error" in str(exc_info.value)


class TestValidateConfig:
    """Tests for validate_config function."""

    def test_validate_config_missing_secrets_dir(self, tmp_path, capsys):
        """Test validation fails when secrets dir is missing."""
        nonexistent = tmp_path / "nonexistent"
        with pytest.raises(ConfigError):
            validate_config(secrets_dir=nonexistent)
        captured = capsys.readouterr()
        assert "Secrets directory not mounted" in captured.err

    def test_validate_config_missing_launcher_secret(self, tmp_path, capsys):
        """Test validation fails when launcher secret is missing."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        # Don't create launcher-secret file

        with pytest.raises(ConfigError):
            validate_config(secrets_dir=secrets_dir, squid_conf_path=tmp_path / "squid.conf")
        captured = capsys.readouterr()
        assert "Launcher secret not found" in captured.err

    def test_validate_config_missing_squid_conf(self, tmp_path, capsys):
        """Test validation fails when squid.conf is missing."""
        # Create valid secrets
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("secret123")

        # Create squid dir but not squid.conf
        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()

        with pytest.raises(ConfigError):
            validate_config(
                secrets_dir=secrets_dir,
                squid_conf_path=squid_dir / "squid.conf",
            )
        captured = capsys.readouterr()
        assert "Squid configuration not found" in captured.err

    def test_validate_config_missing_squid_allow_all(self, tmp_path, capsys):
        """Test validation fails when squid-allow-all.conf is missing."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("secret123")

        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()
        (squid_dir / "squid.conf").write_text("http_port 3128")
        # Don't create squid-allow-all.conf

        with pytest.raises(ConfigError):
            validate_config(
                secrets_dir=secrets_dir,
                squid_conf_path=squid_dir / "squid.conf",
            )
        captured = capsys.readouterr()
        assert "allow-all configuration not found" in captured.err

    def test_validate_config_missing_allowed_domains(self, tmp_path, capsys):
        """Test validation fails when allowed_domains.txt is missing."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("secret123")

        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()
        (squid_dir / "squid.conf").write_text("http_port 3128")
        (squid_dir / "squid-allow-all.conf").write_text("acl allow_all src all")
        # Don't create allowed_domains.txt

        with pytest.raises(ConfigError):
            validate_config(
                secrets_dir=secrets_dir,
                squid_conf_path=squid_dir / "squid.conf",
            )
        captured = capsys.readouterr()
        assert "Allowed domains file not found" in captured.err

    def test_validate_config_empty_allowed_domains(self, tmp_path, capsys):
        """Test validation fails when allowed_domains.txt is empty."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("secret123")

        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()
        (squid_dir / "squid.conf").write_text("http_port 3128")
        (squid_dir / "squid-allow-all.conf").write_text("acl allow_all src all")
        (squid_dir / "allowed_domains.txt").write_text("# comment only\n")
        # Don't create squid-ca.pem

        with pytest.raises(ConfigError) as exc_info:
            validate_config(
                secrets_dir=secrets_dir,
                squid_conf_path=squid_dir / "squid.conf",
            )
        # Should have multiple errors
        assert "configuration error" in str(exc_info.value)

    def test_validate_config_missing_squid_cert(self, tmp_path, capsys):
        """Test validation fails when squid-ca.pem is missing."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("secret123")

        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()
        (squid_dir / "squid.conf").write_text("http_port 3128")
        (squid_dir / "squid-allow-all.conf").write_text("acl allow_all src all")
        (squid_dir / "allowed_domains.txt").write_text("api.anthropic.com\n")
        # Don't create squid-ca.pem

        with pytest.raises(ConfigError):
            validate_config(
                secrets_dir=secrets_dir,
                squid_conf_path=squid_dir / "squid.conf",
            )
        captured = capsys.readouterr()
        assert "CA certificate not found" in captured.err

    def test_validate_config_success(self, tmp_path):
        """Test validation succeeds with all required files."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("secret123")

        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()
        (squid_dir / "squid.conf").write_text("http_port 3128")
        (squid_dir / "squid-allow-all.conf").write_text("acl allow_all src all")
        (squid_dir / "allowed_domains.txt").write_text("api.anthropic.com\n")
        (squid_dir / "squid-ca.pem").write_text("-----BEGIN CERTIFICATE-----\n")

        # Should not raise
        validate_config(
            secrets_dir=secrets_dir,
            squid_conf_path=squid_dir / "squid.conf",
        )

    def test_validate_config_allowed_domains_with_comments(self, tmp_path):
        """Test validation handles comments in allowed_domains.txt."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("secret123")

        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()
        (squid_dir / "squid.conf").write_text("http_port 3128")
        (squid_dir / "squid-allow-all.conf").write_text("acl allow_all src all")
        (squid_dir / "allowed_domains.txt").write_text(
            "# This is a comment\n"
            "\n"
            "api.anthropic.com\n"
            "# Another comment\n"
            "api.github.com\n"
        )
        (squid_dir / "squid-ca.pem").write_text("-----BEGIN CERTIFICATE-----\n")

        # Should not raise - has valid domains
        validate_config(
            secrets_dir=secrets_dir,
            squid_conf_path=squid_dir / "squid.conf",
        )

    def test_validate_config_squid_dir_not_exists(self, tmp_path):
        """Test validation skips squid checks when squid dir doesn't exist."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("secret123")

        # Point to non-existent squid dir - validation should succeed
        # because squid checks are optional when parent dir doesn't exist
        nonexistent_squid = tmp_path / "nonexistent" / "squid.conf"
        validate_config(
            secrets_dir=secrets_dir,
            squid_conf_path=nonexistent_squid,
        )


class TestValidateNetworkLockdownMode:
    """Tests for validate_network_lockdown_mode function."""

    def test_all_components_present(self, tmp_path):
        """Test returns True when all lockdown components are present."""
        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()
        (squid_dir / "squid.conf").write_text("http_port 3128")
        (squid_dir / "allowed_domains.txt").write_text("api.anthropic.com")
        (squid_dir / "squid-ca.pem").write_text("-----BEGIN CERTIFICATE-----")

        assert validate_network_lockdown_mode(squid_conf_dir=squid_dir) is True

    def test_missing_squid_conf(self, tmp_path):
        """Test returns False when squid.conf is missing."""
        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()
        (squid_dir / "allowed_domains.txt").write_text("api.anthropic.com")
        (squid_dir / "squid-ca.pem").write_text("-----BEGIN CERTIFICATE-----")

        assert validate_network_lockdown_mode(squid_conf_dir=squid_dir) is False

    def test_missing_allowed_domains(self, tmp_path):
        """Test returns False when allowed_domains.txt is missing."""
        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()
        (squid_dir / "squid.conf").write_text("http_port 3128")
        (squid_dir / "squid-ca.pem").write_text("-----BEGIN CERTIFICATE-----")

        assert validate_network_lockdown_mode(squid_conf_dir=squid_dir) is False

    def test_missing_squid_cert(self, tmp_path):
        """Test returns False when squid-ca.pem is missing."""
        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()
        (squid_dir / "squid.conf").write_text("http_port 3128")
        (squid_dir / "allowed_domains.txt").write_text("api.anthropic.com")

        assert validate_network_lockdown_mode(squid_conf_dir=squid_dir) is False


class TestIsPrivateModeEnabled:
    """Tests for is_private_mode_enabled function."""

    @patch.dict(os.environ, {"PRIVATE_MODE": "true"})
    def test_private_mode_true(self):
        """Test returns True when PRIVATE_MODE=true."""
        assert is_private_mode_enabled() is True

    @patch.dict(os.environ, {"PRIVATE_MODE": "TRUE"})
    def test_private_mode_true_uppercase(self):
        """Test returns True when PRIVATE_MODE=TRUE."""
        assert is_private_mode_enabled() is True

    @patch.dict(os.environ, {"PRIVATE_MODE": "1"})
    def test_private_mode_one(self):
        """Test returns True when PRIVATE_MODE=1."""
        assert is_private_mode_enabled() is True

    @patch.dict(os.environ, {"PRIVATE_MODE": "yes"})
    def test_private_mode_yes(self):
        """Test returns True when PRIVATE_MODE=yes."""
        assert is_private_mode_enabled() is True

    @patch.dict(os.environ, {"PRIVATE_MODE": "false"})
    def test_private_mode_false(self):
        """Test returns False when PRIVATE_MODE=false."""
        assert is_private_mode_enabled() is False

    @patch.dict(os.environ, {"PRIVATE_MODE": "0"})
    def test_private_mode_zero(self):
        """Test returns False when PRIVATE_MODE=0."""
        assert is_private_mode_enabled() is False

    @patch.dict(os.environ, {}, clear=True)
    def test_private_mode_not_set(self):
        """Test returns False when PRIVATE_MODE not set (default)."""
        # Need to clear it entirely
        if "PRIVATE_MODE" in os.environ:
            del os.environ["PRIVATE_MODE"]
        assert is_private_mode_enabled() is False

    @patch.dict(os.environ, {"PRIVATE_MODE": "  true  "})
    def test_private_mode_with_whitespace(self):
        """Test handles whitespace in value."""
        assert is_private_mode_enabled() is True
