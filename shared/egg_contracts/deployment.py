"""
Deployment validation configuration models and loader.

Defines the configuration format that target applications use to opt into
deployment validation during the check phase. Target repos provide a
`.egg/deployment.yml` file describing their docker-compose devserver stack,
service-to-source mappings, health endpoints, and optional smoke tests.
"""

import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

# Pattern for suspicious cloud credential env var names
_CREDENTIAL_PATTERNS = re.compile(
    r"^(AWS_|GCP_|AZURE_|GOOGLE_CLOUD_|"
    r".*_SECRET_KEY$|.*_API_KEY$|.*_ACCESS_KEY$|"
    r".*_TOKEN$|.*_PASSWORD$|.*_CREDENTIALS$)",
    re.IGNORECASE,
)


class ServiceMapping(BaseModel):
    """Maps a source directory to a docker-compose service name.

    Used to determine which devserver services need agent-modified code
    mounted in based on the files the agent changed.
    """

    source_dir: str = Field(
        ...,
        min_length=1,
        description="Source directory relative to repo root (e.g. 'services/api/')",
    )
    service_name: str = Field(
        ...,
        min_length=1,
        description="Docker compose service name (e.g. 'api')",
    )
    container_mount_path: str = Field(
        default="/app",
        description="Path inside the container where source is mounted",
    )

    @field_validator("source_dir")
    @classmethod
    def reject_path_traversal(cls, v: str) -> str:
        """Reject source directories containing path traversal sequences."""
        if v.startswith("/"):
            raise ValueError("source_dir must be relative (no leading '/')")
        if ".." in PurePosixPath(v).parts:
            raise ValueError("source_dir must not contain '..' path traversal")
        return v

    @field_validator("container_mount_path")
    @classmethod
    def validate_mount_path(cls, v: str) -> str:
        """Validate container mount path is absolute."""
        if not v.startswith("/"):
            raise ValueError("container_mount_path must be an absolute path")
        if ".." in PurePosixPath(v).parts:
            raise ValueError("container_mount_path must not contain '..' path traversal")
        return v


class ValidationTest(BaseModel):
    """Defines an HTTP test to run against a devserver service.

    Used for smoke testing beyond basic health checks — verifying
    specific endpoints return expected responses.
    """

    service: str = Field(
        ...,
        min_length=1,
        description="Docker compose service name to test",
    )
    method: str = Field(
        default="GET",
        pattern=r"^(GET|POST|PUT|PATCH|DELETE|HEAD)$",
        description="HTTP method",
    )
    path: str = Field(
        ...,
        min_length=1,
        description="HTTP path to request (e.g. '/_api/ping')",
    )
    expected_status: int = Field(
        default=200,
        ge=100,
        le=599,
        description="Expected HTTP status code",
    )
    expected_body_contains: str | None = Field(
        default=None,
        description="Optional string that must appear in response body",
    )
    description: str = Field(
        default="",
        description="Human-readable description of what this test validates",
    )


class DeploymentConfig(BaseModel):
    """Configuration for deployment validation of a target application.

    Target applications opt into deployment validation by placing this
    configuration at `.egg/deployment.yml` in their repository root.
    The orchestrator reads this config (from committed state) to determine
    how to bring up the devserver stack and what to validate.
    """

    compose_file: str = Field(
        default="docker-compose.yml",
        min_length=1,
        description="Path to docker-compose file relative to repo root",
    )
    services: list[ServiceMapping] = Field(
        ...,
        min_length=1,
        description="Mappings from source directories to docker-compose service names",
    )
    health_endpoints: dict[str, str] = Field(
        default_factory=dict,
        description="Map of service name to health check path (e.g. {'api': '/_api/ping'})",
    )
    startup_timeout_seconds: int = Field(
        default=120,
        ge=10,
        le=600,
        description="Maximum seconds to wait for all services to become healthy",
    )
    validation_tests: list[ValidationTest] = Field(
        default_factory=list,
        description="Optional HTTP smoke tests to run after services are healthy",
    )
    image_registry: str | None = Field(
        default=None,
        description="Optional registry prefix for pre-built images (e.g. 'ghcr.io/org')",
    )

    @field_validator("compose_file")
    @classmethod
    def reject_compose_path_traversal(cls, v: str) -> str:
        """Reject compose file paths containing path traversal."""
        if v.startswith("/"):
            raise ValueError("compose_file must be relative (no leading '/')")
        if ".." in PurePosixPath(v).parts:
            raise ValueError("compose_file must not contain '..' path traversal")
        return v

    @field_validator("health_endpoints")
    @classmethod
    def validate_health_paths(cls, v: dict[str, str]) -> dict[str, str]:
        """Validate that health endpoint paths start with /."""
        for service, path in v.items():
            if not path.startswith("/"):
                raise ValueError(
                    f"Health endpoint path for '{service}' must start with '/': {path}"
                )
        return v


def load_deployment_config(repo_root: Path) -> DeploymentConfig | None:
    """Load deployment validation config from a target repository.

    Looks for `.egg/deployment.yml` (or `.egg/deployment.json`) in the
    repo root. Returns None if the file doesn't exist (target app hasn't
    opted in to deployment validation).

    Args:
        repo_root: Path to the repository root.

    Returns:
        DeploymentConfig if config file exists and is valid, None if missing.

    Raises:
        ValueError: If the config file exists but is malformed or invalid.
    """
    yml_path = repo_root / ".egg" / "deployment.yml"
    json_path = repo_root / ".egg" / "deployment.json"

    config_path: Path | None = None
    if yml_path.exists():
        config_path = yml_path
    elif json_path.exists():
        config_path = json_path
    else:
        return None

    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Failed to read deployment config at {config_path}: {e}") from e

    if not raw.strip():
        raise ValueError(f"Deployment config at {config_path} is empty")

    try:
        data: Any = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in deployment config at {config_path}: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(
            f"Deployment config at {config_path} must be a YAML mapping, got {type(data).__name__}"
        )

    return DeploymentConfig(**data)


def check_suspicious_env_vars(env_vars: dict[str, str]) -> list[str]:
    """Check for environment variables that look like cloud credentials.

    This is a pre-flight safety check — devserver containers should use
    local emulators with hardcoded dev defaults, not real cloud credentials.

    Args:
        env_vars: Dictionary of environment variable names to values.

    Returns:
        List of suspicious environment variable names found.
    """
    return [name for name in env_vars if _CREDENTIAL_PATTERNS.match(name)]
