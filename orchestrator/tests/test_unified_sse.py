"""
Tests for unified SSE streaming module.
"""

import json
import time
from pathlib import Path
from queue import Queue
from unittest.mock import MagicMock, patch

import pytest
import unified_sse as unified_sse_module
from events import Event, EventBus, EventType
from models import Pipeline, PipelinePhase, PipelineStatus
from unified_sse import (
    UnifiedSSEManager,
    create_unified_sse_stream,
    get_unified_sse_manager,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the singleton unified SSE manager before and after each test."""
    original = unified_sse_module._unified_manager
    unified_sse_module._unified_manager = None
    yield
    unified_sse_module._unified_manager = original


def create_test_pipeline(
    pipeline_id: str = "test-123",
    status: PipelineStatus = PipelineStatus.RUNNING,
    current_phase: PipelinePhase = PipelinePhase.IMPLEMENT,
    repo: str = "test/repo",
    branch: str = "egg/test-feature",
) -> Pipeline:
    """Create a test pipeline."""
    return Pipeline(
        id=pipeline_id,
        issue_number=123,
        repo=repo,
        branch=branch,
        status=status,
        current_phase=current_phase,
    )


class TestUnifiedSSEManager:
    """Tests for UnifiedSSEManager."""

    def test_add_and_remove_client(self):
        """Test adding and removing a client."""
        bus = EventBus()
        manager = UnifiedSSEManager(event_bus=bus)

        q = manager.add_client()
        assert isinstance(q, Queue)
        assert manager.get_client_count() == 1

        manager.remove_client(q)
        assert manager.get_client_count() == 0

    def test_multiple_clients(self):
        """Test multiple unified clients."""
        bus = EventBus()
        manager = UnifiedSSEManager(event_bus=bus)

        q1 = manager.add_client()
        q2 = manager.add_client()
        assert manager.get_client_count() == 2

        manager.remove_client(q1)
        assert manager.get_client_count() == 1

        manager.remove_client(q2)
        assert manager.get_client_count() == 0

    def test_remove_nonexistent_client(self):
        """Test removing a client that doesn't exist doesn't raise."""
        bus = EventBus()
        manager = UnifiedSSEManager(event_bus=bus)
        q = Queue()
        manager.remove_client(q)  # Should not raise

    def test_events_reach_all_clients(self):
        """Test that ALL pipeline events reach all unified clients."""
        bus = EventBus()
        manager = UnifiedSSEManager(event_bus=bus)
        manager.subscribe_to_events()

        q1 = manager.add_client()
        q2 = manager.add_client()

        # Publish event for pipeline-1
        event = Event(
            event_type=EventType.PHASE_STARTED,
            pipeline_id="pipeline-1",
            data={"phase": "plan"},
        )
        bus.publish(event)

        # Both clients should receive it
        assert not q1.empty()
        assert not q2.empty()

        payload1 = q1.get_nowait()
        assert payload1["event_type"] == "phase.started"
        assert payload1["pipeline_id"] == "pipeline-1"

        payload2 = q2.get_nowait()
        assert payload2["pipeline_id"] == "pipeline-1"

        manager.unsubscribe_from_events()
        manager.remove_client(q1)
        manager.remove_client(q2)

    def test_events_from_different_pipelines(self):
        """Test that events from any pipeline reach unified clients."""
        bus = EventBus()
        manager = UnifiedSSEManager(event_bus=bus)
        manager.subscribe_to_events()

        q = manager.add_client()

        # Events from two different pipelines
        bus.publish(Event(
            event_type=EventType.PHASE_STARTED,
            pipeline_id="pipeline-1",
            data={"phase": "plan"},
        ))
        bus.publish(Event(
            event_type=EventType.PHASE_COMPLETED,
            pipeline_id="pipeline-2",
            data={"phase": "implement"},
        ))

        assert q.qsize() == 2
        p1 = q.get_nowait()
        p2 = q.get_nowait()
        assert p1["pipeline_id"] == "pipeline-1"
        assert p2["pipeline_id"] == "pipeline-2"

        manager.unsubscribe_from_events()
        manager.remove_client(q)

    def test_terminal_event_sets_flag(self):
        """Test that terminal events set is_terminal in payload."""
        bus = EventBus()
        manager = UnifiedSSEManager(event_bus=bus)
        manager.subscribe_to_events()

        q = manager.add_client()

        event = Event(
            event_type=EventType.PIPELINE_COMPLETED,
            pipeline_id="pipeline-1",
            data={"status": "complete"},
        )
        bus.publish(event)

        payload = q.get_nowait()
        assert payload["is_terminal"] is True

        manager.unsubscribe_from_events()
        manager.remove_client(q)

    def test_non_terminal_event_flag(self):
        """Test that non-terminal events set is_terminal=False."""
        bus = EventBus()
        manager = UnifiedSSEManager(event_bus=bus)
        manager.subscribe_to_events()

        q = manager.add_client()

        event = Event(
            event_type=EventType.PHASE_STARTED,
            pipeline_id="pipeline-1",
            data={},
        )
        bus.publish(event)

        payload = q.get_nowait()
        assert payload["is_terminal"] is False

        manager.unsubscribe_from_events()
        manager.remove_client(q)

    def test_subscribe_idempotent(self):
        """Test that subscribing twice doesn't create duplicate handlers."""
        bus = EventBus()
        manager = UnifiedSSEManager(event_bus=bus)
        manager.subscribe_to_events()
        manager.subscribe_to_events()
        assert manager._subscribed is True
        manager.unsubscribe_from_events()

    def test_unsubscribe(self):
        """Test unsubscribing stops event delivery."""
        bus = EventBus()
        manager = UnifiedSSEManager(event_bus=bus)
        manager.subscribe_to_events()
        manager.unsubscribe_from_events()
        assert manager._subscribed is False

    def test_enrichment_with_visualization(self):
        """Test that events are enriched with compact status when available."""
        bus = EventBus()
        manager = UnifiedSSEManager(event_bus=bus)
        manager.subscribe_to_events()

        q = manager.add_client()
        pipeline = create_test_pipeline()

        with patch("unified_sse._resolve_repo_path") as mock_path, \
             patch("unified_sse.get_state_store") as mock_store, \
             patch("unified_sse.render_compact_status") as mock_compact, \
             patch("unified_sse.render_progress_bar") as mock_progress:
            mock_path.return_value = Path("/tmp/test")
            store_instance = MagicMock()
            store_instance.load_pipeline.return_value = pipeline
            mock_store.return_value = store_instance
            mock_compact.return_value = "COMPACT STATUS"
            mock_progress.return_value = "[####] 50%"

            event = Event(
                event_type=EventType.PHASE_STARTED,
                pipeline_id="test-123",
                data={},
            )
            bus.publish(event)

        payload = q.get_nowait()
        assert payload["compact"] == "COMPACT STATUS"
        assert payload["progress"] == "[####] 50%"
        assert payload["status"] == "running"
        assert payload["current_phase"] == "implement"

        manager.unsubscribe_from_events()
        manager.remove_client(q)


class TestCreateUnifiedSSEStream:
    """Tests for create_unified_sse_stream generator."""

    def test_initial_snapshot_with_active_pipelines(self):
        """Test that snapshot includes all active pipelines."""
        pipelines = [
            create_test_pipeline("p-1", PipelineStatus.RUNNING),
            create_test_pipeline("p-2", PipelineStatus.RUNNING),
        ]

        with patch("unified_sse.get_unified_sse_manager") as mock_mgr, \
             patch("unified_sse._collect_all_pipelines_safe") as mock_collect, \
             patch("unified_sse.render_compact_status") as mock_compact, \
             patch("unified_sse.render_progress_bar") as mock_progress:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager

            mock_collect.return_value = pipelines
            mock_compact.return_value = "COMPACT"
            mock_progress.return_value = "[##] 50%"

            gen = create_unified_sse_stream(repo_path=Path("/tmp/test"))
            snapshot = next(gen)
            gen.close()

            assert "event: snapshot" in snapshot
            assert "retry: 5000" in snapshot

            # Parse the snapshot data
            data_line = [
                l for l in snapshot.split("\n") if l.startswith("data: ")
            ][0]
            data = json.loads(data_line[6:])
            assert len(data["pipelines"]) == 2
            assert data["pipelines"][0]["pipeline_id"] == "p-1"
            assert data["pipelines"][1]["pipeline_id"] == "p-2"

    def test_snapshot_filters_terminal_pipelines(self):
        """Test that snapshot excludes terminal pipelines when active_only=True."""
        pipelines = [
            create_test_pipeline("p-1", PipelineStatus.RUNNING),
            create_test_pipeline("p-2", PipelineStatus.COMPLETE),
            create_test_pipeline("p-3", PipelineStatus.FAILED),
        ]

        with patch("unified_sse.get_unified_sse_manager") as mock_mgr, \
             patch("unified_sse._collect_all_pipelines_safe") as mock_collect, \
             patch("unified_sse.render_compact_status") as mock_compact, \
             patch("unified_sse.render_progress_bar") as mock_progress:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager

            mock_collect.return_value = pipelines
            mock_compact.return_value = "COMPACT"
            mock_progress.return_value = "[##] 50%"

            gen = create_unified_sse_stream(
                repo_path=Path("/tmp/test"), active_only=True
            )
            snapshot = next(gen)
            gen.close()

            data_line = [
                l for l in snapshot.split("\n") if l.startswith("data: ")
            ][0]
            data = json.loads(data_line[6:])
            assert len(data["pipelines"]) == 1
            assert data["pipelines"][0]["pipeline_id"] == "p-1"

    def test_snapshot_includes_all_when_active_only_false(self):
        """Test that snapshot includes all pipelines when active_only=False."""
        pipelines = [
            create_test_pipeline("p-1", PipelineStatus.RUNNING),
            create_test_pipeline("p-2", PipelineStatus.COMPLETE),
        ]

        with patch("unified_sse.get_unified_sse_manager") as mock_mgr, \
             patch("unified_sse._collect_all_pipelines_safe") as mock_collect, \
             patch("unified_sse.render_compact_status") as mock_compact, \
             patch("unified_sse.render_progress_bar") as mock_progress:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager

            mock_collect.return_value = pipelines
            mock_compact.return_value = "COMPACT"
            mock_progress.return_value = "[##] 50%"

            gen = create_unified_sse_stream(
                repo_path=Path("/tmp/test"), active_only=False
            )
            snapshot = next(gen)
            gen.close()

            data_line = [
                l for l in snapshot.split("\n") if l.startswith("data: ")
            ][0]
            data = json.loads(data_line[6:])
            assert len(data["pipelines"]) == 2

    def test_terminal_event_does_not_end_stream(self):
        """Test that terminal event for one pipeline doesn't end the unified stream."""
        with patch("unified_sse.get_unified_sse_manager") as mock_mgr, \
             patch("unified_sse._resolve_repo_path") as mock_path:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager
            mock_path.return_value = None

            # Put a terminal event then a normal event
            terminal_payload = {
                "event_type": "pipeline.completed",
                "pipeline_id": "p-1",
                "is_terminal": True,
            }
            normal_payload = {
                "event_type": "phase.started",
                "pipeline_id": "p-2",
                "is_terminal": False,
            }
            mock_q.put(terminal_payload)
            mock_q.put(normal_payload)

            gen = create_unified_sse_stream(repo_path=None)
            events = []
            # Get snapshot
            events.append(next(gen))
            # Get terminal event (should NOT end stream)
            events.append(next(gen))
            # Get normal event (stream continues)
            events.append(next(gen))
            gen.close()

            assert "event: snapshot" in events[0]
            assert "pipeline.completed" in events[1]
            assert "phase.started" in events[2]

    def test_heartbeat_on_idle(self):
        """Test that heartbeat is sent when no events arrive."""
        with patch("unified_sse.get_unified_sse_manager") as mock_mgr, \
             patch("unified_sse._resolve_repo_path") as mock_path, \
             patch("unified_sse.HEARTBEAT_INTERVAL", 0.1):

            # Need to also patch the imported constant
            import unified_sse
            original_interval = unified_sse.HEARTBEAT_INTERVAL

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager
            mock_path.return_value = None

            gen = create_unified_sse_stream(repo_path=None)

            # Get snapshot
            snapshot = next(gen)
            assert "event: snapshot" in snapshot

            # Patch the heartbeat interval to be very short for testing
            with patch.object(unified_sse, "HEARTBEAT_INTERVAL", 0.05):
                # Next call should timeout and yield heartbeat
                heartbeat = next(gen)
                assert heartbeat.startswith(": heartbeat")

            gen.close()

    def test_max_connection_time(self):
        """Test that stream ends after max connection time."""
        with patch("unified_sse.get_unified_sse_manager") as mock_mgr, \
             patch("unified_sse._resolve_repo_path") as mock_path, \
             patch("unified_sse.MAX_CONNECTION_TIME", 0), \
             patch("unified_sse.HEARTBEAT_INTERVAL", 0.01):

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager
            mock_path.return_value = None

            gen = create_unified_sse_stream(repo_path=None)
            events = list(gen)

            # Should have snapshot + done (timeout)
            assert len(events) == 2
            assert "event: snapshot" in events[0]
            assert "event: done" in events[1]
            assert "timeout" in events[1]

    def test_cleanup_on_generator_close(self):
        """Test that client is removed when generator is closed."""
        with patch("unified_sse.get_unified_sse_manager") as mock_mgr, \
             patch("unified_sse._resolve_repo_path") as mock_path:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager
            mock_path.return_value = None

            gen = create_unified_sse_stream(repo_path=None)
            next(gen)  # Get snapshot
            gen.close()

            mock_manager.remove_client.assert_called_once_with(mock_q)

    def test_empty_snapshot_when_no_repo_path(self):
        """Test that an empty snapshot is sent when no repo path available."""
        with patch("unified_sse.get_unified_sse_manager") as mock_mgr, \
             patch("unified_sse._resolve_repo_path") as mock_path:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager
            mock_path.return_value = None

            gen = create_unified_sse_stream(repo_path=None)
            snapshot = next(gen)
            gen.close()

            data_line = [
                l for l in snapshot.split("\n") if l.startswith("data: ")
            ][0]
            data = json.loads(data_line[6:])
            assert data["pipelines"] == []

    def test_snapshot_includes_pipeline_metadata(self):
        """Test that snapshot entries include repo, branch, compact, progress."""
        pipelines = [
            create_test_pipeline(
                "p-1", repo="owner/repo", branch="egg/fix-bug"
            ),
        ]

        with patch("unified_sse.get_unified_sse_manager") as mock_mgr, \
             patch("unified_sse._collect_all_pipelines_safe") as mock_collect, \
             patch("unified_sse.render_compact_status") as mock_compact, \
             patch("unified_sse.render_progress_bar") as mock_progress:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager

            mock_collect.return_value = pipelines
            mock_compact.return_value = "[>Implement] -> oPR"
            mock_progress.return_value = "[####----] 50%"

            gen = create_unified_sse_stream(repo_path=Path("/tmp/test"))
            snapshot = next(gen)
            gen.close()

            data_line = [
                l for l in snapshot.split("\n") if l.startswith("data: ")
            ][0]
            data = json.loads(data_line[6:])
            entry = data["pipelines"][0]
            assert entry["repo"] == "owner/repo"
            assert entry["branch"] == "egg/fix-bug"
            assert entry["compact"] == "[>Implement] -> oPR"
            assert entry["progress"] == "[####----] 50%"


class TestGetUnifiedSSEManager:
    """Tests for get_unified_sse_manager singleton."""

    def test_returns_manager(self):
        """Test singleton returns a manager instance."""
        with patch("unified_sse.UnifiedSSEManager") as MockManager:
            instance = MagicMock()
            MockManager.return_value = instance
            manager = get_unified_sse_manager()
            assert manager is instance
            instance.subscribe_to_events.assert_called_once()

    def test_singleton_reuse(self):
        """Test that subsequent calls return the same instance."""
        with patch("unified_sse.UnifiedSSEManager") as MockManager:
            instance = MagicMock()
            MockManager.return_value = instance

            m1 = get_unified_sse_manager()
            m2 = get_unified_sse_manager()
            assert m1 is m2
            MockManager.assert_called_once()
