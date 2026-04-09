"""Tests for _read_phase_draft, _get_draft_path, and _cleanup_stale_generic_drafts."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from routes.pipelines import (
    _cleanup_stale_generic_drafts,
    _get_generic_draft_path,
    _read_phase_draft,
)


class TestReadPhaseDraft:
    """Tests for _read_phase_draft."""

    def test_returns_full_content_within_limit(self, tmp_path: Path):
        """Content shorter than max_chars is returned in full."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text("short content", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "refine", issue_number=42)
        assert result == "short content"

    def test_truncates_content_exceeding_limit(self, tmp_path: Path):
        """Content longer than max_chars is truncated with a suffix."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        content = "x" * 200
        (drafts / "42-analysis.md").write_text(content, encoding="utf-8")

        result = _read_phase_draft(tmp_path, "refine", issue_number=42, max_chars=100)
        assert result.startswith("x" * 100)
        assert "... (truncated, 200 chars total)" in result

    def test_no_draft_for_implement_phase(self, tmp_path: Path):
        """Implement phase has no draft file; returns None."""
        result = _read_phase_draft(tmp_path, "implement", issue_number=42)
        assert result is None

    def test_missing_draft_file(self, tmp_path: Path):
        """Returns None when draft file does not exist on disk."""
        result = _read_phase_draft(tmp_path, "refine", issue_number=42)
        assert result is None

    def test_pipeline_id_used_when_no_issue(self, tmp_path: Path):
        """Pipeline ID is used as prefix when no issue_number is provided."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "pid123-plan.md").write_text("prompt plan", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "plan", pipeline_id="pid123")
        assert result == "prompt plan"

    def test_fallback_prefix_without_identifiers(self, tmp_path: Path):
        """Without issue_number or pipeline_id, falls back to 'unknown' prefix."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "unknown-analysis.md").write_text("fallback", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "refine")
        assert result == "fallback"

    def test_truncation_suffix_format(self, tmp_path: Path):
        """Truncation suffix includes newlines and exact char count."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        content = "a" * 500
        (drafts / "7-plan.md").write_text(content, encoding="utf-8")

        result = _read_phase_draft(tmp_path, "plan", issue_number=7, max_chars=10)
        assert result == "a" * 10 + "\n\n... (truncated, 500 chars total)"

    def test_logs_debug_when_file_missing(self, tmp_path: Path, monkeypatch):
        """Debug log is emitted when the draft file does not exist."""
        import routes.pipelines as mod

        mock_logger = MagicMock()
        monkeypatch.setattr(mod, "logger", mock_logger)

        result = _read_phase_draft(tmp_path, "refine", issue_number=99)

        assert result is None
        mock_logger.debug.assert_called_once()
        call_args = mock_logger.debug.call_args
        assert "Draft file not found" in call_args[0][0]


