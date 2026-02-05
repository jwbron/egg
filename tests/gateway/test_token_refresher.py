"""Tests for gateway token_refresher module."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from token_refresher import (
    TokenInfo,
    TokenRefresher,
    _read_secrets_env,
    get_bot_token,
    get_token_refresher,
    reset_token_refresher,
)


class TestTokenInfo:
    """Tests for TokenInfo dataclass."""

    def test_not_expired(self):
        """Token in the future is not expired."""
        info = TokenInfo(
            token="ghs_test",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            generated_at=datetime.now(UTC),
            source="refresher",
        )
        assert info.is_expired is False
        assert info.minutes_until_expiry > 50

    def test_expired(self):
        """Token in the past is expired."""
        info = TokenInfo(
            token="ghs_test",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
            generated_at=datetime.now(UTC) - timedelta(hours=2),
            source="refresher",
        )
        assert info.is_expired is True
        assert info.minutes_until_expiry < 0

    def test_source_field(self):
        """Source field is stored."""
        info = TokenInfo(
            token="t",
            expires_at=datetime.now(UTC),
            generated_at=datetime.now(UTC),
            source="test-source",
        )
        assert info.source == "test-source"


class TestTokenRefresher:
    """Tests for TokenRefresher class."""

    def _make_refresher(self):
        """Helper to create a TokenRefresher with test values."""
        return TokenRefresher(
            app_id="12345",
            private_key="fake-key",
            installation_id=67890,
        )

    def test_init(self):
        """Initialize with required parameters."""
        r = self._make_refresher()
        assert r._app_id == "12345"
        assert r._installation_id == 67890

    def test_needs_refresh_no_token(self):
        """Needs refresh when no token."""
        r = self._make_refresher()
        assert r._needs_refresh() is True

    def test_needs_refresh_expired(self):
        """Needs refresh when token expired."""
        r = self._make_refresher()
        r._token = "ghs_old"
        r._expires_at = datetime.now(UTC) - timedelta(hours=1)
        assert r._needs_refresh() is True

    def test_needs_refresh_within_margin(self):
        """Needs refresh when within margin."""
        r = self._make_refresher()
        r._token = "ghs_current"
        r._expires_at = datetime.now(UTC) + timedelta(minutes=10)
        # Margin is 15 minutes, so 10 minutes left = needs refresh
        assert r._needs_refresh() is True

    def test_no_refresh_needed(self):
        """No refresh when plenty of time left."""
        r = self._make_refresher()
        r._token = "ghs_current"
        r._expires_at = datetime.now(UTC) + timedelta(hours=1)
        assert r._needs_refresh() is False

    def test_get_token_no_token_available(self):
        """get_token returns None when refresh fails and no cached token."""
        r = self._make_refresher()
        with patch.object(r, "_refresh", side_effect=Exception("API error")):
            token = r.get_token()
            assert token is None

    def test_get_token_uses_cache_on_failure(self):
        """get_token returns cached token when refresh fails."""
        r = self._make_refresher()
        r._token = "ghs_cached"
        r._expires_at = datetime.now(UTC) - timedelta(minutes=1)  # expired
        r._generated_at = datetime.now(UTC) - timedelta(hours=1)

        with patch.object(r, "_refresh", side_effect=Exception("API error")):
            token = r.get_token()
            assert token == "ghs_cached"
            assert r._consecutive_failures == 1

    def test_max_failures_clears_cache(self):
        """Cache is cleared after max consecutive failures."""
        r = self._make_refresher()
        r._token = "ghs_cached"
        r._expires_at = datetime.now(UTC) - timedelta(minutes=1)
        r._generated_at = datetime.now(UTC) - timedelta(hours=1)

        with patch.object(r, "_refresh", side_effect=Exception("fail")):
            # Fail max_failures times
            for _ in range(3):
                r.get_token()

            assert r._token is None  # Cache cleared
            assert r._consecutive_failures == 3

    def test_get_token_info(self):
        """get_token_info returns TokenInfo when token available."""
        r = self._make_refresher()
        r._token = "ghs_valid"
        r._expires_at = datetime.now(UTC) + timedelta(hours=1)
        r._generated_at = datetime.now(UTC)

        info = r.get_token_info()
        assert info is not None
        assert info.token == "ghs_valid"
        assert info.source == "refresher"

    def test_get_token_info_none(self):
        """get_token_info returns None when no token."""
        r = self._make_refresher()
        with patch.object(r, "_refresh", side_effect=Exception("fail")):
            info = r.get_token_info()
            assert info is None

    def test_consecutive_failures_property(self):
        """consecutive_failures property is thread-safe."""
        r = self._make_refresher()
        assert r.consecutive_failures == 0

    def test_reset_failure_count(self):
        """reset_failure_count resets counter."""
        r = self._make_refresher()
        r._consecutive_failures = 5
        r.reset_failure_count()
        assert r.consecutive_failures == 0

    @patch("token_refresher.requests.post")
    @patch("token_refresher.jwt.encode", return_value="fake-jwt")
    def test_refresh_success(self, mock_jwt, mock_post):
        """Successful token refresh via API."""
        r = self._make_refresher()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "token": "ghs_newtoken",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        r._refresh()

        assert r._token == "ghs_newtoken"
        assert r._expires_at is not None
        assert r._generated_at is not None

    @patch("token_refresher.requests.post")
    @patch("token_refresher.jwt.encode", return_value="fake-jwt")
    def test_refresh_api_error(self, mock_jwt, mock_post):
        """API error during refresh raises exception."""
        r = self._make_refresher()
        mock_post.return_value.raise_for_status.side_effect = Exception("HTTP 500")

        with pytest.raises(Exception, match="HTTP 500"):
            r._refresh()


class TestReadSecretsEnv:
    """Tests for _read_secrets_env function."""

    def test_parse_file(self, tmp_path):
        """Parse secrets.env file."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("KEY1=value1\nKEY2=value2\n")
        result = _read_secrets_env(secrets_file)
        assert result["KEY1"] == "value1"
        assert result["KEY2"] == "value2"

    def test_skip_comments(self, tmp_path):
        """Skip comment lines."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("# Comment\nKEY=value\n")
        result = _read_secrets_env(secrets_file)
        assert len(result) == 1

    def test_nonexistent_file(self, tmp_path):
        """Return empty dict for missing file."""
        result = _read_secrets_env(tmp_path / "nonexistent")
        assert result == {}

    def test_empty_lines_skipped(self, tmp_path):
        """Empty lines are skipped."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("\n\nKEY=val\n\n")
        result = _read_secrets_env(secrets_file)
        assert len(result) == 1


