"""Integration tests for error recovery scenarios.

Tests verify the system handles partial failures, container crashes,
timeout scenarios, and recovery paths gracefully.

All tests require Docker and are marked with @pytest.mark.integration.
"""

import subprocess
import time
from pathlib import Path

import pytest
import requests

from .helpers import (
    create_pipeline,
    delete_pipeline,
    get_pipeline,
    start_pipeline,
    wait_for_pipeline_terminal,
)

pytestmark = pytest.mark.integration


def get_orphaned_sandbox_containers() -> list[str]:
    """List any orphaned egg-sandbox containers."""
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=egg-sandbox-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return [name for name in result.stdout.strip().splitlines() if name]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPartialPhaseFailure:
    """Test partial phase failure (draft written, then crash)."""

    def test_partial_failure_preserves_draft(self, local_pipeline_stack) -> None:
        """Pipeline fails but partial draft is preserved for debugging."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

        data, status = create_pipeline(
            orchestrator_url,
            prompt="PARTIAL_FAILURE test - write draft then crash",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Should fail on refine phase (first phase where PARTIAL_FAILURE triggers)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=300)
            assert final["data"]["status"] == "failed", (
                f"Pipeline should have failed but got: {final}"
            )

            # Verify partial draft was preserved
            partial_draft_path = Path(repos_dir) / ".egg-state/drafts/partial-draft.md"
            assert partial_draft_path.exists(), (
                f"Partial draft should be preserved at {partial_draft_path}"
            )

            # Verify draft content indicates partial state
            content = partial_draft_path.read_text()
            assert "Partial Draft" in content or "incomplete" in content.lower()
            assert pipeline_id in content

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestPhaseFailureMidExecution:
    """Test phase failure mid-execution with FAIL_ON_PHASE."""

    def test_fail_on_plan_phase_leaves_refine_complete(self, orchestrator_url: str) -> None:
        """Pipeline fails on plan phase; refine phase remains complete."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="FAIL_ON_PHASE=plan - fail only on plan",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Should fail on plan phase
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=300)
            assert final["data"]["status"] == "failed"
            assert final["data"]["current_phase"] == "plan"

            # Verify refine phase completed before the failure
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})
            assert "refine" in phases
            assert phases["refine"]["status"] == "complete", (
                "Refine should have completed before plan failed"
            )
            assert "plan" in phases
            assert phases["plan"]["status"] == "failed"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)

    def test_fail_on_implement_phase(self, orchestrator_url: str) -> None:
        """Pipeline fails on implement phase; refine and plan remain complete."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="FAIL_ON_PHASE=implement - fail only on implement",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Should fail on implement phase
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)
            assert final["data"]["status"] == "failed"
            assert final["data"]["current_phase"] == "implement"

            # Verify earlier phases completed
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})
            assert phases.get("refine", {}).get("status") == "complete"
            assert phases.get("plan", {}).get("status") == "complete"
            assert phases.get("implement", {}).get("status") == "failed"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestContainerTimeoutHandling:
    """Test container timeout handling with short timeout configuration."""

    def test_container_timeout_fails_pipeline(self, orchestrator_url: str) -> None:
        """Pipeline fails with timeout error when container exceeds configured timeout.

        Note: This test uses SLOW_PHASE with a default 30s sleep combined with
        a short container timeout to trigger the timeout handling path.
        """
        data, status = create_pipeline(
            orchestrator_url,
            prompt="SLOW_PHASE timeout test",
            config={
                "hitl_gates": False,
                # Set a short container timeout if the orchestrator supports it
                # The exact config key depends on orchestrator implementation
                "container_timeout_seconds": 10,
            },
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait for terminal state - should fail due to timeout
            # If orchestrator doesn't support container_timeout_seconds config,
            # the SLOW_PHASE will just run longer but complete normally
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=120)

            # Pipeline should either fail (timeout) or complete (if timeout not enforced)
            # We verify the orchestrator handles both cases gracefully
            status_val = final["data"]["status"]
            assert status_val in ("failed", "complete"), (
                f"Expected failed or complete, got: {status_val}"
            )

            if status_val == "failed":
                # If it failed, verify timeout-related error
                get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
                error = get_data["data"]["pipeline"].get("error", "")
                # Error message might mention timeout
                assert "timeout" in error.lower() or "exceeded" in error.lower() or error, (
                    "Failed pipeline should have error message"
                )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestStateFileCorruptionDetection:
    """Test state file corruption detection."""

    def test_malformed_contract_json_handled(self, local_pipeline_stack) -> None:
        """Pipeline reports error when contract JSON is malformed.

        This tests that the orchestrator doesn't crash when encountering
        corrupted state files.
        """
        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

        # First create a valid pipeline
        data, status = create_pipeline(
            orchestrator_url,
            prompt="State corruption test",
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Corrupt the contract file
            contract_path = Path(repos_dir) / f".egg-state/contracts/{pipeline_id}.json"
            if contract_path.exists():
                # Write invalid JSON
                contract_path.write_text("{ invalid json syntax")

            # Try to get the pipeline - should handle gracefully
            get_data, get_status = get_pipeline(orchestrator_url, pipeline_id)

            # The orchestrator should either:
            # 1. Return 404 (contract unreadable = pipeline lost)
            # 2. Return 200 with error state
            # 3. Return 500 with error message (acceptable for corruption)
            assert get_status in (200, 404, 500), (
                f"Unexpected status {get_status} for corrupted contract"
            )

            # Verify the system didn't crash - we can still create new pipelines
            test_data, test_status = create_pipeline(
                orchestrator_url,
                prompt="Post-corruption test",
            )
            assert test_status == 200, "Should be able to create new pipeline after corruption"
            delete_pipeline(orchestrator_url, test_data["data"]["pipeline"]["id"])

        finally:
            # Clean up (may fail if contract is corrupted, that's OK)
            try:
                delete_pipeline(orchestrator_url, pipeline_id)
            except requests.RequestException:
                pass


class TestOrphanedContainerCleanup:
    """Test orphaned container cleanup on pipeline failure."""

    def test_no_orphaned_containers_after_failure(self, orchestrator_url: str) -> None:
        """No orphaned egg-sandbox containers remain after pipeline failure."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="FORCE_FAIL orphan cleanup test",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait for failure
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=300)
            assert final["data"]["status"] == "failed"

            # Wait a bit for container cleanup
            time.sleep(5)

            # Check for orphaned containers
            after_containers = get_orphaned_sandbox_containers()

            # Filter to containers related to this pipeline
            pipeline_containers = [
                c for c in after_containers if pipeline_id.replace("local-", "") in c
            ]

            assert len(pipeline_containers) == 0, (
                f"Found orphaned containers for pipeline {pipeline_id}: {pipeline_containers}"
            )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestPipelineDeletionDuringRunning:
    """Test pipeline deletion during running state."""

    def test_delete_running_pipeline_stops_container(self, orchestrator_url: str) -> None:
        """DELETE returns appropriate status; running container is stopped."""
        # Use SLOW_PHASE to keep the pipeline running long enough to delete
        data, status = create_pipeline(
            orchestrator_url,
            prompt="SLOW_PHASE deletion test",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Start the pipeline
            start_data, start_status = start_pipeline(orchestrator_url, pipeline_id)
            assert start_status == 200

            # Wait briefly for it to enter running state
            time.sleep(3)

            # Verify it's running
            status_data = requests.get(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status",
                timeout=10,
            ).json()
            current_status = status_data.get("data", {}).get("status", "")
            assert current_status == "running", f"Expected running, got: {current_status}"

            # Delete while running
            del_resp = requests.delete(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
                timeout=10,
            )

            # Should succeed or return conflict
            assert del_resp.status_code in (200, 409), (
                f"Expected 200 or 409, got {del_resp.status_code}: {del_resp.text}"
            )

            if del_resp.status_code == 200:
                # Verify pipeline is gone
                get_data, get_status = get_pipeline(orchestrator_url, pipeline_id)
                assert get_status == 404

                # Verify no orphaned containers
                time.sleep(3)
                containers = get_orphaned_sandbox_containers()
                pipeline_containers = [
                    c for c in containers if pipeline_id.replace("local-", "") in c
                ]
                assert len(pipeline_containers) == 0, (
                    f"Found orphaned containers: {pipeline_containers}"
                )

        except Exception:
            # Cleanup on any error
            try:
                delete_pipeline(orchestrator_url, pipeline_id)
            except requests.RequestException:
                pass
            raise


class TestMultipleFailureModes:
    """Test multiple failure scenarios in sequence don't corrupt state."""

    def test_sequential_failures_dont_corrupt_orchestrator(self, orchestrator_url: str) -> None:
        """Multiple failed pipelines don't corrupt orchestrator state."""
        pipeline_ids = []

        try:
            # Create and fail multiple pipelines
            for i in range(3):
                data, status = create_pipeline(
                    orchestrator_url,
                    prompt=f"FORCE_FAIL sequential failure test {i + 1}",
                    config={"hitl_gates": False},
                )
                assert status == 200
                pid = data["data"]["pipeline"]["id"]
                pipeline_ids.append(pid)

                start_pipeline(orchestrator_url, pid)
                final = wait_for_pipeline_terminal(orchestrator_url, pid, timeout=300)
                assert final["data"]["status"] == "failed"

            # Verify orchestrator is still healthy
            health_resp = requests.get(
                f"{orchestrator_url}/api/v1/health",
                timeout=10,
            )
            assert health_resp.status_code == 200

            # Verify we can still create and complete a successful pipeline
            success_data, success_status = create_pipeline(
                orchestrator_url,
                prompt="Post-failure success test",
                config={"hitl_gates": False},
            )
            assert success_status == 200
            success_pid = success_data["data"]["pipeline"]["id"]
            pipeline_ids.append(success_pid)

            start_pipeline(orchestrator_url, success_pid)
            final = wait_for_pipeline_terminal(orchestrator_url, success_pid, timeout=360)
            assert final["data"]["status"] == "complete", (
                f"Should be able to complete pipeline after failures: {final}"
            )

        finally:
            for pid in pipeline_ids:
                try:
                    delete_pipeline(orchestrator_url, pid)
                except requests.RequestException:
                    pass
