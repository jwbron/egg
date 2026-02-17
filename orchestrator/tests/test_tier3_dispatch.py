"""Tests for Tier 3 phase-level dispatch.

Covers:
- 3-tier complexity detection and signal parsing
- Phase-scoped prompt building
- Sequential phase cycling flow
- Complexity tier model fields
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import ComplexityTier, Pipeline, PipelineConfig

# Import pipeline functions using direct path manipulation
sys.path.insert(0, str(Path(__file__).parent.parent / "routes"))


class TestComplexityTierModel:
    """Tests for ComplexityTier enum and Pipeline model."""

    def test_complexity_tier_values(self):
        """ComplexityTier has low, mid, high values."""
        assert ComplexityTier.LOW == "low"
        assert ComplexityTier.MID == "mid"
        assert ComplexityTier.HIGH == "high"

    def test_pipeline_default_complexity_tier(self):
        """Pipeline defaults to mid complexity tier."""
        pipeline = Pipeline(
            id="test-1",
            issue_number=1,
            repo="owner/repo",
        )
        assert pipeline.complexity_tier == ComplexityTier.MID

    def test_pipeline_complexity_tier_set(self):
        """Pipeline complexity_tier can be set to high."""
        pipeline = Pipeline(
            id="test-1",
            issue_number=1,
            repo="owner/repo",
            complexity_tier=ComplexityTier.HIGH,
        )
        assert pipeline.complexity_tier == ComplexityTier.HIGH

    def test_pipeline_config_enable_parallel_phases(self):
        """PipelineConfig has enable_parallel_phases flag."""
        config = PipelineConfig(enable_parallel_phases=True)
        assert config.enable_parallel_phases is True

    def test_pipeline_config_default_parallel_phases(self):
        """PipelineConfig defaults enable_parallel_phases to False."""
        config = PipelineConfig()
        assert config.enable_parallel_phases is False


class TestHighComplexitySignalDetection:
    """Tests for _check_high_complexity_signal()."""

    @pytest.fixture(autouse=True)
    def setup_paths(self):
        """Import the signal detection function."""
        try:
            from pipelines import _check_high_complexity_signal, _get_draft_path
            self._check_signal = _check_high_complexity_signal
            self._get_draft_path = _get_draft_path
        except ImportError:
            pytest.skip("Cannot import pipelines module")

    def test_detects_high_complexity(self, tmp_path: Path):
        """Detects complexity_tier: high from YAML metadata."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n```yaml\n# metadata\ncomplexity_tier: high\nparallel_phases: true\n```\n",
            encoding="utf-8",
        )

        tier, parallel = self._check_signal(tmp_path, "issue", 42, "test-1")
        assert tier == "high"
        assert parallel is True

    def test_detects_mid_complexity(self, tmp_path: Path):
        """Detects complexity_tier: mid from YAML metadata."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n```yaml\n# metadata\ncomplexity_tier: mid\n```\n",
            encoding="utf-8",
        )

        tier, parallel = self._check_signal(tmp_path, "issue", 42, "test-1")
        assert tier == "mid"
        assert parallel is False

    def test_detects_low_complexity(self, tmp_path: Path):
        """Detects complexity_tier: low from YAML metadata."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n```yaml\n# metadata\nshort_circuit: true\ncomplexity_tier: low\n```\n",
            encoding="utf-8",
        )

        tier, parallel = self._check_signal(tmp_path, "issue", 42, "test-1")
        assert tier == "low"
        assert parallel is False

    def test_missing_signal_defaults_to_mid(self, tmp_path: Path):
        """Missing YAML block defaults to mid/False."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\nJust text, no YAML.\n",
            encoding="utf-8",
        )

        tier, parallel = self._check_signal(tmp_path, "issue", 42, "test-1")
        assert tier == "mid"
        assert parallel is False

    def test_missing_draft_defaults_to_mid(self, tmp_path: Path):
        """Missing draft file defaults to mid/False."""
        tier, parallel = self._check_signal(tmp_path, "issue", 42, "test-1")
        assert tier == "mid"
        assert parallel is False

    def test_malformed_yaml_defaults_to_mid(self, tmp_path: Path):
        """Malformed YAML block falls back to regex parsing."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n```yaml\n  invalid: [yaml: {broken\n```\n",
            encoding="utf-8",
        )

        tier, parallel = self._check_signal(tmp_path, "issue", 42, "test-1")
        assert tier == "mid"
        assert parallel is False

    def test_invalid_tier_value_defaults_to_mid(self, tmp_path: Path):
        """Invalid complexity_tier value defaults to mid."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n```yaml\n# metadata\ncomplexity_tier: extreme\n```\n",
            encoding="utf-8",
        )

        tier, parallel = self._check_signal(tmp_path, "issue", 42, "test-1")
        assert tier == "mid"
        assert parallel is False

    def test_parallel_phases_without_high_tier(self, tmp_path: Path):
        """parallel_phases is captured even with mid tier."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n```yaml\n# metadata\ncomplexity_tier: mid\nparallel_phases: true\n```\n",
            encoding="utf-8",
        )

        tier, parallel = self._check_signal(tmp_path, "issue", 42, "test-1")
        assert tier == "mid"
        assert parallel is True


