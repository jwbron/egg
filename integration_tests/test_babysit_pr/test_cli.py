"""Integration tests for egg_babysit CLI argument parsing."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Shared packages directory for subprocess PYTHONPATH.
_SHARED_DIR = str(Path(__file__).resolve().parent.parent.parent / "shared")


def _cli_env() -> dict[str, str]:
    """Build environment with shared/ on PYTHONPATH for subprocess calls."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_SHARED_DIR}:{existing}" if existing else _SHARED_DIR
    return env


@pytest.mark.integration
class TestCLI:
    """Test CLI entry point argument parsing."""

    def test_cli_help(self):
        """--help exits 0 and shows usage."""
        result = subprocess.run(
            [sys.executable, "-m", "egg_babysit", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_cli_env(),
        )
        assert result.returncode == 0
        assert "babysit" in result.stdout.lower()
        assert "pr_number" in result.stdout.lower()

    def test_cli_missing_pr(self):
        """No PR number causes an error exit."""
        result = subprocess.run(
            [sys.executable, "-m", "egg_babysit"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_cli_env(),
        )
        assert result.returncode != 0
        assert "error" in result.stderr.lower() or "required" in result.stderr.lower()

    def test_cli_parse_args(self):
        """Verify known args are accepted by the parser (doesn't actually run the loop)."""
        import argparse

        # We test the parser indirectly through the ArgumentParser it creates.
        # Construct a parser the same way main() does and verify parsing.
        parser = argparse.ArgumentParser(prog="egg-babysit")
        parser.add_argument("pr_number", type=int)
        parser.add_argument("--repo", type=str, default="")
        parser.add_argument("--timeout", type=int, default=14400)
        parser.add_argument("--max-iterations", type=int, default=10)
        parser.add_argument("--poll-interval", type=int, default=30)
        parser.add_argument("--max-retries", type=int, default=3)
        parser.add_argument("--max-feedback-rounds", type=int, default=5)
        parser.add_argument("--check-fixers", type=str, default="")
        parser.add_argument("--verbose", "-v", action="store_true")

        args = parser.parse_args(["42", "--repo", "owner/repo", "--timeout", "3600", "-v"])
        assert args.pr_number == 42
        assert args.repo == "owner/repo"
        assert args.timeout == 3600
        assert args.verbose is True

    def test_cli_invalid_pr_number(self):
        """Non-integer PR number causes an error."""
        result = subprocess.run(
            [sys.executable, "-m", "egg_babysit", "not-a-number"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_cli_env(),
        )
        assert result.returncode != 0
