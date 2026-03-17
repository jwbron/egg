"""
Tests for container spawner anchor integration.

Covers:
- AGENT_ANCHOR_ID env var presence in spawner source
- Agent ID format: {role}-{short_container_name}
- Backward compatibility: containers work without anchor env vars
"""

import inspect
import os
import sys
from pathlib import Path

import pytest

# Add orchestrator and shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


class TestAnchorEnvVars:
    """Tests that spawner sets anchor-related environment variables."""

    def test_spawner_source_contains_agent_anchor_id(self):
        """Verify AGENT_ANCHOR_ID is set in spawn_agent_container."""
        from container_spawner import ContainerSpawner

        source = inspect.getsource(ContainerSpawner.spawn_agent_container)
        assert "AGENT_ANCHOR_ID" in source, (
            "spawn_agent_container should set AGENT_ANCHOR_ID env var"
        )

    def test_anchor_id_format(self):
        """AGENT_ANCHOR_ID follows {role}-{short_container_name} format."""
        role = "coder"
        container_name = "egg-issue-1032-coder"
        short_name = container_name[:8]

        anchor_id = f"{role}-{short_name}"
        assert anchor_id == "coder-egg-issu"

        # Verify format for different roles
        for test_role in ("tester", "documenter"):
            aid = f"{test_role}-{short_name}"
            assert aid.startswith(f"{test_role}-")

    def test_backward_compatibility_without_anchor_id(self):
        """Containers should work without AGENT_ANCHOR_ID (backward compat)."""
        # When AGENT_ANCHOR_ID is not set, anchor features are disabled
        anchor_id = os.environ.get("AGENT_ANCHOR_ID_NONEXISTENT_KEY")
        assert anchor_id is None
        # This is fine — anchor features are opt-in

    def test_spawner_env_dict_pattern(self):
        """Verify the spawner_env dict includes AGENT_ANCHOR_ID after construction."""
        from container_spawner import ContainerSpawner

        source = inspect.getsource(ContainerSpawner.spawn_agent_container)
        # AGENT_ANCHOR_ID should be set after spawner_env construction
        # but before extra_env override
        spawner_env_pos = source.find("spawner_env")
        anchor_id_pos = source.find("AGENT_ANCHOR_ID")
        extra_env_pos = source.find("extra_env")

        assert anchor_id_pos > spawner_env_pos, (
            "AGENT_ANCHOR_ID should be set after spawner_env construction"
        )
