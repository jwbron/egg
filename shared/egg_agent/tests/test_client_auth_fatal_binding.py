"""End-to-end binding: an SDK credential failure reaches ``EX_AUTH_FATAL`` (#3373).

The fast-fail feature fires off exactly one predicate —
``is_auth_fatal_error(result.error)`` in :mod:`egg_agent.__main__`. Every other
test in this area feeds that predicate a *hand-built* error string, which proves
the classifier but says nothing about whether the production path actually binds
an upstream credential failure into ``AgentResult.error``. If
:func:`run_agent` does not surface the SDK's credential text into ``result.error``,
the whole feature is a silent no-op and those unit tests still pass.

These tests close that gap. They drive a representative SDK failure *shape*
through the real :func:`run_agent` code path (only ``claude_agent_sdk.query`` is
stubbed — the SDK boundary egg does not own) and assert the credential cause
lands in ``result.error`` matching the classifier, and that the CLI maps it to
``EX_AUTH_FATAL``. The two production binding sites in ``client.py`` are covered:

  * the ``ResultMessage.is_error`` branch (``error = message.result``), e.g. a
    subscription weekly-limit stop delivered as a result message, and
  * the ``ProcessError`` / ``ClaudeSDKError`` branch (``error = str(e)``), e.g.
    a 401 ``authentication_error`` or an exhausted-credit-balance API error.

A negative case pins the reviewer's concern that an *empty* error message must
NOT be misclassified fatal (it stays on the ordinary retryable path).
"""

from __future__ import annotations

import claude_agent_sdk
import egg_agent.__main__ as cli
import pytest
from claude_agent_sdk import ClaudeSDKError, ProcessError, ResultMessage
from egg_agent.auth_errors import EX_AUTH_FATAL, is_auth_fatal_error
from egg_agent.client import run_agent
from egg_agent.result import AgentResult


def _result_message(text: str | None) -> ResultMessage:
    """A minimal ``is_error`` ResultMessage, as the SDK delivers a failed run."""
    return ResultMessage(
        subtype="error",
        duration_ms=10,
        duration_api_ms=5,
        is_error=True,
        num_turns=1,
        session_id="sess-1",
        total_cost_usd=0.0,
        usage=None,
        result=text,
    )


def _query_yielding(message: ResultMessage):
    async def _q(*, prompt, options):  # noqa: ANN001, ARG001 — SDK kwargs
        yield message

    return _q


def _query_raising(exc: Exception):
    async def _q(*, prompt, options):  # noqa: ANN001, ARG001 — SDK kwargs
        raise exc
        yield  # pragma: no cover — generator marker, never reached

    return _q


def _stub_cli_collaborators(monkeypatch):
    """Neutralise the CLI's non-mapping collaborators (resume/persist/measure)."""

    class _Decision:
        session_id = None

    monkeypatch.setattr(cli, "decide_resume_session", lambda **_kw: _Decision())
    monkeypatch.setattr(cli, "write_session_state", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "record_measurement", lambda **_kw: None)
    monkeypatch.setattr("sys.argv", ["egg_agent", "do the thing"])


# --- ResultMessage.is_error branch (error = message.result) -----------------


def test_result_message_weekly_limit_binds_to_error_and_classifies(monkeypatch):
    """A subscription weekly-limit stop delivered as a ResultMessage surfaces
    its text into ``result.error`` and is classified auth-fatal."""
    text = "You've hit your weekly limit · resets Jul 3, 5am (UTC)"
    monkeypatch.setattr(claude_agent_sdk, "query", _query_yielding(_result_message(text)))

    result = run_agent("do the thing")

    assert result.success is False
    assert result.error and "weekly limit" in result.error
    assert is_auth_fatal_error(result.error) is True


def test_result_message_empty_body_is_not_misclassified(monkeypatch):
    """Reviewer's concern: an ``is_error`` result with an empty body becomes the
    literal ``"Agent reported error"`` — which matches no pattern, so it stays
    on the ordinary retryable path rather than being misclassified fatal."""
    monkeypatch.setattr(claude_agent_sdk, "query", _query_yielding(_result_message("")))

    result = run_agent("do the thing")

    assert result.success is False
    assert result.error == "Agent reported error"
    assert is_auth_fatal_error(result.error) is False


