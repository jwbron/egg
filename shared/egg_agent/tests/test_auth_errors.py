"""Tests for the non-retryable credential/quota failure classifier (#3373)."""

from __future__ import annotations

import pytest
from egg_agent.auth_errors import EX_AUTH_FATAL, is_auth_fatal_error


class TestExAuthFatalCode:
    def test_distinct_from_reserved_wrapper_and_signal_codes(self):
        # Must not collide with the consensus wrapper's reserved codes
        # (64 EX_USAGE / 75 EX_TEMPFAIL) or the SIGTERM code (143) the
        # kubernetes monitor treats as a clean stop, nor with success (0).
        assert EX_AUTH_FATAL not in (0, 64, 75, 143)
        assert EX_AUTH_FATAL == 77


class TestIsAuthFatalError:
    @pytest.mark.parametrize(
        "text",
        [
            # The #3373 repro — subscription weekly limit.
            "You've hit your weekly limit · resets Jul 3, 5am (UTC)",
            "Weekly limit reached",
            "usage limit exceeded for this account",
            # Auth / credential rejection shapes.
            "authentication_error: invalid bearer token",
            "authentication error",
            "Invalid API key provided",
            "invalid x-api-key",
            "Your OAuth token has expired",
            "oauth token expired",
            "Could not resolve authentication method",
            # Bare HTTP 401 (#3373 re-review note 4) — no structured body.
            "HTTP 401 Unauthorized",
            "API request failed with status 401",
            "http_401: token rejected",
            # Billing exhaustion.
            "Your credit balance is too low to access the API",
        ],
    )
    def test_matches_non_retryable_credential_failures(self, text):
        assert is_auth_fatal_error(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            None,
            "",
            # Transient throttling must NOT be classified fatal — it retries.
            "rate limit exceeded, please retry",
            "Error: 429 Too Many Requests",
            "Overloaded: the API is temporarily overloaded",
            # Unrelated failures stay on the ordinary abnormal path.
            "Agent reported error",
            "Timed out after 7200 seconds",
            "Tool execution failed: file not found",
            "connection reset by peer",
            # False-positive hardening (#3373 re-review note 3): incidental
            # agent prose that merely mentions these terms — e.g. an agent
            # editing auth_errors.py or summarising a credential discussion —
            # must NOT be misclassified as a credential-fatal stop. Only the
            # characteristic stop / status phrasing matches.
            "The weekly limit pattern lives in auth_errors.py",
            "Configure the usage limit in the dashboard before launch",
            "the endpoint returns 401 when the payload is malformed",
        ],
    )
    def test_does_not_match_transient_or_unrelated(self, text):
        assert is_auth_fatal_error(text) is False

    def test_case_insensitive(self):
        # Real stop phrasing, upper-cased — matching is case-insensitive.
        assert is_auth_fatal_error("YOU'VE HIT YOUR WEEKLY LIMIT") is True
        assert is_auth_fatal_error("Authentication_Error") is True
