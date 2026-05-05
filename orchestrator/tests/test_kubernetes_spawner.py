"""
Tests for the KubernetesSpawner.

Covers Job spawning, gateway session integration, restart tracking,
pipeline cleanup, and error handling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from kubernetes_client import (
    DEFAULT_NAMESPACE,
    LABEL_AGENT_ROLE,
    LABEL_CONTAINER_NAME,
    LABEL_ORCHESTRATOR,
    LABEL_PIPELINE_ID,
    JobOperationError,
    KubernetesClientError,
    PodNotFoundError,
)
from models import AgentRole, ContainerInfo, ContainerStatus

# ---------------------------------------------------------------------------
# Fake gateway objects (avoid importing gateway_client directly)
# ---------------------------------------------------------------------------


@dataclass
class _FakeSessionInfo:
    session_token: str = "tok-abcdef123456"
    container_id: str = "job-coder"
    container_ip: str | None = None
    mode: str = "public"
    created_at: datetime = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    expires_at: datetime = datetime(2024, 1, 16, 12, 0, 0, tzinfo=UTC)


@dataclass
class _FakeGatewayHealth:
    healthy: bool = True
    status: str = "ok"
    version: str | None = "1.0.0"
    uptime_seconds: float | None = 3600.0
    error: str | None = None


@dataclass
class _FakeWorktreeResult:
    success: bool = True
    worktrees: dict = None  # type: ignore[assignment]
    errors: list = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.worktrees is None:
            self.worktrees = {"owner/repo": "/home/egg/.egg-worktrees/test/repo"}
        if self.errors is None:
            self.errors = []


class _FakeGatewayError(Exception):
    """Fake GatewayError for testing."""

    def __init__(self, message: str, status_code: int | None = None, details=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_k8s_client():
    """Create a mock KubernetesClient."""
    client = MagicMock()
    client.delete_job.side_effect = PodNotFoundError("No existing job")
    client.create_container.return_value = ContainerInfo(
        container_id="uid-abc123",
        container_name="egg-agent-pipe1-coder",
        job_name="egg-agent-pipe1-coder",
        namespace="egg-agents",
        status=ContainerStatus.PENDING,
    )
    client.stop_container.return_value = ContainerInfo(
        container_id="uid-abc123",
        container_name="egg-agent-pipe1-coder",
        status=ContainerStatus.EXITED,
    )
    client.remove_container.return_value = None
    client.list_containers.return_value = []
    return client


@pytest.fixture()
def mock_gateway():
    """Create a mock GatewayClient."""
    gw = MagicMock()
    gw.check_health.return_value = _FakeGatewayHealth()
    gw.register_session.return_value = _FakeSessionInfo()
    gw.delete_session.return_value = True
    gw.delete_session_by_container.return_value = True
    gw.create_worktrees.return_value = _FakeWorktreeResult()
    gw.delete_worktrees.return_value = _FakeWorktreeResult(worktrees={})
    return gw


@pytest.fixture()
def spawner(mock_k8s_client, mock_gateway):
    """Create a KubernetesSpawner with mock dependencies."""
    # Patch the gateway_client module's GatewayError so except clauses work
    with patch.dict(
        "sys.modules",
        {
            "gateway_client": MagicMock(
                GatewayClient=MagicMock,
                GatewayError=_FakeGatewayError,
                SessionInfo=_FakeSessionInfo,
                get_gateway_client=MagicMock(return_value=mock_gateway),
            ),
        },
    ):
        from kubernetes_spawner import KubernetesSpawner

        s = KubernetesSpawner(
            k8s_client=mock_k8s_client,
            gateway_client=mock_gateway,
            namespace="test-ns",
        )
        return s


@pytest.fixture()
def _patch_gateway_error():
    """Ensure GatewayError is importable for the spawner module."""
    import sys

    mod = sys.modules.get("gateway_client")
    if mod is None or not hasattr(mod, "GatewayError") or not isinstance(mod.GatewayError, type):
        mock_mod = MagicMock()
        mock_mod.GatewayError = _FakeGatewayError
        mock_mod.GatewayClient = MagicMock
        mock_mod.SessionInfo = _FakeSessionInfo
        mock_mod.get_gateway_client = MagicMock()
        sys.modules["gateway_client"] = mock_mod
    yield


# ---------------------------------------------------------------------------
# TestSpawnedContainer
# ---------------------------------------------------------------------------


class TestSpawnedContainer:
    """Test the SpawnedContainer dataclass."""

    def test_spawned_container_fields(self, spawner):
        """SpawnedContainer stores all required fields."""
        from kubernetes_spawner import SpawnedContainer

        info = ContainerInfo(container_id="uid-1", container_name="test")
        sc = SpawnedContainer(
            container_info=info,
            session_info=_FakeSessionInfo(),
            agent_role=AgentRole.CODER,
            pipeline_id="pipe-1",
            environment={"KEY": "val"},
        )
        assert sc.container_info is info
        assert sc.agent_role == AgentRole.CODER
        assert sc.pipeline_id == "pipe-1"
        assert sc.environment["KEY"] == "val"

    def test_spawned_container_no_session(self, spawner):
        """SpawnedContainer can have session_info=None."""
        from kubernetes_spawner import SpawnedContainer

        sc = SpawnedContainer(
            container_info=ContainerInfo(container_id="u", container_name="n"),
            session_info=None,
            agent_role=AgentRole.TESTER,
            pipeline_id="p2",
            environment={},
        )
        assert sc.session_info is None


# ---------------------------------------------------------------------------
# TestKubernetesSpawnerInit
# ---------------------------------------------------------------------------


class TestKubernetesSpawnerInit:
    """Test KubernetesSpawner initialization."""

    def test_init_with_clients(self, mock_k8s_client, mock_gateway):
        """Constructor accepts explicit clients."""
        from kubernetes_spawner import KubernetesSpawner

        s = KubernetesSpawner(
            k8s_client=mock_k8s_client,
            gateway_client=mock_gateway,
            namespace="custom-ns",
        )
        assert s._namespace == "custom-ns"
        assert s.k8s is mock_k8s_client
        assert s.gateway is mock_gateway

    def test_init_default_namespace(self, mock_k8s_client, mock_gateway):
        """Default namespace is DEFAULT_NAMESPACE."""
        from kubernetes_spawner import KubernetesSpawner

        s = KubernetesSpawner(
            k8s_client=mock_k8s_client,
            gateway_client=mock_gateway,
        )
        assert s._namespace == DEFAULT_NAMESPACE

    def test_empty_restart_counts(self, spawner):
        """Restart counts start empty."""
        assert spawner._restart_counts == {}


# ---------------------------------------------------------------------------
# Worktree allowlist validation
# ---------------------------------------------------------------------------


def test_roles_without_worktree_are_valid():
    """Every entry in _ROLES_WITHOUT_WORKTREE must be a real AgentRole."""
    from kubernetes_spawner import _ROLES_WITHOUT_WORKTREE

    assert _ROLES_WITHOUT_WORKTREE.issubset(set(AgentRole)), (
        f"Unknown roles in _ROLES_WITHOUT_WORKTREE: {_ROLES_WITHOUT_WORKTREE - set(AgentRole)}"
    )


def test_lens_reviewers_in_roles_without_worktree():
    """Lens reviewers must be exempt from the per-agent-worktree requirement.

    Regression for the egg-reviewer feedback on PR #2061: the lens reviewer
    roles (``REVIEWER_SECURITY``, ``REVIEWER_CONCURRENCY``) operate purely on
    the diff via the BRC consensus bus and never write code, so they belong
    in ``_ROLES_WITHOUT_WORKTREE`` alongside the other reviewer roles.
    Without this membership a spawn with ``repos=[]`` would raise
    ``KubernetesSpawnError`` and a spawn with a repo would provision an
    unnecessary worktree.
    """
    from kubernetes_spawner import _ROLES_WITHOUT_WORKTREE

    assert {AgentRole.REVIEWER_SECURITY, AgentRole.REVIEWER_CONCURRENCY}.issubset(
        _ROLES_WITHOUT_WORKTREE
    ), (
        "Lens reviewer roles (REVIEWER_SECURITY, REVIEWER_CONCURRENCY) must be "
        "in _ROLES_WITHOUT_WORKTREE — they review diffs via the BRC bus and do "
        "not need a per-agent git worktree."
    )


# ---------------------------------------------------------------------------
# TestSpawnAgentJob
# ---------------------------------------------------------------------------


class TestSpawnAgentJob:
    """Test spawn_agent_job method."""

    def test_basic_spawn(self, spawner, mock_k8s_client, mock_gateway):
        """Basic spawn creates a Job with gateway session."""
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
        )
        assert result.pipeline_id == "pipe-1"
        assert result.agent_role == AgentRole.CODER
        assert result.session_info is not None
        assert result.container_info.container_id == "uid-abc123"

        # Verify gateway health was checked
        mock_gateway.check_health.assert_called_once()

        # Verify session was registered
        mock_gateway.register_session.assert_called_once()
        call_kwargs = mock_gateway.register_session.call_args.kwargs
        assert call_kwargs["container_id"] == "egg-agent-pipe-1-coder"
        assert call_kwargs["container_ip"] is None  # Token-only
        assert call_kwargs["pipeline_id"] == "pipe-1"
        assert call_kwargs["agent_role"] == "coder"

        # Verify k8s job was created
        mock_k8s_client.create_container.assert_called_once()

    def test_spawn_sets_environment(self, spawner, mock_k8s_client, mock_gateway):
        """Spawn sets required environment variables."""
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-2",
            agent_role=AgentRole.TESTER,
            issue_number=42,
            phase="implement",
            branch="egg/issue-42",
            repos=["owner/repo"],
        )
        env = result.environment
        assert env["EGG_PIPELINE_ID"] == "pipe-2"
        assert env["EGG_AGENT_ROLE"] == "tester"
        assert env["EGG_ISSUE_NUMBER"] == "42"
        assert env["EGG_PHASE"] == "implement"
        assert env["EGG_BRANCH"] == "egg/issue-42"
        assert "EGG_SESSION_TOKEN" in env
        assert "GATEWAY_URL" in env
        assert "EGG_ORCHESTRATOR_URL" in env

    def test_spawn_extra_env_overrides(self, spawner, mock_k8s_client):
        """extra_env overrides default environment."""
        result = spawner.spawn_agent_job(
            pipeline_id="p",
            agent_role=AgentRole.CODER,
            extra_env={"EGG_AGENT_ROLE": "custom", "MY_KEY": "val"},
            repos=["owner/repo"],
        )
        assert result.environment["EGG_AGENT_ROLE"] == "custom"
        assert result.environment["MY_KEY"] == "val"

    def test_spawn_with_slice_id_sets_egg_slice_id_env(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """``slice_id=`` parameter propagates into ``EGG_SLICE_ID`` (#2410).

        The spawner's ``slice_id`` parameter previously only drove the Job
        name and worktree id; the agent container had no slice scope in
        its environment, so its BRC handlers couldn't tag CONSENSUS_*
        signals with the slice (failure mode #3 from #2410).
        """
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            slice_id="slice-2",
        )
        assert result.environment.get("EGG_SLICE_ID") == "slice-2"
        create_kwargs = mock_k8s_client.create_container.call_args.kwargs
        assert create_kwargs["environment"].get("EGG_SLICE_ID") == "slice-2"

    def test_spawn_without_slice_id_does_not_set_egg_slice_id(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """Pipeline-level spawns leave ``EGG_SLICE_ID`` unset."""
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
        )
        assert "EGG_SLICE_ID" not in result.environment

    def test_extra_env_cannot_override_egg_slice_id(self, spawner, mock_k8s_client, mock_gateway):
        """``extra_env`` cannot override ``EGG_SLICE_ID`` — protected key (#2410 v2 review).

        The spawner is the single source of truth: ``EGG_SLICE_ID`` is
        derived from the ``slice_id`` parameter that already drives Job
        naming and worktree id. A future caller that tried to ship a
        different value via ``extra_env`` would silently end up with the
        agent's signals tagged for one slice while its Job + worktree
        belong to another. Protecting the key catches that.
        """
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            slice_id="slice-2",
            extra_env={"EGG_SLICE_ID": "slice-99"},
        )
        # Spawner's value wins, not extra_env's.
        assert result.environment.get("EGG_SLICE_ID") == "slice-2"
        create_kwargs = mock_k8s_client.create_container.call_args.kwargs
        assert create_kwargs["environment"].get("EGG_SLICE_ID") == "slice-2"

    def test_extra_env_cannot_inject_egg_slice_id_when_pipeline_level(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """Without ``slice_id``, ``extra_env`` cannot smuggle ``EGG_SLICE_ID`` in.

        Pipeline-level spawns must not be tagged with a slice scope —
        protecting the key blocks a regression where a slice-aware
        caller would forget the ``slice_id`` parameter and try to bolt
        the env var on directly via ``extra_env``.
        """
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            extra_env={"EGG_SLICE_ID": "slice-2"},
        )
        assert "EGG_SLICE_ID" not in result.environment
        create_kwargs = mock_k8s_client.create_container.call_args.kwargs
        assert "EGG_SLICE_ID" not in create_kwargs["environment"]

    def test_spawn_labels(self, spawner, mock_k8s_client):
        """Spawn sets the expected labels on the Job."""
        spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            issue_number=99,
            repos=["owner/repo"],
        )
        call_kwargs = mock_k8s_client.create_container.call_args.kwargs
        labels = call_kwargs["labels"]
        assert labels[LABEL_ORCHESTRATOR] == "true"
        assert labels[LABEL_PIPELINE_ID] == "pipe-1"
        assert labels[LABEL_AGENT_ROLE] == "coder"
        assert labels[LABEL_CONTAINER_NAME] == "egg-agent-pipe-1-coder"
        assert labels["egg.issue.number"] == "99"

    def test_spawn_without_gateway_wait(self, spawner, mock_gateway):
        """wait_for_gateway=False skips health check."""
        spawner.spawn_agent_job(
            pipeline_id="p",
            agent_role=AgentRole.CODER,
            wait_for_gateway=False,
            repos=["owner/repo"],
        )
        mock_gateway.check_health.assert_not_called()

    def test_spawn_unhealthy_gateway_raises(self, spawner, mock_gateway):
        """Spawn raises when gateway is unhealthy."""
        from kubernetes_spawner import KubernetesSpawnError

        mock_gateway.check_health.return_value = _FakeGatewayHealth(
            healthy=False, status="down", error="connection refused"
        )
        with pytest.raises(KubernetesSpawnError, match="Gateway is not healthy"):
            spawner.spawn_agent_job(
                pipeline_id="p",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
            )

    def test_spawn_cleans_existing_job(self, spawner, mock_k8s_client):
        """Spawn deletes any existing Job with the same name."""
        mock_k8s_client.delete_job.side_effect = None  # Simulate success
        spawner.spawn_agent_job(
            pipeline_id="p",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
        )
        mock_k8s_client.delete_job.assert_called_once_with(
            "egg-sandbox-egg-agent-p-coder", "test-ns"
        )

    def test_spawn_with_repos_creates_worktrees(self, spawner, mock_gateway):
        """Spawn creates worktrees when repos are provided."""
        spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
        )
        mock_gateway.create_worktrees.assert_called_once()
        call_kwargs = mock_gateway.create_worktrees.call_args.kwargs
        assert call_kwargs["container_id"] == "pipe-1-coder"
        assert call_kwargs["repos"] == ["owner/repo"]

    def test_spawn_passes_worktree_container_id_to_register_session(self, spawner, mock_gateway):
        """register_session must receive worktree_container_id=agent_worktree_id.

        Regression for #1857: without this, the gateway created a second
        worktree under the k8s job_name and raced on .git/config.lock with
        concurrent spawns, intermittently killing one agent per phase.
        """
        spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
        )
        register_kwargs = mock_gateway.register_session.call_args.kwargs
        # container_id is still the job name (used for session identity).
        assert register_kwargs["container_id"] == "egg-agent-pipe-1-coder"
        # But the worktree comes from the earlier create_worktrees call.
        assert register_kwargs["worktree_container_id"] == "pipe-1-coder"

    def test_spawn_without_repos_omits_worktree_container_id(self, spawner, mock_gateway):
        """Review-only spawns skip worktree creation entirely — passing a
        worktree_container_id would force the gateway to look up a
        worktree that was never made.

        Uses ``REVIEWER_CODE`` because producers with empty ``repos`` are
        now rejected at spawn time (#1869).
        """
        spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.REVIEWER_CODE,
        )
        register_kwargs = mock_gateway.register_session.call_args.kwargs
        assert register_kwargs.get("worktree_container_id") is None

    def test_spawn_producer_without_repos_raises(self, spawner, monkeypatch):
        """Spawning a producer role with empty ``repos`` now raises.

        Regression guard for #1869: previously the container came up
        without a worktree and burned tokens retrying git against a
        gateway that kept returning "Worktree not found" — the
        pipeline stalled until a human cancelled.
        """
        import kubernetes_spawner
        from kubernetes_spawner import KubernetesSpawnError

        # Undo the conftest autouse stub for this regression test.
        monkeypatch.setattr(
            kubernetes_spawner,
            "_role_needs_worktree",
            lambda role: role not in kubernetes_spawner._ROLES_WITHOUT_WORKTREE,
        )

        with pytest.raises(KubernetesSpawnError, match="no repos provided"):
            spawner.spawn_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
            )

    def test_spawn_missing_worktree_on_disk_raises(self, spawner, monkeypatch):
        """Spawn fails when the worktree disappears between creation and Job start.

        Simulates the race where ``create_worktrees`` returns success but
        a concurrent ``cleanup_pipeline`` wipes the directory before the
        Job can start — surfaced now so producers don't silently burn
        tokens on a missing worktree (#1869).
        """
        from kubernetes_spawner import KubernetesSpawner, KubernetesSpawnError

        # The conftest autouse fixture stubs _find_missing_worktrees to
        # return empty.  Re-patch here so the check reports the worktree
        # as missing — simulating a concurrent cleanup race.
        monkeypatch.setattr(
            KubernetesSpawner,
            "_find_missing_worktrees",
            lambda self, agent_worktree_id, repos: [
                f"/home/egg/.egg-worktrees/{agent_worktree_id}/repo"
            ],
        )

        with pytest.raises(KubernetesSpawnError, match="missing at spawn time"):
            spawner.spawn_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
            )

    def test_spawn_reviewer_without_repos_succeeds(self, spawner, monkeypatch):
        """Reviewer roles can spawn without ``repos`` — they never do git."""
        import kubernetes_spawner

        # Undo the conftest autouse stub so the real guard runs.
        monkeypatch.setattr(
            kubernetes_spawner,
            "_role_needs_worktree",
            lambda role: role not in kubernetes_spawner._ROLES_WITHOUT_WORKTREE,
        )
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.REVIEWER_CONTRACT,
        )
        assert result.agent_role == AgentRole.REVIEWER_CONTRACT

    def test_spawn_worktree_failure_raises(self, spawner, mock_gateway):
        """Spawn raises when worktree creation fails."""
        from kubernetes_spawner import KubernetesSpawnError

        mock_gateway.create_worktrees.return_value = _FakeWorktreeResult(
            success=False, worktrees={}, errors=["clone failed"]
        )
        with pytest.raises(KubernetesSpawnError, match="worktree creation returned no worktrees"):
            spawner.spawn_agent_job(
                pipeline_id="p",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
            )

    def test_spawn_k8s_error_cleans_session(self, spawner, mock_k8s_client, mock_gateway):
        """K8s error during spawn cleans up gateway session."""
        from kubernetes_spawner import KubernetesSpawnError

        mock_k8s_client.create_container.side_effect = KubernetesClientError("API error")
        with pytest.raises(KubernetesSpawnError, match="Failed to spawn Job"):
            spawner.spawn_agent_job(
                pipeline_id="p",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
            )
        mock_gateway.delete_session.assert_called_once_with("tok-abcdef123456")

    def test_spawn_default_branch_from_pipeline(self, spawner):
        """Without branch, defaults to egg/{pipeline_id}/work."""
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-5",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
        )
        assert result.environment["EGG_BRANCH"] == "egg/pipe-5/work"

    def test_spawn_custom_image(self, spawner, mock_k8s_client):
        """Spawn uses custom image when provided."""
        spawner.spawn_agent_job(
            pipeline_id="p",
            agent_role=AgentRole.CODER,
            image="custom-image:v2",
            repos=["owner/repo"],
        )
        call_kwargs = mock_k8s_client.create_container.call_args.kwargs
        assert call_kwargs["image"] == "custom-image:v2"


# ---------------------------------------------------------------------------
# TestSpawnRetry (#1839)
# ---------------------------------------------------------------------------


class TestSpawnRetry:
    """Test bounded retry behavior for transient worktree-creation failures."""

    def test_transient_then_success(self, spawner, mock_gateway):
        """A transient failure on attempt 1 is retried; attempt 2 succeeds."""
        mock_gateway.create_worktrees.side_effect = [
            _FakeGatewayError("Timed out fetching refs", status_code=504),
            _FakeWorktreeResult(),
        ]
        with patch("kubernetes_spawner.time.sleep") as mock_sleep:
            result = spawner.spawn_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
                spawn_max_retries=2,
                spawn_retry_initial_backoff_seconds=0.01,
            )
        assert result.pipeline_id == "pipe-1"
        assert mock_gateway.create_worktrees.call_count == 2
        mock_sleep.assert_called_once()

    def test_all_attempts_fail_raises_with_attempt_count(self, spawner, mock_gateway):
        """When all attempts fail, the final error names the attempt count."""
        from kubernetes_spawner import KubernetesSpawnError

        mock_gateway.create_worktrees.side_effect = _FakeGatewayError(
            "Timed out fetching refs", status_code=504
        )
        with (
            patch("kubernetes_spawner.time.sleep"),
            pytest.raises(KubernetesSpawnError, match=r"after 3 attempt\(s\)"),
        ):
            spawner.spawn_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
                spawn_max_retries=2,
                spawn_retry_initial_backoff_seconds=0.01,
            )
        assert mock_gateway.create_worktrees.call_count == 3

    def test_permanent_failure_fails_fast(self, spawner, mock_gateway):
        """Permanent failures (404/422) are not retried."""
        from kubernetes_spawner import KubernetesSpawnError

        mock_gateway.create_worktrees.side_effect = _FakeGatewayError(
            "Repository not found", status_code=404
        )
        with (
            patch("kubernetes_spawner.time.sleep") as mock_sleep,
            pytest.raises(KubernetesSpawnError, match=r"after 1 attempt\(s\)"),
        ):
            spawner.spawn_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
                spawn_max_retries=2,
                spawn_retry_initial_backoff_seconds=0.01,
            )
        assert mock_gateway.create_worktrees.call_count == 1
        mock_sleep.assert_not_called()

    def test_max_retries_zero_disables_retry(self, spawner, mock_gateway):
        """spawn_max_retries=0 gives the pre-#1839 single-attempt behavior."""
        from kubernetes_spawner import KubernetesSpawnError

        mock_gateway.create_worktrees.side_effect = _FakeGatewayError(
            "Timed out fetching refs", status_code=504
        )
        with (
            patch("kubernetes_spawner.time.sleep") as mock_sleep,
            pytest.raises(KubernetesSpawnError, match=r"after 1 attempt\(s\)"),
        ):
            spawner.spawn_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
                spawn_max_retries=0,
            )
        assert mock_gateway.create_worktrees.call_count == 1
        mock_sleep.assert_not_called()

    def test_connection_failure_no_status_code_is_transient(self, spawner, mock_gateway):
        """GatewayError with status_code=None classifies as transient."""
        mock_gateway.create_worktrees.side_effect = [
            _FakeGatewayError("Failed to connect to gateway", status_code=None),
            _FakeWorktreeResult(),
        ]
        with patch("kubernetes_spawner.time.sleep"):
            result = spawner.spawn_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
                spawn_max_retries=2,
                spawn_retry_initial_backoff_seconds=0.01,
            )
        assert result.pipeline_id == "pipe-1"
        assert mock_gateway.create_worktrees.call_count == 2

    def test_empty_worktree_result_not_retried(self, spawner, mock_gateway):
        """A successful-looking response with no worktrees is treated as permanent."""
        from kubernetes_spawner import KubernetesSpawnError

        mock_gateway.create_worktrees.return_value = _FakeWorktreeResult(
            success=True, worktrees={}, errors=["no repos matched"]
        )
        with (
            patch("kubernetes_spawner.time.sleep") as mock_sleep,
            pytest.raises(KubernetesSpawnError, match="no worktrees"),
        ):
            spawner.spawn_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
                spawn_max_retries=2,
                spawn_retry_initial_backoff_seconds=0.01,
            )
        assert mock_gateway.create_worktrees.call_count == 1
        mock_sleep.assert_not_called()

    def test_backoff_scales_between_attempts(self, spawner, mock_gateway):
        """Backoff grows between retries rather than staying flat."""
        mock_gateway.create_worktrees.side_effect = [
            _FakeGatewayError("Timed out", status_code=504),
            _FakeGatewayError("Timed out", status_code=504),
            _FakeWorktreeResult(),
        ]
        with patch("kubernetes_spawner.time.sleep") as mock_sleep:
            spawner.spawn_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
                spawn_max_retries=2,
                spawn_retry_initial_backoff_seconds=0.01,
            )
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert len(delays) == 2
        assert delays[1] > delays[0]


# ---------------------------------------------------------------------------
# TestIsTransientSpawnFailure (#1839)
# ---------------------------------------------------------------------------


class TestIsTransientSpawnFailure:
    """Test classification of spawn failures.

    Uses the ``spawner`` fixture (even though not needed directly) so that
    ``kubernetes_spawner.GatewayError`` is bound to ``_FakeGatewayError``
    before the classifier runs.
    """

    def test_transient_status_codes(self, spawner):
        from kubernetes_spawner import _is_transient_spawn_failure

        for code in (408, 429, 500, 502, 503, 504):
            err = _FakeGatewayError("x", status_code=code)
            assert _is_transient_spawn_failure(err) is True, f"status {code}"

    def test_permanent_status_codes(self, spawner):
        from kubernetes_spawner import _is_transient_spawn_failure

        for code in (400, 401, 403, 404, 422):
            err = _FakeGatewayError("x", status_code=code)
            assert _is_transient_spawn_failure(err) is False, f"status {code}"

    def test_repository_not_found_is_permanent(self, spawner):
        """'Repository not found' trumps status_code heuristics."""
        from kubernetes_spawner import _is_transient_spawn_failure

        err = _FakeGatewayError("Repository not found", status_code=500)
        assert _is_transient_spawn_failure(err) is False

    def test_no_status_code_transient_message(self, spawner):
        from kubernetes_spawner import _is_transient_spawn_failure

        err = _FakeGatewayError("Failed to connect", status_code=None)
        assert _is_transient_spawn_failure(err) is True

    def test_no_status_code_unknown_message_is_transient(self, spawner):
        """Unknown error with no status code defaults to transient per #1839."""
        from kubernetes_spawner import _is_transient_spawn_failure

        err = _FakeGatewayError("mystery gateway error", status_code=None)
        assert _is_transient_spawn_failure(err) is True

    def test_unknown_http_status_is_permanent(self, spawner):
        """An HTTP status we don't know is treated as permanent (fail fast)."""
        from kubernetes_spawner import _is_transient_spawn_failure

        err = _FakeGatewayError("weird", status_code=418)
        assert _is_transient_spawn_failure(err) is False

    def test_non_gateway_exception_is_transient(self, spawner):
        from kubernetes_spawner import _is_transient_spawn_failure

        assert _is_transient_spawn_failure(OSError("socket timeout")) is True

    def test_classify_agrees_with_is_transient_on_permanent_message_with_transient_status(
        self, spawner
    ):
        """_classify_spawn_error must return 'permanent_message' when the message
        contains a permanent fragment, even if the status code is transient (e.g. 500).
        This ensures the logged error_category agrees with the retry decision."""
        from kubernetes_spawner import _classify_spawn_error, _is_transient_spawn_failure

        err = _FakeGatewayError("Repository not found", status_code=500)
        assert _is_transient_spawn_failure(err) is False
        assert _classify_spawn_error(err) == "permanent_message"


# ---------------------------------------------------------------------------
# TestStopAgentJob
# ---------------------------------------------------------------------------


class TestStopAgentJob:
    """Test stop_agent_job method."""

    def test_stop_job(self, spawner, mock_k8s_client, mock_gateway):
        """Stop delegates to k8s and cleans up session."""
        result = spawner.stop_agent_job("job-name")
        mock_k8s_client.stop_container.assert_called_once_with("job-name", timeout=10)
        mock_gateway.delete_session_by_container.assert_called_once_with("job-name")
        assert result.status == ContainerStatus.EXITED

    def test_stop_job_skip_session(self, spawner, mock_k8s_client, mock_gateway):
        """Stop can skip session cleanup."""
        spawner.stop_agent_job("job-name", cleanup_session=False)
        mock_gateway.delete_session_by_container.assert_not_called()

    def test_stop_not_found_cleans_session(self, spawner, mock_k8s_client, mock_gateway):
        """Stop cleans up session even when Job is not found."""
        mock_k8s_client.stop_container.side_effect = PodNotFoundError("gone")
        with pytest.raises(PodNotFoundError):
            spawner.stop_agent_job("job-name")
        mock_gateway.delete_session_by_container.assert_called_once_with("job-name")


# ---------------------------------------------------------------------------
# TestRemoveAgentJob
# ---------------------------------------------------------------------------


class TestRemoveAgentJob:
    """Test remove_agent_job method."""

    def test_remove_job(self, spawner, mock_k8s_client, mock_gateway):
        """Remove delegates to k8s and cleans up session."""
        spawner.remove_agent_job("job-name")
        mock_k8s_client.remove_container.assert_called_once_with("job-name", force=False)
        mock_gateway.delete_session_by_container.assert_called_once_with("job-name")

    def test_remove_force(self, spawner, mock_k8s_client):
        """Remove passes force flag."""
        spawner.remove_agent_job("job-name", force=True)
        mock_k8s_client.remove_container.assert_called_once_with("job-name", force=True)

    def test_remove_cleans_session_on_k8s_error(self, spawner, mock_k8s_client, mock_gateway):
        """Session cleanup happens even if k8s removal fails."""
        mock_k8s_client.remove_container.side_effect = JobOperationError("API error")
        with pytest.raises(JobOperationError):
            spawner.remove_agent_job("job-name")
        # Session cleanup still happened (finally block)
        mock_gateway.delete_session_by_container.assert_called_once_with("job-name")


# ---------------------------------------------------------------------------
# TestListPipelineJobs
# ---------------------------------------------------------------------------


class TestListPipelineJobs:
    """Test list_pipeline_jobs method."""

    def test_list_jobs(self, spawner, mock_k8s_client):
        """list_pipeline_jobs delegates to k8s with correct labels."""
        mock_k8s_client.list_containers.return_value = [
            ContainerInfo(container_id="u1", container_name="j1"),
        ]
        result = spawner.list_pipeline_jobs("pipe-1")
        mock_k8s_client.list_containers.assert_called_once_with(
            labels={LABEL_PIPELINE_ID: "pipe-1"},
        )
        assert len(result) == 1

    def test_list_jobs_empty(self, spawner, mock_k8s_client):
        """list_pipeline_jobs returns empty list when no Jobs."""
        result = spawner.list_pipeline_jobs("nonexistent")
        assert result == []


# ---------------------------------------------------------------------------
# TestCleanupPipeline
# ---------------------------------------------------------------------------


class TestCleanupPipeline:
    """Test cleanup_pipeline method."""

    def test_cleanup_removes_jobs(self, spawner, mock_k8s_client, mock_gateway):
        """cleanup_pipeline removes all Jobs for a pipeline."""
        mock_k8s_client.list_containers.return_value = [
            ContainerInfo(
                container_id="u1",
                container_name="j1",
                job_name="egg-agent-pipe-1-coder",
            ),
            ContainerInfo(
                container_id="u2",
                container_name="j2",
                job_name="egg-agent-pipe-1-tester",
            ),
        ]
        removed = spawner.cleanup_pipeline("pipe-1")
        assert removed == 2
        assert mock_k8s_client.remove_container.call_count == 2

    def test_cleanup_handles_errors(self, spawner, mock_k8s_client):
        """cleanup_pipeline continues when removal fails."""
        mock_k8s_client.list_containers.return_value = [
            ContainerInfo(container_id="u1", container_name="j1", job_name="j1"),
        ]
        mock_k8s_client.remove_container.side_effect = JobOperationError("fail")
        removed = spawner.cleanup_pipeline("pipe-1")
        assert removed == 0  # Failed to remove

    def test_cleanup_empty_pipeline(self, spawner, mock_k8s_client):
        """cleanup_pipeline returns 0 for empty pipeline."""
        removed = spawner.cleanup_pipeline("empty-pipe")
        assert removed == 0


# ---------------------------------------------------------------------------
# TestRestartAgentJob
# ---------------------------------------------------------------------------


class TestRestartAgentJob:
    """Test restart_agent_job method."""

    def test_restart_increments_count(self, spawner, mock_k8s_client, mock_gateway):
        """Restart increments the restart counter."""
        result = spawner.restart_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
        )
        assert spawner.get_restart_count("pipe-1", "coder") == 1
        assert result.pipeline_id == "pipe-1"

    def test_restart_limit_exceeded(self, spawner):
        """Restart raises when limit is exceeded."""
        from kubernetes_spawner import KubernetesSpawnError

        spawner._restart_counts[("pipe-1", "coder", None)] = 2
        with pytest.raises(KubernetesSpawnError, match="Restart limit.*exceeded"):
            spawner.restart_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                max_restarts=2,
            )

    def test_restart_removes_existing(self, spawner, mock_k8s_client):
        """Restart deletes the existing Job before respawning."""
        spawner.restart_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
        )
        mock_k8s_client.delete_job.assert_called()

    def test_restart_preserves_worktree(self, spawner, mock_k8s_client):
        """Restart calls spawn_agent_job with preserve_worktree_on_failure=True."""
        # We can verify indirectly — the spawn should NOT clean up worktrees on error
        spawner.restart_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
        )
        assert spawner.get_restart_count("pipe-1", "coder") == 1

    @pytest.mark.parametrize(
        "role",
        [
            AgentRole.TASK_PLANNER,
            AgentRole.RISK_ANALYST,
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_CONTRACT,
            AgentRole.REVIEWER_AGENT_DESIGN,
            AgentRole.REVIEWER_REFINE,
            AgentRole.REVIEWER_PLAN,
            AgentRole.CONFLICT_RESOLVER,
        ],
    )
    def test_restart_underscore_roles_use_hyphenated_k8s_name(
        self, spawner, mock_k8s_client, mock_gateway, role
    ):
        """Restart must convert underscored roles to hyphenated k8s names (#2070).

        K8s resource names are RFC-1123 labels and reject underscores, so a
        role like ``task_planner`` must become ``task-planner`` in the Job
        name. Independently, the call site must pass the prefixed
        ``egg-sandbox-`` name to ``delete_job`` (the actual k8s name) and
        the unprefixed name to ``delete_session_by_container`` (which is
        what the gateway session was registered under).
        """
        spawner.restart_agent_job(
            pipeline_id="issue-1962",
            agent_role=role,
            repos=["owner/repo"],
        )

        hyphen_role = role.value.replace("_", "-")
        unprefixed = f"egg-agent-issue-1962-{hyphen_role}"
        prefixed = f"egg-sandbox-{unprefixed}"

        # k8s deletion uses the prefixed Job name.
        delete_call = mock_k8s_client.delete_job.call_args_list[0]
        assert delete_call.args[0] == prefixed
        # No raw underscore must reach the k8s API call.
        assert "_" not in delete_call.args[0]

        # Gateway session cleanup uses the unprefixed name (matches what
        # spawn_agent_job registered with).
        gw_call = mock_gateway.delete_session_by_container.call_args_list[0]
        assert gw_call.args[0] == unprefixed
        assert "_" not in gw_call.args[0]


