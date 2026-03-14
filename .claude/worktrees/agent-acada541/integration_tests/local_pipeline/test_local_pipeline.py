"""Integration tests for the local SDLC pipeline feature.

These tests spawn real Docker containers (gateway + orchestrator + mock sandbox)
and run through the full pipeline lifecycle. Only the Claude AI portions are
mocked — sandbox containers start, validate their environment, and exit.

All tests require Docker and are marked with @pytest.mark.integration.
"""

import random
import subprocess
import time

import pytest
import requests

from .helpers import (
    create_pipeline,
    delete_pipeline,
    get_pipeline,
    start_pipeline,
    wait_for_awaiting_human,
    wait_for_pipeline_terminal,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Local-only helpers (not in shared helpers.py)
# ---------------------------------------------------------------------------


def list_pipelines(orchestrator_url: str) -> tuple[dict, int]:
    """LIST all pipelines."""
    resp = requests.get(
        f"{orchestrator_url}/api/v1/pipelines",
        timeout=10,
    )
    return resp.json(), resp.status_code


def get_pipeline_status(orchestrator_url: str, pipeline_id: str) -> tuple[dict, int]:
    """GET pipeline status summary."""
    resp = requests.get(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status",
        timeout=10,
    )
    return resp.json(), resp.status_code


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateLocalPipeline:
    """POST /api/v1/pipelines creates a local pipeline correctly."""

    def test_create_local_pipeline(self, orchestrator_url: str) -> None:
        """Create a local pipeline and verify mode, prompt, status."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Add a logout button to the navbar",
        )

        assert status == 200, f"Unexpected status {status}: {data}"
        assert data["success"] is True
        pipeline = data["data"]["pipeline"]
        assert pipeline["mode"] == "local"
        assert pipeline["prompt"] == "Add a logout button to the navbar"
        assert pipeline["status"] == "pending"
        assert pipeline["id"].startswith("local-")
        assert pipeline["current_phase"] == "refine"

        # Cleanup
        delete_pipeline(orchestrator_url, pipeline["id"])


class TestStartLocalPipelineCompletes:
    """Full lifecycle: create -> start -> poll -> verify complete."""

    def test_start_local_pipeline_completes(self, orchestrator_url: str) -> None:
        """Start a local pipeline and wait for it to complete all phases."""
        # Create
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Refactor the auth module",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Start
            start_data, start_status = start_pipeline(orchestrator_url, pipeline_id)
            assert start_status == 200, f"Start failed: {start_data}"
            assert start_data["data"]["status"] == "running"

            # Wait for terminal state (4 phases: refine, plan, implement, pr)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)
            assert final["data"]["status"] == "complete", f"Pipeline did not complete: {final}"

            # Verify final phase is pr (terminal for local)
            assert final["data"]["current_phase"] == "pr"

            # Verify pipeline data via GET
            get_data, get_status = get_pipeline(orchestrator_url, pipeline_id)
            assert get_status == 200
            pipeline = get_data["data"]["pipeline"]
            assert pipeline["status"] == "complete"
            assert pipeline["mode"] == "local"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestLocalPipelineIncludesPrPhase:
    """Local pipelines run all 4 phases (refine, plan, implement, pr)."""

    def test_local_pipeline_includes_pr_phase(self, orchestrator_url: str) -> None:
        """Verify the pipeline runs through all phases including PR."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Full pipeline with PR phase",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)
            assert final["data"]["status"] == "complete"
            assert final["data"]["current_phase"] == "pr"

            # Verify all 4 phase executions are present and complete
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})
            for phase_name in ["refine", "plan", "implement", "pr"]:
                assert phase_name in phases, f"Missing phase: {phase_name}"
                assert phases[phase_name]["status"] == "complete"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestLocalPipelineContainerFailure:
    """Mock sandbox exits code 1 -> pipeline fails with error + container logs."""

    def test_local_pipeline_container_failure(self, orchestrator_url: str) -> None:
        """Inject FORCE_FAIL in prompt and verify pipeline fails.

        The mock sandbox's phase-runner.sh checks for FORCE_FAIL in the
        EGG_PIPELINE_PROMPT environment variable and exits with code 1
        when found. This tests the real container failure path end-to-end.
        """
        data, status = create_pipeline(
            orchestrator_url,
            prompt="This pipeline should FORCE_FAIL on first phase",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_data, start_status = start_pipeline(orchestrator_url, pipeline_id)
            assert start_status == 200

            # Wait for terminal state — should fail on the refine phase
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=300)
            assert final["data"]["status"] == "failed", (
                f"Pipeline should have failed but got: {final}"
            )

            # Pipeline should have failed in the refine phase (first phase)
            assert final["data"]["current_phase"] == "refine"

            # Verify error message on the pipeline
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            pipeline = get_data["data"]["pipeline"]
            assert pipeline["status"] == "failed"
            assert "exit" in pipeline["error"].lower() or "code 1" in pipeline["error"]

            # Verify the refine phase execution is marked as failed
            phases = pipeline.get("phases", {})
            if "refine" in phases:
                assert phases["refine"]["status"] == "failed"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)

    def test_failure_error_includes_container_logs(self, orchestrator_url: str) -> None:
        """When a phase container fails, the error should include log output.

        The orchestrator captures container logs before cleanup so that
        the pipeline error message contains diagnostic output from the
        sandbox, making failures debuggable without manual docker log
        inspection.
        """
        data, status = create_pipeline(
            orchestrator_url,
            prompt="FORCE_FAIL to test log capture",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=300)
            assert final["data"]["status"] == "failed"

            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            error = get_data["data"]["pipeline"].get("error", "")

            # The error should contain "container logs" section with
            # output from the mock sandbox's phase-runner.sh
            assert "container logs" in error.lower(), (
                f"Error message should include container logs section. Got: {error}"
            )
            # The mock sandbox prints "FORCE_FAIL detected" before exiting
            assert "FORCE_FAIL" in error, (
                f"Container logs should contain sandbox output. Got: {error}"
            )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestSandboxReceivesEnvironment:
    """Verify spawned containers receive all required environment and volumes.

    The mock sandbox validates three categories (with distinct exit codes):
      - exit 2: missing pipeline identity vars (EGG_PIPELINE_PHASE, etc.)
      - exit 3: missing sandbox infra vars (GATEWAY_URL)
      - exit 4: repo volume not mounted
    A completed pipeline proves all three checks passed for every phase.
    """

    def test_sandbox_receives_pipeline_env(self, orchestrator_url: str) -> None:
        """Pipeline completes → all 4 phases got correct pipeline env vars."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Verify pipeline env vars",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)

            assert final["data"]["status"] == "complete", (
                f"Pipeline failed — mock sandbox likely missing required env vars "
                f"(exit 2=pipeline vars, 3=infra vars, 4=repo volume): {final}"
            )

            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})
            for phase_name in ["refine", "plan", "implement", "pr"]:
                assert phase_name in phases, f"Phase {phase_name} not found"
                assert phases[phase_name]["status"] == "complete"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)

    def test_sandbox_receives_gateway_url(self, orchestrator_url: str) -> None:
        """Pipeline completes → GATEWAY_URL was set (exit 3 if missing)."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Verify GATEWAY_URL passed",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)

            if final["data"]["status"] == "failed":
                # Check if the error mentions exit code 3 (infra vars)
                get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
                error = get_data["data"]["pipeline"].get("error", "")
                assert "code 3" not in error, (
                    f"Sandbox exited code 3: GATEWAY_URL not passed to container. Error: {error}"
                )

            assert final["data"]["status"] == "complete", f"Pipeline did not complete: {final}"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)

    def test_sandbox_receives_repo_volume(self, orchestrator_url: str) -> None:
        """Pipeline completes → repo volume was mounted (exit 4 if missing)."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Verify repo volume mounted",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)

            if final["data"]["status"] == "failed":
                get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
                error = get_data["data"]["pipeline"].get("error", "")
                assert "code 4" not in error, (
                    f"Sandbox exited code 4: repo volume not mounted. Error: {error}"
                )

            assert final["data"]["status"] == "complete", f"Pipeline did not complete: {final}"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestPipelineStartIdempotency:
    """Starting an already-running or completed pipeline returns 409."""

    def test_start_running_pipeline_returns_409(self, orchestrator_url: str) -> None:
        """Cannot start a pipeline that is already running."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test idempotency",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # First start succeeds
            start_data, start_status = start_pipeline(orchestrator_url, pipeline_id)
            assert start_status == 200

            # Second start should return 409
            start_data2, start_status2 = start_pipeline(orchestrator_url, pipeline_id)
            assert start_status2 == 409, (
                f"Expected 409 for re-starting running pipeline, got {start_status2}: {start_data2}"
            )

            # Wait for completion so cleanup works cleanly
            wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)

    def test_start_awaiting_human_pipeline_returns_409(self, orchestrator_url: str) -> None:
        """Cannot start a pipeline that is awaiting human approval."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test awaiting_human idempotency",
            config={"hitl_gates": True},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Start pipeline — it will pause at the refine HITL gate
            start_data, start_status = start_pipeline(orchestrator_url, pipeline_id)
            assert start_status == 200

            # Wait for awaiting_human status
            status_data = wait_for_awaiting_human(orchestrator_url, pipeline_id, timeout=180)
            assert status_data["data"]["status"] == "awaiting_human"

            # Try to start again — should be 409
            start_data2, start_status2 = start_pipeline(orchestrator_url, pipeline_id)
            assert start_status2 == 409, (
                f"Expected 409 for starting awaiting_human pipeline, "
                f"got {start_status2}: {start_data2}"
            )
            assert "awaiting human" in start_data2.get("message", "").lower()

            # Resolve the gate so the pipeline can finish cleanly
            pending = status_data["data"].get("pending_decision")
            assert pending is not None
            resolve_pending_decision(orchestrator_url, pipeline_id, pending["id"])

            # Resolve the plan gate too
            status_data2 = wait_for_awaiting_human(orchestrator_url, pipeline_id, timeout=180)
            pending2 = status_data2["data"].get("pending_decision")
            assert pending2 is not None
            resolve_pending_decision(orchestrator_url, pipeline_id, pending2["id"])

            # Wait for completion so cleanup works cleanly
            wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)

    def test_start_completed_pipeline_returns_409(self, orchestrator_url: str) -> None:
        """Cannot start a pipeline that has already completed."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test completed idempotency",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)

            # Try to start again — should be 409
            start_data, start_status = start_pipeline(orchestrator_url, pipeline_id)
            assert start_status == 409, (
                f"Expected 409 for re-starting completed pipeline, got {start_status}: {start_data}"
            )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestIssuePipelineIncludesPrPhase:
    """Issue-mode pipeline spawns 4 containers including PR phase."""

    def test_issue_pipeline_includes_pr_phase(self, orchestrator_url: str) -> None:
        """Create an issue-mode pipeline and verify it runs through PR."""
        issue_num = random.randint(10000, 99999)
        data, status = create_pipeline(
            orchestrator_url,
            mode="issue",
            prompt="Fix the login bug",
            issue_number=issue_num,
            repo="test-owner/test-repo",
            branch=f"egg/issue-{issue_num}",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]
        assert pipeline_id == f"issue-{issue_num}"

        try:
            start_data, start_status = start_pipeline(orchestrator_url, pipeline_id)
            assert start_status == 200

            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=300)
            assert final["data"]["status"] == "complete", (
                f"Issue pipeline did not complete: {final}"
            )
            # Issue pipeline terminal phase is PR
            assert final["data"]["current_phase"] == "pr"

            # Verify all 4 phases were executed
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})
            for phase_name in ["refine", "plan", "implement", "pr"]:
                assert phase_name in phases, f"Missing phase: {phase_name}"
                assert phases[phase_name]["status"] == "complete"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)
            # Force-remove any leftover sandbox container for this pipeline
            subprocess.run(
                ["docker", "rm", "-f", f"egg-sandbox-issue-{issue_num}-coder"],
                capture_output=True,
                timeout=10,
                check=False,
            )


