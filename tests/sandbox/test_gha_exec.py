"""Tests for ``sandbox/egg_lib/gha_exec.py``.

Replaces the deleted ``tests/sandbox/test_cli_main.py`` after #1762,
which removed interactive mode (``egg_lib.cli.main``). The GHA
entry-point ``gha_exec()`` was relocated from ``egg_lib.cli`` to a
dedicated ``egg_lib/gha_exec.py`` module so ``action/entrypoint.sh`` can
continue to import it without pulling in dead interactive code.

This module covers:
    * The new import path ``egg_lib.gha_exec.gha_exec`` works.
    * Legacy ``egg_lib.cli`` is gone (ImportError).
    * ``action/entrypoint.sh`` references the new path.
    * ``gha_exec()`` end-to-end orchestration on the happy path.
    * Failure paths (network creation, gateway start, empty prompt,
      mode detection, extra-env passthrough).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
if str(sandbox_path) not in sys.path:
    sys.path.insert(0, str(sandbox_path))


# ---------------------------------------------------------------------------
# Module-level relocation checks (risk_analyst R1)
# ---------------------------------------------------------------------------


class TestImportPathRelocation:
    def test_gha_exec_importable_from_new_path(self):
        from egg_lib.gha_exec import gha_exec

        assert callable(gha_exec)

    def test_gha_exec_reexported_from_package(self):
        """``egg_lib/__init__.py`` re-exports ``gha_exec`` so the old
        ``from egg_lib import gha_exec`` style still works."""
        import egg_lib

        assert hasattr(egg_lib, "gha_exec")
        assert callable(egg_lib.gha_exec)

    def test_legacy_cli_module_removed(self):
        """The old ``egg_lib.cli`` module must no longer exist — a stale
        import hiding behind it would silently bring interactive mode
        back."""
        # Force a fresh import so a stale cached module can't mask the
        # regression. ``importlib.util.find_spec`` returns None when the
        # module is absent from every entry on sys.path.
        import importlib.util

        assert importlib.util.find_spec("egg_lib.cli") is None


class TestActionEntrypointScriptUpdated:
    """``action/entrypoint.sh`` must import from the new path so the
    GitHub Action keeps working in lockstep with this PR."""

    def test_entrypoint_sh_references_gha_exec_module(self):
        entry = Path(__file__).parent.parent.parent / "action" / "entrypoint.sh"
        assert entry.exists(), f"{entry} missing"
        body = entry.read_text()
        assert "egg_lib.gha_exec" in body, (
            "action/entrypoint.sh must import gha_exec from egg_lib.gha_exec "
            "after #1762"
        )
        assert "from egg_lib.cli import" not in body


# ---------------------------------------------------------------------------
# Happy path — gha_exec() orchestration
# ---------------------------------------------------------------------------


def _common_env(monkeypatch, **overrides):
    """Populate env vars that gha_exec reads. Tests override per-case."""
    defaults = {
        "INPUT_PROMPT": "investigate the failing test",
        "INPUT_MODEL": "opus[1m]",
        "INPUT_TIMEOUT": "30",
        "GITHUB_EVENT_REPOSITORY_VISIBILITY": "public",
    }
    for k, v in {**defaults, **overrides}.items():
        monkeypatch.setenv(k, str(v))


class TestGhaExecHappyPath:
    def test_success_returns_zero(self, monkeypatch):
        _common_env(monkeypatch)
        with (
            patch("egg_lib.gha_exec.RuntimeContext"),
            patch("egg_lib.gha_exec.set_context"),
            patch("egg_lib.gha_exec.set_quiet_mode"),
            patch(
                "egg_lib.docker.ensure_gateway_networks", return_value=True
            ),
            patch(
                "egg_lib.gateway.start_gateway_container", return_value=True
            ),
            patch(
                "egg_lib.gha_exec.exec_in_new_container", return_value=True
            ) as mock_exec,
        ):
            from egg_lib.gha_exec import gha_exec

            assert gha_exec() == 0
            mock_exec.assert_called_once()

    def test_failure_returns_one(self, monkeypatch):
        _common_env(monkeypatch)
        with (
            patch("egg_lib.gha_exec.RuntimeContext"),
            patch("egg_lib.gha_exec.set_context"),
            patch("egg_lib.gha_exec.set_quiet_mode"),
            patch(
                "egg_lib.docker.ensure_gateway_networks", return_value=True
            ),
            patch(
                "egg_lib.gateway.start_gateway_container", return_value=True
            ),
            patch(
                "egg_lib.gha_exec.exec_in_new_container", return_value=False
            ),
        ):
            from egg_lib.gha_exec import gha_exec

            assert gha_exec() == 1


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestGhaExecFailurePaths:
    def test_network_creation_failure_returns_one(self, monkeypatch):
        _common_env(monkeypatch)
        with (
            patch("egg_lib.gha_exec.RuntimeContext"),
            patch("egg_lib.gha_exec.set_context"),
            patch("egg_lib.gha_exec.set_quiet_mode"),
            patch(
                "egg_lib.docker.ensure_gateway_networks", return_value=False
            ),
        ):
            from egg_lib.gha_exec import gha_exec

            assert gha_exec() == 1

    def test_gateway_start_failure_returns_one(self, monkeypatch):
        _common_env(monkeypatch)
        with (
            patch("egg_lib.gha_exec.RuntimeContext"),
            patch("egg_lib.gha_exec.set_context"),
            patch("egg_lib.gha_exec.set_quiet_mode"),
            patch(
                "egg_lib.docker.ensure_gateway_networks", return_value=True
            ),
            patch(
                "egg_lib.gateway.start_gateway_container", return_value=False
            ),
        ):
            from egg_lib.gha_exec import gha_exec

            assert gha_exec() == 1

    def test_empty_prompt_returns_one(self, monkeypatch):
        _common_env(monkeypatch, INPUT_PROMPT="   ")
        with (
            patch("egg_lib.gha_exec.RuntimeContext"),
            patch("egg_lib.gha_exec.set_context"),
            patch("egg_lib.gha_exec.set_quiet_mode"),
            patch(
                "egg_lib.docker.ensure_gateway_networks", return_value=True
            ),
            patch(
                "egg_lib.gateway.start_gateway_container", return_value=True
            ),
            patch("egg_lib.gha_exec.exec_in_new_container") as mock_exec,
        ):
            from egg_lib.gha_exec import gha_exec

            assert gha_exec() == 1
            mock_exec.assert_not_called()


# ---------------------------------------------------------------------------
# Mode detection + command construction
# ---------------------------------------------------------------------------


class TestGhaExecModeAndCommand:
    def test_visibility_private_maps_to_private_mode(self, monkeypatch):
        _common_env(
            monkeypatch,
            INPUT_MODE="auto",
            GITHUB_EVENT_REPOSITORY_VISIBILITY="private",
        )
        with (
            patch("egg_lib.gha_exec.RuntimeContext"),
            patch("egg_lib.gha_exec.set_context"),
            patch("egg_lib.gha_exec.set_quiet_mode"),
            patch(
                "egg_lib.docker.ensure_gateway_networks", return_value=True
            ),
            patch(
                "egg_lib.gateway.start_gateway_container", return_value=True
            ),
            patch(
                "egg_lib.gha_exec.exec_in_new_container", return_value=True
            ) as mock_exec,
        ):
            from egg_lib.gha_exec import gha_exec

            gha_exec()
            call_kwargs = mock_exec.call_args.kwargs
            assert call_kwargs.get("repo_mode") == "private"

    def test_visibility_internal_maps_to_private_mode(self, monkeypatch):
        _common_env(
            monkeypatch,
            INPUT_MODE="auto",
            GITHUB_EVENT_REPOSITORY_VISIBILITY="internal",
        )
        with (
            patch("egg_lib.gha_exec.RuntimeContext"),
            patch("egg_lib.gha_exec.set_context"),
            patch("egg_lib.gha_exec.set_quiet_mode"),
            patch(
                "egg_lib.docker.ensure_gateway_networks", return_value=True
            ),
            patch(
                "egg_lib.gateway.start_gateway_container", return_value=True
            ),
            patch(
                "egg_lib.gha_exec.exec_in_new_container", return_value=True
            ) as mock_exec,
        ):
            from egg_lib.gha_exec import gha_exec

            gha_exec()
            assert mock_exec.call_args.kwargs.get("repo_mode") == "private"

    def test_explicit_mode_overrides_auto_detection(self, monkeypatch):
        _common_env(
            monkeypatch,
            INPUT_MODE="public",
            GITHUB_EVENT_REPOSITORY_VISIBILITY="private",
        )
        with (
            patch("egg_lib.gha_exec.RuntimeContext"),
            patch("egg_lib.gha_exec.set_context"),
            patch("egg_lib.gha_exec.set_quiet_mode"),
            patch(
                "egg_lib.docker.ensure_gateway_networks", return_value=True
            ),
            patch(
                "egg_lib.gateway.start_gateway_container", return_value=True
            ),
            patch(
                "egg_lib.gha_exec.exec_in_new_container", return_value=True
            ) as mock_exec,
        ):
            from egg_lib.gha_exec import gha_exec

            gha_exec()
            assert mock_exec.call_args.kwargs.get("repo_mode") == "public"

    def test_claude_command_includes_prompt_and_model(self, monkeypatch):
        _common_env(
            monkeypatch,
            INPUT_PROMPT="fix the bug",
            INPUT_MODEL="sonnet[4m]",
        )
        with (
            patch("egg_lib.gha_exec.RuntimeContext"),
            patch("egg_lib.gha_exec.set_context"),
            patch("egg_lib.gha_exec.set_quiet_mode"),
            patch(
                "egg_lib.docker.ensure_gateway_networks", return_value=True
            ),
            patch(
                "egg_lib.gateway.start_gateway_container", return_value=True
            ),
            patch(
                "egg_lib.gha_exec.exec_in_new_container", return_value=True
            ) as mock_exec,
        ):
            from egg_lib.gha_exec import gha_exec

            gha_exec()
            cmd = mock_exec.call_args.kwargs.get("command")
            assert cmd is not None
            assert "fix the bug" in cmd
            assert "sonnet[4m]" in cmd


# ---------------------------------------------------------------------------
# Extra environment passthrough (bot name, issue, commit, role)
# ---------------------------------------------------------------------------


class TestGhaExecExtraEnv:
    @pytest.mark.parametrize(
        "env_var,env_val",
        [
            ("EGG_BOT_NAME", "egg-bot"),
            ("EGG_ISSUE_NUMBER", "1762"),
            ("EGG_COMMIT_SHA", "abc1234"),
            ("EGG_AGENT_ROLE", "reviewer_code"),
            ("EGG_PR_NUMBER", "42"),
            ("EGG_PIPELINE_ID", "issue-1762-membump"),
        ],
    )
    def test_env_var_forwarded_to_container(self, monkeypatch, env_var, env_val):
        _common_env(monkeypatch)
        monkeypatch.setenv(env_var, env_val)
        with (
            patch("egg_lib.gha_exec.RuntimeContext"),
            patch("egg_lib.gha_exec.set_context"),
            patch("egg_lib.gha_exec.set_quiet_mode"),
            patch(
                "egg_lib.docker.ensure_gateway_networks", return_value=True
            ),
            patch(
                "egg_lib.gateway.start_gateway_container", return_value=True
            ),
            patch(
                "egg_lib.gha_exec.exec_in_new_container", return_value=True
            ) as mock_exec,
        ):
            from egg_lib.gha_exec import gha_exec

            gha_exec()
            extra_env = mock_exec.call_args.kwargs.get("extra_env") or {}
            assert extra_env.get(env_var) == env_val

    def test_no_extra_env_when_all_unset(self, monkeypatch):
        _common_env(monkeypatch)
        for v in (
            "EGG_BOT_NAME",
            "EGG_ISSUE_NUMBER",
            "EGG_COMMIT_SHA",
            "EGG_AGENT_ROLE",
            "EGG_PR_NUMBER",
            "EGG_PIPELINE_ID",
        ):
            monkeypatch.delenv(v, raising=False)
        with (
            patch("egg_lib.gha_exec.RuntimeContext"),
            patch("egg_lib.gha_exec.set_context"),
            patch("egg_lib.gha_exec.set_quiet_mode"),
            patch(
                "egg_lib.docker.ensure_gateway_networks", return_value=True
            ),
            patch(
                "egg_lib.gateway.start_gateway_container", return_value=True
            ),
            patch(
                "egg_lib.gha_exec.exec_in_new_container", return_value=True
            ) as mock_exec,
        ):
            from egg_lib.gha_exec import gha_exec

            gha_exec()
            extra_env = mock_exec.call_args.kwargs.get("extra_env")
            # extra_env should be None or empty (no spurious keys).
            assert not extra_env
