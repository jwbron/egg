"""
Tests for gateway configuration reload functionality.

Tests cover:
- repo_config.reload_config() clears caches
- policy.reload_policy_caches() clears bot identity and trusted user caches
- SIGHUP handler triggers config reload
- /api/v1/config/reload endpoint triggers config reload with auth
"""

import os

import pytest

# conftest.py sets up the module loading and TEST_LAUNCHER_SECRET
TEST_LAUNCHER_SECRET = os.environ.get("EGG_LAUNCHER_SECRET", "test-launcher-secret-12345")

import policy

import gateway


@pytest.fixture
def client():
    """Create test client for Flask app."""
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


@pytest.fixture
def launcher_auth_headers():
    """Return valid launcher authentication headers."""
    return {"Authorization": f"Bearer {TEST_LAUNCHER_SECRET}"}


class TestRepoConfigReload:
    """Tests for config/repo_config.py reload_config()."""

    def test_reload_clears_checkpoint_repos_cache(self):
        """reload_config() should clear the checkpoint repos cache."""
        from config.repo_config import (
            get_all_checkpoint_repos,
            reload_config,
        )

        # Prime the cache
        get_all_checkpoint_repos()
        from config import repo_config

        assert repo_config._checkpoint_repos_cache is not None

        # Reload should clear it
        reload_config()
        assert repo_config._checkpoint_repos_cache is None

    def test_reload_allows_fresh_read(self):
        """After reload, the next call re-reads from disk."""
        from config.repo_config import get_all_checkpoint_repos, reload_config

        # Prime cache
        get_all_checkpoint_repos()
        reload_config()
        # Should not raise — re-reads config
        result2 = get_all_checkpoint_repos()
        assert isinstance(result2, frozenset)


class TestPolicyReload:
    """Tests for policy.py reload functions."""

    def test_reload_policy_caches_clears_bot_caches(self):
        """reload_policy_caches() should clear bot identity caches."""
        # Prime caches
        policy.get_bot_identities()
        policy.get_bot_branch_prefixes()

        assert policy._bot_identities_cache is not None
        assert policy._bot_branch_prefixes_cache is not None

        policy.reload_policy_caches()

        assert policy._bot_identities_cache is None
        assert policy._bot_branch_prefixes_cache is None

    def test_reload_trusted_users_re_reads_env(self):
        """reload_trusted_users() should re-read GATEWAY_TRUSTED_USERS."""
        original = policy.TRUSTED_BRANCH_OWNERS

        os.environ["GATEWAY_TRUSTED_USERS"] = "alice,bob"
        try:
            policy.reload_trusted_users()
            assert policy.TRUSTED_BRANCH_OWNERS == frozenset({"alice", "bob"})
        finally:
            # Restore
            if original:
                os.environ["GATEWAY_TRUSTED_USERS"] = ",".join(original)
            else:
                os.environ.pop("GATEWAY_TRUSTED_USERS", None)
            policy.reload_trusted_users()

    def test_reload_policy_caches_reloads_trusted_users(self):
        """reload_policy_caches() should also reload trusted users."""
        original = policy.TRUSTED_BRANCH_OWNERS

        os.environ["GATEWAY_TRUSTED_USERS"] = "charlie"
        try:
            policy.reload_policy_caches()
            assert "charlie" in policy.TRUSTED_BRANCH_OWNERS
        finally:
            if original:
                os.environ["GATEWAY_TRUSTED_USERS"] = ",".join(original)
            else:
                os.environ.pop("GATEWAY_TRUSTED_USERS", None)
            policy.reload_policy_caches()


class TestConfigReloadEndpoint:
    """Tests for POST /api/v1/config/reload endpoint."""

    def test_reload_requires_auth(self, client):
        """Reload endpoint should require launcher authentication."""
        response = client.post("/api/v1/config/reload")
        assert response.status_code == 401

    def test_reload_rejects_bad_auth(self, client):
        """Reload endpoint should reject invalid auth tokens."""
        response = client.post(
            "/api/v1/config/reload",
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert response.status_code == 401

    def test_reload_succeeds_with_auth(self, client, launcher_auth_headers):
        """Reload endpoint should succeed with valid launcher auth."""
        response = client.post(
            "/api/v1/config/reload",
            headers=launcher_auth_headers,
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "ok"
        assert "reloaded" in data["message"].lower()

    def test_reload_clears_caches(self, client, launcher_auth_headers):
        """Reload endpoint should clear config caches."""
        # Prime policy caches
        policy.get_bot_identities()
        assert policy._bot_identities_cache is not None

        response = client.post(
            "/api/v1/config/reload",
            headers=launcher_auth_headers,
        )
        assert response.status_code == 200

        # Caches should be cleared
        assert policy._bot_identities_cache is None


class TestSighupHandler:
    """Tests for SIGHUP-based config reload."""

    def test_reload_all_config_clears_policy_caches(self):
        """_reload_all_config() should clear policy caches."""
        # Prime caches
        policy.get_bot_identities()
        assert policy._bot_identities_cache is not None

        gateway._reload_all_config()

        assert policy._bot_identities_cache is None

    def test_reload_all_config_clears_repo_config_cache(self):
        """_reload_all_config() should clear repo_config caches."""
        from config import repo_config
        from config.repo_config import get_all_checkpoint_repos

        get_all_checkpoint_repos()
        assert repo_config._checkpoint_repos_cache is not None

        gateway._reload_all_config()

        assert repo_config._checkpoint_repos_cache is None
