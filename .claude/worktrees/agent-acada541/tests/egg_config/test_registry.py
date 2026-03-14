"""
Tests for egg_config.registry module.
"""

from typing import Any

import pytest
from egg_config.base import (
    BaseConfig,
    HealthCheckResult,
    ValidationResult,
)
from egg_config.registry import (
    AggregateHealthResult,
    AggregateValidationResult,
    ConfigRegistry,
    get_registry,
    reset_registry,
)


class MockConfig(BaseConfig):
    """A mock config for testing."""

    def __init__(
        self,
        name: str = "mock",
        valid: bool = True,
        healthy: bool = True,
        latency: float | None = 50.0,
    ):
        self._name = name
        self._valid = valid
        self._healthy = healthy
        self._latency = latency

    @property
    def service_name(self) -> str:
        return self._name

    def validate(self) -> ValidationResult:
        if self._valid:
            return ValidationResult.valid()
        return ValidationResult.invalid(errors=[f"{self._name} validation failed"])

    def health_check(self, timeout: float = 5.0) -> HealthCheckResult:
        return HealthCheckResult(
            healthy=self._healthy,
            service_name=self._name,
            message="OK" if self._healthy else "Failed",
            latency_ms=self._latency if self._healthy else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"name": self._name, "token": "****"}

    @classmethod
    def from_env(cls) -> "MockConfig":
        return cls()


@pytest.fixture(autouse=True)
def clean_registry():
    """Reset the registry before and after each test."""
    reset_registry()
    yield
    reset_registry()


class TestConfigRegistry:
    """Tests for ConfigRegistry class."""

    def test_singleton_pattern(self):
        """Test that ConfigRegistry is a singleton."""
        reg1 = ConfigRegistry()
        reg2 = ConfigRegistry()
        assert reg1 is reg2

    def test_register_config(self):
        """Test registering a config."""
        registry = get_registry()
        config = MockConfig(name="test")

        registry.register(config)

        assert "test" in registry.configs
        assert registry.get("test") is config

    def test_register_with_custom_name(self):
        """Test registering with a custom name."""
        registry = get_registry()
        config = MockConfig(name="original")

        registry.register(config, name="custom")

        assert "custom" in registry.configs
        assert "original" not in registry.configs

    def test_unregister_config(self):
        """Test unregistering a config."""
        registry = get_registry()
        config = MockConfig(name="test")
        registry.register(config)

        registry.unregister("test")

        assert "test" not in registry.configs
        assert registry.get("test") is None

    def test_unregister_nonexistent(self):
        """Test unregistering a config that doesn't exist (no error)."""
        registry = get_registry()
        registry.unregister("nonexistent")  # Should not raise

    def test_get_nonexistent(self):
        """Test getting a config that doesn't exist."""
        registry = get_registry()
        assert registry.get("nonexistent") is None

    def test_configs_property_returns_copy(self):
        """Test that configs property returns a copy."""
        registry = get_registry()
        config = MockConfig(name="test")
        registry.register(config)

        configs_copy = registry.configs
        configs_copy["hacked"] = MockConfig()

        assert "hacked" not in registry.configs

    def test_clear(self):
        """Test clearing all configs."""
        registry = get_registry()
        registry.register(MockConfig(name="one"))
        registry.register(MockConfig(name="two"))

        registry.clear()

        assert len(registry.configs) == 0


class TestValidateAll:
    """Tests for validate_all method."""

    def test_all_valid(self):
        """Test validate_all when all configs are valid."""
        registry = get_registry()
        registry.register(MockConfig(name="one", valid=True))
        registry.register(MockConfig(name="two", valid=True))

        result = registry.validate_all()

        assert result.all_valid
        assert "one" in result.results
        assert "two" in result.results
        assert result.results["one"].is_valid
        assert result.results["two"].is_valid

    def test_some_invalid(self):
        """Test validate_all when some configs are invalid."""
        registry = get_registry()
        registry.register(MockConfig(name="valid", valid=True))
        registry.register(MockConfig(name="invalid", valid=False))

        result = registry.validate_all()

        assert not result.all_valid
        assert result.results["valid"].is_valid
        assert not result.results["invalid"].is_valid

    def test_all_invalid(self):
        """Test validate_all when all configs are invalid."""
        registry = get_registry()
        registry.register(MockConfig(name="one", valid=False))
        registry.register(MockConfig(name="two", valid=False))

        result = registry.validate_all()

        assert not result.all_valid

    def test_empty_registry(self):
        """Test validate_all with no registered configs."""
        registry = get_registry()
        result = registry.validate_all()

        assert result.all_valid
        assert len(result.results) == 0


