"""Tests for shared.egg_jira_credentials (#1557 TASK-1-5).

Covers:
- ``parse_env_file`` parsing rules (quotes, comments, blanks, malformed lines).
- ``JiraCredentialsManager`` happy path, mtime caching, reload, thread safety.
- ``JiraCredentials.basic_auth_header`` encoding.
- ``JiraCredentialsUnavailable`` raised on missing file / missing keys.
- Per-key ``ATLASSIAN_*`` over ``JIRA_*`` precedence (decision F1 / #1931).
- ``get_jira_credentials`` / ``get_jira_credentials_manager`` singleton wiring
  via ``reset_manager_for_tests``.
"""

from __future__ import annotations

import base64
import dataclasses
import os
import threading
import time
from pathlib import Path

# ``shared/`` is added to sys.path by tests/conftest.py so the modules below
# import as bare names.
import egg_jira_credentials
import pytest
from egg_jira_credentials import (
    JiraCredentials,
    JiraCredentialsManager,
    JiraCredentialsUnavailable,
    get_jira_credentials,
    get_jira_credentials_manager,
    parse_env_file,
    reset_manager_for_tests,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _write_secrets(path: Path, lines: list[str]) -> None:
    """Write ``lines`` to ``path`` (newline-terminated)."""
    path.write_text("\n".join(lines) + "\n")


@pytest.fixture
def secrets_path(tmp_path: Path) -> Path:
    """Return a temp secrets.env path (not yet created)."""
    return tmp_path / "secrets.env"


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the module-level singleton between tests so state doesn't leak."""
    # Replace with a manager pointing at a definitely-missing path so any
    # accidental ``get_jira_credentials()`` call raises rather than reading
    # a real ``~/.config/egg/secrets.env``.
    reset_manager_for_tests(secrets_path=Path("/nonexistent/egg-test-secrets.env"))
    yield
    reset_manager_for_tests(secrets_path=Path("/nonexistent/egg-test-secrets.env"))


# ---------------------------------------------------------------------------
# parse_env_file
# ---------------------------------------------------------------------------


class TestParseEnvFile:
    def test_basic_kv(self, secrets_path: Path) -> None:
        _write_secrets(secrets_path, ["FOO=bar", "BAZ=qux"])
        assert parse_env_file(secrets_path) == {"FOO": "bar", "BAZ": "qux"}

    def test_double_quoted_value_stripped(self, secrets_path: Path) -> None:
        _write_secrets(secrets_path, ['KEY="value with spaces"'])
        assert parse_env_file(secrets_path) == {"KEY": "value with spaces"}

    def test_single_quoted_value_stripped(self, secrets_path: Path) -> None:
        _write_secrets(secrets_path, ["KEY='value'"])
        assert parse_env_file(secrets_path) == {"KEY": "value"}

    def test_comments_and_blank_lines_ignored(self, secrets_path: Path) -> None:
        _write_secrets(
            secrets_path,
            [
                "# a comment",
                "",
                "   ",
                "FOO=bar",
                "# another comment",
                "BAZ=qux",
            ],
        )
        assert parse_env_file(secrets_path) == {"FOO": "bar", "BAZ": "qux"}

    def test_lines_without_equals_skipped(self, secrets_path: Path) -> None:
        _write_secrets(secrets_path, ["no_equals_here", "FOO=bar"])
        assert parse_env_file(secrets_path) == {"FOO": "bar"}

    def test_empty_key_skipped(self, secrets_path: Path) -> None:
        _write_secrets(secrets_path, ["=orphan", "FOO=bar"])
        assert parse_env_file(secrets_path) == {"FOO": "bar"}

    def test_missing_file_returns_empty_dict(self, tmp_path: Path) -> None:
        # No file written — read should log and return {}.
        missing = tmp_path / "does-not-exist.env"
        assert parse_env_file(missing) == {}

    def test_value_with_equals_preserves_remainder(self, secrets_path: Path) -> None:
        # ``partition('=')`` keeps everything after the first ``=``.
        _write_secrets(secrets_path, ["KEY=a=b=c"])
        assert parse_env_file(secrets_path) == {"KEY": "a=b=c"}


# ---------------------------------------------------------------------------
# JiraCredentials
# ---------------------------------------------------------------------------


class TestJiraCredentialsDataclass:
    def test_basic_auth_header_encodes_email_token(self) -> None:
        creds = JiraCredentials(
            base_url="https://foo.atlassian.net",
            username="alice@example.com",
            api_token="tok-123",
        )
        expected_raw = b"alice@example.com:tok-123"
        expected = "Basic " + base64.b64encode(expected_raw).decode("ascii")
        assert creds.basic_auth_header() == expected

    def test_frozen_dataclass(self) -> None:
        creds = JiraCredentials(base_url="x", username="y", api_token="z")
        with pytest.raises(dataclasses.FrozenInstanceError):
            creds.base_url = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# JiraCredentialsManager — happy path / precedence / errors
# ---------------------------------------------------------------------------


class TestManagerHappyPath:
    def test_loads_atlassian_keys(self, secrets_path: Path) -> None:
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=https://foo.atlassian.net",
                "ATLASSIAN_USERNAME=alice@example.com",
                "ATLASSIAN_API_TOKEN=tok-123",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://foo.atlassian.net"
        assert creds.username == "alice@example.com"
        assert creds.api_token == "tok-123"

    def test_loads_jira_keys_fallback(self, secrets_path: Path) -> None:
        _write_secrets(
            secrets_path,
            [
                "JIRA_BASE_URL=https://bar.atlassian.net",
                "JIRA_USERNAME=bob@example.com",
                "JIRA_API_TOKEN=tok-bob",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://bar.atlassian.net"
        assert creds.username == "bob@example.com"
        assert creds.api_token == "tok-bob"

    def test_trailing_slash_stripped_from_base_url(self, secrets_path: Path) -> None:
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=https://foo.atlassian.net/",
                "ATLASSIAN_USERNAME=alice@example.com",
                "ATLASSIAN_API_TOKEN=tok",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        assert mgr.get_credentials().base_url == "https://foo.atlassian.net"

    def test_whitespace_stripped(self, secrets_path: Path) -> None:
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=   https://foo.atlassian.net   ",
                'ATLASSIAN_USERNAME="  alice@example.com  "',
                "ATLASSIAN_API_TOKEN=  tok-trimmed  ",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://foo.atlassian.net"
        # The quoted-value form preserves inner spaces (quotes are stripped
        # before the per-key ``.strip()``).
        assert creds.username == "alice@example.com"
        assert creds.api_token == "tok-trimmed"


class TestManagerPrecedence:
    """Per-key precedence: ATLASSIAN_* wins over JIRA_* (decision F1)."""

    def test_atlassian_wins_when_both_present(self, secrets_path: Path) -> None:
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=https://from-atlassian.example",
                "JIRA_BASE_URL=https://from-jira.example",
                "ATLASSIAN_USERNAME=atl-user",
                "JIRA_USERNAME=jira-user",
                "ATLASSIAN_API_TOKEN=atl-tok",
                "JIRA_API_TOKEN=jira-tok",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://from-atlassian.example"
        assert creds.username == "atl-user"
        assert creds.api_token == "atl-tok"

    def test_mixed_per_key_fallback(self, secrets_path: Path) -> None:
        # Each of the three keys is checked independently.
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=https://from-atlassian.example",
                # No ATLASSIAN_USERNAME → fall back to JIRA_USERNAME.
                "JIRA_USERNAME=jira-user",
                # No ATLASSIAN_API_TOKEN → fall back to JIRA_API_TOKEN.
                "JIRA_API_TOKEN=jira-tok",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://from-atlassian.example"
        assert creds.username == "jira-user"
        assert creds.api_token == "jira-tok"

    def test_empty_atlassian_value_falls_back_to_jira(self, secrets_path: Path) -> None:
        # An empty / whitespace-only ATLASSIAN_* value is treated as absent.
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=",
                "JIRA_BASE_URL=https://from-jira.example",
                "ATLASSIAN_USERNAME=   ",
                "JIRA_USERNAME=jira-user",
                "ATLASSIAN_API_TOKEN=",
                "JIRA_API_TOKEN=jira-tok",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        creds = mgr.get_credentials()
        assert creds.base_url == "https://from-jira.example"
        assert creds.username == "jira-user"
        assert creds.api_token == "jira-tok"


class TestManagerErrors:
    def test_missing_file_raises(self, secrets_path: Path) -> None:
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        with pytest.raises(JiraCredentialsUnavailable) as excinfo:
            mgr.get_credentials()
        assert str(secrets_path) in str(excinfo.value)

    def test_empty_file_raises(self, secrets_path: Path) -> None:
        secrets_path.write_text("")
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        with pytest.raises(JiraCredentialsUnavailable):
            mgr.get_credentials()

    def test_only_comments_raises(self, secrets_path: Path) -> None:
        _write_secrets(secrets_path, ["# just a comment", "# nothing else"])
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        with pytest.raises(JiraCredentialsUnavailable):
            mgr.get_credentials()

    def test_partial_credentials_raises(self, secrets_path: Path) -> None:
        # base_url present, username missing, token missing.
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=https://foo.example",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        with pytest.raises(JiraCredentialsUnavailable):
            mgr.get_credentials()

    def test_missing_after_load_clears_cache(self, secrets_path: Path) -> None:
        # First load succeeds, then the file disappears between calls.
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=https://foo.example",
                "ATLASSIAN_USERNAME=alice",
                "ATLASSIAN_API_TOKEN=tok",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        mgr.get_credentials()  # primes cache
        secrets_path.unlink()
        with pytest.raises(JiraCredentialsUnavailable):
            mgr.get_credentials()
        # And the manager's internal cache should be cleared.
        assert mgr._credentials is None  # type: ignore[attr-defined]
        assert mgr._cached_mtime == 0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# mtime caching / reload
# ---------------------------------------------------------------------------


class TestManagerCaching:
    def test_same_mtime_skips_reload(self, secrets_path: Path) -> None:
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=https://foo.example",
                "ATLASSIAN_USERNAME=alice",
                "ATLASSIAN_API_TOKEN=tok",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        first = mgr.get_credentials()
        second = mgr.get_credentials()
        # Without mtime bump the manager returns the same cached object.
        assert first is second

    def test_mtime_change_triggers_reload(self, secrets_path: Path) -> None:
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=https://first.example",
                "ATLASSIAN_USERNAME=alice",
                "ATLASSIAN_API_TOKEN=tok-1",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        first = mgr.get_credentials()
        assert first.base_url == "https://first.example"

        # Rewrite with a future mtime so the stat-based check sees a change
        # even on coarse-grained filesystem timestamps.
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=https://second.example",
                "ATLASSIAN_USERNAME=alice",
                "ATLASSIAN_API_TOKEN=tok-2",
            ],
        )
        future = time.time() + 60
        os.utime(secrets_path, (future, future))

        second = mgr.get_credentials()
        assert second.base_url == "https://second.example"
        assert second.api_token == "tok-2"
        assert second is not first

    def test_reload_forces_next_read(self, secrets_path: Path) -> None:
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=https://foo.example",
                "ATLASSIAN_USERNAME=alice",
                "ATLASSIAN_API_TOKEN=tok",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        mgr.get_credentials()
        mgr.reload()
        # After reload the cache slot is cleared and mtime sentinel reset.
        assert mgr._credentials is None  # type: ignore[attr-defined]
        assert mgr._cached_mtime == 0  # type: ignore[attr-defined]
        # The next call re-reads and populates again.
        again = mgr.get_credentials()
        assert again.base_url == "https://foo.example"


# ---------------------------------------------------------------------------
# Thread safety (smoke test)
# ---------------------------------------------------------------------------


class TestManagerThreadSafety:
    def test_concurrent_get_credentials(self, secrets_path: Path) -> None:
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=https://foo.example",
                "ATLASSIAN_USERNAME=alice",
                "ATLASSIAN_API_TOKEN=tok",
            ],
        )
        mgr = JiraCredentialsManager(secrets_path=secrets_path)
        results: list[JiraCredentials] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append(mgr.get_credentials())
            except BaseException as exc:  # noqa: BLE001 — propagate after join
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"thread errors: {errors!r}"
        assert len(results) == 16
        for r in results:
            assert r.base_url == "https://foo.example"
            assert r.username == "alice"
            assert r.api_token == "tok"


