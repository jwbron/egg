"""
Functional tests for git command routing via the gateway.

These tests verify that git commands are properly:
- Routed through the gateway API
- Validated against allowlists
- Blocked for disallowed operations
- Redirected to proper endpoints for network operations

Focus: command routing, argument validation, error message quality.
"""

import pytest

from tests.functional.conftest import GitCommandResult


@pytest.mark.functional
class TestGitCommandRouting:
    """Tests for git command interception and routing."""

    def test_status_command_accepted(self, git_command_tester):
        """git status is a valid operation (routed through gateway)."""
        result: GitCommandResult = git_command_tester("status")
        # May fail with 400 if repo doesn't exist, but should not be auth error
        assert result.status_code not in (401, 403), (
            f"Status command should not be auth-rejected: {result.error}"
        )

    def test_status_with_porcelain_flag(self, git_command_tester):
        """git status --porcelain accepts the porcelain flag."""
        result = git_command_tester("status", args=["--porcelain"])
        assert result.status_code not in (401, 403)

    def test_log_command_accepted(self, git_command_tester):
        """git log is a valid operation."""
        result = git_command_tester("log", args=["--oneline", "-5"])
        assert result.status_code not in (401, 403)

    def test_diff_command_accepted(self, git_command_tester):
        """git diff is a valid operation."""
        result = git_command_tester("diff")
        assert result.status_code not in (401, 403)

    def test_branch_command_accepted(self, git_command_tester):
        """git branch is a valid operation."""
        result = git_command_tester("branch", args=["--list"])
        assert result.status_code not in (401, 403)

    def test_rev_parse_command_accepted(self, git_command_tester):
        """git rev-parse is a valid operation (used for repo detection)."""
        result = git_command_tester("rev-parse", args=["--git-dir"])
        assert result.status_code not in (401, 403)


@pytest.mark.functional
class TestGitCommandBlocking:
    """Tests for git commands that should be blocked."""

    def test_gc_command_blocked(self, git_command_tester):
        """git gc is not in the allowlist and should be blocked."""
        result = git_command_tester("gc")
        assert result.status_code == 403
        assert "not allowed" in result.error.lower() or "gc" in result.error.lower()

    def test_fsck_command_blocked(self, git_command_tester):
        """git fsck is not in the allowlist and should be blocked."""
        result = git_command_tester("fsck")
        assert result.status_code == 403

    def test_prune_command_blocked(self, git_command_tester):
        """git prune is not in the allowlist and should be blocked."""
        result = git_command_tester("prune")
        assert result.status_code == 403

    def test_reflog_command_blocked(self, git_command_tester):
        """git reflog is not in the allowlist (security: could leak history)."""
        result = git_command_tester("reflog")
        assert result.status_code == 403

    def test_arbitrary_command_blocked(self, git_command_tester):
        """Arbitrary commands that aren't real git commands are blocked."""
        result = git_command_tester("notarealcommand")
        assert result.status_code == 403


@pytest.mark.functional
class TestNetworkOperationsRedirect:
    """Tests for network operations that should use dedicated endpoints."""

    def test_push_via_execute_blocked(self, git_command_tester):
        """git push via /git/execute should redirect to /git/push."""
        result = git_command_tester("push")
        # Should be blocked (400 or 403) with message about dedicated endpoint
        assert result.status_code in (400, 403)

    def test_fetch_via_execute_blocked(self, git_command_tester):
        """git fetch via /git/execute should redirect to /git/fetch."""
        result = git_command_tester("fetch")
        assert result.status_code in (400, 403)

    def test_ls_remote_via_execute_blocked(self, git_command_tester):
        """git ls-remote via /git/execute should use dedicated endpoint."""
        result = git_command_tester("ls-remote")
        assert result.status_code in (400, 403)


@pytest.mark.functional
class TestGitArgumentValidation:
    """Tests for git argument validation and sanitization."""

    def test_dangerous_flag_blocked(self, git_command_tester):
        """Dangerous flags like --exec should be blocked."""
        # git log with --exec-path could be exploited
        result = git_command_tester("log", args=["--exec-path=/tmp/malicious"])
        # Should be blocked or sanitized
        assert result.status_code in (400, 403) or not result.success

    def test_add_with_normal_paths(self, git_command_tester):
        """git add with normal file paths is accepted."""
        result = git_command_tester("add", args=["--dry-run", "file.txt"])
        # May fail because file doesn't exist, but should not be 403
        assert result.status_code != 403

    def test_commit_with_message(self, git_command_tester):
        """git commit with -m flag is accepted."""
        result = git_command_tester("commit", args=["-m", "test commit", "--dry-run"])
        # May fail because nothing to commit, but should not be 403
        assert result.status_code != 403

    def test_config_local_accepted(self, git_command_tester):
        """git config for local repo is accepted."""
        result = git_command_tester("config", args=["--get", "user.name"])
        # May fail if config not set, but should not be 403
        assert result.status_code != 403


