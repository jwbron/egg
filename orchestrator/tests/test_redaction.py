"""Unit tests for ``orchestrator/redaction.py``.

Covers the two public helpers — :func:`redact_env` and
:func:`redact_log_tail` — plus the private name matcher.  Mirrors the
redaction contract documented in
``docs/reference/mcp-deployment-tools.md`` and the diagnostic skills
(``skills/deployment-diagnose``, ``skills/agent-diagnose``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure orchestrator and shared dirs are on sys.path so imports work
# whether the test runs under the orchestrator harness or the repo-wide
# ``make test`` invocation.
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_config.constants import TEST_GATEWAY_PORT  # noqa: E402

from redaction import (  # noqa: E402
    REDACTION_PLACEHOLDER,
    _name_is_protected,
    redact_env,
    redact_log_tail,
)


class TestNameIsProtected:
    """The private predicate — explicit coverage keeps regressions cheap."""

    def test_exact_match_in_denylist(self):
        assert _name_is_protected("EGG_SESSION_TOKEN")
        assert _name_is_protected("EGG_LIFECYCLE_SECRET")
        assert _name_is_protected("GATEWAY_URL")

    def test_case_sensitive_denylist_lowercase_variant(self):
        # ``http_proxy`` is in the denylist as the lowercase form.
        assert _name_is_protected("http_proxy")
        assert _name_is_protected("HTTP_PROXY")

    def test_extra_named_credentials(self):
        assert _name_is_protected("GITHUB_TOKEN")
        assert _name_is_protected("GH_TOKEN")
        assert _name_is_protected("ANTHROPIC_API_KEY")
        assert _name_is_protected("CLAUDE_API_KEY")
        # Case-insensitive
        assert _name_is_protected("github_token")

    @pytest.mark.parametrize(
        "name",
        [
            "CUSTOM_TOKEN",
            "ANY_SECRET",
            "VENDOR_API_KEY",
            "custom_token",
            "any_secret",
        ],
    )
    def test_suffix_heuristics(self, name):
        assert _name_is_protected(name)

    def test_extra_protected_arg_accepted(self):
        assert _name_is_protected("MY_CUSTOM_ID", extra=["my_custom_id"])
        # Case-insensitive
        assert _name_is_protected("MY_CUSTOM_ID", extra=["MY_custom_ID"])

    @pytest.mark.parametrize(
        "name",
        ["HOME", "PATH", "USER", "PWD", "LANG", "EGG_REPO_PATH"],
    )
    def test_non_protected_names(self, name):
        assert _name_is_protected(name) is False


class TestRedactEnv:
    def test_redacts_lifecycle_secret(self):
        env = {"EGG_LIFECYCLE_SECRET": "super-secret", "HOME": "/home/egg"}
        out = redact_env(env)
        assert out["EGG_LIFECYCLE_SECRET"] == REDACTION_PLACEHOLDER
        assert out["HOME"] == "/home/egg"

    def test_redacts_all_protected_values(self):
        env = {
            "EGG_SESSION_TOKEN": "tok",
            "GATEWAY_URL": f"http://gw:{TEST_GATEWAY_PORT}",
            "GITHUB_TOKEN": "ghp_abc",
            "ANTHROPIC_API_KEY": "sk-xyz",
            "CUSTOM_TOKEN": "abc",
            "PATH": "/usr/bin",
        }
        out = redact_env(env)
        for key in (
            "EGG_SESSION_TOKEN",
            "GATEWAY_URL",
            "GITHUB_TOKEN",
            "ANTHROPIC_API_KEY",
            "CUSTOM_TOKEN",
        ):
            assert out[key] == REDACTION_PLACEHOLDER, key
        assert out["PATH"] == "/usr/bin"

    def test_input_not_mutated(self):
        env = {"EGG_LIFECYCLE_SECRET": "plain"}
        redact_env(env)
        assert env["EGG_LIFECYCLE_SECRET"] == "plain"

    def test_extra_protected_kwarg(self):
        env = {"MY_CUSTOM_ID": "sensitive", "PATH": "/usr/bin"}
        out = redact_env(env, extra_protected=["MY_CUSTOM_ID"])
        assert out["MY_CUSTOM_ID"] == REDACTION_PLACEHOLDER
        assert out["PATH"] == "/usr/bin"

    def test_empty_env(self):
        assert redact_env({}) == {}

    def test_non_string_values_preserved(self):
        env = {"SAFE_FLAG": True, "COUNT": 42, "LIST_ITEM": ["a", "b"]}
        out = redact_env(env)
        assert out["SAFE_FLAG"] is True
        assert out["COUNT"] == 42
        assert out["LIST_ITEM"] == ["a", "b"]

    def test_protected_non_string_values_still_redacted(self):
        env = {"EGG_LIFECYCLE_SECRET": 12345}
        out = redact_env(env)
        assert out["EGG_LIFECYCLE_SECRET"] == REDACTION_PLACEHOLDER


class TestRedactLogTail:
    def test_scrubs_bearer_jwt(self):
        text = (
            "Got Authorization: Bearer "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        redacted = redact_log_tail(text)
        assert REDACTION_PLACEHOLDER in redacted
        assert "eyJhbGciOi" not in redacted

    def test_scrubs_bare_jwt(self):
        # No Bearer prefix — still a JWT shape
        text = (
            "log line including "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
            " and more"
        )
        out = redact_log_tail(text)
        assert REDACTION_PLACEHOLDER in out
        assert "eyJhbGciOi" not in out

    def test_scrubs_openai_sk_keys(self):
        text = "Using API key sk-abcdefghijklmnopqrstuv1234567890 in request"
        out = redact_log_tail(text)
        assert "sk-abcdefghijklmnopqrstuv1234567890" not in out
        assert REDACTION_PLACEHOLDER in out

    def test_scrubs_github_ghp_tokens(self):
        for prefix in ("ghp_", "ghu_", "gho_", "ghs_", "ghr_"):
            text = f"token detected: {prefix}abcdefghijklmnopqrstuvwxyz1234"
            out = redact_log_tail(text)
            assert prefix not in out or REDACTION_PLACEHOLDER in out
            assert REDACTION_PLACEHOLDER in out

    def test_passes_through_safe_lines(self):
        text = "ran pytest, 42 passed in 0.12s"
        assert redact_log_tail(text) == text

    def test_passes_through_short_commit_sha(self):
        # Short identifiers should survive — operators rely on them.
        text = "Merged commit 8434d4d on branch egg/issue-1759-v3"
        assert "8434d4d" in redact_log_tail(text)

    def test_empty_and_none_safe(self):
        assert redact_log_tail("") == ""
        # Accepts None-like falsy guard
        assert redact_log_tail(None) is None  # type: ignore[arg-type]

    def test_multiple_secrets_in_one_line(self):
        # Each JWT segment must be >=5 chars to match ``_BEARER_JWT_RE``.
        text = (
            "Authorization: Bearer eyJhbGciOi.eyJzdWIiOi.signaturepart "
            "and key sk-abcdefghijklmnopqrstuvwxyz0123"
        )
        out = redact_log_tail(text)
        assert out.count(REDACTION_PLACEHOLDER) >= 2, (
            f"Expected both the Bearer JWT and the sk- key to be redacted, got: {out!r}"
        )

    def test_does_not_scrub_plain_words_starting_with_ey(self):
        # Plain text that happens to contain "ey" should not be altered.
        text = "eye of the storm"
        assert redact_log_tail(text) == text
