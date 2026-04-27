"""
Tests for ``gateway/confluence_credentials.py``.

Covers Phase 1 / Task 4-1 acceptance criteria:

- ``basic_auth_header()`` base64 shape (email:token).
- mtime-based cache refresh — touching the file invalidates the cache.
- ``reload_confluence_credentials()`` clears the cache.
- Missing values raise ``ConfluenceCredentialsUnavailable``.
- F1 credential precedence: ``ATLASSIAN_*`` over ``CONFLUENCE_*`` per-key.
- Base-URL derivation: ``ATLASSIAN_BASE_URL`` gets ``/wiki`` appended;
  ``CONFLUENCE_BASE_URL`` is used verbatim.
- Mixed per-key shapes (``ATLASSIAN_USERNAME`` + ``CONFLUENCE_BASE_URL``).
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

# Modules are loaded via conftest.py.
import confluence_credentials
import pytest
from confluence_credentials import (
    ConfluenceCredentials,
    ConfluenceCredentialsManager,
    ConfluenceCredentialsUnavailable,
)


def _write_secrets(path: Path, **kv: str | None) -> None:
    """Write key=value pairs to the secrets file, omitting None values."""
    lines = [f"{k}={v}" for k, v in kv.items() if v is not None]
    path.write_text("\n".join(lines) + "\n")


@pytest.fixture
def tmp_secrets(tmp_path: Path) -> Path:
    return tmp_path / "secrets.env"


# ---------------------------------------------------------------------------
# basic_auth_header()
# ---------------------------------------------------------------------------


class TestBasicAuthHeader:
    def test_header_encodes_username_and_token(self):
        creds = ConfluenceCredentials(
            base_url="https://example.atlassian.net/wiki",
            username="alice@example.com",
            api_token="atk-abcdef1234567890",
        )
        header = creds.basic_auth_header()
        assert header.startswith("Basic ")
        decoded = base64.b64decode(header.removeprefix("Basic ")).decode("ascii")
        assert decoded == "alice@example.com:atk-abcdef1234567890"

    def test_header_handles_special_characters(self):
        creds = ConfluenceCredentials(
            base_url="https://example.atlassian.net/wiki",
            username="a+b@example.com",
            api_token="token with spaces?",
        )
        header = creds.basic_auth_header()
        decoded = base64.b64decode(header.removeprefix("Basic ")).decode("ascii")
        assert decoded == "a+b@example.com:token with spaces?"


# ---------------------------------------------------------------------------
# CONFLUENCE_* (back-compat) only
# ---------------------------------------------------------------------------


class TestConfluenceOnlyLoading:
    def test_loads_all_three_required_keys(self, tmp_secrets: Path):
        _write_secrets(
            tmp_secrets,
            CONFLUENCE_BASE_URL="https://example.atlassian.net/wiki",
            CONFLUENCE_USERNAME="alice@example.com",
            CONFLUENCE_API_TOKEN="atk-xyz",
        )
        mgr = ConfluenceCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        # CONFLUENCE_BASE_URL is used verbatim (operators have already added /wiki).
        assert creds.base_url == "https://example.atlassian.net/wiki"
        assert creds.username == "alice@example.com"
        assert creds.api_token == "atk-xyz"

    def test_trailing_slash_on_base_url_is_stripped(self, tmp_secrets: Path):
        _write_secrets(
            tmp_secrets,
            CONFLUENCE_BASE_URL="https://example.atlassian.net/wiki/",
            CONFLUENCE_USERNAME="alice@example.com",
            CONFLUENCE_API_TOKEN="atk-xyz",
        )
        mgr = ConfluenceCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://example.atlassian.net/wiki"

    @pytest.mark.parametrize(
        "missing_key",
        [
            "CONFLUENCE_BASE_URL",
            "CONFLUENCE_USERNAME",
            "CONFLUENCE_API_TOKEN",
        ],
    )
    def test_missing_any_required_key_raises(self, tmp_secrets: Path, missing_key: str):
        values = {
            "CONFLUENCE_BASE_URL": "https://example.atlassian.net/wiki",
            "CONFLUENCE_USERNAME": "alice@example.com",
            "CONFLUENCE_API_TOKEN": "atk-xyz",
        }
        values.pop(missing_key)
        _write_secrets(tmp_secrets, **values)
        mgr = ConfluenceCredentialsManager(tmp_secrets)
        with pytest.raises(ConfluenceCredentialsUnavailable):
            mgr.get_credentials()

    def test_blank_values_are_treated_as_missing(self, tmp_secrets: Path):
        _write_secrets(
            tmp_secrets,
            CONFLUENCE_BASE_URL="https://example.atlassian.net/wiki",
            CONFLUENCE_USERNAME="",
            CONFLUENCE_API_TOKEN="atk-xyz",
        )
        mgr = ConfluenceCredentialsManager(tmp_secrets)
        with pytest.raises(ConfluenceCredentialsUnavailable):
            mgr.get_credentials()

    def test_missing_file_raises(self, tmp_path: Path):
        mgr = ConfluenceCredentialsManager(tmp_path / "no-such-file.env")
        with pytest.raises(ConfluenceCredentialsUnavailable):
            mgr.get_credentials()


# ---------------------------------------------------------------------------
# ATLASSIAN_* precedence (decision F1)
# ---------------------------------------------------------------------------


class TestAtlassianPrecedence:
    def test_atlassian_only_appends_wiki(self, tmp_secrets: Path):
        _write_secrets(
            tmp_secrets,
            ATLASSIAN_BASE_URL="https://example.atlassian.net",
            ATLASSIAN_USERNAME="alice@example.com",
            ATLASSIAN_API_TOKEN="atk-shared",
        )
        mgr = ConfluenceCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        # ATLASSIAN_BASE_URL has /wiki appended for Confluence.
        assert creds.base_url == "https://example.atlassian.net/wiki"
        assert creds.username == "alice@example.com"
        assert creds.api_token == "atk-shared"

    def test_atlassian_wins_over_confluence_per_key(self, tmp_secrets: Path):
        """When both prefixes are set, ATLASSIAN_* wins for each key."""
        _write_secrets(
            tmp_secrets,
            ATLASSIAN_BASE_URL="https://atlassian.atlassian.net",
            ATLASSIAN_USERNAME="atlassian@example.com",
            ATLASSIAN_API_TOKEN="atk-atlassian",
            CONFLUENCE_BASE_URL="https://confluence.atlassian.net/wiki",
            CONFLUENCE_USERNAME="confluence@example.com",
            CONFLUENCE_API_TOKEN="atk-confluence",
        )
        mgr = ConfluenceCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://atlassian.atlassian.net/wiki"
        assert creds.username == "atlassian@example.com"
        assert creds.api_token == "atk-atlassian"

    def test_per_key_mixed_fallback(self, tmp_secrets: Path):
        """ATLASSIAN_USERNAME + CONFLUENCE_BASE_URL + CONFLUENCE_API_TOKEN."""
        _write_secrets(
            tmp_secrets,
            ATLASSIAN_USERNAME="alice@example.com",
            CONFLUENCE_BASE_URL="https://example.atlassian.net/wiki",
            CONFLUENCE_API_TOKEN="atk-xyz",
        )
        mgr = ConfluenceCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        # CONFLUENCE_BASE_URL used verbatim (no /wiki suffix added).
        assert creds.base_url == "https://example.atlassian.net/wiki"
        assert creds.username == "alice@example.com"
        assert creds.api_token == "atk-xyz"

    def test_atlassian_base_used_when_confluence_base_blank(self, tmp_secrets: Path):
        """Per-key fall-back: blank CONFLUENCE_BASE_URL → ATLASSIAN_BASE_URL+/wiki."""
        _write_secrets(
            tmp_secrets,
            ATLASSIAN_BASE_URL="https://example.atlassian.net",
            CONFLUENCE_BASE_URL="",
            ATLASSIAN_USERNAME="alice@example.com",
            ATLASSIAN_API_TOKEN="atk",
        )
        mgr = ConfluenceCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://example.atlassian.net/wiki"

    def test_blank_atlassian_keys_fall_through_to_confluence(self, tmp_secrets: Path):
        """Blank ATLASSIAN_* values must fall through to CONFLUENCE_*."""
        _write_secrets(
            tmp_secrets,
            ATLASSIAN_BASE_URL="",
            ATLASSIAN_USERNAME="",
            ATLASSIAN_API_TOKEN="",
            CONFLUENCE_BASE_URL="https://example.atlassian.net/wiki",
            CONFLUENCE_USERNAME="alice@example.com",
            CONFLUENCE_API_TOKEN="atk",
        )
        mgr = ConfluenceCredentialsManager(tmp_secrets)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://example.atlassian.net/wiki"
        assert creds.username == "alice@example.com"
        assert creds.api_token == "atk"

    def test_all_six_keys_missing_raises(self, tmp_secrets: Path):
        _write_secrets(tmp_secrets, OTHER_KEY="value")
        mgr = ConfluenceCredentialsManager(tmp_secrets)
        with pytest.raises(ConfluenceCredentialsUnavailable):
            mgr.get_credentials()


# ---------------------------------------------------------------------------
# Cache refresh
# ---------------------------------------------------------------------------


class TestCacheRefresh:
    def test_cached_until_mtime_changes(self, tmp_secrets: Path):
        """Two consecutive get_credentials calls hit the cache; touching the
        file forces a reload on the next call."""
        _write_secrets(
            tmp_secrets,
            CONFLUENCE_BASE_URL="https://example.atlassian.net/wiki",
            CONFLUENCE_USERNAME="alice@example.com",
            CONFLUENCE_API_TOKEN="atk-1",
        )
        mgr = ConfluenceCredentialsManager(tmp_secrets)
        first = mgr.get_credentials()
        assert first.api_token == "atk-1"

        # Without touching mtime, the cached credentials must be returned
        # even after rewriting the file in place (same mtime if quick enough,
        # but rewriting bumps mtime — so we just check the same call returns
        # a logically equal object, exercising the cached path).
        second = mgr.get_credentials()
        assert second.api_token == "atk-1"

        # Now touch the file with a clearly later mtime + new content.
        time.sleep(0.05)
        _write_secrets(
            tmp_secrets,
            CONFLUENCE_BASE_URL="https://example.atlassian.net/wiki",
            CONFLUENCE_USERNAME="alice@example.com",
            CONFLUENCE_API_TOKEN="atk-2",
        )
        new_mtime = time.time() + 1
        os.utime(tmp_secrets, (new_mtime, new_mtime))
        third = mgr.get_credentials()
        assert third.api_token == "atk-2"

    def test_reload_clears_cache(self, tmp_secrets: Path):
        _write_secrets(
            tmp_secrets,
            CONFLUENCE_BASE_URL="https://example.atlassian.net/wiki",
            CONFLUENCE_USERNAME="alice@example.com",
            CONFLUENCE_API_TOKEN="atk-1",
        )
        mgr = ConfluenceCredentialsManager(tmp_secrets)
        assert mgr.get_credentials().api_token == "atk-1"

        # Rewrite contents but DON'T bump mtime; reload() must still pick it up.
        _write_secrets(
            tmp_secrets,
            CONFLUENCE_BASE_URL="https://example.atlassian.net/wiki",
            CONFLUENCE_USERNAME="alice@example.com",
            CONFLUENCE_API_TOKEN="atk-2",
        )
        # Force the file to keep its old mtime.
        old_mtime = mgr._cached_mtime
        os.utime(tmp_secrets, (old_mtime, old_mtime))
        mgr.reload()
        assert mgr.get_credentials().api_token == "atk-2"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestModuleSingleton:
    def test_reload_helper_clears_singleton_cache(self, tmp_secrets: Path, monkeypatch):
        # Re-point the SECRETS_PATH to our tmp file and reset the singleton.
        monkeypatch.setattr(confluence_credentials, "SECRETS_PATH", tmp_secrets)
        confluence_credentials.reset_confluence_credentials_manager()

        _write_secrets(
            tmp_secrets,
            CONFLUENCE_BASE_URL="https://example.atlassian.net/wiki",
            CONFLUENCE_USERNAME="alice@example.com",
            CONFLUENCE_API_TOKEN="atk-1",
        )
        # Re-create singleton against the new SECRETS_PATH explicitly because
        # the manager constructor captured SECRETS_PATH at module-level.
        mgr = confluence_credentials.ConfluenceCredentialsManager(tmp_secrets)
        monkeypatch.setattr(
            confluence_credentials,
            "_credentials_manager",
            mgr,
        )
        assert confluence_credentials.get_confluence_credentials().api_token == "atk-1"

        _write_secrets(
            tmp_secrets,
            CONFLUENCE_BASE_URL="https://example.atlassian.net/wiki",
            CONFLUENCE_USERNAME="alice@example.com",
            CONFLUENCE_API_TOKEN="atk-2",
        )
        # Keep mtime stable to prove reload() (not mtime) is what flushes.
        old_mtime = mgr._cached_mtime
        os.utime(tmp_secrets, (old_mtime, old_mtime))
        confluence_credentials.reload_confluence_credentials()
        assert confluence_credentials.get_confluence_credentials().api_token == "atk-2"

    def test_reset_drops_singleton(self):
        # Just a smoke test — reset is a test helper.
        confluence_credentials.reset_confluence_credentials_manager()
        assert confluence_credentials._credentials_manager is None
