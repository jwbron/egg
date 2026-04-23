"""Tests that ``--scope-filter`` and its infrastructure were fully removed (#1882).

The gateway's auto-filter (#1882) supersedes the client-side
``--scope-filter`` workaround that shipped in #1547.  Both must disappear
in the same PR so agents that accidentally keep calling the old flag
fail loudly rather than silently bypassing the gateway.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pytest

_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)


@pytest.fixture
def push_parser() -> argparse.ArgumentParser:
    """Build the ``egg-orch push`` argparse tree so we can probe its flags."""
    from egg_lib.cli_push import register_push_subcommand

    parser = argparse.ArgumentParser(prog="egg-orch")
    subparsers = parser.add_subparsers(dest="command")
    register_push_subcommand(subparsers)
    return parser


# ---------------------------------------------------------------------------
# Argparse rejection
# ---------------------------------------------------------------------------


class TestScopeFilterArgparseRejection:
    def test_scope_filter_flag_rejected(self, push_parser, capsys):
        """``egg-orch push --scope-filter`` must exit 2 (argparse "unrecognized")."""
        with pytest.raises(SystemExit) as exc_info:
            push_parser.parse_args(["push", "--scope-filter"])
        # argparse exits 2 for unrecognized arguments.
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "unrecognized arguments" in captured.err
        assert "--scope-filter" in captured.err

    def test_scope_filter_with_value_rejected(self, push_parser, capsys):
        """``--scope-filter=yes`` (form-with-value) also rejected."""
        with pytest.raises(SystemExit) as exc_info:
            push_parser.parse_args(["push", "--scope-filter=yes"])
        assert exc_info.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err

    def test_plain_push_parses_cleanly(self, push_parser):
        """``egg-orch push`` without --scope-filter parses without error."""
        ns = push_parser.parse_args(["push"])
        assert ns.command == "push"
        # The func is registered as cmd_push.
        assert callable(ns.func)


# ---------------------------------------------------------------------------
# Source-level removal — code and env-var references are gone
# ---------------------------------------------------------------------------


class TestSourceLevelRemoval:
    _REPO_ROOT = Path(__file__).resolve().parent.parent.parent

    def test_cli_push_has_no_scope_filter_references(self):
        """The cli_push module must not define ``--scope-filter`` or its helper.

        Docstrings may reference the removal for historical context;
        what we actually ban is live code that parses the flag or
        invokes the old helper.  We strip out triple-quoted docstrings
        and single-line comments before scanning.
        """
        src = (self._REPO_ROOT / "sandbox" / "egg_lib" / "cli_push.py").read_text()
        # Strip triple-quoted docstrings / string literals so we only scan live code.
        stripped = re.sub(r'"""[\s\S]*?"""', "", src)
        stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
        # Strip single-line comments.
        stripped = re.sub(r"#.*", "", stripped)
        # argparse add_argument("--scope-filter", ...) must be gone.
        assert "--scope-filter" not in stripped, "cli_push.py still parses --scope-filter"
        assert "def _filter_files" not in stripped, "cli_push.py still defines _filter_files"
        assert "scope_filter" not in stripped, (
            "cli_push.py still references scope_filter in live code"
        )

    def test_cli_push_has_no_egg_agent_file_patterns_read(self):
        """The env-var is no longer read by live code in the sandbox push path."""
        src = (self._REPO_ROOT / "sandbox" / "egg_lib" / "cli_push.py").read_text()
        stripped = re.sub(r'"""[\s\S]*?"""', "", src)
        stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
        stripped = re.sub(r"#.*", "", stripped)
        assert "EGG_AGENT_FILE_PATTERNS" not in stripped, (
            "cli_push.py still reads EGG_AGENT_FILE_PATTERNS in live code"
        )

    def test_concurrent_executor_no_longer_injects_env_var(self):
        """concurrent_executor.py must not *emit* EGG_AGENT_FILE_PATTERNS (comments OK)."""
        src = (self._REPO_ROOT / "orchestrator" / "concurrent_executor.py").read_text()
        # Strip docstrings and comments; the file is allowed to
        # document that the emission was removed.
        stripped = re.sub(r'"""[\s\S]*?"""', "", src)
        stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
        stripped = re.sub(r"#.*", "", stripped)
        assert "EGG_AGENT_FILE_PATTERNS" not in stripped, (
            "concurrent_executor.py still emits EGG_AGENT_FILE_PATTERNS in live code"
        )

    def test_get_agent_env_does_not_include_egg_agent_file_patterns(self):
        """Structural check: concurrent_executor's env builder must not set the var."""
        # Import the executor; build a minimal Pipeline and drive
        # get_agent_env() for a role.  If the removal regresses, the
        # returned dict will contain the env var.
        from unittest.mock import MagicMock

        sys.path.insert(0, str(self._REPO_ROOT / "orchestrator"))
        sys.path.insert(0, str(self._REPO_ROOT / "shared"))
        from concurrent_executor import ConcurrentPhaseExecutor  # type: ignore
        from models import AgentRole as _AgentRole  # type: ignore

        pipeline = MagicMock()
        pipeline.config = MagicMock()
        pipeline.config.message_poll_hint_seconds = 30
        pipeline.branch = "egg/issue-1882"
        pipeline.current_phase = MagicMock()
        pipeline.current_phase.value = "implement"
        pipeline.repo = "jwbron/egg"
        pipeline.id = "issue-1882"

        def fake_spawn(**_kwargs):
            return MagicMock()

        executor = ConcurrentPhaseExecutor(
            pipeline=pipeline,
            spawn_fn=fake_spawn,
        )
        env = executor.get_agent_env(_AgentRole.CODER)
        assert "EGG_AGENT_FILE_PATTERNS" not in env


# ---------------------------------------------------------------------------
# Passthrough semantics — egg-orch push still invokes git push
# ---------------------------------------------------------------------------


class TestPushPassthrough:
    def test_plain_push_calls_git_push(self, monkeypatch):
        """``cmd_push`` must call ``git push`` (or ``git push origin HEAD:<branch>``)."""
        from egg_lib import cli_push

        captured: list[list[str]] = []

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, *_a, **_kw):
            captured.append(list(cmd))
            return _Result()

        monkeypatch.setattr(cli_push.subprocess, "run", fake_run)
        monkeypatch.delenv("EGG_BRANCH", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            cli_push.cmd_push(argparse.Namespace())
        assert exc_info.value.code == 0
        # At least one of the captured calls must be the git push.
        push_calls = [c for c in captured if c[:2] == ["git", "push"]]
        assert push_calls, f"no git push call: {captured}"

    def test_push_retargets_to_assigned_branch(self, monkeypatch):
        """When EGG_BRANCH differs from HEAD, push must retarget to HEAD:<branch>."""
        from egg_lib import cli_push

        captured: list[list[str]] = []

        class _Result:
            returncode = 0
            stdout = "egg/issue-1882-coder/work\n"
            stderr = ""

        def fake_run(cmd, *_a, **_kw):
            captured.append(list(cmd))
            if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return _Result()
            return _Result()

        monkeypatch.setattr(cli_push.subprocess, "run", fake_run)
        monkeypatch.setenv("EGG_BRANCH", "egg/issue-1882")
        with pytest.raises(SystemExit):
            cli_push.cmd_push(argparse.Namespace())
        # The actual push should have used HEAD:egg/issue-1882.
        assert ["git", "push", "origin", "HEAD:egg/issue-1882"] in captured
