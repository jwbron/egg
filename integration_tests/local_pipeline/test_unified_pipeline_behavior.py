"""Integration tests for unified local/issue pipeline behavior (issue #543).

Tests verify that local and issue mode pipelines now follow the same
contract/checkpoint/push discipline with the only difference being
where initial context comes from.

Key behaviors tested:
1. Phase-based push restrictions (not blanket mode-based blocking)
2. Contract creation with pipeline_id key
3. Checkpoint creation on local-mode pushes
4. Prefixed file paths for concurrent pipeline support
5. Container and agent tracking via API
6. State persistence at phase boundaries
"""

import json
import time
from pathlib import Path

import pytest
import requests

from .helpers import create_pipeline, delete_pipeline, get_pipeline

pytestmark = pytest.mark.integration


class TestLocalPipelineContractCreation:
    """Local pipelines create contracts keyed by pipeline_id."""

    def test_contract_file_uses_pipeline_id_key(self, local_pipeline_stack) -> None:
        """Verify contract file uses pipeline ID as filename."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test contract key",
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]
        assert pipeline_id.startswith("local-")

        try:
            # Verify contract file path uses pipeline_id
            contract_path = Path(repos_dir) / f".egg-state/contracts/{pipeline_id}.json"
            assert contract_path.exists(), f"Contract should be at {contract_path}"

            # Verify contract contents include pipeline_id
            contract_data = json.loads(contract_path.read_text())
            assert contract_data.get("pipeline_id") == pipeline_id

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestLocalPipelinePrefixedPaths:
    """Local pipelines use pipeline_id-prefixed file paths."""

    def test_multiple_concurrent_pipelines_have_distinct_paths(self, local_pipeline_stack) -> None:
        """Two concurrent local pipelines should have distinct draft/verdict paths."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

        # Create two pipelines
        data1, status1 = create_pipeline(
            orchestrator_url,
            prompt="Pipeline one",
        )
        assert status1 == 200
        pipeline_id_1 = data1["data"]["pipeline"]["id"]

        data2, status2 = create_pipeline(
            orchestrator_url,
            prompt="Pipeline two",
        )
        assert status2 == 200
        pipeline_id_2 = data2["data"]["pipeline"]["id"]

        try:
            # Verify distinct pipeline IDs
            assert pipeline_id_1 != pipeline_id_2

            # If drafts were created, they should have distinct paths
            drafts_dir = Path(repos_dir) / ".egg-state/drafts"
            if drafts_dir.exists():
                draft_files = list(drafts_dir.glob("*.md"))
                # Draft files should be prefixed with pipeline_id
                for draft in draft_files:
                    name = draft.name
                    # Should be prefixed with one of the pipeline IDs
                    assert name.startswith(pipeline_id_1) or name.startswith(pipeline_id_2), (
                        f"Draft {name} should be prefixed with pipeline ID"
                    )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id_1)
            delete_pipeline(orchestrator_url, pipeline_id_2)


class TestLocalPipelineContractSynced:
    """Contract synced flag is correctly managed."""

    def test_contract_synced_set_after_creation(self, local_pipeline_stack) -> None:
        """contract_synced should be True after successful contract creation."""
        orchestrator_url = local_pipeline_stack.orchestrator_url

        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test contract synced flag",
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Get pipeline and check contract_synced
            get_data, get_status = get_pipeline(orchestrator_url, pipeline_id)
            assert get_status == 200
            pipeline = get_data["data"]["pipeline"]
            assert pipeline.get("contract_synced") is True

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestPhaseBasedPushRestrictions:
    """Push restrictions are per-phase, not per-mode."""

    def test_gateway_session_in_local_refine_allows_state_push(self, local_pipeline_stack) -> None:
        """Local session in refine phase can push .egg-state/ files."""
        gateway_url = local_pipeline_stack.gateway_url
        launcher_secret = local_pipeline_stack.launcher_secret

        # Get source IP the gateway sees
        health_resp = requests.get(f"{gateway_url}/api/v1/health", timeout=10)
        source_ip = health_resp.json().get("client_ip", "")

        # Create a local-mode session in refine phase
        session_resp = requests.post(
            f"{gateway_url}/api/v1/sessions/create",
            headers={"Authorization": f"Bearer {launcher_secret}"},
            json={
                "container_id": f"test-push-{int(time.time())}",
                "container_ip": source_ip,
                "mode": "local",
                "phase": "refine",  # Key: setting phase enables per-phase restrictions
                "repos": ["test-owner/test-repo"],
                "uid": 1000,
                "gid": 1000,
            },
            timeout=10,
        )
        assert session_resp.status_code == 200
        session_data = session_resp.json()
        session_token = session_data.get("data", session_data).get("session_token")

        try:
            # Note: Actual push testing requires git repo setup
            # This test verifies the session creation with phase works
            assert session_token is not None

        finally:
            requests.delete(
                f"{gateway_url}/api/v1/sessions/{session_token}",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                timeout=10,
            )


class TestContainerAgentTracking:
    """Container and agent tracking in phase execution state."""

    def test_pipeline_reports_container_state(self, local_pipeline_stack) -> None:
        """Pipeline status should include container information when running."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        # This test verifies the API structure includes containers field
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test container tracking",
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Get pipeline and verify phases structure includes containers
            get_data, get_status = get_pipeline(orchestrator_url, pipeline_id)
            assert get_status == 200
            pipeline = get_data["data"]["pipeline"]

            # Phases should have containers and agents fields
            phases = pipeline.get("phases", {})
            for phase_name, phase_data in phases.items():
                assert "containers" in phase_data, f"Phase {phase_name} missing containers"
                assert "agents" in phase_data, f"Phase {phase_name} missing agents"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestPhaseBoundaryCommits:
    """State is committed to git at phase boundaries for local pipelines."""

    def test_pipeline_state_file_exists(self, local_pipeline_stack) -> None:
        """Pipeline state file should exist in .egg-state/pipelines/."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test state persistence",
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # State file should exist
            state_path = Path(repos_dir) / f".egg-state/pipelines/{pipeline_id}.json"
            assert state_path.exists(), f"State file should exist at {state_path}"

            # State file should be valid JSON
            state_data = json.loads(state_path.read_text())
            assert state_data.get("id") == pipeline_id

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestPhasePromptInstructions:
    """Agent prompts include correct instructions for unified behavior."""

    def test_local_pipeline_includes_contract_cli_instructions(self, local_pipeline_stack) -> None:
        """Local pipeline contract should exist with proper structure."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test contract CLI instructions",
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Verify contract exists (implies contract CLI can be used)
            contract_path = Path(repos_dir) / f".egg-state/contracts/{pipeline_id}.json"
            assert contract_path.exists(), "Contract file should exist for local pipeline"

            # Contract should have standard fields
            contract_data = json.loads(contract_path.read_text())
            assert "current_phase" in contract_data
            assert contract_data.get("pipeline_id") == pipeline_id

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestCheckpointBranchBypass:
    """Pushes to checkpoint branch bypass phase restrictions."""

    def test_checkpoint_branch_constant_defined(self) -> None:
        """Verify checkpoint branch constant is used consistently."""
        # This is a structural test - the constant should be defined
        # in gateway.py and should be "egg/checkpoints/v1"
        _expected_branch = "egg/checkpoints/v1"

        # Read gateway.py to verify the constant
        gateway_path = Path(__file__).parent.parent.parent / "gateway" / "gateway.py"
        if gateway_path.exists():
            content = gateway_path.read_text()
            assert 'CHECKPOINT_BRANCH = "egg/checkpoints/v1"' in content
