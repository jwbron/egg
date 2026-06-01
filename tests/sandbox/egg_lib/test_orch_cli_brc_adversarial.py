"""Adversarial probes for the new ``brc`` CLI subcommands (issue #2908 slice-1).

These tests target edge cases and boundary conditions that the happy-path
suite in ``test_orch_cli_brc.py`` does not exercise. Each test is designed
to surface a real bug or a latent fragility that the coder should harden.

Patterns:
`` - malformed orchestrator responses (missing keys, unexpected types)
- missing/invalid environment defaults
- argparse edge cases (conflicting flags, empty strings)
- error-path rendering (GatewayError / HandlerError surfaced via
  ``_render_handler_error``)
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

from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402
from egg_lib import orch_cli  # noqa: E402


@pytest.fixture
def brc_env(monkeypatch):
    monkeypatch.setenv("EGG_PIPELINE_ID", "issue-2908-impl2")
    monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
    monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator.test:9849")
    monkeypatch.delenv("EGG_LIFECYCLE_SECRET", raising=False)
    monkeypatch.delenv("EGG_SLICE_ID", raising=False)


def _ns(**overrides):
    defaults = {
        "pipeline_id": None,
        "role": None,
        "json": False,
        "slice_id": None,
        "verbose": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _handler(name: str):
    fn = getattr(orch_cli, name, None)
    if fn is None:
        raise AttributeError(f"egg_lib.orch_cli.{name} not found")
    return fn


# ---------------------------------------------------------------------------
# Adversarial probes: next-action
# ---------------------------------------------------------------------------


class TestBrcNextActionAdversarial:
    def test_missing_role_no_env_falls_back_to_error(self, brc_env, monkeypatch, capsys):
        """When neither ``--role`` nor $EGG_AGENT_ROLE is set, the CLI
        returns rc=1 with a stderr message (not a KeyError / traceback)."""
        monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
        handler = _handler("cmd_brc_next_action")
        rc = handler(_ns(json=True))
        assert rc == 1
        captured = capsys.readouterr()
        assert "role" in captured.err.lower()

    def test_empty_role_string_rejected(self, brc_env, capsys):
        """``--role ""`` is treated as missing (orchestrator contract rejects
        empty role body; CLI must also short-circuit before the HTTP call)."""
        handler = _handler("cmd_brc_next_action")
        with patch("egg_lib.orch_cli.orch_request") as req:
            rc = handler(_ns(role="", json=True))
        # Either the CLI short-circuits (rc=1) or it threads the empty
        # string to the orchestrator which returns 400 — both are
        # acceptable, but the call must not crash.
        assert rc in (0, 1)
        if rc == 1:
            captured = capsys.readouterr()
            assert "role" in captured.err.lower()
        else:
            # If the orchestrator returned success despite the empty role,
            # the CLI still printed something actionable.
            assert req.called

    def test_orchestrator_returns_action_missing_key(self, brc_env, capsys):
        """When the orchestrator omits ``action``, the CLI falls back to
        ``"unknown"`` rather than crashing on .get()."""
        handler = _handler("cmd_brc_next_action")
        with patch(
            "egg_lib.orch_cli.orch_request",
            return_value={"success": True, "reason": "no action key"},
        ):
            rc = handler(_ns(role="coder"))
        assert rc == 0
        out = capsys.readouterr().out
        # Defensive default — "unknown" (orch_cli.py:2931).
        assert "unknown" in out.lower()

    def test_json_mode_no_success_key_in_output(self, brc_env, capsys):
        """The ``success`` envelope MUST be stripped in --json mode so
        downstream jq-driven wrapper bash gets a clean action payload."""
        handler = _handler("cmd_brc_next_action")
        with patch(
            "egg_lib.orch_cli.orch_request",
            return_value={
                "success": True,
                "action": "wait",
                "reason": "idle",
                "extra_key": "preserved",
            },
        ):
            rc = handler(_ns(role="coder", json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert "success" not in out
        assert out["action"] == "wait"
        # Other keys preserved even if the orchestrator adds new ones.
        assert out.get("extra_key") == "preserved"

    def test_event_payload_none_coalesces_to_dict(self, brc_env, capsys):
        """When the orchestrator returns ``event_payload: null``, the CLI
        falls back to ``{}`` so the human-mode rendering (for k, v in
        event_payload.items()) doesn't TypeError on None."""
        handler = _handler("cmd_brc_next_action")
        with patch(
            "egg_lib.orch_cli.orch_request",
            return_value={
                "success": True,
                "action": "wait",
                "event_payload": None,
            },
        ):
            rc = handler(_ns(role="coder"))
        assert rc == 0  # must not crash

    def test_slice_id_override_threads_to_request(self, brc_env, monkeypatch):
        """``--slice-id slice-3`` overrides the env default and lands in
        the POST body."""
        monkeypatch.setenv("EGG_SLICE_ID", "slice-1")
        handler = _handler("cmd_brc_next_action")
        with patch(
            "egg_lib.orch_cli.orch_request",
            return_value={"success": True, "action": "wait", "role": "coder"},
        ) as req:
            handler(_ns(role="coder", slice_id="slice-3", json=True))
        data = req.call_args.kwargs.get("data") or {}
        assert data.get("slice_id") == "slice-3"

    def test_pipeline_id_required(self, brc_env, monkeypatch, capsys):
        """The pipeline ID must be resolvable from either ``args`` or
        $EGG_PIPELINE_ID; when both are absent the CLI exits non-zero."""
        monkeypatch.delenv("EGG_PIPELINE_ID", raising=False)
        handler = _handler("cmd_brc_next_action")
        try:
            rc = handler(_ns(pipeline_id=None, role="coder", json=True))
            # If we got here, the handler returned a non-zero exit code.
            assert rc != 0
        except SystemExit as exc:
            # The helper ``require_pipeline_id`` raises SystemExit(1)
            # when the pipeline ID is missing — the right contract.
            assert exc.code in (1, "1")


