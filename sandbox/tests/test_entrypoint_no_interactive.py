"""Tests for the entrypoint's no-argument handling after #1762.

Replaces the deleted ``test_entrypoint_pipeline_guard.py`` after
interactive mode was removed. The old test verified that pipeline
mode with no command refused to enter ``run_interactive()``; now the
entrypoint has no interactive branch at all, so both paths — pipeline
mode and host mode — must exit with a clear error when no command is
provided.

Covers:
    * ``len(sys.argv) == 1`` in orchestrator mode → exit(1) and the
      orchestrator-completion signal fires with the expected payload.
    * ``len(sys.argv) == 1`` in host mode → exit(2) with a
      "use submit_task MCP tool" message. No ``run_interactive``
      call exists to mock — if one sneaks back in, the pytest run
      surfaces the regression by failing on the ``sys.exit`` contract.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from entrypoint import main


@contextlib.contextmanager
def _noop_context(*_args, **_kwargs):
    yield


def _noop(*_args, **_kwargs):
    return True


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
    patches = [patch("entrypoint.timed_phase", side_effect=_noop_context)]
    patches += [patch(name, side_effect=_noop) for name in _SETUP_PATCHES]
    patches.append(patch("entrypoint.signal.signal"))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


class TestRunInteractiveRemoved:
    """``run_interactive`` must NOT exist on the entrypoint module —
    its removal in #1762 is the whole point of the interactive-mode
    cutover. A stale attribute hiding behind a re-export would silently
    bring it back."""

    def test_run_interactive_attribute_absent(self):
        import entrypoint

        assert not hasattr(entrypoint, "run_interactive"), (
            "entrypoint.run_interactive must be gone after #1762"
        )


class TestNoArgsInPipelineMode:
    """No command + pipeline mode → exit(1) + signal orchestrator."""

    @patch("entrypoint.signal_orchestrator_completion")
    @patch.dict(
        os.environ,
        {
            "EGG_PIPELINE_ID": "issue-1762-test",
            "EGG_AGENT_ROLE": "coder",
        },
    )
    @patch("sys.argv", ["entrypoint.py"])
    def test_pipeline_mode_no_args_exits_one(self, mock_signal, _bypass_setup):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        mock_signal.assert_called_once()
        _, kwargs = mock_signal.call_args
        assert kwargs["exit_code"] == 1
        assert "No command provided" in kwargs["error_message"]


class TestNoArgsInHostMode:
    """No command + host mode → exit(2) with clear 'use submit_task'
    guidance; no orchestrator signal (there is no orchestrator)."""

    @patch("entrypoint.signal_orchestrator_completion")
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
    def test_host_mode_no_args_exits_two(self, mock_signal, _bypass_setup):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 2
        # Host mode doesn't go through the orchestrator signal helper
        # because there's no pipeline to notify.
        mock_signal.assert_not_called()
