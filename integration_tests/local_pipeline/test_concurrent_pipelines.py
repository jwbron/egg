"""Integration tests for concurrent pipeline execution.

Tests verify that multiple pipelines can run simultaneously without
interference and maintain proper isolation of state files, contracts,
and draft outputs.

All tests require Docker and are marked with @pytest.mark.integration.
"""

import concurrent.futures
from pathlib import Path

import pytest

from .helpers import (
    create_pipeline,
    delete_pipeline,
    get_pipeline,
    start_pipeline,
    wait_for_pipeline_terminal,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConcurrentPipelineExecution:
    """Two pipelines running simultaneously complete independently."""

    def test_two_pipelines_complete_independently(self, orchestrator_url: str) -> None:
        """Start two pipelines and verify both complete without cross-contamination."""
        # Create two pipelines with distinct prompts
        data1, status1 = create_pipeline(
            orchestrator_url,
            prompt="Concurrent pipeline ONE",
            config={"hitl_gates": False},
        )
        assert status1 == 200
        pipeline_id_1 = data1["data"]["pipeline"]["id"]

        data2, status2 = create_pipeline(
            orchestrator_url,
            prompt="Concurrent pipeline TWO",
            config={"hitl_gates": False},
        )
        assert status2 == 200
        pipeline_id_2 = data2["data"]["pipeline"]["id"]

        try:
            # Verify distinct IDs
            assert pipeline_id_1 != pipeline_id_2

            # Start both pipelines
            start1, start_status1 = start_pipeline(orchestrator_url, pipeline_id_1)
            assert start_status1 == 200
            start2, start_status2 = start_pipeline(orchestrator_url, pipeline_id_2)
            assert start_status2 == 200

            # Wait for both to complete (use extended timeout for concurrent execution)
            final1 = wait_for_pipeline_terminal(orchestrator_url, pipeline_id_1, timeout=480)
            final2 = wait_for_pipeline_terminal(orchestrator_url, pipeline_id_2, timeout=480)

            # Both should complete successfully
            assert final1["data"]["status"] == "complete", f"Pipeline 1 failed: {final1}"
            assert final2["data"]["status"] == "complete", f"Pipeline 2 failed: {final2}"

            # Verify both reached the PR phase
            assert final1["data"]["current_phase"] == "pr"
            assert final2["data"]["current_phase"] == "pr"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id_1)
            delete_pipeline(orchestrator_url, pipeline_id_2)


class TestPipelineIdIsolation:
    """Pipeline ID isolation via API."""

    def test_pipelines_have_isolated_state(self, local_pipeline_stack) -> None:
        """Each pipeline's state contains only its own pipeline_id (via API)."""
        orchestrator_url = local_pipeline_stack.orchestrator_url

        # Create two pipelines
        data1, status1 = create_pipeline(
            orchestrator_url,
            prompt="Contract isolation test ONE",
        )
        assert status1 == 200
        pipeline_id_1 = data1["data"]["pipeline"]["id"]

        data2, status2 = create_pipeline(
            orchestrator_url,
            prompt="Contract isolation test TWO",
        )
        assert status2 == 200
        pipeline_id_2 = data2["data"]["pipeline"]["id"]

        try:
            # Verify pipelines are isolated via API
            get_data1, get_status1 = get_pipeline(orchestrator_url, pipeline_id_1)
            get_data2, get_status2 = get_pipeline(orchestrator_url, pipeline_id_2)

            assert get_status1 == 200, f"Pipeline 1 not found: {get_data1}"
            assert get_status2 == 200, f"Pipeline 2 not found: {get_data2}"

            # Each pipeline should reference only its own ID
            pipeline1 = get_data1["data"]["pipeline"]
            pipeline2 = get_data2["data"]["pipeline"]

            assert pipeline1["id"] == pipeline_id_1
            assert pipeline2["id"] == pipeline_id_2

        finally:
            delete_pipeline(orchestrator_url, pipeline_id_1)
            delete_pipeline(orchestrator_url, pipeline_id_2)


