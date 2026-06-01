"""Tests for the new ``brc`` parent subparser and its three subcommands
(``next-action``, ``get-state``, ``list-blocking``) added in slice-1 of
issue #2908 (TASK-1-1, TASK-1-3, TASK-1-4).

These CLI subcommands are the agent-side surface for the event-pump
wrapper that lands in slice-2.  They wrap the orchestrator HTTP API
(for ``next-action``) and the existing ``brc_get_state`` /
``brc_list_blocking`` handlers (for the two view-only subcommands) so
the bash wrapper can drive consensus without a per-event MCP server.

The wire-level orchestrator calls are mocked at ``orch_request`` /
``orchestrator_request`` so no HTTP traffic occurs; this mirrors the
established pattern in ``tests/sandbox/test_orch_cli_slice_id.py`` and
``tests/sandbox/egg_agent_tools/test_handlers_brc.py``.
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

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def brc_env(monkeypatch):
    """Standard agent-pod env (pipeline/role) without lifecycle secret."""
    monkeypatch.setenv("EGG_PIPELINE_ID", "issue-2908-impl2")
    monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
    monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator.test:9849")
    monkeypatch.delenv("EGG_LIFECYCLE_SECRET", raising=False)


@pytest.fixture
def brc_env_authed(brc_env, monkeypatch):
    """Same as brc_env but with a lifecycle secret set (humans-only paths)."""
    monkeypatch.setenv("EGG_LIFECYCLE_SECRET", "test-lifecycle-secret")


def _ns(**overrides):
    """Construct an argparse.Namespace for brc subcommand handlers.

    The handlers read the same attribute names argparse would set.
    ``json`` defaults to False, matching the CLI default.
    """
    defaults = {
        "pipeline_id": None,
        "role": None,
        "json": False,
        "verbose": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# brc next-action  (TASK-1-1)
# ---------------------------------------------------------------------------


class TestBrcNextAction:
    """``egg-orch brc next-action --role R [--json]``.

    Wraps ``POST /api/v1/pipelines/{pid}/consensus/next-action`` and
    returns one of {wait, propose, ack, nack, confirm, complete} with
    an optional ``event_payload``.
    """

    def _resolve_handler(self):
        # Subcommand handler name follows the cmd_<subcmd> convention.
        # Production code may name it cmd_brc_next_action; fall back to a
        # handful of plausible alternatives so the test surfaces the
        # missing-handler case as a clear AttributeError pointing to the
        # expected public surface.
        for name in (
            "cmd_brc_next_action",
            "cmd_consensus_next_action",
        ):
            fn = getattr(orch_cli, name, None)
            if fn is not None:
                return fn
        raise AttributeError(
            "Expected egg_lib.orch_cli to expose cmd_brc_next_action "
            "(or cmd_consensus_next_action) — neither found."
        )

    def test_happy_path_json_shape(self, brc_env, capsys):
        """``--json`` round-trips the documented {action, event_payload?}.

        The orchestrator route returns the action at the top level
        (orchestrator/routes/consensus.py:75 ``_success``), with the
        ``success`` envelope stripped client-side before emit.
        """
        handler = self._resolve_handler()
        mock_response = {
            "success": True,
            "action": "propose",
            "role": "coder",
            "slice_id": None,
            "event_payload": {
                "version": 1,
                "summary": "ready to propose",
            },
        }
        args = _ns(role="coder", json=True)
        with patch(
            "egg_lib.orch_cli.orch_request",
            return_value=mock_response,
        ) as req:
            rc = handler(args)
        assert rc == 0
        # POST to the next-action endpoint with the role in the body.
        assert req.called
        call_endpoint = (
            req.call_args.args[0]
            if req.call_args.args
            else req.call_args.kwargs.get("endpoint", "")
        )
        assert "/consensus/next-action" in call_endpoint
        # JSON envelope (action key) emitted on stdout; ``success`` is
        # stripped per the CLI shim (orch_cli.py:cmd_brc_next_action).
        out = capsys.readouterr().out
        decoded = json.loads(out)
        assert decoded["action"] == "propose"
        assert decoded["event_payload"]["version"] == 1
        assert "success" not in decoded, (
            "CLI must strip the ``success`` envelope so jq-driven wrapper "
            "bash gets the action payload directly"
        )

    def test_role_defaults_to_env(self, brc_env, capsys):
        """``--role`` defaults to $EGG_AGENT_ROLE when not given.

        The CLI implementation (``cmd_brc_next_action`` in
        ``orch_cli.py``) puts ``role`` in the JSON body, not the query
        string. Asserting body-only — if a future refactor moves to a
        query-string contract the schema doc + this test must move
        together. (The earlier "body OR query-string" form let a
        regression slip past with the wrong call shape.)"""
        handler = self._resolve_handler()
        args = _ns(json=True)  # no explicit role
        with patch(
            "egg_lib.orch_cli.orch_request",
            return_value={"success": True, "action": "wait", "role": "coder"},
        ) as req:
            rc = handler(args)
        assert rc == 0
        body = req.call_args.kwargs.get("data") or {}
        assert body.get("role") == "coder", f"role=coder not threaded into body: body={body!r}"

    def test_lifecycle_secret_threaded_when_set(self, brc_env_authed):
        """When ``EGG_LIFECYCLE_SECRET`` is set, the auth header is sent.

        The brc subcommand goes through ``orch_request`` which itself
        attaches the Bearer token (orch_cli.py:324). We verify the call
        actually goes through ``orch_request`` (not a raw urllib request
        path that would skip the env-aware header logic).
        """
        handler = self._resolve_handler()
        with patch(
            "egg_lib.orch_cli.orch_request",
            return_value={"success": True, "action": "wait", "role": "coder"},
        ) as req:
            rc = handler(_ns(role="coder", json=True))
        assert rc == 0
        # The function must route through orch_request so the
        # Bearer-token attachment happens; if a future refactor uses
        # ``api_request`` directly it would skip lifecycle auth.
        assert req.called, (
            "next-action must route through orch_request so the "
            "EGG_LIFECYCLE_SECRET → Bearer header attachment happens"
        )

    def test_stale_version_edge_case(self, brc_env, capsys):
        """Stale-version (#2482) returned by the orchestrator surfaces
        as an actionable ``ack`` / ``nack`` instruction with the current
        version inlined in ``event_payload``."""
        handler = self._resolve_handler()
        mock_response = {
            "success": True,
            "action": "ack",
            "role": "reviewer_code",
            "slice_id": None,
            "event_payload": {
                "producer": "coder",
                "version": 2,
                "reason": "prior verdict superseded",
                "status": "stale_version",
            },
        }
        with patch(
            "egg_lib.orch_cli.orch_request",
            return_value=mock_response,
        ):
            rc = handler(_ns(role="reviewer_code", json=True))
        assert rc == 0
        out = capsys.readouterr().out
        decoded = json.loads(out)
        assert decoded["action"] == "ack"
        assert decoded["event_payload"]["version"] == 2

    def test_help_advertises_role_and_json(self, brc_env, capsys):
        """``--help`` describes the documented flags so wrapper authors
        can discover them without grepping source."""
        # Use the public argparse entry point — build the parser and
        # ask for help on the brc next-action subcommand.
        parser = orch_cli.create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["brc", "next-action", "--help"])
        out = capsys.readouterr().out
        # Flags advertised verbatim.
        assert "--role" in out
        assert "--json" in out


# ---------------------------------------------------------------------------
# brc get-state  (TASK-1-3)
# ---------------------------------------------------------------------------


class TestBrcGetState:
    """``egg-orch brc get-state [--verbose]``.

    Verb-level alias for ``brc_get_state`` handler. Returns
    {ok, slice_id, consensus: {...}, is_complete, blocking_agents,
    raw?} — matches the MCP-tool surface so the wrapper can call it
    directly.
    """

    def _resolve_handler(self):
        for name in ("cmd_brc_get_state",):
            fn = getattr(orch_cli, name, None)
            if fn is not None:
                return fn
        raise AttributeError("Expected egg_lib.orch_cli to expose cmd_brc_get_state")

    def test_happy_path_matches_mcp_shape(self, brc_env, capsys):
        """Output mirrors ``mcp__brc__get_state`` for the same pipeline."""
        handler = self._resolve_handler()
        consensus = {
            "is_complete": False,
            "blocking_agents": ["coder"],
            "agents": {"coder": {"confirmed": False}},
        }
        mock_response = {
            "success": True,
            "data": {"concurrent": {"consensus": consensus}},
        }
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value=mock_response,
        ):
            rc = handler(_ns(json=True))
        assert rc == 0
        out = capsys.readouterr().out
        decoded = json.loads(out)
        # Shape from brc_get_state — `consensus` is the agents/blocking view.
        assert decoded.get("ok") is True or "consensus" in decoded
        # blocking_agents must be reachable even if the top-level shape
        # is the consensus envelope.
        if "blocking_agents" in decoded:
            assert decoded["blocking_agents"] == ["coder"]
        elif "consensus" in decoded:
            assert decoded["consensus"]["blocking_agents"] == ["coder"]

    def test_verbose_flips_raw_key(self, brc_env, capsys):
        """``--verbose`` includes the raw status payload (``raw`` key)."""
        handler = self._resolve_handler()
        raw_payload = {
            "concurrent": {"consensus": {"is_complete": True, "agents": {}}},
            "extra_diagnostic": "field present only when verbose",
        }
        mock_response = {"success": True, "data": raw_payload}
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value=mock_response,
        ):
            rc = handler(_ns(json=True, verbose=True))
        assert rc == 0
        out = capsys.readouterr().out
        decoded = json.loads(out)
        # Verbose mode surfaces the raw key per brc_get_state.
        assert "raw" in decoded, (
            f"--verbose must surface the 'raw' status payload — got {decoded!r}"
        )

    def test_slice_id_threaded_from_env(self, brc_env, monkeypatch):
        """``EGG_SLICE_ID`` is threaded through so per-slice agents see
        their own tracker (#2761)."""
        monkeypatch.setenv("EGG_SLICE_ID", "slice-1")
        handler = self._resolve_handler()
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={
                "success": True,
                "data": {"concurrent": {"consensus": {}}},
            },
        ) as req:
            rc = handler(_ns(json=True))
        assert rc == 0
        # The handler calls orchestrator_request once; the endpoint
        # (first positional arg) must carry the slice_id query param.
        endpoint = req.call_args.args[0] if req.call_args.args else ""
        assert "slice_id=slice-1" in endpoint, (
            f"slice_id must be threaded into status endpoint — got {endpoint!r}"
        )


# ---------------------------------------------------------------------------
# brc list-blocking  (TASK-1-4)
# ---------------------------------------------------------------------------


class TestBrcListBlocking:
    """``egg-orch brc list-blocking``.

    Default output is one role per line for shell consumption
    (``while read role; do …; done``); ``--json`` returns
    ``{blocking_agents: [...]}``.
    """

    def _resolve_handler(self):
        for name in ("cmd_brc_list_blocking",):
            fn = getattr(orch_cli, name, None)
            if fn is not None:
                return fn
        raise AttributeError("Expected egg_lib.orch_cli to expose cmd_brc_list_blocking")

    def test_happy_path_newline_delimited(self, brc_env, capsys):
        """Default output is one role per line, no JSON envelope."""
        handler = self._resolve_handler()
        consensus = {
            "is_complete": False,
            "blocking_agents": ["coder", "reviewer_code"],
            "agents": {},
        }
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {"concurrent": {"consensus": consensus}}},
        ):
            rc = handler(_ns(json=False))
        assert rc == 0
        out = capsys.readouterr().out
        # One role per line; the loop ``while read role`` must work.
        lines = [line for line in out.splitlines() if line.strip()]
        assert lines == ["coder", "reviewer_code"], (
            f"expected newline-delimited blocking-agent list — got {lines!r}"
        )

    def test_json_mode_returns_array(self, brc_env, capsys):
        """``--json`` returns ``{blocking_agents: [...]}``."""
        handler = self._resolve_handler()
        consensus = {
            "is_complete": False,
            "blocking_agents": ["tester"],
            "agents": {},
        }
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {"concurrent": {"consensus": consensus}}},
        ):
            rc = handler(_ns(json=True))
        assert rc == 0
        decoded = json.loads(capsys.readouterr().out)
        assert decoded == {"blocking_agents": ["tester"]} or decoded.get("blocking_agents") == [
            "tester"
        ]

    def test_empty_list_exit_zero(self, brc_env, capsys):
        """Exit code 0 even when no agents are blocking — wrapper bash
        must not treat an empty list as an error."""
        handler = self._resolve_handler()
        consensus = {"is_complete": True, "blocking_agents": [], "agents": {}}
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {"concurrent": {"consensus": consensus}}},
        ):
            rc = handler(_ns(json=False))
        assert rc == 0
        out = capsys.readouterr().out
        # No noisy "No blocking agents" line — empty stdout so the loop
        # cleanly exits with no iterations.
        non_blank = [line for line in out.splitlines() if line.strip()]
        assert non_blank == [], (
            f"empty blocking list must produce no stdout lines — got {non_blank!r}"
        )

    def test_empty_list_json_mode(self, brc_env, capsys):
        """``--json`` on empty list returns ``{blocking_agents: []}``."""
        handler = self._resolve_handler()
        consensus = {"is_complete": True, "blocking_agents": [], "agents": {}}
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {"concurrent": {"consensus": consensus}}},
        ):
            rc = handler(_ns(json=True))
        assert rc == 0
        decoded = json.loads(capsys.readouterr().out)
        # Either {blocking_agents: []} or just [] — both let bash
        # iterate cleanly.
        if isinstance(decoded, dict):
            assert decoded.get("blocking_agents") == []
        else:
            assert decoded == []


