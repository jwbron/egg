"""
Tests for repo_visibility module.
"""

import os
from unittest.mock import MagicMock, call, patch

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


class TestVisibilityRetry:
    """Tests for retry logic in _fetch_visibility_with_token."""

    @patch("repo_visibility.time.sleep")
    @patch("repo_visibility.requests.get")
    def test_retries_on_timeout(self, mock_get, mock_sleep):
        """Should retry on request timeout with backoff."""
        import requests as req

        mock_get.side_effect = req.Timeout("timed out")

        checker = RepoVisibilityChecker()
        result = checker._fetch_visibility_with_token("owner", "repo", "tok", "bot")

        assert result is None
        assert mock_get.call_count == 3
        assert mock_sleep.call_args_list == [call(1), call(2)]

    @patch("repo_visibility.time.sleep")
    @patch("repo_visibility.requests.get")
    def test_retries_on_connection_error(self, mock_get, mock_sleep):
        """Should retry on connection errors with backoff."""
        import requests as req

        mock_get.side_effect = req.ConnectionError("refused")

        checker = RepoVisibilityChecker()
        result = checker._fetch_visibility_with_token("owner", "repo", "tok", "bot")

        assert result is None
        assert mock_get.call_count == 3
        assert mock_sleep.call_args_list == [call(1), call(2)]

    @patch("repo_visibility.time.sleep")
    @patch("repo_visibility.requests.get")
    def test_retries_on_503(self, mock_get, mock_sleep):
        """Should retry on 5xx server errors."""
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        result = checker._fetch_visibility_with_token("owner", "repo", "tok", "bot")

        assert result is None
        assert mock_get.call_count == 3
        assert mock_sleep.call_args_list == [call(1), call(2)]

    @patch("repo_visibility.time.sleep")
    @patch("repo_visibility.requests.get")
    def test_retries_on_403_rate_limit(self, mock_get, mock_sleep):
        """Should retry on 403 (rate limit / forbidden)."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        result = checker._fetch_visibility_with_token("owner", "repo", "tok", "bot")

        assert result is None
        assert mock_get.call_count == 3
        assert mock_sleep.call_args_list == [call(1), call(2)]

    @patch("repo_visibility.time.sleep")
    @patch("repo_visibility.requests.get")
    def test_no_retry_on_404(self, mock_get, mock_sleep):
        """Should NOT retry on 404 (definitive no-access)."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        result = checker._fetch_visibility_with_token("owner", "repo", "tok", "bot")

        assert result is None
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    @patch("repo_visibility.time.sleep")
    @patch("repo_visibility.requests.get")
    def test_no_retry_on_success(self, mock_get, mock_sleep):
        """Should NOT retry on 200 success."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "private"}
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker()
        result = checker._fetch_visibility_with_token("owner", "repo", "tok", "bot")

        assert result == "private"
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    @patch("repo_visibility.time.sleep")
    @patch("repo_visibility.requests.get")
    def test_succeeds_after_transient_failure(self, mock_get, mock_sleep):
        """Should succeed when retry clears a transient error."""
        import requests as req

        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.json.return_value = {"visibility": "public"}

        mock_get.side_effect = [req.Timeout("timed out"), ok_response]

        checker = RepoVisibilityChecker()
        result = checker._fetch_visibility_with_token("owner", "repo", "tok", "bot")

        assert result == "public"
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @patch("repo_visibility.time.sleep")
    @patch("repo_visibility.requests.get")
    def test_no_retry_on_other_request_exception(self, mock_get, mock_sleep):
        """Should NOT retry on non-transient RequestException."""
        import requests as req

        mock_get.side_effect = req.RequestException("bad request")

        checker = RepoVisibilityChecker()
        result = checker._fetch_visibility_with_token("owner", "repo", "tok", "bot")

        assert result is None
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()


# --- Slice-2 (#3393) uniform visibility / auth-mode validation ---------------
#
# multi-repo pipelines require every repo in one run to share a single
# visibility posture (all private/internal or all public) and a single auth
# mode (all bot or all user) — private mode is a pipeline-wide posture, so a
# mixed set would leak content across the private/public boundary. These
# helpers resolve each repo's visibility (``get_repo_visibility``) and auth mode
# (``config.repo_config.get_auth_mode``) and REJECT a mixed set with an
# actionable ``ValueError`` that NAMES the offending repos. A same-name /
# different-owner set is NOT rejected — uniformity is a property of the
# visibility/auth *bucket*, not the bare name (operator ruling #6). A single
# repo (N=1) is trivially uniform.
#
# The helpers are added by the slice-2 *coder* (a parallel BRC producer). Until
# that lands in this tester worktree the import below fails and this whole
# section skips with an explicit reason — it activates automatically at
# convergence, when the coder and tester branches merge. The exact interface
# the tester expects is handed to the coder via the task-2-3 contract gap so
# the two halves converge on the same shape:
#
#   validate_visibility_uniformity(repos: list[str]) -> None
#   validate_auth_mode_uniformity(repos: list[str]) -> None
#       * ``repos`` are ``owner/name`` slugs.
#       * no-op when the set is uniform or has < 2 repos.
#       * raise ValueError naming the offending repos + their buckets on a
#         mixed set. ``internal`` shares the private posture.

import pytest

try:
    from repo_visibility import (  # type: ignore[attr-defined]
        validate_auth_mode_uniformity,
        validate_visibility_uniformity,
    )

    _UNIFORMITY_AVAILABLE = True
    _UNIFORMITY_IMPORT_ERR: str | None = None
except Exception as _exc:  # noqa: BLE001
    _UNIFORMITY_AVAILABLE = False
    _UNIFORMITY_IMPORT_ERR = repr(_exc)


_skip_uniformity = pytest.mark.skipif(
    not _UNIFORMITY_AVAILABLE,
    reason=(
        "slice-2 coder uniformity helpers (validate_visibility_uniformity / "
        "validate_auth_mode_uniformity) not yet integrated into the tester "
        "worktree (parallel producer); activates at convergence. import error: "
        f"{_UNIFORMITY_IMPORT_ERR}"
    ),
)


def _patch_visibility(monkeypatch, mapping):
    """Route visibility resolution to an ``{'owner/repo': visibility}`` map.

    Patches both the module-level convenience function and the checker
    accessor, so the helper resolves correctly whichever seam it calls through.
    """

    def _fake(owner, repo, **_):
        return mapping[f"{owner}/{repo}"]

    monkeypatch.setattr("repo_visibility.get_repo_visibility", _fake, raising=False)

    checker = MagicMock()
    checker.get_visibility.side_effect = lambda owner, repo, **_: mapping[f"{owner}/{repo}"]
    checker.is_private.side_effect = lambda owner, repo, **_: (
        mapping[f"{owner}/{repo}"]
        in (
            "private",
            "internal",
        )
    )
    monkeypatch.setattr("repo_visibility.get_visibility_checker", lambda: checker, raising=False)


def _patch_auth_mode(monkeypatch, mapping):
    """Route auth-mode resolution to a ``{'owner/repo': mode}`` map."""

    def _fake(repo, **_):
        return mapping[repo]

    for target in ("repo_visibility.get_auth_mode", "repo_config.get_auth_mode"):
        monkeypatch.setattr(target, _fake, raising=False)


@_skip_uniformity
class TestVisibilityUniformity:
    """Uniform-visibility submission validation (AC-2)."""

    def test_uniform_private_accepted(self, monkeypatch):
        _patch_visibility(monkeypatch, {"jwbron/a": "private", "jwbron/b": "private"})
        validate_visibility_uniformity(["jwbron/a", "jwbron/b"])  # no raise

    def test_uniform_public_accepted(self, monkeypatch):
        _patch_visibility(monkeypatch, {"jwbron/a": "public", "jwbron/b": "public"})
        validate_visibility_uniformity(["jwbron/a", "jwbron/b"])  # no raise

    def test_mixed_visibility_rejected_names_offenders(self, monkeypatch):
        _patch_visibility(monkeypatch, {"jwbron/priv": "private", "jwbron/pub": "public"})
        with pytest.raises(ValueError) as excinfo:
            validate_visibility_uniformity(["jwbron/priv", "jwbron/pub"])
        msg = str(excinfo.value)
        # The error is actionable: it names the repos across the split.
        assert "jwbron/priv" in msg
        assert "jwbron/pub" in msg

    def test_internal_shares_private_posture(self, monkeypatch):
        # internal is on the private side of the boundary; internal+private is
        # uniform and must NOT be rejected.
        _patch_visibility(monkeypatch, {"jwbron/a": "internal", "jwbron/b": "private"})
        validate_visibility_uniformity(["jwbron/a", "jwbron/b"])  # no raise

    def test_same_name_different_owner_not_rejected(self, monkeypatch):
        # ruling #6: identity is the owner/name slug, not the bare name — a
        # same-name set with a uniform bucket is accepted.
        _patch_visibility(monkeypatch, {"ownerA/foo": "private", "ownerB/foo": "private"})
        validate_visibility_uniformity(["ownerA/foo", "ownerB/foo"])  # no raise

    def test_single_repo_is_trivially_uniform(self, monkeypatch):
        _patch_visibility(monkeypatch, {"jwbron/only": "public"})
        validate_visibility_uniformity(["jwbron/only"])  # no raise


@_skip_uniformity
class TestAuthModeUniformity:
    """Uniform-auth-mode submission validation (AC-2)."""

    def test_uniform_bot_accepted(self, monkeypatch):
        _patch_auth_mode(monkeypatch, {"jwbron/a": "bot", "jwbron/b": "bot"})
        validate_auth_mode_uniformity(["jwbron/a", "jwbron/b"])  # no raise

    def test_uniform_user_accepted(self, monkeypatch):
        _patch_auth_mode(monkeypatch, {"jwbron/a": "user", "jwbron/b": "user"})
        validate_auth_mode_uniformity(["jwbron/a", "jwbron/b"])  # no raise

    def test_mixed_auth_rejected_names_offenders(self, monkeypatch):
        _patch_auth_mode(monkeypatch, {"jwbron/bot": "bot", "jwbron/user": "user"})
        with pytest.raises(ValueError) as excinfo:
            validate_auth_mode_uniformity(["jwbron/bot", "jwbron/user"])
        msg = str(excinfo.value)
        assert "jwbron/user" in msg

    def test_same_name_different_owner_not_rejected(self, monkeypatch):
        _patch_auth_mode(monkeypatch, {"ownerA/foo": "bot", "ownerB/foo": "bot"})
        validate_auth_mode_uniformity(["ownerA/foo", "ownerB/foo"])  # no raise

    def test_single_repo_is_trivially_uniform(self, monkeypatch):
        _patch_auth_mode(monkeypatch, {"jwbron/only": "user"})
        validate_auth_mode_uniformity(["jwbron/only"])  # no raise
