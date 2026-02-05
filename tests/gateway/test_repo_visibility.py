"""Tests for gateway repo_visibility module."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from repo_visibility import (
    CachedVisibility,
    RepoVisibilityChecker,
    VALID_VISIBILITIES,
)


class TestCachedVisibility:
    """Tests for CachedVisibility dataclass."""

    def test_fresh_not_stale(self):
        """Recent cache entry is not stale."""
        cv = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=datetime.now(UTC).timestamp(),
        )
        assert cv.is_stale(60) is False

    def test_old_is_stale(self):
        """Old cache entry is stale."""
        cv = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=datetime.now(UTC).timestamp() - 120,
        )
        assert cv.is_stale(60) is True

    def test_zero_ttl_always_stale(self):
        """Zero TTL means always stale."""
        cv = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=datetime.now(UTC).timestamp(),
        )
        assert cv.is_stale(0) is True

    def test_negative_ttl_always_stale(self):
        """Negative TTL means always stale."""
        cv = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=datetime.now(UTC).timestamp(),
        )
        assert cv.is_stale(-1) is True


class TestValidVisibilities:
    """Tests for VALID_VISIBILITIES constant."""

    def test_has_public(self):
        """Contains public."""
        assert "public" in VALID_VISIBILITIES

    def test_has_private(self):
        """Contains private."""
        assert "private" in VALID_VISIBILITIES

    def test_has_internal(self):
        """Contains internal."""
        assert "internal" in VALID_VISIBILITIES

    def test_only_three(self):
        """Only three valid values."""
        assert len(VALID_VISIBILITIES) == 3


class TestRepoVisibilityChecker:
    """Tests for RepoVisibilityChecker class."""

    def test_init_defaults(self, monkeypatch):
        """Initialize with default TTLs."""
        monkeypatch.delenv("VISIBILITY_CACHE_TTL_READ", raising=False)
        monkeypatch.delenv("VISIBILITY_CACHE_TTL_WRITE", raising=False)
        checker = RepoVisibilityChecker()
        assert checker._read_ttl == 60
        assert checker._write_ttl == 0

    def test_init_custom_ttl(self):
        """Initialize with custom TTLs."""
        checker = RepoVisibilityChecker(read_ttl=120, write_ttl=30)
        assert checker._read_ttl == 120
        assert checker._write_ttl == 30

    def test_init_from_env(self, monkeypatch):
        """TTLs from environment variables."""
        monkeypatch.setenv("VISIBILITY_CACHE_TTL_READ", "300")
        monkeypatch.setenv("VISIBILITY_CACHE_TTL_WRITE", "10")
        checker = RepoVisibilityChecker()
        assert checker._read_ttl == 300
        assert checker._write_ttl == 10

    def test_cache_empty_initially(self):
        """Cache starts empty."""
        checker = RepoVisibilityChecker(read_ttl=60)
        assert len(checker._cache) == 0

    @patch("repo_visibility.requests.get")
    def test_get_visibility_private(self, mock_get):
        """Get private repo visibility."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "private"}
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker(read_ttl=60)
        with patch.object(checker, "_get_tokens", return_value=[("test-token", "test")]):
            result = checker.get_visibility("owner", "private-repo")
            assert result == "private"

    @patch("repo_visibility.requests.get")
    def test_get_visibility_public(self, mock_get):
        """Get public repo visibility."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"visibility": "public"}
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker(read_ttl=60)
        with patch.object(checker, "_get_tokens", return_value=[("test-token", "test")]):
            result = checker.get_visibility("owner", "public-repo")
            assert result == "public"

    def test_cache_hit(self):
        """Cache hit returns cached value."""
        checker = RepoVisibilityChecker(read_ttl=60)
        checker._cache[("owner", "repo")] = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=datetime.now(UTC).timestamp(),
        )
        result = checker.get_visibility("owner", "repo")
        assert result == "private"

    def test_cache_stale_for_write(self):
        """Write operations use write TTL for staleness."""
        checker = RepoVisibilityChecker(read_ttl=60, write_ttl=0)
        checker._cache[("owner", "repo")] = CachedVisibility(
            owner="owner",
            repo="repo",
            visibility="private",
            fetched_at=datetime.now(UTC).timestamp(),
        )
        # Write TTL is 0, so cache is always stale for write operations
        # Without tokens, fetch returns None
        with patch.object(checker, "_get_tokens", return_value=[]):
            result = checker.get_visibility("owner", "repo", for_write=True)
            assert result is None

    @patch("repo_visibility.requests.get")
    def test_api_error_returns_none(self, mock_get):
        """API error returns None (fail closed)."""
        import requests as req_lib

        mock_get.side_effect = req_lib.RequestException("Network error")
        checker = RepoVisibilityChecker(read_ttl=60)
        with patch.object(checker, "_get_tokens", return_value=[("test-token", "test")]):
            result = checker.get_visibility("owner", "repo")
            assert result is None

    @patch("repo_visibility.requests.get")
    def test_404_returns_none(self, mock_get):
        """404 response returns None."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        checker = RepoVisibilityChecker(read_ttl=60)
        with patch.object(checker, "_get_tokens", return_value=[("test-token", "test")]):
            result = checker.get_visibility("owner", "missing-repo")
            assert result is None

    def test_no_tokens_returns_none(self):
        """No tokens returns None."""
        checker = RepoVisibilityChecker(read_ttl=60)
        with patch.object(checker, "_get_tokens", return_value=[]):
            result = checker.get_visibility("owner", "repo")
            assert result is None