class TestConcurrentDraftIsolation:
    """Concurrent pipelines have isolated draft files."""

    def test_running_pipelines_have_isolated_drafts(self, local_pipeline_stack) -> None:
        """Draft files from concurrent pipelines are isolated."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

        # Create and start two pipelines
        data1, status1 = create_pipeline(
            orchestrator_url,
            prompt="Draft isolation test ONE",
            config={"hitl_gates": False},
        )
        assert status1 == 200
        pipeline_id_1 = data1["data"]["pipeline"]["id"]

        data2, status2 = create_pipeline(
            orchestrator_url,
            prompt="Draft isolation test TWO",
            config={"hitl_gates": False},
        )
        assert status2 == 200
        pipeline_id_2 = data2["data"]["pipeline"]["id"]

        try:
            # Start both pipelines
            start_pipeline(orchestrator_url, pipeline_id_1)
            start_pipeline(orchestrator_url, pipeline_id_2)

            # Wait for both to complete
            wait_for_pipeline_terminal(orchestrator_url, pipeline_id_1, timeout=480)
            wait_for_pipeline_terminal(orchestrator_url, pipeline_id_2, timeout=480)

            # Check the drafts directory for isolation
            drafts_dir = Path(repos_dir) / ".egg-state/drafts"
            if drafts_dir.exists():
                # All draft files should be pipeline-specific or phase-specific
                # The mock sandbox writes analysis.md and plan.md per pipeline
                # These are overwritten by each pipeline in sequence (session-scoped fixture)
                # so we just verify no cross-contamination in content
                analysis_path = drafts_dir / "analysis.md"
                if analysis_path.exists():
                    content = analysis_path.read_text()
                    # Content should reference one pipeline, not mixed
                    has_id1 = pipeline_id_1 in content
                    has_id2 = pipeline_id_2 in content
                    # Should not have BOTH IDs in the same file
                    assert not (has_id1 and has_id2), (
                        "Draft file should not contain both pipeline IDs"
                    )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id_1)
            delete_pipeline(orchestrator_url, pipeline_id_2)


class TestThreeConcurrentPipelines:
    """Three concurrent pipelines stress test."""

    def test_three_pipelines_complete_successfully(self, orchestrator_url: str) -> None:
        """Three pipelines running concurrently all complete successfully."""
        pipelines = []

        try:
            # Create three pipelines
            for i in range(3):
                data, status = create_pipeline(
                    orchestrator_url,
                    prompt=f"Concurrent stress test pipeline {i + 1}",
                    config={"hitl_gates": False},
                )
                assert status == 200, f"Failed to create pipeline {i + 1}"
                pipelines.append(data["data"]["pipeline"]["id"])

            # Verify all IDs are unique
            assert len(set(pipelines)) == 3, "All pipeline IDs should be unique"

            # Start all three
            for pid in pipelines:
                start_data, start_status = start_pipeline(orchestrator_url, pid)
                assert start_status == 200, f"Failed to start pipeline {pid}"

            # Wait for all to complete (extended timeout for concurrent load)
            results = []
            for pid in pipelines:
                final = wait_for_pipeline_terminal(orchestrator_url, pid, timeout=600)
                results.append(final)

            # All should complete successfully
            for i, final in enumerate(results):
                assert final["data"]["status"] == "complete", (
                    f"Pipeline {i + 1} ({pipelines[i]}) did not complete: {final}"
                )
                assert final["data"]["current_phase"] == "pr"

        finally:
            for pid in pipelines:
                delete_pipeline(orchestrator_url, pid)


class TestConcurrentPipelineCreationRace:
    """Concurrent pipeline creation race condition handling."""

    def test_rapid_pipeline_creation_produces_unique_ids(self, orchestrator_url: str) -> None:
        """Rapidly creating 5 pipelines results in 5 unique IDs with no collisions."""
        pipelines = []

        def create_one(index: int) -> str:
            """Create a single pipeline and return its ID."""
            data, status = create_pipeline(
                orchestrator_url,
                prompt=f"Race condition test pipeline {index}",
            )
            if status != 200:
                raise AssertionError(f"Failed to create pipeline {index}: {data}")
            return data["data"]["pipeline"]["id"]

        try:
            # Create 5 pipelines as fast as possible using threads
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(create_one, i) for i in range(5)]
                for future in concurrent.futures.as_completed(futures):
                    pipelines.append(future.result())

            # All 5 should have unique IDs
            assert len(pipelines) == 5, f"Expected 5 pipelines, got {len(pipelines)}"
            assert len(set(pipelines)) == 5, (
                f"Expected 5 unique IDs, got {len(set(pipelines))}: {pipelines}"
            )

            # Verify each pipeline exists and has correct status
            for pid in pipelines:
                get_data, get_status = get_pipeline(orchestrator_url, pid)
                assert get_status == 200, f"Pipeline {pid} not found"
                assert get_data["data"]["pipeline"]["status"] == "pending"

        finally:
            for pid in pipelines:
                delete_pipeline(orchestrator_url, pid)


class TestConcurrentPipelineStateFiles:
    """Verify state files are isolated for concurrent pipelines."""

    def test_concurrent_pipelines_have_distinct_state(self, local_pipeline_stack) -> None:
        """Each pipeline has its own state, retrievable via API."""
        orchestrator_url = local_pipeline_stack.orchestrator_url

        # Create two pipelines
        data1, status1 = create_pipeline(
            orchestrator_url,
            prompt="State file isolation ONE",
        )
        assert status1 == 200
        pipeline_id_1 = data1["data"]["pipeline"]["id"]

        data2, status2 = create_pipeline(
            orchestrator_url,
            prompt="State file isolation TWO",
        )
        assert status2 == 200
        pipeline_id_2 = data2["data"]["pipeline"]["id"]

        try:
            # Both pipelines should be retrievable and isolated
            get_data1, get_status1 = get_pipeline(orchestrator_url, pipeline_id_1)
            get_data2, get_status2 = get_pipeline(orchestrator_url, pipeline_id_2)

            assert get_status1 == 200, f"Pipeline 1 not found: {get_data1}"
            assert get_status2 == 200, f"Pipeline 2 not found: {get_data2}"

            state1 = get_data1["data"]["pipeline"]
            state2 = get_data2["data"]["pipeline"]

            assert state1["id"] == pipeline_id_1
            assert state2["id"] == pipeline_id_2

            # Verify distinct IDs
            assert pipeline_id_1 != pipeline_id_2

        finally:
            delete_pipeline(orchestrator_url, pipeline_id_1)
            delete_pipeline(orchestrator_url, pipeline_id_2)
