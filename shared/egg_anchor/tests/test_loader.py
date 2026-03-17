"""Tests for agent anchor file I/O and API sync."""

import json
import os
import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from egg_anchor.loader import ANCHOR_DIR, load_anchor, save_anchor, sync_anchor_to_api
from egg_anchor.models import AgentAnchor


def _make_anchor(**overrides):
    """Create a minimal valid AgentAnchor with defaults."""
    now = datetime(2026, 3, 17, 10, 0, 0, tzinfo=UTC)
    defaults = {
        "_meta": {
            "schema_version": "1.0",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "sequence": 1,
        },
        "agent_id": "coder-abc123",
        "role": "coder",
        "team": ["tester-def456"],
        "task": {
            "id": "task-1",
            "description": "Implement feature X",
            "phase": "implement",
        },
        "status": "working",
        "pipeline_id": "pipeline-xyz",
        "progress": [],
        "decisions": [],
        "brc_state": {"phase": "orient", "acks": [], "nacks": []},
        "key_context": [],
        "errors_encountered": [],
        "files_modified": [],
    }
    defaults.update(overrides)
    return AgentAnchor.model_validate(defaults)


class TestSaveAndLoadRoundTrip:
    """Test atomic file write and load round-trip."""

    def test_save_creates_file(self, tmp_path):
        anchor = _make_anchor()
        path = save_anchor(anchor, base_dir=str(tmp_path))
        assert path.exists()
        assert path.name == "coder-abc123.json"

    def test_save_creates_directory(self, tmp_path):
        anchor = _make_anchor()
        save_anchor(anchor, base_dir=str(tmp_path))
        anchor_dir = tmp_path / ANCHOR_DIR
        assert anchor_dir.is_dir()

    def test_load_round_trip(self, tmp_path):
        """Save then load should produce equivalent anchor."""
        anchor = _make_anchor(
            files_modified=["src/main.py"],
            key_context=[{"label": "branch", "value": "egg/test"}],
        )
        save_anchor(anchor, base_dir=str(tmp_path))
        loaded = load_anchor("coder-abc123", base_dir=str(tmp_path))

        assert loaded is not None
        assert loaded.agent_id == anchor.agent_id
        assert loaded.role == anchor.role
        assert loaded.status == anchor.status
        assert loaded.files_modified == anchor.files_modified
        assert loaded.key_context[0].label == "branch"

    def test_load_nonexistent_returns_none(self, tmp_path):
        result = load_anchor("nonexistent-agent", base_dir=str(tmp_path))
        assert result is None

    def test_load_corrupt_file_returns_none(self, tmp_path):
        """Loading a corrupt JSON file should return None."""
        anchor_dir = tmp_path / ANCHOR_DIR
        anchor_dir.mkdir(parents=True)
        corrupt_file = anchor_dir / "corrupt-agent.json"
        corrupt_file.write_text("not valid json {{{")

        result = load_anchor("corrupt-agent", base_dir=str(tmp_path))
        assert result is None

    def test_save_is_valid_json(self, tmp_path):
        """Saved file should be valid JSON."""
        anchor = _make_anchor()
        path = save_anchor(anchor, base_dir=str(tmp_path))
        data = json.loads(path.read_text())
        assert data["agent_id"] == "coder-abc123"
        assert data["_meta"]["schema_version"] == "1.0"

    def test_save_overwrites_existing(self, tmp_path):
        """Saving twice should overwrite the first save."""
        anchor1 = _make_anchor(status="working")
        save_anchor(anchor1, base_dir=str(tmp_path))

        anchor2 = _make_anchor(status="proposed")
        save_anchor(anchor2, base_dir=str(tmp_path))

        loaded = load_anchor("coder-abc123", base_dir=str(tmp_path))
        assert loaded is not None
        assert loaded.status.value == "proposed"

    def test_no_temp_files_left_on_success(self, tmp_path):
        """After successful save, no temp files should remain."""
        anchor = _make_anchor()
        save_anchor(anchor, base_dir=str(tmp_path))
        anchor_dir = tmp_path / ANCHOR_DIR
        tmp_files = list(anchor_dir.glob(".coder-abc123.*.tmp"))
        assert len(tmp_files) == 0


class TestSyncAnchorToApi:
    """Test API sync with mocked HTTP requests."""

    def test_sync_success(self):
        anchor = _make_anchor()
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("egg_anchor.loader.requests.post", return_value=mock_resp) as mock_post:
            result = sync_anchor_to_api(anchor, orchestrator_url="http://localhost:9849")

        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "http://localhost:9849/api/v1/anchors/coder-abc123" == call_args[0][0]

    def test_sync_201_accepted(self):
        anchor = _make_anchor()
        mock_resp = MagicMock()
        mock_resp.status_code = 201

        with patch("egg_anchor.loader.requests.post", return_value=mock_resp):
            result = sync_anchor_to_api(anchor, orchestrator_url="http://localhost:9849")

        assert result is True

    def test_sync_failure_http_error(self):
        anchor = _make_anchor()
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("egg_anchor.loader.requests.post", return_value=mock_resp):
            result = sync_anchor_to_api(anchor, orchestrator_url="http://localhost:9849")

        assert result is False

    def test_sync_failure_connection_error(self):
        anchor = _make_anchor()
        import requests

        with patch(
            "egg_anchor.loader.requests.post",
            side_effect=requests.ConnectionError("refused"),
        ):
            result = sync_anchor_to_api(anchor, orchestrator_url="http://localhost:9849")

        assert result is False

    def test_sync_uses_env_url(self):
        anchor = _make_anchor()
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with (
            patch.dict(os.environ, {"EGG_ORCHESTRATOR_URL": "http://custom:1234"}),
            patch("egg_anchor.loader.requests.post", return_value=mock_resp) as mock_post,
        ):
            sync_anchor_to_api(anchor)

        call_url = mock_post.call_args[0][0]
        assert call_url.startswith("http://custom:1234/")

    def test_sync_sends_serialized_data(self):
        anchor = _make_anchor()
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("egg_anchor.loader.requests.post", return_value=mock_resp) as mock_post:
            sync_anchor_to_api(anchor, orchestrator_url="http://localhost:9849")

        call_kwargs = mock_post.call_args[1]
        assert "json" in call_kwargs
        assert call_kwargs["json"]["agent_id"] == "coder-abc123"
        assert call_kwargs["timeout"] == 5


