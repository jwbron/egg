"""Drift guard for the SDLC skill's standalone wait-status launcher (#2971).

``skills/sdlc/bin/wait-status`` is a self-contained, pure-stdlib vendored
copy of the ``egg-orch pipeline wait-status`` code path in
``egg_lib.orch_cli`` — it carries its own ``cmd_pipeline_wait_status`` so
the skill can run from any working directory with no ``.venv``,
``PYTHONPATH``, or egg checkout.

Because it's a fork, it can drift from the source of truth. This test
pins the two together: it drives **both** ``cmd_pipeline_wait_status``
implementations through identical mocked ``/status/wait`` response
matrices and asserts identical **stdout JSON-lines and exit codes** —
the wire contract the Monitor tool consumes (per
``docs/reference/agent-wait-patterns.md`` §7). stderr is human-facing
diagnostics and intentionally *not* part of the compared contract (the
standalone launcher carries a friendlier unreachable-orchestrator hint).

If you change the loop's observable behavior in ``orch_cli``, port the
same change to ``skills/sdlc/bin/wait-status`` until this test passes.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SANDBOX_PATH = str(_REPO_ROOT / "sandbox")
if _SANDBOX_PATH not in sys.path:
    sys.path.insert(0, _SANDBOX_PATH)

from egg_lib import orch_cli  # noqa: E402

_SKILL_SCRIPT = _REPO_ROOT / "skills" / "sdlc" / "bin" / "wait-status"


def _load_skill_module() -> Any:
    """Import the extension-less standalone script as a module."""
    loader = importlib.machinery.SourceFileLoader("skill_wait_status", str(_SKILL_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


skill_wait_status = _load_skill_module()


# ---------------------------------------------------------------------------
# Envelope fixtures (plain dicts — module-agnostic)
# ---------------------------------------------------------------------------


def _no_change(cursor: str = "msg:|evt:0") -> dict[str, Any]:
    return {"success": True, "data": {"changed": False, "no_change": True, "cursor": cursor}}


def _phase_event(cursor: str, event_type: str = "phase.started") -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "changed": True,
            "trigger": "event",
            "event_type": event_type,
            "cursor": cursor,
            "current_phase": "implement",
            "status": "running",
            "phase_elapsed_seconds": 5,
            "concurrent": {"consensus": {"is_complete": False}},
        },
    }


def _terminal_event(cursor: str, event_type: str = "pipeline.completed") -> dict[str, Any]:
    body = _phase_event(cursor, event_type=event_type)
    body["data"]["status"] = "complete"
    return body


def _message_event(cursor: str, message_type: str = "OVERSEER_ALERT") -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "changed": True,
            "trigger": "message",
            "messages": [{"message_type": message_type, "subject": "stall"}],
            "cursor": cursor,
            "current_phase": "implement",
            "status": "running",
        },
    }


def _no_change_terminal(cursor: str, status: str) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "changed": False,
            "no_change": True,
            "cursor": cursor,
            "current_phase": "implement",
            "status": status,
        },
    }


# A scenario builds its response list from the *module's own* ApiError class
# (each module defines its own, and each loop only catches its own).
ResponsesFactory = Callable[[type], list[Any]]


def _run(
    module: Any, responses: list[Any], capsys: pytest.CaptureFixture[str], **ns_over: Any
) -> tuple[int, str]:
    """Run a module's ``cmd_pipeline_wait_status`` and return (rc, stdout)."""
    ns = argparse.Namespace(pipeline_id="issue-42", since="", inner_timeout=1, max_iterations=None)
    for key, value in ns_over.items():
        setattr(ns, key, value)
    # ``patch("time.sleep")`` covers both modules: orch_cli calls ``_time.sleep``
    # and the standalone calls ``time.sleep`` — both resolve ``sleep`` on the
    # same stdlib ``time`` module at call time.
    with patch.object(module, "api_request", side_effect=list(responses)), patch("time.sleep"):
        rc = module.cmd_pipeline_wait_status(ns)
    return rc, capsys.readouterr().out


# ---------------------------------------------------------------------------
# Scenario matrix — exercised against BOTH modules for parity
# ---------------------------------------------------------------------------