@pytest.mark.functional
class TestGitRepoPathValidation:
    """Tests for repository path validation."""

    def test_repos_parent_directory_rejected(self, git_command_tester):
        """Running git in the repos parent directory is rejected."""
        result = git_command_tester("status", repo_path="/home/egg/repos")
        # Should fail with a clear error about not being a repo
        assert result.status_code == 400
        assert "not a git repository" in result.error.lower() or "directory" in result.error.lower()

    def test_path_traversal_blocked(self, git_command_tester):
        """Path traversal attempts should be blocked."""
        result = git_command_tester("status", repo_path="/home/egg/repos/../../../etc")
        assert result.status_code in (400, 403)

    def test_absolute_path_outside_repos_blocked(self, git_command_tester):
        """Absolute paths outside allowed directories should be blocked."""
        result = git_command_tester("status", repo_path="/etc/passwd")
        assert result.status_code in (400, 403)


@pytest.mark.functional
class TestGhCommandRouting:
    """Tests for gh (GitHub CLI) command routing."""

    def test_gh_version_works(self, gh_command_tester):
        """gh --version executes successfully."""
        result = gh_command_tester(["--version"])
        # Should succeed or at least not be auth failure
        assert result.status_code != 401
        if result.success:
            assert "gh" in result.output.lower()

    def test_gh_help_works(self, gh_command_tester):
        """gh --help executes successfully."""
        result = gh_command_tester(["--help"])
        assert result.status_code != 401

    def test_gh_api_accessible(self, gh_command_tester):
        """gh api command is accessible (may fail without real auth)."""
        result = gh_command_tester(["api", "--help"])
        # Should be allowed (may fail for auth reasons but not 403)
        assert result.status_code != 403

    def test_gh_pr_list_accessible(self, gh_command_tester):
        """gh pr list command is accessible."""
        result = gh_command_tester(["pr", "list", "--help"])
        assert result.status_code != 403


@pytest.mark.functional
class TestGhCommandBlocking:
    """Tests for gh commands that should be blocked."""

    def test_gh_auth_token_blocked(self, gh_command_tester):
        """gh auth token should not expose credentials."""
        result = gh_command_tester(["auth", "token"])
        # Should either fail or not expose real tokens
        if result.success:
            # If it "succeeds", it shouldn't contain real tokens
            assert "dummy" in result.output or len(result.output.strip()) == 0


@pytest.mark.functional
class TestErrorMessageQuality:
    """Tests for clear, helpful error messages."""

    def test_disallowed_operation_shows_allowed_list(self, git_command_tester):
        """Error for disallowed operation should list allowed operations."""
        result = git_command_tester("gc")
        # Error should mention what IS allowed
        assert "allowed" in result.error.lower()

    def test_network_op_error_mentions_endpoint(self, git_command_tester):
        """Error for network ops should mention the correct endpoint."""
        result = git_command_tester("push")
        # Should mention the dedicated endpoint
        assert "endpoint" in result.error.lower() or "/git/push" in result.error

    def test_repo_path_error_is_clear(self, git_command_tester):
        """Error for invalid repo path should be descriptive."""
        result = git_command_tester("status", repo_path="/home/egg/repos")
        # Should explain why it failed
        assert len(result.error) > 10  # Non-trivial error message


@pytest.mark.functional
class TestApiResponseFormat:
    """Tests for API response format consistency."""

    def test_response_is_json(self, minimal_gateway, functional_session):
        """All responses should be JSON, not HTML error pages."""
        token = functional_session.get("session_token")
        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/git/execute",
            token=token,
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "operation": "status",
            },
        )
        content_type = resp.headers.get("Content-Type", "")
        assert "json" in content_type.lower(), f"Expected JSON, got {content_type}"
        # Should be valid JSON
        resp.json()

    def test_error_response_has_success_field(self, git_command_tester):
        """Error responses should have success=false."""
        result = git_command_tester("gc")  # Blocked operation
        assert result.raw_response.get("success") is False

    def test_error_response_has_message(self, git_command_tester):
        """Error responses should have a message field."""
        result = git_command_tester("gc")
        assert "message" in result.raw_response or "error" in result.raw_response


@pytest.mark.functional
class TestMissingRequestFields:
    """Tests for handling missing required fields."""

    def test_missing_operation_rejected(self, minimal_gateway, functional_session):
        """Request without operation field should be rejected."""
        token = functional_session.get("session_token")
        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/git/execute",
            token=token,
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                # Missing "operation"
            },
        )
        assert resp.status_code == 400

    def test_missing_repo_path_rejected(self, minimal_gateway, functional_session):
        """Request without repo_path field should be rejected."""
        token = functional_session.get("session_token")
        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/git/execute",
            token=token,
            json_data={
                "operation": "status",
                # Missing "repo_path"
            },
        )
        assert resp.status_code == 400

    def test_empty_body_rejected(self, minimal_gateway, functional_session):
        """Request with empty body should be rejected."""
        token = functional_session.get("session_token")
        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/git/execute",
            token=token,
            json_data=None,
        )
        assert resp.status_code == 400
