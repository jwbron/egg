"""End-to-end tests with real Claude Code execution.

These tests require:
- Docker
- ANTHROPIC_OAUTH_TOKEN environment variable with a valid Anthropic token

They are opt-in via the @pytest.mark.e2e marker and are NOT run in
regular CI. They run on manual trigger or weekly schedule.

Each test uses structured JSON output to get a machine-readable verdict
from the agent, rather than matching prose substrings.

Run with: pytest integration_tests/test_e2e_workflow.py -v -m e2e
"""

import os

import pytest

from .conftest import assert_agent_verdict, run_claude_structured

# Skip entire module if no Anthropic token
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_OAUTH_TOKEN"),
        reason="ANTHROPIC_OAUTH_TOKEN not set",
    ),
]

MAX_ATTEMPTS = 2


def _run_with_retry(egg_stack, session_token, prompt, **kwargs):
    """Run a structured Claude prompt with retry for resilience."""
    last_verdict = None
    for _attempt in range(MAX_ATTEMPTS):
        verdict = run_claude_structured(egg_stack, session_token, prompt, **kwargs)
        if verdict.passed:
            return verdict
        last_verdict = verdict
    return last_verdict


class TestE2EClaudeCode:
    """End-to-end tests running real Claude Code prompts in the egg stack."""

    @pytest.fixture(autouse=True)
    def _check_token(self):
        """Skip all tests in this class if no Anthropic token."""
        if not os.environ.get("ANTHROPIC_OAUTH_TOKEN"):
            pytest.skip("ANTHROPIC_OAUTH_TOKEN not set")

    def test_simple_prompt_completes(self, egg_stack, gateway_session):
        """A simple Claude Code prompt completes and returns a structured verdict.

        Smoke test that the full stack works end-to-end with structured output.
        """
        token = gateway_session.get("session_token")

        verdict = _run_with_retry(
            egg_stack,
            token,
            "Say exactly 'hello world'. Then evaluate: did you successfully "
            "produce output containing 'hello world'? Report your verdict.",
        )

        assert_agent_verdict(verdict, msg="simple prompt smoke test")

    def test_file_creation_via_prompt(self, egg_stack, gateway_session):
        """Claude Code can create a file and verify it exists."""
        token = gateway_session.get("session_token")

        verdict = _run_with_retry(
            egg_stack,
            token,
            "Create a file at /tmp/e2e_test.txt with the content "
            "'integration test'. Then verify the file exists and contains "
            "the expected text. Report your verdict on whether file creation "
            "succeeded.",
        )

        assert_agent_verdict(verdict, msg="file creation")

    def test_git_status_via_prompt(self, egg_stack, gateway_session):
        """Claude Code can run git status through the gateway."""
        token = gateway_session.get("session_token")

        verdict = _run_with_retry(
            egg_stack,
            token,
            "Run 'git status' and report whether the command executed "
            "successfully (exit code 0 and produced output). Report your "
            "verdict.",
        )

        assert_agent_verdict(verdict, msg="git status via gateway")
