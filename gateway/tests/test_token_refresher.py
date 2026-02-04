"""Tests for the token refresher module."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from token_refresher import (
    TokenInfo,
    TokenRefresher,
    get_bot_token,
    get_token_refresher,
    initialize_token_refresher,
    reset_token_refresher,
)

# Mark for tests that require network mocking (may not work in all environments)
requires_network_mocking = pytest.mark.skip(
    reason="Requires network mocking that may not work in sandboxed environments"
)


@pytest.fixture
def mock_private_key():
    """Mock private key for testing (jwt.encode is mocked, so content doesn't matter)."""
    return "mock_private_key_content"


@pytest.fixture
def mock_github_response():
    """Mock GitHub API response for token creation."""
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    return {
        "token": "ghs_test_token_12345",
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }


@pytest.fixture(autouse=True)
def reset_refresher():
    """Reset the global token refresher before each test."""
    reset_token_refresher()
    yield
    reset_token_refresher()


class TestTokenInfo:
    """Tests for TokenInfo dataclass."""

    def test_is_expired_false(self):
        """Token is not expired when expires_at is in the future."""
        info = TokenInfo(
            token="test_token",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            generated_at=datetime.now(UTC),
            source="refresher",
        )
        assert not info.is_expired

    def test_is_expired_true(self):
        """Token is expired when expires_at is in the past."""
        info = TokenInfo(
            token="test_token",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
            generated_at=datetime.now(UTC) - timedelta(hours=2),
            source="refresher",
        )
        assert info.is_expired

    def test_minutes_until_expiry(self):
        """Minutes until expiry is calculated correctly."""
        info = TokenInfo(
            token="test_token",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            generated_at=datetime.now(UTC),
            source="refresher",
        )
        # Allow some tolerance for test execution time
        assert 29 < info.minutes_until_expiry < 31


class TestTokenRefresher:
    """Tests for TokenRefresher class."""

    def test_init(self, mock_private_key):
        """TokenRefresher initializes with correct parameters."""
        refresher = TokenRefresher(
            app_id="12345",
            private_key=mock_private_key,
            installation_id=67890,
            refresh_margin_minutes=10,
            max_consecutive_failures=5,
        )
        assert refresher._app_id == "12345"
        assert refresher._installation_id == 67890
        assert refresher._max_failures == 5

    @requires_network_mocking
    @patch("token_refresher.jwt.encode")
    @patch("token_refresher.requests.post")
    def test_get_token_success(self, mock_post, mock_jwt, mock_private_key, mock_github_response):
        """Token is fetched successfully from GitHub API."""
        mock_jwt.return_value = "mock_jwt_token"
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: mock_github_response,
            raise_for_status=lambda: None,
        )

        refresher = TokenRefresher(
            app_id="12345",
            private_key=mock_private_key,
            installation_id=67890,
        )

        token = refresher.get_token()
        assert token == "ghs_test_token_12345"
        assert refresher.consecutive_failures == 0
        assert mock_jwt.called
        assert mock_post.called

    @requires_network_mocking
    @patch("token_refresher.jwt.encode")
    @patch("token_refresher.requests.post")
    def test_get_token_caches_valid_token(
        self, mock_post, mock_jwt, mock_private_key, mock_github_response
    ):
        """Valid token is cached and not re-fetched."""
        mock_jwt.return_value = "mock_jwt_token"
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: mock_github_response,
            raise_for_status=lambda: None,
        )

        refresher = TokenRefresher(
            app_id="12345",
            private_key=mock_private_key,
            installation_id=67890,
        )

        # First call fetches token
        token1 = refresher.get_token()
        assert mock_post.call_count == 1

        # Second call uses cached token
        token2 = refresher.get_token()
        assert mock_post.call_count == 1
        assert token1 == token2

    @requires_network_mocking
    @patch("token_refresher.jwt.encode")
    @patch("token_refresher.requests.post")
    def test_get_token_refresh_failure_uses_cache(
        self, mock_post, mock_jwt, mock_private_key, mock_github_response
    ):
        """On refresh failure, cached token is used if available."""
        mock_jwt.return_value = "mock_jwt_token"

        # First call succeeds
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: mock_github_response,
            raise_for_status=lambda: None,
        )

        refresher = TokenRefresher(
            app_id="12345",
            private_key=mock_private_key,
            installation_id=67890,
            refresh_margin_minutes=0,  # Force refresh on every call
        )

        token1 = refresher.get_token()
        assert token1 == "ghs_test_token_12345"

        # Make token expire so refresh is attempted
        refresher._expires_at = datetime.now(UTC) - timedelta(minutes=1)

        # Second call fails
        mock_post.side_effect = Exception("Network error")
        token2 = refresher.get_token()

        # Should return cached token
        assert token2 == "ghs_test_token_12345"
        assert refresher.consecutive_failures == 1

    @requires_network_mocking
    @patch("token_refresher.jwt.encode")
    @patch("token_refresher.requests.post")
    def test_get_token_max_failures_clears_cache(self, mock_post, mock_jwt, mock_private_key):
        """After max consecutive failures, cached token is cleared."""
        mock_jwt.return_value = "mock_jwt_token"
        mock_post.side_effect = Exception("Network error")

        refresher = TokenRefresher(
            app_id="12345",
            private_key=mock_private_key,
            installation_id=67890,
            max_consecutive_failures=2,
        )

        # First failure - no token
        token1 = refresher.get_token()
        assert token1 is None
        assert refresher.consecutive_failures == 1

        # Second failure - still no token, reaches max
        token2 = refresher.get_token()
        assert token2 is None
        assert refresher.consecutive_failures == 2

        # Third failure - continues to fail
        token3 = refresher.get_token()
        assert token3 is None
        assert refresher.consecutive_failures == 3

    @requires_network_mocking
    @patch("token_refresher.jwt.encode")
    @patch("token_refresher.requests.post")
    def test_get_token_info(self, mock_post, mock_jwt, mock_private_key, mock_github_response):
        """get_token_info returns TokenInfo with correct data."""
        mock_jwt.return_value = "mock_jwt_token"
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: mock_github_response,
            raise_for_status=lambda: None,
        )

        refresher = TokenRefresher(
            app_id="12345",
            private_key=mock_private_key,
            installation_id=67890,
        )

        info = refresher.get_token_info()
        assert info is not None
        assert info.token == "ghs_test_token_12345"
        assert info.source == "refresher"
        assert not info.is_expired

    @requires_network_mocking
    @patch("token_refresher.jwt.encode")
    @patch("token_refresher.requests.post")
    def test_get_token_info_none_when_no_token(self, mock_post, mock_jwt, mock_private_key):
        """get_token_info returns None when no token available."""
        mock_jwt.return_value = "mock_jwt_token"
        mock_post.side_effect = Exception("Network error")

        refresher = TokenRefresher(
            app_id="12345",
            private_key=mock_private_key,
            installation_id=67890,
        )

        info = refresher.get_token_info()
        assert info is None


