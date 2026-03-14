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
        (outputs_dir / "871-coder-output.json").write_text(json.dumps({"key": "prefixed"}))

        result = load_agent_output(tmp_path, AgentRole.CODER, identifier=871)
        assert result == {"key": "prefixed"}

    def test_fallback_to_global_path(self, tmp_path: Path):
        """When only old global path exists, fallback reads it."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "coder-output.json").write_text(json.dumps({"key": "global"}))

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
        (outputs_dir / "coder-output.json").write_text(json.dumps({"key": "global"}))
        (outputs_dir / "871-coder-output.json").write_text(json.dumps({"key": "prefixed"}))

        result = load_agent_output(tmp_path, AgentRole.CODER, identifier=871)
        assert result == {"key": "prefixed"}

    def test_no_identifier_uses_global(self, tmp_path: Path):
        """When identifier is None, uses global path directly."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "coder-output.json").write_text(json.dumps({"key": "global"}))

        result = load_agent_output(tmp_path, AgentRole.CODER)
        assert result == {"key": "global"}

    def test_string_identifier(self, tmp_path: Path):
        """String identifiers (e.g. local pipeline IDs) work."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "local-abc123-coder-output.json").write_text(json.dumps({"key": "local"}))

        result = load_agent_output(tmp_path, AgentRole.CODER, identifier="local-abc123")
        assert result == {"key": "local"}


class TestSaveAgentOutputIdentifier:
    """Tests for save_agent_output with identifier parameter."""

    def test_save_with_identifier(self, tmp_path: Path):
        """Saves to prefixed path when identifier provided."""
        path = save_agent_output(tmp_path, AgentRole.CODER, {"key": "value"}, identifier=871)
        assert path.name == "871-coder-output.json"
        assert json.loads(path.read_text()) == {"key": "value"}

    def test_save_without_identifier(self, tmp_path: Path):
        """Saves to global path when identifier is None."""
        path = save_agent_output(tmp_path, AgentRole.CODER, {"key": "value"})
        assert path.name == "coder-output.json"
        assert json.loads(path.read_text()) == {"key": "value"}


class TestLoadAgentOutputErrorHandling:
    """Edge-case and error-handling tests for load_agent_output."""

    def test_corrupted_prefixed_file_returns_empty(self, tmp_path: Path):
        """Corrupted prefixed file returns {} without falling through to global."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-coder-output.json").write_text("NOT VALID JSON{{{")
        (outputs_dir / "coder-output.json").write_text(json.dumps({"key": "global"}))

        # The prefixed file exists but is corrupt — returns empty dict,
        # does NOT fall through to global file.
        result = load_agent_output(tmp_path, AgentRole.CODER, identifier=871)
        assert result == {}

    def test_corrupted_global_file_returns_empty(self, tmp_path: Path):
        """Corrupted global file returns {} when no identifier provided."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "coder-output.json").write_text("{invalid json}")

        result = load_agent_output(tmp_path, AgentRole.CODER)
        assert result == {}

    def test_empty_prefixed_file_returns_empty(self, tmp_path: Path):
        """Empty prefixed file triggers JSONDecodeError, returns {}."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-coder-output.json").write_text("")

        result = load_agent_output(tmp_path, AgentRole.CODER, identifier=871)
        assert result == {}

    def test_integer_zero_identifier(self, tmp_path: Path):
        """Identifier of 0 (falsy int) is still treated as a valid identifier."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "0-coder-output.json").write_text(json.dumps({"key": "zero"}))

        result = load_agent_output(tmp_path, AgentRole.CODER, identifier=0)
        assert result == {"key": "zero"}

    def test_no_identifier_ignores_prefixed_file(self, tmp_path: Path):
        """When identifier=None, prefixed files are ignored entirely."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-coder-output.json").write_text(json.dumps({"key": "prefixed"}))

        # No global file, identifier=None → empty dict (prefixed file ignored)
        result = load_agent_output(tmp_path, AgentRole.CODER, identifier=None)
        assert result == {}

    def test_all_agent_roles_with_identifier(self, tmp_path: Path):
        """All agent roles produce correctly-prefixed filenames."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        for role in [AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER, AgentRole.INTEGRATOR]:
            (outputs_dir / f"42-{role.value}-output.json").write_text(
                json.dumps({"role": role.value})
            )

        for role in [AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER, AgentRole.INTEGRATOR]:
            result = load_agent_output(tmp_path, role, identifier=42)
            assert result == {"role": role.value}


class TestSaveAgentOutputEdgeCases:
    """Edge-case tests for save_agent_output."""

    def test_round_trip_with_identifier(self, tmp_path: Path):
        """save then load with same identifier returns original data."""
        data = {"changed_files": ["a.py", "b.py"], "summary": "test changes"}
        save_agent_output(tmp_path, AgentRole.TESTER, data, identifier=871)
        result = load_agent_output(tmp_path, AgentRole.TESTER, identifier=871)
        assert result == data

    def test_round_trip_without_identifier(self, tmp_path: Path):
        """save then load without identifier returns original data."""
        data = {"changed_files": ["x.py"]}
        save_agent_output(tmp_path, AgentRole.CODER, data)
        result = load_agent_output(tmp_path, AgentRole.CODER)
        assert result == data

    def test_save_creates_directory(self, tmp_path: Path):
        """save_agent_output creates the output directory if missing."""
        path = save_agent_output(tmp_path, AgentRole.CODER, {"k": "v"}, identifier=99)
        assert path.exists()
        assert path.parent.name == "agent-outputs"

    def test_save_overwrites_existing(self, tmp_path: Path):
        """Saving twice with the same identifier overwrites the file."""
        save_agent_output(tmp_path, AgentRole.CODER, {"v": 1}, identifier=10)
        save_agent_output(tmp_path, AgentRole.CODER, {"v": 2}, identifier=10)
        result = load_agent_output(tmp_path, AgentRole.CODER, identifier=10)
        assert result == {"v": 2}

    def test_different_identifiers_coexist(self, tmp_path: Path):
        """Files for different identifiers do not interfere."""
        save_agent_output(tmp_path, AgentRole.CODER, {"id": "a"}, identifier=100)
        save_agent_output(tmp_path, AgentRole.CODER, {"id": "b"}, identifier=200)

        assert load_agent_output(tmp_path, AgentRole.CODER, identifier=100) == {"id": "a"}
        assert load_agent_output(tmp_path, AgentRole.CODER, identifier=200) == {"id": "b"}


class TestCollectHandoffDataIdentifier:
    """Tests for collect_handoff_data with identifier parameter."""

    def test_collects_from_prefixed_paths(self, tmp_path: Path):
        """collect_handoff_data forwards identifier to load_agent_output."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-coder-output.json").write_text(json.dumps({"files": ["main.py"]}))

        # TESTER depends on CODER
        result = collect_handoff_data(tmp_path, AgentRole.TESTER, identifier=871)
        assert "coder" in result
        assert result["coder"]["files"] == ["main.py"]

    def test_collect_fallback_to_global(self, tmp_path: Path):
        """collect_handoff_data falls back to global path when prefixed missing."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "coder-output.json").write_text(json.dumps({"files": ["legacy.py"]}))

        result = collect_handoff_data(tmp_path, AgentRole.TESTER, identifier=999)
        assert "coder" in result
        assert result["coder"]["files"] == ["legacy.py"]

    def test_collect_no_deps_returns_empty(self, tmp_path: Path):
        """Role with no dependencies returns empty dict."""
        # CODER has no dependencies
        result = collect_handoff_data(tmp_path, AgentRole.CODER, identifier=871)
        assert result == {}

    def test_collect_without_identifier(self, tmp_path: Path):
        """collect_handoff_data works without identifier (backward compat)."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "coder-output.json").write_text(json.dumps({"files": ["old.py"]}))

        result = collect_handoff_data(tmp_path, AgentRole.TESTER)
        assert "coder" in result
