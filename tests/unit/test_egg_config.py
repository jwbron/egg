"""Tests for shared/egg_config/ modules."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from shared.egg_config.loader import (
    _expand_env_vars,
    find_config_file,
    load_config,
    load_yaml,
)
from shared.egg_config.validators import (
    ValidationResult,
    mask_secret,
    validate_config,
)


class TestExpandEnvVars:
    """Tests for _expand_env_vars function."""

    @patch.dict(os.environ, {"TEST_VAR": "hello"})
    def test_expand_string(self):
        """Test expanding env var in string."""
        result = _expand_env_vars("$TEST_VAR world")
        assert result == "hello world"

    @patch.dict(os.environ, {"TEST_VAR": "hello"})
    def test_expand_string_braces(self):
        """Test expanding env var with braces."""
        result = _expand_env_vars("${TEST_VAR} world")
        assert result == "hello world"

    @patch.dict(os.environ, {"VAR1": "a", "VAR2": "b"})
    def test_expand_dict(self):
        """Test expanding env vars in dict."""
        result = _expand_env_vars({"key1": "$VAR1", "key2": "$VAR2"})
        assert result == {"key1": "a", "key2": "b"}

    @patch.dict(os.environ, {"VAR": "item"})
    def test_expand_list(self):
        """Test expanding env vars in list."""
        result = _expand_env_vars(["$VAR", "static"])
        assert result == ["item", "static"]

    @patch.dict(os.environ, {"NESTED": "value"})
    def test_expand_nested(self):
        """Test expanding env vars in nested structure."""
        result = _expand_env_vars({"outer": {"inner": ["$NESTED", {"deep": "$NESTED"}]}})
        assert result == {"outer": {"inner": ["value", {"deep": "value"}]}}

    def test_expand_non_string_passthrough(self):
        """Test that non-string values pass through unchanged."""
        assert _expand_env_vars(42) == 42
        assert _expand_env_vars(3.14) == 3.14
        assert _expand_env_vars(True) is True
        assert _expand_env_vars(None) is None


class TestLoadYaml:
    """Tests for load_yaml function."""

    def test_load_valid_yaml(self, tmp_path):
        """Test loading valid YAML file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
egg:
  name: test
  debug: true
  count: 42
"""
        )
        result = load_yaml(config_file)
        assert result == {
            "egg": {
                "name": "test",
                "debug": True,
                "count": 42,
            }
        }

    def test_load_empty_yaml(self, tmp_path):
        """Test loading empty YAML file returns empty dict."""
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")
        result = load_yaml(config_file)
        assert result == {}

    def test_load_yaml_with_null(self, tmp_path):
        """Test loading YAML with null value returns empty dict."""
        config_file = tmp_path / "null.yaml"
        config_file.write_text("---\n")
        result = load_yaml(config_file)
        assert result == {}

    @patch.dict(os.environ, {"MY_VALUE": "expanded"})
    def test_load_yaml_expands_env_vars(self, tmp_path):
        """Test that load_yaml expands environment variables."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
key: $MY_VALUE
nested:
  value: ${MY_VALUE}
"""
        )
        result = load_yaml(config_file)
        assert result["key"] == "expanded"
        assert result["nested"]["value"] == "expanded"

    def test_load_yaml_file_not_found(self, tmp_path):
        """Test loading non-existent file raises error."""
        with pytest.raises(FileNotFoundError):
            load_yaml(tmp_path / "nonexistent.yaml")

    def test_load_yaml_invalid_syntax(self, tmp_path):
        """Test loading invalid YAML raises error."""
        config_file = tmp_path / "invalid.yaml"
        # Create truly invalid YAML with bad structure
        config_file.write_text(":\nkey\n  - invalid")
        with pytest.raises(yaml.YAMLError):
            load_yaml(config_file)


class TestFindConfigFile:
    """Tests for find_config_file function."""

    @patch.dict(os.environ, {"EGG_CONFIG": ""}, clear=False)
    def test_find_in_current_dir(self, tmp_path, monkeypatch):
        """Test finding config file in current directory."""
        monkeypatch.chdir(tmp_path)
        config_file = tmp_path / "egg.yaml"
        config_file.write_text("key: value")

        # Clear env var
        if "EGG_CONFIG" in os.environ:
            del os.environ["EGG_CONFIG"]

        result = find_config_file("egg.yaml", "EGG_CONFIG")
        assert result == config_file

    def test_find_via_env_var(self, tmp_path):
        """Test finding config file via environment variable."""
        config_file = tmp_path / "custom-config.yaml"
        config_file.write_text("key: value")

        with patch.dict(os.environ, {"EGG_CONFIG": str(config_file)}):
            result = find_config_file("egg.yaml", "EGG_CONFIG")
            assert result == config_file

    def test_env_var_file_not_exists(self, tmp_path):
        """Test that non-existent env var path falls through."""
        with patch.dict(os.environ, {"EGG_CONFIG": "/nonexistent/path.yaml"}):
            result = find_config_file("egg.yaml", "EGG_CONFIG")
            # Should not return the non-existent path
            # Will return None if nothing else found
            assert result is None or result != Path("/nonexistent/path.yaml")

    def test_find_in_search_paths(self, tmp_path):
        """Test finding config file in custom search paths."""
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        config_file = custom_dir / "egg.yaml"
        config_file.write_text("key: value")

        # Clear env var
        with patch.dict(os.environ, {}, clear=False):
            if "EGG_CONFIG" in os.environ:
                del os.environ["EGG_CONFIG"]

            result = find_config_file(
                "egg.yaml",
                "EGG_CONFIG",
                search_paths=[custom_dir / "egg.yaml"],
            )
            assert result == config_file

    def test_file_not_found_returns_none(self, tmp_path, monkeypatch):
        """Test returns None when config file not found."""
        monkeypatch.chdir(tmp_path)
        with patch.dict(os.environ, {}, clear=False):
            if "EGG_CONFIG" in os.environ:
                del os.environ["EGG_CONFIG"]
            result = find_config_file("nonexistent.yaml", "EGG_CONFIG")
            assert result is None


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_config_explicit_path(self, tmp_path):
        """Test loading config from explicit path."""
        config_file = tmp_path / "egg.yaml"
        config_file.write_text(
            """
