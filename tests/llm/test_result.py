"""Tests for llm.result module."""

from llm.result import AgentResult, ClaudeResult


class TestAgentResult:
    """Tests for AgentResult dataclass."""

    def test_success_result(self):
        """Create a successful result."""
        r = AgentResult(success=True, stdout="Hello", stderr="", returncode=0)
        assert r.success is True
        assert r.stdout == "Hello"
        assert r.stderr == ""
        assert r.returncode == 0
        assert r.error is None
        assert r.metadata is None

    def test_failed_result(self):
        """Create a failed result with error."""
        r = AgentResult(
            success=False,
            stdout="",
            stderr="something broke",
            returncode=1,
            error="Authentication failed",
        )
        assert r.success is False
        assert r.returncode == 1
        assert r.error == "Authentication failed"

    def test_with_metadata(self):
        """Result with metadata dict."""
        r = AgentResult(
            success=True,
            stdout="done",
            stderr="",
            returncode=0,
            metadata={"model": "claude-sonnet-4-20250514", "tokens": 150},
        )
        assert r.metadata["model"] == "claude-sonnet-4-20250514"
        assert r.metadata["tokens"] == 150

    def test_backward_compat_alias(self):
        """ClaudeResult is an alias for AgentResult."""
        assert ClaudeResult is AgentResult
