"""Tests for pipeline-mode guard in entrypoint main().

Verifies that the entrypoint refuses to enter interactive mode when
running in orchestrator/pipeline mode with no command provided.

Ref: https://github.com/jwbron/egg/issues/1591
"""

import contextlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add sandbox/ to sys.path so entrypoint is importable
_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from entrypoint import main


@contextlib.contextmanager
def _noop_context(*_args, **_kwargs):
    """No-op context manager to replace timed_phase."""
    yield


def _noop(*_args, **_kwargs):
    """No-op function to replace setup functions."""
    return True


# All setup functions called within timed_phase blocks in main()
_SETUP_PATCHES = [
    "entrypoint.setup_user",
    "entrypoint.setup_repo_permissions",
    "entrypoint.setup_environment",
    "entrypoint.setup_egg_symlink",
    "entrypoint.setup_git",
    "entrypoint.setup_gateway_ca",
    "entrypoint.setup_worktrees",
    "entrypoint.restore_prebuilt_deps",
    "entrypoint.setup_agent_rules",
    "entrypoint.setup_claude",
    "entrypoint.setup_bashrc",
    "entrypoint.setup_command_timeout",
    "entrypoint.check_gateway_health",
    "entrypoint.setup_anthropic_api",
    "entrypoint.cleanup_on_exit",
]


@pytest.fixture()
def _bypass_setup():
    """Patch all setup functions to no-ops so main() reaches the mode switch."""
    patches = [patch("entrypoint.timed_phase", side_effect=_noop_context)]
    patches += [patch(name, side_effect=_noop) for name in _SETUP_PATCHES]
    patches.append(patch("entrypoint.signal.signal"))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


class TestPipelineModeInteractiveGuard:
    """Entrypoint should refuse interactive mode when in pipeline mode."""

    @patch("entrypoint.run_interactive")
    @patch.dict(
        os.environ,
        {"EGG_PIPELINE_ID": "issue-1591-v1", "EGG_AGENT_ROLE": "coder"},
    )
    @patch("sys.argv", ["entrypoint.py"])
    def test_rejects_interactive_mode_in_pipeline(self, mock_run_interactive, _bypass_setup):
        """No command + pipeline mode should exit(1), not call run_interactive."""
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        mock_run_interactive.assert_not_called()

    @patch("entrypoint.run_interactive", return_value=0)
    @patch.dict(
        os.environ,
        {
            "EGG_PIPELINE_ID": "",
            "EGG_ORCHESTRATOR_URL": "",
            "EGG_ORCHESTRATOR_MODE": "local",
            "EGG_AGENT_ROLE": "",
        },
    )
    @patch("sys.argv", ["entrypoint.py"])
    def test_allows_interactive_mode_without_pipeline(self, mock_run_interactive, _bypass_setup):
        """No command + no pipeline mode should call run_interactive normally."""
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        mock_run_interactive.assert_called_once()
