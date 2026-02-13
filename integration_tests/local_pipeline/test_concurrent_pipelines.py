"""Integration tests for concurrent pipeline execution.

Tests verify that multiple pipelines can run simultaneously without
interference and maintain proper isolation of state files, contracts,
and draft outputs.

All tests require Docker and are marked with @pytest.mark.integration.
"""

import concurrent.futures
import json
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
    """Pipeline ID isolation in contract files."""

    def test_contract_files_contain_only_own_pipeline_id(self, local_pipeline_stack) -> None:
        """Each pipeline's contract file contains only its own pipeline_id."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

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
            # Verify contract files exist and are isolated
            contract_path_1 = Path(repos_dir) / f".egg-state/contracts/{pipeline_id_1}.json"
            contract_path_2 = Path(repos_dir) / f".egg-state/contracts/{pipeline_id_2}.json"

            assert contract_path_1.exists(), f"Contract 1 should exist at {contract_path_1}"
            assert contract_path_2.exists(), f"Contract 2 should exist at {contract_path_2}"

            # Read and verify contents
            contract1 = json.loads(contract_path_1.read_text())
            contract2 = json.loads(contract_path_2.read_text())

            # Each contract should only reference its own pipeline ID
            assert contract1.get("pipeline_id") == pipeline_id_1
            assert contract2.get("pipeline_id") == pipeline_id_2

            # Contract 1 should NOT contain pipeline 2's ID anywhere
            contract1_text = contract_path_1.read_text()
            assert pipeline_id_2 not in contract1_text, (
                "Contract 1 should not reference pipeline 2's ID"
            )

            # Contract 2 should NOT contain pipeline 1's ID anywhere
            contract2_text = contract_path_2.read_text()
            assert pipeline_id_1 not in contract2_text, (
                "Contract 2 should not reference pipeline 1's ID"
            )

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

    def test_concurrent_pipelines_have_distinct_state_files(self, local_pipeline_stack) -> None:
        """Each pipeline has its own state file in .egg-state/pipelines/."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

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
            # State files should exist for both
            state_path_1 = Path(repos_dir) / f".egg-state/pipelines/{pipeline_id_1}.json"
            state_path_2 = Path(repos_dir) / f".egg-state/pipelines/{pipeline_id_2}.json"

            assert state_path_1.exists(), f"State file 1 should exist at {state_path_1}"
            assert state_path_2.exists(), f"State file 2 should exist at {state_path_2}"

            # Verify contents are isolated
            state1 = json.loads(state_path_1.read_text())
            state2 = json.loads(state_path_2.read_text())

            assert state1.get("id") == pipeline_id_1
            assert state2.get("id") == pipeline_id_2

            # Verify no cross-contamination
            state1_text = state_path_1.read_text()
            state2_text = state_path_2.read_text()

            assert pipeline_id_2 not in state1_text
            assert pipeline_id_1 not in state2_text

        finally:
            delete_pipeline(orchestrator_url, pipeline_id_1)
            delete_pipeline(orchestrator_url, pipeline_id_2)
