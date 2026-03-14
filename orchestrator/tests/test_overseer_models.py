"""
Tests for overseer-related model fields in PipelineConfig and AgentRole.
"""

import pytest
from models import (
    AgentRole,
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)


class TestOverseerRole:
    """Tests for the OVERSEER agent role in the orchestrator AgentRole enum."""

    def test_overseer_role_exists(self):
        """OVERSEER must be a valid AgentRole value."""
        assert AgentRole.OVERSEER == "overseer"

    def test_overseer_in_all_roles(self):
        """OVERSEER must appear in the list of all roles."""
        roles = list(AgentRole)
        assert AgentRole.OVERSEER in roles

    def test_overseer_string_conversion(self):
        """OVERSEER value round-trips through string conversion."""
        assert AgentRole("overseer") == AgentRole.OVERSEER


class TestPipelineConfigOverseerDefaults:
    """Tests for PipelineConfig overseer fields and defaults."""

    def test_overseer_enabled_default(self):
        """overseer_enabled should default to True."""
        config = PipelineConfig()
        assert config.overseer_enabled is True

    def test_overseer_poll_interval_default(self):
        """overseer_poll_interval_seconds should default to 30."""
        config = PipelineConfig()
        assert config.overseer_poll_interval_seconds == 30

    def test_overseer_stall_threshold_default(self):
        """overseer_stall_base_threshold_seconds should default to 120."""
        config = PipelineConfig()
        assert config.overseer_stall_base_threshold_seconds == 120

    def test_overseer_max_redirects_default(self):
        """overseer_max_redirects_before_escalation should default to 2."""
        config = PipelineConfig()
        assert config.overseer_max_redirects_before_escalation == 2

    def test_overseer_custom_values(self):
        """PipelineConfig accepts custom overseer values."""
        config = PipelineConfig(
            overseer_enabled=False,
            overseer_poll_interval_seconds=60,
            overseer_stall_base_threshold_seconds=300,
            overseer_max_redirects_before_escalation=5,
        )
        assert config.overseer_enabled is False
        assert config.overseer_poll_interval_seconds == 60
        assert config.overseer_stall_base_threshold_seconds == 300
        assert config.overseer_max_redirects_before_escalation == 5

    def test_overseer_poll_interval_minimum(self):
        """overseer_poll_interval_seconds must be >= 5."""
        with pytest.raises(Exception):
            PipelineConfig(overseer_poll_interval_seconds=2)

    def test_overseer_stall_threshold_minimum(self):
        """overseer_stall_base_threshold_seconds must be >= 30."""
        with pytest.raises(Exception):
            PipelineConfig(overseer_stall_base_threshold_seconds=10)

    def test_overseer_max_redirects_minimum(self):
        """overseer_max_redirects_before_escalation must be >= 1."""
        with pytest.raises(Exception):
            PipelineConfig(overseer_max_redirects_before_escalation=0)


class TestPipelineConfigOverseerSerialization:
    """Tests for PipelineConfig overseer field serialization."""

    def test_overseer_config_roundtrip(self):
        """PipelineConfig with overseer fields round-trips through JSON."""
        config = PipelineConfig(
            overseer_enabled=False,
            overseer_poll_interval_seconds=45,
            overseer_stall_base_threshold_seconds=180,
            overseer_max_redirects_before_escalation=3,
        )
        data = config.model_dump()
        restored = PipelineConfig(**data)
        assert restored.overseer_enabled == config.overseer_enabled
        assert restored.overseer_poll_interval_seconds == config.overseer_poll_interval_seconds
        assert (
            restored.overseer_stall_base_threshold_seconds
            == config.overseer_stall_base_threshold_seconds
        )
        assert (
            restored.overseer_max_redirects_before_escalation
            == config.overseer_max_redirects_before_escalation
        )

    def test_pipeline_with_overseer_config(self):
        """Pipeline model accepts overseer config fields."""
        pipeline = Pipeline(
            id="test-pipeline",
            config=PipelineConfig(
                overseer_enabled=True,
                overseer_poll_interval_seconds=30,
            ),
        )
        assert pipeline.config.overseer_enabled is True
        assert pipeline.config.overseer_poll_interval_seconds == 30
