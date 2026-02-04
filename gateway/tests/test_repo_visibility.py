"""
Tests for repo_visibility module.
"""

import os
from unittest.mock import MagicMock, patch

# Import from conftest-loaded module
from repo_visibility import (
    DEFAULT_VISIBILITY_CACHE_TTL_READ,
    DEFAULT_VISIBILITY_CACHE_TTL_WRITE,
    CachedVisibility,
    RepoVisibilityChecker,
    get_repo_visibility,
    get_visibility_checker,
    is_repo_private,
)


class TestCachedVisibility:
    """Tests for CachedVisibility dataclass."""

    def test_is_stale_with_zero_ttl(self):
        """TTL of 0 should always be stale."""
        cached = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=1000000000,  # Way in the past
        )
        assert cached.is_stale(0) is True

    def test_is_stale_with_negative_ttl(self):
        """Negative TTL should always be stale."""
        cached = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=1000000000,
        )
        assert cached.is_stale(-1) is True

    def test_is_stale_when_fresh(self):
        """Fresh entries should not be stale."""
        import time

        cached = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=time.time(),  # Just now
        )
        assert cached.is_stale(60) is False

    def test_is_stale_when_old(self):
        """Old entries should be stale."""
        import time

        cached = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=time.time() - 120,  # 2 minutes ago
        )
        assert cached.is_stale(60) is True  # 60 second TTL