class TestPhaseScopedPrompt:
    """Tests for _build_phase_scoped_prompt()."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Import the prompt builder function."""
        try:
            from pipelines import _build_phase_scoped_prompt
            self._build = _build_phase_scoped_prompt
        except ImportError:
            pytest.skip("Cannot import pipelines module")

    def _make_phase(self, phase_id: str, name: str, tasks: list | None = None):
        """Create a mock Phase object."""
        phase = MagicMock()
        phase.id = phase_id
        phase.name = name
        phase.tasks = tasks or []
        return phase

    def _make_task(self, task_id: str, description: str, files: list | None = None):
        """Create a mock Task object."""
        task = MagicMock()
        task.id = task_id
        task.description = description
        task.status = "pending"
        task.acceptance_criteria = "Test passes"
        task.files_affected = files or []
        return task

    def test_prompt_contains_phase_id(self, tmp_path: Path):
        """Phase-scoped prompt contains the phase ID."""
        phase = self._make_phase("phase-1", "Schema changes")
        pipeline = Pipeline(
            id="test-1", issue_number=42, repo="owner/repo", branch="egg/test"
        )

        result = self._build(
            phase_obj=phase,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
        )

        assert "phase-1" in result
        assert "Schema changes" in result

    def test_prompt_contains_task_list(self, tmp_path: Path):
        """Phase-scoped prompt contains task checklist."""
        tasks = [
            self._make_task("TASK-1-1", "Add field X", ["models.py"]),
            self._make_task("TASK-1-2", "Update schema", ["schema.json"]),
        ]
        phase = self._make_phase("phase-1", "Schema changes", tasks)
        pipeline = Pipeline(
            id="test-1", issue_number=42, repo="owner/repo", branch="egg/test"
        )

        result = self._build(
            phase_obj=phase,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
        )

        assert "TASK-1-1" in result
        assert "Add field X" in result
        assert "TASK-1-2" in result
        assert "Update schema" in result

    def test_prompt_scoped_instruction(self, tmp_path: Path):
        """Phase-scoped prompt instructs agent to only implement this phase."""
        phase = self._make_phase("phase-2", "Testing")
        pipeline = Pipeline(
            id="test-1", issue_number=42, repo="owner/repo", branch="egg/test"
        )

        result = self._build(
            phase_obj=phase,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
        )

        assert "only" in result.lower()
        assert "phase-2" in result

    def test_prompt_includes_review_feedback(self, tmp_path: Path):
        """Phase-scoped prompt includes review feedback on retries."""
        phase = self._make_phase("phase-1", "Schema changes")
        pipeline = Pipeline(
            id="test-1", issue_number=42, repo="owner/repo", branch="egg/test"
        )

        result = self._build(
            phase_obj=phase,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
            review_feedback="Fix the type annotation",
            review_cycle=1,
        )

        assert "Fix the type annotation" in result
        assert "Prior Review Feedback" in result
