"""The agent CLI maps a session-budget expiry to EX_SESSION_TIMEOUT (#3658).

An expiry used to return the SDK path's ``returncode=-1`` unchanged, which the
orchestrator read as an ordinary crash: it fed the >=10
``agent-invocation-fail-streak`` halt and the propose-arm ``AGENT_FAILED``
escalation, making a healthy agent that simply ran long indistinguishable from a
crash loop. These tests pin the distinct exit code, the checkpoint that rides
with it, and the deadline surfaces the same entry point installs.
"""

from __future__ import annotations

import egg_agent.__main__ as cli
from egg_agent.auth_errors import EX_AUTH_FATAL, EX_RATE_LIMITED, EX_SESSION_TIMEOUT
from egg_agent.result import AgentResult
from egg_agent.session_deadline import BUDGET_SECONDS_ENV, DEADLINE_EPOCH_ENV


def _stub_side_effects(monkeypatch):
    """Neutralise the CLI's non-mapping collaborators (resume/persist/measure)."""

    class _Decision:
        session_id = None

    monkeypatch.setattr(cli, "decide_resume_session", lambda **_kw: _Decision())
    monkeypatch.setattr(cli, "write_session_state", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "record_measurement", lambda **_kw: None)


def _run_with_result(monkeypatch, result: AgentResult, argv=None) -> int:
    _stub_side_effects(monkeypatch)
    monkeypatch.setattr(cli, "run_agent", lambda *a, **kw: result)
    monkeypatch.setattr("sys.argv", argv or ["egg_agent", "do the thing"])
    return cli.main()


def _timeout_result() -> AgentResult:
    return AgentResult(
        success=False,
        stdout="partial work",
        stderr="",
        returncode=-1,
        error="Timed out after 7200 seconds",
        timed_out=True,
    )


def test_timeout_returns_ex_session_timeout(monkeypatch):
    monkeypatch.setattr(cli, "checkpoint_working_tree", lambda *a, **kw: None)
    rc = _run_with_result(monkeypatch, _timeout_result())
    assert rc == EX_SESSION_TIMEOUT
    # The whole point is that it is none of the other classifications, and in
    # particular not the generic non-zero the streak path counts.
    assert rc not in (EX_AUTH_FATAL, EX_RATE_LIMITED, 0, -1, 1)


def test_timeout_checkpoints_the_working_tree_before_exiting(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "checkpoint_working_tree", lambda *a, **kw: calls.append(True))

    assert _run_with_result(monkeypatch, _timeout_result()) == EX_SESSION_TIMEOUT
    assert calls == [True]


def test_non_timeout_failures_do_not_checkpoint(monkeypatch):
    """Only the boundary snapshots; an ordinary crash keeps today's behaviour."""
    calls = []
    monkeypatch.setattr(cli, "checkpoint_working_tree", lambda *a, **kw: calls.append(True))
    result = AgentResult(
        success=False, stdout="", stderr="boom", returncode=1, error="Tool execution failed"
    )

    assert _run_with_result(monkeypatch, result) == 1
    assert calls == []


def test_timeout_classification_is_structural_not_textual(monkeypatch):
    """A message that merely *reads* like a timeout is still an ordinary crash.

    The flag is the contract; the text is not. An agent whose final message
    happens to mention timing out must not be handed a free session boundary.
    """
    monkeypatch.setattr(cli, "checkpoint_working_tree", lambda *a, **kw: None)
    result = AgentResult(
        success=False,
        stdout="",
        stderr="",
        returncode=1,
        error="the build script timed out after 30 seconds",
    )

    assert _run_with_result(monkeypatch, result) == 1


def test_timeout_wins_over_a_coincidental_throttle_message(monkeypatch):
    """A cancelled call whose last error mentioned a 429 is still a boundary.

    Ordering matters here: routing it to the rate-limit path would pace the
    respawn across a cap window that is not shut, stalling an agent whose only
    problem was running out of clock.
    """
    monkeypatch.setattr(cli, "checkpoint_working_tree", lambda *a, **kw: None)
    result = AgentResult(
        success=False,
        stdout="",
        stderr="",
        returncode=-1,
        error="Timed out after 7200 seconds (last API error: 429 rate limit)",
        timed_out=True,
    )

    assert _run_with_result(monkeypatch, result) == EX_SESSION_TIMEOUT


def test_cli_publishes_the_deadline_before_invoking_the_agent(monkeypatch):
    """The budget must reach the agent's prompt and its subprocess environment."""
    seen = {}

    def _capture(prompt, **kwargs):
        seen["prompt"] = prompt
        seen["timeout"] = kwargs.get("timeout")
        return AgentResult(success=True, stdout="ok", stderr="", returncode=0)

    _stub_side_effects(monkeypatch)
    monkeypatch.setattr(cli, "run_agent", _capture)
    monkeypatch.delenv(DEADLINE_EPOCH_ENV, raising=False)
    monkeypatch.setattr("sys.argv", ["egg_agent", "--timeout", "600", "handle this BRC event"])

    assert cli.main() == 0
    # The banner is appended to the caller's prompt, not substituted for it —
    # and it goes at the END so a varying timestamp can never sit ahead of a
    # cacheable shared prefix.
    assert "Session budget" in seen["prompt"]
    assert seen["prompt"].startswith("handle this BRC event")
    # The agent is told about the SAME budget run_agent enforces — a drift
    # between the two would be worse than saying nothing at all.
    assert "600s" in seen["prompt"]
    assert seen["timeout"] == 600

    import os

    assert os.environ[BUDGET_SECONDS_ENV] == "600"
