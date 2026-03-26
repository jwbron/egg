"""Tests for settings.json generation in setup_claude().

Verifies that private-mode containers get disallowedTools for WebFetch/WebSearch,
while public-mode containers do not.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Add sandbox/ to sys.path so entrypoint is importable
_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from entrypoint import setup_claude


@pytest.fixture()
def mock_config(tmp_path):
    """Create a minimal Config-like object with a temp claude_dir."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    config = MagicMock()
    config.claude_dir = claude_dir
    config.user_home = tmp_path
    config.runtime_uid = os.getuid()
    config.runtime_gid = os.getgid()
    config.repo_path = tmp_path / "repo"
    config.repo_path.mkdir()
    return config


@pytest.fixture()
def mock_logger():
    return MagicMock()


def _read_settings(config: Any) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((config.claude_dir / "settings.json").read_text())
    return result


class TestDisallowedToolsPrivateMode:
    """WebFetch/WebSearch should be disallowed only in private mode."""

    @patch.dict(os.environ, {"EGG_PRIVATE_MODE": "true"})
    @patch("entrypoint.shutil.which", return_value="/usr/bin/claude")
    def test_private_mode_disallows_web_tools(self, _which, mock_config, mock_logger):
        setup_claude(mock_config, mock_logger)
        settings = _read_settings(mock_config)
        assert settings["disallowedTools"] == ["WebFetch", "WebSearch"]

    @patch.dict(os.environ, {"EGG_PRIVATE_MODE": "1"})
    @patch("entrypoint.shutil.which", return_value="/usr/bin/claude")
    def test_private_mode_1_disallows_web_tools(self, _which, mock_config, mock_logger):
        setup_claude(mock_config, mock_logger)
        settings = _read_settings(mock_config)
        assert settings["disallowedTools"] == ["WebFetch", "WebSearch"]

    @patch.dict(os.environ, {"EGG_PRIVATE_MODE": "false"})
    @patch("entrypoint.shutil.which", return_value="/usr/bin/claude")
    def test_public_mode_no_disallowed_tools(self, _which, mock_config, mock_logger):
        setup_claude(mock_config, mock_logger)
        settings = _read_settings(mock_config)
        assert "disallowedTools" not in settings

    @patch.dict(os.environ, {"EGG_PRIVATE_MODE": "0"})
    @patch("entrypoint.shutil.which", return_value="/usr/bin/claude")
    def test_public_mode_0_no_disallowed_tools(self, _which, mock_config, mock_logger):
        """EGG_PRIVATE_MODE=0 is set by sandbox_template.py for public mode."""
        setup_claude(mock_config, mock_logger)
        settings = _read_settings(mock_config)
        assert "disallowedTools" not in settings

    @patch.dict(os.environ, {"EGG_PRIVATE_MODE": ""}, clear=False)
    @patch("entrypoint.shutil.which", return_value="/usr/bin/claude")
    def test_unset_env_no_disallowed_tools(self, _which, mock_config, mock_logger):
        setup_claude(mock_config, mock_logger)
        settings = _read_settings(mock_config)
        assert "disallowedTools" not in settings
