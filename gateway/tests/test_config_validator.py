"""Tests for config_validator.py."""

import os
from unittest.mock import patch

import pytest
from config_validator import (
    ConfigError,
    is_private_mode_enabled,
    validate_config,
    validate_network_lockdown_mode,
)


class TestConfigError:
    """Tests for ConfigError exception."""

    def test_is_exception(self):
        """ConfigError is an Exception subclass."""
        assert issubclass(ConfigError, Exception)

    def test_message(self):
        """ConfigError stores error message."""
        err = ConfigError("test error")
        assert str(err) == "test error"


class TestValidateConfig:
    """Tests for validate_config."""

    def test_missing_secrets_dir(self, tmp_path):
        """validate_config raises when /secrets dir is missing."""
        with patch("config_validator.Path") as mock_path:
            # All paths return False for is_file/is_dir
            instance = mock_path.return_value
            instance.is_dir.return_value = False
            instance.is_file.return_value = False

            # Make Path("/secrets") return our mock
            def path_factory(p):
                m = type(instance)(p)
                m.is_dir = lambda: False
                m.is_file = lambda: False
                return m

            # Simpler approach: use tmp_path
            with pytest.raises(ConfigError, match="configuration error"):
                # Patch individual paths
                with patch("config_validator.Path", wraps=type(tmp_path)):
                    _mock_secrets = type(tmp_path)(str(tmp_path / "nonexistent_secrets"))
                    _calls = {}

                    def make_path(p):
                        from pathlib import Path

                        return Path(p)

                    with patch("config_validator.Path", side_effect=make_path):
                        validate_config()

    def test_all_files_present(self, tmp_path):
        """validate_config passes when all required files exist."""
        # Create all required files
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("secret123")

        squid_dir = tmp_path / "etc" / "squid"
        squid_dir.mkdir(parents=True)
        (squid_dir / "squid.conf").write_text("config")
        (squid_dir / "squid-allow-all.conf").write_text("config")
        (squid_dir / "allowed_domains.txt").write_text("api.anthropic.com\n")
        (squid_dir / "squid-ca.pem").write_text("cert")

        from pathlib import Path as RealPath

        def mock_path(p):
            p = str(p)
            if p == "/secrets":
                return RealPath(str(secrets_dir))
            if p.startswith("/secrets/"):
                return RealPath(str(secrets_dir / p[len("/secrets/"):]))
            if p.startswith("/etc/squid/"):
                return RealPath(str(squid_dir / p[len("/etc/squid/"):]))
            return RealPath(p)

        with patch("config_validator.Path", side_effect=mock_path):
            validate_config()  # Should not raise

    def test_missing_launcher_secret(self, tmp_path):
        """validate_config reports missing launcher secret."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        # No launcher-secret file

        squid_dir = tmp_path / "etc" / "squid"
        squid_dir.mkdir(parents=True)
        (squid_dir / "squid.conf").write_text("config")
        (squid_dir / "squid-allow-all.conf").write_text("config")
        (squid_dir / "allowed_domains.txt").write_text("api.anthropic.com\n")
        (squid_dir / "squid-ca.pem").write_text("cert")

        from pathlib import Path as RealPath

        def mock_path(p):
            p = str(p)
            if p == "/secrets":
                return RealPath(str(secrets_dir))
            if p.startswith("/secrets/"):
                return RealPath(str(secrets_dir / p[len("/secrets/"):]))
            if p.startswith("/etc/squid/"):
                return RealPath(str(squid_dir / p[len("/etc/squid/"):]))
            return RealPath(p)

        with patch("config_validator.Path", side_effect=mock_path):
            with pytest.raises(ConfigError, match="1 configuration error"):
                validate_config()

    def test_empty_allowed_domains(self, tmp_path):
        """validate_config reports empty allowed domains file."""
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "launcher-secret").write_text("secret123")

        squid_dir = tmp_path / "etc" / "squid"
        squid_dir.mkdir(parents=True)
        (squid_dir / "squid.conf").write_text("config")
        (squid_dir / "squid-allow-all.conf").write_text("config")
        (squid_dir / "allowed_domains.txt").write_text("# just comments\n")
        (squid_dir / "squid-ca.pem").write_text("cert")

        from pathlib import Path as RealPath

        def mock_path(p):
            p = str(p)
            if p == "/secrets":
                return RealPath(str(secrets_dir))
            if p.startswith("/secrets/"):
                return RealPath(str(secrets_dir / p[len("/secrets/"):]))
            if p.startswith("/etc/squid/"):
                return RealPath(str(squid_dir / p[len("/etc/squid/"):]))
            return RealPath(p)

        with patch("config_validator.Path", side_effect=mock_path):
            with pytest.raises(ConfigError, match="1 configuration error"):
                validate_config()

    def test_multiple_errors(self, tmp_path):
        """validate_config reports multiple errors at once."""
        from pathlib import Path as RealPath

        def mock_path(p):
            # Everything missing
            return RealPath(str(tmp_path / "nonexistent" / str(p).lstrip("/")))

        with patch("config_validator.Path", side_effect=mock_path):
            with pytest.raises(ConfigError, match="configuration error"):
                validate_config()


class TestValidateNetworkLockdownMode:
    """Tests for validate_network_lockdown_mode."""

    def test_all_present(self, tmp_path):
        """Returns True when all lockdown components are present."""
        squid_dir = tmp_path / "etc" / "squid"
        squid_dir.mkdir(parents=True)
        (squid_dir / "squid.conf").write_text("config")
        (squid_dir / "allowed_domains.txt").write_text("api.anthropic.com")
        (squid_dir / "squid-ca.pem").write_text("cert")

        from pathlib import Path as RealPath

        def mock_path(p):
            p = str(p)
            if p.startswith("/etc/squid/"):
                return RealPath(str(squid_dir / p[len("/etc/squid/"):]))
            return RealPath(p)

        with patch("config_validator.Path", side_effect=mock_path):
            assert validate_network_lockdown_mode() is True

    def test_missing_component(self, tmp_path):
        """Returns False when a lockdown component is missing."""
        from pathlib import Path as RealPath

        def mock_path(p):
            return RealPath(str(tmp_path / "nonexistent" / str(p).lstrip("/")))

        with patch("config_validator.Path", side_effect=mock_path):
            assert validate_network_lockdown_mode() is False


class TestIsPrivateModeEnabled:
    """Tests for is_private_mode_enabled."""

    @patch.dict(os.environ, {"PRIVATE_MODE": "true"})
    def test_true(self):
        """Returns True for 'true'."""
        assert is_private_mode_enabled() is True

    @patch.dict(os.environ, {"PRIVATE_MODE": "1"})
    def test_one(self):
        """Returns True for '1'."""
        assert is_private_mode_enabled() is True

    @patch.dict(os.environ, {"PRIVATE_MODE": "yes"})
    def test_yes(self):
        """Returns True for 'yes'."""
        assert is_private_mode_enabled() is True

    @patch.dict(os.environ, {"PRIVATE_MODE": "TRUE"})
    def test_case_insensitive(self):
        """Returns True for case-insensitive match."""
        assert is_private_mode_enabled() is True

    @patch.dict(os.environ, {"PRIVATE_MODE": "false"})
    def test_false(self):
        """Returns False for 'false'."""
        assert is_private_mode_enabled() is False

    @patch.dict(os.environ, {}, clear=True)
    def test_not_set(self):
        """Returns False when PRIVATE_MODE is not set."""
        os.environ.pop("PRIVATE_MODE", None)
        assert is_private_mode_enabled() is False

    @patch.dict(os.environ, {"PRIVATE_MODE": "  true  "})
    def test_strips_whitespace(self):
        """Returns True when value has extra whitespace."""
        assert is_private_mode_enabled() is True