# ---------------------------------------------------------------------------
# Singleton glue: get_jira_credentials* + reset_manager_for_tests
# ---------------------------------------------------------------------------


class TestSingletonGlue:
    def test_reset_manager_for_tests_returns_new_manager(self, secrets_path: Path) -> None:
        mgr1 = reset_manager_for_tests(secrets_path=secrets_path)
        mgr2 = get_jira_credentials_manager()
        # The singleton getter returns the just-reset instance.
        assert mgr1 is mgr2

    def test_get_jira_credentials_uses_singleton(self, secrets_path: Path) -> None:
        _write_secrets(
            secrets_path,
            [
                "ATLASSIAN_BASE_URL=https://foo.example",
                "ATLASSIAN_USERNAME=alice",
                "ATLASSIAN_API_TOKEN=tok",
            ],
        )
        reset_manager_for_tests(secrets_path=secrets_path)
        creds = get_jira_credentials()
        assert creds.base_url == "https://foo.example"
        assert creds.username == "alice"
        assert creds.api_token == "tok"

    def test_get_jira_credentials_raises_when_missing(self, tmp_path: Path) -> None:
        reset_manager_for_tests(secrets_path=tmp_path / "absent.env")
        with pytest.raises(JiraCredentialsUnavailable):
            get_jira_credentials()

    def test_get_jira_credentials_manager_is_lazy(self) -> None:
        # After reset to None the next call creates a new manager. We force
        # the underlying global back to None to exercise the lazy branch.
        egg_jira_credentials._credentials_manager = None  # type: ignore[attr-defined]
        mgr = get_jira_credentials_manager()
        assert isinstance(mgr, JiraCredentialsManager)
