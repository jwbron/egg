"""
Tests for the sandbox gh CLI wrapper script.

Tests the call_gateway response parsing and output behavior.
The gh wrapper routes all commands through the gateway sidecar,
which returns JSON responses that must be parsed correctly.
"""

import json
import os
import subprocess
import tempfile
import textwrap

# Path to the wrapper scripts
GH_WRAPPER = os.path.join(os.path.dirname(__file__), "..", "..", "sandbox", "scripts", "gh")
GIT_WRAPPER = os.path.join(os.path.dirname(__file__), "..", "..", "sandbox", "scripts", "git")


# The Python parsing logic embedded in call_gateway, kept in sync here for testing.
# If the inline Python in sandbox/scripts/gh changes, update this constant too.
CALL_GATEWAY_PYTHON = textwrap.dedent("""\
    import json, sys

    try:
        with open(sys.argv[1]) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f'ERROR: Failed to parse gateway response: {e}', file=sys.stderr)
        sys.exit(1)

    if data.get('success'):
        stdout = (data.get('data') or {}).get('stdout', '')
        if stdout:
            sys.stdout.write(stdout)
            if not stdout.endswith('\\n'):
                sys.stdout.write('\\n')
        sys.exit(0)
    else:
        message = data.get('message', 'Unknown error')
        print(f'ERROR: {message}', file=sys.stderr)
        stderr_out = (data.get('data') or {}).get('stderr', '')
        if stderr_out:
            print(stderr_out, file=sys.stderr)
        http = sys.argv[2]
        if http == '401':
            print('Authentication failed - check session token', file=sys.stderr)
        elif http == '429':
            print('Rate limit exceeded - please wait before trying again', file=sys.stderr)
        sys.exit(1)
""")


