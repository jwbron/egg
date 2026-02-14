"""
Unit tests for DevserverManager.

Tests compose extraction, override generation, network creation,
service mapping resolution, teardown idempotency, and timeout handling.
All Docker SDK calls are mocked.
"""

import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from devserver import (
    ComposeExtractionError,
    DevserverManager,
    DevserverStatusValue,
    ServiceStatus,
    StackLifecycleError,
)
from egg_contracts.deployment import DeploymentConfig, ServiceMapping


def _make_deployment_config(**kwargs) -> DeploymentConfig:
    """Create a minimal DeploymentConfig for testing."""
    defaults = {
        "services": [
            {"source_dir": "services/api/", "service_name": "api"},
        ],
        "health_endpoints": {"api": "/_api/ping"},
        "startup_timeout_seconds": 30,
    }
    defaults.update(kwargs)
    return DeploymentConfig(**defaults)


def _make_manager(
    tmp_path: Path,
    pipeline_id: str = "issue-645",
    docker_client: any = None,
) -> DevserverManager:
    """Create a DevserverManager with temp paths."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir(exist_ok=True)
    return DevserverManager(
        pipeline_id=pipeline_id,
        repo_path=repo_path,
        worktree_path=worktree_path,
        docker_client=docker_client,
    )


# ── Compose Extraction Tests ────────────────────────────────────────


class TestComposeExtraction:
    """Tests for _extract_compose_config method."""

    def test_extracts_from_head(self, tmp_path):
        manager = _make_manager(tmp_path)
        compose_yaml = "services:\n  api:\n    image: api:latest\n"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=compose_yaml,
            )
            result = manager._extract_compose_config("docker-compose.yml")

        assert result == compose_yaml
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "git" in call_args
        assert "show" in call_args
        assert "HEAD:docker-compose.yml" in call_args

    def test_raises_on_missing_file(self, tmp_path):
        manager = _make_manager(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=128,
                stderr="fatal: path 'docker-compose.yml' does not exist in 'HEAD'",
            )
            with pytest.raises(ComposeExtractionError, match="does not exist"):
                manager._extract_compose_config("docker-compose.yml")

    def test_raises_on_empty_content(self, tmp_path):
        manager = _make_manager(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="  \n  ")
            with pytest.raises(ComposeExtractionError, match="empty"):
                manager._extract_compose_config("docker-compose.yml")

    def test_raises_on_invalid_yaml(self, tmp_path):
        manager = _make_manager(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="{{not valid yaml]]]")
            with pytest.raises(ComposeExtractionError, match="not valid YAML"):
                manager._extract_compose_config("docker-compose.yml")

    def test_raises_on_timeout(self, tmp_path):
        manager = _make_manager(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)
            with pytest.raises(ComposeExtractionError, match="Timed out"):
                manager._extract_compose_config("docker-compose.yml")

    def test_working_tree_not_used(self, tmp_path):
        """Verify extraction reads from HEAD, not working tree."""
        manager = _make_manager(tmp_path)

        committed_content = "services:\n  api:\n    image: api:v1\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=committed_content)
            result = manager._extract_compose_config("docker-compose.yml")

        # The result should be the committed content, not anything from the working tree
        assert "api:v1" in result
        # Verify git show HEAD: was used
        call_args = mock_run.call_args[0][0]
        assert "HEAD:docker-compose.yml" in call_args


# ── Service Mapping Tests ────────────────────────────────────────────


class TestServiceMapping:
    """Tests for _resolve_affected_services method."""

    def test_maps_changed_files_to_services(self, tmp_path):
        manager = _make_manager(tmp_path)
        mappings = [
            ServiceMapping(source_dir="services/api/", service_name="api"),
            ServiceMapping(source_dir="services/worker/", service_name="worker"),
        ]
        changed = ["services/api/views.py", "services/api/models.py"]

        result = manager._resolve_affected_services(changed, mappings)
        assert len(result) == 1
        assert result[0].service_name == "api"

    def test_maps_multiple_services(self, tmp_path):
        manager = _make_manager(tmp_path)
        mappings = [
            ServiceMapping(source_dir="services/api/", service_name="api"),
            ServiceMapping(source_dir="services/worker/", service_name="worker"),
        ]
        changed = [
            "services/api/views.py",
            "services/worker/tasks.py",
        ]

        result = manager._resolve_affected_services(changed, mappings)
        assert len(result) == 2

    def test_ignores_unmapped_files(self, tmp_path):
        manager = _make_manager(tmp_path)
        mappings = [
            ServiceMapping(source_dir="services/api/", service_name="api"),
        ]
        changed = ["README.md", "docs/guide.md"]

        result = manager._resolve_affected_services(changed, mappings)
        assert len(result) == 0

    def test_empty_changed_files(self, tmp_path):
        manager = _make_manager(tmp_path)
        mappings = [
            ServiceMapping(source_dir="services/api/", service_name="api"),
        ]

        result = manager._resolve_affected_services([], mappings)
        assert len(result) == 0

    def test_no_duplicate_services(self, tmp_path):
        manager = _make_manager(tmp_path)
        mappings = [
            ServiceMapping(source_dir="services/api/", service_name="api"),
        ]
        changed = [
            "services/api/views.py",
            "services/api/models.py",
            "services/api/urls.py",
        ]

        result = manager._resolve_affected_services(changed, mappings)
        assert len(result) == 1


# ── Compose Override Generation Tests ────────────────────────────────


class TestComposeOverrideGeneration:
    """Tests for _generate_compose_override method."""

    def test_generates_valid_yaml(self, tmp_path):
        manager = _make_manager(tmp_path)
        affected = [
            ServiceMapping(source_dir="services/api/", service_name="api"),
        ]

        override_yaml = manager._generate_compose_override(
            affected, tmp_path / "worktree", ["api", "db"]
        )

        data = yaml.safe_load(override_yaml)
        assert "services" in data
        assert "api" in data["services"]
        assert "db" in data["services"]

    def test_ro_volume_mounts(self, tmp_path):
        manager = _make_manager(tmp_path)
        worktree = tmp_path / "worktree"
        affected = [
            ServiceMapping(
                source_dir="services/api/",
                service_name="api",
                container_mount_path="/app",
            ),
        ]

        override_yaml = manager._generate_compose_override(affected, worktree, ["api"])

        data = yaml.safe_load(override_yaml)
        volumes = data["services"]["api"].get("volumes", [])
        assert len(volumes) == 1
        assert ":ro" in volumes[0]
        assert str(worktree / "services/api/") in volumes[0]

    def test_resource_limits_on_all_services(self, tmp_path):
        manager = _make_manager(tmp_path)

        override_yaml = manager._generate_compose_override(
            [], tmp_path / "worktree", ["api", "db", "cache"]
        )

        data = yaml.safe_load(override_yaml)
        for svc_name in ["api", "db", "cache"]:
            svc = data["services"][svc_name]
            assert "deploy" in svc
            limits = svc["deploy"]["resources"]["limits"]
            assert "cpus" in limits
            assert "memory" in limits
            assert "pids" in limits

    def test_security_options(self, tmp_path):
        manager = _make_manager(tmp_path)

        override_yaml = manager._generate_compose_override([], tmp_path / "worktree", ["api"])

        data = yaml.safe_load(override_yaml)
        svc = data["services"]["api"]
        assert "cap_drop" in svc
        assert "ALL" in svc["cap_drop"]
        assert svc["privileged"] is False

    def test_network_attached(self, tmp_path):
        manager = _make_manager(tmp_path)

        override_yaml = manager._generate_compose_override([], tmp_path / "worktree", ["api"])

        data = yaml.safe_load(override_yaml)
        networks = data["services"]["api"]["networks"]
        assert manager.network_name in networks

    def test_unaffected_services_no_volumes(self, tmp_path):
        manager = _make_manager(tmp_path)
        affected = [
            ServiceMapping(source_dir="services/api/", service_name="api"),
        ]

        override_yaml = manager._generate_compose_override(
            affected, tmp_path / "worktree", ["api", "db"]
        )

        data = yaml.safe_load(override_yaml)
        assert "volumes" not in data["services"]["db"]


# ── Network Tests ────────────────────────────────────────────────────


class TestNetworkManagement:
    """Tests for network creation and teardown."""

    def test_network_name_includes_pipeline_id(self, tmp_path):
        manager = _make_manager(tmp_path, pipeline_id="issue-123")
        assert "issue-123" in manager.network_name
        assert manager.network_name.startswith("egg-check-")

    @patch("devserver.docker")
    def test_create_network_internal(self, mock_docker_module, tmp_path):
        mock_client = MagicMock()
        mock_docker_module.errors.NotFound = Exception
        manager = _make_manager(tmp_path, docker_client=mock_client)

        mock_client.networks.get.side_effect = Exception("not found")
        mock_network = MagicMock()
        mock_network.id = "net-123456789012"
        mock_client.networks.create.return_value = mock_network

        network_id = manager._create_check_network()

        assert network_id == "net-123456789012"
        mock_client.networks.create.assert_called_once()
        call_kwargs = mock_client.networks.create.call_args[1]
        assert call_kwargs["internal"] is True
        assert call_kwargs["driver"] == "bridge"
        # Docker auto-assigns subnets to avoid collisions with concurrent pipelines
        assert "ipam" not in call_kwargs

    @patch("devserver.docker")
    def test_remove_network(self, mock_docker_module, tmp_path):
        mock_client = MagicMock()
        mock_docker_module.errors.NotFound = Exception
        manager = _make_manager(tmp_path, docker_client=mock_client)
        manager._network_id = "net-123"

        mock_network = MagicMock()
        mock_network.containers = []
        mock_client.networks.get.return_value = mock_network

        manager._remove_check_network()

        mock_network.remove.assert_called_once()
        assert manager._network_id == ""


# ── DevserverStatus Tests ────────────────────────────────────────────


class TestDevserverStatus:
    """Tests for DevserverStatus dataclass."""

    def test_to_dict(self):
        from devserver import DevserverStatus

        status = DevserverStatus(
            status=DevserverStatusValue.HEALTHY,
            services={
                "api": ServiceStatus(name="api", healthy=True, ip="172.34.0.5", port=8080),
            },
            network_id="net-abc",
        )

        d = status.to_dict()
        assert d["status"] == "healthy"
        assert d["services"]["api"]["healthy"] is True
        assert d["services"]["api"]["ip"] == "172.34.0.5"
        assert d["network_id"] == "net-abc"


# ── Teardown Idempotency Tests ───────────────────────────────────────


class TestTeardown:
    """Tests for teardown idempotency."""

    def test_teardown_when_not_started(self, tmp_path):
        manager = _make_manager(tmp_path)
        # Should not raise
        manager.teardown()
        assert manager.status.status == DevserverStatusValue.STOPPED

    @patch("devserver.docker")
    def test_double_teardown_no_error(self, mock_docker_module, tmp_path):
        mock_docker_module.errors.NotFound = Exception
        manager = _make_manager(tmp_path, docker_client=MagicMock())

        manager.teardown()
        manager.teardown()

        assert manager.status.status == DevserverStatusValue.STOPPED

    @patch("devserver.docker")
    @patch("subprocess.run")
    def test_teardown_cleans_temp_dir(self, mock_run, mock_docker_module, tmp_path):
        mock_docker_module.errors.NotFound = Exception
        manager = _make_manager(tmp_path, docker_client=MagicMock())

        # Simulate a started state with temp dir
        temp_dir = tmp_path / "temp-compose"
        temp_dir.mkdir()
        (temp_dir / "docker-compose.yml").write_text("services: {}")
        (temp_dir / "docker-compose.override.yml").write_text("services: {}")
        manager._temp_dir = temp_dir
        manager._started = True

        manager.teardown()

        assert not temp_dir.exists()
        assert manager._temp_dir is None


# ── Credential Check Tests ───────────────────────────────────────────


class TestCredentialCheck:
    """Tests for pre-flight credential checking."""

    def test_detects_suspicious_env_vars(self, tmp_path):
        manager = _make_manager(tmp_path)
        compose = textwrap.dedent("""\
            services:
              api:
                image: api:latest
                environment:
                  DEBUG: "true"
                  AWS_SECRET_ACCESS_KEY: "xxx"
        """)

        warnings = manager._check_suspicious_env_vars_in_compose(compose)
        assert len(warnings) == 1
        assert "AWS_SECRET_ACCESS_KEY" in warnings[0]

    def test_no_warnings_for_clean_config(self, tmp_path):
        manager = _make_manager(tmp_path)
        compose = textwrap.dedent("""\
            services:
              api:
                image: api:latest
                environment:
                  DEBUG: "true"
                  PORT: "8080"
        """)

        warnings = manager._check_suspicious_env_vars_in_compose(compose)
        assert len(warnings) == 0

    def test_handles_list_format_env(self, tmp_path):
        manager = _make_manager(tmp_path)
        compose = textwrap.dedent("""\
            services:
              api:
                image: api:latest
                environment:
                  - DEBUG=true
                  - AWS_ACCESS_KEY_ID=xxx
        """)

        warnings = manager._check_suspicious_env_vars_in_compose(compose)
        assert len(warnings) == 1


# ── Get Compose Service Names Tests ──────────────────────────────────


class TestGetComposeServiceNames:
    """Tests for _get_compose_service_names method."""

    def test_extracts_service_names(self, tmp_path):
        manager = _make_manager(tmp_path)
        content = "services:\n  api:\n    image: api\n  worker:\n    image: worker\n"
        names = manager._get_compose_service_names(content)
        assert set(names) == {"api", "worker"}

    def test_returns_empty_for_invalid_yaml(self, tmp_path):
        manager = _make_manager(tmp_path)
        names = manager._get_compose_service_names("{{invalid}}")
        assert names == []

    def test_returns_empty_for_no_services(self, tmp_path):
        manager = _make_manager(tmp_path)
        names = manager._get_compose_service_names("version: '3'\n")
        assert names == []


# ── Start Method Tests ───────────────────────────────────────────────


class TestStart:
    """Tests for the start() method."""

    def test_idempotent_when_already_started(self, tmp_path):
        manager = _make_manager(tmp_path)
        manager._started = True
        manager._status.status = DevserverStatusValue.HEALTHY

        config = _make_deployment_config()
        status = manager.start(config)

        assert status.status == DevserverStatusValue.HEALTHY

    @patch("devserver.docker")
    @patch("subprocess.run")
    def test_raises_on_no_services(self, mock_run, mock_docker, tmp_path):
        manager = _make_manager(tmp_path, docker_client=MagicMock())
        config = _make_deployment_config()

        # Return a compose file with no services
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="version: '3'\n",
        )

        with pytest.raises(StackLifecycleError, match="No services"):
            manager.start(config, changed_files=[])
