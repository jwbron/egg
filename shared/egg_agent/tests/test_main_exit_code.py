"""The agent CLI maps an auth-fatal failure to EX_AUTH_FATAL (#3373)."""

from __future__ import annotations

import egg_agent.__main__ as cli
from egg_agent.auth_errors import EX_AUTH_FATAL, EX_RATE_LIMITED
from egg_agent.result import AgentResult


def _stub_side_effects(monkeypatch):
    """Neutralise the CLI's non-mapping collaborators (resume/persist/measure)."""

    class _Decision:
        session_id = None

    monkeypatch.setattr(cli, "decide_resume_session", lambda **_kw: _Decision())
    monkeypatch.setattr(cli, "write_session_state", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "record_measurement", lambda **_kw: None)


def _run_with_result(monkeypatch, result: AgentResult) -> int:
    _stub_side_effects(monkeypatch)
    monkeypatch.setattr(cli, "run_agent", lambda *a, **kw: result)
    monkeypatch.setattr("sys.argv", ["egg_agent", "do the thing"])
    return cli.main()


def test_auth_fatal_failure_returns_ex_auth_fatal(monkeypatch):
    result = AgentResult(
        success=False,
        stdout="",
        stderr="You've hit your weekly limit · resets Jul 3, 5am (UTC)",
        returncode=1,
        error="You've hit your weekly limit · resets Jul 3, 5am (UTC)",
    )
    assert _run_with_result(monkeypatch, result) == EX_AUTH_FATAL


def test_ordinary_failure_returns_agent_returncode(monkeypatch):
    result = AgentResult(
        success=False,
        stdout="",
        stderr="boom",
        returncode=1,
        error="Tool execution failed",
    )
    assert _run_with_result(monkeypatch, result) == 1


def test_transient_failure_is_not_auth_fatal(monkeypatch):
    # #3364 PR C: a bare throttle now exits EX_RATE_LIMITED so the
    # orchestrator paces it across the cap window; the invariant under
    # test is unchanged — it must never classify as auth-fatal.
    result = AgentResult(
        success=False,
        stdout="",
        stderr="rate limit",
        returncode=1,
        error="rate limit exceeded, please retry",
    )
    rc = _run_with_result(monkeypatch, result)
    assert rc == EX_RATE_LIMITED
    assert rc != EX_AUTH_FATAL


def test_success_returns_zero(monkeypatch):
    result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
    assert _run_with_result(monkeypatch, result) == 0