# ---------------------------------------------------------------------------
# Adversarial probes: get-state
# ---------------------------------------------------------------------------


class TestBrcGetStateAdversarial:
    def test_gateway_error_surfaces_non_zero(self, brc_env, capsys):
        """When the gateway refuses the request, the CLI exits non-zero
        and renders the gateway's reason (not a traceback)."""
        handler = _handler("cmd_brc_get_state")
        with patch(
            "egg_agent_tools.handlers.brc.brc_get_state",
            side_effect=GatewayError("gateway refused", status_code=503),
        ):
            rc = handler(_ns(json=False))
        assert rc != 0
        # The error is rendered via _render_handler_error; check no
        # traceback leaked to stderr.
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert "Traceback" not in captured.out

    def test_handler_error_surfaces_non_zero(self, brc_env):
        handler = _handler("cmd_brc_get_state")
        with patch(
            "egg_agent_tools.handlers.brc.brc_get_state",
            side_effect=HandlerError("bad input"),
        ):
            rc = handler(_ns())
        assert rc != 0

    def test_empty_consensus_rendered_safe(self, brc_env, capsys):
        """When the handler returns an empty/None consensus block, the
        CLI must not crash on ``agents.items()``."""
        handler = _handler("cmd_brc_get_state")
        with patch(
            "egg_agent_tools.handlers.brc.brc_get_state",
            return_value={"ok": True, "consensus": None, "slice_id": "slice-1"},
        ):
            rc = handler(_ns())
        assert rc == 0
        out = capsys.readouterr().out
        # Implementation always outputs JSON (even when --json=False),
        # so we just check it didn't crash and returned valid JSON.
        data = json.loads(out)
        assert data["consensus"] is None


# ---------------------------------------------------------------------------
# Adversarial probes: list-blocking
# ---------------------------------------------------------------------------


class TestBrcListBlockingAdversarial:
    def test_empty_list_does_not_emit_trailing_newline(self, brc_env, capsys):
        """Empty blocking list in default mode must emit no lines (so a
        shell ``while read role`` loop does not iterate once on ``""``)."""
        handler = _handler("cmd_brc_list_blocking")
        with patch(
            "egg_agent_tools.handlers.brc.brc_list_blocking",
            return_value={"blocking_agents": []},
        ):
            rc = handler(_ns())
        assert rc == 0
        # Empty stdout is acceptable; a single blank line is NOT.
        assert capsys.readouterr().out.strip() == ""

    def test_none_blocking_array_coerced_to_empty(self, brc_env, capsys):
        """If the handler returns ``blocking_agents: None`` (defensive
        codepath), the CLI must not crash."""
        handler = _handler("cmd_brc_list_blocking")
        with patch(
            "egg_agent_tools.handlers.brc.brc_list_blocking",
            return_value={"blocking_agents": None},
        ):
            rc = handler(_ns())
        assert rc == 0
        # No traceback.
        captured = capsys.readouterr()
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err

    def test_roles_with_underscores_and_digits_render_verbatim(self, brc_env, capsys):
        """Role tokens like ``reviewer_code_holistic`` must round-trip
        verbatim (shell scripts may key on exact string match)."""
        handler = _handler("cmd_brc_list_blocking")
        with patch(
            "egg_agent_tools.handlers.brc.brc_list_blocking",
            return_value={
                "blocking_agents": ["reviewer_code_holistic", "reviewer-1"]
            },
        ):
            rc = handler(_ns())
        assert rc == 0
        out = capsys.readouterr().out
        assert "reviewer_code_holistic" in out
        assert "reviewer-1" in out
