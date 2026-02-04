"""
Tests for jib_config.base module.
"""

from typing import Any

import pytest
from egg_config.base import (
    BaseConfig,
    ConfigStatus,
    HealthCheckResult,
    ValidationResult,
)


class TestConfigStatus:
    """Tests for ConfigStatus enum."""

    def test_values(self):
        """Test enum values exist."""
        assert ConfigStatus.VALID.value == "valid"
        assert ConfigStatus.INVALID.value == "invalid"
        assert ConfigStatus.DEGRADED.value == "degraded"


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_valid_result(self):
        """Test creating a valid result."""
        result = ValidationResult.valid()
        assert result.is_valid
        assert result.is_usable
        assert result.status == ConfigStatus.VALID
        assert result.errors == []
        assert result.warnings == []

    def test_valid_with_warnings(self):
        """Test valid result with warnings."""
        result = ValidationResult.valid(warnings=["Consider upgrading"])
        assert result.is_valid
        assert result.is_usable
        assert result.warnings == ["Consider upgrading"]

    def test_invalid_result(self):
        """Test creating an invalid result."""
        result = ValidationResult.invalid(errors=["Missing token"])
        assert not result.is_valid
        assert not result.is_usable
        assert result.status == ConfigStatus.INVALID
        assert result.errors == ["Missing token"]

    def test_invalid_with_warnings(self):
        """Test invalid result with both errors and warnings."""
        result = ValidationResult.invalid(
            errors=["Missing token"],
            warnings=["Deprecated option used"],
        )
        assert not result.is_valid
        assert result.errors == ["Missing token"]
        assert result.warnings == ["Deprecated option used"]

    def test_degraded_result(self):
        """Test creating a degraded result."""
        result = ValidationResult.degraded(
            errors=["Optional service unavailable"],
            warnings=["Rate limit approaching"],
        )
        assert not result.is_valid  # Not fully valid
        assert result.is_usable  # But usable
        assert result.status == ConfigStatus.DEGRADED

    def test_is_valid_property(self):
        """Test is_valid property logic."""
        assert ValidationResult(status=ConfigStatus.VALID).is_valid
        assert not ValidationResult(status=ConfigStatus.INVALID).is_valid
        assert not ValidationResult(status=ConfigStatus.DEGRADED).is_valid

    def test_is_usable_property(self):
        """Test is_usable property logic."""
        assert ValidationResult(status=ConfigStatus.VALID).is_usable
        assert not ValidationResult(status=ConfigStatus.INVALID).is_usable
        assert ValidationResult(status=ConfigStatus.DEGRADED).is_usable


class TestHealthCheckResult:
    """Tests for HealthCheckResult dataclass."""

    def test_healthy_result(self):
        """Test creating a healthy result."""
        result = HealthCheckResult(
            healthy=True,
            service_name="slack",
            message="Connected as jib-bot",
            latency_ms=150.5,
        )
        assert result.healthy
        assert result.service_name == "slack"
        assert result.message == "Connected as jib-bot"
        assert result.latency_ms == 150.5

    def test_unhealthy_result(self):
        """Test creating an unhealthy result."""
        result = HealthCheckResult(
            healthy=False,
            service_name="github",
            message="Token expired",
        )
        assert not result.healthy
        assert result.latency_ms is None

    def test_to_dict_with_latency(self):
        """Test to_dict includes latency when present."""
        result = HealthCheckResult(
            healthy=True,
            service_name="api",
            message="OK",
            latency_ms=50.0,
        )
        d = result.to_dict()
        assert d == {
            "healthy": True,
            "message": "OK",
            "latency_ms": 50.0,
        }

    def test_to_dict_without_latency(self):
        """Test to_dict excludes latency when None."""
        result = HealthCheckResult(
            healthy=False,
            service_name="api",
            message="Connection refused",
        )
        d = result.to_dict()
        assert d == {
            "healthy": False,
            "message": "Connection refused",
        }
        assert "latency_ms" not in d


