"""
Tests for git validation functions in Gateway Sidecar.

Tests cover:
- Path validation (prevent path traversal attacks)
- Git argument sanitization
- Flag normalization
- gh API path allowlist validation
"""

import os
import tempfile
from unittest.mock import patch

import git_client
import github_client


class TestRepoPathValidation:
    """Tests for validate_repo_path function."""

    def test_valid_repos_path(self):
        """Valid path under /home/egg/repos/ is accepted."""
        with patch("git_client.os.path.realpath", return_value="/home/egg/repos/myrepo"):
            valid, error = git_client.validate_repo_path("/home/egg/repos/myrepo")
            assert valid is True
            assert error == ""

    def test_valid_worktree_path(self):
        """Valid path under /home/egg/.egg-worktrees/ is accepted."""
        with patch(
            "git_client.os.path.realpath",
            return_value="/home/egg/.egg-worktrees/egg-123/repo",
        ):
            valid, error = git_client.validate_repo_path("/home/egg/.egg-worktrees/egg-123/repo")
            assert valid is True
            assert error == ""

    def test_valid_legacy_repos_path(self):
        """Valid path under /repos/ (legacy) is accepted."""
        with patch("git_client.os.path.realpath", return_value="/repos/myrepo"):
            valid, error = git_client.validate_repo_path("/repos/myrepo")
            assert valid is True
            assert error == ""

    def test_path_traversal_blocked(self):
        """Path traversal attempt via .. is blocked."""
        # Simulate realpath resolving the traversal
        with patch("git_client.os.path.realpath", return_value="/etc/passwd"):
            valid, error = git_client.validate_repo_path("/home/egg/repos/../../../etc/passwd")
            assert valid is False
            assert "allowed directories" in error

    def test_absolute_escape_blocked(self):
        """Absolute path outside allowed dirs is blocked."""
        with patch("git_client.os.path.realpath", return_value="/etc/passwd"):
            valid, error = git_client.validate_repo_path("/etc/passwd")
            assert valid is False
            assert "allowed directories" in error

    def test_empty_path_rejected(self):
        """Empty path is rejected."""
        valid, error = git_client.validate_repo_path("")
        assert valid is False
        assert "required" in error

    def test_none_path_rejected(self):
        """None path is rejected."""
        valid, error = git_client.validate_repo_path(None)
        assert valid is False
        assert "required" in error

    def test_realpath_exception_handled(self):
        """Exception during path resolution is handled gracefully."""
        with patch("git_client.os.path.realpath", side_effect=OSError("Permission denied")):
            valid, error = git_client.validate_repo_path("/some/path")
            assert valid is False
            assert "Invalid repo_path" in error


