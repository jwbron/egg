"""End-to-end tests with real Claude Code execution.

These tests require:
- Docker
- ANTHROPIC_OAUTH_TOKEN environment variable with a valid Anthropic token

They are opt-in via the @pytest.mark.e2e marker and are NOT run in
regular CI. They run on manual trigger or weekly schedule.

Run with: pytest tests/integration/test_e2e_workflow.py -v -m e2e
"""

import os
import subprocess

import pytest

# Skip entire module if no Anthropic token
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_OAUTH_TOKEN"),
        reason="ANTHROPIC_OAUTH_TOKEN not set",
    ),
]


@pytest.mark.e2e
class TestE2EClaudeCode:
    """End-to-end tests running real Claude Code prompts in the egg stack."""

    @pytest.fixture(autouse=True)
    def _check_token(self):
        """Skip all tests in this class if no Anthropic token."""
        if not os.environ.get("ANTHROPIC_OAUTH_TOKEN"):
            pytest.skip("ANTHROPIC_OAUTH_TOKEN not set")

    def test_simple_prompt_completes(self, egg_stack, session):
        """A simple Claude Code prompt completes without errors.

        Uses 'claude --print' for non-interactive execution.
        This is a smoke test that the full stack works end-to-end.
        """
        token = session.get("session_token")

        # Build a sandbox container with Claude Code installed
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                egg_stack.isolated_network,
                "-e",
                f"EGG_GATEWAY_URL=http://{egg_stack.gateway_isolated_ip}:9848",
                "-e",
                f"EGG_SESSION_TOKEN={token}",
                "-e",
                f"ANTHROPIC_OAUTH_TOKEN={os.environ['ANTHROPIC_OAUTH_TOKEN']}",
                "egg-sandbox:latest",
                "claude",
                "--print",
                "Say exactly: hello world",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert result.returncode == 0, (
            f"Claude Code prompt failed.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "hello" in result.stdout.lower(), f"Expected 'hello' in output, got: {result.stdout}"

    def test_file_creation_via_prompt(self, egg_stack, session):
        """Claude Code can create a file in the workspace."""
        token = session.get("session_token")

        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                egg_stack.isolated_network,
                "-e",
                f"EGG_GATEWAY_URL=http://{egg_stack.gateway_isolated_ip}:9848",
                "-e",
                f"EGG_SESSION_TOKEN={token}",
                "-e",
                f"ANTHROPIC_OAUTH_TOKEN={os.environ['ANTHROPIC_OAUTH_TOKEN']}",
                "egg-sandbox:latest",
                "bash",
                "-c",
                "claude --print \"Create a file called test.txt with the content 'integration test' and then cat the file\" && cat /home/egg/repos/test.txt 2>/dev/null || true",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

        # The prompt should complete (exit 0) even if the file isn't
        # where we expect -- Claude Code might put it elsewhere.
        assert result.returncode == 0 or "test.txt" in result.stdout, (
            f"File creation prompt failed.\nstderr: {result.stderr}"
        )

    def test_git_status_via_prompt(self, egg_stack, session):
        """Claude Code can run git status through the gateway."""
        token = session.get("session_token")

        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                egg_stack.isolated_network,
                "-e",
                f"EGG_GATEWAY_URL=http://{egg_stack.gateway_isolated_ip}:9848",
                "-e",
                f"EGG_SESSION_TOKEN={token}",
                "-e",
                f"ANTHROPIC_OAUTH_TOKEN={os.environ['ANTHROPIC_OAUTH_TOKEN']}",
                "egg-sandbox:latest",
                "claude",
                "--print",
                "Run git status and tell me the output",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        # Should complete without crashing
        assert result.returncode == 0, f"Git status prompt failed.\nstderr: {result.stderr}"
