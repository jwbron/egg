"""Tests for the sensitive data redactor."""

import pytest

from egg_contracts.redactor import (
    Redactor,
    RedactorConfig,
    redact_text,
    redact_dict,
    redact_command,
    REDACTED_PLACEHOLDER,
)


class TestRedactorPatterns:
    """Tests for pattern-based redaction."""

    def test_redact_openai_api_key(self):
        """Test redacting OpenAI API keys."""
        redactor = Redactor()
        text = "Using key sk-abc123def456ghi789jkl012mno345pqr678stu901vwxyz"
        result = redactor.redact_text(text)
        assert "sk-abc123" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_redact_anthropic_api_key(self):
        """Test redacting Anthropic API keys."""
        redactor = Redactor()
        text = "Key: sk-ant-api03-abc123def456ghi789jkl012mno345-xyz"
        result = redactor.redact_text(text)
        assert "sk-ant-" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_redact_github_token(self):
        """Test redacting GitHub tokens."""
        redactor = Redactor()

        # Personal access token
        text1 = "Token: ghp_abc123def456ghi789jkl012mno345pqrstu"
        result1 = redactor.redact_text(text1)
        assert "ghp_" not in result1

        # OAuth token
        text2 = "Token: gho_abc123def456ghi789jkl012mno345pqrstu"
        result2 = redactor.redact_text(text2)
        assert "gho_" not in result2

    def test_redact_bearer_token(self):
        """Test redacting Bearer tokens."""
        redactor = Redactor()
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = redactor.redact_text(text)
        assert "eyJhbGci" not in result

    def test_redact_aws_access_key(self):
        """Test redacting AWS access keys."""
        redactor = Redactor()
        text = "Access key: AKIAIOSFODNN7EXAMPLE"
        result = redactor.redact_text(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_redact_password_assignment(self):
        """Test redacting password assignments."""
        redactor = Redactor()

        # Various formats
        texts = [
            "password = 'secret123'",
            'password: "secret456"',
            "PASSWORD='mysecret'",
        ]
        for text in texts:
            result = redactor.redact_text(text)
            assert "secret" not in result.lower()


class TestSensitiveEnvNames:
    """Tests for sensitive environment variable name detection."""

    def test_sensitive_env_patterns(self):
        """Test detection of sensitive env var names."""
        redactor = Redactor()

        sensitive_names = [
            "API_TOKEN",
            "GITHUB_TOKEN",
            "ANTHROPIC_API_KEY",
            "DATABASE_PASSWORD",
            "AWS_SECRET_ACCESS_KEY",
            "MY_SECRET",
        ]
        for name in sensitive_names:
            assert redactor.is_sensitive_env_name(name), f"{name} should be sensitive"

        non_sensitive_names = [
            "PATH",
            "HOME",
            "USER",
            "DEBUG",
            "LOG_LEVEL",
        ]
        for name in non_sensitive_names:
            assert not redactor.is_sensitive_env_name(name), f"{name} should not be sensitive"


class TestSensitivePaths:
    """Tests for sensitive file path detection."""

    def test_sensitive_paths(self):
        """Test detection of sensitive file paths."""
        redactor = Redactor()

        sensitive_paths = [
            "/home/user/.env",
            "/home/user/.env.local",
            "/path/to/credentials.json",
            "/home/user/.ssh/id_rsa",
            "/home/user/.aws/credentials",
            "/path/to/key.pem",
        ]
        for path in sensitive_paths:
            assert redactor.is_sensitive_path(path), f"{path} should be sensitive"

        non_sensitive_paths = [
            "/home/user/code/main.py",
            "/var/log/app.log",
            "/tmp/test.txt",
        ]
        for path in non_sensitive_paths:
            assert not redactor.is_sensitive_path(path), f"{path} should not be sensitive"


class TestDictRedaction:
    """Tests for dictionary redaction."""

    def test_redact_sensitive_keys(self):
        """Test redacting values for sensitive keys."""
        redactor = Redactor()

        # Keys must match the env patterns like *_PASSWORD, *_TOKEN, *_KEY, etc.
        data = {
            "username": "admin",
            "DB_PASSWORD": "secret123",
            "API_TOKEN": "token456",
            "GITHUB_KEY": "key789",
            "data": "normal value",
        }
        result = redactor.redact_dict(data)

        assert result["username"] == "admin"
        assert result["DB_PASSWORD"] == REDACTED_PLACEHOLDER
        assert result["API_TOKEN"] == REDACTED_PLACEHOLDER
        assert result["GITHUB_KEY"] == REDACTED_PLACEHOLDER
        assert result["data"] == "normal value"

    def test_redact_nested_dict(self):
        """Test redacting nested dictionaries."""
        redactor = Redactor()

        data = {
            "config": {
                "api_token": "secret",
                "endpoint": "https://api.example.com",
            },
            "normal": "value",
        }
        result = redactor.redact_dict(data)

        assert result["config"]["api_token"] == REDACTED_PLACEHOLDER
        assert result["config"]["endpoint"] == "https://api.example.com"

    def test_redact_list_in_dict(self):
        """Test redacting lists within dictionaries."""
        redactor = Redactor()

        data = {
            "tokens": ["secret1", "secret2"],
            "items": [{"api_key": "abc"}, {"name": "test"}],
        }
        result = redactor.redact_dict(data)

        assert result["items"][0]["api_key"] == REDACTED_PLACEHOLDER
        assert result["items"][1]["name"] == "test"


class TestCommandRedaction:
    """Tests for shell command redaction."""

    def test_redact_env_assignment(self):
        """Test redacting environment variable assignments."""
        redactor = Redactor()

        commands = [
            "API_TOKEN=secret123 python app.py",
            "export GITHUB_TOKEN=ghp_abc123",
            'PASSWORD="mysecret" ./run.sh',
        ]
        for cmd in commands:
            result = redactor.redact_command(cmd)
            assert "secret" not in result.lower() or REDACTED_PLACEHOLDER in result

    def test_redact_password_flag(self):
        """Test redacting --password flags."""
        redactor = Redactor()

        result = redactor.redact_command("mysql --password=secret123 -u admin")
        assert "secret123" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_redact_token_flag(self):
        """Test redacting --token flags."""
        redactor = Redactor()

        result = redactor.redact_command("gh auth login --token ghp_abc123")
        assert "ghp_abc123" not in result

    def test_preserve_safe_commands(self):
        """Test that safe commands are preserved."""
        redactor = Redactor()

        safe_commands = [
            "ls -la",
            "git status",
            "python test.py",
            "npm install",
        ]
        for cmd in safe_commands:
            result = redactor.redact_command(cmd)
            assert result == cmd


class TestRedactorConfig:
    """Tests for redactor configuration."""

    def test_custom_placeholder(self):
        """Test using a custom placeholder."""
        config = RedactorConfig(placeholder="[HIDDEN]")
        redactor = Redactor(config)

        result = redactor.redact_text("password='secret'")
        assert "[HIDDEN]" in result
        assert REDACTED_PLACEHOLDER not in result

    def test_disable_path_redaction(self):
        """Test disabling path redaction."""
        config = RedactorConfig(redact_sensitive_paths=False)
        redactor = Redactor(config)

        assert not redactor.is_sensitive_path("/home/user/.env")

    def test_custom_patterns(self):
        """Test adding custom patterns."""
        config = RedactorConfig(
            value_patterns=["custom-token-[a-z0-9]+"],
        )
        redactor = Redactor(config)

        result = redactor.redact_text("Using custom-token-abc123")
        assert "custom-token-abc123" not in result


class TestModuleFunctions:
    """Tests for module-level convenience functions."""

    def test_redact_text_function(self):
        """Test the redact_text convenience function."""
        result = redact_text("password='secret'")
        assert REDACTED_PLACEHOLDER in result

    def test_redact_dict_function(self):
        """Test the redact_dict convenience function."""
        result = redact_dict({"api_key": "secret"})
        assert result["api_key"] == REDACTED_PLACEHOLDER

    def test_redact_command_function(self):
        """Test the redact_command convenience function."""
        result = redact_command("API_KEY=secret ./run.sh")
        assert REDACTED_PLACEHOLDER in result