class TestRepoVisibilityChecker:
    """Tests for RepoVisibilityChecker class."""

    def test_init_default_ttls(self):
        """Checker should use default TTLs."""
        with patch.dict(os.environ, {}, clear=True):
            checker = RepoVisibilityChecker()
            assert checker._read_ttl == DEFAULT_VISIBILITY_CACHE_TTL_READ
            assert checker._write_ttl == DEFAULT_VISIBILITY_CACHE_TTL_WRITE

    def test_init_custom_ttls_from_env(self):
        """Checker should read TTLs from environment."""
        with patch.dict(
            os.environ,
            {"VISIBILITY_CACHE_TTL_READ": "120", "VISIBILITY_CACHE_TTL_WRITE": "30"},
        ):
            checker = RepoVisibilityChecker()
            assert checker._read_ttl == 120
            assert checker._write_ttl == 30

    def test_init_custom_ttls_from_args(self):
        """Checker should accept TTLs as arguments."""
        checker = RepoVisibilityChecker(read_ttl=300, write_ttl=60)
        assert checker._read_ttl == 300
        assert checker._write_ttl == 60

    @patch("repo_visibility.requests.get")
    def test_get_visibility_private_repo(self, mock_get):
        """Should return 'private' for private repos."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "private"}
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        # Mock token availability - returns list of (token, source) tuples
        with patch.object(checker, "_get_tokens", return_value=[("test-token", "bot")]):
            result = checker.get_visibility("owner", "repo")
            assert result == "private"

    @patch("repo_visibility.requests.get")
    def test_get_visibility_public_repo(self, mock_get):
        """Should return 'public' for public repos."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "public"}
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        with patch.object(checker, "_get_tokens", return_value=[("test-token", "bot")]):
            result = checker.get_visibility("owner", "repo")
            assert result == "public"

    @patch("repo_visibility.requests.get")
    def test_get_visibility_internal_repo(self, mock_get):
        """Should return 'internal' for internal repos."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "internal"}
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        with patch.object(checker, "_get_tokens", return_value=[("test-token", "bot")]):
            result = checker.get_visibility("owner", "repo")
            assert result == "internal"

    @patch("repo_visibility.requests.get")
    def test_get_visibility_404_returns_none_single_token(self, mock_get):
        """Should return None when repo not found with single token (fail closed)."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        with patch.object(checker, "_get_tokens", return_value=[("test-token", "bot")]):
            result = checker.get_visibility("owner", "repo")
            assert result is None

    def test_get_visibility_no_token_returns_none(self):
        """Should return None when no token available."""
        checker = RepoVisibilityChecker()
        with patch.object(checker, "_get_tokens", return_value=[]):
            result = checker.get_visibility("owner", "repo")
            assert result is None

    @patch("repo_visibility.requests.get")
    def test_is_private_true_for_private(self, mock_get):
        """is_private should return True for private repos."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "private"}
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        with patch.object(checker, "_get_tokens", return_value=[("test-token", "bot")]):
            result = checker.is_private("owner", "repo")
            assert result is True

    @patch("repo_visibility.requests.get")
    def test_is_private_true_for_internal(self, mock_get):
        """is_private should return True for internal repos."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "internal"}
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        with patch.object(checker, "_get_tokens", return_value=[("test-token", "bot")]):
            result = checker.is_private("owner", "repo")
            assert result is True

    @patch("repo_visibility.requests.get")
    def test_is_private_false_for_public(self, mock_get):
        """is_private should return False for public repos."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "public"}
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        with patch.object(checker, "_get_tokens", return_value=[("test-token", "bot")]):
            result = checker.is_private("owner", "repo")
            assert result is False

    @patch("repo_visibility.requests.get")
    def test_caching_works(self, mock_get):
        """Should cache results and not call API twice."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "private"}
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker(read_ttl=300)
        with patch.object(checker, "_get_tokens", return_value=[("test-token", "bot")]):
            # First call - should hit API
            result1 = checker.get_visibility("owner", "repo")
            assert result1 == "private"
            assert mock_get.call_count == 1

            # Second call - should use cache
            result2 = checker.get_visibility("owner", "repo")
            assert result2 == "private"
            assert mock_get.call_count == 1  # Still 1, no new API call

    # Multi-token fallback tests
    @patch("repo_visibility.requests.get")
    def test_multi_token_bot_success_user_not_tried(self, mock_get):
        """Bot token works, user token not tried."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "private"}
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        with patch.object(
            checker, "_get_tokens", return_value=[("bot-token", "bot"), ("user-token", "user")]
        ):
            result = checker.get_visibility("owner", "repo")
            assert result == "private"
            # Should only call API once (bot token succeeded)
            assert mock_get.call_count == 1
            # Verify it used the bot token
            call_headers = mock_get.call_args[1]["headers"]
            assert call_headers["Authorization"] == "Bearer bot-token"

    @patch("repo_visibility.requests.get")
    def test_multi_token_bot_404_user_success(self, mock_get):
        """Bot token 404, fall back to user token."""

        def side_effect(url, **kwargs):
            headers = kwargs.get("headers", {})
            auth = headers.get("Authorization", "")

            mock_resp = MagicMock()
            if "bot-token" in auth:
                mock_resp.status_code = 404
            else:
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"visibility": "private"}
            return mock_resp

        mock_get.side_effect = side_effect

        checker = RepoVisibilityChecker()
        with patch.object(
            checker, "_get_tokens", return_value=[("bot-token", "bot"), ("user-token", "user")]
        ):
            result = checker.get_visibility("owner", "repo")
            assert result == "private"
            # Should call API twice (bot failed, user succeeded)
            assert mock_get.call_count == 2

    @patch("repo_visibility.requests.get")
    def test_multi_token_both_fail(self, mock_get):
        """Both tokens fail, return None (fail closed)."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        with patch.object(
            checker, "_get_tokens", return_value=[("bot-token", "bot"), ("user-token", "user")]
        ):
            result = checker.get_visibility("owner", "repo")
            assert result is None
            # Should try both tokens
            assert mock_get.call_count == 2

    @patch("repo_visibility.requests.get")
    def test_multi_token_only_user_configured(self, mock_get):
        """Only user token configured - works correctly."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "public"}
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        with patch.object(checker, "_get_tokens", return_value=[("user-token", "user")]):
            result = checker.get_visibility("owner", "repo")
            assert result == "public"
            assert mock_get.call_count == 1

    def test_clear_cache(self):
        """clear_cache should empty the cache."""
        checker = RepoVisibilityChecker()
        # Add something to cache
        checker._cache[("owner", "repo")] = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=0,
        )
        assert len(checker._cache) == 1

        checker.clear_cache()
        assert len(checker._cache) == 0

    def test_invalidate(self):
        """invalidate should remove specific entry from cache."""
        checker = RepoVisibilityChecker()
        # Add something to cache
        checker._cache[("owner", "repo")] = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=0,
        )
        checker._cache[("other", "repo")] = CachedVisibility(
            owner="other",
            repo="repo",
            visibility="public",
            fetched_at=0,
        )
        assert len(checker._cache) == 2

        checker.invalidate("owner", "repo")
        assert len(checker._cache) == 1
        assert ("owner", "repo") not in checker._cache
        assert ("other", "repo") in checker._cache

    def test_case_insensitive_cache_keys(self):
        """Cache keys should be case-insensitive."""
        checker = RepoVisibilityChecker()
        # Add with mixed case
        checker._cache[("owner", "repo")] = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=0,
        )

        # Lookup should normalize case
        checker.invalidate("OWNER", "REPO")
        assert len(checker._cache) == 0


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_get_visibility_checker_returns_singleton(self):
        """get_visibility_checker should return a singleton."""
        # Reset the global
        import repo_visibility

        repo_visibility._checker = None

        checker1 = get_visibility_checker()
        checker2 = get_visibility_checker()
        assert checker1 is checker2

    @patch("repo_visibility.requests.get")
    @patch("token_refresher.get_bot_token")
    def test_get_repo_visibility(self, mock_get_bot_token, mock_get):
        """get_repo_visibility convenience function should work."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "private"}
        mock_get.return_value = mock_response
        mock_get_bot_token.return_value = ("test-token", "bot")

        # Reset singleton
        import repo_visibility

        repo_visibility._checker = None

        result = get_repo_visibility("owner", "repo")
        assert result == "private"

    @patch("repo_visibility.requests.get")
    @patch("token_refresher.get_bot_token")
    def test_is_repo_private(self, mock_get_bot_token, mock_get):
        """is_repo_private convenience function should work."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "private"}
        mock_get.return_value = mock_response
        mock_get_bot_token.return_value = ("test-token", "bot")

        # Reset singleton
        import repo_visibility

        repo_visibility._checker = None

        result = is_repo_private("owner", "repo")
        assert result is True
