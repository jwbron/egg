"""Tests for the TRANSIENT rate-limit / cap-wall classifier + exit code (#3364 PR C).

The credential-fatal classifier (``is_auth_fatal_error``) DELIBERATELY excludes
the bare-throttle signatures — HTTP 429, "rate limit", "overloaded", "too many
requests" — because a transient throttle self-heals once the rolling cap window
lifts (a retry fixes it, so it must never fast-fail as auth-fatal). PR C adds the
disjoint counterpart ``is_transient_rate_limit_error`` + a distinct exit code
``EX_RATE_LIMITED`` so the CLI can signal "throttle, pace me" separately from
"credential dead, stop" (``EX_AUTH_FATAL``) and from an ordinary crash.

These tests pin three things:

* the classifier matches the bare-throttle signatures and NOT auth-fatal /
  unrelated text;
* the DISJOINTNESS invariant (AC-C6): a weekly/usage cap that the API delivers
  ON a 429 stays auth-fatal and is never re-read as a bare transient throttle —
  the two predicates can never both be true for the same text; and
* the full CLI mapping (``egg_agent.__main__.main``): a bare throttle drives
  ``EX_RATE_LIMITED`` while a weekly cap (checked FIRST) still drives
  ``EX_AUTH_FATAL`` and an ordinary failure stays on the normal exit code.
"""

from __future__ import annotations

import pytest
from egg_agent.auth_errors import (
    EX_AUTH_FATAL,
    EX_RATE_LIMITED,
    is_auth_fatal_error,
    is_transient_rate_limit_error,
)

# The classifier + disjointness tests below need NO SDK and run everywhere. The
# end-to-end CLI-mapping tests drive the real ``egg_agent.__main__.main`` and
# only stub the SDK boundary (``claude_agent_sdk.query``), so they require the
# SDK to be importable — present in CI, absent in a bare sandbox. Guard the
# import so the SDK-free coverage still runs when the SDK is missing (mirrors
# the intent of test_client_auth_fatal_binding.py, but without skipping the
# whole module).
try:
    import claude_agent_sdk
    import egg_agent.__main__ as cli
    from claude_agent_sdk import ProcessError, ResultMessage

    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised only in a sandbox without the SDK
    _SDK_AVAILABLE = False


class TestExRateLimitedCode:
    def test_value_and_distinct_from_other_reserved_codes(self):
        # A dedicated exit code the orchestrator maps to the paced rate-limit
        # outcome. Must be distinct from success (0), the consensus wrapper's
        # reserved codes (64 EX_USAGE / 75 EX_TEMPFAIL), the SIGTERM clean-stop
        # code (143), and — critically — the auth-fatal code (77): a throttle
        # and a credential-fatal failure route to different supervisor paths.
        assert EX_RATE_LIMITED == 69
        assert EX_RATE_LIMITED not in (0, 64, 75, 143, EX_AUTH_FATAL)


class TestIsTransientRateLimitError:
    @pytest.mark.parametrize(
        "text",
        [
            # Bare HTTP 429 in its common shapes (as a standalone status token).
            "Error: 429 Too Many Requests",
            "API request failed with status 429",
            "HTTP 429 received",
            # "rate limit" wording the auth-fatal classifier intentionally
            # does NOT match.
            "rate limit exceeded, please retry",
            "You are being rate-limited",
            "rate_limit_error: too many concurrent requests",
            # Anthropic overload (529) — the real body carries a standalone
            # "Overloaded" alongside the "overloaded_error" type token.
            "Overloaded: the API is temporarily overloaded",
            '{"type":"overloaded_error","message":"Overloaded"}',
            "429 Too Many Requests",
        ],
    )
    def test_matches_bare_throttle_signatures(self, text):
        assert is_transient_rate_limit_error(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            None,
            "",
            # Auth-fatal wording must NOT classify as a transient throttle — it
            # is non-retryable and routes to EX_AUTH_FATAL, not the paced path.
            "You've hit your weekly limit · resets Jul 3, 5am (UTC)",
            "usage limit exceeded for this account",
            "authentication_error: invalid bearer token",
            "HTTP 401 Unauthorized",
            "Your credit balance is too low to access the API",
            # Unrelated failures stay on the ordinary abnormal path.
            "Tool execution failed: file not found",
            "connection reset by peer",
            "Timed out after 7200 seconds",
        ],
    )
    def test_does_not_match_auth_fatal_or_unrelated(self, text):
        assert is_transient_rate_limit_error(text) is False

    def test_case_insensitive(self):
        assert is_transient_rate_limit_error("RATE LIMIT EXCEEDED") is True
        assert is_transient_rate_limit_error("OVERLOADED") is True
        assert is_transient_rate_limit_error("HTTP 429") is True