# === GAP TESTS: Concurrent access and edge cases ===


class TestConcurrentAccess:
    """Test concurrent read/write safety."""

    def test_concurrent_reads_during_write(self, tmp_path):
        """Concurrent reads don't get partial data during writes."""
        anchor = _make_anchor()
        save_anchor(anchor, base_dir=str(tmp_path))

        errors = []

        def reader():
            try:
                for _ in range(10):
                    loaded = load_anchor("coder-abc123", base_dir=str(tmp_path))
                    if loaded is not None:
                        # Should always get a valid anchor, never partial data
                        assert loaded.agent_id == "coder-abc123"
            except Exception as e:
                errors.append(e)

        def writer():
            for _i in range(10):
                updated = _make_anchor(status="working")
                save_anchor(updated, base_dir=str(tmp_path))

        threads = [
            threading.Thread(target=reader),
            threading.Thread(target=writer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent access: {errors}"


class TestAnchorPathHandling:
    """Test _anchor_path behavior with different configurations."""

    def test_save_uses_base_dir(self, tmp_path):
        """save_anchor uses base_dir when provided."""
        anchor = _make_anchor()
        path = save_anchor(anchor, base_dir=str(tmp_path))
        assert str(tmp_path) in str(path)

    def test_save_uses_env_when_no_base_dir(self, tmp_path, monkeypatch):
        """save_anchor uses EGG_REPO_PATH when base_dir is None."""
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
        anchor = _make_anchor()
        path = save_anchor(anchor)
        assert str(tmp_path) in str(path)

    def test_load_with_invalid_json_schema(self, tmp_path):
        """Loading valid JSON but invalid schema returns None."""
        anchor_dir = tmp_path / ANCHOR_DIR
        anchor_dir.mkdir(parents=True)
        invalid_file = anchor_dir / "invalid-agent.json"
        invalid_file.write_text('{"not": "an anchor"}')

        result = load_anchor("invalid-agent", base_dir=str(tmp_path))
        assert result is None

    def test_multiple_agents_coexist(self, tmp_path):
        """Multiple agents can save anchors in the same directory."""
        anchor1 = _make_anchor(agent_id="coder-abc123")
        anchor2 = _make_anchor(agent_id="tester-def456")

        save_anchor(anchor1, base_dir=str(tmp_path))
        save_anchor(anchor2, base_dir=str(tmp_path))

        loaded1 = load_anchor("coder-abc123", base_dir=str(tmp_path))
        loaded2 = load_anchor("tester-def456", base_dir=str(tmp_path))

        assert loaded1 is not None
        assert loaded2 is not None
        assert loaded1.agent_id == "coder-abc123"
        assert loaded2.agent_id == "tester-def456"

    def test_save_preserves_full_data_integrity(self, tmp_path):
        """All fields are preserved after save, including nested objects."""
        now = datetime.now(tz=UTC).isoformat()
        anchor = _make_anchor(
            progress=[
                {"step": "Step 1", "state": "complete", "timestamp": now},
                {"step": "Step 2", "state": "working", "detail": "3/5 suites", "timestamp": now},
            ],
            decisions=[
                {
                    "id": "d-1",
                    "question": "Which approach?",
                    "answer": "Approach B",
                    "decided_by": "human",
                    "timestamp": now,
                }
            ],
            key_context=[{"label": "branch", "value": "egg/test"}],
            errors_encountered=[
                {"error": "Connection timeout", "resolution": "Retry", "timestamp": now}
            ],
            files_modified=["file1.py", "file2.py"],
            brc_state={
                "phase": "proposed",
                "proposed_at": now,
                "acks": ["reviewer-1"],
                "nacks": [],
                "last_message_id": "msg-abc",
            },
        )
        save_anchor(anchor, base_dir=str(tmp_path))
        loaded = load_anchor("coder-abc123", base_dir=str(tmp_path))

        assert loaded is not None
        assert len(loaded.progress) == 2
        assert loaded.progress[1].detail == "3/5 suites"
        assert len(loaded.decisions) == 1
        assert loaded.decisions[0].answer == "Approach B"
        assert loaded.brc_state.last_message_id == "msg-abc"
        assert len(loaded.brc_state.acks) == 1
