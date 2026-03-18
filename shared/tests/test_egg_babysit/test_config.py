"""Tests for BabysitConfig dataclass and validation."""

import dataclasses

import pytest
from egg_babysit.config import BabysitConfig


class TestBabysitConfig:
    """Tests for BabysitConfig dataclass."""

    def test_default_config(self):
        """Default config should have sensible values."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")

        assert config.pr_number == 42
        assert config.repo == "owner/repo"
        assert config.timeout_seconds == 14400  # 4 hours
        assert config.max_iterations == 10
        assert config.poll_interval_seconds == 30
        assert config.max_retries_per_job == 3
        assert config.max_feedback_rounds == 5
        assert config.check_fixers_path == ""
        assert config.orchestrator_url == ""
        assert config.pipeline_id == ""

    def test_custom_config(self):
        """Config should accept custom values."""
        config = BabysitConfig(
            pr_number=100,
            repo="myorg/myrepo",
            timeout_seconds=7200,
            max_iterations=5,
            poll_interval_seconds=60,
            max_retries_per_job=2,
            max_feedback_rounds=3,
        )

        assert config.pr_number == 100
        assert config.repo == "myorg/myrepo"
        assert config.timeout_seconds == 7200
        assert config.max_iterations == 5
        assert config.poll_interval_seconds == 60
        assert config.max_retries_per_job == 2
        assert config.max_feedback_rounds == 3

    def test_frozen_immutable(self):
        """Config should be frozen (immutable)."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            config.pr_number = 99  # type: ignore[misc]

    def test_is_dataclass(self):
        """BabysitConfig should be a dataclass."""
        assert dataclasses.is_dataclass(BabysitConfig)

    def test_custom_pipeline_id(self):
        """Should accept a custom pipeline ID."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", pipeline_id="pr-42")
        assert config.pipeline_id == "pr-42"

    def test_custom_check_fixers_path(self):
        """Should accept a custom check-fixers path."""
        config = BabysitConfig(
            pr_number=42, repo="owner/repo", check_fixers_path="/path/to/config.yml"
        )
        assert config.check_fixers_path == "/path/to/config.yml"


class TestBabysitConfigValidation:
    """Tests for BabysitConfig bounds validation."""

    def test_zero_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=0)

    def test_negative_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=-1)

    def test_zero_max_iterations_raises(self):
        with pytest.raises(ValueError, match="max_iterations must be positive"):
            BabysitConfig(pr_number=42, repo="owner/repo", max_iterations=0)

    def test_zero_poll_interval_raises(self):
        with pytest.raises(ValueError, match="poll_interval_seconds must be positive"):
            BabysitConfig(pr_number=42, repo="owner/repo", poll_interval_seconds=0)

    def test_negative_max_retries_raises(self):
        with pytest.raises(ValueError, match="max_retries_per_job must be non-negative"):
            BabysitConfig(pr_number=42, repo="owner/repo", max_retries_per_job=-1)

    def test_negative_max_feedback_rounds_raises(self):
        with pytest.raises(ValueError, match="max_feedback_rounds must be non-negative"):
            BabysitConfig(pr_number=42, repo="owner/repo", max_feedback_rounds=-1)

    def test_zero_retries_allowed(self):
        """Zero retries is valid (disables retries)."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", max_retries_per_job=0)
        assert config.max_retries_per_job == 0

    def test_zero_feedback_rounds_allowed(self):
        """Zero feedback rounds is valid (disables feedback)."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", max_feedback_rounds=0)
        assert config.max_feedback_rounds == 0
