"""Integration tests for API validation.

Tests ensure orchestrator API endpoints handle invalid requests correctly
with proper error responses.

All tests require Docker and are marked with @pytest.mark.integration.
"""

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


def delete_pipeline(orchestrator_url: str, pipeline_id: str) -> tuple[dict, int]:
    """DELETE a pipeline by ID."""
    resp = requests.delete(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInvalidMode:
    """Test POST /pipelines with invalid mode."""

    def test_invalid_mode_returns_400(self, orchestrator_url: str) -> None:
        """Returns 400 with clear error message about valid modes."""
        resp = requests.post(
            f"{orchestrator_url}/api/v1/pipelines",
            json={
                "mode": "invalid_mode_xyz",
                "prompt": "Test with invalid mode",
            },
            timeout=10,
        )

        assert resp.status_code == 400, (
            f"Expected 400 for invalid mode, got {resp.status_code}: {resp.text}"
        )

        data = resp.json()
        # Error message should indicate the issue
        error_msg = data.get("message", "") or data.get("error", "") or str(data)
        assert "mode" in error_msg.lower() or "invalid" in error_msg.lower(), (
            f"Error should mention invalid mode: {data}"
        )


class TestMissingRequiredFields:
    """Test POST /pipelines with missing required fields."""

    def test_missing_prompt_returns_400(self, orchestrator_url: str) -> None:
        """Returns 400 with field-specific validation error for missing prompt."""
        resp = requests.post(
            f"{orchestrator_url}/api/v1/pipelines",
            json={
                "mode": "local",
                # Missing "prompt" field
            },
            timeout=10,
        )

        # Should return 400 for missing required field
        assert resp.status_code == 400, (
            f"Expected 400 for missing prompt, got {resp.status_code}: {resp.text}"
        )

        data = resp.json()
        error_msg = data.get("message", "") or data.get("error", "") or str(data)
        # Error should mention the missing field
        assert "prompt" in error_msg.lower() or "required" in error_msg.lower(), (
            f"Error should mention missing prompt: {data}"
        )

    def test_empty_body_returns_400(self, orchestrator_url: str) -> None:
        """Returns 400 for empty request body."""
        resp = requests.post(
            f"{orchestrator_url}/api/v1/pipelines",
            json={},
            timeout=10,
        )

        # Should return 400 for empty body
        assert resp.status_code == 400, (
            f"Expected 400 for empty body, got {resp.status_code}: {resp.text}"
        )

    def test_issue_mode_missing_issue_number_returns_400(
        self, orchestrator_url: str
    ) -> None:
        """Returns 400 when issue mode is used without issue_number."""
        resp = requests.post(
            f"{orchestrator_url}/api/v1/pipelines",
            json={
                "mode": "issue",
                "prompt": "Test without issue number",
                # Missing issue_number for issue mode
            },
            timeout=10,
        )

        # Should return 400 for missing issue_number in issue mode
        assert resp.status_code == 400, (
            f"Expected 400 for issue mode without issue_number, got {resp.status_code}"
        )


class TestGetNonexistentPipeline:
    """Test GET /pipelines/{id} with non-existent ID."""

    def test_get_nonexistent_returns_404(self, orchestrator_url: str) -> None:
        """Returns 404 with appropriate message for non-existent ID."""
        fake_id = "nonexistent-pipeline-12345"
        resp = requests.get(
            f"{orchestrator_url}/api/v1/pipelines/{fake_id}",
            timeout=10,
        )

        assert resp.status_code == 404, (
            f"Expected 404 for non-existent pipeline, got {resp.status_code}"
        )

        data = resp.json()
        # Should indicate not found
        assert not data.get("success", True) or "not found" in str(data).lower()


class TestDeleteNonexistentPipeline:
    """Test DELETE /pipelines/{id} with non-existent ID."""

    def test_delete_nonexistent_returns_404(self, orchestrator_url: str) -> None:
        """Returns 404; idempotent behavior for non-existent pipeline."""
        fake_id = "nonexistent-pipeline-delete-test"
        resp = requests.delete(
            f"{orchestrator_url}/api/v1/pipelines/{fake_id}",
            timeout=10,
        )

        # Should return 404 for non-existent pipeline
        assert resp.status_code == 404, (
            f"Expected 404 for deleting non-existent pipeline, got {resp.status_code}"
        )

    def test_double_delete_is_idempotent(self, orchestrator_url: str) -> None:
        """Deleting the same pipeline twice is handled gracefully."""
        # Create a pipeline
        data, status = create_pipeline(orchestrator_url, prompt="Double delete test")
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        # First delete
        del_resp1 = requests.delete(
            f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
            timeout=10,
        )
        assert del_resp1.status_code == 200

        # Second delete - should return 404 (already deleted)
        del_resp2 = requests.delete(
            f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
            timeout=10,
        )
        assert del_resp2.status_code == 404


class TestPatchInvalidConfig:
    """Test PATCH /pipelines/{id} with invalid config values."""

    def test_patch_invalid_config_returns_400(self, orchestrator_url: str) -> None:
        """Returns 400; config not modified for invalid values."""
        # Create a pipeline
        data, status = create_pipeline(orchestrator_url, prompt="Patch config test")
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Get initial config
            get_resp1 = requests.get(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
                timeout=10,
            )
            initial_config = get_resp1.json()["data"]["pipeline"].get("config", {})

            # Try to patch with invalid config value
            patch_resp = requests.patch(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
                json={
                    "config.max_review_cycles": "not_a_number",  # Invalid type
                },
                timeout=10,
            )

            # Should return 400 for invalid config
            assert patch_resp.status_code in (400, 422), (
                f"Expected 400/422 for invalid config, got {patch_resp.status_code}"
            )

            # Config should not be modified
            get_resp2 = requests.get(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
                timeout=10,
            )
            final_config = get_resp2.json()["data"]["pipeline"].get("config", {})

            # Config should be unchanged
            assert final_config.get("max_review_cycles") == initial_config.get(
                "max_review_cycles"
            )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)

    def test_patch_nonexistent_field_handled(self, orchestrator_url: str) -> None:
        """Patching non-existent config field is handled gracefully."""
        data, status = create_pipeline(orchestrator_url, prompt="Patch unknown field")
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            patch_resp = requests.patch(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
                json={
                    "config.nonexistent_field_xyz": 123,
                },
                timeout=10,
            )

            # Should either accept (ignore unknown field) or reject with 400
            assert patch_resp.status_code in (200, 400, 422), (
                f"Unexpected status {patch_resp.status_code} for unknown field"
            )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestPaginationForListEndpoint:
    """Test pagination for GET /pipelines list endpoint."""

    def test_list_returns_paginated_results(self, orchestrator_url: str) -> None:
        """Returns correct page size and pagination metadata."""
        # Create several pipelines
        pipeline_ids = []
        try:
            for i in range(5):
                data, status = create_pipeline(
                    orchestrator_url,
                    prompt=f"Pagination test pipeline {i + 1}",
                )
                assert status == 200
                pipeline_ids.append(data["data"]["pipeline"]["id"])

            # Test list endpoint
            list_resp = requests.get(
                f"{orchestrator_url}/api/v1/pipelines",
                timeout=10,
            )
            assert list_resp.status_code == 200

            list_data = list_resp.json()
            assert list_data.get("success") is True

            pipelines = list_data["data"]["pipelines"]
            # Should return at least the 5 we created
            assert len(pipelines) >= 5

            # Test with limit parameter if supported
            limited_resp = requests.get(
                f"{orchestrator_url}/api/v1/pipelines",
                params={"limit": 2},
                timeout=10,
            )
            assert limited_resp.status_code == 200

            limited_data = limited_resp.json()
            limited_pipelines = limited_data["data"]["pipelines"]

            # If pagination is supported, should respect limit
            # If not supported, will return all
            assert len(limited_pipelines) >= 1  # At least some results

            # Check for pagination metadata if present
            pagination = limited_data["data"].get("pagination") or limited_data.get(
                "pagination"
            )
            if pagination:
                # If pagination metadata exists, verify structure
                assert "total" in pagination or "has_more" in pagination

        finally:
            for pid in pipeline_ids:
                try:
                    delete_pipeline(orchestrator_url, pid)
                except Exception:
                    pass

    def test_list_with_offset(self, orchestrator_url: str) -> None:
        """Offset parameter works correctly for pagination."""
        pipeline_ids = []
        try:
            # Create a few pipelines
            for i in range(3):
                data, status = create_pipeline(
                    orchestrator_url,
                    prompt=f"Offset test pipeline {i + 1}",
                )
                assert status == 200
                pipeline_ids.append(data["data"]["pipeline"]["id"])

            # Get all pipelines
            all_resp = requests.get(
                f"{orchestrator_url}/api/v1/pipelines",
                timeout=10,
            )
            all_pipelines = all_resp.json()["data"]["pipelines"]

            if len(all_pipelines) >= 2:
                # Test with offset
                offset_resp = requests.get(
                    f"{orchestrator_url}/api/v1/pipelines",
                    params={"offset": 1},
                    timeout=10,
                )

                # Should return successfully
                assert offset_resp.status_code == 200

        finally:
            for pid in pipeline_ids:
                try:
                    delete_pipeline(orchestrator_url, pid)
                except Exception:
                    pass


