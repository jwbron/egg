"""Tests for agent_timeout_seconds config field (#3665)."""

from __future__ import annotations

import pytest
from models import PipelineConfig
from pydantic import ValidationError


class TestAgentTimeoutConfig:
    """Tests for the agent_timeout_seconds field on PipelineConfig."""

    def test_default_is_7200(self) -> None:
        """Default timeout should be 7200 seconds (2 hours)."""
        config = PipelineConfig()
        assert config.agent_timeout_seconds == 7200

    def test_can_be_overridden(self) -> None:
        """Timeout should be configurable per-pipeline."""
        config = PipelineConfig(agent_timeout_seconds=3600)
        assert config.agent_timeout_seconds == 3600

    def test_minimum_is_60(self) -> None:
        """Timeout must be at least 60 seconds."""
        with pytest.raises(ValidationError):
            PipelineConfig(agent_timeout_seconds=30)

    def test_large_value_accepted(self) -> None:
        """Large timeout values should be accepted."""
        config = PipelineConfig(agent_timeout_seconds=86400)
        assert config.agent_timeout_seconds == 86400

    def test_field_exists_in_config(self) -> None:
        """The field should be present in the model's fields."""
        config = PipelineConfig()
        assert hasattr(config, "agent_timeout_seconds")