_SCENARIOS: dict[str, tuple[ResponsesFactory, dict[str, Any]]] = {
    "terminal_only": (lambda _Err: [_terminal_event("msg:|evt:1")], {"max_iterations": 5}),
    "phase_then_terminal": (
        lambda _Err: [_phase_event("msg:|evt:1"), _terminal_event("msg:|evt:2")],
        {"max_iterations": 5},
    ),
    "no_change_then_terminal": (
        lambda _Err: [_no_change("msg:|evt:1"), _terminal_event("msg:|evt:2")],
        {"max_iterations": 5},
    ),
    "message_then_terminal": (
        lambda _Err: [_message_event("msg:m-1|evt:1"), _terminal_event("msg:|evt:2")],
        {"max_iterations": 5},
    ),
    "explicit_since_threaded": (
        lambda _Err: [_phase_event("msg:|evt:7"), _terminal_event("msg:|evt:8")],
        {"since": "msg:abc|evt:5", "max_iterations": 5},
    ),
    "http_400_permanent": (
        lambda Err: [Err("Invalid 'since' cursor", status_code=400)],
        {"max_iterations": 5},
    ),
    "http_404_permanent": (
        lambda Err: [Err("Pipeline issue-42 not found", status_code=404)],
        {"max_iterations": 5},
    ),
    "http_403_permanent": (
        lambda Err: [Err("forbidden", status_code=403)],
        {"max_iterations": 5},
    ),
    "transient_then_terminal": (
        lambda Err: [
            Err("server error", status_code=503),
            Err("server error", status_code=503),
            _terminal_event("msg:|evt:9"),
        ],
        {"max_iterations": 10},
    ),
    "transient_budget_exhausted": (
        lambda Err: [Err("server error", status_code=500)] * 200,
        {"max_iterations": 200},
    ),
    "network_error_then_terminal": (
        lambda Err: [Err("Network error: connection refused"), _terminal_event("msg:|evt:1")],
        {"max_iterations": 5},
    ),
    "path_b_terminal_failed": (
        lambda _Err: [_no_change_terminal("msg:|evt:0", "failed")],
        {"max_iterations": 5},
    ),
    "path_b_terminal_complete": (
        lambda _Err: [_no_change_terminal("msg:|evt:0", "complete")],
        {"max_iterations": 5},
    ),
    "path_b_terminal_cancelled": (
        lambda _Err: [_no_change_terminal("msg:|evt:0", "cancelled")],
        {"max_iterations": 5},
    ),
    "path_b_running_keeps_looping": (
        lambda _Err: [
            _no_change_terminal("msg:|evt:0", "running"),
            _no_change_terminal("msg:|evt:1", "running"),
            _terminal_event("msg:|evt:9"),
        ],
        {"max_iterations": 5},
    ),
    "max_iterations_cap": (
        lambda _Err: [_no_change("msg:|evt:1"), _no_change("msg:|evt:2"), _no_change("msg:|evt:3")],
        {"max_iterations": 2},
    ),
}


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_standalone_matches_orch_cli(scenario: str, capsys: pytest.CaptureFixture[str]) -> None:
    """The vendored launcher emits byte-identical JSON-lines and the same
    exit code as ``egg_lib.orch_cli`` across the full contract matrix."""
    make_responses, ns_over = _SCENARIOS[scenario]

    rc_ref, out_ref = _run(orch_cli, make_responses(orch_cli.ApiError), capsys, **ns_over)
    rc_skill, out_skill = _run(
        skill_wait_status, make_responses(skill_wait_status.ApiError), capsys, **ns_over
    )

    assert rc_skill == rc_ref, f"exit code drift in {scenario}: skill={rc_skill} ref={rc_ref}"
    assert out_skill == out_ref, f"stdout drift in {scenario}"


# ---------------------------------------------------------------------------
# Absolute spot-checks — pin the actual contract values, not just parity
# ---------------------------------------------------------------------------


class TestAbsoluteContract:
    def test_terminal_returns_zero_one_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        import json

        rc, out = _run(skill_wait_status, [_terminal_event("msg:|evt:1")], capsys, max_iterations=5)
        assert rc == 0
        lines = out.strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["event_type"] == "pipeline.completed"

    def test_4xx_returns_three(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc, _ = _run(
            skill_wait_status,
            [skill_wait_status.ApiError("bad", status_code=400)],
            capsys,
            max_iterations=5,
        )
        assert rc == 3

    def test_transient_budget_returns_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc, _ = _run(
            skill_wait_status,
            [skill_wait_status.ApiError("5xx", status_code=500)] * 200,
            capsys,
            max_iterations=200,
        )
        assert rc == 2

    def test_path_b_terminal_emits_synthetic_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        import json

        rc, out = _run(
            skill_wait_status,
            [_no_change_terminal("msg:|evt:0", "failed")],
            capsys,
            max_iterations=5,
        )
        assert rc == 0
        line = json.loads(out.strip())
        assert line["trigger"] == "synthetic-terminal"
        assert line["event_type"] == "pipeline.failed"


# ---------------------------------------------------------------------------
# Self-contained property: no egg-package dependency, runnable as a script
# ---------------------------------------------------------------------------


class TestSelfContained:
    def test_distinct_module_from_orch_cli(self) -> None:
        assert skill_wait_status is not orch_cli
        assert skill_wait_status.__file__ is None or "skills/sdlc/bin/wait-status" in str(
            _SKILL_SCRIPT
        )

    def test_no_egg_imports(self) -> None:
        """The whole point of #2971: the launcher must not import any egg
        package (egg_lib, egg_config, egg_agent_tools, …) — that's what let
        it require a checkout + venv. Pin it to stdlib-only imports."""
        src = _SKILL_SCRIPT.read_text()
        offending = re.findall(r"^\s*(?:from|import)\s+egg\w*", src, re.MULTILINE)
        assert offending == [], f"standalone launcher imports egg packages: {offending}"

    def test_has_python3_shebang_and_executable(self) -> None:
        src = _SKILL_SCRIPT.read_text()
        assert src.startswith("#!/usr/bin/env python3"), "missing python3 shebang"
        import os

        assert os.access(_SKILL_SCRIPT, os.X_OK), "launcher is not executable"