def run_call_gateway_raw(raw_content: str, http_code: str = "200") -> subprocess.CompletedProcess:
    """Run the call_gateway Python parser against a file with raw content.

    Use this when testing malformed input (invalid JSON, empty files, etc.)
    where json.dump() isn't appropriate.
    """
    tmpfile = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(raw_content)
            tmpfile = f.name

        return subprocess.run(
            ["python3", "-c", CALL_GATEWAY_PYTHON, tmpfile, http_code],
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        if tmpfile and os.path.exists(tmpfile):
            os.unlink(tmpfile)


def run_call_gateway_python(
    response_data: dict, http_code: str = "200"
) -> subprocess.CompletedProcess:
    """Run the Python parsing logic from call_gateway against a test response.

    This uses the same Python code embedded in call_gateway and runs it against
    a temp file containing the response data, verifying parsing behavior
    without needing a real gateway.
    """
    tmpfile = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(response_data, f)
            tmpfile = f.name

        return subprocess.run(
            ["python3", "-c", CALL_GATEWAY_PYTHON, tmpfile, http_code],
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        if tmpfile and os.path.exists(tmpfile):
            os.unlink(tmpfile)


class TestCallGatewayParsing:
    """Test the Python response parsing logic used in call_gateway."""

    def test_success_response_outputs_stdout(self):
        """Successful response should output the stdout field."""
        response = {
            "success": True,
            "message": "Command executed",
            "data": {
                "stdout": "hello world\n",
                "stderr": "",
                "returncode": 0,
            },
        }
        result = run_call_gateway_python(response)
        assert result.returncode == 0
        assert result.stdout == "hello world\n"
        assert result.stderr == ""

    def test_success_response_adds_trailing_newline(self):
        """Output without trailing newline should get one added."""
        response = {
            "success": True,
            "message": "Command executed",
            "data": {
                "stdout": "no newline",
                "stderr": "",
                "returncode": 0,
            },
        }
        result = run_call_gateway_python(response)
        assert result.returncode == 0
        assert result.stdout == "no newline\n"

    def test_success_response_preserves_existing_newline(self):
        """Output already ending with newline should not get a double newline."""
        response = {
            "success": True,
            "message": "Command executed",
            "data": {
                "stdout": "has newline\n",
                "stderr": "",
                "returncode": 0,
            },
        }
        result = run_call_gateway_python(response)
        assert result.returncode == 0
        assert result.stdout == "has newline\n"
        assert not result.stdout.endswith("\n\n")

    def test_success_response_empty_stdout(self):
        """Successful response with empty stdout should produce no output."""
        response = {
            "success": True,
            "message": "Command executed",
            "data": {
                "stdout": "",
                "stderr": "",
                "returncode": 0,
            },
        }
        result = run_call_gateway_python(response)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_success_response_json_stdout(self):
        """Successful response containing JSON (e.g., gh api output) should be valid."""
        api_response = [
            {"id": 1, "body": "comment 1", "user": {"login": "alice"}},
            {"id": 2, "body": "comment 2", "user": {"login": "bob"}},
        ]
        response = {
            "success": True,
            "message": "Command executed",
            "data": {
                "stdout": json.dumps(api_response),
                "stderr": "",
                "returncode": 0,
            },
        }
        result = run_call_gateway_python(response)
        assert result.returncode == 0
        # Output should be parseable as JSON
        parsed = json.loads(result.stdout)
        assert len(parsed) == 2
        assert parsed[0]["user"]["login"] == "alice"

    def test_success_response_large_stdout(self):
        """Large responses (simulating gh api with many comments) should work."""
        # Generate a large JSON response (~100KB)
        large_data = [
            {
                "id": i,
                "body": f"This is comment number {i} with some extra text to make it longer " * 5,
                "user": {"login": f"user{i}"},
                "created_at": "2026-01-15T10:00:00Z",
                "updated_at": "2026-01-15T10:00:00Z",
            }
            for i in range(100)
        ]
        large_json = json.dumps(large_data)
        assert len(large_json) > 40000  # Verify it's actually large

        response = {
            "success": True,
            "message": "Command executed",
            "data": {
                "stdout": large_json,
                "stderr": "",
                "returncode": 0,
            },
        }
        result = run_call_gateway_python(response)
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert len(parsed) == 100

    def test_success_response_special_characters(self):
        """Output with special characters should be preserved exactly."""
        # Characters that could be mangled by echo: backslashes, -n, -e, etc.
        special_output = "line1\nline2\ttab\r\nwindows\n-n not a flag\n-e not a flag\n"
        response = {
            "success": True,
            "message": "Command executed",
            "data": {
                "stdout": special_output,
                "stderr": "",
                "returncode": 0,
            },
        }
        result = run_call_gateway_python(response)
        assert result.returncode == 0
        # The output should contain the literal characters, not interpreted escapes
        assert "-n not a flag" in result.stdout
        assert "-e not a flag" in result.stdout

    def test_success_response_null_data(self):
        """Success with null data field should not crash."""
        response = {
            "success": True,
            "message": "Command executed",
            "data": None,
        }
        result = run_call_gateway_python(response)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_error_response_shows_message(self):
        """Error response should output message to stderr."""
        response = {
            "success": False,
            "message": "Command 'pr merge' is not allowed",
            "data": None,
        }
        result = run_call_gateway_python(response)
        assert result.returncode == 1
        assert "Command 'pr merge' is not allowed" in result.stderr
        assert result.stdout == ""

    def test_error_response_shows_stderr(self):
        """Error response with stderr data should include it."""
        response = {
            "success": False,
            "message": "Command failed",
            "data": {
                "stdout": "",
                "stderr": "fatal: not a git repository",
                "returncode": 128,
            },
        }
        result = run_call_gateway_python(response)
        assert result.returncode == 1
        assert "Command failed" in result.stderr
        assert "fatal: not a git repository" in result.stderr

    def test_error_401_shows_auth_hint(self):
        """401 error should include authentication hint."""
        response = {
            "success": False,
            "message": "Unauthorized",
        }
        result = run_call_gateway_python(response, http_code="401")
        assert result.returncode == 1
        assert "Authentication failed" in result.stderr

    def test_error_429_shows_rate_limit_hint(self):
        """429 error should include rate limit hint."""
        response = {
            "success": False,
            "message": "Rate limited",
        }
        result = run_call_gateway_python(response, http_code="429")
        assert result.returncode == 1
        assert "Rate limit" in result.stderr

    def test_invalid_json_response(self):
        """Non-JSON response should produce a clear error."""
        result = run_call_gateway_raw("this is not json")
        assert result.returncode == 1
        assert "Failed to parse gateway response" in result.stderr

    def test_missing_response_file(self):
        """Missing response file should produce a clear error."""
        result = subprocess.run(
            ["python3", "-c", CALL_GATEWAY_PYTHON, "/nonexistent/file.json", "200"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1
        assert "Failed to parse gateway response" in result.stderr


class TestGhWrapperSyntax:
    """Test that the gh wrapper script is syntactically valid."""

    def test_bash_syntax_valid(self):
        """gh wrapper should pass bash -n syntax check."""
        result = subprocess.run(
            ["bash", "-n", GH_WRAPPER],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Syntax error:\n{result.stderr}"

    def test_has_shebang(self):
        """gh wrapper should have a proper bash shebang."""
        with open(GH_WRAPPER) as f:
            first_line = f.readline().strip()
        assert first_line == "#!/bin/bash"


class TestGhWrapperMergeBlocking:
    """Test that merge operations are always blocked."""

    def test_merge_blocked_without_gateway(self):
        """gh pr merge should be blocked even before checking gateway."""
        env = os.environ.copy()
        # Set gateway to unreachable address to prove merge check happens first
        env["GATEWAY_URL"] = "http://127.0.0.1:1"
        env.pop("EGG_SESSION_TOKEN", None)

        result = subprocess.run(
            ["bash", GH_WRAPPER, "pr", "merge", "123"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 1
        assert "MERGE OPERATIONS NOT SUPPORTED" in result.stderr


class TestExclamationUnescaping:
    """Test that \\! → ! unescaping works in both wrapper scripts.

    Claude Code's Bash tool escapes ! to \\! in command strings. Since the
    wrapper scripts run as non-interactive bash, the backslash persists as
    a literal character. Both wrappers unescape \\! early in arg processing.
    """

    # Bash snippet that replicates the gh wrapper's unescaping pattern
    # (ARGS array + set --) and prints the resulting args.
    GH_UNESCAPE = textwrap.dedent("""\
        ARGS=("$@")
        for i in "${!ARGS[@]}"; do
            ARGS[$i]="${ARGS[$i]//\\\\!/!}"
        done
        set -- "${ARGS[@]}"
        printf '%s\\n' "${ARGS[@]}"
    """)

    # Bash snippet that replicates the git wrapper's unescaping pattern
    # (rebuild $@ via set --) and prints the resulting args.
    GIT_UNESCAPE = textwrap.dedent("""\
        _unescaped_args=()
        for _arg in "$@"; do
            _unescaped_args+=("${_arg//\\\\!/!}")
        done
        set -- "${_unescaped_args[@]}"
        unset _unescaped_args _arg
        printf '%s\\n' "$@"
    """)

    def _run_unescape(self, snippet: str, args: list[str]) -> list[str]:
        """Run an unescaping snippet with the given args and return output lines."""
        result = subprocess.run(
            ["bash", "-c", snippet, "_"] + args,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        # Filter out empty trailing line from printf
        return [line for line in result.stdout.split("\n") if line]

    def test_gh_unescapes_backslash_bang(self):
        """gh wrapper should convert \\! to ! in arguments."""
        args = ['select(.conclusion \\!= "SUCCESS")', "normal_arg"]
        lines = self._run_unescape(self.GH_UNESCAPE, args)
        assert lines[0] == 'select(.conclusion != "SUCCESS")'
        assert lines[1] == "normal_arg"

    def test_git_unescapes_backslash_bang(self):
        """git wrapper should convert \\! to ! in arguments."""
        args = ['select(.conclusion \\!= "SUCCESS")', "normal_arg"]
        lines = self._run_unescape(self.GIT_UNESCAPE, args)
        assert lines[0] == 'select(.conclusion != "SUCCESS")'
        assert lines[1] == "normal_arg"

    def test_gh_no_change_without_backslash_bang(self):
        """Arguments without \\! should pass through unchanged."""
        args = ["--title", "hello world", "--body", "no special chars"]
        lines = self._run_unescape(self.GH_UNESCAPE, args)
        assert lines == args

    def test_git_no_change_without_backslash_bang(self):
        """Arguments without \\! should pass through unchanged."""
        args = ["push", "origin", "main"]
        lines = self._run_unescape(self.GIT_UNESCAPE, args)
        assert lines == args

    def test_gh_multiple_bangs_in_one_arg(self):
        """Multiple \\! in a single argument should all be unescaped."""
        args = ["\\!a\\!b\\!c"]
        lines = self._run_unescape(self.GH_UNESCAPE, args)
        assert lines[0] == "!a!b!c"

    def test_git_multiple_bangs_in_one_arg(self):
        """Multiple \\! in a single argument should all be unescaped."""
        args = ["\\!a\\!b\\!c"]
        lines = self._run_unescape(self.GIT_UNESCAPE, args)
        assert lines[0] == "!a!b!c"

    def test_gh_preserves_other_backslashes(self):
        """Backslashes not followed by ! should be preserved."""
        args = ["path\\to\\file", "tab\\there"]
        lines = self._run_unescape(self.GH_UNESCAPE, args)
        assert lines[0] == "path\\to\\file"
        assert lines[1] == "tab\\there"

    def test_git_preserves_other_backslashes(self):
        """Backslashes not followed by ! should be preserved."""
        args = ["path\\to\\file", "tab\\there"]
        lines = self._run_unescape(self.GIT_UNESCAPE, args)
        assert lines[0] == "path\\to\\file"
        assert lines[1] == "tab\\there"


class TestPrReviewMarker:
    """Test that handle_pr_review adds the automated review marker.

    The gh wrapper adds a hidden HTML comment marker to all reviews posted
    via `gh pr review`. This allows workflows to identify automated reviews
    by looking for the marker rather than relying on username heuristics.

    Marker format: <!-- egg-automated-review bot=<name> commit=<sha> verdict=<type> -->
    """

    # Extract the handle_pr_review logic for marker generation
    # This tests the marker construction without needing a real gateway
    MARKER_GENERATION = textwrap.dedent("""\
        # Simulate the marker generation from handle_pr_review
        commit_sha="${1:-abc123def456}"
        bot_name="${EGG_BOT_NAME:-egg}"
        body="${2:-}"
        review_type="${3:-comment}"
        verdict="${review_type:-comment}"

        marker="<!-- egg-automated-review bot=${bot_name} commit=${commit_sha} verdict=${verdict} -->"
        if [ -n "$body" ]; then
            echo "${body}

${marker}"
        else
            echo "${marker}"
        fi
    """)

    def _run_marker_generation(
        self, commit_sha: str = "abc123def456", body: str = "", bot_name: str = ""
    ) -> str:
        """Run the marker generation logic and return the result."""
        env = os.environ.copy()
        if bot_name:
            env["EGG_BOT_NAME"] = bot_name

        result = subprocess.run(
            ["bash", "-c", self.MARKER_GENERATION, "_", commit_sha, body],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        return result.stdout.rstrip("\n")

    def test_marker_format_no_body(self):
        """Marker alone should have correct format."""
        output = self._run_marker_generation(commit_sha="abc123", body="")
        assert output == "<!-- egg-automated-review bot=egg commit=abc123 verdict=comment -->"

    def test_marker_format_with_body(self):
        """Marker should be appended after body with blank line."""
        output = self._run_marker_generation(commit_sha="def456", body="LGTM!")
        assert (
            output == "LGTM!\n\n<!-- egg-automated-review bot=egg commit=def456 verdict=comment -->"
        )

    def test_marker_uses_custom_bot_name(self):
        """Marker should use EGG_BOT_NAME if set."""
        output = self._run_marker_generation(
            commit_sha="abc123", body="", bot_name="james-in-a-box"
        )
        assert (
            output
            == "<!-- egg-automated-review bot=james-in-a-box commit=abc123 verdict=comment -->"
        )

    def test_marker_with_multiline_body(self):
        """Marker should work with multiline review body."""
        body = "Great changes!\n\nSome minor suggestions:\n- Fix typo on line 10"
        output = self._run_marker_generation(commit_sha="789abc", body=body)
        expected = f"{body}\n\n<!-- egg-automated-review bot=egg commit=789abc verdict=comment -->"
        assert output == expected

    def test_marker_is_parseable_by_workflow(self):
        """Marker should be parseable by the workflow regex."""
        import re

        output = self._run_marker_generation(commit_sha="abc123def456789", body="LGTM!")
        # This is the regex used in reusable-review.yml (with optional verdict)
        marker_regex = (
            r"<!-- egg-automated-review bot=([^ ]+) commit=([a-f0-9]+)( verdict=([a-z-]+))? -->"
        )
        match = re.search(marker_regex, output)
        assert match is not None
        assert match.group(1) == "egg"
        assert match.group(2) == "abc123def456789"
        assert match.group(4) == "comment"

    def test_empty_commit_sha_not_parseable(self):
        """Marker with empty commit SHA should not match the workflow regex.

        This verifies that if git rev-parse HEAD fails and returns empty,
        the workflow won't incorrectly match a malformed marker.
        """
        import re

        # Generate marker with empty commit SHA directly (bypass default in helper)
        marker = "<!-- egg-automated-review bot=egg commit= verdict=comment -->"
        # The workflow regex requires at least one hex char: commit=([a-f0-9]+)
        marker_regex = (
            r"<!-- egg-automated-review bot=([^ ]+) commit=([a-f0-9]+)( verdict=([a-z-]+))? -->"
        )
        match = re.search(marker_regex, marker)
        assert match is None, "Empty commit SHA should not match workflow regex"


class TestPrReviewEmptyCommitWarning:
    """Test that handle_pr_review warns when commit SHA is empty."""

    # Bash snippet that simulates the warning logic from handle_pr_review
    WARNING_LOGIC = textwrap.dedent("""\
        commit_sha="${1:-}"

        if [ -z "$commit_sha" ]; then
            echo "WARNING: Could not determine commit SHA for review marker (git rev-parse HEAD failed)" >&2
        fi

        echo "done"
    """)

    def test_warning_on_empty_commit_sha(self):
        """Empty commit SHA should produce a warning on stderr."""
        result = subprocess.run(
            ["bash", "-c", self.WARNING_LOGIC, "_", ""],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "WARNING" in result.stderr
        assert "Could not determine commit SHA" in result.stderr
        assert "done" in result.stdout

    def test_no_warning_on_valid_commit_sha(self):
        """Valid commit SHA should not produce a warning."""
        result = subprocess.run(
            ["bash", "-c", self.WARNING_LOGIC, "_", "abc123def456"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "WARNING" not in result.stderr
        assert "done" in result.stdout


class TestPrReviewHandler:
    """Test the handle_pr_review function's JSON escaping logic.

    This tests the Python JSON escaping that prevents body content
    from being corrupted when it contains special characters like
    curly braces {} (issue #193).
    """

    # Extract just the Python escaping logic from handle_pr_review
    PR_REVIEW_PYTHON = textwrap.dedent("""\
        import json
        import sys

        pr_number = sys.argv[2]
        body = sys.argv[3]
        review_type = sys.argv[4]

        args = ['pr', 'review', pr_number, '--repo', sys.argv[1], '--body', body]

        if review_type == 'approve':
            args.append('--approve')
        elif review_type == 'request-changes':
            args.append('--request-changes')
        elif review_type == 'comment':
            args.append('--comment')

        print(json.dumps({'args': args}))
    """)

    def _run_pr_review_escaper(
        self, repo: str, pr_number: str, body: str, review_type: str
    ) -> dict:
        """Run the PR review JSON escaping and return the parsed result."""
        result = subprocess.run(
            ["python3", "-c", self.PR_REVIEW_PYTHON, repo, pr_number, body, review_type],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        return json.loads(result.stdout)

    def test_simple_body(self):
        """Simple body text should be escaped correctly."""
        result = self._run_pr_review_escaper("owner/repo", "123", "Looks good!", "approve")
        assert result["args"] == [
            "pr",
            "review",
            "123",
            "--repo",
            "owner/repo",
            "--body",
            "Looks good!",
            "--approve",
        ]

    def test_body_with_curly_braces(self):
        """Body with curly braces should be properly escaped (issue #193)."""
        body = "The workflow uses ${{ github.repository }} syntax"
        result = self._run_pr_review_escaper("owner/repo", "456", body, "comment")
        # Verify the body is preserved exactly
        assert result["args"][6] == body
        assert "${{" in result["args"][6]

    def test_body_with_json_content(self):
        """Body containing JSON should be properly escaped."""
        body = '{"key": "value", "nested": {"a": 1}}'
        result = self._run_pr_review_escaper("owner/repo", "789", body, "request-changes")
        assert result["args"][6] == body

    def test_body_with_special_characters(self):
        """Body with various special characters should be preserved."""
        body = "Fix: `code` with 'quotes' and \"double quotes\" and $vars"
        result = self._run_pr_review_escaper("owner/repo", "101", body, "comment")
        assert result["args"][6] == body

    def test_approve_review_type(self):
        """approve review type should use --approve flag."""
        result = self._run_pr_review_escaper("owner/repo", "123", "LGTM", "approve")
        assert "--approve" in result["args"]

    def test_request_changes_review_type(self):
        """request-changes review type should use --request-changes flag."""
        result = self._run_pr_review_escaper("owner/repo", "123", "Needs fixes", "request-changes")
        assert "--request-changes" in result["args"]

    def test_comment_review_type(self):
        """comment review type should use --comment flag."""
        result = self._run_pr_review_escaper("owner/repo", "123", "Some notes", "comment")
        assert "--comment" in result["args"]

    def test_multiline_body(self):
        """Multi-line body should be preserved."""
        body = "## Summary\n\n- Point 1\n- Point 2\n\nSigned: egg"
        result = self._run_pr_review_escaper("owner/repo", "123", body, "comment")
        assert result["args"][6] == body
        assert "\n" in result["args"][6]


class TestReviewMarkerFormat:
    """Test the automated review marker includes the verdict field.

    The gh wrapper generates a marker in the format:
      <!-- egg-automated-review bot=<name> commit=<sha> verdict=<type> -->
    This tests that the marker is correctly formed for each review type.
    """

    # Bash snippet that reproduces the marker generation logic from handle_pr_review
    MARKER_SCRIPT = textwrap.dedent("""\
        bot_name="${1:-egg}"
        commit_sha="${2:-abc123}"
        review_type="$3"
        verdict="${review_type:-comment}"
        echo "<!-- egg-automated-review bot=${bot_name} commit=${commit_sha} verdict=${verdict} -->"
    """)

    def _generate_marker(
        self, review_type: str, bot_name: str = "egg", commit_sha: str = "abc123"
    ) -> str:
        """Run the marker generation logic and return the marker string."""
        result = subprocess.run(
            ["bash", "-c", self.MARKER_SCRIPT, "_", bot_name, commit_sha, review_type],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        return result.stdout.strip()

    def test_approve_marker(self):
        """Approve review should produce verdict=approve in marker."""
        marker = self._generate_marker("approve")
        assert marker == "<!-- egg-automated-review bot=egg commit=abc123 verdict=approve -->"

    def test_request_changes_marker(self):
        """Request-changes review should produce verdict=request-changes in marker."""
        marker = self._generate_marker("request-changes")
        assert (
            marker == "<!-- egg-automated-review bot=egg commit=abc123 verdict=request-changes -->"
        )

    def test_comment_marker(self):
        """Comment review should produce verdict=comment in marker."""
        marker = self._generate_marker("comment")
        assert marker == "<!-- egg-automated-review bot=egg commit=abc123 verdict=comment -->"

    def test_empty_review_type_defaults_to_comment(self):
        """Empty review type should default to verdict=comment."""
        marker = self._generate_marker("")
        assert "verdict=comment" in marker

    def test_custom_bot_name(self):
        """Marker should use the provided bot name."""
        marker = self._generate_marker("approve", bot_name="sdlc-review")
        assert "bot=sdlc-review" in marker

    def test_marker_is_html_comment(self):
        """Marker should be a valid HTML comment (hidden in rendered markdown)."""
        marker = self._generate_marker("approve")
        assert marker.startswith("<!--")
        assert marker.endswith("-->")

    def test_verdict_regex_only_matches_lowercase(self):
        """Verify the workflow regex only matches lowercase verdicts.

        The gh wrapper always produces lowercase verdicts, but this test
        documents that uppercase would NOT match the regex used in
        sdlc-pipeline.yml and reusable-review.yml.
        """
        import re

        # The regex used in workflows to parse the verdict field
        marker_regex = r"verdict=([a-z-]+)"

        # Lowercase verdict should match
        lowercase_marker = "<!-- egg-automated-review bot=egg commit=abc123 verdict=approve -->"
        match = re.search(marker_regex, lowercase_marker)
        assert match is not None
        assert match.group(1) == "approve"

        # Uppercase verdict should NOT match (documents expected behavior)
        uppercase_marker = "<!-- egg-automated-review bot=egg commit=abc123 verdict=APPROVE -->"
        match = re.search(marker_regex, uppercase_marker)
        assert match is None, "Uppercase verdicts should not match the regex"

        # Mixed case should NOT match
        mixedcase_marker = "<!-- egg-automated-review bot=egg commit=abc123 verdict=Approve -->"
        match = re.search(marker_regex, mixedcase_marker)
        assert match is None, "Mixed case verdicts should not match the regex"


class TestIssueCommentHandler:
    """Test handle_issue_comment JSON escaping, --body, and --body-file support.

    This mirrors TestPrReviewHandler but targets the issue comment handler.
    Tests verify that ${{ }} expressions and other shell metacharacters are
    safely passed through the Python JSON escaping layer (issue #283).
    """

    # Extract the Python escaping logic from handle_issue_comment
    ISSUE_COMMENT_PYTHON = textwrap.dedent("""\
        import json
        import sys

        args = ['issue', 'comment', sys.argv[2], '--repo', sys.argv[1], '--body', sys.argv[3]]
        print(json.dumps({'args': args}))
    """)

    def _run_issue_comment_escaper(self, repo: str, issue_number: str, body: str) -> dict:
        """Run the issue comment JSON escaping and return the parsed result."""
        result = subprocess.run(
            ["python3", "-c", self.ISSUE_COMMENT_PYTHON, repo, issue_number, body],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        return json.loads(result.stdout)

    def test_simple_body(self):
        """Simple body text should be escaped correctly."""
        result = self._run_issue_comment_escaper("owner/repo", "42", "Hello world")
        assert result["args"] == [
            "issue",
            "comment",
            "42",
            "--repo",
            "owner/repo",
            "--body",
            "Hello world",
        ]

    def test_body_with_curly_braces(self):
        """Body with ${{ }} expressions should be properly escaped (issue #283)."""
        body = "The workflow uses ${{ github.repository }} and ${{ github.event.issue.number }}"
        result = self._run_issue_comment_escaper("owner/repo", "283", body)
        assert result["args"][6] == body
        assert "${{" in result["args"][6]

    def test_body_with_github_actions_expressions(self):
        """Body with multiple GitHub Actions expressions should be preserved."""
        body = textwrap.dedent("""\
            ## Problem Statement

            The workflow at `.github/workflows/ci.yml` uses:
            - `${{ github.repository }}` for the repo name
            - `${{ secrets.TOKEN }}` for authentication
            - `${{ steps.build.outputs.result }}` for build output
        """)
        result = self._run_issue_comment_escaper("owner/repo", "283", body)
        assert result["args"][6] == body
        assert "${{ github.repository }}" in result["args"][6]
        assert "${{ secrets.TOKEN }}" in result["args"][6]

    def test_body_with_json_content(self):
        """Body containing JSON should be properly escaped."""
        body = '{"key": "value", "nested": {"a": 1}}'
        result = self._run_issue_comment_escaper("owner/repo", "100", body)
        assert result["args"][6] == body

    def test_multiline_body(self):
        """Multi-line body should be preserved."""
        body = "## Analysis\n\n- Point 1\n- Point 2\n\nAuthored by egg"
        result = self._run_issue_comment_escaper("owner/repo", "42", body)
        assert result["args"][6] == body
        assert "\n" in result["args"][6]


class TestBodyFileArgParsing:
    """Test --body-file/-F argument parsing in issue comment and PR comment handlers.

    These tests verify the bash arg parsing logic for --body-file support,
    ensuring file content is correctly read and used as the body.
    --body and --body-file are mutually exclusive; specifying both is an error.
    """

    # Bash snippet that replicates handle_issue_comment's arg parsing.
    # Uses Python for output to handle multiline body content correctly.
    ISSUE_COMMENT_ARG_PARSER = textwrap.dedent("""\
        ARGS=("$@")
        issue_number="" body="" body_file=""

        i=0
        while [ $i -lt ${#ARGS[@]} ]; do
            case "${ARGS[$i]}" in
                --body|-b)
                    ((i++))
                    body="${ARGS[$i]}"
                    ;;
                --body-file|-F)
                    ((i++))
                    body_file="${ARGS[$i]}"
                    ;;
                [0-9]*)
                    if [ -z "$issue_number" ]; then
                        issue_number="${ARGS[$i]}"
                    fi
                    ;;
            esac
            ((i++))
        done

        # Resolve body: --body and --body-file are mutually exclusive
        if [ -n "$body" ] && [ -n "$body_file" ]; then
            echo "ERROR: Cannot use both --body and --body-file" >&2
            exit 1
        fi

        if [ -n "$body_file" ]; then
            if [ ! -f "$body_file" ]; then
                echo "ERROR: File not found: $body_file" >&2
                exit 1
            fi
            body=$(cat "$body_file") || { echo "ERROR: Failed to read $body_file" >&2; exit 1; }
        fi

        # Output as JSON for reliable multiline handling
        python3 -c "
import json, sys
print(json.dumps({'issue_number': sys.argv[1], 'body': sys.argv[2]}))
" "$issue_number" "$body"
    """)

    def _run_arg_parser(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run the arg parser and return the CompletedProcess."""
        return subprocess.run(
            ["bash", "-c", self.ISSUE_COMMENT_ARG_PARSER, "_"] + args,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _run_arg_parser_ok(self, args: list[str]) -> dict[str, str]:
        """Run the arg parser, assert success, and return parsed values."""
        result = self._run_arg_parser(args)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        return json.loads(result.stdout)

    def test_body_flag_works(self):
        """--body flag should set the body correctly."""
        result = self._run_arg_parser_ok(["issue", "comment", "42", "--body", "Hello"])
        assert result["issue_number"] == "42"
        assert result["body"] == "Hello"

    def test_body_short_flag_works(self):
        """-b short flag should set the body correctly."""
        result = self._run_arg_parser_ok(["issue", "comment", "42", "-b", "Hello"])
        assert result["issue_number"] == "42"
        assert result["body"] == "Hello"

    def test_body_file_long_flag(self):
        """--body-file should read body content from file."""
        content = "## Analysis\n\nThis uses ${{ github.repository }}."
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(["issue", "comment", "42", "--body-file", tmpfile])
            assert result["issue_number"] == "42"
            assert result["body"] == content
        finally:
            os.unlink(tmpfile)

    def test_body_file_short_flag(self):
        """-F short flag should read body content from file."""
        content = "Plan content with ${{ vars.NAME }}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(["issue", "comment", "42", "-F", tmpfile])
            assert result["issue_number"] == "42"
            assert result["body"] == content
        finally:
            os.unlink(tmpfile)

    def test_body_file_with_curly_braces(self):
        """--body-file content with ${{ }} expressions should be preserved."""
        content = textwrap.dedent("""\
            ## Problem Statement

            The workflow uses ${{ github.repository }} and
            references ${{ github.event.issue.number }}.

            ## Recommended Approach

            Update the action to use `--body-file` flag.""")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(["issue", "comment", "283", "--body-file", tmpfile])
            assert result["issue_number"] == "283"
            assert "${{ github.repository }}" in result["body"]
            assert "${{ github.event.issue.number }}" in result["body"]
        finally:
            os.unlink(tmpfile)

    def test_both_body_and_body_file_errors(self):
        """Specifying both --body and --body-file should error."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("file content")
            tmpfile = f.name

        try:
            result = self._run_arg_parser(
                [
                    "issue",
                    "comment",
                    "42",
                    "--body",
                    "inline content",
                    "--body-file",
                    tmpfile,
                ]
            )
            assert result.returncode != 0
            assert "Cannot use both --body and --body-file" in result.stderr
        finally:
            os.unlink(tmpfile)

    def test_body_file_not_found_errors(self):
        """--body-file pointing to nonexistent file should error."""
        result = self._run_arg_parser(
            [
                "issue",
                "comment",
                "42",
                "--body-file",
                "/nonexistent/file.md",
            ]
        )
        assert result.returncode != 0
        assert "File not found" in result.stderr


class TestPrEditBodyFile:
    """Test --body-file/-F support in handle_pr_edit.

    Mirrors TestBodyFileArgParsing but targets the PR edit handler,
    which also supports --title alongside --body/--body-file.
    """

    PR_EDIT_ARG_PARSER = textwrap.dedent("""\
        ARGS=("$@")
        pr_number="" title="" body="" body_file=""

        i=0
        while [ $i -lt ${#ARGS[@]} ]; do
            case "${ARGS[$i]}" in
                --title|-t)
                    ((i++))
                    title="${ARGS[$i]}"
                    ;;
                --body|-b)
                    ((i++))
                    body="${ARGS[$i]}"
                    ;;
                --body-file|-F)
                    ((i++))
                    body_file="${ARGS[$i]}"
                    ;;
                [0-9]*)
                    if [ -z "$pr_number" ]; then
                        pr_number="${ARGS[$i]}"
                    fi
                    ;;
            esac
            ((i++))
        done

        # Resolve body: --body and --body-file are mutually exclusive
        if [ -n "$body" ] && [ -n "$body_file" ]; then
            echo "ERROR: Cannot use both --body and --body-file" >&2
            exit 1
        fi

        if [ -n "$body_file" ]; then
            if [ ! -f "$body_file" ]; then
                echo "ERROR: File not found: $body_file" >&2
                exit 1
            fi
            body=$(cat "$body_file") || { echo "ERROR: Failed to read $body_file" >&2; exit 1; }
        fi

        python3 -c "
import json, sys
print(json.dumps({'pr_number': sys.argv[1], 'title': sys.argv[2], 'body': sys.argv[3]}))
" "$pr_number" "$title" "$body"
    """)

    def _run_arg_parser(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run the arg parser and return the CompletedProcess."""
        return subprocess.run(
            ["bash", "-c", self.PR_EDIT_ARG_PARSER, "_"] + args,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _run_arg_parser_ok(self, args: list[str]) -> dict[str, str]:
        """Run the arg parser, assert success, and return parsed values."""
        result = self._run_arg_parser(args)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        return json.loads(result.stdout)

    def test_body_file_long_flag(self):
        """--body-file should read body content from file for PR edit."""
        content = "Updated PR body with ${{ github.repository }}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(
                ["pr", "edit", "123", "--body-file", tmpfile, "--title", "My PR"]
            )
            assert result["pr_number"] == "123"
            assert result["body"] == content
            assert result["title"] == "My PR"
        finally:
            os.unlink(tmpfile)

    def test_body_file_short_flag(self):
        """-F short flag should read body content from file for PR edit."""
        content = "Short flag body"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(["pr", "edit", "456", "-F", tmpfile])
            assert result["pr_number"] == "456"
            assert result["body"] == content
        finally:
            os.unlink(tmpfile)

    def test_body_inline_still_works(self):
        """--body flag should continue to work for PR edit."""
        result = self._run_arg_parser_ok(
            ["pr", "edit", "789", "--body", "inline body", "--title", "Title"]
        )
        assert result["pr_number"] == "789"
        assert result["body"] == "inline body"
        assert result["title"] == "Title"

    def test_both_body_and_body_file_errors(self):
        """Specifying both --body and --body-file should error for PR edit."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("file content")
            tmpfile = f.name

        try:
            result = self._run_arg_parser(
                ["pr", "edit", "123", "--body", "inline", "--body-file", tmpfile]
            )
            assert result.returncode != 0
            assert "Cannot use both --body and --body-file" in result.stderr
        finally:
            os.unlink(tmpfile)

    def test_body_file_not_found_errors(self):
        """--body-file pointing to nonexistent file should error for PR edit."""
        result = self._run_arg_parser(["pr", "edit", "123", "--body-file", "/nonexistent/file.md"])
        assert result.returncode != 0
        assert "File not found" in result.stderr

    def test_body_file_with_curly_braces(self):
        """--body-file content with ${{ }} should be preserved for PR edit."""
        content = "Update refs to ${{ secrets.TOKEN }} and ${{ github.sha }}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(["pr", "edit", "100", "--body-file", tmpfile])
            assert "${{ secrets.TOKEN }}" in result["body"]
            assert "${{ github.sha }}" in result["body"]
        finally:
            os.unlink(tmpfile)


class TestPrCreateBodyFile:
    """Test --body-file/-F support in handle_pr_create.

    Mirrors TestBodyFileArgParsing but targets the PR create handler.
    """

    PR_CREATE_ARG_PARSER = textwrap.dedent("""\
        ARGS=("$@")
        title="" body="" body_file="" base="main" head=""

        i=0
        while [ $i -lt ${#ARGS[@]} ]; do
            case "${ARGS[$i]}" in
                --title|-t)
                    ((i++))
                    title="${ARGS[$i]}"
                    ;;
                --body|-b)
                    ((i++))
                    body="${ARGS[$i]}"
                    ;;
                --body-file|-F)
                    ((i++))
                    body_file="${ARGS[$i]}"
                    ;;
                --base|-B)
                    ((i++))
                    base="${ARGS[$i]}"
                    ;;
                --head|-H)
                    ((i++))
                    head="${ARGS[$i]}"
                    ;;
            esac
            ((i++))
        done

        # Resolve body: --body and --body-file are mutually exclusive
        if [ -n "$body" ] && [ -n "$body_file" ]; then
            echo "ERROR: Cannot use both --body and --body-file" >&2
            exit 1
        fi

        if [ -n "$body_file" ]; then
            if [ ! -f "$body_file" ]; then
                echo "ERROR: File not found: $body_file" >&2
                exit 1
            fi
            body=$(cat "$body_file") || { echo "ERROR: Failed to read $body_file" >&2; exit 1; }
        fi

        python3 -c "
import json, sys
print(json.dumps({'title': sys.argv[1], 'body': sys.argv[2], 'base': sys.argv[3], 'head': sys.argv[4]}))
" "$title" "$body" "$base" "$head"
    """)

    def _run_arg_parser(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run the arg parser and return the CompletedProcess."""
        return subprocess.run(
            ["bash", "-c", self.PR_CREATE_ARG_PARSER, "_"] + args,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _run_arg_parser_ok(self, args: list[str]) -> dict[str, str]:
        """Run the arg parser, assert success, and return parsed values."""
        result = self._run_arg_parser(args)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        return json.loads(result.stdout)

    def test_body_file_long_flag(self):
        """--body-file should read body content from file for PR create."""
        content = "PR body with ${{ github.repository }}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(
                ["pr", "create", "--title", "My PR", "--body-file", tmpfile]
            )
            assert result["title"] == "My PR"
            assert result["body"] == content
        finally:
            os.unlink(tmpfile)

    def test_body_file_short_flag(self):
        """-F short flag should read body content from file for PR create."""
        content = "Short flag body"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(["pr", "create", "--title", "PR", "-F", tmpfile])
            assert result["body"] == content
        finally:
            os.unlink(tmpfile)

    def test_body_inline_still_works(self):
        """--body flag should continue to work for PR create."""
        result = self._run_arg_parser_ok(
            ["pr", "create", "--title", "My PR", "--body", "inline body"]
        )
        assert result["title"] == "My PR"
        assert result["body"] == "inline body"

    def test_both_body_and_body_file_errors(self):
        """Specifying both --body and --body-file should error for PR create."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("file content")
            tmpfile = f.name

        try:
            result = self._run_arg_parser(
                ["pr", "create", "--title", "PR", "--body", "inline", "--body-file", tmpfile]
            )
            assert result.returncode != 0
            assert "Cannot use both --body and --body-file" in result.stderr
        finally:
            os.unlink(tmpfile)

    def test_body_file_not_found_errors(self):
        """--body-file pointing to nonexistent file should error for PR create."""
        result = self._run_arg_parser(
            ["pr", "create", "--title", "PR", "--body-file", "/nonexistent/file.md"]
        )
        assert result.returncode != 0
        assert "File not found" in result.stderr


class TestIssueCreateBodyFile:
    """Test --body-file/-F support in handle_issue_create."""

    ISSUE_CREATE_ARG_PARSER = textwrap.dedent("""\
        ARGS=("$@")
        title="" body="" body_file=""

        i=0
        while [ $i -lt ${#ARGS[@]} ]; do
            case "${ARGS[$i]}" in
                --title|-t)
                    ((i++))
                    title="${ARGS[$i]}"
                    ;;
                --body|-b)
                    ((i++))
                    body="${ARGS[$i]}"
                    ;;
                --body-file|-F)
                    ((i++))
                    body_file="${ARGS[$i]}"
                    ;;
            esac
            ((i++))
        done

        # Resolve body: --body and --body-file are mutually exclusive
        if [ -n "$body" ] && [ -n "$body_file" ]; then
            echo "ERROR: Cannot use both --body and --body-file" >&2
            exit 1
        fi

        if [ -n "$body_file" ]; then
            if [ ! -f "$body_file" ]; then
                echo "ERROR: File not found: $body_file" >&2
                exit 1
            fi
            body=$(cat "$body_file") || { echo "ERROR: Failed to read $body_file" >&2; exit 1; }
        fi

        python3 -c "
import json, sys
print(json.dumps({'title': sys.argv[1], 'body': sys.argv[2]}))
" "$title" "$body"
    """)

    def _run_arg_parser(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run the arg parser and return the CompletedProcess."""
        return subprocess.run(
            ["bash", "-c", self.ISSUE_CREATE_ARG_PARSER, "_"] + args,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _run_arg_parser_ok(self, args: list[str]) -> dict[str, str]:
        """Run the arg parser, assert success, and return parsed values."""
        result = self._run_arg_parser(args)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        return json.loads(result.stdout)

    def test_body_file_long_flag(self):
        """--body-file should read body content from file for issue create."""
        content = "Issue body with ${{ github.repository }}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(
                ["issue", "create", "--title", "Bug report", "--body-file", tmpfile]
            )
            assert result["title"] == "Bug report"
            assert result["body"] == content
        finally:
            os.unlink(tmpfile)

    def test_body_file_short_flag(self):
        """-F short flag should read body content from file for issue create."""
        content = "Short flag body"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(["issue", "create", "--title", "Issue", "-F", tmpfile])
            assert result["body"] == content
        finally:
            os.unlink(tmpfile)

    def test_both_body_and_body_file_errors(self):
        """Specifying both --body and --body-file should error for issue create."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("file content")
            tmpfile = f.name

        try:
            result = self._run_arg_parser(
                ["issue", "create", "--title", "Issue", "--body", "inline", "--body-file", tmpfile]
            )
            assert result.returncode != 0
            assert "Cannot use both --body and --body-file" in result.stderr
        finally:
            os.unlink(tmpfile)

    def test_body_file_not_found_errors(self):
        """--body-file pointing to nonexistent file should error for issue create."""
        result = self._run_arg_parser(
            ["issue", "create", "--title", "Issue", "--body-file", "/nonexistent/file.md"]
        )
        assert result.returncode != 0
        assert "File not found" in result.stderr


class TestIssueEditBodyFile:
    """Test --body-file/-F support in handle_issue_edit."""

    ISSUE_EDIT_ARG_PARSER = textwrap.dedent("""\
        ARGS=("$@")
        issue_number="" title="" body="" body_file=""

        i=0
        while [ $i -lt ${#ARGS[@]} ]; do
            case "${ARGS[$i]}" in
                --title|-t)
                    ((i++))
                    title="${ARGS[$i]}"
                    ;;
                --body|-b)
                    ((i++))
                    body="${ARGS[$i]}"
                    ;;
                --body-file|-F)
                    ((i++))
                    body_file="${ARGS[$i]}"
                    ;;
                [0-9]*)
                    if [ -z "$issue_number" ]; then
                        issue_number="${ARGS[$i]}"
                    fi
                    ;;
            esac
            ((i++))
        done

        # Resolve body: --body and --body-file are mutually exclusive
        if [ -n "$body" ] && [ -n "$body_file" ]; then
            echo "ERROR: Cannot use both --body and --body-file" >&2
            exit 1
        fi

        if [ -n "$body_file" ]; then
            if [ ! -f "$body_file" ]; then
                echo "ERROR: File not found: $body_file" >&2
                exit 1
            fi
            body=$(cat "$body_file") || { echo "ERROR: Failed to read $body_file" >&2; exit 1; }
        fi

        python3 -c "
import json, sys
print(json.dumps({'issue_number': sys.argv[1], 'title': sys.argv[2], 'body': sys.argv[3]}))
" "$issue_number" "$title" "$body"
    """)

    def _run_arg_parser(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run the arg parser and return the CompletedProcess."""
        return subprocess.run(
            ["bash", "-c", self.ISSUE_EDIT_ARG_PARSER, "_"] + args,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _run_arg_parser_ok(self, args: list[str]) -> dict[str, str]:
        """Run the arg parser, assert success, and return parsed values."""
        result = self._run_arg_parser(args)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        return json.loads(result.stdout)

    def test_body_file_long_flag(self):
        """--body-file should read body content from file for issue edit."""
        content = "Updated issue body with ${{ github.repository }}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(
                ["issue", "edit", "42", "--body-file", tmpfile, "--title", "Updated title"]
            )
            assert result["issue_number"] == "42"
            assert result["body"] == content
            assert result["title"] == "Updated title"
        finally:
            os.unlink(tmpfile)

    def test_body_file_short_flag(self):
        """-F short flag should read body content from file for issue edit."""
        content = "Short flag body"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(["issue", "edit", "99", "-F", tmpfile])
            assert result["issue_number"] == "99"
            assert result["body"] == content
        finally:
            os.unlink(tmpfile)

    def test_both_body_and_body_file_errors(self):
        """Specifying both --body and --body-file should error for issue edit."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("file content")
            tmpfile = f.name

        try:
            result = self._run_arg_parser(
                ["issue", "edit", "42", "--body", "inline", "--body-file", tmpfile]
            )
            assert result.returncode != 0
            assert "Cannot use both --body and --body-file" in result.stderr
        finally:
            os.unlink(tmpfile)

    def test_body_file_not_found_errors(self):
        """--body-file pointing to nonexistent file should error for issue edit."""
        result = self._run_arg_parser(
            ["issue", "edit", "42", "--body-file", "/nonexistent/file.md"]
        )
        assert result.returncode != 0
        assert "File not found" in result.stderr

    def test_body_file_with_curly_braces(self):
        """--body-file content with ${{ }} should be preserved for issue edit."""
        content = "Update refs to ${{ secrets.TOKEN }} and ${{ github.sha }}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmpfile = f.name

        try:
            result = self._run_arg_parser_ok(["issue", "edit", "100", "--body-file", tmpfile])
            assert "${{ secrets.TOKEN }}" in result["body"]
            assert "${{ github.sha }}" in result["body"]
        finally:
            os.unlink(tmpfile)
