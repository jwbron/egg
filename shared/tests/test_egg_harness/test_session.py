"""Tests for egg_harness.session — JSONL session persistence."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest
from egg_harness.session import SessionManager, SessionState

# ---------------------------------------------------------------------------
# TestSessionState
# ---------------------------------------------------------------------------


class TestSessionState:
    def test_default_field_values(self):
        state = SessionState(session_id="test-1", model="opus")
        assert state.messages == []
        assert state.system_prompt is None
        assert state.total_cost_usd == 0.0
        assert state.turn_count == 0
        assert state.duration_ms == 0
        assert state.compaction_count == 0

    def test_timestamps_are_iso8601(self):
        state = SessionState(session_id="test-1", model="opus")
        # ISO 8601 contains 'T' and timezone info
        assert "T" in state.created_at
        assert "T" in state.updated_at

    def test_custom_fields(self):
        msgs = [{"role": "user", "content": "hi"}]
        state = SessionState(
            session_id="custom-1",
            model="sonnet",
            messages=msgs,
            system_prompt="Be helpful",
            total_cost_usd=1.23,
            turn_count=5,
            duration_ms=1000,
            compaction_count=2,
        )
        assert state.session_id == "custom-1"
        assert state.model == "sonnet"
        assert state.messages == msgs
        assert state.system_prompt == "Be helpful"
        assert state.total_cost_usd == 1.23


# ---------------------------------------------------------------------------
# TestSessionManagerSave
# ---------------------------------------------------------------------------


class TestSessionManagerSave:
    def test_save_creates_directory_if_missing(self, tmp_path):
        storage = str(tmp_path / "sessions")
        mgr = SessionManager(session_id="s1", storage_dir=storage)
        state = SessionState(session_id="s1", model="opus")
        mgr.save(state)
        assert os.path.isdir(storage)

    def test_save_creates_jsonl_file(self, tmp_path):
        storage = str(tmp_path / "sessions")
        mgr = SessionManager(session_id="s1", storage_dir=storage)
        state = SessionState(session_id="s1", model="opus")
        mgr.save(state)
        filepath = os.path.join(storage, "s1.jsonl")
        assert os.path.isfile(filepath)

    def test_metadata_on_line_1(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(session_id="s1", storage_dir=storage)
        state = SessionState(session_id="s1", model="opus", total_cost_usd=0.5)
        mgr.save(state)
        with open(os.path.join(storage, "s1.jsonl")) as f:
            lines = f.read().splitlines()
        meta = json.loads(lines[0])
        assert meta["session_id"] == "s1"
        assert meta["model"] == "opus"
        assert meta["total_cost_usd"] == 0.5
        assert "messages" not in meta

    def test_messages_on_subsequent_lines(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(session_id="s1", storage_dir=storage)
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        state = SessionState(session_id="s1", model="opus", messages=msgs)
        mgr.save(state)
        with open(os.path.join(storage, "s1.jsonl")) as f:
            lines = f.read().splitlines()
        assert len(lines) == 3  # 1 metadata + 2 messages
        assert json.loads(lines[1])["role"] == "user"
        assert json.loads(lines[2])["role"] == "assistant"

    def test_save_overwrites_existing(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(session_id="s1", storage_dir=storage)
        state1 = SessionState(session_id="s1", model="opus", turn_count=1)
        mgr.save(state1)
        state2 = SessionState(session_id="s1", model="opus", turn_count=5)
        mgr.save(state2)
        with open(os.path.join(storage, "s1.jsonl")) as f:
            meta = json.loads(f.readline())
        assert meta["turn_count"] == 5

    def test_save_with_empty_messages(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(session_id="s1", storage_dir=storage)
        state = SessionState(session_id="s1", model="opus")
        mgr.save(state)
        with open(os.path.join(storage, "s1.jsonl")) as f:
            lines = f.read().splitlines()
        assert len(lines) == 1  # metadata only

    def test_no_tmp_files_after_save(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(session_id="s1", storage_dir=storage)
        state = SessionState(session_id="s1", model="opus")
        mgr.save(state)
        tmp_files = [f for f in os.listdir(storage) if f.endswith(".tmp")]
        assert tmp_files == []

    def test_save_updates_updated_at(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(session_id="s1", storage_dir=storage)
        state = SessionState(session_id="s1", model="opus", updated_at="old-timestamp")
        mgr.save(state)
        with open(os.path.join(storage, "s1.jsonl")) as f:
            meta = json.loads(f.readline())
        assert meta["updated_at"] != "old-timestamp"


# ---------------------------------------------------------------------------
# TestSessionManagerSaveFailure
# ---------------------------------------------------------------------------


class TestSessionManagerSaveFailure:
    def test_failure_cleans_temp_file(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(session_id="s1", storage_dir=storage)
        state = SessionState(session_id="s1", model="opus")

        original_fdopen = os.fdopen

        def failing_fdopen(fd, *args, **kwargs):
            fh = original_fdopen(fd, *args, **kwargs)
            fh.write = lambda _: (_ for _ in ()).throw(OSError("disk full"))
            return fh

        with patch("egg_harness.session.os.fdopen", side_effect=failing_fdopen):
            with pytest.raises(OSError):
                mgr.save(state)

        # No temp or session files should remain
        files = os.listdir(storage)
        tmp_files = [f for f in files if f.endswith(".tmp")]
        assert tmp_files == []


# ---------------------------------------------------------------------------
# TestSessionManagerLoad
# ---------------------------------------------------------------------------


class TestSessionManagerLoad:
    def test_round_trip(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(session_id="s1", storage_dir=storage)
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        state = SessionState(
            session_id="s1",
            model="opus",
            messages=msgs,
            system_prompt="Be helpful",
            total_cost_usd=1.5,
            turn_count=3,
            duration_ms=500,
            compaction_count=1,
        )
        mgr.save(state)
        loaded = mgr.load("s1")
        assert loaded is not None
        assert loaded.session_id == "s1"
        assert loaded.model == "opus"
        assert loaded.messages == msgs
        assert loaded.system_prompt == "Be helpful"
        assert loaded.total_cost_usd == 1.5
        assert loaded.turn_count == 3
        assert loaded.duration_ms == 500
        assert loaded.compaction_count == 1

    def test_load_nonexistent_returns_none(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(session_id="s1", storage_dir=storage)
        assert mgr.load("nonexistent") is None

    def test_load_empty_file_returns_none(self, tmp_path):
        storage = str(tmp_path)
        filepath = os.path.join(storage, "empty.jsonl")
        os.makedirs(storage, exist_ok=True)
        open(filepath, "w").close()
        mgr = SessionManager(session_id="empty", storage_dir=storage)
        assert mgr.load("empty") is None

    def test_load_preserves_message_order(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(session_id="s1", storage_dir=storage)
        msgs = [{"role": "user", "content": f"msg-{i}"} for i in range(10)]
        state = SessionState(session_id="s1", model="opus", messages=msgs)
        mgr.save(state)
        loaded = mgr.load("s1")
        for i, msg in enumerate(loaded.messages):
            assert msg["content"] == f"msg-{i}"

    def test_load_missing_fields_use_defaults(self, tmp_path):
        storage = str(tmp_path)
        os.makedirs(storage, exist_ok=True)
        filepath = os.path.join(storage, "minimal.jsonl")
        with open(filepath, "w") as f:
            f.write(json.dumps({"session_id": "minimal", "model": "opus"}) + "\n")
        mgr = SessionManager(storage_dir=storage)
        loaded = mgr.load("minimal")
        assert loaded is not None
        assert loaded.total_cost_usd == 0.0
        assert loaded.turn_count == 0
        assert loaded.messages == []


# ---------------------------------------------------------------------------
# TestShouldAutoSave
# ---------------------------------------------------------------------------


class TestShouldAutoSave:
    def test_positive_multiple_of_interval(self):
        mgr = SessionManager(auto_save_interval=5)
        assert mgr.should_auto_save(5) is True
        assert mgr.should_auto_save(10) is True
        assert mgr.should_auto_save(15) is True

    def test_non_multiple_returns_false(self):
        mgr = SessionManager(auto_save_interval=5)
        assert mgr.should_auto_save(3) is False
        assert mgr.should_auto_save(7) is False

    def test_zero_turn_returns_false(self):
        mgr = SessionManager(auto_save_interval=5)
        assert mgr.should_auto_save(0) is False

    def test_negative_turn_returns_false(self):
        mgr = SessionManager(auto_save_interval=5)
        assert mgr.should_auto_save(-5) is False

    def test_custom_interval(self):
        mgr = SessionManager(auto_save_interval=3)
        assert mgr.should_auto_save(3) is True
        assert mgr.should_auto_save(4) is False
        assert mgr.should_auto_save(6) is True

    def test_interval_1_always_true_for_positive(self):
        mgr = SessionManager(auto_save_interval=1)
        assert mgr.should_auto_save(1) is True
        assert mgr.should_auto_save(2) is True


# ---------------------------------------------------------------------------
# TestSessionPathSanitization
# ---------------------------------------------------------------------------


class TestSessionPathSanitization:
    def test_path_traversal_stripped(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(storage_dir=storage)
        path = mgr._session_path("../../etc/passwd")
        # os.path.basename strips directory components
        assert ".." not in os.path.basename(path)
        assert path.startswith(storage)

    def test_dot_prefix_gets_session_prefix(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(storage_dir=storage)
        path = mgr._session_path(".hidden")
        basename = os.path.basename(path)
        assert basename.startswith("session-")

    def test_empty_session_id(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(storage_dir=storage)
        path = mgr._session_path("")
        basename = os.path.basename(path)
        assert "session-unknown" in basename

    def test_normal_id_produces_expected_path(self, tmp_path):
        storage = str(tmp_path)
        mgr = SessionManager(storage_dir=storage)
        path = mgr._session_path("abc-123")
        assert path == os.path.join(storage, "abc-123.jsonl")


# ---------------------------------------------------------------------------
# TestSessionManagerProperties
# ---------------------------------------------------------------------------


class TestSessionManagerProperties:
    def test_session_id_property(self):
        mgr = SessionManager(session_id="my-session")
        assert mgr.session_id == "my-session"

    def test_auto_generated_session_id(self):
        mgr = SessionManager()
        assert mgr.session_id  # not empty
        assert len(mgr.session_id) > 0

    def test_storage_dir_property(self):
        mgr = SessionManager(storage_dir="/tmp/custom")
        assert mgr.storage_dir == "/tmp/custom"