class TestHealthCheckAll:
    """Tests for health_check_all method."""

    def test_all_healthy(self):
        """Test health_check_all when all services are healthy."""
        registry = get_registry()
        registry.register(MockConfig(name="one", healthy=True))
        registry.register(MockConfig(name="two", healthy=True))

        result = registry.health_check_all()

        assert result.status == "healthy"
        assert result.services["one"].healthy
        assert result.services["two"].healthy

    def test_some_unhealthy(self):
        """Test health_check_all when some services are unhealthy."""
        registry = get_registry()
        registry.register(MockConfig(name="healthy", healthy=True))
        registry.register(MockConfig(name="unhealthy", healthy=False))

        result = registry.health_check_all()

        assert result.status == "degraded"
        assert result.services["healthy"].healthy
        assert not result.services["unhealthy"].healthy

    def test_all_unhealthy(self):
        """Test health_check_all when all services are unhealthy."""
        registry = get_registry()
        registry.register(MockConfig(name="one", healthy=False))
        registry.register(MockConfig(name="two", healthy=False))

        result = registry.health_check_all()

        assert result.status == "unhealthy"

    def test_empty_registry(self):
        """Test health_check_all with no registered configs."""
        registry = get_registry()
        result = registry.health_check_all()

        assert result.status == "healthy"
        assert len(result.services) == 0

    def test_includes_latency(self):
        """Test that health check results include latency."""
        registry = get_registry()
        registry.register(MockConfig(name="fast", healthy=True, latency=10.0))
        registry.register(MockConfig(name="slow", healthy=True, latency=500.0))

        result = registry.health_check_all()

        assert result.services["fast"].latency_ms == 10.0
        assert result.services["slow"].latency_ms == 500.0

    def test_respects_timeout_parameter(self):
        """Test that timeout parameter is passed to health checks."""
        # This is more of a smoke test since MockConfig doesn't use timeout
        registry = get_registry()
        registry.register(MockConfig(name="test", healthy=True))

        result = registry.health_check_all(timeout=1.0)

        assert result.status == "healthy"


class TestDryRunMode:
    """Tests for dry-run mode."""

    def test_dry_run_default_off(self):
        """Test dry-run mode is off by default."""
        registry = get_registry()
        assert not registry.dry_run

    def test_set_dry_run(self):
        """Test enabling dry-run mode."""
        registry = get_registry()

        registry.set_dry_run(True)
        assert registry.dry_run

        registry.set_dry_run(False)
        assert not registry.dry_run


class TestToDict:
    """Tests for to_dict method."""

    def test_returns_all_configs(self):
        """Test to_dict returns all configs."""
        registry = get_registry()
        registry.register(MockConfig(name="one"))
        registry.register(MockConfig(name="two"))

        result = registry.to_dict()

        assert "one" in result
        assert "two" in result

    def test_configs_have_masked_secrets(self):
        """Test that returned configs have masked secrets."""
        registry = get_registry()
        registry.register(MockConfig(name="test"))

        result = registry.to_dict()

        assert result["test"]["token"] == "****"


class TestAggregateValidationResult:
    """Tests for AggregateValidationResult."""

    def test_to_dict(self):
        """Test to_dict serialization."""
        result = AggregateValidationResult(
            all_valid=False,
            results={
                "service1": ValidationResult.valid(),
                "service2": ValidationResult.invalid(errors=["Error"]),
            },
        )

        d = result.to_dict()

        assert d["all_valid"] is False
        assert d["results"]["service1"]["status"] == "valid"
        assert d["results"]["service2"]["status"] == "invalid"
        assert d["results"]["service2"]["errors"] == ["Error"]


class TestAggregateHealthResult:
    """Tests for AggregateHealthResult."""

    def test_to_dict(self):
        """Test to_dict serialization."""
        result = AggregateHealthResult(
            status="degraded",
            services={
                "healthy": HealthCheckResult(True, "healthy", "OK", 50.0),
                "unhealthy": HealthCheckResult(False, "unhealthy", "Down"),
            },
        )

        d = result.to_dict()

        assert d["status"] == "degraded"
        assert d["services"]["healthy"]["healthy"] is True
        assert d["services"]["healthy"]["latency_ms"] == 50.0
        assert d["services"]["unhealthy"]["healthy"] is False
        assert "timestamp" in d

    def test_timestamp_is_set(self):
        """Test that timestamp is automatically set."""
        result = AggregateHealthResult(status="healthy", services={})
        assert result.timestamp is not None


class TestGetRegistry:
    """Tests for get_registry function."""

    def test_returns_singleton(self):
        """Test get_registry returns the same instance."""
        reg1 = get_registry()
        reg2 = get_registry()
        assert reg1 is reg2

    def test_reset_registry_clears_state(self):
        """Test reset_registry clears all state."""
        registry = get_registry()
        registry.register(MockConfig(name="test"))
        registry.set_dry_run(True)

        reset_registry()

        new_registry = get_registry()
        assert len(new_registry.configs) == 0
        assert not new_registry.dry_run