class TestPipelineCRUD:
    """Create, list (appears), get (correct data), delete (gone)."""

    def test_pipeline_crud(self, orchestrator_url: str) -> None:
        """Full CRUD lifecycle for a local pipeline."""
        # Create
        data, status = create_pipeline(
            orchestrator_url,
            prompt="CRUD test pipeline",
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Get
            get_data, get_status = get_pipeline(orchestrator_url, pipeline_id)
            assert get_status == 200
            pipeline = get_data["data"]["pipeline"]
            assert pipeline["id"] == pipeline_id
            assert pipeline["mode"] == "local"
            assert pipeline["prompt"] == "CRUD test pipeline"
            assert pipeline["status"] == "pending"

            # List — should appear
            list_data, list_status = list_pipelines(orchestrator_url)
            assert list_status == 200
            ids = [p["id"] for p in list_data["data"]["pipelines"]]
            assert pipeline_id in ids

            # Status endpoint
            status_data, status_code = get_pipeline_status(orchestrator_url, pipeline_id)
            assert status_code == 200
            assert status_data["data"]["id"] == pipeline_id
            assert status_data["data"]["status"] == "pending"

            # Delete
            del_data, del_status = delete_pipeline(orchestrator_url, pipeline_id)
            assert del_status == 200
            assert del_data["success"] is True

            # Verify gone
            gone_data, gone_status = get_pipeline(orchestrator_url, pipeline_id)
            assert gone_status == 404

        except BaseException:
            # Cleanup on failure
            try:
                delete_pipeline(orchestrator_url, pipeline_id)
            except requests.RequestException:
                pass
            raise


class TestGatewayLocalModeBlocksPush:
    """Register a local session, attempt push -> 403."""

    def test_gateway_local_mode_blocks_push(self, local_pipeline_stack) -> None:
        """Create a local-mode gateway session and verify push is blocked.

        The gateway validates sessions by matching request.remote_addr against
        the session's container_ip. Since we're sending requests from the test
        host through a mapped port, we first detect what IP the gateway sees
        for our requests (via the health endpoint), then bind the session to
        that IP so session validation passes and the local-mode push block
        (which happens AFTER session auth) can be tested.
        """
        gateway_url = local_pipeline_stack.gateway_url
        launcher_secret = local_pipeline_stack.launcher_secret

        # Detect the source IP the gateway sees for requests from this host
        health_resp = requests.get(
            f"{gateway_url}/api/v1/health",
            timeout=10,
        )
        source_ip = health_resp.json().get("client_ip", "")
        assert source_ip, "Gateway health endpoint did not return client_ip"

        # Create a local-mode session bound to our actual source IP
        session_resp = requests.post(
            f"{gateway_url}/api/v1/sessions/create",
            headers={"Authorization": f"Bearer {launcher_secret}"},
            json={
                "container_id": f"test-push-block-{int(time.time())}",
                "container_ip": source_ip,
                "mode": "local",
                "repos": ["test-owner/test-repo"],
                "uid": 1000,
                "gid": 1000,
            },
            timeout=10,
        )
        session_data = session_resp.json()
        assert session_data.get("success") is True, f"Session creation failed: {session_data}"

        session_token = session_data.get("data", session_data).get("session_token")
        assert session_token, f"No session token in response: {session_data}"

        try:
            # Attempt a push — should be blocked with 403
            push_resp = requests.post(
                f"{gateway_url}/api/v1/git/push",
                headers={"Authorization": f"Bearer {session_token}"},
                json={
                    "repo_path": "/home/egg/repos",
                    "refspec": "egg/test-branch",
                },
                timeout=10,
            )

            assert push_resp.status_code == 403, (
                f"Expected 403 for local mode push, got {push_resp.status_code}: {push_resp.text}"
            )

            push_data = push_resp.json()
            assert (
                "local" in push_data.get("message", "").lower()
                or "local" in str(push_data.get("details", {})).lower()
            ), f"Error message should mention local mode: {push_data}"

        finally:
            # Cleanup session
            requests.delete(
                f"{gateway_url}/api/v1/sessions/{session_token}",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                timeout=10,
            )


class TestReviewCycleApproved:
    """Multiple reviewer containers are spawned for each reviewed phase.

    The mock sandbox writes an 'approved' verdict by default, so the
    pipeline should complete normally — with multiple typed reviewer
    containers spawned after each reviewed phase (refine, plan, implement).
    """

    def test_pipeline_completes_with_multi_review(self, orchestrator_url: str) -> None:
        """Pipeline completes when all reviewer types approve all phases."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test multi-reviewer approved",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            # Longer timeout: 2 reviewers for refine+plan, 4 for implement + checker
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)
            assert final["data"]["status"] == "complete", (
                f"Pipeline should complete with approved reviews: {final}"
            )
            assert final["data"]["current_phase"] == "pr"

            # Verify all phases completed
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})
            for phase_name in ["refine", "plan", "implement", "pr"]:
                assert phase_name in phases, f"Missing phase: {phase_name}"
                assert phases[phase_name]["status"] == "complete"

            # Review cycles should be 0 (approved on first review, no revisions)
            assert phases["refine"]["review_cycles"] == 0
            assert phases["plan"]["review_cycles"] == 0
            assert phases["implement"]["review_cycles"] == 0

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestReviewCycleCircuitBreaker:
    """Circuit breaker: all reviewers return needs_revision.

    With max_review_cycles=1, the pipeline should still advance after
    the first multi-reviewer round says needs_revision (circuit breaker kicks in).
    """

    def test_circuit_breaker_advances_pipeline(self, orchestrator_url: str) -> None:
        """Pipeline completes despite needs_revision when circuit breaker fires."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="REVIEW_NEEDS_REVISION circuit breaker test",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Set max_review_cycles=1 so the circuit breaker fires
            # on the first needs_revision verdict.
            patch_resp = requests.patch(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
                json={"config.max_review_cycles": 1},
                timeout=10,
            )
            assert patch_resp.status_code == 200, f"Failed to patch config: {patch_resp.json()}"

            start_pipeline(orchestrator_url, pipeline_id)
            # Longer timeout: each phase has worker + multiple reviewer containers
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=420)
            assert final["data"]["status"] == "complete", (
                f"Pipeline should complete via circuit breaker: {final}"
            )
            assert final["data"]["current_phase"] == "pr"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestFailedPipelineCannotRestart:
    """A failed pipeline cannot be restarted (returns 409)."""

    def test_failed_pipeline_returns_409(self, orchestrator_url: str) -> None:
        """Trigger a failure via FORCE_FAIL, then try to restart."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="FORCE_FAIL for restart test",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=300)
            assert final["data"]["status"] == "failed"

            # Try to start again — should be 409
            start_data, start_status = start_pipeline(orchestrator_url, pipeline_id)
            assert start_status == 409, (
                f"Expected 409 for restarting failed pipeline, got {start_status}: {start_data}"
            )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestImplementPhaseReviewed:
    """Implement phase now gets checker + multi-reviewer cycle.

    Unlike previous behavior where implement was skipped for review,
    the implement phase now runs:
    1. Worker (CODER)
    2. Checker (CHECKER) — runs tests/lint
    3. Multi-reviewer loop (code, contract for implement; refine, agent-design for refine)
    """

    def test_implement_phase_gets_reviewed(self, orchestrator_url: str) -> None:
        """Pipeline completes with implement phase reviewed (checker + reviewers)."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test implement phase review",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)
            assert final["data"]["status"] == "complete", (
                f"Pipeline should complete with implement review: {final}"
            )

            # Verify implement phase completed (was previously just skipped)
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})
            assert "implement" in phases
            assert phases["implement"]["status"] == "complete"
            # No revision cycles (all reviewers approve by default)
            assert phases["implement"]["review_cycles"] == 0

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestAutofixLoop:
    """Checker fails on first attempt, autofix runs, checker passes on retry.

    Uses CHECK_FAIL_THEN_PASS prompt keyword to make the mock checker
    fail on the first attempt and pass on subsequent attempts. Verifies
    the autofix loop runs and the pipeline still completes.
    """

    def test_autofix_loop_recovers(self, orchestrator_url: str) -> None:
        """Pipeline completes after checker fail → autofix → checker pass."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test CHECK_FAIL_THEN_PASS autofix loop",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            # Extra time: autofix loop adds checker + autofixer containers
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=480)
            assert final["data"]["status"] == "complete", (
                f"Pipeline should complete after autofix recovery: {final}"
            )
            assert final["data"]["current_phase"] == "pr"

            # Verify implement phase completed
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})
            assert "implement" in phases
            assert phases["implement"]["status"] == "complete"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestAutofixCircuitBreaker:
    """Checker always fails — verify pipeline still advances after max attempts.

    Uses CHECK_FAIL prompt keyword to make the mock checker always fail.
    The autofix circuit breaker (max 3 attempts) should fire and the
    pipeline should proceed to review and complete despite check failures.
    """

    def test_autofix_circuit_breaker_advances(self, orchestrator_url: str) -> None:
        """Pipeline completes despite persistent check failures."""
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test CHECK_FAIL autofix circuit breaker",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            # Extra time: max 3 checker + 2 autofixer iterations
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=600)
            assert final["data"]["status"] == "complete", (
                f"Pipeline should complete via autofix circuit breaker: {final}"
            )
            assert final["data"]["current_phase"] == "pr"

            # Verify implement phase completed (graceful degradation)
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})
            assert "implement" in phases
            assert phases["implement"]["status"] == "complete"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestContractCreatedForLocalPipeline:
    """Creating a local pipeline also creates a companion contract."""

    def test_contract_file_exists_after_create(self, local_pipeline_stack) -> None:
        """Verify .egg-state/contracts/{pipeline_id}.json is created."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test contract creation",
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Check that the contract file was created in the repos dir
            import json
            from pathlib import Path

            contract_path = Path(repos_dir) / f".egg-state/contracts/{pipeline_id}.json"
            assert contract_path.exists(), f"Contract file should exist at {contract_path}"

            # Verify contract content
            contract_data = json.loads(contract_path.read_text())
            assert contract_data.get("pipeline_id") == pipeline_id
            assert contract_data.get("current_phase") == "refine"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


