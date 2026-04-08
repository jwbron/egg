"""Tests for _read_phase_draft, _get_draft_path, and _cleanup_stale_generic_drafts."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from routes.pipelines import _cleanup_stale_generic_drafts, _read_phase_draft


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
        from unittest.mock import MagicMock

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
        """Unprefixed analysis.md and plan.md are removed."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "analysis.md").write_text("stale analysis", encoding="utf-8")
        (drafts / "plan.md").write_text("stale plan", encoding="utf-8")

        _cleanup_stale_generic_drafts(tmp_path)

        assert not (drafts / "analysis.md").exists()
        assert not (drafts / "plan.md").exists()

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
        _cleanup_stale_generic_drafts(tmp_path)  # Should not raise

    def test_noop_when_no_stale_files(self, tmp_path: Path):
        """No error when drafts dir exists but has no generic files."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text("ok", encoding="utf-8")

        _cleanup_stale_generic_drafts(tmp_path)

        assert (drafts / "42-analysis.md").exists()
