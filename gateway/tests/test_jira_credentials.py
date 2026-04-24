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