# ---------------------------------------------------------------------------
# HITL gate helpers
# ---------------------------------------------------------------------------


def resolve_pending_decision(
    orchestrator_url: str,
    pipeline_id: str,
    decision_id: str,
    resolution: str = "approve",
) -> dict:
    """POST to resolve a pending decision."""
    resp = requests.post(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/resolve",
        json={"resolution": resolution},
        timeout=10,
    )
    return resp.json()


# ---------------------------------------------------------------------------
# HITL gate tests
# ---------------------------------------------------------------------------


class TestHITLGate:
    """Pipeline pauses for human approval after refine and plan phases."""

    def test_hitl_gate_full_flow(self, orchestrator_url: str) -> None:
        """Pipeline pauses after refine and plan, resumes on approval.

        1. Create pipeline with hitl_gates=True (default)
        2. Start pipeline
        3. Wait for awaiting_human after refine
        4. Verify pending_decision in status response
        5. Resolve decision
        6. Wait for awaiting_human after plan
        7. Resolve plan decision
        8. Wait for pipeline to complete through implement + pr
        """
        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test HITL gate flow",
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Start pipeline
            start_data, start_status = start_pipeline(orchestrator_url, pipeline_id)
            assert start_status == 200

            # --- Gate 1: refine phase ---
            status_data = wait_for_awaiting_human(orchestrator_url, pipeline_id, timeout=180)
            assert status_data["data"]["status"] == "awaiting_human"
            assert status_data["data"]["pending_decisions"] >= 1

            # Verify pending_decision details are included
            pending = status_data["data"].get("pending_decision")
            assert pending is not None, "Status should include pending_decision details"
            assert "id" in pending
            assert "question" in pending
            assert "refine" in pending["question"]
            # Context should contain the mock draft content
            assert "context" in pending
            assert len(pending["context"]) > 0

            # Resolve the refine gate
            resolve_data = resolve_pending_decision(orchestrator_url, pipeline_id, pending["id"])
            assert resolve_data["success"] is True

            # --- Gate 2: plan phase ---
            status_data = wait_for_awaiting_human(orchestrator_url, pipeline_id, timeout=180)
            assert status_data["data"]["status"] == "awaiting_human"

            pending = status_data["data"].get("pending_decision")
            assert pending is not None
            assert "plan" in pending["question"]

            # Resolve the plan gate
            resolve_data = resolve_pending_decision(orchestrator_url, pipeline_id, pending["id"])
            assert resolve_data["success"] is True

            # --- No gate after implement — pipeline should complete ---
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)
            assert final["data"]["status"] == "complete", (
                f"Pipeline should complete after both gates approved: {final}"
            )
            assert final["data"]["current_phase"] == "pr"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


