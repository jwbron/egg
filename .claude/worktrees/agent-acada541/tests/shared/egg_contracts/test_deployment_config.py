"""
Unit tests for DeploymentConfig, ServiceMapping, ValidationTest models
and the deployment config loader.
"""

import json
import textwrap

import pytest
from egg_contracts.deployment import (
    DeploymentConfig,
    ServiceMapping,
    ValidationTest,
    check_suspicious_env_vars,
    load_deployment_config,
)
from pydantic import ValidationError

# ── ServiceMapping Tests ──────────────────────────────────────────────


class TestServiceMapping:
    """Tests for ServiceMapping model."""

    def test_valid_mapping(self):
        m = ServiceMapping(source_dir="services/api/", service_name="api")
        assert m.source_dir == "services/api/"
        assert m.service_name == "api"
        assert m.container_mount_path == "/app"

    def test_custom_mount_path(self):
        m = ServiceMapping(source_dir="src/", service_name="web", container_mount_path="/opt/app")
        assert m.container_mount_path == "/opt/app"

    def test_rejects_path_traversal_in_source_dir(self):
        with pytest.raises(ValidationError, match="path traversal"):
            ServiceMapping(source_dir="../etc/passwd", service_name="bad")

    def test_rejects_dotdot_in_middle(self):
        with pytest.raises(ValidationError, match="path traversal"):
            ServiceMapping(source_dir="services/../secrets/", service_name="bad")

    def test_rejects_absolute_source_dir(self):
        with pytest.raises(ValidationError, match="relative"):
            ServiceMapping(source_dir="/etc/passwd", service_name="bad")

    def test_rejects_empty_source_dir(self):
        with pytest.raises(ValidationError):
            ServiceMapping(source_dir="", service_name="api")

    def test_rejects_empty_service_name(self):
        with pytest.raises(ValidationError):
            ServiceMapping(source_dir="src/", service_name="")

    def test_rejects_relative_mount_path(self):
        with pytest.raises(ValidationError, match="absolute"):
            ServiceMapping(source_dir="src/", service_name="web", container_mount_path="app")

    def test_rejects_traversal_in_mount_path(self):
        with pytest.raises(ValidationError, match="path traversal"):
            ServiceMapping(
                source_dir="src/",
                service_name="web",
                container_mount_path="/app/../etc",
            )


# ── ValidationTest Tests ──────────────────────────────────────────────


class TestValidationTest:
    """Tests for ValidationTest model."""

    def test_defaults(self):
        t = ValidationTest(service="api", path="/_api/ping")
        assert t.method == "GET"
        assert t.expected_status == 200
        assert t.expected_body_contains is None
        assert t.description == ""

    def test_post_method(self):
        t = ValidationTest(service="api", method="POST", path="/submit")
        assert t.method == "POST"

    def test_invalid_method(self):
        with pytest.raises(ValidationError):
            ValidationTest(service="api", method="INVALID", path="/test")

    def test_expected_body_contains(self):
        t = ValidationTest(
            service="api",
            path="/health",
            expected_body_contains="ok",
        )
        assert t.expected_body_contains == "ok"

    def test_custom_status_code(self):
        t = ValidationTest(service="api", path="/redirect", expected_status=302)
        assert t.expected_status == 302

    def test_invalid_status_code_too_low(self):
        with pytest.raises(ValidationError):
            ValidationTest(service="api", path="/test", expected_status=50)

    def test_invalid_status_code_too_high(self):
        with pytest.raises(ValidationError):
            ValidationTest(service="api", path="/test", expected_status=600)

    def test_rejects_empty_path(self):
        with pytest.raises(ValidationError):
            ValidationTest(service="api", path="")


# ── DeploymentConfig Tests ────────────────────────────────────────────


class TestDeploymentConfig:
    """Tests for DeploymentConfig model."""

    def _minimal_config(self, **kwargs):
        defaults = {
            "services": [
                {"source_dir": "src/", "service_name": "api"},
            ],
        }
        defaults.update(kwargs)
        return DeploymentConfig(**defaults)

    def test_minimal_valid_config(self):
        config = self._minimal_config()
        assert config.compose_file == "docker-compose.yml"
        assert config.startup_timeout_seconds == 120
        assert config.validation_tests == []
        assert config.image_registry is None

    def test_custom_compose_file(self):
        config = self._minimal_config(compose_file="docker-compose.dev.yml")
        assert config.compose_file == "docker-compose.dev.yml"

    def test_health_endpoints(self):
        config = self._minimal_config(health_endpoints={"api": "/_api/ping", "worker": "/healthz"})
        assert config.health_endpoints["api"] == "/_api/ping"
        assert config.health_endpoints["worker"] == "/healthz"

    def test_health_endpoint_must_start_with_slash(self):
        with pytest.raises(ValidationError, match="start with '/'"):
            self._minimal_config(health_endpoints={"api": "health"})

    def test_rejects_compose_path_traversal(self):
        with pytest.raises(ValidationError, match="path traversal"):
            self._minimal_config(compose_file="../evil/compose.yml")

    def test_rejects_absolute_compose_path(self):
        with pytest.raises(ValidationError, match="relative"):
            self._minimal_config(compose_file="/etc/compose.yml")

    def test_requires_at_least_one_service(self):
        with pytest.raises(ValidationError):
            DeploymentConfig(services=[])

    def test_startup_timeout_bounds(self):
        config = self._minimal_config(startup_timeout_seconds=10)
        assert config.startup_timeout_seconds == 10

        config = self._minimal_config(startup_timeout_seconds=600)
        assert config.startup_timeout_seconds == 600

        with pytest.raises(ValidationError):
            self._minimal_config(startup_timeout_seconds=5)

        with pytest.raises(ValidationError):
            self._minimal_config(startup_timeout_seconds=700)

    def test_validation_tests(self):
        config = self._minimal_config(
            validation_tests=[
                {"service": "api", "path": "/test", "method": "POST"},
            ]
        )
        assert len(config.validation_tests) == 1
        assert config.validation_tests[0].method == "POST"

    def test_image_registry(self):
        config = self._minimal_config(image_registry="ghcr.io/myorg")
        assert config.image_registry == "ghcr.io/myorg"