class TestGlobalFunctions:
    """Tests for module-level functions."""

    def test_reset_token_refresher(self):
        """reset_token_refresher clears global state."""
        import token_refresher

        token_refresher._token_refresher = MagicMock()
        token_refresher._refresher_initialization_attempted = True

        reset_token_refresher()
        assert token_refresher._token_refresher is None
        assert token_refresher._refresher_initialization_attempted is False

    def test_get_token_refresher_none(self):
        """get_token_refresher returns None when not initialized."""
        import token_refresher

        token_refresher._token_refresher = None
        assert get_token_refresher() is None

    def test_get_bot_token_no_refresher(self):
        """get_bot_token returns None when no refresher."""
        import token_refresher

        token_refresher._token_refresher = None
        token, source = get_bot_token()
        assert token is None
        assert source == "none"

    def test_get_bot_token_with_refresher(self):
        """get_bot_token returns token from refresher."""
        import token_refresher

        mock_refresher = MagicMock()
        mock_refresher.get_token.return_value = "ghs_test_token"
        token_refresher._token_refresher = mock_refresher

        token, source = get_bot_token()
        assert token == "ghs_test_token"
        assert source == "refresher"

        # Cleanup
        token_refresher._token_refresher = None

    def test_get_bot_token_refresher_returns_none(self):
        """get_bot_token handles refresher returning None."""
        import token_refresher

        mock_refresher = MagicMock()
        mock_refresher.get_token.return_value = None
        token_refresher._token_refresher = mock_refresher

        token, source = get_bot_token()
        assert token is None
        assert source == "none"

        # Cleanup
        token_refresher._token_refresher = None