class TestGitArgsValidation:
    """Tests for validate_git_args function with per-operation allowlists."""

    def test_upload_pack_blocked(self):
        """--upload-pack flag is blocked."""
        valid, error, _ = git_client.validate_git_args("fetch", ["--upload-pack=/evil/cmd"])
        assert valid is False
        assert "not allowed" in error

    def test_exec_blocked(self):
        """--exec flag is blocked."""
        valid, error, _ = git_client.validate_git_args("fetch", ["--exec=/evil/cmd"])
        assert valid is False
        assert "not allowed" in error

    def test_config_override_blocked(self):
        """-c flag (config override) is blocked."""
        valid, error, _ = git_client.validate_git_args(
            "fetch", ["-c", "protocol.file.allow=always"]
        )
        assert valid is False
        assert "not allowed" in error

    def test_upload_pack_case_insensitive(self):
        """Blocking is case-insensitive."""
        valid, error, _ = git_client.validate_git_args("fetch", ["--Upload-Pack=/evil"])
        assert valid is False
        assert "not allowed" in error

    def test_normal_fetch_args_allowed(self):
        """Normal git fetch arguments are allowed."""
        valid, error, args = git_client.validate_git_args("fetch", ["--tags", "--prune", "main"])
        assert valid is True
        assert error == ""
        assert args == ["--tags", "--prune", "main"]

    def test_empty_args_allowed(self):
        """Empty argument list is allowed."""
        valid, error, args = git_client.validate_git_args("fetch", [])
        assert valid is True
        assert error == ""
        assert args == []

    def test_non_string_rejected(self):
        """Non-string arguments are rejected."""
        valid, error, _ = git_client.validate_git_args("fetch", [{"nested": "object"}])
        assert valid is False
        assert "Invalid argument type" in error

    def test_mixed_valid_and_blocked(self):
        """Mixed valid and blocked args are rejected."""
        valid, error, _ = git_client.validate_git_args(
            "fetch", ["--tags", "--upload-pack=/evil", "--prune"]
        )
        assert valid is False
        assert "not allowed" in error

    def test_unknown_flag_rejected(self):
        """Unknown flags are rejected (allowlist, not blocklist)."""
        valid, error, _ = git_client.validate_git_args("fetch", ["--unknown-flag"])
        assert valid is False
        assert "not allowed" in error
        assert "Allowed flags:" in error

    def test_ls_remote_flags(self):
        """ls-remote operation has its own allowed flags."""
        valid, error, _args = git_client.validate_git_args("ls-remote", ["--heads", "--tags"])
        assert valid is True
        assert error == ""

    def test_ls_remote_rejects_fetch_only_flags(self):
        """ls-remote rejects flags only allowed for fetch."""
        valid, error, _ = git_client.validate_git_args("ls-remote", ["--all"])
        assert valid is False
        assert "not allowed" in error

    def test_push_flags(self):
        """push operation has its own allowed flags."""
        valid, error, _args = git_client.validate_git_args("push", ["--force", "--set-upstream"])
        assert valid is True
        assert error == ""

    def test_unknown_operation_rejected(self):
        """Unknown operation is rejected."""
        valid, error, _ = git_client.validate_git_args("unknown", ["--flag"])
        assert valid is False
        assert "Unknown operation" in error

    def test_short_flags_normalized(self):
        """Short flags are normalized to long form."""
        valid, _error, args = git_client.validate_git_args("fetch", ["-t", "-p"])
        assert valid is True
        assert args == ["--tags", "--prune"]

    def test_refs_pass_through(self):
        """Non-flag arguments (refs, branch names) pass through."""
        valid, _error, args = git_client.validate_git_args("fetch", ["origin", "main", "--tags"])
        assert valid is True
        assert "origin" in args
        assert "main" in args

    def test_apply_allowed_flags(self):
        """git apply accepts its allowed flags."""
        valid, error, args = git_client.validate_git_args(
            "apply", ["--check", "--stat", "--3way", "patch.diff"]
        )
        assert valid is True
        assert error == ""
        assert "--check" in args
        assert "patch.diff" in args

    def test_apply_rejects_unknown_flags(self):
        """git apply rejects unknown flags."""
        valid, error, _ = git_client.validate_git_args("apply", ["--exec=evil"])
        assert valid is False
        assert "not allowed" in error

    def test_apply_file_paths_pass_through(self):
        """git apply passes file path arguments through."""
        valid, error, args = git_client.validate_git_args("apply", ["--verbose", "fix.patch"])
        assert valid is True
        assert "fix.patch" in args

    def test_diff_filter_flag(self):
        """git diff accepts --diff-filter flag with value."""
        valid, error, args = git_client.validate_git_args(
            "diff", ["--name-only", "--diff-filter=U"]
        )
        assert valid is True
        assert error == ""
        assert "--diff-filter=U" in args

    def test_log_diff_filter_flag(self):
        """git log accepts --diff-filter flag."""
        valid, error, args = git_client.validate_git_args("log", ["--oneline", "--diff-filter=M"])
        assert valid is True
        assert error == ""

    def test_format_patch_allowed_flags(self):
        """git format-patch accepts its allowed flags."""
        valid, error, args = git_client.validate_git_args("format-patch", ["--stdout", "HEAD~1"])
        assert valid is True
        assert error == ""
        assert "--stdout" in args
        assert "HEAD~1" in args

    def test_format_patch_rejects_unknown_flags(self):
        """git format-patch rejects unknown flags."""
        valid, error, _ = git_client.validate_git_args("format-patch", ["--exec=evil"])
        assert valid is False
        assert "not allowed" in error

    def test_format_patch_output_directory(self):
        """git format-patch accepts -o flag for output directory."""
        valid, error, args = git_client.validate_git_args(
            "format-patch", ["-o", "/tmp/patches", "HEAD~3"]
        )
        assert valid is True
        assert "-o" in args

    def test_format_patch_numbered_long_flag(self):
        """git format-patch accepts --numbered flag."""
        valid, error, args = git_client.validate_git_args("format-patch", ["--numbered", "HEAD~3"])
        assert valid is True
        assert "--numbered" in args

    def test_format_patch_short_n_rejected(self):
        """git format-patch -n is rejected because FLAG_NORMALIZATION maps -n to --dry-run.

        Users should use --numbered instead. This matches how log and reflog
        handle the same -n normalization conflict.
        """
        valid, error, _ = git_client.validate_git_args("format-patch", ["-n", "HEAD~3"])
        assert valid is False
        assert "not allowed" in error

    def test_format_patch_numeric_flag(self):
        """git format-patch accepts numeric flags like -1, -3."""
        valid, error, args = git_client.validate_git_args("format-patch", ["-1", "HEAD"])
        assert valid is True
        assert "-1" in args


