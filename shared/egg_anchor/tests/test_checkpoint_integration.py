"""Tests for anchor integration with checkpoint captures."""

import sys
from pathlib import Path

import pytest

_shared_path = Path(__file__).parent.parent.parent
if str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


class TestCheckpointAnchorField:
    """Test that checkpoint model supports anchor data."""

    def test_checkpoint_model_has_anchors_field(self):
        """Verify the checkpoint model accepts anchor data."""
        from egg_contracts.checkpoints import CheckpointV2

        # The model should accept an 'anchors' field
        # If the field doesn't exist, this will raise a validation error
        import inspect

        source = inspect.getsource(CheckpointV2)
        assert "anchors" in source

    def test_checkpoint_anchors_default_is_none(self):
        """Verify the anchors field defaults to None."""
        from egg_contracts.checkpoints import CheckpointV2

        field_info = CheckpointV2.model_fields["anchors"]
        assert field_info.default is None

    def test_checkpoint_accepts_anchor_data(self):
        """Verify CheckpointV2 can be instantiated with anchor data."""
        from datetime import UTC, datetime

        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            TriggerType,
        )

        now = datetime.now(UTC)
        anchor_data = [
            {
                "agent_id": "coder-abc12345",
                "role": "coder",
                "status": "working",
                "task": "Implement feature",
            }
        ]

        checkpoint = CheckpointV2(
            id="ckpt-abcdef0123456789",
            trigger_type=TriggerType.SESSION_END,
            session_id="test-session-1",
            session=SessionMetadata(
                session_id="test-session-1",
                started_at=now,
            ),
            created_at=now,
            session_started_at=now,
            anchors=anchor_data,
        )

        assert checkpoint.anchors is not None
        assert len(checkpoint.anchors) == 1
        assert checkpoint.anchors[0]["agent_id"] == "coder-abc12345"

    def test_checkpoint_without_anchors(self):
        """Verify CheckpointV2 works without anchor data (backward compat)."""
        from datetime import UTC, datetime

        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            TriggerType,
        )

        now = datetime.now(UTC)

        checkpoint = CheckpointV2(
            id="ckpt-abcdef0123456789",
            trigger_type=TriggerType.SESSION_END,
            session_id="test-session-1",
            session=SessionMetadata(
                session_id="test-session-1",
                started_at=now,
            ),
            created_at=now,
            session_started_at=now,
        )

        assert checkpoint.anchors is None
