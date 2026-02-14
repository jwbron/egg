"""Tests for _read_phase_draft and _get_draft_path helper functions."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from routes.pipelines import _read_phase_draft


class TestReadPhaseDraft:
    """Tests for _read_phase_draft."""

    def test_returns_full_content_within_limit(self, tmp_path: Path):
        """Content shorter than max_chars is returned in full."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text("short content", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "refine", "issue", issue_number=42)
        assert result == "short content"

    def test_truncates_content_exceeding_limit(self, tmp_path: Path):
        """Content longer than max_chars is truncated with a suffix."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        content = "x" * 200
        (drafts / "42-analysis.md").write_text(content, encoding="utf-8")

        result = _read_phase_draft(tmp_path, "refine", "issue", issue_number=42, max_chars=100)
        assert result.startswith("x" * 100)
        assert "... (truncated, 200 chars total)" in result

    def test_no_draft_for_implement_phase(self, tmp_path: Path):
        """Implement phase has no draft file; returns informative message."""
        result = _read_phase_draft(tmp_path, "implement", "issue", issue_number=42)
        assert result == "(No draft file for implement phase)"

    def test_missing_draft_file(self, tmp_path: Path):
        """Returns informative message when draft file does not exist on disk."""
        result = _read_phase_draft(tmp_path, "refine", "issue", issue_number=42)
        assert "(Draft file not found:" in result
        assert "42-analysis.md" in result

    def test_local_mode_uses_pipeline_id(self, tmp_path: Path):
        """Local mode constructs path with pipeline_id prefix."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "pid123-plan.md").write_text("local plan", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "plan", "local", pipeline_id="pid123")
        assert result == "local plan"

    def test_local_mode_fallback_prefix(self, tmp_path: Path):
        """Local mode without pipeline_id falls back to 'local' prefix."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "local-analysis.md").write_text("fallback", encoding="utf-8")

        result = _read_phase_draft(tmp_path, "refine", "local")
        assert result == "fallback"

    def test_truncation_suffix_format(self, tmp_path: Path):
        """Truncation suffix includes newlines and exact char count."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        content = "a" * 500
        (drafts / "7-plan.md").write_text(content, encoding="utf-8")

        result = _read_phase_draft(tmp_path, "plan", "issue", issue_number=7, max_chars=10)
        assert result == "a" * 10 + "\n\n... (truncated, 500 chars total)"