# ── Config Loader Tests ──────────────────────────────────────────────


class TestLoadDeploymentConfig:
    """Tests for load_deployment_config function."""

    def test_returns_none_when_no_config(self, tmp_path):
        result = load_deployment_config(tmp_path)
        assert result is None

    def test_loads_yml_config(self, tmp_path):
        egg_dir = tmp_path / ".egg"
        egg_dir.mkdir()
        config_path = egg_dir / "deployment.yml"
        config_path.write_text(
            textwrap.dedent("""\
                compose_file: docker-compose.yml
                services:
                  - source_dir: src/
                    service_name: api
                health_endpoints:
                  api: /health
            """)
        )
        config = load_deployment_config(tmp_path)
        assert config is not None
        assert config.compose_file == "docker-compose.yml"
        assert len(config.services) == 1
        assert config.health_endpoints["api"] == "/health"

    def test_loads_json_config(self, tmp_path):
        egg_dir = tmp_path / ".egg"
        egg_dir.mkdir()
        config_path = egg_dir / "deployment.json"
        config_path.write_text(
            json.dumps(
                {
                    "services": [{"source_dir": "app/", "service_name": "web"}],
                }
            )
        )
        config = load_deployment_config(tmp_path)
        assert config is not None
        assert config.services[0].service_name == "web"

    def test_yml_preferred_over_json(self, tmp_path):
        egg_dir = tmp_path / ".egg"
        egg_dir.mkdir()
        (egg_dir / "deployment.yml").write_text(
            "services:\n  - source_dir: yml/\n    service_name: yml-svc\n"
        )
        (egg_dir / "deployment.json").write_text(
            json.dumps({"services": [{"source_dir": "json/", "service_name": "json-svc"}]})
        )
        config = load_deployment_config(tmp_path)
        assert config is not None
        assert config.services[0].service_name == "yml-svc"

    def test_raises_on_malformed_yaml(self, tmp_path):
        egg_dir = tmp_path / ".egg"
        egg_dir.mkdir()
        (egg_dir / "deployment.yml").write_text("{{invalid yaml]]]")
        with pytest.raises(ValueError, match="Invalid YAML"):
            load_deployment_config(tmp_path)

    def test_raises_on_empty_file(self, tmp_path):
        egg_dir = tmp_path / ".egg"
        egg_dir.mkdir()
        (egg_dir / "deployment.yml").write_text("")
        with pytest.raises(ValueError, match="empty"):
            load_deployment_config(tmp_path)

    def test_raises_on_non_mapping(self, tmp_path):
        egg_dir = tmp_path / ".egg"
        egg_dir.mkdir()
        (egg_dir / "deployment.yml").write_text("- just\n- a\n- list\n")
        with pytest.raises(ValueError, match="mapping"):
            load_deployment_config(tmp_path)

    def test_raises_on_invalid_config(self, tmp_path):
        egg_dir = tmp_path / ".egg"
        egg_dir.mkdir()
        # Missing required 'services' field
        (egg_dir / "deployment.yml").write_text("compose_file: docker-compose.yml\n")
        with pytest.raises(ValidationError):
            load_deployment_config(tmp_path)


# ── Credential Check Tests ───────────────────────────────────────────


class TestCheckSuspiciousEnvVars:
    """Tests for check_suspicious_env_vars function."""

    def test_no_suspicious_vars(self):
        result = check_suspicious_env_vars({"DEBUG": "true", "PORT": "8080"})
        assert result == []

    def test_detects_aws_vars(self):
        result = check_suspicious_env_vars({"AWS_ACCESS_KEY_ID": "xxx"})
        assert "AWS_ACCESS_KEY_ID" in result

    def test_detects_gcp_vars(self):
        result = check_suspicious_env_vars({"GCP_PROJECT": "myproject"})
        assert "GCP_PROJECT" in result

    def test_detects_secret_key_suffix(self):
        result = check_suspicious_env_vars({"DJANGO_SECRET_KEY": "xxx"})
        assert "DJANGO_SECRET_KEY" in result

    def test_detects_api_key_suffix(self):
        result = check_suspicious_env_vars({"STRIPE_API_KEY": "sk_xxx"})
        assert "STRIPE_API_KEY" in result

    def test_detects_token_suffix(self):
        result = check_suspicious_env_vars({"AUTH_TOKEN": "xxx"})
        assert "AUTH_TOKEN" in result

    def test_case_insensitive(self):
        result = check_suspicious_env_vars({"aws_access_key_id": "xxx"})
        assert "aws_access_key_id" in result

    def test_empty_dict(self):
        assert check_suspicious_env_vars({}) == []
