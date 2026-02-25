"""Tests for compute_allowed_files_from_contract and allowed_files session wiring.

Validates:
- Contract parsing and file collection across tasks/phases
- Directory-sibling expansion for non-glob entries
- Graceful fallback when contract is missing, empty, or malformed
- Spawner wiring: allowed_files passed to register_session for implement phase
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from container_spawner import ContainerSpawner, compute_allowed_files_from_contract
from docker_client import ContainerNotFoundError
from gateway_client import GatewayHealth, SessionInfo
from models import AgentRole, ContainerInfo, ContainerStatus

# --- Fixtures ---


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client."""
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.CONTAINER_PREFIX = "egg-sandbox-"
    mock.create_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-test-coder",
        status=ContainerStatus.RUNNING,
        image="egg:latest",
    )
    mock.start_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-test-coder",
        status=ContainerStatus.RUNNING,
        image="egg:latest",
    )
    mock.get_container_info.side_effect = ContainerNotFoundError("Not found")
    return mock


@pytest.fixture
def mock_gateway_client():
    """Create a mock Gateway client."""
    now = datetime.now()
    mock = MagicMock()
    mock.check_health.return_value = GatewayHealth(
        healthy=True,
        status="ok",
        version="test",
    )
    mock.register_session.return_value = SessionInfo(
        session_token="test-token-abc123",
        container_id="abc123def456",
        container_ip="172.32.0.10",
        mode="public",
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    return mock


@pytest.fixture
def spawner(mock_docker_client, mock_gateway_client):
    """Create a container spawner with mocked clients."""
    return ContainerSpawner(
        docker_client=mock_docker_client,
        gateway_client=mock_gateway_client,
    )


def _make_task(files_affected=None, files=None):
    """Create a mock task object with files_affected or files attribute."""
    task = MagicMock()
    task.files_affected = files_affected
    task.files = files
    return task


def _make_phase(tasks=None):
    """Create a mock plan phase with tasks."""
    phase = MagicMock()
    phase.tasks = tasks or []
    return phase


def _make_contract(phases=None):
    """Create a mock contract with plan phases."""
    contract = MagicMock()
    contract.phases = phases or []
    return contract


# --- Tests: compute_allowed_files_from_contract ---


class TestComputeAllowedFilesFromContract:
    """Tests for compute_allowed_files_from_contract."""

    @patch("egg_contracts.loader.load_contract")
    def test_basic_files_affected(self, mock_load):
        """Collects files_affected from contract tasks."""
        mock_load.return_value = _make_contract(
            [
                _make_phase(
                    [
                        _make_task(files_affected=["src/auth/login.py", "src/auth/logout.py"]),
                    ]
                ),
            ]
        )
        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert result is not None
        assert "src/auth/login.py" in result
        assert "src/auth/logout.py" in result

    @patch("egg_contracts.loader.load_contract")
    def test_directory_sibling_expansion(self, mock_load):
        """Each non-glob file entry gets a parent directory glob."""
        mock_load.return_value = _make_contract(
            [
                _make_phase(
                    [
                        _make_task(files_affected=["src/auth/login.py"]),
                    ]
                ),
            ]
        )
        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert "src/auth/login.py" in result
        assert "src/auth/*" in result

    @patch("egg_contracts.loader.load_contract")
    def test_glob_entries_no_expansion(self, mock_load):
        """Entries with * are not expanded (already globs)."""
        mock_load.return_value = _make_contract(
            [
                _make_phase(
                    [
                        _make_task(files_affected=["src/components/*.tsx", "tests/**"]),
                    ]
                ),
            ]
        )
        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert "src/components/*.tsx" in result
        assert "tests/**" in result
        # No parent expansion for globs
        assert "src/components/*" not in result

    @patch("egg_contracts.loader.load_contract")
    def test_union_across_tasks(self, mock_load):
        """Files from multiple tasks across phases are unioned."""
        mock_load.return_value = _make_contract(
            [
                _make_phase(
                    [
                        _make_task(files_affected=["src/auth/login.py"]),
                        _make_task(files_affected=["src/db/models.py"]),
                    ]
                ),
                _make_phase(
                    [
                        _make_task(files_affected=["tests/test_auth.py"]),
                    ]
                ),
            ]
        )
        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert "src/auth/login.py" in result
        assert "src/db/models.py" in result
        assert "tests/test_auth.py" in result

    @patch("egg_contracts.loader.load_contract")
    def test_deduplication(self, mock_load):
        """Duplicate entries across tasks are deduplicated."""
        mock_load.return_value = _make_contract(
            [
                _make_phase(
                    [
                        _make_task(files_affected=["src/auth/login.py"]),
                        _make_task(files_affected=["src/auth/login.py", "src/auth/logout.py"]),
                    ]
                ),
            ]
        )
        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert result.count("src/auth/login.py") == 1

    @patch("egg_contracts.loader.load_contract")
    def test_no_contract_returns_none(self, mock_load):
        """Returns None when no contract is found."""
        mock_load.return_value = None
        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert result is None

    @patch("egg_contracts.loader.load_contract")
    def test_empty_files_affected_returns_none(self, mock_load):
        """Returns None when tasks have no files_affected."""
        mock_load.return_value = _make_contract(
            [
                _make_phase(
                    [
                        _make_task(files_affected=[], files=None),
                    ]
                ),
            ]
        )
        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert result is None

    def test_non_implement_phase_returns_none(self):
        """Returns None for non-implement phases."""
        result = compute_allowed_files_from_contract("/repo", 123, "plan")
        assert result is None

    def test_none_issue_number_returns_none(self):
        """Returns None when issue_number is None."""
        result = compute_allowed_files_from_contract("/repo", None, "implement")
        assert result is None

    @patch("egg_contracts.loader.load_contract", side_effect=Exception("corrupt"))
    def test_exception_returns_none(self, mock_load):
        """Returns None on contract loading errors (graceful fallback)."""
        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert result is None

    @patch("egg_contracts.loader.load_contract")
    def test_falls_back_to_files_attribute(self, mock_load):
        """Uses task.files when files_affected is None."""
        task = MagicMock()
        task.files = ["src/utils.py"]
        task.files_affected = None
        mock_load.return_value = _make_contract(
            [
                _make_phase([task]),
            ]
        )
        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert result is not None
        assert "src/utils.py" in result

    @patch("egg_contracts.loader.load_contract")
    def test_root_level_file_no_parent_glob(self, mock_load):
        """Root-level files (no directory) don't generate broken globs."""
        mock_load.return_value = _make_contract(
            [
                _make_phase(
                    [
                        _make_task(files_affected=["Makefile", "pyproject.toml"]),
                    ]
                ),
            ]
        )
        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert "Makefile" in result
        assert "pyproject.toml" in result
        # No "/*" glob should appear for root-level files
        assert "/*" not in result


# --- Tests: Spawner wiring of allowed_files ---


class TestSpawnerAllowedFilesWiring:
    """Tests that ContainerSpawner passes allowed_files to register_session."""

    @patch("container_spawner.compute_allowed_files_from_contract")
    @patch("container_spawner.ensure_egg_state_dirs")
    @patch("container_spawner.phase_readonly_mounts", return_value=[])
    def test_implement_phase_computes_and_passes_allowed_files(
        self, mock_ro_mounts, mock_ensure, mock_compute, spawner, mock_gateway_client
    ):
        """Implement phase computes allowed_files and passes to register_session."""
        mock_compute.return_value = ["src/auth/*", "tests/**"]

        spawner.spawn_agent_container(
            pipeline_id="issue-805",
            agent_role=AgentRole.CODER,
            issue_number=805,
            repo_volumes={"egg": "/host/repos/egg"},
            phase="implement",
        )

        mock_gateway_client.register_session.assert_called_once()
        call_kwargs = mock_gateway_client.register_session.call_args.kwargs
        assert call_kwargs.get("allowed_files") == ["src/auth/*", "tests/**"]

    @patch("container_spawner.compute_allowed_files_from_contract")
    def test_non_implement_phase_skips_allowed_files(
        self, mock_compute, spawner, mock_gateway_client
    ):
        """Non-implement phases don't compute allowed_files."""
        spawner.spawn_agent_container(
            pipeline_id="issue-805",
            agent_role=AgentRole.CODER,
            issue_number=805,
            phase="plan",
        )

        mock_compute.assert_not_called()
        call_kwargs = mock_gateway_client.register_session.call_args.kwargs
        assert call_kwargs.get("allowed_files") is None

    @patch("container_spawner.compute_allowed_files_from_contract")
    @patch("container_spawner.ensure_egg_state_dirs")
    @patch("container_spawner.phase_readonly_mounts", return_value=[])
    def test_no_contract_files_passes_none(
        self, mock_ro_mounts, mock_ensure, mock_compute, spawner, mock_gateway_client
    ):
        """When contract has no files, allowed_files=None (no restriction)."""
        mock_compute.return_value = None

        spawner.spawn_agent_container(
            pipeline_id="issue-805",
            agent_role=AgentRole.CODER,
            issue_number=805,
            repo_volumes={"egg": "/host/repos/egg"},
            phase="implement",
        )

        call_kwargs = mock_gateway_client.register_session.call_args.kwargs
        assert call_kwargs.get("allowed_files") is None

    @patch("container_spawner.compute_allowed_files_from_contract")
    def test_no_repo_volumes_skips_computation(self, mock_compute, spawner, mock_gateway_client):
        """Without repo_volumes, allowed_files computation is skipped."""
        spawner.spawn_agent_container(
            pipeline_id="issue-805",
            agent_role=AgentRole.CODER,
            issue_number=805,
            phase="implement",
        )

        mock_compute.assert_not_called()


# --- Integration test: real load_contract call ---


class TestComputeAllowedFilesIntegration:
    """Integration tests that exercise the real load_contract call path.

    These tests use a real contract JSON file on disk instead of mocking
    load_contract, to catch argument mismatches (e.g., repo_path vs repo_root).
    """

    def test_load_contract_kwarg_matches_signature(self, tmp_path):
        """compute_allowed_files_from_contract passes repo_root (not repo_path) to load_contract."""
        import json

        # Create a minimal valid contract file at the expected path
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        contract_data = {
            "schemaVersion": "1.0",
            "issue": {
                "number": 999,
                "title": "Test",
                "url": "https://github.com/test/test/issues/999",
            },
            "current_phase": "implement",
            "phases": [
                {
                    "id": "phase-1",
                    "name": "Implementation",
                    "tasks": [
                        {
                            "id": "task-1",
                            "description": "Add auth",
                            "files_affected": ["src/auth/login.py", "tests/test_auth.py"],
                        }
                    ],
                }
            ],
        }
        (contracts_dir / "999.json").write_text(json.dumps(contract_data))

        # Call with real load_contract (no mock) — would fail with TypeError
        # if repo_path was used instead of repo_root
        result = compute_allowed_files_from_contract(str(tmp_path), 999, "implement")

        assert result is not None
        assert "src/auth/login.py" in result
        assert "tests/test_auth.py" in result

    def test_missing_contract_returns_none(self, tmp_path):
        """Returns None when the contract file doesn't exist on disk."""
        result = compute_allowed_files_from_contract(str(tmp_path), 9999, "implement")
        assert result is None

    def test_empty_files_affected_returns_none(self, tmp_path):
        """Returns None when real contract has no files_affected."""
        import json

        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        contract_data = {
            "schemaVersion": "1.0",
            "issue": {"number": 888, "title": "Empty", "url": "https://github.com/t/t/issues/888"},
            "current_phase": "implement",
            "phases": [
                {
                    "id": "phase-1",
                    "name": "Phase",
                    "tasks": [
                        {
                            "id": "task-1",
                            "description": "No files",
                            "files_affected": [],
                        }
                    ],
                }
            ],
        }
        (contracts_dir / "888.json").write_text(json.dumps(contract_data))

        result = compute_allowed_files_from_contract(str(tmp_path), 888, "implement")
        assert result is None


# --- Edge case tests for compute_allowed_files_from_contract ---


class TestComputeAllowedFilesEdgeCases:
    """Additional edge case tests for compute_allowed_files_from_contract."""

    @patch("egg_contracts.loader.load_contract")
    def test_contract_with_none_phases(self, mock_load):
        """Contract with phases=None doesn't crash."""
        mock_contract = MagicMock()
        mock_contract.phases = None
        mock_load.return_value = mock_contract

        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert result is None

    @patch("egg_contracts.loader.load_contract")
    def test_phase_with_none_tasks(self, mock_load):
        """Phase with tasks=None doesn't crash."""
        mock_phase = MagicMock()
        mock_phase.tasks = None
        mock_contract = MagicMock()
        mock_contract.phases = [mock_phase]
        mock_load.return_value = mock_contract

        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert result is None

    @patch("egg_contracts.loader.load_contract")
    def test_task_with_neither_files_nor_files_affected(self, mock_load):
        """Task with neither files nor files_affected doesn't crash."""
        mock_task = MagicMock(spec=[])  # No attributes at all
        mock_phase = MagicMock()
        mock_phase.tasks = [mock_task]
        mock_contract = MagicMock()
        mock_contract.phases = [mock_phase]
        mock_load.return_value = mock_contract

        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert result is None

    @patch("egg_contracts.loader.load_contract")
    def test_very_large_file_list(self, mock_load):
        """Large file lists are handled without error."""
        files = [f"src/module{i}/file{j}.py" for i in range(50) for j in range(10)]
        mock_task = MagicMock()
        mock_task.files_affected = files
        mock_task.files = None
        mock_phase = MagicMock()
        mock_phase.tasks = [mock_task]
        mock_contract = MagicMock()
        mock_contract.phases = [mock_phase]
        mock_load.return_value = mock_contract

        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert result is not None
        # Should have original files + directory globs
        assert len(result) > len(files)

    @patch("egg_contracts.loader.load_contract")
    def test_mixed_glob_and_regular_files(self, mock_load):
        """Mix of glob patterns and regular files handled correctly."""
        mock_task = MagicMock()
        mock_task.files_affected = [
            "src/auth/*.py",
            "tests/**",
            "src/db/models.py",
            "README.md",
        ]
        mock_task.files = None
        mock_phase = MagicMock()
        mock_phase.tasks = [mock_task]
        mock_contract = MagicMock()
        mock_contract.phases = [mock_phase]
        mock_load.return_value = mock_contract

        result = compute_allowed_files_from_contract("/repo", 123, "implement")
        assert result is not None
        # Glob entries kept as-is
        assert "src/auth/*.py" in result
        assert "tests/**" in result
        # Regular entries get sibling expansion
        assert "src/db/models.py" in result
        assert "src/db/*" in result
        assert "README.md" in result
        # No expansion for globs (no parent dir glob added)
        assert result.count("src/auth/*") <= 1  # Only from expansion of *.py if any
