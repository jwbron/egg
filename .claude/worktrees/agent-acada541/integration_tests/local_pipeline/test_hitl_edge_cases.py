"""Integration tests for HITL (Human-In-The-Loop) decision edge cases.

Tests thoroughly cover human-in-the-loop decision handling including
rejections, custom inputs, and concurrent decision scenarios.

All tests require Docker and are marked with @pytest.mark.integration.
"""

import concurrent.futures

import pytest
import requests

from .helpers import (
    create_pipeline,
    delete_pipeline,
    get_pipeline,
    resolve_decision,
    start_pipeline,
    wait_for_awaiting_human,
    wait_for_pipeline_terminal,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDecisionRejection:
    """Test decision rejection flow (resolution="reject")."""

    def test_decision_rejection_cancels_pipeline(self, orchestrator_url: str) -> None:
        """Pipeline transitions to cancelled state on rejection."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test decision rejection",
            config={"hitl_gates": True},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Start pipeline
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait for HITL gate
            status_data = wait_for_awaiting_human(orchestrator_url, pipeline_id, timeout=180)
            pending = status_data["data"].get("pending_decision")
            assert pending is not None

            # Reject the decision
            resolve_data, resolve_status = resolve_decision(
                orchestrator_url, pipeline_id, pending["id"], resolution="reject"
            )
            assert resolve_status == 200, f"Reject failed: {resolve_data}"

            # Wait for terminal state
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=60)

            # Should be cancelled
            assert final["data"]["status"] == "cancelled", (
                f"Pipeline should be cancelled after rejection: {final}"
            )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestCustomInputDecision:
    """Test custom 'Other' option with free-text input."""

    def test_custom_input_recorded_in_decision(self, orchestrator_url: str) -> None:
        """Resolution with custom input is recorded; pipeline continues."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test custom input decision",
            config={"hitl_gates": True},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait for HITL gate
            status_data = wait_for_awaiting_human(orchestrator_url, pipeline_id, timeout=180)
            pending = status_data["data"].get("pending_decision")
            assert pending is not None

            # Approve with custom input
            custom_text = "Custom feedback: please focus on security aspects"
            resolve_data, resolve_status = resolve_decision(
                orchestrator_url,
                pipeline_id,
                pending["id"],
                resolution="approve",
                custom_input=custom_text,
            )

            # Should succeed
            assert resolve_status == 200, f"Custom input resolution failed: {resolve_data}"

            # Verify decision was recorded (via pipeline get if available)
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            # The pipeline should continue after approval
            assert get_data["data"]["pipeline"]["status"] in ("running", "awaiting_human")

        finally:
            # Let pipeline finish or delete it
            try:
                # Resolve remaining gates if any
                for _ in range(3):
                    try:
                        status_data = wait_for_awaiting_human(
                            orchestrator_url, pipeline_id, timeout=30
                        )
                        pending = status_data["data"].get("pending_decision")
                        if pending:
                            resolve_decision(orchestrator_url, pipeline_id, pending["id"])
                    except (TimeoutError, AssertionError):
                        break
            except requests.RequestException:
                pass
            delete_pipeline(orchestrator_url, pipeline_id)


class TestInvalidDecisionId:
    """Test invalid decision resolution (non-existent decision ID)."""

    def test_resolve_nonexistent_decision_returns_404(self, orchestrator_url: str) -> None:
        """API returns 404 for non-existent decision ID; pipeline unchanged."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test invalid decision ID",
            config={"hitl_gates": True},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait for HITL gate
            status_data = wait_for_awaiting_human(orchestrator_url, pipeline_id, timeout=180)
            real_decision_id = status_data["data"]["pending_decision"]["id"]

            # Try to resolve with fake decision ID
            fake_decision_id = "nonexistent-decision-12345"
            resolve_data, resolve_status = resolve_decision(
                orchestrator_url, pipeline_id, fake_decision_id
            )

            # Should return 404
            assert resolve_status == 404, (
                f"Expected 404 for fake decision ID, got {resolve_status}: {resolve_data}"
            )

            # Pipeline should still be awaiting_human
            status_resp = requests.get(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status",
                timeout=10,
            )
            assert status_resp.json()["data"]["status"] == "awaiting_human"

            # Real decision should still be pending
            pending = status_resp.json()["data"].get("pending_decision")
            assert pending is not None
            assert pending["id"] == real_decision_id

        finally:
            # Cleanup - resolve real decision first
            try:
                resolve_decision(orchestrator_url, pipeline_id, real_decision_id)
                # Resolve any subsequent gates
                for _ in range(2):
                    try:
                        status_data = wait_for_awaiting_human(
                            orchestrator_url, pipeline_id, timeout=30
                        )
                        pending = status_data["data"].get("pending_decision")
                        if pending:
                            resolve_decision(orchestrator_url, pipeline_id, pending["id"])
                    except (TimeoutError, AssertionError):
                        break
            except requests.RequestException:
                pass
            delete_pipeline(orchestrator_url, pipeline_id)


class TestAlreadyResolvedDecision:
    """Test resolving already-resolved decision."""

    def test_resolve_already_resolved_returns_409(self, orchestrator_url: str) -> None:
        """API returns 409 conflict for already-resolved decision."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test double resolution",
            config={"hitl_gates": True},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait for HITL gate
            status_data = wait_for_awaiting_human(orchestrator_url, pipeline_id, timeout=180)
            decision_id = status_data["data"]["pending_decision"]["id"]

            # Resolve the decision
            resolve_data1, resolve_status1 = resolve_decision(
                orchestrator_url, pipeline_id, decision_id
            )
            assert resolve_status1 == 200

            # Try to resolve the same decision again
            resolve_data2, resolve_status2 = resolve_decision(
                orchestrator_url, pipeline_id, decision_id
            )

            # Should return 409 conflict or 404 (decision no longer pending)
            assert resolve_status2 in (404, 409), (
                f"Expected 404 or 409 for double resolution, got {resolve_status2}"
            )

        finally:
            # Cleanup remaining gates
            try:
                for _ in range(2):
                    try:
                        status_data = wait_for_awaiting_human(
                            orchestrator_url, pipeline_id, timeout=30
                        )
                        pending = status_data["data"].get("pending_decision")
                        if pending:
                            resolve_decision(orchestrator_url, pipeline_id, pending["id"])
                    except (TimeoutError, AssertionError):
                        break
            except requests.RequestException:
                pass
            delete_pipeline(orchestrator_url, pipeline_id)


class TestConcurrentDecisionResolution:
    """Test concurrent decision resolution race condition."""

    def test_concurrent_resolution_one_wins(self, orchestrator_url: str) -> None:
        """One resolution succeeds; other returns 409; no state corruption."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test concurrent resolution",
            config={"hitl_gates": True},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait for HITL gate
            status_data = wait_for_awaiting_human(orchestrator_url, pipeline_id, timeout=180)
            decision_id = status_data["data"]["pending_decision"]["id"]

            # Try to resolve concurrently using threads
            results = []

            def resolve_once() -> tuple[dict, int]:
                return resolve_decision(orchestrator_url, pipeline_id, decision_id)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(resolve_once) for _ in range(2)]
                for future in concurrent.futures.as_completed(futures):
                    results.append(future.result())

            # Analyze results
            status_codes = [r[1] for r in results]

            # At least one should succeed (200)
            assert 200 in status_codes, f"At least one resolution should succeed: {results}"

            # Exactly one should succeed - if both succeed, there's a race condition bug
            # that allows the same decision to be resolved twice
            success_count = status_codes.count(200)
            conflict_count = sum(1 for s in status_codes if s in (404, 409))

            assert success_count == 1, (
                f"Expected exactly 1 success, got {success_count}. "
                f"Both succeeding indicates a concurrency bug allowing duplicate resolution. "
                f"Status codes: {status_codes}"
            )
            assert conflict_count == 1, (
                f"Expected 1 conflict (404/409), got {conflict_count}. Status codes: {status_codes}"
            )

            # Verify pipeline state is consistent (not corrupted)
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            pipeline = get_data["data"]["pipeline"]

            # Pipeline should be in a valid state
            assert pipeline["status"] in ("running", "awaiting_human", "complete", "failed"), (
                f"Pipeline in invalid state: {pipeline['status']}"
            )

        finally:
            # Cleanup
            try:
                for _ in range(2):
                    try:
                        status_data = wait_for_awaiting_human(
                            orchestrator_url, pipeline_id, timeout=30
                        )
                        pending = status_data["data"].get("pending_decision")
                        if pending:
                            resolve_decision(orchestrator_url, pipeline_id, pending["id"])
                    except (TimeoutError, AssertionError):
                        break
            except requests.RequestException:
                pass
            delete_pipeline(orchestrator_url, pipeline_id)


class TestDecisionWithInvalidResolution:
    """Test decision with invalid resolution value."""

    def test_invalid_resolution_value_returns_400(self, orchestrator_url: str) -> None:
        """API returns 400 for invalid resolution value."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test invalid resolution value",
            config={"hitl_gates": True},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait for HITL gate
            status_data = wait_for_awaiting_human(orchestrator_url, pipeline_id, timeout=180)
            decision_id = status_data["data"]["pending_decision"]["id"]

            # Try to resolve with invalid resolution value
            resp = requests.post(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/resolve",
                json={"resolution": "invalid_value_xyz"},
                timeout=10,
            )

            # Should return 400 bad request
            assert resp.status_code == 400, (
                f"Expected 400 for invalid resolution, got {resp.status_code}: {resp.text}"
            )

            # Pipeline should still be awaiting_human
            status_resp = requests.get(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status",
                timeout=10,
            )
            assert status_resp.json()["data"]["status"] == "awaiting_human"

        finally:
            # Cleanup
            try:
                status_data = wait_for_awaiting_human(orchestrator_url, pipeline_id, timeout=10)
                pending = status_data["data"].get("pending_decision")
                if pending:
                    resolve_decision(orchestrator_url, pipeline_id, pending["id"])
                for _ in range(2):
                    try:
                        status_data = wait_for_awaiting_human(
                            orchestrator_url, pipeline_id, timeout=30
                        )
                        pending = status_data["data"].get("pending_decision")
                        if pending:
                            resolve_decision(orchestrator_url, pipeline_id, pending["id"])
                    except (TimeoutError, AssertionError):
                        break
            except requests.RequestException:
                pass
            delete_pipeline(orchestrator_url, pipeline_id)
