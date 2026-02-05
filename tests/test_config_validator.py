"""Tests for gateway/config_validator.py."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "gateway"))
from config_validator import (
    ConfigError,
    is_private_mode_enabled,
    validate_config,
    validate_network_lockdown_mode,
)


class TestIsPrivateModeEnabled:
    """Tests for is_private_mode_enabled()."""

    def test_default_is_false(self):
        with patch.dict(os.environ, {}, clear=True):
            assert is_private_mode_enabled() is False

    def test_explicit_false(self):
        with patch.dict(os.environ, {"PRIVATE_MODE": "false"}):
            assert is_private_mode_enabled() is False

    def test_true_string(self):
        with patch.dict(os.environ, {"PRIVATE_MODE": "true"}):
            assert is_private_mode_enabled() is True

    def test_one_string(self):
        with patch.dict(os.environ, {"PRIVATE_MODE": "1"}):
            assert is_private_mode_enabled() is True

    def test_yes_string(self):
        with patch.dict(os.environ, {"PRIVATE_MODE": "yes"}):
            assert is_private_mode_enabled() is True

    def test_case_insensitive(self):
        with patch.dict(os.environ, {"PRIVATE_MODE": "TRUE"}):
            assert is_private_mode_enabled() is True

    def test_whitespace_stripped(self):
        with patch.dict(os.environ, {"PRIVATE_MODE": " true "}):
            assert is_private_mode_enabled() is True

    def test_invalid_value(self):
        with patch.dict(os.environ, {"PRIVATE_MODE": "maybe"}):
            assert is_private_mode_enabled() is False


class TestValidateNetworkLockdownMode:
    """Tests for validate_network_lockdown_mode()."""

    def test_all_files_present(self, tmp_path):
        squid_conf = tmp_path / "squid.conf"
        domains = tmp_path / "allowed_domains.txt"
        cert = tmp_path / "squid-ca.pem"
        squid_conf.touch()
        domains.touch()
        cert.touch()

        with patch("config_validator.Path") as mock_path:
            # Each Path() call returns an object whose .is_file() returns True
            mock_path.return_value.is_file.return_value = True
            assert validate_network_lockdown_mode() is True

    def test_missing_squid_conf(self):
        with patch("config_validator.Path") as mock_path:
            def is_file_side_effect(path_str=None):
                mock = type("MockPath", (), {"is_file": lambda self: False})()
                return mock

            # Return False for squid.conf, True for others
            original_path = Path

            def fake_path(p):
                result = original_path(p)
                return result

            mock_path.side_effect = fake_path
            # Just test with all missing
            mock_path.return_value.is_file.return_value = False
            assert validate_network_lockdown_mode() is False


class TestValidateConfig:
    """Tests for validate_config()."""

    def test_missing_secrets_dir(self):
        """Config validation fails when /secrets doesn't exist."""
        with patch("config_validator.Path") as mock_path:
            mock_instance = mock_path.return_value
            mock_instance.is_dir.return_value = False
            mock_instance.is_file.return_value = False

            with pytest.raises(ConfigError, match="configuration error"):
                validate_config()

    def test_missing_launcher_secret(self, tmp_path):
        """Config validation fails when launcher-secret is missing."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()

        with patch("config_validator.Path") as mock_path:
            def path_factory(path_str):
                p = Path(path_str)
                if path_str == "/secrets":
                    return secrets_dir
                return p

            mock_path.side_effect = path_factory

            with pytest.raises(ConfigError):
                validate_config()

    def test_all_present(self, tmp_path):
        """Config validation passes when everything exists."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("secret")

        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()
        (squid_dir / "squid.conf").touch()
        (squid_dir / "squid-allow-all.conf").touch()
        (squid_dir / "allowed_domains.txt").write_text("api.anthropic.com\n")
        (squid_dir / "squid-ca.pem").touch()

        with patch("config_validator.Path") as mock_path:
            def path_factory(path_str):
                if path_str == "/secrets":
                    return secrets_dir
                if path_str == "/secrets/launcher-secret":
                    return secrets_dir / "launcher-secret"
                if path_str == "/etc/squid/squid.conf":
                    return squid_dir / "squid.conf"
                if path_str == "/etc/squid/squid-allow-all.conf":
                    return squid_dir / "squid-allow-all.conf"
                if path_str == "/etc/squid/allowed_domains.txt":
                    return squid_dir / "allowed_domains.txt"
                if path_str == "/etc/squid/squid-ca.pem":
                    return squid_dir / "squid-ca.pem"
                return Path(path_str)

            mock_path.side_effect = path_factory
            # Should not raise
            validate_config()

    def test_empty_domains_file(self, tmp_path):
        """Config validation fails when domains file has no domains."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("secret")

        squid_dir = tmp_path / "squid"
        squid_dir.mkdir()
        (squid_dir / "squid.conf").touch()
        (squid_dir / "squid-allow-all.conf").touch()
        (squid_dir / "allowed_domains.txt").write_text("# just a comment\n")
        (squid_dir / "squid-ca.pem").touch()

        with patch("config_validator.Path") as mock_path:
            def path_factory(path_str):
                if path_str == "/secrets":
                    return secrets_dir
                if path_str == "/secrets/launcher-secret":
                    return secrets_dir / "launcher-secret"
                if path_str == "/etc/squid/squid.conf":
                    return squid_dir / "squid.conf"
                if path_str == "/etc/squid/squid-allow-all.conf":
                    return squid_dir / "squid-allow-all.conf"
                if path_str == "/etc/squid/allowed_domains.txt":
                    return squid_dir / "allowed_domains.txt"
                if path_str == "/etc/squid/squid-ca.pem":
                    return squid_dir / "squid-ca.pem"
                return Path(path_str)

            mock_path.side_effect = path_factory
            with pytest.raises(ConfigError, match="1 configuration error"):
                validate_config()