# ---------------------------------------------------------------------------
# TestRestartCounts
# ---------------------------------------------------------------------------


class TestRestartCounts:
    """Test restart count tracking."""

    def test_get_restart_count_default(self, spawner):
        """Default restart count is 0."""
        assert spawner.get_restart_count("pipe-1", "coder") == 0

    def test_reset_restart_counts(self, spawner):
        """reset_restart_counts clears all counts for a pipeline."""
        spawner._restart_counts[("pipe-1", "coder", None)] = 3
        spawner._restart_counts[("pipe-1", "tester", None)] = 1
        spawner._restart_counts[("pipe-2", "coder", None)] = 2

        spawner.reset_restart_counts("pipe-1")

        assert spawner.get_restart_count("pipe-1", "coder") == 0
        assert spawner.get_restart_count("pipe-1", "tester") == 0
        assert spawner.get_restart_count("pipe-2", "coder") == 2  # Unaffected


# ---------------------------------------------------------------------------
# TestDetectUncommittedChanges
# ---------------------------------------------------------------------------


class TestDetectUncommittedChanges:
    """Test detect_uncommitted_changes method."""

    def test_no_worktree_directory(self, spawner, tmp_path):
        """Returns None when worktree directory doesn't exist."""
        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path / "nonexistent"):
            result = spawner.detect_uncommitted_changes("pipe-1", "coder")
        assert result is None

    def test_detects_changes(self, spawner, tmp_path):
        """Detects uncommitted changes in the worktree."""
        worktree_dir = tmp_path / "pipe-1-coder" / "owner-repo"
        worktree_dir.mkdir(parents=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=" M file1.py\n?? file2.py\n",
            )
            result = spawner.detect_uncommitted_changes("pipe-1", "coder")

        assert result is not None
        assert result["pipeline_id"] == "pipe-1"
        assert result["agent_role"] == "coder"
        assert result["file_count"] == 2

    def test_no_changes(self, spawner, tmp_path):
        """Returns None when no uncommitted changes."""
        worktree_dir = tmp_path / "pipe-1-coder" / "owner-repo"
        worktree_dir.mkdir(parents=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = spawner.detect_uncommitted_changes("pipe-1", "coder")

        assert result is None


# ---------------------------------------------------------------------------
# TestCreateConcurrentSpawnFn
# ---------------------------------------------------------------------------


class TestCreateConcurrentSpawnFn:
    """Test create_concurrent_spawn_fn method."""

    def test_returns_callable(self, spawner):
        """create_concurrent_spawn_fn returns a callable."""
        fn = spawner.create_concurrent_spawn_fn(
            pipeline_id="p",
            issue_number=1,
            repo_volumes=None,
            mode="public",
            repos=None,
            phase="implement",
        )
        assert callable(fn)

    def test_spawn_fn_delegates(self, spawner, mock_k8s_client, mock_gateway):
        """The returned callable delegates to spawn_agent_job."""
        fn = spawner.create_concurrent_spawn_fn(
            pipeline_id="pipe-1",
            issue_number=42,
            repo_volumes=None,
            mode="public",
            repos=["owner/repo"],
            phase="implement",
        )
        result = fn(AgentRole.CODER, branch="egg/issue-42")
        assert result.pipeline_id == "pipe-1"
        assert result.agent_role == AgentRole.CODER

    def test_spawn_fn_merges_env(self, spawner, mock_k8s_client, mock_gateway):
        """The returned callable merges sandbox_env and extra_env."""
        fn = spawner.create_concurrent_spawn_fn(
            pipeline_id="p",
            issue_number=1,
            repo_volumes=None,
            mode="public",
            repos=["owner/repo"],
            phase="implement",
            sandbox_env={"BASE_KEY": "base_val"},
        )
        result = fn(AgentRole.TESTER, extra_env={"EXTRA_KEY": "extra_val"})
        assert result.environment["BASE_KEY"] == "base_val"
        assert result.environment["EXTRA_KEY"] == "extra_val"


# ---------------------------------------------------------------------------
# TestKubernetesSpawnError
# ---------------------------------------------------------------------------


class TestKubernetesSpawnError:
    """Test KubernetesSpawnError exception."""

    def test_is_exception(self):
        """KubernetesSpawnError is a standard Exception."""
        from kubernetes_spawner import KubernetesSpawnError

        assert issubclass(KubernetesSpawnError, Exception)

    def test_message_preserved(self):
        """Exception message is preserved."""
        from kubernetes_spawner import KubernetesSpawnError

        err = KubernetesSpawnError("spawn failed")
        assert str(err) == "spawn failed"


# ---------------------------------------------------------------------------
# TestGetKubernetesSpawner
# ---------------------------------------------------------------------------


class TestGetKubernetesSpawner:
    """Test get_kubernetes_spawner singleton."""

    def test_returns_spawner(self):
        """get_kubernetes_spawner returns a KubernetesSpawner."""
        # Reset singleton
        import kubernetes_spawner
        from kubernetes_spawner import KubernetesSpawner, get_kubernetes_spawner

        kubernetes_spawner._spawner = None

        with patch.object(KubernetesSpawner, "__init__", return_value=None):
            result = get_kubernetes_spawner()
            assert isinstance(result, KubernetesSpawner)

        # Clean up
        kubernetes_spawner._spawner = None

    def test_singleton_reuses_instance(self):
        """Repeated calls return the same instance."""
        import kubernetes_spawner
        from kubernetes_spawner import KubernetesSpawner, get_kubernetes_spawner

        kubernetes_spawner._spawner = None

        with patch.object(KubernetesSpawner, "__init__", return_value=None):
            first = get_kubernetes_spawner()
            second = get_kubernetes_spawner()
            assert first is second

        kubernetes_spawner._spawner = None


# ---------------------------------------------------------------------------
# Slice-scope plumbing (#2403)
# ---------------------------------------------------------------------------


class TestSliceScopedJobAndWorktreeIds:
    """Concurrent slices in the same pipeline must spawn under distinct ids.

    Without slice scope, slice-N's coder spawn:
      * builds the same Job name as slice-(N-1)'s coder, so the
        pre-spawn cleanup at the top of ``spawn_agent_job`` deletes the
        sibling slice's still-running Job;
      * builds the same ``agent_worktree_id`` so the gateway worktree
        is reused, mounting slice-(N-1)'s contents (or stepping on
        them mid-flight).
    Both bugs surfaced together in #2403.
    """

    def test_build_k8s_job_names_includes_slice_segment(self):
        from kubernetes_spawner import KubernetesSpawner

        job_name, k8s_name = KubernetesSpawner._build_k8s_job_names(
            "issue-2261-v7", AgentRole.CODER, slice_id="slice-2"
        )
        assert job_name == "egg-agent-issue-2261-v7-slice-2-coder"
        assert k8s_name.endswith("egg-agent-issue-2261-v7-slice-2-coder")

    def test_build_k8s_job_names_omits_slice_segment_when_unscoped(self):
        from kubernetes_spawner import KubernetesSpawner

        job_name, _ = KubernetesSpawner._build_k8s_job_names("issue-2261-v7", AgentRole.CODER)
        assert job_name == "egg-agent-issue-2261-v7-coder"

    def test_build_agent_worktree_id_includes_slice(self):
        from kubernetes_spawner import KubernetesSpawner

        wt_id = KubernetesSpawner._build_agent_worktree_id(
            "issue-2261-v7", AgentRole.CODER, slice_id="slice-2"
        )
        assert wt_id == "issue-2261-v7-slice-2-coder"

    def test_build_agent_worktree_id_omits_slice_when_unscoped(self):
        from kubernetes_spawner import KubernetesSpawner

        wt_id = KubernetesSpawner._build_agent_worktree_id("issue-2261-v7", AgentRole.CODER)
        assert wt_id == "issue-2261-v7-coder"

    def test_concurrent_slices_get_distinct_ids(self):
        """Two slice spawns for the same role must NOT collide on either id."""
        from kubernetes_spawner import KubernetesSpawner

        s1_job, _ = KubernetesSpawner._build_k8s_job_names(
            "issue-2261-v7", AgentRole.CODER, slice_id="slice-1"
        )
        s2_job, _ = KubernetesSpawner._build_k8s_job_names(
            "issue-2261-v7", AgentRole.CODER, slice_id="slice-2"
        )
        assert s1_job != s2_job

        s1_wt = KubernetesSpawner._build_agent_worktree_id(
            "issue-2261-v7", AgentRole.CODER, slice_id="slice-1"
        )
        s2_wt = KubernetesSpawner._build_agent_worktree_id(
            "issue-2261-v7", AgentRole.CODER, slice_id="slice-2"
        )
        assert s1_wt != s2_wt

    def test_underscore_role_still_hyphenated_in_job_name(self):
        """``task_planner`` etc. stay hyphenated under slice scope (RFC-1123)."""
        from kubernetes_spawner import KubernetesSpawner

        job_name, _ = KubernetesSpawner._build_k8s_job_names(
            "issue-2261-v7", AgentRole.TASK_PLANNER, slice_id="slice-3"
        )
        assert job_name == "egg-agent-issue-2261-v7-slice-3-task-planner"


class TestSpawnAgentJobSliceScope:
    """``spawn_agent_job`` threads ``slice_id`` into the gateway worktree key."""

    def test_create_worktrees_called_with_slice_scoped_id(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        spawner.spawn_agent_job(
            pipeline_id="issue-2261-v7",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            slice_id="slice-2",
        )
        # Pre-spawn worktree creation is keyed by the slice-scoped id.
        cw_kwargs = mock_gateway.create_worktrees.call_args.kwargs
        assert cw_kwargs["container_id"] == "issue-2261-v7-slice-2-coder"

    def test_session_register_uses_slice_scoped_worktree_container_id(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        spawner.spawn_agent_job(
            pipeline_id="issue-2261-v7",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            slice_id="slice-2",
        )
        # The gateway session reuses the worktree under the same key —
        # without slice scope here the agent's session would dangle.
        rs_kwargs = mock_gateway.register_session.call_args.kwargs
        assert rs_kwargs["worktree_container_id"] == "issue-2261-v7-slice-2-coder"

    def test_concurrent_spawn_fn_forwards_slice_id(self, spawner, mock_k8s_client, mock_gateway):
        spawn_fn = spawner.create_concurrent_spawn_fn(
            pipeline_id="issue-2261-v7",
            issue_number=2261,
            repo_volumes={},
            mode="public",
            repos=["owner/repo"],
            phase="implement",
            slice_id="slice-2",
        )
        spawn_fn(role=AgentRole.CODER, branch="egg/issue-2261-v7/slice-2")
        cw_kwargs = mock_gateway.create_worktrees.call_args.kwargs
        assert cw_kwargs["container_id"] == "issue-2261-v7-slice-2-coder"


class TestRestartAgentJobSliceScope:
    """``restart_agent_job`` threads ``slice_id`` into delete + respawn (#2410)."""

    def test_delete_targets_slice_scoped_job_name(self, spawner, mock_k8s_client, mock_gateway):
        """A slice-scoped restart must delete the slice-scoped Job, not the pipeline-level one.

        Without the fix, ``delete_job`` was called against ``egg-sandbox-egg-agent-{pid}-{role}``
        — leaving the actual ``egg-agent-{pid}-slice-{N}-{role}`` Job running while a fresh
        non-scoped Job was spawned alongside it.
        """
        spawner.restart_agent_job(
            pipeline_id="issue-2261-v7",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            slice_id="slice-2",
        )
        delete_call = mock_k8s_client.delete_job.call_args_list[-1]
        assert delete_call.args[0] == "egg-sandbox-egg-agent-issue-2261-v7-slice-2-coder"

    def test_gateway_session_cleanup_uses_slice_scoped_container_id(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """The gateway session is keyed by the slice-scoped unprefixed name."""
        spawner.restart_agent_job(
            pipeline_id="issue-2261-v7",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            slice_id="slice-2",
        )
        gw_call = mock_gateway.delete_session_by_container.call_args_list[-1]
        assert gw_call.args[0] == "egg-agent-issue-2261-v7-slice-2-coder"

    def test_respawn_uses_slice_scoped_worktree_id(self, spawner, mock_k8s_client, mock_gateway):
        """The respawned Job mounts the slice-scoped worktree.

        Pre-spawn ``create_worktrees`` is keyed by the slice-scoped container_id
        — failure mode #2 from the issue (worktree wrong / absent) is fixed by
        threading slice_id into the spawn call.
        """
        spawner.restart_agent_job(
            pipeline_id="issue-2261-v7",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            slice_id="slice-2",
        )
        cw_kwargs = mock_gateway.create_worktrees.call_args.kwargs
        assert cw_kwargs["container_id"] == "issue-2261-v7-slice-2-coder"

    def test_restart_count_is_per_slice(self, spawner, mock_k8s_client, mock_gateway):
        """Concurrent slices each get an independent restart budget."""
        spawner.restart_agent_job(
            pipeline_id="issue-2261-v7",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            slice_id="slice-2",
        )
        spawner.restart_agent_job(
            pipeline_id="issue-2261-v7",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            slice_id="slice-3",
        )
        # Each slice's coder has burned exactly one budget slot.
        assert spawner.get_restart_count("issue-2261-v7", "coder", slice_id="slice-2") == 1
        assert spawner.get_restart_count("issue-2261-v7", "coder", slice_id="slice-3") == 1
        # The pipeline-level bucket is untouched.
        assert spawner.get_restart_count("issue-2261-v7", "coder") == 0

    def test_reset_restart_counts_clears_slice_buckets(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """Per-pipeline reset must sweep every slice bucket too."""
        spawner._restart_counts[("issue-2261-v7", "coder", "slice-2")] = 3
        spawner._restart_counts[("issue-2261-v7", "coder", "slice-3")] = 2
        spawner._restart_counts[("issue-2261-v7", "coder", None)] = 1
        spawner._restart_counts[("issue-9999", "coder", "slice-2")] = 4

        spawner.reset_restart_counts("issue-2261-v7")

        assert spawner.get_restart_count("issue-2261-v7", "coder", slice_id="slice-2") == 0
        assert spawner.get_restart_count("issue-2261-v7", "coder", slice_id="slice-3") == 0
        assert spawner.get_restart_count("issue-2261-v7", "coder") == 0
        # Sibling pipeline untouched.
        assert spawner.get_restart_count("issue-9999", "coder", slice_id="slice-2") == 4

    def test_restart_propagates_egg_slice_id_to_container_env(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """The respawned Job's environment carries ``EGG_SLICE_ID``.

        Failure mode #3 from #2410: without the env var on the new Job,
        the agent's BRC handlers can't tag CONSENSUS_* signals with the
        slice and the orchestrator routes them to the pipeline-level
        tracker. Naming + worktree id alone are insufficient — the env
        is what the *agent* reads.
        """
        result = spawner.restart_agent_job(
            pipeline_id="issue-2261-v7",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            slice_id="slice-2",
        )
        # The env on the SpawnedContainer reflects what spawn_agent_job
        # assembled — and what was forwarded to ``create_container``.
        assert result.environment.get("EGG_SLICE_ID") == "slice-2"
        # Belt-and-braces: the env actually reached the k8s create call.
        create_kwargs = mock_k8s_client.create_container.call_args.kwargs
        assert create_kwargs["environment"].get("EGG_SLICE_ID") == "slice-2"

    def test_pipeline_level_restart_does_not_set_egg_slice_id(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """Without ``slice_id``, the restarted Job's env has no slice scope."""
        result = spawner.restart_agent_job(
            pipeline_id="issue-2261-v7",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
        )
        assert "EGG_SLICE_ID" not in result.environment
        create_kwargs = mock_k8s_client.create_container.call_args.kwargs
        assert "EGG_SLICE_ID" not in create_kwargs["environment"]


class TestDetectUncommittedChangesSliceScope:
    """``detect_uncommitted_changes`` inspects the slice-scoped worktree (#2410)."""

    def test_detects_changes_in_slice_scoped_worktree(self, spawner, tmp_path):
        worktree_dir = tmp_path / "issue-2261-v7-slice-2-coder" / "owner-repo"
        worktree_dir.mkdir(parents=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=" M file1.py\n?? file2.py\n",
            )
            result = spawner.detect_uncommitted_changes(
                "issue-2261-v7", "coder", slice_id="slice-2"
            )

        assert result is not None
        assert result["worktree_id"] == "issue-2261-v7-slice-2-coder"
        assert result["slice_id"] == "slice-2"
        assert result["file_count"] == 2

    def test_pipeline_level_call_does_not_pick_up_slice_worktree(self, spawner, tmp_path):
        """Without slice_id, only the pipeline-level worktree is inspected.

        A slice agent's uncommitted work must not surface through a
        pipeline-level call — they're separate worktrees with separate
        ownership semantics.
        """
        # Only the slice-scoped worktree exists on disk.
        slice_dir = tmp_path / "issue-2261-v7-slice-2-coder" / "owner-repo"
        slice_dir.mkdir(parents=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=" M file1.py\n")
            result = spawner.detect_uncommitted_changes("issue-2261-v7", "coder")

        # No pipeline-level worktree → returns None even though the slice worktree
        # has uncommitted changes.
        assert result is None

    def test_slice_call_does_not_pick_up_pipeline_worktree(self, spawner, tmp_path):
        """Symmetric guard: a slice-scoped lookup must not surface pipeline-level work."""
        # Only the pipeline-level worktree exists on disk.
        pipeline_dir = tmp_path / "issue-2261-v7-coder" / "owner-repo"
        pipeline_dir.mkdir(parents=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=" M file1.py\n")
            result = spawner.detect_uncommitted_changes(
                "issue-2261-v7", "coder", slice_id="slice-2"
            )

        assert result is None


class TestCleanupPipelineSliceWorktrees:
    """``cleanup_pipeline``'s filesystem scan recognises slice-scoped worktrees."""

    def test_filesystem_scan_picks_up_slice_scoped_worktrees(
        self, spawner, mock_k8s_client, mock_gateway, tmp_path, monkeypatch
    ):
        import kubernetes_spawner as ks_mod

        # Lay out a mix of pipeline-level, role-level, slice-scoped, and
        # unrelated entries so the scan's allowlist is exercised end-to-end.
        (tmp_path / "issue-2261-v7").mkdir()
        (tmp_path / "issue-2261-v7-coder").mkdir()
        (tmp_path / "issue-2261-v7-slice-2-coder").mkdir()
        (tmp_path / "issue-2261-v7-slice-3-tester").mkdir()
        # Sibling pipeline whose id starts with the same prefix — must NOT
        # be swept (mirrors the #1865 regression guard).
        (tmp_path / "issue-2261-v7-other-thing").mkdir()
        (tmp_path / "issue-9999-coder").mkdir()

        monkeypatch.setattr(ks_mod, "WORKTREE_BASE_DIR", tmp_path)
        # No Jobs returned — drive cleanup purely off the filesystem scan.
        mock_k8s_client.list_containers.return_value = []

        spawner.cleanup_pipeline("issue-2261-v7")

        cleaned = {
            call.kwargs.get("container_id") for call in mock_gateway.delete_worktrees.call_args_list
        }
        assert "issue-2261-v7" in cleaned
        assert "issue-2261-v7-coder" in cleaned
        assert "issue-2261-v7-slice-2-coder" in cleaned
        assert "issue-2261-v7-slice-3-tester" in cleaned
        # Sibling pipelines are left alone.
        assert "issue-2261-v7-other-thing" not in cleaned
        assert "issue-9999-coder" not in cleaned
