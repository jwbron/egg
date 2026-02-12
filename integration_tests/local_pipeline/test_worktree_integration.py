"""Integration tests for worktree management in realistic SDLC pipeline scenarios.

These tests exercise the full worktree lifecycle through the gateway API:
- Create/use/delete worktrees
- Permission handling (uid/gid ownership)
- Edge cases from PR #569 (empty worktrees, root-owned worktrees)
- Pipeline container worktree sharing

All tests require Docker and use the local_pipeline_stack fixture.
Tests are marked with @pytest.mark.integration.
"""

import subprocess
import time
from pathlib import Path

import pytest
import requests

from .conftest import LocalPipelineStack, wait_for_pipeline_terminal

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper Functions for Worktree Verification
# ---------------------------------------------------------------------------


def verify_worktree_exists_and_valid(
    worktree_path: str,
    check_via_gateway_exec: bool = False,
    gateway_container_name: str | None = None,
) -> tuple[bool, str]:
    """
    Check if a worktree path exists and is a valid git worktree.

    A valid worktree has:
    - The path exists and is not empty
    - Contains a .git file (not directory) with "gitdir:" content

    Args:
        worktree_path: Path to the worktree directory
        check_via_gateway_exec: If True, check via docker exec in gateway container
        gateway_container_name: Name of the gateway container (required if check_via_gateway_exec)

    Returns:
        Tuple of (is_valid, reason_message)
    """
    if check_via_gateway_exec:
        if not gateway_container_name:
            return False, "gateway_container_name required for docker exec"

        # Check path exists
        result = subprocess.run(
            ["docker", "exec", gateway_container_name, "test", "-d", worktree_path],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return False, f"Directory does not exist: {worktree_path}"

        # Check not empty (has files)
        result = subprocess.run(
            ["docker", "exec", gateway_container_name, "ls", "-A", worktree_path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False, f"Directory is empty: {worktree_path}"

        # Check .git is a file with gitdir content
        git_path = f"{worktree_path}/.git"
        result = subprocess.run(
            ["docker", "exec", gateway_container_name, "test", "-f", git_path],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return False, f".git is not a file (may be directory or missing): {git_path}"

        result = subprocess.run(
            ["docker", "exec", gateway_container_name, "cat", git_path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0 or "gitdir:" not in result.stdout:
            return False, f".git file does not contain gitdir: {git_path}"

        return True, "Worktree is valid"
    else:
        # Check locally
        path = Path(worktree_path)
        if not path.exists():
            return False, f"Directory does not exist: {worktree_path}"

        if not any(path.iterdir()):
            return False, f"Directory is empty: {worktree_path}"

        git_file = path / ".git"
        if not git_file.exists():
            return False, f".git does not exist: {git_file}"

        if not git_file.is_file():
            return False, f".git is not a file (is directory): {git_file}"

        content = git_file.read_text()
        if "gitdir:" not in content:
            return False, f".git file does not contain gitdir: {git_file}"

        return True, "Worktree is valid"


def verify_worktree_ownership(
    worktree_path: str,
    expected_uid: int,
    expected_gid: int,
    gateway_container_name: str,
) -> tuple[bool, str]:
    """
    Check if a worktree has correct ownership (not root-owned).

    Args:
        worktree_path: Path to the worktree directory
        expected_uid: Expected user ID
        expected_gid: Expected group ID
        gateway_container_name: Name of the gateway container

    Returns:
        Tuple of (is_correct, reason_message)
    """
    # stat -c '%u:%g' gives uid:gid
    result = subprocess.run(
        ["docker", "exec", gateway_container_name, "stat", "-c", "%u:%g", worktree_path],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return False, f"Failed to stat worktree: {result.stderr}"

    actual_ownership = result.stdout.strip()
    expected_ownership = f"{expected_uid}:{expected_gid}"

    if actual_ownership == expected_ownership:
        return True, f"Ownership correct: {actual_ownership}"

    # Special case: check if owned by root
    if actual_ownership == "0:0":
        return False, f"Worktree is root-owned (0:0), expected {expected_ownership}"

    return False, f"Ownership mismatch: actual {actual_ownership}, expected {expected_ownership}"


def verify_worktree_writable(
    worktree_path: str,
    gateway_container_name: str,
    test_filename: str = ".worktree-write-test",
) -> tuple[bool, str]:
    """
    Check if a worktree is writable by attempting to create a test file.

    Args:
        worktree_path: Path to the worktree directory
        gateway_container_name: Name of the gateway container
        test_filename: Name of the test file to create

    Returns:
        Tuple of (is_writable, reason_message)
    """
    test_file_path = f"{worktree_path}/{test_filename}"

    # Try to create a test file
    result = subprocess.run(
        ["docker", "exec", gateway_container_name, "touch", test_file_path],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return False, f"Cannot write to worktree: {result.stderr}"

    # Clean up test file
    subprocess.run(
        ["docker", "exec", gateway_container_name, "rm", "-f", test_file_path],
        capture_output=True,
        timeout=10,
        check=False,
    )

    return True, "Worktree is writable"


def get_gateway_container_name(compose_project: str) -> str:
    """Get the gateway container name for a compose project."""
    return f"{compose_project}-gateway"


# ---------------------------------------------------------------------------
# Test Infrastructure Setup (Phase 1)
# ---------------------------------------------------------------------------


class TestWorktreeInfrastructure:
    """Verify test infrastructure for worktree integration tests."""

    def test_local_pipeline_stack_available(self, local_pipeline_stack: LocalPipelineStack) -> None:
        """Verify the local pipeline stack fixture works."""
        assert local_pipeline_stack.gateway_url
        assert local_pipeline_stack.orchestrator_url
        assert local_pipeline_stack.launcher_secret
        assert local_pipeline_stack.compose_project

    def test_gateway_health(self, local_pipeline_stack: LocalPipelineStack) -> None:
        """Verify the gateway is healthy."""
        resp = requests.get(f"{local_pipeline_stack.gateway_url}/api/v1/health", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok" or data.get("success") is True

    def test_gateway_container_accessible(self, local_pipeline_stack: LocalPipelineStack) -> None:
        """Verify we can exec into the gateway container."""
        gateway_container = get_gateway_container_name(local_pipeline_stack.compose_project)
        result = subprocess.run(
            ["docker", "exec", gateway_container, "echo", "hello"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0
        assert "hello" in result.stdout


# ---------------------------------------------------------------------------
# Worktree Lifecycle Tests (Phase 2)
# ---------------------------------------------------------------------------


class TestWorktreeCreation:
    """Test worktree creation through the gateway API."""

    def test_worktree_create_returns_valid_paths(
        self, local_pipeline_stack: LocalPipelineStack
    ) -> None:
        """Gateway /api/v1/worktree/create returns paths that exist and are non-empty."""
        gateway_url = local_pipeline_stack.gateway_url
        launcher_secret = local_pipeline_stack.launcher_secret
        gateway_container = get_gateway_container_name(local_pipeline_stack.compose_project)

        container_id = f"test-worktree-{int(time.time())}"

        try:
            # Create worktree via gateway API
            resp = requests.post(
                f"{gateway_url}/api/v1/worktree/create",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                json={
                    "container_id": container_id,
                    "repos": ["test-owner/test-repo"],
                    "uid": 1000,
                    "gid": 1000,
                },
                timeout=30,
            )
            assert resp.status_code == 200, f"Worktree create failed: {resp.text}"

            data = resp.json()
            assert data.get("success") is True
            worktrees = data.get("data", {}).get("worktrees", {})
            assert "test-repo" in worktrees, f"test-repo not in worktrees: {worktrees}"

            # The returned path is a host path, but we need to verify inside the container
            # The gateway container path is /home/egg/.egg-worktrees/{container_id}/test-repo
            container_worktree_path = f"/home/egg/.egg-worktrees/{container_id}/test-repo"

            is_valid, reason = verify_worktree_exists_and_valid(
                container_worktree_path,
                check_via_gateway_exec=True,
                gateway_container_name=gateway_container,
            )
            assert is_valid, f"Worktree not valid: {reason}"

        finally:
            # Cleanup
            requests.post(
                f"{gateway_url}/api/v1/worktree/delete",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                json={"container_id": container_id, "force": True},
                timeout=30,
            )

    def test_worktree_has_correct_ownership(self, local_pipeline_stack: LocalPipelineStack) -> None:
        """Created worktree is owned by specified uid:gid, not root."""
        gateway_url = local_pipeline_stack.gateway_url
        launcher_secret = local_pipeline_stack.launcher_secret
        gateway_container = get_gateway_container_name(local_pipeline_stack.compose_project)

        container_id = f"test-ownership-{int(time.time())}"
        expected_uid = 1000
        expected_gid = 1000

        try:
            resp = requests.post(
                f"{gateway_url}/api/v1/worktree/create",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                json={
                    "container_id": container_id,
                    "repos": ["test-owner/test-repo"],
                    "uid": expected_uid,
                    "gid": expected_gid,
                },
                timeout=30,
            )
            assert resp.status_code == 200, f"Worktree create failed: {resp.text}"

            container_worktree_path = f"/home/egg/.egg-worktrees/{container_id}/test-repo"

            is_correct, reason = verify_worktree_ownership(
                container_worktree_path,
                expected_uid,
                expected_gid,
                gateway_container,
            )
            assert is_correct, f"Worktree ownership incorrect: {reason}"

        finally:
            requests.post(
                f"{gateway_url}/api/v1/worktree/delete",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                json={"container_id": container_id, "force": True},
                timeout=30,
            )

    def test_worktree_is_writable(self, local_pipeline_stack: LocalPipelineStack) -> None:
        """Worktree can be written to by the container user."""
        gateway_url = local_pipeline_stack.gateway_url
        launcher_secret = local_pipeline_stack.launcher_secret
        gateway_container = get_gateway_container_name(local_pipeline_stack.compose_project)

        container_id = f"test-writable-{int(time.time())}"

        try:
            resp = requests.post(
                f"{gateway_url}/api/v1/worktree/create",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                json={
                    "container_id": container_id,
                    "repos": ["test-owner/test-repo"],
                    "uid": 1000,
                    "gid": 1000,
                },
                timeout=30,
            )
            assert resp.status_code == 200, f"Worktree create failed: {resp.text}"

            container_worktree_path = f"/home/egg/.egg-worktrees/{container_id}/test-repo"

            is_writable, reason = verify_worktree_writable(
                container_worktree_path, gateway_container
            )
            assert is_writable, f"Worktree not writable: {reason}"

        finally:
            requests.post(
                f"{gateway_url}/api/v1/worktree/delete",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                json={"container_id": container_id, "force": True},
                timeout=30,
            )


class TestWorktreeDeletion:
    """Test worktree deletion through the gateway API."""

    def test_worktree_deletion_cleans_up_properly(
        self, local_pipeline_stack: LocalPipelineStack
    ) -> None:
        """After delete, worktree path no longer exists."""
        gateway_url = local_pipeline_stack.gateway_url
        launcher_secret = local_pipeline_stack.launcher_secret
        gateway_container = get_gateway_container_name(local_pipeline_stack.compose_project)

        container_id = f"test-delete-{int(time.time())}"
        container_worktree_path = f"/home/egg/.egg-worktrees/{container_id}/test-repo"

        # Create worktree
        resp = requests.post(
            f"{gateway_url}/api/v1/worktree/create",
            headers={"Authorization": f"Bearer {launcher_secret}"},
            json={
                "container_id": container_id,
                "repos": ["test-owner/test-repo"],
                "uid": 1000,
                "gid": 1000,
            },
            timeout=30,
        )
        assert resp.status_code == 200

        # Verify it exists
        is_valid, _ = verify_worktree_exists_and_valid(
            container_worktree_path,
            check_via_gateway_exec=True,
            gateway_container_name=gateway_container,
        )
        assert is_valid, "Worktree should exist after creation"

        # Delete worktree
        resp = requests.post(
            f"{gateway_url}/api/v1/worktree/delete",
            headers={"Authorization": f"Bearer {launcher_secret}"},
            json={"container_id": container_id, "force": True},
            timeout=30,
        )
        assert resp.status_code == 200

        # Verify it's gone
        result = subprocess.run(
            ["docker", "exec", gateway_container, "test", "-d", container_worktree_path],
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode != 0, "Worktree directory should not exist after deletion"


# ---------------------------------------------------------------------------
# SDLC Pipeline Worktree Tests (Phase 3)
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


def start_pipeline(orchestrator_url: str, pipeline_id: str) -> tuple[dict, int]:
    """POST to start a pipeline."""
    resp = requests.post(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/start",
        timeout=10,
    )
    return resp.json(), resp.status_code


class TestPipelineWorktreeSharing:
    """Test that pipeline containers properly share worktrees."""

    def test_files_created_in_pipeline_persist_across_phases(
        self, local_pipeline_stack: LocalPipelineStack
    ) -> None:
        """Files created in refine phase exist in plan phase (via mock sandbox drafts)."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        repos_dir = local_pipeline_stack.repos_dir

        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test worktree file persistence",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)
            assert final["data"]["status"] == "complete", f"Pipeline did not complete: {final}"

            # The mock sandbox creates draft files during refine and plan phases
            # These files should persist in the repo directory (mounted as worktree)
            drafts_dir = Path(repos_dir) / ".egg-state" / "drafts"

            # Check that refine phase created analysis.md
            analysis_file = drafts_dir / "analysis.md"
            assert analysis_file.exists(), (
                f"analysis.md should exist after refine phase: {drafts_dir}"
            )

            # Check that plan phase created plan.md
            plan_file = drafts_dir / "plan.md"
            assert plan_file.exists(), f"plan.md should exist after plan phase: {drafts_dir}"

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


class TestPipelineWorktreeCleanup:
    """Test that worktrees are cleaned up after pipeline completion/failure."""

    def test_worktree_cleanup_on_pipeline_completion(
        self, local_pipeline_stack: LocalPipelineStack
    ) -> None:
        """Worktrees are removed when pipeline reaches terminal state (complete)."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        gateway_url = local_pipeline_stack.gateway_url
        launcher_secret = local_pipeline_stack.launcher_secret

        data, status = create_pipeline(
            orchestrator_url,
            prompt="Test worktree cleanup on completion",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=360)
            assert final["data"]["status"] == "complete", f"Pipeline did not complete: {final}"

            # Wait a moment for cleanup to finish
            time.sleep(2)

            # List worktrees and verify none remain for this pipeline's containers
            resp = requests.get(
                f"{gateway_url}/api/v1/worktree/list",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                timeout=10,
            )
            assert resp.status_code == 200

            worktrees = resp.json().get("data", {}).get("worktrees", [])

            # Check no worktrees exist for this pipeline's containers
            # Pipeline containers are named egg-sandbox-{pipeline_id}-{role}
            pipeline_container_prefix = f"egg-sandbox-{pipeline_id}"
            for wt in worktrees:
                container_id = wt.get("container_id", "")
                assert not container_id.startswith(pipeline_container_prefix), (
                    f"Worktree for {container_id} should be cleaned up after pipeline completion"
                )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)

    def test_worktree_cleanup_on_pipeline_failure(
        self, local_pipeline_stack: LocalPipelineStack
    ) -> None:
        """Worktrees are removed even when pipeline fails."""
        orchestrator_url = local_pipeline_stack.orchestrator_url
        gateway_url = local_pipeline_stack.gateway_url
        launcher_secret = local_pipeline_stack.launcher_secret

        data, status = create_pipeline(
            orchestrator_url,
            prompt="FORCE_FAIL test worktree cleanup on failure",
            config={"hitl_gates": False},
        )
        assert status == 200
        pipeline_id = data["data"]["pipeline"]["id"]

        try:
            start_pipeline(orchestrator_url, pipeline_id)
            final = wait_for_pipeline_terminal(orchestrator_url, pipeline_id, timeout=300)
            assert final["data"]["status"] == "failed", f"Pipeline should have failed: {final}"

            # Wait a moment for cleanup to finish
            time.sleep(2)

            # List worktrees and verify none remain for this pipeline's containers
            resp = requests.get(
                f"{gateway_url}/api/v1/worktree/list",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                timeout=10,
            )
            assert resp.status_code == 200

            worktrees = resp.json().get("data", {}).get("worktrees", [])

            # Check no worktrees exist for this pipeline's containers
            pipeline_container_prefix = f"egg-sandbox-{pipeline_id}"
            for wt in worktrees:
                container_id = wt.get("container_id", "")
                assert not container_id.startswith(pipeline_container_prefix), (
                    f"Worktree for {container_id} should be cleaned up after pipeline failure"
                )

        finally:
            delete_pipeline(orchestrator_url, pipeline_id)


# ---------------------------------------------------------------------------
# Edge Cases and Regression Tests (Phase 4)
# ---------------------------------------------------------------------------


class TestWorktreeEdgeCases:
    """Test edge cases and regression scenarios from PR #569."""

    def test_worktree_not_empty_after_creation(
        self, local_pipeline_stack: LocalPipelineStack
    ) -> None:
        """Worktree contains expected git files after creation."""
        gateway_url = local_pipeline_stack.gateway_url
        launcher_secret = local_pipeline_stack.launcher_secret
        gateway_container = get_gateway_container_name(local_pipeline_stack.compose_project)

        container_id = f"test-not-empty-{int(time.time())}"
        container_worktree_path = f"/home/egg/.egg-worktrees/{container_id}/test-repo"

        try:
            resp = requests.post(
                f"{gateway_url}/api/v1/worktree/create",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                json={
                    "container_id": container_id,
                    "repos": ["test-owner/test-repo"],
                    "uid": 1000,
                    "gid": 1000,
                },
                timeout=30,
            )
            assert resp.status_code == 200, f"Worktree create failed: {resp.text}"

            # Check worktree has files (not empty)
            result = subprocess.run(
                ["docker", "exec", gateway_container, "ls", "-A", container_worktree_path],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 0
            files = result.stdout.strip().split("\n")
            assert len(files) > 0, "Worktree should not be empty"
            assert ".git" in files, "Worktree should contain .git file"

            # Verify .git is a file (not directory) with gitdir content
            git_path = f"{container_worktree_path}/.git"
            result = subprocess.run(
                ["docker", "exec", gateway_container, "cat", git_path],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 0
            assert "gitdir:" in result.stdout, ".git should contain gitdir pointer"

        finally:
            requests.post(
                f"{gateway_url}/api/v1/worktree/delete",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                json={"container_id": container_id, "force": True},
                timeout=30,
            )

    def test_worktree_not_root_owned(self, local_pipeline_stack: LocalPipelineStack) -> None:
        """Worktree is not owned by root (uid/gid != 0:0)."""
        gateway_url = local_pipeline_stack.gateway_url
        launcher_secret = local_pipeline_stack.launcher_secret
        gateway_container = get_gateway_container_name(local_pipeline_stack.compose_project)

        container_id = f"test-not-root-{int(time.time())}"
        container_worktree_path = f"/home/egg/.egg-worktrees/{container_id}/test-repo"

        try:
            resp = requests.post(
                f"{gateway_url}/api/v1/worktree/create",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                json={
                    "container_id": container_id,
                    "repos": ["test-owner/test-repo"],
                    "uid": 1000,
                    "gid": 1000,
                },
                timeout=30,
            )
            assert resp.status_code == 200, f"Worktree create failed: {resp.text}"

            # Check ownership is not root
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    gateway_container,
                    "stat",
                    "-c",
                    "%u:%g",
                    container_worktree_path,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 0
            ownership = result.stdout.strip()
            assert ownership != "0:0", f"Worktree should not be root-owned, got {ownership}"

        finally:
            requests.post(
                f"{gateway_url}/api/v1/worktree/delete",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                json={"container_id": container_id, "force": True},
                timeout=30,
            )

    def test_orphaned_worktree_cleanup_on_session_delete(
        self, local_pipeline_stack: LocalPipelineStack
    ) -> None:
        """Orphaned worktrees are cleaned up when session is deleted."""
        gateway_url = local_pipeline_stack.gateway_url
        launcher_secret = local_pipeline_stack.launcher_secret
        gateway_container = get_gateway_container_name(local_pipeline_stack.compose_project)

        # Detect the source IP the gateway sees
        health_resp = requests.get(f"{gateway_url}/api/v1/health", timeout=10)
        source_ip = health_resp.json().get("client_ip", "127.0.0.1")

        container_id = f"test-orphan-{int(time.time())}"
        container_worktree_path = f"/home/egg/.egg-worktrees/{container_id}/test-repo"

        # Create session (which creates worktree)
        session_resp = requests.post(
            f"{gateway_url}/api/v1/sessions/create",
            headers={"Authorization": f"Bearer {launcher_secret}"},
            json={
                "container_id": container_id,
                "container_ip": source_ip,
                "mode": "local",
                "repos": ["test-owner/test-repo"],
                "uid": 1000,
                "gid": 1000,
            },
            timeout=30,
        )
        assert session_resp.status_code == 200, f"Session create failed: {session_resp.text}"
        session_data = session_resp.json()
        session_token = session_data.get("data", {}).get("session_token")
        assert session_token

        # Verify worktree exists
        result = subprocess.run(
            ["docker", "exec", gateway_container, "test", "-d", container_worktree_path],
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, "Worktree should exist after session creation"

        # Delete session (should clean up worktree)
        delete_resp = requests.delete(
            f"{gateway_url}/api/v1/sessions/{session_token}",
            headers={"Authorization": f"Bearer {launcher_secret}"},
            timeout=30,
        )
        assert delete_resp.status_code == 200

        # Verify worktree is cleaned up
        result = subprocess.run(
            ["docker", "exec", gateway_container, "test", "-d", container_worktree_path],
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode != 0, "Worktree should be cleaned up after session deletion"

    def test_worktree_with_docker_precreated_git_directory(
        self, local_pipeline_stack: LocalPipelineStack
    ) -> None:
        """Worktree creation handles Docker pre-creating empty .git directory.

        When Docker creates a bind mount, it sometimes pre-creates the target
        directory (including subdirectories like .git). The worktree manager
        should handle this by removing the empty .git directory before creating
        the worktree.
        """
        gateway_url = local_pipeline_stack.gateway_url
        launcher_secret = local_pipeline_stack.launcher_secret
        gateway_container = get_gateway_container_name(local_pipeline_stack.compose_project)

        container_id = f"test-precreated-{int(time.time())}"
        container_worktree_path = f"/home/egg/.egg-worktrees/{container_id}/test-repo"

        try:
            # Pre-create the worktree directory with an empty .git directory
            # This simulates what Docker does when creating bind mount targets
            subprocess.run(
                [
                    "docker",
                    "exec",
                    gateway_container,
                    "mkdir",
                    "-p",
                    f"{container_worktree_path}/.git",
                ],
                capture_output=True,
                timeout=10,
                check=True,
            )

            # Verify the .git directory exists (as a directory, not file)
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    gateway_container,
                    "test",
                    "-d",
                    f"{container_worktree_path}/.git",
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 0, "Pre-created .git directory should exist"

            # Now create the worktree - this should succeed despite the pre-existing .git dir
            resp = requests.post(
                f"{gateway_url}/api/v1/worktree/create",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                json={
                    "container_id": container_id,
                    "repos": ["test-owner/test-repo"],
                    "uid": 1000,
                    "gid": 1000,
                },
                timeout=30,
            )
            assert resp.status_code == 200, f"Worktree create failed: {resp.text}"

            # Verify the worktree is valid (has .git file with gitdir content)
            is_valid, reason = verify_worktree_exists_and_valid(
                container_worktree_path,
                check_via_gateway_exec=True,
                gateway_container_name=gateway_container,
            )
            assert is_valid, f"Worktree should be valid after handling pre-created .git: {reason}"

            # Specifically verify .git is now a FILE (not directory)
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    gateway_container,
                    "test",
                    "-f",
                    f"{container_worktree_path}/.git",
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 0, ".git should be a file after worktree creation"

        finally:
            requests.post(
                f"{gateway_url}/api/v1/worktree/delete",
                headers={"Authorization": f"Bearer {launcher_secret}"},
                json={"container_id": container_id, "force": True},
                timeout=30,
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