class TestFlagNormalization:
    """Tests for normalize_flag function."""

    def test_short_to_long_fetch(self):
        """Short flags are normalized to long form for fetch."""
        assert git_client.normalize_flag("-a") == "--all"
        assert git_client.normalize_flag("-t") == "--tags"
        assert git_client.normalize_flag("-p") == "--prune"
        assert git_client.normalize_flag("-v") == "--verbose"
        assert git_client.normalize_flag("-q") == "--quiet"
        assert git_client.normalize_flag("-j") == "--jobs"

    def test_short_to_long_push(self):
        """Short flags are normalized to long form for push."""
        assert git_client.normalize_flag("-f") == "--force"
        assert git_client.normalize_flag("-d") == "--delete"
        assert git_client.normalize_flag("-n") == "--dry-run"

    def test_long_flags_unchanged(self):
        """Long flags are returned unchanged."""
        assert git_client.normalize_flag("--all") == "--all"
        assert git_client.normalize_flag("--force") == "--force"
        assert git_client.normalize_flag("--verbose") == "--verbose"

    def test_unknown_flags_unchanged(self):
        """Unknown flags are returned unchanged."""
        assert git_client.normalize_flag("--unknown-flag") == "--unknown-flag"
        assert git_client.normalize_flag("-x") == "-x"

    def test_flag_with_value_normalized(self):
        """Flags with = values have base normalized."""
        assert git_client.normalize_flag("-j=4") == "--jobs=4"
        assert git_client.normalize_flag("-f=foo") == "--force=foo"

    def test_long_flag_with_value_unchanged(self):
        """Long flags with values are unchanged."""
        assert git_client.normalize_flag("--jobs=4") == "--jobs=4"