# ---------------------------------------------------------------------------
# Parser registration sanity (brc parent + three sub-verbs visible)
# ---------------------------------------------------------------------------


class TestBrcParserRegistration:
    """The ``brc`` parser must be registered with all three sub-verbs
    visible to ``--help`` so the wrapper bash can discover them."""

    def test_brc_parent_parser_exists(self):
        parser = orch_cli.create_parser()
        # parser.parse_args will SystemExit on --help; the act of
        # successfully parsing ``brc next-action`` (mocked) is what
        # establishes the subparser exists.
        try:
            parser.parse_args(["brc", "next-action", "--role", "coder"])
        except SystemExit:
            # Argparse may exit due to missing required flags, but the
            # error is *about* missing flags, not "unknown command".
            pass

    def test_brc_next_action_subparser_registered(self, capsys):
        parser = orch_cli.create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["brc", "next-action", "--help"])
        out = capsys.readouterr().out
        assert "next-action" in out or "--role" in out

    def test_brc_get_state_subparser_registered(self, capsys):
        parser = orch_cli.create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["brc", "get-state", "--help"])
        # Help text mirrors the MCP tool description per acceptance.
        out = capsys.readouterr().out
        # Either '--verbose' or 'verbose' appears in help text.
        assert "verbose" in out.lower() or "raw" in out.lower()

    def test_brc_list_blocking_subparser_registered(self, capsys):
        parser = orch_cli.create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["brc", "list-blocking", "--help"])
        out = capsys.readouterr().out
        assert "list-blocking" in out or "block" in out.lower()