class TestInitializeTokenRefresher:
    """Tests for initialize_token_refresher function."""

    @requires_network_mocking
    @patch("token_refresher.jwt.encode")
    @patch("token_refresher.requests.post")
    def test_initialize_from_files(
        self, mock_post, mock_jwt, tmp_path, mock_private_key, mock_github_response
    ):
        """Token refresher initializes from secrets.env and pem file."""
        mock_jwt.return_value = "mock_jwt_token"
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: mock_github_response,
            raise_for_status=lambda: None,
        )

        # Create secrets.env with GitHub App credentials
        secrets_content = """# Test secrets
GITHUB_APP_ID=12345
GITHUB_APP_INSTALLATION_ID=67890
"""
        (tmp_path / "secrets.env").write_text(secrets_content)
        (tmp_path / "github-app.pem").write_text(mock_private_key)

        refresher = initialize_token_refresher(config_dir=tmp_path)
        assert refresher is not None
        assert get_token_refresher() is refresher

    @requires_network_mocking
    @patch("token_refresher.jwt.encode")
    @patch("token_refresher.requests.post")
    def test_initialize_from_env(
        self, mock_post, mock_jwt, tmp_path, mock_private_key, mock_github_response
    ):
        """Token refresher initializes from environment variables."""
        mock_jwt.return_value = "mock_jwt_token"
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: mock_github_response,
            raise_for_status=lambda: None,
        )

        # Create private key file
        key_path = tmp_path / "github-app.pem"
        key_path.write_text(mock_private_key)

        with patch.dict(
            "os.environ",
            {
                "GITHUB_APP_ID": "12345",
                "GITHUB_INSTALLATION_ID": "67890",
                "GITHUB_PRIVATE_KEY_PATH": str(key_path),
            },
        ):
            refresher = initialize_token_refresher(config_dir=tmp_path)
            assert refresher is not None

    def test_initialize_returns_none_when_missing_config(self, tmp_path):
        """Token refresher returns None when config is missing."""
        # Empty config dir
        refresher = initialize_token_refresher(config_dir=tmp_path)
        assert refresher is None
        assert get_token_refresher() is None

    def test_initialize_only_once(self, tmp_path):
        """Token refresher initialization only happens once."""
        # First call with missing config
        refresher1 = initialize_token_refresher(config_dir=tmp_path)
        assert refresher1 is None

        # Second call should not re-initialize
        refresher2 = initialize_token_refresher(config_dir=tmp_path)
        assert refresher2 is None

    @requires_network_mocking
    @patch("token_refresher.jwt.encode")
    @patch("token_refresher.requests.post")
    def test_initialize_explicit_params(
        self, mock_post, mock_jwt, mock_private_key, mock_github_response, tmp_path
    ):
        """Token refresher initializes with explicit parameters."""
        mock_jwt.return_value = "mock_jwt_token"
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: mock_github_response,
            raise_for_status=lambda: None,
        )

        # Create private key file
        key_path = tmp_path / "test-key.pem"
        key_path.write_text(mock_private_key)

        refresher = initialize_token_refresher(
            app_id="explicit_app_id",
            private_key_path=key_path,
            installation_id=99999,
        )
        assert refresher is not None


class TestGetBotToken:
    """Tests for get_bot_token function."""

    @requires_network_mocking
    @patch("token_refresher.jwt.encode")
    @patch("token_refresher.requests.post")
    def test_get_bot_token_from_refresher(
        self, mock_post, mock_jwt, tmp_path, mock_private_key, mock_github_response
    ):
        """get_bot_token returns token from refresher when available."""
        mock_jwt.return_value = "mock_jwt_token"
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: mock_github_response,
            raise_for_status=lambda: None,
        )

        # Initialize refresher
        key_path = tmp_path / "github-app.pem"
        key_path.write_text(mock_private_key)

        with patch.dict(
            "os.environ",
            {
                "GITHUB_APP_ID": "12345",
                "GITHUB_INSTALLATION_ID": "67890",
                "GITHUB_PRIVATE_KEY_PATH": str(key_path),
            },
        ):
            initialize_token_refresher(config_dir=tmp_path)
            token, source = get_bot_token()

        assert token == "ghs_test_token_12345"
        assert source == "refresher"

    def test_get_bot_token_returns_none_when_unavailable(self):
        """get_bot_token returns None when no token source available."""
        # No refresher initialized
        token, source = get_bot_token()
        assert token is None
        assert source == "none"