class TestGhApiPathValidation:
    """Tests for validate_gh_api_path function."""

    def test_pr_list_allowed(self):
        """PR list endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/pulls")
        assert valid is True
        assert error == ""

    def test_pr_view_allowed(self):
        """PR view endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/pulls/123")
        assert valid is True
        assert error == ""

    def test_pr_comments_allowed(self):
        """PR comments endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/pulls/123/comments")
        assert valid is True
        assert error == ""

    def test_pr_reviews_allowed(self):
        """PR reviews endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/pulls/123/reviews")
        assert valid is True
        assert error == ""

    def test_issue_list_allowed(self):
        """Issue list endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/issues")
        assert valid is True
        assert error == ""

    def test_issue_view_allowed(self):
        """Issue view endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/issues/456")
        assert valid is True
        assert error == ""

    def test_issue_comment_by_id_allowed(self):
        """Fetching a specific issue/PR comment by ID is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/issues/comments/3836710182"
        )
        assert valid is True
        assert error == ""

    def test_pr_review_comment_by_id_allowed(self):
        """Fetching a specific PR review comment by ID is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/pulls/comments/123456789"
        )
        assert valid is True
        assert error == ""

    def test_issue_events_allowed(self):
        """Issue events endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/issues/123/events")
        assert valid is True
        assert error == ""

    def test_issue_timeline_allowed(self):
        """Issue timeline endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/issues/123/timeline")
        assert valid is True
        assert error == ""

    def test_commit_comments_allowed(self):
        """Commit comments endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/commits/abc123def456/comments"
        )
        assert valid is True
        assert error == ""

    def test_commit_comment_by_id_allowed(self):
        """Fetching a specific commit comment by ID is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/comments/987654321")
        assert valid is True
        assert error == ""

    def test_releases_list_allowed(self):
        """Releases list endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/releases")
        assert valid is True
        assert error == ""

    def test_release_by_id_allowed(self):
        """Specific release endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/releases/12345")
        assert valid is True
        assert error == ""

    def test_release_latest_allowed(self):
        """Latest release endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/releases/latest")
        assert valid is True
        assert error == ""

    def test_release_by_tag_allowed(self):
        """Release by tag endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/releases/tags/v1.0.0")
        assert valid is True
        assert error == ""

    def test_repo_info_allowed(self):
        """Repo info endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo")
        assert valid is True
        assert error == ""

    def test_branches_allowed(self):
        """Branches endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/branches")
        assert valid is True
        assert error == ""

    def test_git_ref_singular_allowed(self):
        """Single git ref endpoint is allowed (e.g., git/ref/heads/main)."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/git/ref/heads/main"
        )
        assert valid is True
        assert error == ""

    def test_git_ref_singular_nested_allowed(self):
        """Nested git ref paths are allowed (e.g., git/ref/heads/egg/issue-451)."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/git/ref/heads/egg/issue-451"
        )
        assert valid is True
        assert error == ""

    def test_user_info_allowed(self):
        """User info endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("user")
        assert valid is True
        assert error == ""

    def test_leading_slash_stripped(self):
        """Leading slash is stripped before validation."""
        valid, error = github_client.validate_gh_api_path("/repos/owner/repo/pulls")
        assert valid is True
        assert error == ""

    def test_unknown_path_blocked(self):
        """Unknown API paths are blocked."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/unknown")
        assert valid is False
        assert "not in allowlist" in error

    def test_admin_paths_blocked(self):
        """Admin/dangerous paths are blocked."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/hooks")
        assert valid is False
        assert "not in allowlist" in error

    def test_delete_method_blocked(self):
        """DELETE method is blocked."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/pulls/123", method="DELETE"
        )
        assert valid is False
        assert "method" in error.lower()

    def test_get_method_allowed(self):
        """GET method is allowed."""
        valid, _error = github_client.validate_gh_api_path("repos/owner/repo/pulls", method="GET")
        assert valid is True

    def test_post_method_allowed(self):
        """POST method is allowed."""
        valid, _error = github_client.validate_gh_api_path(
            "repos/owner/repo/pulls/123/comments", method="POST"
        )
        assert valid is True

    def test_patch_method_allowed(self):
        """PATCH method is allowed."""
        valid, _error = github_client.validate_gh_api_path(
            "repos/owner/repo/pulls/123", method="PATCH"
        )
        assert valid is True

    def test_issue_reactions_allowed(self):
        """Issue reactions endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/issues/123/reactions")
        assert valid is True
        assert error == ""

    def test_issue_comment_reactions_allowed(self):
        """Issue comment reactions endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/issues/comments/456/reactions"
        )
        assert valid is True
        assert error == ""

    def test_pr_review_comment_reactions_allowed(self):
        """PR review comment reactions endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/pulls/comments/789/reactions"
        )
        assert valid is True
        assert error == ""

    def test_actions_runs_list_allowed(self):
        """Actions runs list endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/actions/runs")
        assert valid is True
        assert error == ""

    def test_actions_run_by_id_allowed(self):
        """Specific actions run endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/actions/runs/21738507325"
        )
        assert valid is True
        assert error == ""

    def test_actions_run_jobs_allowed(self):
        """Actions run jobs endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/actions/runs/12345/jobs"
        )
        assert valid is True
        assert error == ""

    def test_actions_run_logs_allowed(self):
        """Actions run logs endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/actions/runs/12345/logs"
        )
        assert valid is True
        assert error == ""

    def test_actions_run_artifacts_allowed(self):
        """Actions run artifacts endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/actions/runs/12345/artifacts"
        )
        assert valid is True
        assert error == ""

    def test_actions_job_by_id_allowed(self):
        """Specific actions job endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/actions/jobs/98765")
        assert valid is True
        assert error == ""

    def test_actions_job_logs_allowed(self):
        """Actions job logs endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/actions/jobs/98765/logs"
        )
        assert valid is True
        assert error == ""

    def test_actions_workflows_list_allowed(self):
        """Actions workflows list endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/actions/workflows")
        assert valid is True
        assert error == ""

    def test_actions_workflow_by_id_allowed(self):
        """Specific actions workflow endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/actions/workflows/456")
        assert valid is True
        assert error == ""

    def test_actions_workflow_runs_allowed(self):
        """Actions workflow runs endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/actions/workflows/456/runs"
        )
        assert valid is True
        assert error == ""

    def test_actions_artifacts_list_allowed(self):
        """Actions repo artifacts endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/actions/artifacts")
        assert valid is True
        assert error == ""

    def test_actions_artifact_by_id_allowed(self):
        """Specific actions artifact endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/actions/artifacts/789")
        assert valid is True
        assert error == ""

    def test_check_run_by_id_allowed(self):
        """Check run endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/check-runs/12345")
        assert valid is True
        assert error == ""

    def test_check_suite_by_id_allowed(self):
        """Check suite endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/check-suites/67890")
        assert valid is True
        assert error == ""

    def test_check_suite_check_runs_allowed(self):
        """Check runs in suite endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/check-suites/67890/check-runs"
        )
        assert valid is True
        assert error == ""

    def test_commit_check_runs_allowed(self):
        """Check runs for commit endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/commits/abc123def/check-runs"
        )
        assert valid is True
        assert error == ""

    def test_commit_check_suites_allowed(self):
        """Check suites for commit endpoint is allowed."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/commits/abc123def/check-suites"
        )
        assert valid is True
        assert error == ""

    def test_actions_run_cancel_blocked(self):
        """Actions run cancel endpoint is NOT allowed (not in allowlist)."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/actions/runs/12345/cancel"
        )
        assert valid is False
        assert "not in allowlist" in error

    def test_actions_run_rerun_blocked(self):
        """Actions run rerun endpoint is NOT allowed (not in allowlist)."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/actions/runs/12345/rerun"
        )
        assert valid is False
        assert "not in allowlist" in error

    def test_actions_workflow_dispatches_blocked(self):
        """Actions workflow dispatch endpoint is NOT allowed (not in allowlist)."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/actions/workflows/456/dispatches"
        )
        assert valid is False
        assert "not in allowlist" in error

    def test_actions_run_id_must_be_numeric(self):
        """Actions run ID must be numeric."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/actions/runs/abc")
        assert valid is False
        assert "not in allowlist" in error

    def test_pr_number_must_be_numeric(self):
        """PR number must be numeric."""
        valid, error = github_client.validate_gh_api_path("repos/owner/repo/pulls/abc")
        assert valid is False
        assert "not in allowlist" in error

    def test_query_parameters_stripped(self):
        """Query parameters should be stripped before matching."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/actions/runs?event=pull_request&per_page=15"
        )
        assert valid is True
        assert error == ""

    def test_query_parameters_stripped_pulls(self):
        """Query parameters on pulls endpoint should be stripped."""
        valid, error = github_client.validate_gh_api_path(
            "repos/owner/repo/pulls?state=open&per_page=10"
        )
        assert valid is True
        assert error == ""


class TestParseGhApiArgs:
    """Tests for parse_gh_api_args function."""

    def test_simple_path(self):
        """Simple path without flags is parsed correctly."""
        path, method = github_client.parse_gh_api_args(["repos/owner/repo/pulls"])
        assert path == "repos/owner/repo/pulls"
        assert method == "GET"

    def test_path_with_leading_slash(self):
        """Path with leading slash is parsed correctly."""
        path, method = github_client.parse_gh_api_args(["/repos/owner/repo/pulls"])
        assert path == "/repos/owner/repo/pulls"
        assert method == "GET"

    def test_method_flag_short(self):
        """Short -X flag for method is parsed correctly."""
        path, method = github_client.parse_gh_api_args(
            ["-X", "PATCH", "repos/owner/repo/pulls/123"]
        )
        assert path == "repos/owner/repo/pulls/123"
        assert method == "PATCH"

    def test_method_flag_long(self):
        """Long --method flag is parsed correctly."""
        path, method = github_client.parse_gh_api_args(
            ["--method", "POST", "repos/owner/repo/issues"]
        )
        assert path == "repos/owner/repo/issues"
        assert method == "POST"

    def test_method_flag_equals_format(self):
        """Method flag with = format is parsed correctly."""
        path, method = github_client.parse_gh_api_args(
            ["--method=PATCH", "repos/owner/repo/pulls/123"]
        )
        assert path == "repos/owner/repo/pulls/123"
        assert method == "PATCH"

    def test_method_flag_short_equals_format(self):
        """Short method flag with = format is parsed correctly."""
        path, method = github_client.parse_gh_api_args(["-X=POST", "repos/owner/repo/issues"])
        assert path == "repos/owner/repo/issues"
        assert method == "POST"

    def test_multiple_flags_before_path(self):
        """Multiple flags before path are skipped correctly."""
        path, method = github_client.parse_gh_api_args(
            ["-X", "PATCH", "-H", "Accept: application/json", "repos/owner/repo/pulls/123"]
        )
        assert path == "repos/owner/repo/pulls/123"
        assert method == "PATCH"

    def test_field_flags_skipped(self):
        """Field flags (-f, -F) with values are skipped correctly."""
        path, method = github_client.parse_gh_api_args(
            ["-X", "PATCH", "repos/owner/repo/pulls/123", "-f", "base=main"]
        )
        assert path == "repos/owner/repo/pulls/123"
        assert method == "PATCH"

    def test_repo_flag_skipped(self):
        """Repo flag (-R, --repo) with value is skipped correctly."""
        path, method = github_client.parse_gh_api_args(
            ["--repo", "owner/repo", "repos/owner/repo/pulls"]
        )
        assert path == "repos/owner/repo/pulls"
        assert method == "GET"

    def test_boolean_flags_skipped(self):
        """Boolean flags (--paginate, --silent, etc.) are skipped correctly."""
        path, method = github_client.parse_gh_api_args(
            ["--paginate", "--silent", "repos/owner/repo/issues"]
        )
        assert path == "repos/owner/repo/issues"
        assert method == "GET"

    def test_jq_flag_skipped(self):
        """JQ flag with query is skipped correctly."""
        path, method = github_client.parse_gh_api_args(
            ["repos/owner/repo/pulls", "--jq", ".[] | .number"]
        )
        assert path == "repos/owner/repo/pulls"
        assert method == "GET"

    def test_complex_command(self):
        """Complex command with many flags is parsed correctly."""
        # gh api -X POST -H "Accept: application/json" --repo owner/repo repos/owner/repo/pulls/123/comments -f body="test"
        path, method = github_client.parse_gh_api_args(
            [
                "-X",
                "POST",
                "-H",
                "Accept: application/json",
                "--repo",
                "owner/repo",
                "repos/owner/repo/pulls/123/comments",
                "-f",
                "body=test",
            ]
        )
        assert path == "repos/owner/repo/pulls/123/comments"
        assert method == "POST"

    def test_no_path_returns_none(self):
        """When no path is provided, returns None."""
        path, method = github_client.parse_gh_api_args(["-X", "POST"])
        assert path is None
        assert method == "POST"

    def test_empty_args_returns_none(self):
        """Empty args list returns None path."""
        path, method = github_client.parse_gh_api_args([])
        assert path is None
        assert method == "GET"

    def test_only_flags_returns_none(self):
        """Only flags without path returns None."""
        path, method = github_client.parse_gh_api_args(
            ["-X", "PATCH", "-H", "Accept: application/json", "-f", "key=value"]
        )
        assert path is None
        assert method == "PATCH"

    def test_method_case_insensitive(self):
        """Method value is uppercased."""
        _path, method = github_client.parse_gh_api_args(["-X", "patch", "repos/owner/repo"])
        assert method == "PATCH"

    def test_unknown_flags_skipped(self):
        """Unknown flags are skipped (defensive behavior)."""
        path, method = github_client.parse_gh_api_args(["--unknown-flag", "repos/owner/repo/pulls"])
        assert path == "repos/owner/repo/pulls"
        assert method == "GET"


class TestSharedHelperFunctions:
    """Tests for shared credential helper functions."""

    def test_cleanup_credential_helper_with_none(self):
        """cleanup_credential_helper handles None gracefully."""
        # Should not raise
        git_client.cleanup_credential_helper(None)

    def test_cleanup_credential_helper_with_nonexistent(self):
        """cleanup_credential_helper handles nonexistent path gracefully."""
        # Should not raise
        git_client.cleanup_credential_helper("/nonexistent/path/to/file")

    def test_cleanup_credential_helper_removes_file(self):
        """cleanup_credential_helper removes existing file."""
        # Create a temp file
        fd, path = tempfile.mkstemp()
        os.close(fd)
        assert os.path.exists(path)

        git_client.cleanup_credential_helper(path)

        assert not os.path.exists(path)

    def test_create_credential_helper_creates_file(self):
        """create_credential_helper creates executable file."""
        env = {}
        path, updated_env = git_client.create_credential_helper("test-token", env)

        try:
            assert os.path.exists(path)
            # Check it's executable
            assert os.access(path, os.X_OK)
            # Check env was updated
            assert updated_env["GIT_ASKPASS"] == path
            assert updated_env["GIT_USERNAME"] == "x-access-token"
            assert updated_env["GIT_PASSWORD"] == "test-token"
            assert updated_env["GIT_TERMINAL_PROMPT"] == "0"
        finally:
            git_client.cleanup_credential_helper(path)

    def test_create_credential_helper_doesnt_modify_original_env(self):
        """create_credential_helper doesn't modify the original env dict."""
        original_env = {"EXISTING": "value"}
        path, updated_env = git_client.create_credential_helper("token", original_env)

        try:
            # Original should be unchanged
            assert "GIT_ASKPASS" not in original_env
            assert "EXISTING" in original_env
            # Updated should have both
            assert "GIT_ASKPASS" in updated_env
            assert "EXISTING" in updated_env
        finally:
            git_client.cleanup_credential_helper(path)
