"""Tests for settings.json generation in setup_claude().

Verifies that private-mode containers get disallowedTools for WebFetch/WebSearch,
while public-mode containers do not.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Add sandbox/ to sys.path so entrypoint is importable
_sandbox_path = str(Path(__file__).parent.parent.parent / "sandbox")
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
    """WebFetch/WebSearch should be disallowed only in private mode.

    Originally lived in ``sandbox/tests/test_entrypoint_settings.py``
    alongside ``TestPostToolUseHook`` coverage for the #2804 truncation
    hook. The hook was removed in this PR after review showed
    PostToolUse ``decision: block`` does not suppress oversized tool
    responses (#2810). The private-mode coverage moved here because the
    production code at ``sandbox/entrypoint.py`` (the
    ``EGG_PRIVATE_MODE`` branch that writes
    ``settings["disallowedTools"]``) is still live.

    Defense-in-depth note: ``run_agent_async`` also sets
    ``ClaudeAgentOptions.disallowed_tools`` via a CLI flag (see
    ``shared/egg_agent/client.py``); that is a different code path
    covered by ``tests/shared/egg_agent/test_client.py``. This file
    covers the ``settings.json`` write layer.
    """

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


class TestWebToolsHook:
    """PreToolUse hook denies built-in WebSearch/WebFetch on the LiteLLM→non-Anthropic
    public-mode path.

    The hook is installed only when ANTHROPIC_CUSTOM_MODEL_OPTION is set (LiteLLM
    routing to Qwen/DeepSeek) AND the container is not in private mode — private
    mode already disallows the web tools, and the in-sandbox DDG MCP server cannot
    reach duckduckgo.com through the locked-down proxy. The DDG server itself is
    registered programmatically in ``run_agent_async`` via
    ``ClaudeAgentOptions.mcp_servers`` (covered by
    ``tests/shared/egg_agent/test_client.py``), never via settings.json —
    ``mcpServers`` is not a valid settings.json key.

    See https://github.com/jwbron/egg/issues/2856.
    """

    _LITELLM_ENV = {
        "ANTHROPIC_CUSTOM_MODEL_OPTION": "qwen3-coder-30b[1m]",
        "EGG_PRIVATE_MODE": "false",
    }

    @patch.dict(os.environ, _LITELLM_ENV)
    @patch("entrypoint.shutil.which", return_value="/usr/bin/claude")
    def test_hook_installed_on_litellm_public_path(self, _which, mock_config, mock_logger):
        setup_claude(mock_config, mock_logger)
        settings = _read_settings(mock_config)
        hooks = settings.get("hooks", {}).get("PreToolUse", [])
        matchers = [h["matcher"] for h in hooks]
        assert "WebSearch" in matchers
        assert "WebFetch" in matchers

    @patch.dict(os.environ, _LITELLM_ENV)
    @patch("entrypoint.shutil.which", return_value="/usr/bin/claude")
    def test_hook_command_path(self, _which, mock_config, mock_logger):
        setup_claude(mock_config, mock_logger)
        settings = _read_settings(mock_config)
        hooks = settings["hooks"]["PreToolUse"]
        web_search_hook = next(h for h in hooks if h["matcher"] == "WebSearch")
        assert web_search_hook["hooks"][0]["type"] == "command"
        assert web_search_hook["hooks"][0]["command"].endswith("block-builtin-web-tools.sh")

    @patch("entrypoint.shutil.which", return_value="/usr/bin/claude")
    def test_hook_not_installed_on_first_party_route(self, _which, mock_config, mock_logger):
        # No ANTHROPIC_CUSTOM_MODEL_OPTION → first-party Claude → built-in tools are
        # live, so the redirect hook must not be installed.
        with patch.dict(os.environ, {"EGG_PRIVATE_MODE": "false"}, clear=False):
            os.environ.pop("ANTHROPIC_CUSTOM_MODEL_OPTION", None)
            setup_claude(mock_config, mock_logger)
        settings = _read_settings(mock_config)
        assert "hooks" not in settings

    @patch.dict(
        os.environ,
        {"ANTHROPIC_CUSTOM_MODEL_OPTION": "qwen3-coder-30b[1m]", "EGG_PRIVATE_MODE": "true"},
    )
    @patch("entrypoint.shutil.which", return_value="/usr/bin/claude")
    def test_hook_not_installed_in_private_mode(self, _which, mock_config, mock_logger):
        # Private mode disallows the web tools outright and the DDG server is
        # unreachable through the locked-down proxy, so the hook must not install
        # even on the LiteLLM path.
        setup_claude(mock_config, mock_logger)
        settings = _read_settings(mock_config)
        assert "hooks" not in settings
        assert settings["disallowedTools"] == ["WebFetch", "WebSearch"]

    @patch.dict(os.environ, _LITELLM_ENV)
    @patch("entrypoint.shutil.which", return_value="/usr/bin/claude")
    def test_mcpServers_never_written_to_settings(self, _which, mock_config, mock_logger):
        # mcpServers is not a valid settings.json key — server *definitions* live in
        # ClaudeAgentOptions.mcp_servers, not here. Guard against re-introducing the
        # dead key (the original #2857 defect).
        setup_claude(mock_config, mock_logger)
        settings = _read_settings(mock_config)
        assert "mcpServers" not in settings


class TestBlockBuiltinWebToolsScript:
    """Shell script behavior: allow (no output) on first-party route, deny on LiteLLM route.

    Asserts the real PreToolUse consumer contract: the decision is carried in
    ``hookSpecificOutput.permissionDecision``, which is what Claude Code honors
    (the deprecated top-level ``decision`` field is not read for PreToolUse, and
    only ``permissionDecision:"deny"`` overrides ``bypassPermissions``).
    """

    @pytest.fixture()
    def script_path(self):
        return (
            Path(__file__).parent.parent.parent
            / "sandbox"
            / "scripts"
            / "block-builtin-web-tools.sh"
        )

    def test_script_exists(self, script_path):
        assert script_path.exists()
        assert os.access(script_path, os.X_OK)

    def test_first_party_route_allows(self, script_path):
        result = subprocess.run(
            [str(script_path)],
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_CUSTOM_MODEL_OPTION"},
        )
        assert result.returncode == 0
        # No output → the tool call proceeds normally (allow by fallthrough).
        assert result.stdout.strip() == ""

    def test_litellm_route_denies(self, script_path):
        env = os.environ.copy()
        env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = "qwen3-coder-30b[1m]"
        result = subprocess.run(
            [str(script_path)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        hook_out = payload["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "deny"
        # No deprecated top-level decision field that PreToolUse would ignore.
        assert "decision" not in payload
        reason = hook_out["permissionDecisionReason"]
        assert "mcp__ddg__search" in reason
        assert "mcp__ddg__fetch_content" in reason
