"""
Tests for pipeline prompt builder functions (_build_checker_prompt, _build_autofix_prompt).
"""

import sys
from unittest.mock import MagicMock

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from routes.pipelines import _build_autofix_prompt, _build_checker_prompt


class TestBuildCheckerPrompt:
    """Tests for _build_checker_prompt with repo_checks parameter."""

    def test_discovery_mode_without_repo_checks(self):
        """Without repo_checks, prompt uses discovery instructions."""
        result = _build_checker_prompt("pid-1", "local")
        assert "Discover and run all project test and lint commands" in result
        assert "Makefile" in result

    def test_discovery_mode_with_repo(self):
        """Discovery mode includes repo working directory."""
        result = _build_checker_prompt("pid-1", "local", repo="acme/web-app")
        assert "Repository: acme/web-app" in result
        assert "~/repos/web-app" in result
        assert "Discover and run all" in result

    def test_explicit_checks_mode(self):
        """With repo_checks, prompt lists explicit commands instead of discovery."""
        checks = [
            {"name": "lint", "command": "make lint"},
            {"name": "test", "command": "make test"},
        ]
        result = _build_checker_prompt("pid-1", "local", repo="acme/web-app", repo_checks=checks)
        assert "make lint" in result
        assert "make test" in result
        assert "**lint**" in result
        assert "**test**" in result
        # Should NOT contain discovery instructions
        assert "Discover and run all" not in result
        assert "Makefile" not in result

    def test_explicit_checks_without_repo(self):
        """Explicit checks work even without a repo specified."""
        checks = [{"name": "build", "command": "npm run build"}]
        result = _build_checker_prompt("pid-1", "issue", repo_checks=checks)
        assert "npm run build" in result
        assert "Repository:" not in result

    def test_always_includes_results_format(self):
        """Both modes include the results JSON format."""
        checks = [{"name": "test", "command": "pytest"}]
        for prompt in [
            _build_checker_prompt("pid-1", "local"),
            _build_checker_prompt("pid-1", "local", repo_checks=checks),
        ]:
            assert "implement-results.json" in prompt
            assert "all_passed" in prompt

    def test_includes_pipeline_metadata(self):
        """Prompt always includes pipeline ID and mode."""
        result = _build_checker_prompt("pid-42", "issue")
        assert "pid-42" in result
        assert "issue" in result


class TestBuildAutofixPrompt:
    """Tests for _build_autofix_prompt with repo parameter."""

    def test_without_repo(self):
        """Basic autofix prompt without repo context."""
        results = {"checks": [{"name": "lint", "passed": False, "output": "3 errors"}]}
        result = _build_autofix_prompt("pid-1", "local", results)
        assert "**lint**" in result
        assert "3 errors" in result
        assert "Repository:" not in result

    def test_with_repo(self):
        """Autofix prompt includes repo working directory."""
        results = {"checks": [{"name": "test", "passed": False, "output": "1 failure"}]}
        result = _build_autofix_prompt("pid-1", "local", results, repo="acme/web-app")
        assert "Repository: acme/web-app" in result
        assert "~/repos/web-app" in result

    def test_no_failures(self):
        """Prompt handles case with no failing checks."""
        results = {"checks": [{"name": "lint", "passed": True, "output": "ok"}]}
        result = _build_autofix_prompt("pid-1", "local", results)
        assert "No specific failures recorded" in result

    def test_includes_pipeline_metadata(self):
        """Prompt includes pipeline ID and mode."""
        results = {"checks": []}
        result = _build_autofix_prompt("pid-99", "issue", results)
        assert "pid-99" in result
        assert "issue" in result