egg:
  name: test
"""
        )
        result = load_config(config_path=config_file)
        assert result == {"egg": {"name": "test"}}

    def test_load_config_with_secrets(self, tmp_path):
        """Test loading config merges secrets."""
        config_file = tmp_path / "egg.yaml"
        config_file.write_text(
            """
egg:
  name: test
"""
        )
        secrets_file = tmp_path / "secrets.yaml"
        secrets_file.write_text(
            """
secrets:
  api_key: secret123
"""
        )
        result = load_config(config_path=config_file, secrets_path=secrets_file)
        assert result["egg"]["name"] == "test"
        assert result["secrets"]["api_key"] == "secret123"

    def test_load_config_secrets_without_wrapper(self, tmp_path):
        """Test loading secrets file without 'secrets' key."""
        config_file = tmp_path / "egg.yaml"
        config_file.write_text("egg: {}")

        secrets_file = tmp_path / "secrets.yaml"
        secrets_file.write_text(
            """
api_key: secret123
database_url: postgres://localhost
"""
        )
        result = load_config(config_path=config_file, secrets_path=secrets_file)
        assert result["secrets"]["api_key"] == "secret123"

    def test_load_config_no_files_found(self, tmp_path, monkeypatch):
        """Test loading config when no files found."""
        monkeypatch.chdir(tmp_path)
        with patch.dict(os.environ, {}, clear=False):
            if "EGG_CONFIG" in os.environ:
                del os.environ["EGG_CONFIG"]
            if "EGG_SECRETS" in os.environ:
                del os.environ["EGG_SECRETS"]
            result = load_config()
            assert result == {}


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_valid_result_is_truthy(self):
        """Test that valid result is truthy."""
        result = ValidationResult(valid=True)
        assert result
        assert bool(result) is True

    def test_invalid_result_is_falsy(self):
        """Test that invalid result is falsy."""
        result = ValidationResult(valid=False, errors=["Error"])
        assert not result
        assert bool(result) is False

    def test_result_with_warnings(self):
        """Test result with warnings but still valid."""
        result = ValidationResult(valid=True, warnings=["Warning"])
        assert result
        assert len(result.warnings) == 1

    def test_result_defaults(self):
        """Test default values."""
        result = ValidationResult(valid=True)
        assert result.errors == []
        assert result.warnings == []


class TestValidateConfig:
    """Tests for validate_config function."""

    def test_validate_empty_config(self):
        """Test validating empty config."""
        result = validate_config({})
        # Should be valid but with warnings
        assert result.valid
        assert len(result.warnings) > 0

    def test_validate_valid_config(self):
        """Test validating a valid config."""
        config = {
            "egg": {
                "git": {
                    "branch_prefix": "egg/",
                    "protected_branches": ["main", "master"],
                },
                "repositories": {
                    "allowed": ["owner/repo", "org/*"],
                },
            }
        }
        result = validate_config(config)
        assert result.valid

    def test_validate_empty_branch_prefix(self):
        """Test that empty branch_prefix is invalid."""
        config = {
            "egg": {
                "git": {
                    "branch_prefix": "",
                },
            }
        }
        result = validate_config(config)
        assert not result.valid
        assert any("branch_prefix cannot be empty" in e for e in result.errors)

    def test_validate_invalid_branch_prefix_chars(self):
        """Test that invalid characters in branch_prefix is invalid."""
        config = {
            "egg": {
                "git": {
                    "branch_prefix": "egg..prefix",
                },
            }
        }
        result = validate_config(config)
        assert not result.valid
        assert any("invalid characters" in e for e in result.errors)

    def test_validate_invalid_branch_prefix_start(self):
        """Test that branch_prefix must start with letter."""
        config = {
            "egg": {
                "git": {
                    "branch_prefix": "123prefix/",
                },
            }
        }
        result = validate_config(config)
        assert not result.valid

    def test_validate_empty_protected_branches_warning(self):
        """Test that empty protected_branches generates warning."""
        config = {
            "egg": {
                "git": {
                    "branch_prefix": "egg/",
                    "protected_branches": [],
                },
            }
        }
        result = validate_config(config)
        assert any("protected_branches is empty" in w for w in result.warnings)

    def test_validate_empty_allowed_repos_warning(self):
        """Test that empty allowed repos generates warning."""
        config = {
            "egg": {
                "repositories": {
                    "allowed": [],
                },
            }
        }
        result = validate_config(config)
        assert any("allowed is empty" in w for w in result.warnings)

    def test_validate_invalid_repo_format(self):
        """Test that invalid repo format is rejected."""
        config = {
            "egg": {
                "repositories": {
                    "allowed": ["invalid-no-slash"],
                },
            }
        }
        result = validate_config(config)
        assert not result.valid
        assert any("Invalid repository format" in e for e in result.errors)

    def test_validate_secrets_no_github_auth(self):
        """Test that missing GitHub auth generates warning."""
        config = {
            "secrets": {
                "anthropic": {"api_key": "sk-ant-xxx"},
            }
        }
        result = validate_config(config)
        assert any("No GitHub authentication" in w for w in result.warnings)

    def test_validate_secrets_no_anthropic_auth(self):
        """Test that missing Anthropic auth generates warning."""
        config = {
            "secrets": {
                "pats": [{"token": "ghp_xxx"}],
            }
        }
        result = validate_config(config)
        assert any("No Anthropic authentication" in w for w in result.warnings)

    def test_validate_github_app_missing_app_id(self):
        """Test that missing app_id is invalid."""
        config = {
            "secrets": {
                "github_app": {
                    "private_key_path": "/path/to/key.pem",
                },
            }
        }
        result = validate_config(config)
        assert not result.valid
        assert any("app_id is required" in e for e in result.errors)

    def test_validate_github_app_missing_key_path(self):
        """Test that missing private_key_path is invalid."""
        config = {
            "secrets": {
                "github_app": {
                    "app_id": "123456",
                },
            }
        }
        result = validate_config(config)
        assert not result.valid
        assert any("private_key_path is required" in e for e in result.errors)

    def test_validate_github_app_key_not_found(self, tmp_path):
        """Test that non-existent key file is invalid."""
        config = {
            "secrets": {
                "github_app": {
                    "app_id": "123456",
                    "private_key_path": str(tmp_path / "nonexistent.pem"),
                },
            }
        }
        result = validate_config(config)
        assert not result.valid
        assert any("private key not found" in e for e in result.errors)

    def test_validate_github_app_valid(self, tmp_path):
        """Test that valid GitHub app config passes."""
        key_file = tmp_path / "key.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----")

        config = {
            "secrets": {
                "github_app": {
                    "app_id": "123456",
                    "private_key_path": str(key_file),
                },
            }
        }
        result = validate_config(config)
        # Should not have GitHub app errors
        assert not any("github_app" in e for e in result.errors)


class TestMaskSecret:
    """Tests for mask_secret function."""

    def test_mask_normal_secret(self):
        """Test masking a normal secret."""
        secret = "sk-ant-api-key-12345"  # 20 chars
        result = mask_secret(secret)
        # With default visible_chars=4, shows "sk-a" + 16 asterisks
        assert result.startswith("sk-a")
        assert "*" in result
        assert len(result) == len(secret)

    def test_mask_short_secret(self):
        """Test masking a short secret."""
        result = mask_secret("abc")
        assert result == "***"

    def test_mask_exact_length_secret(self):
        """Test masking secret at exact visible chars length."""
        result = mask_secret("abcd")
        assert result == "****"

    def test_mask_empty_secret(self):
        """Test masking empty string."""
        result = mask_secret("")
        assert result == ""

    def test_mask_custom_visible_chars(self):
        """Test masking with custom visible chars."""
        result = mask_secret("my-long-secret-value", visible_chars=8)
        assert result == "my-long-************"
        assert result.startswith("my-long-")
