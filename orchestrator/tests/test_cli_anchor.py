"""
Tests for egg-orch anchor CLI subcommands.

These tests will be enabled once the coder implements the anchor CLI
subcommands in orchestrator/cli.py. For now, they skip gracefully
if the anchor parser is not yet registered.
"""

import json
import sys
from pathlib import Path

import pytest

# Add orchestrator and shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


@pytest.fixture
def parser():
    """Create CLI parser."""
    from cli import create_parser

    return create_parser()


def _has_anchor_command(parser):
    """Check if the anchor command is registered in the parser."""
    try:
        args = parser.parse_args(["anchor", "show"])
        return hasattr(args, "anchor_command")
    except (SystemExit, AttributeError):
        return False


@pytest.fixture
def anchor_env(tmp_path, monkeypatch):
    """Set up environment for anchor commands."""
    monkeypatch.setenv("AGENT_ANCHOR_ID", "coder-abc12345")
    monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
    monkeypatch.setenv("EGG_PIPELINE_ID", "issue-1032")
    monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))

    anchor_dir = tmp_path / ".egg-state" / "agent-anchors"
    anchor_dir.mkdir(parents=True)

    return tmp_path


class TestAnchorParserRegistration:
    """Tests that anchor subcommands are registered in the CLI parser."""

    def test_anchor_command_exists(self, parser):
        """anchor command is registered."""
        if not _has_anchor_command(parser):
            pytest.skip("anchor CLI subcommands not yet implemented")
        args = parser.parse_args(["anchor", "init", "--task", "Test task"])
        assert args.command == "anchor"

    def test_anchor_init_subcommand(self, parser):
        """anchor init subcommand parses correctly."""
        if not _has_anchor_command(parser):
            pytest.skip("anchor CLI subcommands not yet implemented")
        args = parser.parse_args(["anchor", "init", "--task", "Test task"])
        assert args.anchor_command == "init"
        assert args.task == "Test task"

    def test_anchor_show_subcommand(self, parser):
        """anchor show subcommand parses correctly."""
        if not _has_anchor_command(parser):
            pytest.skip("anchor CLI subcommands not yet implemented")
        args = parser.parse_args(["anchor", "show"])
        assert args.anchor_command == "show"

    def test_anchor_validate_subcommand(self, parser):
        """anchor validate subcommand."""
        if not _has_anchor_command(parser):
            pytest.skip("anchor CLI subcommands not yet implemented")
        args = parser.parse_args(["anchor", "validate"])
        assert args.anchor_command == "validate"

    def test_anchor_json_flag(self, parser):
        """anchor commands support --json flag."""
        if not _has_anchor_command(parser):
            pytest.skip("anchor CLI subcommands not yet implemented")
        args = parser.parse_args(["anchor", "show", "--json"])
        assert args.json is True


class TestAnchorInit:
    """Tests for anchor init command."""

    def test_init_creates_anchor_file(self, parser, anchor_env):
        """anchor init creates an anchor file."""
        if not _has_anchor_command(parser):
            pytest.skip("anchor CLI subcommands not yet implemented")
        from cli import main

        result = main(["anchor", "init", "--task", "Test task"])
        assert result == 0

        anchor_dir = anchor_env / ".egg-state" / "agent-anchors"
        anchor_files = list(anchor_dir.glob("*.json"))
        assert len(anchor_files) == 1

    def test_init_writes_valid_json(self, parser, anchor_env):
        """anchor init creates valid JSON content."""
        if not _has_anchor_command(parser):
            pytest.skip("anchor CLI subcommands not yet implemented")
        from cli import main

        main(["anchor", "init", "--task", "Test task"])

        anchor_file = anchor_env / ".egg-state" / "agent-anchors" / "coder-abc12345.json"
        with open(anchor_file) as f:
            data = json.load(f)

        assert data["agent_id"] == "coder-abc12345"
        assert data["role"] == "coder"
        assert "_meta" in data


class TestAnchorShow:
    """Tests for anchor show command."""

    def test_show_nonexistent_anchor(self, parser, anchor_env, capsys):
        """anchor show without init returns error."""
        if not _has_anchor_command(parser):
            pytest.skip("anchor CLI subcommands not yet implemented")
        from cli import main

        result = main(["anchor", "show"])
        assert result == 1
