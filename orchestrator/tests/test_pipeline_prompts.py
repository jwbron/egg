"""
Tests for pipeline prompt builder functions (_build_checker_prompt, _build_autofix_prompt).
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from routes.pipelines import (
    _build_autofix_prompt,
    _build_checker_prompt,
    _get_agent_design_criteria,
    _get_code_review_criteria,
    _get_contract_review_criteria,
    _read_shared_criteria,
)


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

    def test_autofix_includes_shared_rules(self):
        """Autofix prompt loads rules from shared/prompts/autofixer-rules.md."""
        results = {"checks": [{"name": "lint", "passed": False, "output": "3 errors"}]}
        result = _build_autofix_prompt("pid-1", "local", results)
        # The shared file content should appear in the prompt
        assert "Auto-fixable" in result or "auto-fixable" in result.lower()


class TestReadSharedCriteria:
    """Tests for _read_shared_criteria helper function."""

    def test_reads_from_source_tree(self):
        """Reads criteria from shared/prompts/ in the source tree."""
        content = _read_shared_criteria("code-review-criteria.md")
        assert content is not None
        assert "Security" in content

    def test_returns_none_for_missing_file(self):
        """Returns None when no file is found."""
        content = _read_shared_criteria("nonexistent-file.md")
        assert content is None

    def test_user_override_takes_priority(self):
        """User override file takes priority over shared file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            egg_dir = Path(tmpdir) / ".egg"
            egg_dir.mkdir()
            (egg_dir / "review-rules.md").write_text("## Custom Override\nCustom content.")

            content = _read_shared_criteria(
                "code-review-criteria.md",
                user_override="review-rules.md",
                repo_path=tmpdir,
            )
            assert content is not None
            assert "Custom Override" in content
            assert "Custom content" in content

    def test_shared_file_when_no_override(self):
        """Falls through to shared file when no user override exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _read_shared_criteria(
                "code-review-criteria.md",
                user_override="review-rules.md",
                repo_path=tmpdir,
            )
            assert content is not None
            # Should contain shared file content, not None
            assert "Security" in content


class TestGetCodeReviewCriteria:
    """Tests for _get_code_review_criteria with shared file loading."""

    def test_returns_criteria_content(self):
        """Returns review criteria content."""
        result = _get_code_review_criteria()
        assert "Security" in result

    def test_loads_from_shared_file(self):
        """Criteria loads from shared/prompts/code-review-criteria.md."""
        result = _get_code_review_criteria()
        # Shared file has enriched content compared to inline fallback
        assert "How to Review" in result

    def test_user_override(self):
        """Supports .egg/review-rules.md user override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            egg_dir = Path(tmpdir) / ".egg"
            egg_dir.mkdir()
            (egg_dir / "review-rules.md").write_text("## My Custom Rules")

            result = _get_code_review_criteria(repo_path=tmpdir)
            assert "My Custom Rules" in result

    def test_inline_fallback(self):
        """Falls back to inline criteria when shared file is missing."""
        # Patch _read_shared_criteria to return None (simulating missing files)
        with patch("routes.pipelines._read_shared_criteria", return_value=None):
            result = _get_code_review_criteria()
            assert "Security" in result
            assert "Correctness" in result


class TestGetAgentDesignCriteria:
    """Tests for _get_agent_design_criteria with shared file loading."""

    def test_returns_criteria_content(self):
        """Returns agent-design criteria content."""
        result = _get_agent_design_criteria()
        assert "pre-fetching" in result.lower()

    def test_loads_from_shared_file(self):
        """Criteria loads from shared/prompts/agent-design-criteria.md."""
        result = _get_agent_design_criteria()
        # Shared file has enriched content including review philosophy
        assert "guidelines, not absolute rules" in result

    def test_inline_fallback(self):
        """Falls back to inline criteria when shared file is missing."""
        with patch("routes.pipelines._read_shared_criteria", return_value=None):
            result = _get_agent_design_criteria()
            assert "Excessive pre-fetching" in result
            assert "Rigid procedures" in result


class TestGetContractReviewCriteria:
    """Tests for _get_contract_review_criteria with shared file loading."""

    def test_returns_criteria_content(self):
        """Returns contract review criteria content."""
        result = _get_contract_review_criteria()
        assert "Task Verification" in result

    def test_user_override(self):
        """Supports .egg/contract-rules.md user override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            egg_dir = Path(tmpdir) / ".egg"
            egg_dir.mkdir()
            (egg_dir / "contract-rules.md").write_text("## Custom Contract Rules")

            result = _get_contract_review_criteria(repo_path=tmpdir)
            assert "Custom Contract Rules" in result

    def test_inline_fallback(self):
        """Falls back to inline criteria when shared file is missing."""
        with patch("routes.pipelines._read_shared_criteria", return_value=None):
            result = _get_contract_review_criteria()
            assert "Task Verification" in result
            assert "Contract Integrity" in result
