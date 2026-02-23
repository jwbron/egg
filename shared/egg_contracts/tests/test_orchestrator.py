"""
Tests for load_agent_output / save_agent_output identifier-prefixed paths.

Verifies the backward-compat fallback behavior:
1. Prefixed path exists → used
2. Only old global path exists → fallback reads it
3. Neither exists → empty dict returned
4. Both exist → prefixed path takes priority
"""

import json
from pathlib import Path

import pytest

from egg_contracts.agent_roles import AgentRole
from egg_contracts.orchestrator import (
    collect_handoff_data,
    load_agent_output,
    save_agent_output,
)


class TestLoadAgentOutputIdentifier:
    """Tests for load_agent_output with identifier parameter."""

    def test_prefixed_path_used(self, tmp_path: Path):
        """When prefixed path exists, it is returned."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-coder-output.json").write_text(
            json.dumps({"key": "prefixed"})
        )

        result = load_agent_output(tmp_path, AgentRole.CODER, identifier=871)
        assert result == {"key": "prefixed"}

    def test_fallback_to_global_path(self, tmp_path: Path):
        """When only old global path exists, fallback reads it."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "coder-output.json").write_text(
            json.dumps({"key": "global"})
        )

        result = load_agent_output(tmp_path, AgentRole.CODER, identifier=871)
        assert result == {"key": "global"}

    def test_neither_exists_returns_empty(self, tmp_path: Path):
        """When neither file exists, returns empty dict."""
        result = load_agent_output(tmp_path, AgentRole.CODER, identifier=871)
        assert result == {}

    def test_prefixed_takes_priority(self, tmp_path: Path):
        """When both prefixed and global exist, prefixed takes priority."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "coder-output.json").write_text(
            json.dumps({"key": "global"})
        )
        (outputs_dir / "871-coder-output.json").write_text(
            json.dumps({"key": "prefixed"})
        )

        result = load_agent_output(tmp_path, AgentRole.CODER, identifier=871)
        assert result == {"key": "prefixed"}

    def test_no_identifier_uses_global(self, tmp_path: Path):
        """When identifier is None, uses global path directly."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "coder-output.json").write_text(
            json.dumps({"key": "global"})
        )

        result = load_agent_output(tmp_path, AgentRole.CODER)
        assert result == {"key": "global"}

    def test_string_identifier(self, tmp_path: Path):
        """String identifiers (e.g. local pipeline IDs) work."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "local-abc123-coder-output.json").write_text(
            json.dumps({"key": "local"})
        )

        result = load_agent_output(tmp_path, AgentRole.CODER, identifier="local-abc123")
        assert result == {"key": "local"}


class TestSaveAgentOutputIdentifier:
    """Tests for save_agent_output with identifier parameter."""

    def test_save_with_identifier(self, tmp_path: Path):
        """Saves to prefixed path when identifier provided."""
        path = save_agent_output(
            tmp_path, AgentRole.CODER, {"key": "value"}, identifier=871
        )
        assert path.name == "871-coder-output.json"
        assert json.loads(path.read_text()) == {"key": "value"}

    def test_save_without_identifier(self, tmp_path: Path):
        """Saves to global path when identifier is None."""
        path = save_agent_output(tmp_path, AgentRole.CODER, {"key": "value"})
        assert path.name == "coder-output.json"
        assert json.loads(path.read_text()) == {"key": "value"}


class TestCollectHandoffDataIdentifier:
    """Tests for collect_handoff_data with identifier parameter."""

    def test_collects_from_prefixed_paths(self, tmp_path: Path):
        """collect_handoff_data forwards identifier to load_agent_output."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-coder-output.json").write_text(
            json.dumps({"files": ["main.py"]})
        )

        # TESTER depends on CODER
        result = collect_handoff_data(tmp_path, AgentRole.TESTER, identifier=871)
        assert "coder" in result
        assert result["coder"]["files"] == ["main.py"]