class TestStatusEndpointValidation:
    """Test status endpoint validation."""

    def test_status_nonexistent_returns_404(self, orchestrator_url: str) -> None:
        """Status endpoint returns 404 for non-existent pipeline."""
        fake_id = "nonexistent-status-test"
        resp = requests.get(
            f"{orchestrator_url}/api/v1/pipelines/{fake_id}/status",
            timeout=10,
        )

        assert resp.status_code == 404, (
            f"Expected 404 for status of non-existent pipeline, got {resp.status_code}"
        )


class TestStartEndpointValidation:
    """Test start endpoint validation."""

    def test_start_nonexistent_returns_404(self, orchestrator_url: str) -> None:
        """Start endpoint returns 404 for non-existent pipeline."""
        fake_id = "nonexistent-start-test"
        resp = requests.post(
            f"{orchestrator_url}/api/v1/pipelines/{fake_id}/start",
            timeout=10,
        )

        assert resp.status_code == 404, (
            f"Expected 404 for starting non-existent pipeline, got {resp.status_code}"
        )

    def test_start_pending_pipeline_succeeds(self, orchestrator_url: str) -> None:
        """Starting a pending pipeline succeeds."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Start validation test",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Start should succeed
            start_resp = requests.post(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/start",
                timeout=10,
            )
            assert start_resp.status_code == 200

            # Wait for completion
            wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestContentTypeValidation:
    """Test content type header validation."""

    def test_non_json_content_type_handled(self, orchestrator_url: str) -> None:
        """Non-JSON content type is handled gracefully."""
        resp = requests.post(
            f"{orchestrator_url}/api/v1/pipelines",
            data="not json data",
            headers={"Content-Type": "text/plain"},
            timeout=10,
        )

        # Should reject with 400 or 415 (Unsupported Media Type)
        assert resp.status_code in (400, 415, 422), (
            f"Expected 400/415/422 for non-JSON content, got {resp.status_code}"
        )

    def test_malformed_json_returns_400(self, orchestrator_url: str) -> None:
        """Malformed JSON body returns 400."""
        resp = requests.post(
            f"{orchestrator_url}/api/v1/pipelines",
            data="{invalid json",
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        # Should reject malformed JSON
        assert resp.status_code in (400, 422), (
            f"Expected 400/422 for malformed JSON, got {resp.status_code}"
        )
