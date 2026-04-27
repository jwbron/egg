"""
Tests for gateway/jira_credentials.py.

Covers:
- mtime-based cache refresh (touching the secrets file triggers reload)
- missing-value typed exception (`JiraCredentialsUnavailable`)
- `basic_auth_header()` base64 shape and content
- `reload_jira_credentials()` clears the cache
- missing file → typed exception
- round-trip of all three required keys
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

# Modules are loaded via conftest.py.
import jira_credentials
import pytest
from jira_credentials import (
    JiraCredentials,
    JiraCredentialsManager,
    JiraCredentialsUnavailable,
)


def _write_secrets(path: Path, **kv: str) -> None:
    """Write key=value pairs to the secrets file."""
    lines = [f"{k}={v}" for k, v in kv.items() if v is not None]
    path.write_text("\n".join(lines) + "\n")


@pytest.fixture
def tmp_secrets(tmp_path: Path) -> Path:
    return tmp_path / "secrets.env"


class TestJiraCredentialsBasicAuthHeader:
    """Header encoding is explicit: base64(email:token)."""

    def test_header_encodes_username_and_token(self):
        creds = JiraCredentials(
            base_url="https://example.atlassian.net",
            username="alice@example.com",
            api_token="atk-abcdef1234567890",
        )
        header = creds.basic_auth_header()
        assert header.startswith("Basic ")

        decoded = base64.b64decode(header.removeprefix("Basic ")).decode("ascii")
        assert decoded == "alice@example.com:atk-abcdef1234567890"

    def test_header_handles_special_characters(self):
        creds = JiraCredentials(
            base_url="https://example.atlassian.net",
            username="a+b@example.com",
            api_token="token with spaces?",
        )
        header = creds.basic_auth_header()
        decoded = base64.b64decode(header.removeprefix("Basic ")).decode("ascii")
        assert decoded == "a+b@example.com:token with spaces?"


class TestJiraCredentialsLoading:
    """Loading from a secrets.env file."""

    def test_loads_all_three_required_keys(self, tmp_secrets: Path):
        _write_secrets(
            tmp_secrets,
            JIRA_BASE_URL="https://example.atlassian.net",
            JIRA_USERNAME="alice@example.com",
            JIRA_API_TOKEN="atk-xyz",
        )
        mgr = JiraCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://example.atlassian.net"
        assert creds.username == "alice@example.com"
        assert creds.api_token == "atk-xyz"

    def test_trailing_slash_on_base_url_is_stripped(self, tmp_secrets: Path):
        _write_secrets(
            tmp_secrets,
            JIRA_BASE_URL="https://example.atlassian.net/",
            JIRA_USERNAME="alice@example.com",
            JIRA_API_TOKEN="atk-xyz",
        )
        mgr = JiraCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://example.atlassian.net"

    @pytest.mark.parametrize(
        "missing_key",
        ["JIRA_BASE_URL", "JIRA_USERNAME", "JIRA_API_TOKEN"],
    )
    def test_missing_any_required_key_raises(self, tmp_secrets: Path, missing_key: str):
        values = {
            "JIRA_BASE_URL": "https://example.atlassian.net",
            "JIRA_USERNAME": "alice@example.com",
            "JIRA_API_TOKEN": "atk-xyz",
        }
        values.pop(missing_key)
        _write_secrets(tmp_secrets, **values)
        mgr = JiraCredentialsManager(tmp_secrets)
        with pytest.raises(JiraCredentialsUnavailable):
            mgr.get_credentials()

    def test_blank_values_are_treated_as_missing(self, tmp_secrets: Path):
        _write_secrets(
            tmp_secrets,
            JIRA_BASE_URL="https://example.atlassian.net",
            JIRA_USERNAME="",
            JIRA_API_TOKEN="atk-xyz",
        )
        mgr = JiraCredentialsManager(tmp_secrets)
        with pytest.raises(JiraCredentialsUnavailable):
            mgr.get_credentials()

    def test_missing_file_raises(self, tmp_path: Path):
        missing = tmp_path / "never-existed.env"
        mgr = JiraCredentialsManager(missing)
        with pytest.raises(JiraCredentialsUnavailable):
            mgr.get_credentials()


class TestJiraCredentialsCache:
    """mtime-based reload behaviour."""

    def test_cache_survives_unchanged_mtime(self, tmp_secrets: Path):
        _write_secrets(
            tmp_secrets,
            JIRA_BASE_URL="https://example.atlassian.net",
            JIRA_USERNAME="alice@example.com",
            JIRA_API_TOKEN="tok-v1",
        )
        mgr = JiraCredentialsManager(tmp_secrets)
        assert mgr.get_credentials().api_token == "tok-v1"

        # Internal cache should be populated without re-reading on the next
        # call (same mtime) — we assert by rewriting the FILE CONTENT WITHOUT
        # touching mtime.  If the loader truly caches by mtime, the old value
        # survives.
        _write_secrets_inplace_preserving_mtime(tmp_secrets, "tok-v2")
        # Reading again with same mtime → should still see cached v1 value.
        assert mgr.get_credentials().api_token == "tok-v1"

    def test_mtime_change_triggers_reload(self, tmp_secrets: Path):
        _write_secrets(
            tmp_secrets,
            JIRA_BASE_URL="https://example.atlassian.net",
            JIRA_USERNAME="alice@example.com",
            JIRA_API_TOKEN="tok-v1",
        )
        mgr = JiraCredentialsManager(tmp_secrets)
        assert mgr.get_credentials().api_token == "tok-v1"

        # Bump mtime by 2s to avoid filesystem granularity issues, then rewrite.
        new_mtime = tmp_secrets.stat().st_mtime + 2
        _write_secrets(
            tmp_secrets,
            JIRA_BASE_URL="https://example.atlassian.net",
            JIRA_USERNAME="alice@example.com",
            JIRA_API_TOKEN="tok-v2",
        )
        os.utime(tmp_secrets, (new_mtime, new_mtime))

        assert mgr.get_credentials().api_token == "tok-v2"

    def test_reload_forces_rereading_on_next_call(self, tmp_secrets: Path):
        _write_secrets(
            tmp_secrets,
            JIRA_BASE_URL="https://example.atlassian.net",
            JIRA_USERNAME="alice@example.com",
            JIRA_API_TOKEN="tok-v1",
        )
        mgr = JiraCredentialsManager(tmp_secrets)
        assert mgr.get_credentials().api_token == "tok-v1"

        # Rewrite WITHOUT changing mtime; cache should hold without reload().
        _write_secrets_inplace_preserving_mtime(tmp_secrets, "tok-v2")
        assert mgr.get_credentials().api_token == "tok-v1"  # cached

        mgr.reload()
        assert mgr.get_credentials().api_token == "tok-v2"  # re-read


class TestModuleLevelSingleton:
    """`reload_jira_credentials` and `reset_jira_credentials_manager` helpers."""

    def test_reset_manager_drops_singleton(self, tmp_secrets: Path, monkeypatch):
        _write_secrets(
            tmp_secrets,
            JIRA_BASE_URL="https://example.atlassian.net",
            JIRA_USERNAME="alice@example.com",
            JIRA_API_TOKEN="tok-v1",
        )
        monkeypatch.setattr(jira_credentials, "SECRETS_PATH", tmp_secrets)
        jira_credentials.reset_jira_credentials_manager()

        first = jira_credentials.get_jira_credentials_manager()
        second = jira_credentials.get_jira_credentials_manager()
        assert first is second

        jira_credentials.reset_jira_credentials_manager()
        third = jira_credentials.get_jira_credentials_manager()
        assert third is not first

    def test_reload_jira_credentials_clears_singleton_cache(self, tmp_secrets: Path, monkeypatch):
        _write_secrets(
            tmp_secrets,
            JIRA_BASE_URL="https://example.atlassian.net",
            JIRA_USERNAME="alice@example.com",
            JIRA_API_TOKEN="tok-v1",
        )
        monkeypatch.setattr(jira_credentials, "SECRETS_PATH", tmp_secrets)
        jira_credentials.reset_jira_credentials_manager()

        # Point the newly-created manager at our tmp file.
        mgr = jira_credentials.get_jira_credentials_manager()
        mgr._secrets_path = tmp_secrets
        assert jira_credentials.get_jira_credentials().api_token == "tok-v1"

        # Rewrite file WITHOUT changing mtime.
        _write_secrets_inplace_preserving_mtime(tmp_secrets, "tok-v2")
        # Without reload, still returns cached value.
        assert jira_credentials.get_jira_credentials().api_token == "tok-v1"

        jira_credentials.reload_jira_credentials()
        assert jira_credentials.get_jira_credentials().api_token == "tok-v2"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _write_secrets_inplace_preserving_mtime(path: Path, token: str) -> None:
    """Rewrite the secrets file but restore the original mtime.

    Used to prove that the cache ONLY invalidates on an mtime change — a bare
    content rewrite must not trigger a reload.
    """
    original_mtime = path.stat().st_mtime
    _write_secrets(
        path,
        JIRA_BASE_URL="https://example.atlassian.net",
        JIRA_USERNAME="alice@example.com",
        JIRA_API_TOKEN=token,
    )
    os.utime(path, (original_mtime, original_mtime))
    # Guard against flaky filesystems that bump the mtime anyway.
    time.sleep(0.001)


# -----------------------------------------------------------------------------
# Risk R11 / Task 4-1b — shared ATLASSIAN_* credential precedence (#1931)
# -----------------------------------------------------------------------------


def _write_kv(path: Path, **kv: str | None) -> None:
    """Write key=value pairs to ``path``, omitting None values."""
    lines = [f"{k}={v}" for k, v in kv.items() if v is not None]
    path.write_text("\n".join(lines) + "\n")


class TestAtlassianPrecedence:
    """Per-key fallback: ATLASSIAN_* wins; JIRA_* is the back-compat path.

    The Confluence wrapper (#1931) reuses the same secrets.env, so the Jira
    loader must honour ``ATLASSIAN_*`` first or operators who switch to the
    shared triple silently break Jira.  These tests pin the per-key fallback
    matrix called for in plan-task 4-1b / risk R11.
    """

    def test_atlassian_only_resolves(self, tmp_secrets: Path):
        _write_kv(
            tmp_secrets,
            ATLASSIAN_BASE_URL="https://example.atlassian.net",
            ATLASSIAN_USERNAME="alice@example.com",
            ATLASSIAN_API_TOKEN="atk-shared",
        )
        mgr = JiraCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        # Jira sits at the bare origin (no ``/wiki`` suffix).
        assert creds.base_url == "https://example.atlassian.net"
        assert creds.username == "alice@example.com"
        assert creds.api_token == "atk-shared"

    def test_jira_only_still_works(self, tmp_secrets: Path):
        """Existing deployments setting only ``JIRA_*`` keys must keep working
        without changes."""
        _write_kv(
            tmp_secrets,
            JIRA_BASE_URL="https://example.atlassian.net",
            JIRA_USERNAME="alice@example.com",
            JIRA_API_TOKEN="atk-jira",
        )
        mgr = JiraCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://example.atlassian.net"
        assert creds.username == "alice@example.com"
        assert creds.api_token == "atk-jira"

    def test_atlassian_wins_over_jira_per_key(self, tmp_secrets: Path):
        """When both prefixes are set, ATLASSIAN_* wins for each key."""
        _write_kv(
            tmp_secrets,
            ATLASSIAN_BASE_URL="https://atlassian.atlassian.net",
            ATLASSIAN_USERNAME="atlassian@example.com",
            ATLASSIAN_API_TOKEN="atk-atlassian",
            JIRA_BASE_URL="https://jira.atlassian.net",
            JIRA_USERNAME="jira@example.com",
            JIRA_API_TOKEN="atk-jira",
        )
        mgr = JiraCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://atlassian.atlassian.net"
        assert creds.username == "atlassian@example.com"
        assert creds.api_token == "atk-atlassian"

    def test_per_key_mixed_fallback(self, tmp_secrets: Path):
        """ATLASSIAN_USERNAME + JIRA_BASE_URL + JIRA_API_TOKEN → all resolve."""
        _write_kv(
            tmp_secrets,
            ATLASSIAN_USERNAME="alice@example.com",
            JIRA_BASE_URL="https://example.atlassian.net",
            JIRA_API_TOKEN="atk-mixed",
        )
        mgr = JiraCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://example.atlassian.net"
        assert creds.username == "alice@example.com"
        assert creds.api_token == "atk-mixed"

    def test_blank_atlassian_keys_fall_through_to_jira(self, tmp_secrets: Path):
        _write_kv(
            tmp_secrets,
            ATLASSIAN_BASE_URL="",
            ATLASSIAN_USERNAME="",
            ATLASSIAN_API_TOKEN="",
            JIRA_BASE_URL="https://example.atlassian.net",
            JIRA_USERNAME="alice@example.com",
            JIRA_API_TOKEN="atk-jira",
        )
        mgr = JiraCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://example.atlassian.net"
        assert creds.username == "alice@example.com"
        assert creds.api_token == "atk-jira"

    def test_all_six_missing_raises(self, tmp_secrets: Path):
        _write_kv(tmp_secrets, OTHER_KEY="value")
        mgr = JiraCredentialsManager(tmp_secrets)
        with pytest.raises(JiraCredentialsUnavailable):
            mgr.get_credentials()

    def test_atlassian_base_url_no_wiki_suffix(self, tmp_secrets: Path):
        """Jira lives at the bare Atlassian origin — no /wiki appended.
        (Confluence is the one that needs /wiki.)"""
        _write_kv(
            tmp_secrets,
            ATLASSIAN_BASE_URL="https://example.atlassian.net",
            ATLASSIAN_USERNAME="alice@example.com",
            ATLASSIAN_API_TOKEN="atk",
        )
        mgr = JiraCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://example.atlassian.net"
        assert "/wiki" not in creds.base_url
