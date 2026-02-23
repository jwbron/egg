"""
Tests for orchestrator/handoffs.py identifier-namespaced wrappers.

These tests verify that the handoffs module correctly forwards the
``identifier`` parameter to the underlying egg_contracts functions,
covering save_agent_output, load_agent_output_data, collect_handoff_data,
and get_handoff_env_var.
"""

import json
from pathlib import Path

import pytest

from handoffs import (
    AgentOutput,
    collect_handoff_data,
    get_handoff_env_var,
    load_agent_output_data,
    save_agent_output,
)
from models import AgentRole


class TestSaveAgentOutputWrapper:
    """Tests for handoffs.save_agent_output with identifier."""

    def test_save_with_identifier_creates_prefixed_file(self, tmp_path: Path):
        """Wrapper saves to {identifier}-{role}-output.json."""
        output = AgentOutput(
            role=AgentRole.CODER,
            commit="abc123",
            files_changed=["file.py"],
            handoff_data={"changed_files": ["file.py"]},
        )

        path = save_agent_output(tmp_path, output, identifier=871)
        assert path.name == "871-coder-output.json"
        data = json.loads(path.read_text())
        assert data["role"] == "coder"
        assert data["commit"] == "abc123"

    def test_save_without_identifier_creates_global_file(self, tmp_path: Path):
        """Wrapper saves to {role}-output.json when identifier is None."""
        output = AgentOutput(
            role=AgentRole.TESTER,
            handoff_data={"tests_passed": 10},
        )

        path = save_agent_output(tmp_path, output)
        assert path.name == "tester-output.json"


class TestLoadAgentOutputDataWrapper:
    """Tests for handoffs.load_agent_output_data with identifier."""

    def test_load_with_identifier_reads_prefixed(self, tmp_path: Path):
        """Loads from prefixed file when identifier provided."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-coder-output.json").write_text(
            json.dumps({
                "role": "coder",
                "commit": "abc123",
                "files_changed": ["a.py"],
                "handoff_data": {"files": ["a.py"]},
                "logs": None,
                "metrics": {},
                "timestamp": "2026-01-01T00:00:00",
            })
        )

        result = load_agent_output_data(tmp_path, AgentRole.CODER, identifier=871)
        assert result is not None
        assert result.role == AgentRole.CODER
        assert result.commit == "abc123"

    def test_load_fallback_to_global(self, tmp_path: Path):
        """Falls back to global file when prefixed not found."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "coder-output.json").write_text(
            json.dumps({
                "role": "coder",
                "commit": "def456",
                "files_changed": [],
                "handoff_data": {},
                "logs": None,
                "metrics": {},
                "timestamp": "2026-01-01T00:00:00",
            })
        )

        result = load_agent_output_data(tmp_path, AgentRole.CODER, identifier=999)
        assert result is not None
        assert result.commit == "def456"

    def test_load_returns_none_when_missing(self, tmp_path: Path):
        """Returns None when no output file exists."""
        result = load_agent_output_data(tmp_path, AgentRole.CODER, identifier=871)
        assert result is None


class TestCollectHandoffDataWrapper:
    """Tests for handoffs.collect_handoff_data with identifier."""

    def test_collect_with_identifier(self, tmp_path: Path):
        """Collects handoff data from prefixed files."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-coder-output.json").write_text(
            json.dumps({
                "role": "coder",
                "commit": "abc",
                "files_changed": ["main.py"],
                "handoff_data": {"changed_files": ["main.py"]},
                "logs": None,
                "metrics": {},
                "timestamp": "2026-01-01T00:00:00",
            })
        )

        result = collect_handoff_data(tmp_path, AgentRole.TESTER, identifier=871)
        assert "coder" in result
        assert result["coder"].data == {"changed_files": ["main.py"]}

    def test_collect_without_identifier(self, tmp_path: Path):
        """Collects from global files when identifier is None."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "coder-output.json").write_text(
            json.dumps({
                "role": "coder",
                "commit": "abc",
                "files_changed": [],
                "handoff_data": {"changed_files": ["old.py"]},
                "logs": None,
                "metrics": {},
                "timestamp": "2026-01-01T00:00:00",
            })
        )

        result = collect_handoff_data(tmp_path, AgentRole.TESTER)
        assert "coder" in result
        assert result["coder"].data == {"changed_files": ["old.py"]}

    def test_collect_empty_handoff_data_excluded(self, tmp_path: Path):
        """Agent outputs with empty handoff_data are excluded."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-coder-output.json").write_text(
            json.dumps({
                "role": "coder",
                "commit": "abc",
                "files_changed": [],
                "handoff_data": {},
                "logs": None,
                "metrics": {},
                "timestamp": "2026-01-01T00:00:00",
            })
        )

        result = collect_handoff_data(tmp_path, AgentRole.TESTER, identifier=871)
        assert "coder" not in result


class TestGetHandoffEnvVarWrapper:
    """Tests for handoffs.get_handoff_env_var with identifier."""

    def test_env_var_with_identifier(self, tmp_path: Path):
        """Returns JSON string with handoff data from prefixed files."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-coder-output.json").write_text(
            json.dumps({
                "role": "coder",
                "commit": "abc",
                "files_changed": ["x.py"],
                "handoff_data": {"key": "value"},
                "logs": None,
                "metrics": {},
                "timestamp": "2026-01-01T00:00:00",
            })
        )

        result = get_handoff_env_var(tmp_path, AgentRole.TESTER, identifier=871)
        parsed = json.loads(result)
        assert "coder" in parsed
        assert parsed["coder"]["key"] == "value"

    def test_env_var_without_identifier(self, tmp_path: Path):
        """Returns JSON string from global files when identifier is None."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "coder-output.json").write_text(
            json.dumps({
                "role": "coder",
                "commit": "abc",
                "files_changed": [],
                "handoff_data": {"legacy": True},
                "logs": None,
                "metrics": {},
                "timestamp": "2026-01-01T00:00:00",
            })
        )

        result = get_handoff_env_var(tmp_path, AgentRole.TESTER)
        parsed = json.loads(result)
        assert "coder" in parsed
        assert parsed["coder"]["legacy"] is True

    def test_env_var_empty_when_no_deps(self, tmp_path: Path):
        """Returns empty JSON object for role with no dependencies."""
        result = get_handoff_env_var(tmp_path, AgentRole.CODER, identifier=871)
        assert json.loads(result) == {}

    def test_round_trip_save_then_env_var(self, tmp_path: Path):
        """Save output, then collect via env var — full round-trip."""
        output = AgentOutput(
            role=AgentRole.CODER,
            commit="abc",
            files_changed=["test.py"],
            handoff_data={"changed_files": ["test.py"], "summary": "Added tests"},
        )
        save_agent_output(tmp_path, output, identifier=42)

        result = get_handoff_env_var(tmp_path, AgentRole.TESTER, identifier=42)
        parsed = json.loads(result)
        assert "coder" in parsed
        assert parsed["coder"]["changed_files"] == ["test.py"]