class TestBaseConfig:
    """Tests for BaseConfig abstract base class."""

    def test_cannot_instantiate_directly(self):
        """Test that BaseConfig cannot be instantiated."""
        with pytest.raises(TypeError, match="abstract"):
            BaseConfig()

    def test_service_name_property(self):
        """Test default service_name property strips 'Config' suffix."""

        class SlackConfig(BaseConfig):
            def validate(self):
                return ValidationResult.valid()

            def health_check(self, timeout=5.0):
                return HealthCheckResult(True, "slack", "OK")

            def to_dict(self):
                return {}

            @classmethod
            def from_env(cls):
                return cls()

        config = SlackConfig()
        assert config.service_name == "slack"

    def test_service_name_no_suffix(self):
        """Test service_name when class doesn't end with 'Config'."""

        class MyService(BaseConfig):
            def validate(self):
                return ValidationResult.valid()

            def health_check(self, timeout=5.0):
                return HealthCheckResult(True, "myservice", "OK")

            def to_dict(self):
                return {}

            @classmethod
            def from_env(cls):
                return cls()

        config = MyService()
        assert config.service_name == "myservice"

    def test_subclass_must_implement_validate(self):
        """Test that subclass must implement validate."""

        class IncompleteConfig(BaseConfig):
            def health_check(self, timeout=5.0):
                return HealthCheckResult(True, "test", "OK")

            def to_dict(self):
                return {}

            @classmethod
            def from_env(cls):
                return cls()

        with pytest.raises(TypeError, match="abstract"):
            IncompleteConfig()

    def test_subclass_must_implement_health_check(self):
        """Test that subclass must implement health_check."""

        class IncompleteConfig(BaseConfig):
            def validate(self):
                return ValidationResult.valid()

            def to_dict(self):
                return {}

            @classmethod
            def from_env(cls):
                return cls()

        with pytest.raises(TypeError, match="abstract"):
            IncompleteConfig()

    def test_subclass_must_implement_to_dict(self):
        """Test that subclass must implement to_dict."""

        class IncompleteConfig(BaseConfig):
            def validate(self):
                return ValidationResult.valid()

            def health_check(self, timeout=5.0):
                return HealthCheckResult(True, "test", "OK")

            @classmethod
            def from_env(cls):
                return cls()

        with pytest.raises(TypeError, match="abstract"):
            IncompleteConfig()

    def test_subclass_must_implement_from_env(self):
        """Test that subclass must implement from_env."""

        class IncompleteConfig(BaseConfig):
            def validate(self):
                return ValidationResult.valid()

            def health_check(self, timeout=5.0):
                return HealthCheckResult(True, "test", "OK")

            def to_dict(self):
                return {}

        with pytest.raises(TypeError, match="abstract"):
            IncompleteConfig()


class TestCompleteConfigSubclass:
    """Tests for a complete BaseConfig subclass implementation."""

    def setup_method(self):
        """Create a complete config subclass for testing."""

        class TestConfig(BaseConfig):
            def __init__(self, token: str = "", url: str = ""):
                self.token = token
                self.url = url

            def validate(self) -> ValidationResult:
                errors = []
                if not self.token:
                    errors.append("Token is required")
                if not self.url:
                    errors.append("URL is required")
                if errors:
                    return ValidationResult.invalid(errors)
                return ValidationResult.valid()

            def health_check(self, timeout: float = 5.0) -> HealthCheckResult:
                if not self.token:
                    return HealthCheckResult(False, "test", "No token configured")
                return HealthCheckResult(True, "test", "Connected", latency_ms=10.0)

            def to_dict(self) -> dict[str, Any]:
                return {
                    "token": "****" if self.token else "[EMPTY]",
                    "url": self.url,
                }

            @classmethod
            def from_env(cls) -> "TestConfig":
                import os

                return cls(
                    token=os.environ.get("TEST_TOKEN", ""),
                    url=os.environ.get("TEST_URL", ""),
                )

        self.TestConfig = TestConfig

    def test_valid_config(self):
        """Test a fully configured config validates successfully."""
        config = self.TestConfig(token="secret", url="https://example.com")
        result = config.validate()
        assert result.is_valid
        assert result.errors == []

    def test_invalid_config_missing_token(self):
        """Test config with missing token is invalid."""
        config = self.TestConfig(url="https://example.com")
        result = config.validate()
        assert not result.is_valid
        assert "Token is required" in result.errors

    def test_invalid_config_missing_both(self):
        """Test config with multiple missing fields."""
        config = self.TestConfig()
        result = config.validate()
        assert not result.is_valid
        assert len(result.errors) == 2

    def test_health_check_success(self):
        """Test health check with valid config."""
        config = self.TestConfig(token="secret", url="https://example.com")
        result = config.health_check()
        assert result.healthy
        assert result.latency_ms == 10.0

    def test_health_check_failure(self):
        """Test health check with invalid config."""
        config = self.TestConfig()
        result = config.health_check()
        assert not result.healthy
        assert "No token" in result.message

    def test_to_dict_masks_secrets(self):
        """Test to_dict masks sensitive values."""
        config = self.TestConfig(token="super-secret", url="https://example.com")
        d = config.to_dict()
        assert d["token"] == "****"
        assert d["url"] == "https://example.com"

    def test_to_dict_shows_empty(self):
        """Test to_dict shows [EMPTY] for missing values."""
        config = self.TestConfig()
        d = config.to_dict()
        assert d["token"] == "[EMPTY]"

    def test_from_env(self, monkeypatch):
        """Test from_env loads from environment."""
        monkeypatch.setenv("TEST_TOKEN", "env-token")
        monkeypatch.setenv("TEST_URL", "https://env.example.com")

        config = self.TestConfig.from_env()
        assert config.token == "env-token"
        assert config.url == "https://env.example.com"

    def test_from_env_missing_vars(self, monkeypatch):
        """Test from_env with missing environment variables."""
        monkeypatch.delenv("TEST_TOKEN", raising=False)
        monkeypatch.delenv("TEST_URL", raising=False)

        config = self.TestConfig.from_env()
        assert config.token == ""
        assert config.url == ""
