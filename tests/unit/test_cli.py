"""Unit tests for CLI module."""

import sys
from unittest.mock import patch

from cli.main import main


def test_cli_help(capsys):
    """Test that CLI shows help when called without arguments."""
    # Mock sys.argv to simulate no arguments
    with patch.object(sys, "argv", ["egg"]):
        result = main()
        assert result == 0


def test_cli_imports():
    """Test that CLI modules can be imported."""
    import cli
    import cli.commands

    assert cli is not None
    assert cli.commands is not None
