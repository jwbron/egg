"""
Tests for egg_config.cli module.
"""

from typing import Any

import pytest
from egg_config.base import (
    BaseConfig,
    HealthCheckResult,
    ValidationResult,
)
from egg_config.cli import cmd_health, cmd_show, cmd_validate, create_parser, main
from egg_config.registry import get_registry, reset_registry


class MockConfig(BaseConfig):
    """A mock config for testing CLI commands."""

    def __init__(
        self,
        name: str = "mock",
        valid: bool = True,
        healthy: bool = True,
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
    ):
        self._name = name
        self._valid = valid
        self._healthy = healthy
        self._errors = errors or []
        self._warnings = warnings or []

    @property
    def service_name(self) -> str:
        return self._name

    def validate(self) -> ValidationResult:
        if self._valid:
            return ValidationResult.valid(warnings=self._warnings)
        return ValidationResult.invalid(errors=self._errors, warnings=self._warnings)

    def health_check(self, timeout: float = 5.0) -> HealthCheckResult:
        return HealthCheckResult(
            healthy=self._healthy,
            service_name=self._name,
            message="Connected" if self._healthy else "Connection failed",
            latency_ms=50.0 if self._healthy else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self._name,
            "token": "****",
            "url": "https://example.com",
        }

    @classmethod
    def from_env(cls) -> "MockConfig":
        return cls()


@pytest.fixture(autouse=True)
def clean_registry():
    """Reset the registry before and after each test."""
    reset_registry()
    yield
    reset_registry()


class TestCreateParser:
    """Tests for argument parser creation."""

    def test_parser_created(self):
        """Test parser is created successfully."""
        parser = create_parser()
        assert parser is not None

    def test_validate_command(self):
        """Test validate command parsing."""
        parser = create_parser()
        args = parser.parse_args(["validate"])
        assert args.command == "validate"

    def test_validate_with_service(self):
        """Test validate --service option."""
        parser = create_parser()
        args = parser.parse_args(["validate", "--service", "slack"])
        assert args.command == "validate"
        assert args.service == "slack"

    def test_validate_with_verbose(self):
        """Test validate --verbose option."""
        parser = create_parser()
        args = parser.parse_args(["validate", "-v"])
        assert args.verbose is True

    def test_health_command(self):
        """Test health command parsing."""
        parser = create_parser()
        args = parser.parse_args(["health"])
        assert args.command == "health"

    def test_health_with_timeout(self):
        """Test health --timeout option."""
        parser = create_parser()
        args = parser.parse_args(["health", "--timeout", "10.0"])
        assert args.timeout == 10.0

    def test_health_with_json(self):
        """Test health --json option."""
        parser = create_parser()
        args = parser.parse_args(["health", "--json"])
        assert args.json is True

    def test_show_command(self):
        """Test show command parsing."""
        parser = create_parser()
        args = parser.parse_args(["show"])
        assert args.command == "show"

    def test_show_with_service(self):
        """Test show --service option."""
        parser = create_parser()
        args = parser.parse_args(["show", "-s", "github"])
        assert args.service == "github"

    def test_watch_command(self):
        """Test watch command parsing."""
        parser = create_parser()
        args = parser.parse_args(["watch"])
        assert args.command == "watch"


class TestCmdValidate:
    """Tests for validate command."""

    def test_no_configs_registered(self, capsys):
        """Test validate with no configs registered."""
        parser = create_parser()
        args = parser.parse_args(["validate"])

        exit_code = cmd_validate(args)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "No configurations registered" in captured.out

    def test_all_valid(self, capsys):
        """Test validate when all configs are valid."""
        registry = get_registry()
        registry.register(MockConfig(name="one", valid=True))
        registry.register(MockConfig(name="two", valid=True))

        parser = create_parser()
        args = parser.parse_args(["validate"])

        exit_code = cmd_validate(args)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "[OK]" in captured.out
        assert "All configurations valid" in captured.out

    def test_some_invalid(self, capsys):
        """Test validate when some configs are invalid."""
        registry = get_registry()
        registry.register(MockConfig(name="valid", valid=True))
        registry.register(MockConfig(name="invalid", valid=False, errors=["Missing token"]))

        parser = create_parser()
        args = parser.parse_args(["validate"])

        exit_code = cmd_validate(args)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "[FAIL]" in captured.out
        assert "Missing token" in captured.out
        assert "Some configurations have errors" in captured.out

    def test_validate_specific_service(self, capsys):
        """Test validate with --service option."""
        registry = get_registry()
        registry.register(MockConfig(name="slack", valid=True))
        registry.register(MockConfig(name="github", valid=False))

        parser = create_parser()
        args = parser.parse_args(["validate", "--service", "slack"])

        exit_code = cmd_validate(args)

        assert exit_code == 0

    def test_validate_unknown_service(self, capsys):
        """Test validate with unknown service name."""
        registry = get_registry()
        registry.register(MockConfig(name="slack", valid=True))

        parser = create_parser()
        args = parser.parse_args(["validate", "--service", "unknown"])

        exit_code = cmd_validate(args)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Unknown service" in captured.out

    def test_verbose_shows_warnings(self, capsys):
        """Test validate --verbose shows warnings."""
        registry = get_registry()
        registry.register(MockConfig(name="test", valid=True, warnings=["Token expires soon"]))

        parser = create_parser()
        args = parser.parse_args(["validate", "--verbose"])

        cmd_validate(args)

        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "Token expires soon" in captured.out


