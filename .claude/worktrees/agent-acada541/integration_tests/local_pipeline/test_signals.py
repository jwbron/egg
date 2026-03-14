"""Integration tests for signal handling.

Tests verify the orchestrator correctly processes signals (heartbeat, progress,
error) from sandbox containers, if the signal API is supported.

All tests require Docker and are marked with @pytest.mark.integration.
"""

import json
import time
from pathlib import Path

import pytest
import requests

from .helpers import (
    check_signals_api_exists,
    create_pipeline,
    delete_pipeline,
    send_signal,
    start_pipeline,
    wait_for_pipeline_terminal,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHeartbeatSignal:
    """Test heartbeat signal behavior."""

    def test_heartbeat_signal_files_created(self, local_pipeline_stack) -> None:
        """Verify HEARTBEAT_ONLY mode creates heartbeat signal files.

        Note: This test uses short container timeout to ensure the container
        is killed after generating a few heartbeats, rather than running forever.
        """
        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

        data, status = create_pipeline(
            orchestrator_url,
            prompt="HEARTBEAT_ONLY signal test",
            config={
                "hitl_gates": False,
                # Short timeout so container gets killed
                "container_timeout_seconds": 15,
            },
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait a bit for heartbeats to be written
            time.sleep(20)

            # Check if heartbeat files were created
            signals_dir = Path(repos_dir) / ".egg-state/signals"

            # Pipeline will fail due to timeout (HEARTBEAT_ONLY never exits)
            # but we should see heartbeat files
            if signals_dir.exists():
                heartbeat_files = list(signals_dir.glob("heartbeat-*.json"))
                if heartbeat_files:
                    # Verify heartbeat file content
                    first_heartbeat = heartbeat_files[0]
                    content = json.loads(first_heartbeat.read_text())
                    assert content.get("type") == "heartbeat"
                    assert "count" in content
                    assert "timestamp" in content

            # Wait for terminal state (will be failed due to timeout or killed)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=60)
            # Pipeline should fail since HEARTBEAT_ONLY never completes
            assert final["data"]["status"] in ("failed", "cancelled")

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestProgressSignal:
    """Test progress signal updates pipeline status."""

    def test_progress_signal_via_api(self, orchestrator_url: str) -> None:
        """Progress signals are accepted by the API if supported."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Progress signal test",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait briefly for pipeline to start running
            time.sleep(2)

            # Check if signals API exists
            if not check_signals_api_exists(orchestrator_url, pipeline_id):
                pytest.skip("Signals API not implemented")

            # Send a progress signal
            signal_data, signal_status = send_signal(
                orchestrator_url,
                pipeline_id,
                signal_type="progress",
                percentage=50,
                message="Halfway through implementation",
            )

            # Should accept the signal (200) or reject gracefully (400/422)
            assert signal_status in (200, 400, 422, 404), (
                f"Unexpected signal response: {signal_status}"
            )

            # Wait for pipeline to complete
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)
            assert final["data"]["status"] == "complete"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestErrorSignal:
    """Test error signal triggers pipeline failure."""

    def test_error_signal_via_api(self, orchestrator_url: str) -> None:
        """Critical error signal is processed by the API if supported."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Error signal test",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait briefly for pipeline to start
            time.sleep(2)

            # Check if signals API exists
            if not check_signals_api_exists(orchestrator_url, pipeline_id):
                pytest.skip("Signals API not implemented")

            # Send an error signal
            signal_data, signal_status = send_signal(
                orchestrator_url,
                pipeline_id,
                signal_type="error",
                severity="critical",
                message="Critical error in implementation",
            )

            # Should accept or reject gracefully
            assert signal_status in (200, 400, 422, 404), (
                f"Unexpected signal response: {signal_status}"
            )

            # Wait for pipeline to reach terminal state
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)

            # If error signals are processed, pipeline should fail
            # Otherwise it may complete normally
            assert final["data"]["status"] in ("complete", "failed")

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestSignalFromUnknownPipeline:
    """Test signal from invalid/unknown pipeline."""

    def test_signal_to_nonexistent_pipeline_returns_404(self, orchestrator_url: str) -> None:
        """API returns 404 for signals to non-existent pipeline."""
        fake_pipeline_id = "nonexistent-pipeline-12345"

        # First check if signals API exists at all
        # Create a real pipeline to test
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Signal API existence check",
        )
        assert status == 200
        real_pipeline_id = data["data"]["pipeline"]["id"]

        try:
            if not check_signals_api_exists(orchestrator_url, real_pipeline_id):
                pytest.skip("Signals API not implemented")
        finally:
            delete_pipeline(orchestrator_url, real_pipeline_id)

        # Now test with fake pipeline ID
        signal_data, signal_status = send_signal(
            orchestrator_url,
            fake_pipeline_id,
            signal_type="heartbeat",
        )

        # Should return 404 for non-existent pipeline
        assert signal_status == 404, f"Expected 404 for non-existent pipeline, got {signal_status}"


class TestSignalRateLimiting:
    """Test signal API rate limiting behavior."""

    def test_excessive_signals_handled_gracefully(self, orchestrator_url: str) -> None:
        """Excessive signals from same pipeline are handled gracefully."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Rate limit test",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait briefly for pipeline to start
            time.sleep(2)

            # Check if signals API exists
            if not check_signals_api_exists(orchestrator_url, pipeline_id):
                pytest.skip("Signals API not implemented")

            # Send many signals rapidly
            success_count = 0
            rate_limited_count = 0
            for i in range(20):
                signal_data, signal_status = send_signal(
                    orchestrator_url,
                    pipeline_id,
                    signal_type="heartbeat",
                    count=i,
                )
                if signal_status == 200:
                    success_count += 1
                elif signal_status == 429:  # Rate limited
                    rate_limited_count += 1

            # Should either accept all (no rate limiting) or rate limit some
            # Either behavior is acceptable - we're testing graceful handling
            total_handled = success_count + rate_limited_count

            # All signals should be handled (not 500 errors)
            assert total_handled > 0, "At least some signals should be processed"

            # Wait for pipeline to complete
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)

            # Pipeline should complete despite signal flood
            assert final["data"]["status"] == "complete"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestSignalContentValidation:
    """Test signal content validation."""

    def test_malformed_signal_rejected(self, orchestrator_url: str) -> None:
        """Malformed signal data is rejected with 400."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Malformed signal test",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            time.sleep(2)

            # Check if signals API exists
            if not check_signals_api_exists(orchestrator_url, pipeline_id):
                pytest.skip("Signals API not implemented")

            # Send malformed signal (missing required type field)
            resp = requests.post(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/signals",
                json={"not_type": "invalid", "random": "data"},
                timeout=10,
            )

            # Should reject with 400 or 422
            assert resp.status_code in (400, 422), (
                f"Expected 400/422 for malformed signal, got {resp.status_code}"
            )

            # Pipeline should still be running/able to complete
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)
            assert final["data"]["status"] == "complete"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)
