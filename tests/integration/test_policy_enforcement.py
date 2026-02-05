"""Integration tests for policy enforcement and blocked operations.

Ported from gateway/tests/integration_test.sh lines 409-457.
Verifies the gateway blocks dangerous operations like PR merge,
repo delete, and enforces branch ownership rules.
"""

import pytest


@pytest.mark.integration
@pytest.mark.security
class TestBlockedOperations:
    """Tests that dangerous gh operations are blocked by policy."""

    def test_gh_pr_merge_blocked(self, egg_stack, session):
        """gh pr merge is blocked -- this is the PRIMARY security control.

        The gateway MUST block all merge operations. Human review is required.
        """
        token = session.get("session_token")
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/gh/execute",
            token=token,
            json_data={
                "args": ["pr", "merge", "1"],
            },
        )
        # Should be rejected by policy
        body = resp.json()
        assert body.get("success") is not True, "SECURITY VIOLATION: gh pr merge was not blocked"

    def test_gh_repo_delete_blocked(self, egg_stack, session):
        """gh repo delete is blocked."""
        token = session.get("session_token")
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/gh/execute",
            token=token,
            json_data={
                "args": ["repo", "delete", "test-owner/test-repo", "--yes"],
            },
        )
        body = resp.json()
        assert body.get("success") is not True, "SECURITY VIOLATION: gh repo delete was not blocked"

    def test_gh_repo_create_blocked(self, egg_stack, session):
        """gh repo create is blocked."""
        token = session.get("session_token")
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/gh/execute",
            token=token,
            json_data={
                "args": ["repo", "create", "malicious-repo", "--public"],
            },
        )
        body = resp.json()
        assert body.get("success") is not True, "SECURITY VIOLATION: gh repo create was not blocked"


@pytest.mark.integration
@pytest.mark.security
class TestBranchOwnership:
    """Tests for branch ownership enforcement on git push."""

    def test_push_to_non_prefixed_branch_blocked(self, egg_stack, session):
        """Push to a branch without the bot prefix is blocked."""
        token = session.get("session_token")
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/git/push",
            token=token,
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "remote": "origin",
                "refspec": "my-feature:my-feature",
            },
        )
        # Should be blocked by branch ownership policy
        # May also fail because repo doesn't exist, but should never
        # indicate that a push to a non-prefixed branch was allowed.
        if resp.status_code == 200:
            body = resp.json()
            assert body.get("success") is not True, (
                "Push to non-prefixed branch should be blocked by policy"
            )

    def test_push_to_egg_prefixed_branch_allowed(self, egg_stack, session):
        """Push to an egg-prefixed branch is allowed by policy.

        Note: This test verifies the policy check passes, not that the
        push actually succeeds (which requires a real repo and remote).
        The assertion is that we get past the policy check (no 403)
        and fail for a different reason (repo not found, etc).
        """
        token = session.get("session_token")
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/git/push",
            token=token,
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "remote": "origin",
                "refspec": "HEAD:egg-test-branch",
                "args": ["--dry-run"],
            },
        )
        # If we get 403, it means policy rejected the egg-prefix branch
        # which would be a bug. Other errors (400/500) are expected because
        # the repo doesn't exist on disk.
        if resp.status_code == 403:
            body = resp.json()
            msg = body.get("message", "")
            # Only fail if the 403 is specifically about branch ownership
            if "branch" in msg.lower() or "policy" in msg.lower() or "ownership" in msg.lower():
                pytest.fail(f"Push to egg-prefixed branch was blocked by policy: {msg}")