class TestDisjointFromAuthFatal:
    """AC-C6: a weekly/usage cap and a bare throttle can never both classify true.

    The API can deliver a subscription weekly cap ON an HTTP 429. The
    disjointness guard means such a message stays auth-fatal (non-retryable)
    and is NEVER misread as a bare transient throttle, even though it carries a
    429 token that would otherwise match the rate-limit patterns.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "API error 429: You've hit your weekly limit · resets Jul 3, 5am (UTC)",
            "429 Too Many Requests — usage limit exceeded for this account",
            "HTTP 429: your credit balance is too low to access the API",
        ],
    )
    def test_weekly_cap_on_a_429_stays_auth_fatal_only(self, text):
        # Auth-fatal wins; the rate-limit predicate defers to it.
        assert is_auth_fatal_error(text) is True
        assert is_transient_rate_limit_error(text) is False

    @pytest.mark.parametrize(
        "text",
        [
            "Error: 429 Too Many Requests",
            "rate limit exceeded, please retry",
            "Overloaded: the API is temporarily overloaded",
            "You've hit your weekly limit · resets Jul 3, 5am (UTC)",
            "authentication_error: invalid bearer token",
            "Tool execution failed: file not found",
            "",
            None,
        ],
    )
    def test_predicates_are_never_both_true(self, text):
        # The load-bearing invariant, asserted directly across throttle,
        # auth-fatal, ordinary, and empty shapes: at most one predicate is true.
        assert not (is_auth_fatal_error(text) and is_transient_rate_limit_error(text))


# ---------------------------------------------------------------------------
# Full CLI mapping: SDK failure -> run_agent -> exit code.
#
# Mirrors test_client_auth_fatal_binding.py's end-to-end approach: only the SDK
# boundary (``claude_agent_sdk.query``) is stubbed, so the real ``run_agent``
# surfaces the error text into ``result.error`` and the real ``main()`` applies
# its predicates in order (auth-fatal FIRST, then transient rate-limit).
# ---------------------------------------------------------------------------


def _result_message(text: str | None) -> ResultMessage:
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


@pytest.mark.skipif(not _SDK_AVAILABLE, reason="claude_agent_sdk not installed")
class TestCliMapsThrottleToExRateLimited:
    def test_cli_maps_bare_429_process_error_to_ex_rate_limited(self, monkeypatch):
        _stub_cli_collaborators(monkeypatch)
        monkeypatch.setattr(
            claude_agent_sdk,
            "query",
            _query_raising(ProcessError("API error 429: too many requests", exit_code=1)),
        )
        assert cli.main() == EX_RATE_LIMITED

    def test_cli_maps_overloaded_to_ex_rate_limited(self, monkeypatch):
        _stub_cli_collaborators(monkeypatch)
        monkeypatch.setattr(
            claude_agent_sdk,
            "query",
            _query_raising(
                ProcessError(
                    'API error 529: {"type":"error","error":'
                    '{"type":"overloaded_error","message":"Overloaded"}}',
                    exit_code=1,
                )
            ),
        )
        assert cli.main() == EX_RATE_LIMITED

    def test_cli_maps_rate_limit_result_message_to_ex_rate_limited(self, monkeypatch):
        _stub_cli_collaborators(monkeypatch)
        monkeypatch.setattr(
            claude_agent_sdk,
            "query",
            _query_yielding(_result_message("rate limit exceeded, please retry")),
        )
        assert cli.main() == EX_RATE_LIMITED

    def test_cli_weekly_cap_on_429_still_maps_to_ex_auth_fatal(self, monkeypatch):
        """Auth-fatal is checked FIRST: a weekly cap riding on a 429 must return
        EX_AUTH_FATAL, not EX_RATE_LIMITED — otherwise the pipeline would pace a
        credential wall forever instead of surfacing the non-retryable stop."""
        _stub_cli_collaborators(monkeypatch)
        text = "API error 429: You've hit your weekly limit · resets Jul 3, 5am (UTC)"
        monkeypatch.setattr(
            claude_agent_sdk, "query", _query_raising(ProcessError(text, exit_code=1))
        )
        assert cli.main() == EX_AUTH_FATAL

    def test_cli_ordinary_failure_is_neither_rate_limited_nor_auth_fatal(self, monkeypatch):
        _stub_cli_collaborators(monkeypatch)
        monkeypatch.setattr(
            claude_agent_sdk,
            "query",
            _query_raising(ProcessError("Tool execution failed: file not found", exit_code=1)),
        )
        rc = cli.main()
        assert rc != EX_RATE_LIMITED
        assert rc != EX_AUTH_FATAL