class TestCleanupStaleGenericDrafts:
    """Tests for _cleanup_stale_generic_drafts."""

    def test_removes_generic_analysis_and_plan(self, tmp_path: Path):
        """Unprefixed analysis.md and plan.md are removed (untracked fallback)."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "analysis.md").write_text("stale analysis", encoding="utf-8")
        (drafts / "plan.md").write_text("stale plan", encoding="utf-8")

        result = _cleanup_stale_generic_drafts(tmp_path)

        assert not (drafts / "analysis.md").exists()
        assert not (drafts / "plan.md").exists()
        # Untracked files use os.unlink fallback — no commit is made
        assert result is False

    def test_preserves_prefixed_files(self, tmp_path: Path):
        """Prefixed files like 1553-analysis.md are untouched."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "analysis.md").write_text("stale", encoding="utf-8")
        (drafts / "1553-analysis.md").write_text("correct", encoding="utf-8")
        (drafts / "1553-plan.md").write_text("correct plan", encoding="utf-8")

        _cleanup_stale_generic_drafts(tmp_path)

        assert not (drafts / "analysis.md").exists()
        assert (drafts / "1553-analysis.md").exists()
        assert (drafts / "1553-plan.md").exists()

    def test_noop_when_no_drafts_dir(self, tmp_path: Path):
        """No error when .egg-state/drafts/ does not exist."""
        result = _cleanup_stale_generic_drafts(tmp_path)
        assert result is False

    def test_noop_when_no_stale_files(self, tmp_path: Path):
        """No error when drafts dir exists but has no generic files."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text("ok", encoding="utf-8")

        result = _cleanup_stale_generic_drafts(tmp_path)

        assert result is False
        assert (drafts / "42-analysis.md").exists()

    def test_git_rm_success_returns_true(self, tmp_path: Path, monkeypatch):
        """When git rm succeeds, function commits and returns True."""
        import routes.pipelines as mod

        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "analysis.md").write_text("stale", encoding="utf-8")

        call_log: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            call_log.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        result = _cleanup_stale_generic_drafts(tmp_path)

        assert result is True
        # Should have called git rm then git commit
        assert any("rm" in c for c in call_log[0])
        assert any("commit" in c for c in call_log[1])
        # commit should include --no-verify
        assert "--no-verify" in call_log[1]

    def test_git_rm_failure_logs_warning(self, tmp_path: Path, monkeypatch):
        """When git rm fails, a warning is logged and unlink is used."""
        import routes.pipelines as mod

        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "plan.md").write_text("stale", encoding="utf-8")

        mock_logger = MagicMock()
        monkeypatch.setattr(mod, "logger", mock_logger)

        # Let subprocess.run raise (git rm fails) — default behavior in
        # non-git tmp_path already does this, but be explicit:
        result = _cleanup_stale_generic_drafts(tmp_path)

        assert result is False
        assert not (drafts / "plan.md").exists()
        # Should have logged a warning about git rm failure
        mock_logger.warning.assert_called_once()
        assert "git rm failed" in mock_logger.warning.call_args[0][0]


class TestGetGenericDraftPath:
    """Tests for _get_generic_draft_path."""

    def test_refine_returns_analysis_md(self):
        """Refine phase maps to the unprefixed analysis.md path."""
        result = _get_generic_draft_path("refine")
        assert result == ".egg-state/drafts/analysis.md"

    def test_implement_returns_none(self):
        """Implement phase has no draft; returns None."""
        result = _get_generic_draft_path("implement")
        assert result is None

    def test_plan_returns_plan_md(self):
        """Plan phase maps to the unprefixed plan.md path."""
        result = _get_generic_draft_path("plan")
        assert result == ".egg-state/drafts/plan.md"

    def test_arbitrary_phase_returns_phase_md(self):
        """Other phases use {phase}.md pattern."""
        result = _get_generic_draft_path("design")
        assert result == ".egg-state/drafts/design.md"


class TestReadPhaseDraftFallback:
    """Tests for _read_phase_draft generic fallback behavior (issue #1575)."""

    def test_fallback_to_generic_when_issue_specific_missing(self, tmp_path: Path):
        """When issue-specific draft is missing, falls back to generic path."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        # Only the generic path exists (no 42-analysis.md)
        (drafts / "analysis.md").write_text("generic analysis content", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "refine", issue_number=42)
        assert result == "generic analysis content"

    def test_primary_path_preferred_over_generic(self, tmp_path: Path):
        """When both issue-specific and generic exist, primary wins."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text("primary content", encoding="utf-8")
        (drafts / "analysis.md").write_text("generic content", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "refine", issue_number=42)
        assert result == "primary content"

    def test_returns_none_when_both_paths_missing(self, tmp_path: Path):
        """Returns None when neither issue-specific nor generic path exists."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)

        result = _read_phase_draft(tmp_path, "refine", issue_number=42)
        assert result is None

    def test_fallback_for_plan_phase(self, tmp_path: Path):
        """Fallback works for the plan phase (plan.md)."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "plan.md").write_text("generic plan", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "plan", issue_number=99)
        assert result == "generic plan"

    def test_fallback_with_pipeline_id_prefix(self, tmp_path: Path):
        """Fallback works when pipeline_id is used as prefix (no issue_number)."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        # pipeline-specific path doesn't exist, only generic
        (drafts / "analysis.md").write_text("fallback analysis", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "refine", pipeline_id="pid-abc")
        assert result == "fallback analysis"

    def test_fallback_respects_truncation(self, tmp_path: Path):
        """Generic fallback content is still truncated when over max_chars."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        content = "z" * 300
        (drafts / "analysis.md").write_text(content, encoding="utf-8")

        result = _read_phase_draft(tmp_path, "refine", issue_number=42, max_chars=50)
        assert result.startswith("z" * 50)
        assert "... (truncated, 300 chars total)" in result

    def test_implement_phase_still_returns_none_with_generic(self, tmp_path: Path):
        """Implement phase returns None even if generic files exist."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "implement.md").write_text("should not be read", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "implement", issue_number=42)
        assert result is None

    def test_fallback_logs_debug_when_used(self, tmp_path: Path, monkeypatch):
        """Debug log is emitted when the generic fallback path is used."""
        import routes.pipelines as mod

        mock_logger = MagicMock()
        monkeypatch.setattr(mod, "logger", mock_logger)

        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "analysis.md").write_text("fallback content", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "refine", issue_number=42)
        assert result == "fallback content"

        # Should have two debug calls: one for primary miss, one for fallback hit
        debug_calls = mock_logger.debug.call_args_list
        assert len(debug_calls) == 2

        # First debug: primary path not found
        assert "Draft file not found" in debug_calls[0][0][0]

        # Second debug: using fallback
        assert "Using generic fallback draft path" in debug_calls[1][0][0]

    def test_no_fallback_log_when_primary_exists(self, tmp_path: Path, monkeypatch):
        """No fallback debug log when the primary path is found directly."""
        import routes.pipelines as mod

        mock_logger = MagicMock()
        monkeypatch.setattr(mod, "logger", mock_logger)

        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text("primary", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "refine", issue_number=42)
        assert result == "primary"
        # No debug logs should be emitted when primary path works
        mock_logger.debug.assert_not_called()

    def test_fallback_returns_none_when_no_drafts_dir(self, tmp_path: Path):
        """Returns None when the drafts directory doesn't exist at all."""
        result = _read_phase_draft(tmp_path, "refine", issue_number=42)
        assert result is None

    def test_fallback_for_unknown_prefix(self, tmp_path: Path):
        """Fallback works even with the 'unknown' prefix (no identifiers)."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "plan.md").write_text("generic plan no prefix", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "plan")
        assert result == "generic plan no prefix"
