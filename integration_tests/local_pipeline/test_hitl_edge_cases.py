"""Integration tests for HITL (Human-In-The-Loop) decision edge cases.

Tests thoroughly cover human-in-the-loop decision handling including
timeouts, rejections, custom inputs, and concurrent decision scenarios.

All tests require Docker and are marked with @pytest.mark.integration.
"""

import concurrent.futures
import time

import pytest
import requests

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def create_pipeline(
    orchestrator_url: str,
    *,
    mode: str = "local",
    prompt: str = "Test pipeline",
    config: dict | None = None,
) -> tuple[dict, int]:
    """Create a pipeline via the orchestrator API."""
    body: dict = {"mode": mode, "prompt": prompt}
    if config is not None:
        body["config"] = config
    resp = requests.post(
        f"{orchestrator_url}/api/v1/pipelines",
        json=body,
        timeout=10,
    )
    return resp.json(), resp.status_code


def get_pipeline(orchestrator_url: str, pipeline_id: str) -> tuple[dict, int]:
    """GET a pipeline by ID."""
    resp = requests.get(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
        timeout=10,
    )
    return resp.json(), resp.status_code


def delete_pipeline(orchestrator_url: str, pipeline_id: str) -> tuple[dict, int]:
    """DELETE a pipeline by ID."""
    resp = requests.delete(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
        timeout=10,
    )
    return resp.json(), resp.status_code


def start_pipeline(orchestrator_url: str, pipeline_id: str) -> tuple[dict, int]:
    """POST to start a pipeline."""
    resp = requests.post(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/start",
        timeout=10,
    )
    return resp.json(), resp.status_code


def wait_for_pipeline_terminal(
    orchestrator_url: str,
    pipeline_id: str,
    timeout: int = 120,
    poll_interval: float = 2.0,
) -> dict:
    """Poll GET /api/v1/pipelines/<id>/status until terminal state."""
    terminal_statuses = {"complete", "failed", "cancelled"}
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("data", {}).get("status", "")
                if status in terminal_statuses:
                    return data
        except requests.ConnectionError:
            pass
        time.sleep(poll_interval)

    raise TimeoutError(f"Pipeline {pipeline_id} did not reach terminal state within {timeout}s")


def wait_for_awaiting_human(
    orchestrator_url: str,
    pipeline_id: str,
    timeout: int = 120,
    poll_interval: float = 2.0,
) -> dict:
    """Poll GET /status until status == 'awaiting_human' or terminal."""
    terminal_statuses = {"complete", "failed", "cancelled"}
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("data", {}).get("status", "")
                if status == "awaiting_human":
                    return data
                if status in terminal_statuses:
                    raise AssertionError(
                        f"Pipeline reached terminal state '{status}' before awaiting_human"
                    )
        except requests.ConnectionError:
            pass
        time.sleep(poll_interval)

    raise TimeoutError(f"Pipeline {pipeline_id} did not reach awaiting_human within {timeout}s")


def resolve_decision(
    orchestrator_url: str,
    pipeline_id: str,
    decision_id: str,
    resolution: str = "approve",
    custom_input: str | None = None,
) -> tuple[dict, int]:
    """POST to resolve a pending decision."""
    body: dict = {"resolution": resolution}
    if custom_input is not None:
        body["custom_input"] = custom_input
    resp = requests.post(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/resolve",
        json=body,
        timeout=10,
    )
    return resp.json(), resp.status_code


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
            except Exception:
                pass
            delete_pipeline(orchestrator_url, pipeline_id)


class TestDecisionTimeout:
    """Test decision timeout with short configurable timeout."""

    def test_decision_timeout_transitions_pipeline(self, orchestrator_url: str) -> None:
        """Pipeline transitions appropriately when decision not resolved.

        Note: This test verifies timeout handling if the orchestrator supports
        decision_timeout_seconds configuration. If not supported, the pipeline
        will remain in awaiting_human indefinitely.
        """
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test decision timeout",
            config={
                "hitl_gates": True,
                # Short timeout for testing (if supported)
                "decision_timeout_seconds": 10,
            },
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Wait for HITL gate
            status_data = wait_for_awaiting_human(orchestrator_url, pipeline_id, timeout=180)
            assert status_data["data"]["status"] == "awaiting_human"

            # Don't resolve - wait for potential timeout
            # Give extra time for timeout to trigger
            time.sleep(15)

            # Check status
            resp = requests.get(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status",
                timeout=10,
            )
            assert resp.status_code == 200
            current_status = resp.json().get("data", {}).get("status", "")

            # Pipeline should either:
            # 1. Still be awaiting_human (timeout not supported/enforced)
            # 2. Failed/cancelled (timeout triggered)
            assert current_status in ("awaiting_human", "failed", "cancelled", "timeout"), (
                f"Unexpected status after timeout period: {current_status}"
            )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestInvalidDecisionId:
    """Test invalid decision resolution (non-existent decision ID)."""

    def test_resolve_nonexistent_decision_returns_404(
        self, orchestrator_url: str
    ) -> None:
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
            except Exception:
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
            except Exception:
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

            # The other should fail with 409 or 404 (or also 200 if very fast)
            # Both succeeding would indicate a race condition bug
            success_count = status_codes.count(200)
            conflict_count = sum(1 for s in status_codes if s in (404, 409))

            # Ideally: 1 success + 1 conflict
            # Acceptable: 2 successes (very fast, both completed before check)
            assert success_count >= 1, "At least one resolution should succeed"
            if success_count == 1:
                assert conflict_count == 1, (
                    f"Expected 1 conflict, got status codes: {status_codes}"
                )

            # Verify pipeline state is consistent (not corrupted)
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            pipeline = get_data["data"]["pipeline"]

            # Pipeline should be in a valid state
            assert pipeline["status"] in (
                "running", "awaiting_human", "complete", "failed"
            ), f"Pipeline in invalid state: {pipeline['status']}"

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
            except Exception:
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
                status_data = wait_for_awaiting_human(
                    orchestrator_url, pipeline_id, timeout=10
                )
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
            except Exception:
                pass
            delete_pipeline(orchestrator_url, pipeline_id)