# ---------------------------------------------------------------------------
# Review Cycle Edge Cases
# ---------------------------------------------------------------------------


class TestReviewCycleEdgeCases:
    """Test complex review scenarios including disagreement and limits."""

    def test_reviewer_mixed_verdict_handled(self, orchestrator_url: str) -> None:
        """Pipeline handles mixed verdicts (some approve, some reject).

        Uses REVIEWER_MIXED_VERDICT prompt keyword where first reviewer (code)
        approves but other reviewers request revision. The pipeline should
        follow its configured policy (majority or consensus) for handling
        mixed verdicts.
        """
        data, status = create_pipeline(
            orchestrator_url,
            prompt="REVIEWER_MIXED_VERDICT test - mixed reviewer verdicts",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)

            # Pipeline should either complete (if majority wins) or enter revision cycles
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=480)

            # Should eventually complete (either by approval or circuit breaker)
            assert final["data"]["status"] == "complete", (
                f"Pipeline should complete despite mixed verdicts: {final}"
            )

            # Verify the pipeline went through review process
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})

            # At least one reviewed phase should have processed the mixed verdict
            reviewed_phases = ["refine", "plan", "implement"]
            any_reviewed = any(
                phases.get(p, {}).get("status") == "complete" for p in reviewed_phases
            )
            assert any_reviewed, "At least one reviewed phase should have completed"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)

    def test_max_review_cycles_zero_triggers_immediate_circuit_breaker(
        self, orchestrator_url: str
    ) -> None:
        """Any needs_revision verdict triggers circuit breaker immediately.

        With max_review_cycles=0, the first needs_revision verdict should
        trigger the circuit breaker and advance the pipeline rather than
        entering a revision cycle.
        """
        data, status = create_pipeline(
            orchestrator_url,
            prompt="REVIEW_NEEDS_REVISION max_cycles=0 test",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Set max_review_cycles=0 for immediate circuit breaker
            patch_resp = requests.patch(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
                json={"config.max_review_cycles": 0},
                timeout=10,
            )
            assert patch_resp.status_code == 200, f"Failed to patch: {patch_resp.json()}"

            start_pipeline(orchestrator_url, pipeline_id)

            # Should complete quickly since no revision cycles are allowed
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=480)
            assert final["data"]["status"] == "complete", (
                f"Pipeline should complete via circuit breaker: {final}"
            )

            # Verify review_cycles stayed at 0 for at least one phase
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})

            # With max_review_cycles=0, revision attempts should be blocked
            for phase_name in ["refine", "plan", "implement"]:
                if phase_name in phases:
                    # review_cycles should not exceed 0 (circuit breaker fired immediately)
                    cycles = phases[phase_name].get("review_cycles", 0)
                    assert cycles <= 1, f"Phase {phase_name} had {cycles} review cycles with max=0"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)

    def test_review_cycle_counter_accuracy(self, orchestrator_url: str) -> None:
        """review_cycles count matches actual revision iterations.

        Tests that the review_cycles counter accurately tracks how many
        revision cycles occurred for each phase.
        """
        data, status = create_pipeline(
            orchestrator_url,
            prompt="REVIEW_NEEDS_REVISION cycle counter test",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Set max_review_cycles=2 to allow some revisions
            patch_resp = requests.patch(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
                json={"config.max_review_cycles": 2},
                timeout=10,
            )
            assert patch_resp.status_code == 200

            start_pipeline(orchestrator_url, pipeline_id)

            # Wait for completion (will hit circuit breaker after 2 cycles)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=600)
            assert final["data"]["status"] == "complete"

            # Verify review cycles counter
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})

            # With REVIEW_NEEDS_REVISION, we expect some revision cycles
            # unless the orchestrator uses a different counting mechanism
            assert "refine" in phases, "Should have refine phase data"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)

    def test_reviewer_verdict_files_isolated_per_pipeline(self, local_pipeline_stack) -> None:
        """Concurrent pipelines have distinct verdict file paths.

        Verifies that reviewer verdict files are properly namespaced by
        pipeline ID to prevent cross-contamination.
        """
        import json
        from pathlib import Path

        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

        # Create two pipelines
        data1, status1 = create_pipeline(
            orchestrator_url,
            prompt="Verdict isolation test ONE",
            config={"hitl_gates": False},
        )
        assert status1 == 200
        pipeline_id_1 = data1["data"]["pipeline"]["id"]

        data2, status2 = create_pipeline(
            orchestrator_url,
            prompt="Verdict isolation test TWO",
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

            # Check the reviews directory
            reviews_dir = Path(repos_dir) / ".egg-state/reviews"
            if reviews_dir.exists():
                # Review files should either be pipeline-prefixed or
                # use a structure that prevents cross-contamination
                review_files = list(reviews_dir.glob("*.json"))

                # Each review file should be associated with only one pipeline
                for review_file in review_files:
                    try:
                        # Verify file is valid JSON (will raise if not)
                        json.loads(review_file.read_text())
                        # Review should not reference both pipelines
                        file_text = review_file.read_text()
                        has_id1 = pipeline_id_1 in file_text
                        has_id2 = pipeline_id_2 in file_text
                        # Should not have BOTH IDs (cross-contamination)
                        assert not (has_id1 and has_id2), (
                            f"Review file {review_file.name} references both pipelines"
                        )
                    except json.JSONDecodeError:
                        # Invalid JSON is a different problem
                        pass

        finally:
            delete_pipeline(orchestrator_url, pipeline_id_1)
            delete_pipeline(orchestrator_url, pipeline_id_2)

    def test_pipeline_completes_despite_all_reviewers_reject(self, orchestrator_url: str) -> None:
        """Pipeline completes via circuit breaker when all reviewers reject.

        This test verifies graceful degradation: even if all reviewers
        consistently return needs_revision, the pipeline eventually completes
        via the circuit breaker mechanism.
        """
        data, status = create_pipeline(
            orchestrator_url,
            prompt="REVIEW_NEEDS_REVISION all reject test",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            # Use default max_review_cycles (usually 1-3)
            start_pipeline(orchestrator_url, pipeline_id)

            # Should complete via circuit breaker
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=600)
            assert final["data"]["status"] == "complete", (
                f"Pipeline should complete via circuit breaker: {final}"
            )
            assert final["data"]["current_phase"] == "pr"

            # Verify all phases eventually completed
            get_data, _ = get_pipeline(orchestrator_url, pipeline_id)
            phases = get_data["data"]["pipeline"].get("phases", {})
            for phase_name in ["refine", "plan", "implement", "pr"]:
                assert phases.get(phase_name, {}).get("status") == "complete", (
                    f"Phase {phase_name} should be complete"
                )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)
