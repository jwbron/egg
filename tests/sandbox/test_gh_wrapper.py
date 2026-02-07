"""
Tests for the sandbox gh CLI wrapper script.

Tests the call_gateway response parsing and output behavior.
The gh wrapper routes all commands through the gateway sidecar,
which returns JSON responses that must be parsed correctly.
"""

import json
import os
import subprocess
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
    import tempfile

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
    import tempfile

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

    Marker format: <!-- egg-automated-review bot=<name> commit=<sha> -->
    """

    # Extract the handle_pr_review logic for marker generation
    # This tests the marker construction without needing a real gateway
    MARKER_GENERATION = textwrap.dedent("""\
        # Simulate the marker generation from handle_pr_review
        commit_sha="${1:-abc123def456}"
        bot_name="${EGG_BOT_NAME:-egg}"
        body="${2:-}"

        marker="<!-- egg-automated-review bot=${bot_name} commit=${commit_sha} -->"
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
        assert output == "<!-- egg-automated-review bot=egg commit=abc123 -->"

    def test_marker_format_with_body(self):
        """Marker should be appended after body with blank line."""
        output = self._run_marker_generation(commit_sha="def456", body="LGTM!")
        assert output == "LGTM!\n\n<!-- egg-automated-review bot=egg commit=def456 -->"

    def test_marker_uses_custom_bot_name(self):
        """Marker should use EGG_BOT_NAME if set."""
        output = self._run_marker_generation(
            commit_sha="abc123", body="", bot_name="james-in-a-box"
        )
        assert output == "<!-- egg-automated-review bot=james-in-a-box commit=abc123 -->"

    def test_marker_with_multiline_body(self):
        """Marker should work with multiline review body."""
        body = "Great changes!\n\nSome minor suggestions:\n- Fix typo on line 10"
        output = self._run_marker_generation(commit_sha="789abc", body=body)
        expected = f"{body}\n\n<!-- egg-automated-review bot=egg commit=789abc -->"
        assert output == expected

    def test_marker_is_parseable_by_workflow(self):
        """Marker should be parseable by the workflow regex."""
        import re

        output = self._run_marker_generation(commit_sha="abc123def456789", body="LGTM!")
        # This is the regex used in on-pull-request.yml
        marker_regex = r"<!-- egg-automated-review bot=([^ ]+) commit=([a-f0-9]+) -->"
        match = re.search(marker_regex, output)
        assert match is not None
        assert match.group(1) == "egg"
        assert match.group(2) == "abc123def456789"

    def test_empty_commit_sha_not_parseable(self):
        """Marker with empty commit SHA should not match the workflow regex.

        This verifies that if git rev-parse HEAD fails and returns empty,
        the workflow won't incorrectly match a malformed marker.
        """
        import re

        # Generate marker with empty commit SHA directly (bypass default in helper)
        marker = "<!-- egg-automated-review bot=egg commit= -->"
        # The workflow regex requires at least one hex char: commit=([a-f0-9]+)
        marker_regex = r"<!-- egg-automated-review bot=([^ ]+) commit=([a-f0-9]+) -->"
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
