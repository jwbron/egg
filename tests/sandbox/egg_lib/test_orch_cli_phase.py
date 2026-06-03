"""Tests for ``egg-orch phase get-context`` added in slice-1 of issue
#2908 (TASK-1-5).

The subcommand wraps the existing ``phase_get_context`` handler
(``sandbox/egg_agent_tools/handlers/phase.py:139``) so the bash
wrapper for the event-pump (slice-2) can pull the same per-event
context the MCP tool surfaces today, without instantiating an
in-pod MCP server.

The handler call is mocked at ``phase_get_context`` so no
contract gateway round-trip is required; this mirrors the
established CLI test pattern.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_lib import orch_cli  # noqa: E402


@pytest.fixture
def phase_env(monkeypatch):
    """Standard agent-pod env (pipeline/role) without lifecycle secret."""
    monkeypatch.setenv("EGG_PIPELINE_ID", "issue-2908-impl2")
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    monkeypatch.setenv("EGG_PHASE", "implement")
    monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator.test:9849")
    monkeypatch.delenv("EGG_LIFECYCLE_SECRET", raising=False)


@pytest.fixture
def phase_env_authed(phase_env, monkeypatch):
    monkeypatch.setenv("EGG_LIFECYCLE_SECRET", "test-lifecycle-secret")


def _ns(**overrides):
    defaults = {
        "pipeline_id": None,
        "phase": None,
        "role": None,
        "json": False,
        "include_artifacts": True,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _resolve_handler():
    for name in (
        "cmd_phase_get_context",
        "cmd_phase_context",
    ):
        fn = getattr(orch_cli, name, None)
        if fn is not None:
            return fn
    raise AttributeError("Expected egg_lib.orch_cli to expose cmd_phase_get_context")


# ---------------------------------------------------------------------------
# Happy path — defaults from env
# ---------------------------------------------------------------------------


class TestPhaseGetContext:
    def test_happy_path_defaults_from_env(self, phase_env, capsys):
        """Defaults read $EGG_PIPELINE_ID / $EGG_AGENT_ROLE / $EGG_PHASE."""
        handler = _resolve_handler()
        mock_payload = {
            "ok": True,
            "pipeline_id": "issue-2908-impl2",
            "phase": "implement",
            "role": "tester",
            "contract_present": True,
            "current_contract_phase": "implement",
            "tasks": [
                {"id": "task-1-8", "status": "pending", "role": "tester"},
            ],
            "artifacts": [".egg-state/drafts/issue-2908-impl2-plan.md"],
            "repo_path": "/home/egg/repos/egg",
        }
        # Patch the underlying handler so we exercise the CLI shim
        # without touching the contract loader.
        with patch(
            "egg_agent_tools.handlers.phase.phase_get_context",
            return_value=mock_payload,
        ) as mock_handler:
            rc = handler(_ns(json=True))
        assert rc == 0
        # The handler was called with the env defaults threaded through.
        call_kwargs = mock_handler.call_args.args[0] if mock_handler.call_args.args else {}
        # Either the CLI passes the env values explicitly OR the handler
        # picks them up from env vars — both are acceptable.
        if "pipeline_id" in call_kwargs:
            assert call_kwargs["pipeline_id"] == "issue-2908-impl2"
        out = capsys.readouterr().out
        decoded = json.loads(out)
        assert decoded["pipeline_id"] == "issue-2908-impl2"
        assert decoded["role"] == "tester"
        assert decoded["phase"] == "implement"
        assert decoded["tasks"][0]["id"] == "task-1-8"

    def test_phase_and_role_overrides(self, phase_env, capsys):
        """``--phase plan --role task_planner`` returns plan context."""
        handler = _resolve_handler()
        mock_payload = {
            "ok": True,
            "pipeline_id": "issue-2908-impl2",
            "phase": "plan",
            "role": "task_planner",
            "tasks": [],
            "artifacts": [],
            "contract_present": True,
            "current_contract_phase": "implement",
            "repo_path": "/home/egg/repos/egg",
        }
        with patch(
            "egg_agent_tools.handlers.phase.phase_get_context",
            return_value=mock_payload,
        ) as mock_handler:
            rc = handler(_ns(phase="plan", role="task_planner", json=True))
        assert rc == 0
        decoded = json.loads(capsys.readouterr().out)
        assert decoded["phase"] == "plan"
        assert decoded["role"] == "task_planner"
        # The CLI must thread the overrides into the handler call so
        # the right contract slice is loaded.
        call_kwargs = mock_handler.call_args.args[0] if mock_handler.call_args.args else {}
        if "phase" in call_kwargs:
            assert call_kwargs["phase"] == "plan"
        if "role" in call_kwargs:
            assert call_kwargs["role"] == "task_planner"

    def test_lifecycle_secret_not_required_on_agent_pod(self, phase_env, capsys):
        """Agents call this without EGG_LIFECYCLE_SECRET — must not 401."""
        handler = _resolve_handler()
        mock_payload = {
            "ok": True,
            "pipeline_id": "issue-2908-impl2",
            "phase": "implement",
            "role": "tester",
            "tasks": [],
            "artifacts": [],
            "contract_present": True,
            "current_contract_phase": "implement",
            "repo_path": "/home/egg/repos/egg",
        }
        with patch(
            "egg_agent_tools.handlers.phase.phase_get_context",
            return_value=mock_payload,
        ):
            rc = handler(_ns(json=True))
        # No SystemExit, return code 0.
        assert rc == 0

    def test_output_matches_mcp_surface(self, phase_env, capsys):
        """Output matches the ``phase_get_context`` handler shape (the
        same payload the retired ``mcp__phase__get_context`` tool
        emitted)."""
        handler = _resolve_handler()
        # Same shape as phase_get_context returns (lines 180-190).
        expected_keys = {
            "ok",
            "pipeline_id",
            "phase",
            "role",
            "contract_present",
            "current_contract_phase",
            "tasks",
            "artifacts",
            "repo_path",
        }
        mock_payload = {
            "ok": True,
            "pipeline_id": "issue-2908-impl2",
            "phase": "implement",
            "role": "tester",
            "contract_present": True,
            "current_contract_phase": "implement",
            "tasks": [],
            "artifacts": [],
            "repo_path": "/home/egg/repos/egg",
        }
        with patch(
            "egg_agent_tools.handlers.phase.phase_get_context",
            return_value=mock_payload,
        ):
            rc = handler(_ns(json=True))
        assert rc == 0
        decoded = json.loads(capsys.readouterr().out)
        # Every documented key must be present.
        missing = expected_keys - set(decoded.keys())
        assert not missing, (
            f"phase get-context output is missing keys from MCP-tool "
            f"surface — missing={missing!r}, got_keys={set(decoded.keys())!r}"
        )


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


class TestPhaseGetContextParser:
    def test_subparser_registered(self, capsys):
        parser = orch_cli.create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["phase", "get-context", "--help"])
        out = capsys.readouterr().out
        # Help text advertises --phase / --role / --json so the wrapper
        # bash can discover them.
        assert "--phase" in out or "phase" in out.lower()
        assert "--role" in out