class TestCmdHealth:
    """Tests for health command."""

    def test_no_configs_registered(self, capsys):
        """Test health with no configs registered."""
        parser = create_parser()
        args = parser.parse_args(["health"])

        exit_code = cmd_health(args)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "No configurations registered" in captured.out

    def test_all_healthy(self, capsys):
        """Test health when all services are healthy."""
        registry = get_registry()
        registry.register(MockConfig(name="one", healthy=True))
        registry.register(MockConfig(name="two", healthy=True))

        parser = create_parser()
        args = parser.parse_args(["health"])

        exit_code = cmd_health(args)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "healthy" in captured.out
        assert "[OK]" in captured.out

    def test_some_unhealthy(self, capsys):
        """Test health when some services are unhealthy."""
        registry = get_registry()
        registry.register(MockConfig(name="healthy", healthy=True))
        registry.register(MockConfig(name="unhealthy", healthy=False))

        parser = create_parser()
        args = parser.parse_args(["health"])

        exit_code = cmd_health(args)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "degraded" in captured.out
        assert "[FAIL]" in captured.out

    def test_json_output(self, capsys):
        """Test health --json outputs JSON."""
        registry = get_registry()
        registry.register(MockConfig(name="test", healthy=True))

        parser = create_parser()
        args = parser.parse_args(["health", "--json"])

        cmd_health(args)

        captured = capsys.readouterr()
        assert '"status":' in captured.out
        assert '"services":' in captured.out

    def test_shows_latency(self, capsys):
        """Test health shows latency for healthy services."""
        registry = get_registry()
        registry.register(MockConfig(name="test", healthy=True))

        parser = create_parser()
        args = parser.parse_args(["health"])

        cmd_health(args)

        captured = capsys.readouterr()
        assert "50ms" in captured.out


class TestCmdShow:
    """Tests for show command."""

    def test_no_configs_registered(self, capsys):
        """Test show with no configs registered."""
        parser = create_parser()
        args = parser.parse_args(["show"])

        exit_code = cmd_show(args)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "No configurations registered" in captured.out

    def test_show_all_configs(self, capsys):
        """Test show displays all configs."""
        registry = get_registry()
        registry.register(MockConfig(name="one"))
        registry.register(MockConfig(name="two"))

        parser = create_parser()
        args = parser.parse_args(["show"])

        exit_code = cmd_show(args)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "one" in captured.out
        assert "two" in captured.out
        assert "****" in captured.out  # Masked token

    def test_show_specific_service(self, capsys):
        """Test show --service displays specific config."""
        registry = get_registry()
        registry.register(MockConfig(name="slack"))
        registry.register(MockConfig(name="github"))

        parser = create_parser()
        args = parser.parse_args(["show", "--service", "slack"])

        exit_code = cmd_show(args)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "slack" in captured.out
        assert "github" not in captured.out

    def test_show_unknown_service(self, capsys):
        """Test show with unknown service name."""
        registry = get_registry()
        registry.register(MockConfig(name="slack"))

        parser = create_parser()
        args = parser.parse_args(["show", "--service", "unknown"])

        exit_code = cmd_show(args)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Unknown service" in captured.out


class TestMain:
    """Tests for main entry point."""

    def test_no_command_shows_help(self, capsys):
        """Test running with no command shows help."""
        exit_code = main([])
        assert exit_code == 0

    def test_validate_command(self):
        """Test running validate command."""
        registry = get_registry()
        registry.register(MockConfig(name="test", valid=True))

        exit_code = main(["validate"])
        assert exit_code == 0

    def test_health_command(self):
        """Test running health command."""
        registry = get_registry()
        registry.register(MockConfig(name="test", healthy=True))

        exit_code = main(["health"])
        assert exit_code == 0

    def test_show_command(self):
        """Test running show command."""
        registry = get_registry()
        registry.register(MockConfig(name="test"))

        exit_code = main(["show"])
        assert exit_code == 0

    def test_watch_command(self, capsys):
        """Test running watch command (placeholder)."""
        exit_code = main(["watch"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "not yet implemented" in captured.out