# --- ProcessError / ClaudeSDKError branch (error = str(e)) ------------------


@pytest.mark.parametrize(
    "exc",
    [
        ProcessError(
            'API error 401: {"type":"error","error":{"type":"authentication_error",'
            '"message":"invalid bearer token"}}',
            exit_code=1,
        ),
        ClaudeSDKError(
            "Your credit balance is too low to access the Anthropic API. "
            "Please go to Plans & Billing to upgrade or purchase credits."
        ),
    ],
    ids=["authentication_error_401", "credit_balance_too_low"],
)
def test_process_error_credential_shapes_bind_and_classify(monkeypatch, exc):
    """A credential/billing failure raised by the SDK surfaces ``str(e)`` into
    ``result.error`` and is classified auth-fatal."""
    monkeypatch.setattr(claude_agent_sdk, "query", _query_raising(exc))

    result = run_agent("do the thing")

    assert result.success is False
    assert is_auth_fatal_error(result.error) is True


def test_process_error_transient_overload_is_not_fatal(monkeypatch):
    """A transient overload raised by the SDK must NOT classify fatal — it
    stays on the backoff-and-respawn path."""
    monkeypatch.setattr(
        claude_agent_sdk,
        "query",
        _query_raising(ProcessError("API error 529: overloaded_error", exit_code=1)),
    )

    result = run_agent("do the thing")

    assert result.success is False
    assert is_auth_fatal_error(result.error) is False


# --- Full CLI mapping: SDK failure -> run_agent -> EX_AUTH_FATAL ------------


def test_cli_maps_sdk_weekly_limit_to_ex_auth_fatal(monkeypatch):
    """The whole trigger, end-to-end through the real CLI: a weekly-limit
    ResultMessage drives ``cli.main()`` to return ``EX_AUTH_FATAL`` — the code
    k8s reads and the orchestrator fast-fails on."""
    _stub_cli_collaborators(monkeypatch)
    text = "You've hit your weekly limit · resets Jul 3, 5am (UTC)"
    monkeypatch.setattr(claude_agent_sdk, "query", _query_yielding(_result_message(text)))

    assert cli.main() == EX_AUTH_FATAL


def test_cli_maps_sdk_401_to_ex_auth_fatal(monkeypatch):
    """End-to-end through the CLI for the ProcessError 401 shape."""
    _stub_cli_collaborators(monkeypatch)
    monkeypatch.setattr(
        claude_agent_sdk,
        "query",
        _query_raising(
            ProcessError("API error 401: authentication_error: invalid bearer token", exit_code=1)
        ),
    )

    assert cli.main() == EX_AUTH_FATAL


def test_cli_ordinary_sdk_failure_is_not_ex_auth_fatal(monkeypatch):
    """An ordinary tool/runtime failure stays on the normal exit code, not
    EX_AUTH_FATAL — the orchestrator keeps retrying it."""
    _stub_cli_collaborators(monkeypatch)
    monkeypatch.setattr(
        claude_agent_sdk,
        "query",
        _query_raising(ProcessError("Tool execution failed: file not found", exit_code=1)),
    )

    rc = cli.main()
    assert rc != EX_AUTH_FATAL


def test_run_agent_result_error_is_what_main_classifies(monkeypatch):
    """Belt-and-braces: the exact ``result.error`` the CLI sees is the one the
    classifier is fed — no transformation between ``run_agent`` and the
    ``is_auth_fatal_error(result.error)`` predicate in ``main``."""
    text = "usage limit exceeded for this account"
    monkeypatch.setattr(claude_agent_sdk, "query", _query_yielding(_result_message(text)))

    result = run_agent("do the thing")
    # The predicate main() applies, applied to the real produced error.
    assert isinstance(result, AgentResult)
    assert is_auth_fatal_error(result.error) is True
