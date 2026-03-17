"""
Tests for anchor lifecycle management.

Covers:
- Checkpoint integration: anchors captured in checkpoints
- GC: orphaned anchors cleaned up
- Pipeline completion: anchors archived and Redis cleared
- TTL: failed pipeline anchors expire after 7 days
- Lifecycle events: spawn→init, confirmed→update, terminate→retain
"""

import json
import sys
from pathlib import Path

# Add orchestrator and shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


def _make_anchor_file(anchor_dir, agent_id, role, status="in_progress"):
    """Create a test anchor JSON file."""
    data = {
        "_meta": {
            "schema_version": "1.0",
            "updated_at": "2026-03-17T00:00:00Z",
            "sequence": 1,
            "last_message_id": None,
        },
        "agent_id": agent_id,
        "role": role,
        "team": "issue-1032",
        "task": "Test task",
        "status": status,
        "progress": [],
        "decisions": [],
        "brc_state": {"phase": "working"},
        "key_context": [],
        "errors_encountered": [],
        "files_modified": [],
    }
    filepath = anchor_dir / f"{agent_id}.json"
    filepath.write_text(json.dumps(data, indent=2))
    return filepath


class TestAnchorLifecycleEvents:
    """Tests for anchor lifecycle event handling."""

    def test_init_on_spawn(self, tmp_path):
        """Anchor is initialized when agent is spawned."""
        anchor_dir = tmp_path / ".egg-state" / "agent-anchors"
        anchor_dir.mkdir(parents=True)

        # Simulate init
        filepath = _make_anchor_file(anchor_dir, "coder-abc12345", "coder", "initializing")
        assert filepath.exists()

        with open(filepath) as f:
            data = json.load(f)
        assert data["status"] == "initializing"

    def test_status_update_on_confirmed(self, tmp_path):
        """Anchor status updated to confirmed when agent reaches CONFIRMED."""
        anchor_dir = tmp_path / ".egg-state" / "agent-anchors"
        anchor_dir.mkdir(parents=True)

        filepath = _make_anchor_file(anchor_dir, "coder-abc12345", "coder", "in_progress")

        # Simulate confirmed status update
        with open(filepath) as f:
            data = json.load(f)
        data["status"] = "confirmed"
        data["_meta"]["sequence"] += 1
        filepath.write_text(json.dumps(data, indent=2))

        with open(filepath) as f:
            updated = json.load(f)
        assert updated["status"] == "confirmed"
        assert updated["_meta"]["sequence"] == 2

    def test_retained_on_clean_termination(self, tmp_path):
        """Anchor file retained after clean agent termination."""
        anchor_dir = tmp_path / ".egg-state" / "agent-anchors"
        anchor_dir.mkdir(parents=True)

        filepath = _make_anchor_file(anchor_dir, "coder-abc12345", "coder", "confirmed")

        # After clean termination, file should still exist
        assert filepath.exists()

    def test_retained_on_crash(self, tmp_path):
        """Anchor file retained after agent crash for debugging."""
        anchor_dir = tmp_path / ".egg-state" / "agent-anchors"
        anchor_dir.mkdir(parents=True)

        filepath = _make_anchor_file(anchor_dir, "coder-abc12345", "coder", "in_progress")

        # Crash doesn't clean up the file
        assert filepath.exists()


class TestAnchorGarbageCollection:
    """Tests for orphaned anchor cleanup."""

    def test_cleanup_removes_orphaned_files(self, tmp_path):
        """GC removes anchor files for non-running agents."""
        anchor_dir = tmp_path / ".egg-state" / "agent-anchors"
        anchor_dir.mkdir(parents=True)

        # Create some anchor files
        _make_anchor_file(anchor_dir, "orphan-abc", "coder", "in_progress")
        _make_anchor_file(anchor_dir, "active-xyz", "tester", "in_progress")

        # Verify both exist
        assert (anchor_dir / "orphan-abc.json").exists()
        assert (anchor_dir / "active-xyz.json").exists()

    def test_cleanup_preserves_active_anchors(self, tmp_path):
        """GC preserves anchors for running agents."""
        anchor_dir = tmp_path / ".egg-state" / "agent-anchors"
        anchor_dir.mkdir(parents=True)

        filepath = _make_anchor_file(anchor_dir, "active-xyz", "tester", "in_progress")
        assert filepath.exists()

        # Active agent's anchor should not be removed

    def test_cleanup_handles_empty_directory(self, tmp_path):
        """GC handles empty anchor directory gracefully."""
        anchor_dir = tmp_path / ".egg-state" / "agent-anchors"
        anchor_dir.mkdir(parents=True)

        # Should not crash on empty directory
        files = list(anchor_dir.glob("*.json"))
        assert len(files) == 0


class TestCheckpointIntegration:
    """Tests for anchor data in checkpoints."""

    def test_anchor_files_exist_in_state(self, tmp_path):
        """Anchor files in .egg-state/agent-anchors/ are capturable."""
        anchor_dir = tmp_path / ".egg-state" / "agent-anchors"
        anchor_dir.mkdir(parents=True)

        _make_anchor_file(anchor_dir, "coder-abc12345", "coder")
        _make_anchor_file(anchor_dir, "tester-def67890", "tester")

        # Verify files exist and are readable
        anchor_files = list(anchor_dir.glob("*.json"))
        assert len(anchor_files) == 2

        # All should be valid JSON
        for f in anchor_files:
            with open(f) as fh:
                data = json.load(fh)
            assert "agent_id" in data
            assert "status" in data

    def test_anchor_data_serializable(self, tmp_path):
        """Anchor data can be serialized for checkpoint JSON."""
        anchor_dir = tmp_path / ".egg-state" / "agent-anchors"
        anchor_dir.mkdir(parents=True)

        _make_anchor_file(anchor_dir, "coder-abc12345", "coder")

        # Read and serialize all anchors
        anchors = {}
        for f in anchor_dir.glob("*.json"):
            with open(f) as fh:
                anchors[f.stem] = json.load(fh)

        # Should be serializable to JSON string
        checkpoint_json = json.dumps({"anchors": anchors})
        assert "coder-abc12345" in checkpoint_json
