"""Tests for _cleanup_drafts_for_pr (#1599).

Covers:
- Pipeline-scoped draft removal (matching {id}-*.md files)
- No-op when drafts dir missing or no matching files
- Scoping: other pipelines' drafts are untouched
- git rm success path (commit made, returns True)
- git rm failure fallback (unlink, returns False)
- Integer and string pipeline identifiers
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from routes.pipelines import _cleanup_drafts_for_pr


class TestCleanupDraftsForPr:
    """Tests for _cleanup_drafts_for_pr."""

    def test_noop_when_drafts_dir_missing(self, tmp_path: Path):
        """Returns False when .egg-state/drafts/ does not exist."""
        result = _cleanup_drafts_for_pr(tmp_path, 42)
        assert result is False

    def test_noop_when_no_matching_files(self, tmp_path: Path):
        """Returns False when drafts dir exists but has no matching files."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "99-analysis.md").write_text("other pipeline", encoding="utf-8")

        result = _cleanup_drafts_for_pr(tmp_path, 42)

        assert result is False
        assert (drafts / "99-analysis.md").exists(), "Other pipeline's drafts must be untouched"

    def test_removes_matching_drafts_untracked_fallback(self, tmp_path: Path):
        """Matching drafts are removed via unlink fallback when git rm fails."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text("analysis", encoding="utf-8")
        (drafts / "42-plan.md").write_text("plan", encoding="utf-8")

        # In a non-git tmp_path, git rm will fail → unlink fallback
        result = _cleanup_drafts_for_pr(tmp_path, 42)

        assert not (drafts / "42-analysis.md").exists()
        assert not (drafts / "42-plan.md").exists()
        # Returns False because git rm failed (unlink fallback doesn't set removed=True
        # since CalledProcessError is caught before removed=True)
        assert result is False

    def test_preserves_other_pipelines_drafts(self, tmp_path: Path):
        """Only this pipeline's drafts are removed; others are preserved."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text("this pipeline", encoding="utf-8")
        (drafts / "42-plan.md").write_text("this pipeline", encoding="utf-8")
        (drafts / "99-analysis.md").write_text("other pipeline", encoding="utf-8")
        (drafts / "99-plan.md").write_text("other pipeline", encoding="utf-8")
        (drafts / "analysis.md").write_text("generic", encoding="utf-8")

        _cleanup_drafts_for_pr(tmp_path, 42)

        assert not (drafts / "42-analysis.md").exists()
        assert not (drafts / "42-plan.md").exists()
        assert (drafts / "99-analysis.md").exists()
        assert (drafts / "99-plan.md").exists()
        assert (drafts / "analysis.md").exists()

    def test_string_pipeline_identifier(self, tmp_path: Path):
        """Works with string pipeline identifiers (prompt-driven pipelines)."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "my-pipeline-analysis.md").write_text("analysis", encoding="utf-8")
        (drafts / "my-pipeline-plan.md").write_text("plan", encoding="utf-8")

        _cleanup_drafts_for_pr(tmp_path, "my-pipeline")

        assert not (drafts / "my-pipeline-analysis.md").exists()
        assert not (drafts / "my-pipeline-plan.md").exists()

    def test_no_substring_false_positive(self, tmp_path: Path):
        """Pipeline 4 does not match pipeline 42's drafts."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text("pipeline 42", encoding="utf-8")
        (drafts / "4-analysis.md").write_text("pipeline 4", encoding="utf-8")

        _cleanup_drafts_for_pr(tmp_path, 4)

        assert not (drafts / "4-analysis.md").exists()
        assert (drafts / "42-analysis.md").exists(), "Pipeline 42's draft must not be touched"

    def test_git_rm_success_commits_and_returns_true(self, tmp_path: Path, monkeypatch):
        """When git rm succeeds, removal is committed and function returns True."""
        import routes.pipelines as mod

        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text("analysis", encoding="utf-8")
        (drafts / "42-plan.md").write_text("plan", encoding="utf-8")

        call_log: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            call_log.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        result = _cleanup_drafts_for_pr(tmp_path, 42)

        assert result is True
        # Should have git rm calls followed by a commit
        rm_calls = [c for c in call_log if "rm" in c]
        commit_calls = [c for c in call_log if "commit" in c]
        assert len(rm_calls) == 2, "Should git rm both draft files"
        assert len(commit_calls) == 1, "Should commit once"
        assert "--no-verify" in commit_calls[0]
        assert "--ignore-unmatch" in rm_calls[0]

    def test_git_rm_failure_falls_back_to_unlink(self, tmp_path: Path, monkeypatch):
        """When git rm fails, files are removed via unlink and warning is logged."""
        import routes.pipelines as mod

        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text("analysis", encoding="utf-8")

        mock_logger = MagicMock()
        monkeypatch.setattr(mod, "logger", mock_logger)

        # Default behavior in non-git tmp_path: git rm fails → unlink fallback
        _cleanup_drafts_for_pr(tmp_path, 42)

        assert not (drafts / "42-analysis.md").exists(), "File should be removed via unlink"
        mock_logger.warning.assert_called()
        assert "git rm failed" in mock_logger.warning.call_args[0][0]

    def test_commit_failure_returns_false(self, tmp_path: Path, monkeypatch):
        """When git rm succeeds but commit fails, returns False."""
        import routes.pipelines as mod

        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text("analysis", encoding="utf-8")

        call_count = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if "commit" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        result = _cleanup_drafts_for_pr(tmp_path, 42)

        assert result is False

    def test_commit_message_includes_identifier(self, tmp_path: Path, monkeypatch):
        """Commit message references the pipeline identifier."""
        import routes.pipelines as mod

        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-plan.md").write_text("plan", encoding="utf-8")

        commit_cmd: list[str] = []

        def fake_run(cmd, **kwargs):
            if "commit" in cmd:
                commit_cmd.extend(cmd)
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        _cleanup_drafts_for_pr(tmp_path, 42)

        assert any("42" in arg for arg in commit_cmd), "Commit message should include identifier"
