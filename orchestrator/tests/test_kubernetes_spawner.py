"""
Tests for the KubernetesSpawner.

Covers Job spawning, gateway session integration, restart tracking,
pipeline cleanup, and error handling.
"""

from __future__ import annotations

import hashlib
import shutil
import statistics
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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

    def test_spawn_injects_oauth_placeholder_on_anthropic_path(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """Default (Anthropic) spawns carry the session-token placeholder in
        CLAUDE_CODE_OAUTH_TOKEN.

        The k8s container command overrides the image ENTRYPOINT, so
        ``setup_anthropic_api()`` never runs to derive the placeholder; the
        spawner must inject it or Claude Code aborts with "Not logged in"
        (#2817). The payload is the session token already in
        EGG_SESSION_TOKEN — no real credential enters the sandbox.
        """
        from egg_session_placeholder import to_placeholder

        result = spawner.spawn_agent_job(
            pipeline_id="pipe-anthropic",
            agent_role=AgentRole.TESTER,
            repos=["owner/repo"],
        )
        env = result.environment
        expected = to_placeholder(env["EGG_SESSION_TOKEN"])
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == expected
        assert env["CLAUDE_CODE_OAUTH_TOKEN"].startswith("sk-ant-oat01-")
        assert "ANTHROPIC_API_KEY" not in env

    def test_spawn_injects_api_key_placeholder_on_litellm_path(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """LiteLLM spawns carry the placeholder in ANTHROPIC_API_KEY.

        On the LiteLLM path Claude Code uses api_key auth and sends the
        credential via x-api-key, so the placeholder must land in
        ANTHROPIC_API_KEY (not CLAUDE_CODE_OAUTH_TOKEN) for the session
        token to reach the gateway. Mirrors setup_anthropic_api (#2864) at
        the layer that actually runs under k8s.
        """
        from egg_session_placeholder import to_placeholder

        result = spawner.spawn_agent_job(
            pipeline_id="pipe-litellm",
            agent_role=AgentRole.TESTER,
            repos=["owner/repo"],
            upstream="litellm",
            upstream_model="qwen3.7-max",
        )
        env = result.environment
        expected = to_placeholder(env["EGG_SESSION_TOKEN"])
        assert env["ANTHROPIC_API_KEY"] == expected
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    def test_extra_env_cannot_override_credential_placeholder(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """``extra_env`` cannot override the credential placeholder keys (#2817).

        The spawner is the single source of truth: the placeholder is
        derived from the session token and injected into
        ``CLAUDE_CODE_OAUTH_TOKEN`` (or ``ANTHROPIC_API_KEY`` on the
        LiteLLM path). A caller that tried to ship a different value via
        ``extra_env`` would desync the credential header from the session
        the gateway resolves; protecting both keys catches that.
        """
        from egg_session_placeholder import to_placeholder

        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            extra_env={
                "CLAUDE_CODE_OAUTH_TOKEN": "attacker-supplied",
                "ANTHROPIC_API_KEY": "attacker-supplied",
            },
        )
        env = result.environment
        expected = to_placeholder(env["EGG_SESSION_TOKEN"])
        # Spawner's placeholder wins; the attacker value is dropped.
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == expected
        assert "ANTHROPIC_API_KEY" not in env

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

    def test_spawn_with_base_branch_sets_egg_base_branch_env(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """``base_branch=`` propagates into ``EGG_BASE_BRANCH`` (#2967).

        The BRC event-pump's per-producer ``git log --not origin/<base>``
        delta reads this env var (via the consensus wrapper and the
        event-prompt composer). Nothing exported it before, so both
        consumers fell back to ``origin/main`` and the delta errored on any
        non-``main`` repo, silently dropping the slice-3 diff reviewers audit.
        """
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            base_branch="jwbron-claude-md",
        )
        assert result.environment.get("EGG_BASE_BRANCH") == "jwbron-claude-md"
        create_kwargs = mock_k8s_client.create_container.call_args.kwargs
        assert create_kwargs["environment"].get("EGG_BASE_BRANCH") == "jwbron-claude-md"

    def test_spawn_without_base_branch_does_not_set_egg_base_branch(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """An unresolved (None) base leaves ``EGG_BASE_BRANCH`` unset (#2967).

        The consumers carry their own documented ``main`` default, so the
        spawner leaves the key absent rather than injecting an empty string
        when the caller couldn't resolve a concrete branch.
        """
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
        )
        assert "EGG_BASE_BRANCH" not in result.environment

    def test_extra_env_cannot_override_egg_base_branch(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """``extra_env`` cannot override ``EGG_BASE_BRANCH`` — protected key (#2967).

        The spawner is the single source of truth: the same resolved base
        branch creates the worktree, builds the prompt's diff commands, and
        is exported here. An ``extra_env`` override could point the
        ``--not origin/<base>`` delta at a different branch than the worktree
        was based on, silently corrupting the re-review scope.
        """
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            base_branch="develop",
            extra_env={"EGG_BASE_BRANCH": "attacker-supplied"},
        )
        assert result.environment.get("EGG_BASE_BRANCH") == "develop"
        create_kwargs = mock_k8s_client.create_container.call_args.kwargs
        assert create_kwargs["environment"].get("EGG_BASE_BRANCH") == "develop"

    def test_spawn_sets_egg_wait_producer_allowlist_for_reviewer(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """#2725: spawner pre-resolves the producer allowlist from the
        BRC review graph for a reviewer role.

        ``reviewer_code`` reviews ``coder`` and ``tester`` in the implement
        graph; the env var must list both producers plus the system
        senders so the wait-loop CLI auto-scopes without rubric changes.
        """
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.REVIEWER_CODE,
            repos=["jwbron/egg"],
            phase="implement",
            slice_id="slice-1",
        )
        allowlist = result.environment.get("EGG_WAIT_PRODUCER_ALLOWLIST")
        assert allowlist is not None
        roles = set(allowlist.split(","))
        # Graph neighbors: producers reviewer_code reviews.
        assert "coder" in roles
        assert "tester" in roles
        # System senders always included so OVERSEER_ALERT /
        # CONSENSUS_RE_REVIEW keep waking the reviewer.
        assert "overseer" in roles
        assert "orchestrator" in roles

    def test_spawn_sets_egg_wait_producer_allowlist_for_producer(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """#2725: producers get their reviewers in the allowlist so they
        wake on ACK/NACK from the right callers."""
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["jwbron/egg"],
            phase="implement",
            slice_id="slice-1",
        )
        allowlist = result.environment.get("EGG_WAIT_PRODUCER_ALLOWLIST")
        assert allowlist is not None
        roles = set(allowlist.split(","))
        # coder is reviewed by reviewer_code, reviewer_code_holistic,
        # reviewer_contract, tester, reviewer_security,
        # reviewer_concurrency — all must be present.
        for r in (
            "reviewer_code",
            "reviewer_code_holistic",
            "reviewer_contract",
            "tester",
            "reviewer_security",
            "reviewer_concurrency",
        ):
            assert r in roles, f"expected {r} in coder's allowlist: {roles}"
        assert {"overseer", "orchestrator"}.issubset(roles)

    def test_spawn_skips_allowlist_without_phase(self, spawner, mock_k8s_client, mock_gateway):
        """#2725: pipeline-level spawns (no phase) skip the env var so
        legacy wake-on-anything behavior is preserved unchanged."""
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["jwbron/egg"],
        )
        assert "EGG_WAIT_PRODUCER_ALLOWLIST" not in result.environment

    def test_extra_env_cannot_override_egg_wait_producer_allowlist(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """#2725: spawner is the single source of truth for the
        allowlist. ``extra_env`` cannot smuggle a stale or wrong list
        in — silent acceptance would sleep the agent through
        legitimate events its rubric expects to handle.
        """
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.REVIEWER_CODE,
            repos=["jwbron/egg"],
            phase="implement",
            slice_id="slice-1",
            extra_env={"EGG_WAIT_PRODUCER_ALLOWLIST": "evil_role"},
        )
        allowlist = result.environment.get("EGG_WAIT_PRODUCER_ALLOWLIST")
        assert allowlist is not None
        # extra_env's "evil_role" did not win — the spawner-derived
        # graph allowlist did.
        assert "evil_role" not in allowlist
        assert "coder" in allowlist
        assert "tester" in allowlist

    def test_extra_env_cannot_override_egg_branch(self, spawner, mock_k8s_client, mock_gateway):
        """``extra_env`` cannot override ``EGG_BRANCH`` — protected key (#2428).

        The agent's ``egg-orch push`` reads ``EGG_BRANCH`` to retarget
        the refspec; the gateway's session-scoped allowlist compares
        that target against the ``assigned_branch`` registered at
        session creation. Both must agree, so the spawner's ``branch``
        parameter is the single source of truth. Slice scheduling
        previously routed the spawn loop's pipeline-level ``EGG_BRANCH``
        through ``extra_env``; that override silently won and every
        slice agent's push ended up retargeted to ``<pid>/work``
        instead of ``<pid>/<slice>``, getting rejected by the gateway.
        """
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            branch="egg/issue-2261/slice-2",
            extra_env={"EGG_BRANCH": "egg/issue-2261/work"},
        )
        # Spawner's branch wins, not extra_env's.
        assert result.environment.get("EGG_BRANCH") == "egg/issue-2261/slice-2"
        create_kwargs = mock_k8s_client.create_container.call_args.kwargs
        assert create_kwargs["environment"].get("EGG_BRANCH") == "egg/issue-2261/slice-2"

    def test_concurrent_spawn_fn_slice_branch_wins_over_pipeline_sandbox_env(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """End-to-end repro of #2428 through the concurrent spawn entrypoint.

        The pipeline run loop builds ``sandbox_env`` once for the whole
        phase and threads it through ``create_concurrent_spawn_fn`` to
        every per-slice agent. When the slice scheduler then asks the
        executor to spawn a slice agent, the per-spawn ``branch`` is
        the slice integration branch but the merged ``extra_env`` still
        carried the pipeline-level value. The spawner's ``branch``
        parameter must win on ``EGG_BRANCH`` regardless.
        """
        sandbox_env_pipeline_branch = "egg/issue-2261/work"
        spawn_fn = spawner.create_concurrent_spawn_fn(
            pipeline_id="issue-2261",
            issue_number=2261,
            repo_volumes={"owner/repo": "/home/egg/.egg-worktrees/test/repo"},
            mode="public",
            repos=["owner/repo"],
            phase="implement",
            sandbox_env={"EGG_BRANCH": sandbox_env_pipeline_branch},
            slice_id="slice-2",
        )
        result = spawn_fn(
            role=AgentRole.CODER,
            branch="egg/issue-2261/slice-2",
        )
        assert result.environment.get("EGG_BRANCH") == "egg/issue-2261/slice-2"
        create_kwargs = mock_k8s_client.create_container.call_args.kwargs
        assert create_kwargs["environment"].get("EGG_BRANCH") == "egg/issue-2261/slice-2"

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

    def test_spawn_k8s_error_preserves_reused_session(self, spawner, mock_k8s_client, mock_gateway):
        """A k8s create failure on the #3064 slice-4 reuse path must NOT delete
        the supplied session — we did not register it, and the next event still
        reuses it.
        """
        from kubernetes_spawner import KubernetesSpawnError

        mock_k8s_client.create_container.side_effect = KubernetesClientError("API error")
        with pytest.raises(KubernetesSpawnError, match="Failed to spawn Job"):
            spawner.spawn_agent_job(
                pipeline_id="p",
                agent_role=AgentRole.CODER,
                slice_id="slice-4",
                repos=["owner/repo"],
                reuse_worktree_id="issue-3064-slice-4-coder",
                repo_volumes={"owner/repo": "/x"},
                existing_session_token="tok-live",
                wait_for_gateway=False,
            )
        # The supplied (reused) token is left intact; we never registered it.
        mock_gateway.delete_session.assert_not_called()

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

    def test_restart_forwards_wait_for_gateway_false(self, spawner):
        """restart_agent_job forwards wait_for_gateway=False to spawn_agent_job.

        Mirrors ``test_spawn_without_gateway_wait`` so a future refactor that
        drops the forwarded kwarg fails at unit-test time. The integration
        suite covers it end-to-end, but the seam itself is worth pinning.
        """
        with patch.object(spawner, "spawn_agent_job") as mock_spawn:
            spawner.restart_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
                wait_for_gateway=False,
            )
        mock_spawn.assert_called_once()
        assert mock_spawn.call_args.kwargs["wait_for_gateway"] is False

    def test_restart_forwards_wait_for_gateway_default_true(self, spawner):
        """restart_agent_job defaults wait_for_gateway=True and forwards it."""
        with patch.object(spawner, "spawn_agent_job") as mock_spawn:
            spawner.restart_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
            )
        mock_spawn.assert_called_once()
        assert mock_spawn.call_args.kwargs["wait_for_gateway"] is True


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


# ---------------------------------------------------------------------------
# Slice-2 (#3064 TASK-2-3): one-shot event-spawn entry
# ---------------------------------------------------------------------------
#
# Test-first contract for TASK-2-2's one-shot spawn entry. The orchestrator
# event loop (TASK-2-1) drives per-event Jobs through a NEW spawner entry
# pinned here as ``KubernetesSpawner.spawn_event_job(...)``. It must:
#   * set EGG_EVENT_LOOP_OWNER=orchestrator plus the event identity
#     (EGG_EVENT_ACTION, EGG_EVENT_DEDUPE_KEY, payload refs) in Job env;
#   * carry the dedupe key as a Job label (the reconciliation handle the
#     loop uses for stateless restart);
#   * derive the Job name from the existing
#     egg-agent-<pipeline>[-<slice>]-<role> convention plus a short event
#     discriminator, staying within the k8s 63-char budget;
#   * adopt an already-live Job for the same dedupe key rather than
#     duplicating it;
#   * leave the long-lived ``spawn_agent_job()`` pod-mode path unchanged.
#
# These tests fail until the coder's parallel TASK-2-2 lands; convergence
# (coder + tester merge) makes them green, mirroring slice-1's test-first
# alignment. ``confirm``/``complete`` are agent-free and never reach here.


class TestSpawnEventJobOneShot:
    """One-shot per-event spawn entry (orchestrator-owned lifecycle)."""

    _KEY = "a" * 64  # a stand-in sha256 dedupe key

    def test_event_job_sets_owner_and_event_identity_env(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        result = spawner.spawn_event_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            action="propose",
            dedupe_key=self._KEY,
            slice_id="slice-2",
            phase="implement",
            repos=["owner/repo"],
        )
        env = result.environment
        # Owner flag flips the wrapper into its one-shot arm.
        assert env["EGG_EVENT_LOOP_OWNER"] == "orchestrator"
        # Full event identity is injected.
        assert env["EGG_EVENT_ACTION"] == "propose"
        assert env["EGG_EVENT_DEDUPE_KEY"] == self._KEY
        # Standard agent scope still present.
        assert env["EGG_PIPELINE_ID"] == "pipe-1"
        assert env["EGG_AGENT_ROLE"] == "coder"
        assert env["EGG_SLICE_ID"] == "slice-2"
        assert env["EGG_PHASE"] == "implement"

    def test_event_job_carries_dedupe_key_as_label(self, spawner, mock_k8s_client, mock_gateway):
        from kubernetes_spawner import LABEL_EVENT_DEDUPE, _dedupe_label_value

        spawner.spawn_event_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.REVIEWER_CODE,
            action="ack",
            dedupe_key=self._KEY,
            slice_id="slice-2",
            phase="implement",
            repos=["owner/repo"],
        )
        labels = mock_k8s_client.create_container.call_args.kwargs["labels"]
        # The dedupe key is selectable as a Job label — the reconciliation
        # handle the event loop rebuilds its live set from on restart. It is
        # shortened to the k8s 63-char label-value limit; the full key rides in
        # env, and the reconcile selector applies the identical shortening.
        label_value = labels[LABEL_EVENT_DEDUPE]
        assert label_value == _dedupe_label_value(self._KEY)
        assert len(label_value) <= 63, (
            f"dedupe label value exceeds k8s 63-char limit: {label_value!r} ({len(label_value)})"
        )
        # Standard orchestrator labels remain.
        assert labels[LABEL_ORCHESTRATOR] == "true"
        assert labels[LABEL_PIPELINE_ID] == "pipe-1"
        assert labels[LABEL_AGENT_ROLE] == "reviewer_code"

    def test_event_dedupe_label_value_within_k8s_limit(self, spawner, mock_k8s_client):
        """Regression (#3064): a real 64-char sha256 dedupe key must be
        shortened to <=63 chars at the actual label path, since k8s rejects any
        label value longer than 63 chars at the API server.
        """
        from kubernetes_spawner import LABEL_EVENT_DEDUPE

        real_key = hashlib.sha256(
            b"pipe\x00slice\x00implement\x00coder\x00propose\x00v1"
        ).hexdigest()
        assert len(real_key) == 64  # guard: sha256 hexdigest is 64 chars
        spawner.spawn_event_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            action="propose",
            dedupe_key=real_key,
            slice_id="slice-2",
            phase="implement",
            repos=["owner/repo"],
        )
        labels = mock_k8s_client.create_container.call_args.kwargs["labels"]
        label_value = labels[LABEL_EVENT_DEDUPE]
        assert len(label_value) <= 63, (
            f"dedupe label value exceeds k8s 63-char limit: {label_value!r} ({len(label_value)})"
        )

    def test_event_action_must_be_a_spawn_verb(self, spawner, mock_k8s_client):
        """confirm/complete are agent-free and must never reach the spawner;
        the one-shot entry rejects them loudly.
        """
        for bad in ("confirm", "complete"):
            with pytest.raises((ValueError, AssertionError)):
                spawner.spawn_event_job(
                    pipeline_id="pipe-1",
                    agent_role=AgentRole.CODER,
                    action=bad,
                    dedupe_key=self._KEY,
                    slice_id="slice-2",
                    phase="implement",
                    repos=["owner/repo"],
                )

    def test_event_job_name_within_k8s_budget(self, spawner, mock_k8s_client):
        """Long pipeline/slice/role + event discriminator stays within the
        63-char RFC-1123 budget (existing truncation convention applies).
        """
        spawner.spawn_event_job(
            pipeline_id="issue-2261-verylongpipelineidentifier-v7",
            agent_role=AgentRole.REVIEWER_CODE_HOLISTIC,
            action="nack",
            dedupe_key="b" * 64,
            slice_id="slice-12-a-very-long-slice-name",
            phase="implement",
            repos=["owner/repo"],
        )
        name = mock_k8s_client.create_container.call_args.kwargs["name"]
        assert len(name) <= 63, f"Job name exceeds k8s budget: {name!r} ({len(name)})"
        assert name.startswith("egg-agent-")
        # RFC-1123: lowercase, hyphen-separated, no underscores.
        assert "_" not in name

    def test_same_dedupe_key_adopts_existing_job(self, spawner, mock_k8s_client, mock_gateway):
        """Requesting a spawn for an already-live dedupe key adopts the
        existing Job (no duplicate create_container).
        """
        # First spawn: no live Job for the key.
        mock_k8s_client.list_jobs.return_value = []
        spawner.spawn_event_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            action="propose",
            dedupe_key=self._KEY,
            slice_id="slice-2",
            phase="implement",
            repos=["owner/repo"],
        )
        assert mock_k8s_client.create_container.call_count == 1

        # Second request for the SAME key: a live Job already carries the
        # dedupe label, so the entry adopts it instead of creating another.
        existing = MagicMock()
        # The live Job carries the label-safe (shortened) dedupe value, exactly
        # as the spawn side wrote it — never the full 64-char key (k8s would
        # have rejected that).
        existing.labels = {"egg.event.dedupe-key": self._KEY[:63]}
        existing.job_name = "egg-agent-pipe-1-slice-2-coder-ev"
        # Adoption only counts a Job whose pod is still doing work; a RUNNING
        # status is what makes it adoptable (a terminal Job would not be —
        # see test_terminated_job_does_not_block_respawn).
        existing.status = ContainerStatus.RUNNING
        mock_k8s_client.list_jobs.return_value = [existing]

        spawner.spawn_event_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            action="propose",
            dedupe_key=self._KEY,
            slice_id="slice-2",
            phase="implement",
            repos=["owner/repo"],
        )
        # create_container was NOT called a second time — the Job was adopted.
        assert mock_k8s_client.create_container.call_count == 1

    def test_terminated_job_does_not_block_respawn(self, spawner, mock_k8s_client):
        """A label-matching but TERMINATED Job (EXITED/FAILED) lingering under
        the finished-TTL must NOT be adopted — a re-derived identical event
        whose prior pod failed without advancing the tracker must respawn.
        """
        for terminal_status in (ContainerStatus.EXITED, ContainerStatus.FAILED):
            mock_k8s_client.create_container.reset_mock()
            terminated = MagicMock()
            terminated.labels = {"egg.event.dedupe-key": self._KEY[:63]}
            terminated.job_name = "egg-agent-pipe-1-slice-2-coder-ev"
            terminated.status = terminal_status
            mock_k8s_client.list_jobs.return_value = [terminated]

            spawner.spawn_event_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                action="propose",
                dedupe_key=self._KEY,
                slice_id="slice-2",
                phase="implement",
                repos=["owner/repo"],
            )
            # The terminated Job is not live, so a new Job is created.
            assert mock_k8s_client.create_container.call_count == 1, (
                f"terminated Job ({terminal_status}) must not block respawn"
            )

    def test_spawn_agent_job_pod_path_unchanged(self, spawner, mock_k8s_client):
        """The long-lived pod-mode entry never injects the one-shot event
        identity — pod mode is byte-for-byte the prior behavior.
        """
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
        )
        env = result.environment
        assert "EGG_EVENT_ACTION" not in env
        assert "EGG_EVENT_DEDUPE_KEY" not in env
        # Owner flag is not forced on by the pod-mode path.
        assert env.get("EGG_EVENT_LOOP_OWNER", "pod") in ("pod", None)


class TestEventJobStatusView:
    """The #3064 slice-3 Job-status observer maps k8s status → loop outcomes."""

    _KEY = "f" * 64

    def _info(self, status):
        return ContainerInfo(
            container_id="uid-1", container_name="job-1", status=status, job_name="job-1"
        )

    def test_failed_job_is_abnormal(self, spawner, mock_k8s_client):
        import event_loop

        mock_k8s_client.list_jobs.return_value = [self._info(ContainerStatus.FAILED)]
        view = spawner.create_event_job_status_view()
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_ABNORMAL

    def test_exited_job_is_success(self, spawner, mock_k8s_client):
        import event_loop

        mock_k8s_client.list_jobs.return_value = [self._info(ContainerStatus.EXITED)]
        view = spawner.create_event_job_status_view()
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_SUCCESS

    def test_active_job_is_running(self, spawner, mock_k8s_client):
        import event_loop

        mock_k8s_client.list_jobs.return_value = [self._info(ContainerStatus.RUNNING)]
        view = spawner.create_event_job_status_view()
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_RUNNING

    def test_failed_wins_over_running(self, spawner, mock_k8s_client):
        """A FAILED Job among the matches classifies as abnormal."""
        import event_loop

        mock_k8s_client.list_jobs.return_value = [
            self._info(ContainerStatus.RUNNING),
            self._info(ContainerStatus.FAILED),
        ]
        view = spawner.create_event_job_status_view()
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_ABNORMAL

    def test_missing_job_is_running_not_failure(self, spawner, mock_k8s_client):
        """No Job found (GC'd / not yet visible) must never be a spurious abort."""
        import event_loop

        mock_k8s_client.list_jobs.return_value = []
        view = spawner.create_event_job_status_view()
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_RUNNING

    def test_list_error_is_running_not_failure(self, spawner, mock_k8s_client):
        """A list error degrades to running (best-effort) — never abnormal."""
        import event_loop

        mock_k8s_client.list_jobs.side_effect = RuntimeError("API down")
        view = spawner.create_event_job_status_view()
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_RUNNING

    def test_query_uses_dedupe_label_selector(self, spawner, mock_k8s_client):
        """The view queries by the label-safe (shortened) dedupe selector."""
        mock_k8s_client.list_jobs.return_value = []
        spawner.create_event_job_status_view().outcome_for(self._KEY)
        _, kwargs = mock_k8s_client.list_jobs.call_args
        assert kwargs["label_selector"] == f"egg.event.dedupe-key={self._KEY[:63]}"

    # -- reap_terminated (#3181 re-review) ---------------------------------

    @staticmethod
    def _named(status, name):
        return ContainerInfo(
            container_id=f"uid-{name}", container_name=name, status=status, job_name=name
        )

    def test_reap_terminated_deletes_only_terminal_jobs(self, spawner, mock_k8s_client):
        """Reap removes FAILED/EXITED Jobs but leaves a live RUNNING one."""
        mock_k8s_client.list_jobs.return_value = [
            self._named(ContainerStatus.RUNNING, "live"),
            self._named(ContainerStatus.FAILED, "dead"),
            self._named(ContainerStatus.EXITED, "done"),
        ]
        view = spawner.create_event_job_status_view()
        assert view.reap_terminated(self._KEY) == 2
        removed = {c.args[0] for c in mock_k8s_client.remove_container.call_args_list}
        assert removed == {"dead", "done"}
        assert "live" not in removed

    def test_reap_terminated_swallows_delete_errors(self, spawner, mock_k8s_client):
        """Reaping is best-effort: a delete failure is logged, never raised,
        and that Job is not counted as reaped (the live-only adoption filter is
        the backstop)."""
        mock_k8s_client.list_jobs.return_value = [self._named(ContainerStatus.FAILED, "dead")]
        mock_k8s_client.remove_container.side_effect = RuntimeError("API down")
        view = spawner.create_event_job_status_view()
        assert view.reap_terminated(self._KEY) == 0

    def test_reap_terminated_no_jobs_is_noop(self, spawner, mock_k8s_client):
        """No matching Job (already GC'd) reaps nothing and never deletes."""
        mock_k8s_client.list_jobs.return_value = []
        view = spawner.create_event_job_status_view()
        assert view.reap_terminated(self._KEY) == 0
        mock_k8s_client.remove_container.assert_not_called()


class _StatefulEventJobs:
    """Faithful single-dedupe-key k8s Job store for the crash→respawn path.

    Models the lifecycle the cross-module respawn depends on (#3181 re-review):
    a created Job is RUNNING and visible under its dedupe label; a crash flips
    it to FAILED; a terminal Job lingers (the real ``ttlSecondsAfterFinished``
    window) until reaped. Backing the *real* ``KubernetesSpawner`` with this
    store exercises ``_event_dedupe_key_live`` adoption, ``_EventJobStatusView``
    classification, and ``reap_terminated`` against one shared Job set — the
    interaction the always-spawns fake elided.
    """

    def __init__(self) -> None:
        self.jobs: list[ContainerInfo] = []
        self._seq = 0

    # --- k8s client surface the spawner touches ---------------------------
    def list_jobs(self, namespace, label_selector=None):
        # The spawner only ever queries our single key's selector.
        return list(self.jobs)

    def create_container(self, **kwargs):
        self._seq += 1
        name = kwargs.get("name") or f"event-job-{self._seq}"
        info = ContainerInfo(
            container_id=f"uid-{self._seq}",
            container_name=name,
            job_name=name,
            namespace="test-ns",
            status=ContainerStatus.RUNNING,
        )
        self.jobs.append(info)
        return info

    def remove_container(self, name, force=False):
        self.jobs = [j for j in self.jobs if j.job_name != name]

    def delete_job(self, name, namespace=None, **kwargs):
        # Idempotent pre-spawn cleanup; our generated names never collide.
        self.jobs = [j for j in self.jobs if j.job_name != name]

    # --- test helpers -----------------------------------------------------
    def crash_all(self):
        self.jobs = [j.model_copy(update={"status": ContainerStatus.FAILED}) for j in self.jobs]

    @property
    def names(self):
        return [j.job_name for j in self.jobs]

    @property
    def statuses(self):
        return [j.status for j in self.jobs]


class TestEventJobCrashRespawn:
    """Crash → respawn drives the REAL spawner adoption + status view together.

    Regression for the #3181 re-review cross-module silent no-op: a crashed
    one-shot Job lingers FAILED, and the prior adoption check treated *any*
    Job carrying the dedupe label (including a terminal one) as live — so the
    respawn adopted the corpse, created no pod, and the status view kept
    re-reading the same FAILED Job, climbing the streak to a false AGENT_FAILED
    without ever retrying. These tests bind the real ``spawn_event_job``
    adoption, ``_EventJobStatusView``, and ``reap_terminated`` to one stateful
    Job store so the interaction — not an always-spawns fake — is exercised.
    """

    _KEY = "c" * 64

    def _spawn(self, spawner):
        return spawner.spawn_event_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            action="propose",
            dedupe_key=self._KEY,
            slice_id="slice-2",
            phase="implement",
            repos=["owner/repo"],
        )

    def _wire(self, store, mock_k8s_client):
        mock_k8s_client.list_jobs.side_effect = store.list_jobs
        mock_k8s_client.create_container.side_effect = store.create_container
        mock_k8s_client.remove_container.side_effect = store.remove_container
        mock_k8s_client.delete_job.side_effect = store.delete_job

    def test_crash_then_respawn_creates_a_fresh_job(self, spawner, mock_k8s_client, mock_gateway):
        import event_loop

        store = _StatefulEventJobs()
        self._wire(store, mock_k8s_client)
        view = spawner.create_event_job_status_view()

        # 1. First spawn: nothing live → a Job is created and runs.
        assert self._spawn(spawner) is not None
        assert len(store.names) == 1
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_RUNNING

        # 2. The pod crashes — the Job goes FAILED and lingers (TTL window).
        store.crash_all()
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_ABNORMAL

        # 3. The loop's abnormal branch reaps the terminated Job (fix #2)...
        assert view.reap_terminated(self._KEY) == 1
        assert store.names == []

        # 4. ...so the respawn actually creates a NEW Job instead of adopting
        #    the corpse — the cross-module dead-end #3181 flagged.
        assert self._spawn(spawner) is not None
        assert store.statuses == [ContainerStatus.RUNNING]
        # The fresh Job classifies as running: the streak does not re-increment
        # against a dead Job on the next observation.
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_RUNNING
        # Two real spawns happened (initial + respawn), not one adopted no-op.
        assert mock_k8s_client.create_container.call_count == 2

    def test_terminal_job_alone_does_not_block_respawn(self, spawner, mock_k8s_client):
        """Even if the reap is skipped/fails (best-effort), a lingering terminal
        Job must not be adopted: the live-only adoption filter (fix #1) lets the
        respawn create a new Job rather than dead-ending for the TTL window.

        The corpse is seeded under the *deterministic* Job name the respawn
        collides with (not an arbitrary one), so this also exercises the
        production same-name pre-spawn ``delete_job`` cleanup
        (``spawn_agent_job``): after the spawn the corpse is gone, leaving only
        the fresh RUNNING Job."""
        from kubernetes_client import KubernetesClient
        from kubernetes_spawner import (
            _EVENT_JOB_NAME_DISCRIMINATOR_LEN,
            KubernetesSpawner,
            _fit_k8s_name,
        )

        # Mirror spawn_event_job → spawn_agent_job's deterministic naming for
        # this _spawn()'s args so the corpse collides with the respawn's name.
        base, _ = KubernetesSpawner._build_k8s_job_names(
            "pipe-1", AgentRole.CODER, slice_id="slice-2"
        )
        fitted = _fit_k8s_name(f"{base}-{self._KEY[:_EVENT_JOB_NAME_DISCRIMINATOR_LEN]}")
        corpse_name = f"{KubernetesClient.JOB_PREFIX}{fitted}"

        store = _StatefulEventJobs()
        # Seed a lingering FAILED Job under the dedupe label (reap "missed" it).
        store.jobs = [
            ContainerInfo(
                container_id="uid-old",
                container_name=corpse_name,
                job_name=corpse_name,
                namespace="test-ns",
                status=ContainerStatus.FAILED,
            )
        ]
        self._wire(store, mock_k8s_client)

        assert self._spawn(spawner) is not None
        # A new Job was created (not adopted) despite the terminal Job present.
        assert mock_k8s_client.create_container.call_count == 1
        assert ContainerStatus.RUNNING in store.statuses
        # The same-name pre-spawn delete_job reaped the corpse: it no longer
        # lingers alongside the fresh Job.
        assert corpse_name not in store.names
        assert store.statuses == [ContainerStatus.RUNNING]

    def test_live_job_still_blocks_respawn(self, spawner, mock_k8s_client):
        """Regression guard: a genuinely RUNNING Job for the key is still
        adopted (no duplicate pod) — fix #1 narrows adoption to live Jobs, it
        does not disable it."""
        store = _StatefulEventJobs()
        store.jobs = [
            ContainerInfo(
                container_id="uid-live",
                container_name="live",
                job_name="live",
                namespace="test-ns",
                status=ContainerStatus.RUNNING,
            )
        ]
        self._wire(store, mock_k8s_client)

        assert self._spawn(spawner) is None  # adopted
        mock_k8s_client.create_container.assert_not_called()


# ---------------------------------------------------------------------------
# #3064 slice-4 — worktree re-attach + gateway-session reuse
#
# These tests drive the REAL helpers against real on-disk git worktrees
# (re-attach validation / dirty-state clean / hard-sync) and the real
# production session-reuse path. No patching of the function under test.
# ---------------------------------------------------------------------------

_WT_ID = "issue-3064-slice-4-coder"
_BRANCH = "egg/issue-3064/slice-4"
_REPOS = ["owner/repo"]

_GIT_IDENT = [
    "-c",
    "user.email=t@egg.test",
    "-c",
    "user.name=egg-test",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "safe.directory=*",
    "-c",
    "protocol.file.allow=always",
]


def _git(cwd, *args, check=True):
    """Run a git command in *cwd* with a deterministic identity."""
    return subprocess.run(
        ["git", *_GIT_IDENT, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _make_worktree(base, worktree_id, repo_name, branch, *, with_origin=False):
    """Create a real git worktree at ``base/worktree_id/repo_name`` on *branch*.

    When ``with_origin`` is set, also create a bare ``origin`` repo, wire it as
    the ``origin`` remote, and push *branch* so ``origin/<branch>`` exists (the
    hard-sync target). Returns ``(repo_path, origin_path_or_None)``.
    """
    repo = Path(base) / worktree_id / repo_name
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", branch)
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    origin = None
    if with_origin:
        origin = Path(base) / f"{worktree_id}-{repo_name}-origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", branch, str(origin)],
            capture_output=True,
            text=True,
            check=True,
        )
        _git(repo, "remote", "add", "origin", str(origin))
        _git(repo, "push", "origin", branch)
    return repo, origin


class _FakeClock:
    """Deterministic monotonic clock for the spawn-timing tests.

    Yields a (start, end) pair per spawn so the measured ``spawn_ms`` equals the
    requested per-spawn delta exactly — no real sleeps, fully simulated.
    """

    def __init__(self, deltas_ms):
        self._ticks: list[float] = []
        t = 0.0
        for d in deltas_ms:
            self._ticks.append(t)
            self._ticks.append(t + d / 1000.0)
            t += d / 1000.0 + 1.0
        self._i = 0

    def __call__(self) -> float:
        v = self._ticks[self._i]
        self._i += 1
        return v


class TestSpawnEventJobWorktreeReattach:
    """Re-attach-first worktree validation matrix (#3064 slice-4).

    Each test stands up a real worktree in the matching state and drives the
    real :func:`_validate_worktree_for_reuse` — the branch check,
    ``git rev-parse``, and lock-file scan all execute. A regression in any of
    them (e.g. an inverted branch comparison) breaks a test.
    """

    def test_reattach_valid_worktree_returns_volumes(self, tmp_path):
        """A healthy worktree on the expected branch validates and maps repos."""
        from kubernetes_spawner import _validate_worktree_for_reuse

        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH)
        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            vols = _validate_worktree_for_reuse(_WT_ID, _REPOS, _BRANCH)

        assert vols is not None
        assert vols["owner/repo"] == str(repo)

    def test_reattach_wrong_branch_falls_back(self, tmp_path):
        """A worktree checked out on a different branch ⇒ None (fall back)."""
        from kubernetes_spawner import _validate_worktree_for_reuse

        _make_worktree(tmp_path, _WT_ID, "repo", "egg/some-other-branch")
        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            vols = _validate_worktree_for_reuse(_WT_ID, _REPOS, _BRANCH)

        assert vols is None

    def test_reattach_corrupt_git_falls_back(self, tmp_path):
        """A directory whose ``.git`` is gone ⇒ rev-parse fails ⇒ None."""
        from kubernetes_spawner import _validate_worktree_for_reuse

        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH)
        shutil.rmtree(repo / ".git")  # corrupt: no longer a git repo
        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            vols = _validate_worktree_for_reuse(_WT_ID, _REPOS, _BRANCH)

        assert vols is None

    def test_reattach_foreign_lock_falls_back(self, tmp_path):
        """An ``index.lock`` in ``.git`` ⇒ None (a writer may be mid-operation)."""
        from kubernetes_spawner import _validate_worktree_for_reuse

        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH)
        (repo / ".git" / "index.lock").write_text("")
        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            vols = _validate_worktree_for_reuse(_WT_ID, _REPOS, _BRANCH)

        assert vols is None

    def test_reattach_ref_lock_falls_back(self, tmp_path):
        """A ``refs/heads/*.lock`` ⇒ None (ref update may be mid-flight)."""
        from kubernetes_spawner import _validate_worktree_for_reuse

        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH)
        heads = repo / ".git" / "refs" / "heads"
        heads.mkdir(parents=True, exist_ok=True)
        (heads / "wip.lock").write_text("")
        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            vols = _validate_worktree_for_reuse(_WT_ID, _REPOS, _BRANCH)

        assert vols is None

    def test_reattach_missing_worktree_falls_back(self, tmp_path):
        """No worktree directory at all ⇒ None."""
        from kubernetes_spawner import _validate_worktree_for_reuse

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            vols = _validate_worktree_for_reuse("does-not-exist", _REPOS, _BRANCH)

        assert vols is None

    def test_try_reuse_composes_validate_and_clean(self, spawner, tmp_path):
        """End-to-end: a healthy worktree with origin validates AND cleans."""
        _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            result = spawner._try_reuse_worktree(_WT_ID, _BRANCH, _REPOS)

        assert result is not None
        success, repo_volumes = result
        assert success
        assert "owner/repo" in repo_volumes


class TestSpawnEventJobDirtyWorktree:
    """Dirty-state policy (architect R6) for re-attached worktrees (#3064 slice-4).

    On every successful re-attach the spawner discards uncommitted changes and
    untracked artifacts (reset --hard + clean -fd) and hard-syncs to the role
    branch tip. A predecessor's residue — including a *committed-but-unpushed*
    commit — must never reach a successor. If discard OR hard-sync fails, the
    spawner falls back to recreate.
    """

    def test_reattach_discards_uncommitted_changes(self, spawner, tmp_path):
        """A re-attached worktree's dirty/untracked state is discarded."""
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH)
        (repo / "seed.txt").write_text("DIRTY — uncommitted edit\n")  # modify tracked
        (repo / "untracked.txt").write_text("staging residue\n")  # untracked

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            # branch=None exercises the discard half in isolation (no hard-sync).
            cleaned = spawner._clean_reused_worktree(_WT_ID, None, _REPOS)

        assert cleaned is True
        assert (repo / "seed.txt").read_text() == "seed\n"  # reverted
        assert not (repo / "untracked.txt").exists()  # removed
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    def test_reattach_discard_failure_falls_back(self, spawner, tmp_path):
        """When discard itself fails (filesystem error) ⇒ recreate (False)."""
        repo_dir = tmp_path / _WT_ID / "repo"
        repo_dir.mkdir(parents=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("subprocess.run", side_effect=OSError("Permission denied on reset --hard")),
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS)

        assert cleaned is False

    def test_reattach_residue_not_in_successor_view(self, spawner, tmp_path):
        """A predecessor's unpushed commit + dirty tree is provably gone after clean.

        Seeds a real worktree with (a) an uncommitted edit, (b) an untracked
        file, and (c) a *committed-but-never-pushed* commit ahead of origin —
        exactly the residue a pod killed mid-event leaves behind. After
        ``_clean_reused_worktree`` the successor's HEAD is back at the origin
        tip and none of the residue survives in its working tree.
        """
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        origin_head = _git(repo, "rev-parse", f"origin/{_BRANCH}").stdout.strip()

        # Predecessor leaves an unpushed local commit ...
        (repo / "residue.txt").write_text("unproposed work\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "predecessor residue (never pushed)")
        residue_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert residue_head != origin_head
        # ... and a dirty working tree.
        (repo / "dirty.txt").write_text("uncommitted\n")

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS)

        assert cleaned is True
        # HEAD is back at the origin tip — the predecessor commit is gone.
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
        # No residue survives in the successor's view.
        assert not (repo / "residue.txt").exists()
        assert not (repo / "dirty.txt").exists()
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    def test_reattach_hard_sync_failure_falls_back(self, spawner, tmp_path):
        """Hard-sync failure is FATAL to reuse (#3064 review): recreate instead.

        The worktree is clean and on the right branch, but has no reachable
        ``origin`` remote, so ``git fetch origin <branch>`` fails. Because the
        hard-sync is the only step that drops a predecessor's unpushed commit,
        a failure must fall back to recreate rather than continue on the
        current HEAD (which could still carry residue ahead of origin).
        """
        _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=False)

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS)

        assert cleaned is False

    def test_reattach_pristine_worktree_succeeds(self, spawner, tmp_path):
        """A pristine worktree with origin cleans and hard-syncs successfully."""
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        origin_head = _git(repo, "rev-parse", f"origin/{_BRANCH}").stdout.strip()

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""


class TestSpawnEventJobSessionReuse:
    """Per-role gateway-session reuse across one-shot event spawns (#3064 slice-4).

    A single session is registered under the STABLE per-role+slice base
    ``container_id`` (not the per-event Job name) so it is reused across the
    distinct Job names of successive events. Teardown happens at phase end (via
    :meth:`cleanup_pipeline`) or streak exhaustion. These drive the real
    production path (:meth:`spawn_event_job` → :meth:`_get_or_create_session`),
    not a divergent helper.
    """

    def test_reuses_live_session(self, spawner, mock_gateway):
        """A live, un-aged cached session is reused — no register_session call."""
        cache_key = ("pipe-1", "coder", "slice-4", "egg-agent-pipe-1-slice-4-coder")
        spawner._session_token_cache[cache_key] = "tok-live-abcdef"
        mock_gateway.heartbeat_session_by_container.return_value = True

        session = spawner._get_or_create_session(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            slice_id="slice-4",
            mode="public",
            repos=["owner/repo"],
        )

        assert session is not None
        assert session.session_token == "tok-live-abcdef"
        mock_gateway.register_session.assert_not_called()

    def test_aged_out_session_re_registers(self, spawner, mock_gateway):
        """An aged-out session triggers re-registration under the base id."""
        cache_key = ("pipe-1", "coder", "slice-4", "egg-agent-pipe-1-slice-4-coder")
        spawner._session_token_cache[cache_key] = "tok-stale"
        mock_gateway.heartbeat_session_by_container.return_value = False
        mock_gateway.register_session.return_value = _FakeSessionInfo(
            session_token="tok-fresh-ghijkl",
            container_id="egg-agent-pipe-1-slice-4-coder",
        )

        session = spawner._get_or_create_session(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            slice_id="slice-4",
            mode="public",
            repos=["owner/repo"],
        )

        assert session is not None
        assert session.session_token == "tok-fresh-ghijkl"
        mock_gateway.register_session.assert_called_once()
        # The fresh token is cached under the stable base id for next time.
        assert spawner._session_token_cache[cache_key] == "tok-fresh-ghijkl"

    def test_no_prior_session_registers(self, spawner, mock_gateway):
        """No prior session at all ⇒ fresh registration under the base id."""
        mock_gateway.heartbeat_session_by_container.return_value = False
        mock_gateway.register_session.return_value = _FakeSessionInfo(
            session_token="tok-first-time",
            container_id="egg-agent-pipe-1-slice-4-coder",
        )

        session = spawner._get_or_create_session(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            slice_id="slice-4",
            mode="public",
            repos=["owner/repo"],
        )

        assert session is not None
        assert session.session_token == "tok-first-time"
        mock_gateway.register_session.assert_called_once()
        # Registered under the STABLE base id, not a per-event Job name.
        assert (
            mock_gateway.register_session.call_args.kwargs["container_id"]
            == "egg-agent-pipe-1-slice-4-coder"
        )

    def test_session_reused_across_distinct_events(
        self, spawner, mock_k8s_client, mock_gateway, tmp_path
    ):
        """The core fix: two DISTINCT events for one role register the session once.

        Before the fix the cache key embedded the per-event Job-name
        discriminator, so every distinct event (propose, ack, …) missed the
        cache and re-registered. With the session keyed on the stable base id,
        the second event's re-attach + cache hit reuses the first event's
        session — ``register_session`` fires exactly once across both spawns.
        """
        _make_worktree(tmp_path, "pipe-1-slice-4-coder", "repo", _BRANCH, with_origin=True)
        mock_k8s_client.list_jobs.return_value = []  # no live Job → no adoption
        mock_gateway.heartbeat_session_by_container.return_value = True
        mock_gateway.register_session.return_value = _FakeSessionInfo(
            session_token="tok-shared",
            container_id="egg-agent-pipe-1-slice-4-coder",
        )

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            for key in ("a" * 64, "b" * 64):  # two DISTINCT event dedupe keys
                spawner.spawn_event_job(
                    pipeline_id="pipe-1",
                    agent_role=AgentRole.CODER,
                    action="propose",
                    dedupe_key=key,
                    slice_id="slice-4",
                    phase="implement",
                    repos=["owner/repo"],
                    branch=_BRANCH,
                    wait_for_gateway=False,
                )

        # Registered once; the second event reused the cached session.
        mock_gateway.register_session.assert_called_once()
        assert (
            mock_gateway.register_session.call_args.kwargs["container_id"]
            == "egg-agent-pipe-1-slice-4-coder"
        )

    def test_teardown_session_deletes_by_base_id_and_evicts(self, spawner, mock_gateway):
        """The teardown primitive deletes the session by base id and evicts the cache.

        This is the shared delete-and-evict unit used by BOTH teardown callers:
        ``cleanup_pipeline`` (phase end, driven by ``test_teardown_at_phase_end``)
        and the streak-exhaustion path (the supervisor's ``on_exhausted`` hook →
        the ``spawn_fn`` teardown closure → here, with the production trigger
        driven in ``test_event_loop`` / ``test_concurrent_executor``). It is no
        longer a dead method whose only caller is this test.
        """
        base = "egg-agent-pipe-1-slice-4-coder"
        cache_key = ("pipe-1", "coder", "slice-4", base)
        spawner._session_token_cache[cache_key] = "tok-x"

        spawner._teardown_session("pipe-1", AgentRole.CODER, slice_id="slice-4")

        mock_gateway.delete_session_by_container.assert_called_once_with(base)
        assert cache_key not in spawner._session_token_cache

    def test_teardown_event_session_closure_routes_to_teardown(self, spawner, mock_gateway):
        """``create_concurrent_spawn_fn`` exposes a teardown closure on ``spawn_fn``.

        The executor's streak-exhaustion wiring reaches ``_teardown_session``
        through this closure (it holds no direct spawner reference), so verify
        the closure targets the stable per-role+slice base id.
        """
        base = "egg-agent-pipe-1-slice-4-coder"
        cache_key = ("pipe-1", "coder", "slice-4", base)
        spawner._session_token_cache[cache_key] = "tok-z"

        spawn_fn = spawner.create_concurrent_spawn_fn(
            pipeline_id="pipe-1",
            issue_number=1,
            repo_volumes=None,
            mode="public",
            repos=["owner/repo"],
            phase="implement",
            slice_id="slice-4",
        )
        teardown = getattr(spawn_fn, "teardown_event_session", None)
        assert teardown is not None, "spawn_fn must expose teardown_event_session"

        teardown(AgentRole.CODER)

        mock_gateway.delete_session_by_container.assert_called_once_with(base)
        assert cache_key not in spawner._session_token_cache

    def test_teardown_at_phase_end(self, spawner, mock_gateway):
        """cleanup_pipeline tears down reused event-mode sessions (phase end)."""
        base = "egg-agent-pipe-1-slice-4-coder"
        cache_key = ("pipe-1", "coder", "slice-4", base)
        spawner._session_token_cache[cache_key] = "tok-y"
        # A session for an unrelated pipeline must survive this cleanup.
        other_key = ("pipe-2", "coder", None, "egg-agent-pipe-2-coder")
        spawner._session_token_cache[other_key] = "tok-other"

        with (
            patch.object(spawner, "list_pipeline_jobs", return_value=[]),
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", Path("/nonexistent-egg-wt")),
            patch("kubernetes_spawner.agent_salvage.auto_salvage_pipeline"),
        ):
            spawner.cleanup_pipeline("pipe-1")

        mock_gateway.delete_session_by_container.assert_any_call(base)
        assert cache_key not in spawner._session_token_cache
        assert other_key in spawner._session_token_cache  # untouched

    def test_pod_mode_session_keyed_by_job_name(self, spawner, mock_k8s_client, mock_gateway):
        """Pod-mode spawn (no session_container_id) registers/caches by Job name.

        The session-reuse changes must not alter pod-mode: with
        ``session_container_id`` unset, the gateway session is registered under
        the per-Job name exactly as before, so the existing stop/remove cleanup
        paths still target the right key.
        """
        mock_gateway.register_session.return_value = _FakeSessionInfo(
            session_token="tok-pod",
            container_id="egg-agent-pipe-1-coder",
        )
        spawner.spawn_agent_job(
            "pipe-1",
            AgentRole.CODER,
            repos=["owner/repo"],
            reuse_worktree_id="pipe-1-coder",  # skip worktree creation in this unit test
            repo_volumes={"owner/repo": "/x"},
            wait_for_gateway=False,
        )

        assert (
            mock_gateway.register_session.call_args.kwargs["container_id"]
            == "egg-agent-pipe-1-coder"
        )
        # Cached under the Job name (pod-mode key), not a stable override.
        assert ("pipe-1", "coder", None, "egg-agent-pipe-1-coder") in spawner._session_token_cache


class TestSpawnEventJobAtMostOneLivePod:
    """The slice-2 at-most-one-live-pod-per-role+slice invariant is the
    ownership story for safe re-attach — no concurrent writers to one
    worktree. (#3064 slice-4)
    """

    def test_spawn_event_job_enforces_at_most_one_live_pod(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """spawn_event_job must not create a duplicate pod while a live
        Job with the same dedupe key already exists — adoption returns
        None rather than creating a duplicate. This is the defense-in-depth
        backstop that ensures at-most-one-writer to the worktree.
        """
        _KEY = "e" * 64

        # First spawn: no live Job for the key → new Job created.
        mock_k8s_client.list_jobs.return_value = []
        spawner.spawn_event_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            action="propose",
            dedupe_key=_KEY,
            slice_id="slice-4",
            phase="implement",
            repos=["owner/repo"],
        )
        # Second spawn with the same key while Job is live → adoption (None).
        # The Job must carry a *live* status (PENDING/CREATING/RUNNING) — the
        # adoption filter (#3181) ignores terminal Jobs lingering in the TTL
        # window, so a status-less mock would be treated as "not live".
        existing = MagicMock()
        existing.labels = {"egg.event.dedupe-key": _KEY[:63]}
        existing.job_name = "egg-agent-pipe-1-slice-4-coder-ev123456"
        existing.status = ContainerStatus.RUNNING
        mock_k8s_client.list_jobs.return_value = [existing]

        second = spawner.spawn_event_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            action="propose",
            dedupe_key=_KEY,
            slice_id="slice-4",
            phase="implement",
            repos=["owner/repo"],
        )
        # Second call returned None (adopted existing, did not create a new Job).
        assert second is None


class TestSpawnAgentJobSpawnMsTiming:
    """``SpawnedContainer.spawn_ms`` is the finer-grained ``spawn_agent_job``
    sub-segment timer (#3064 slice-4).

    It is telemetry covering only the ``spawn_agent_job`` body — NOT the
    authoritative spawn→invoke budget. The p50<60s budget must span the whole
    ``spawn_event`` (re-attach + clean + hard-sync + session resolve) and is
    asserted against the slice-2 ``EventDecision.timing['spawn_dispatch_seconds']``
    field in ``test_event_loop.py`` (``TestLatencyBudgetFromTimingField``). These
    tests only pin that ``spawn_ms`` faithfully reflects an injected clock, so a
    consumer reading it gets an accurate sub-metric.
    """

    def _spawn_with_clock(self, spawner, deltas_ms):
        """Spawn once per delta with an injected clock; return measured spawn_ms."""
        spawner._clock = _FakeClock(deltas_ms)
        samples = []
        for i, _ in enumerate(deltas_ms):
            sc = spawner.spawn_agent_job(
                "pipe-1",
                AgentRole.CODER,
                slice_id="slice-4",
                repos=["owner/repo"],
                # Re-attach + session reuse simulated: no worktree create, no
                # gateway registration — isolates the spawn timer.
                reuse_worktree_id="issue-3064-slice-4-coder",
                repo_volumes={"owner/repo": "/x"},
                existing_session_token="tok-live",
                wait_for_gateway=False,
                job_name_suffix=f"ev{i}",
            )
            samples.append(sc.spawn_ms)
        return samples

    def test_spawn_ms_matches_injected_clock(self, spawner, mock_k8s_client):
        """``spawn_ms`` equals the simulated per-spawn delta exactly (no sleeps)."""
        deltas = [5_000, 9_000, 8_000, 12_000, 7_000]  # ms per spawn
        samples = self._spawn_with_clock(spawner, deltas)

        # The timing field is real and matches the simulated clock exactly.
        assert all(abs(s - d) < 1.0 for s, d in zip(samples, deltas, strict=True))

    def test_spawn_ms_tracks_large_deltas(self, spawner, mock_k8s_client):
        """The field stays accurate for large (≥60s) sub-segment latencies too,
        so a slow ``spawn_agent_job`` body is faithfully reported to a consumer.
        """
        deltas = [61_000, 65_000, 62_000]  # ms per spawn
        samples = self._spawn_with_clock(spawner, deltas)

        assert all(abs(s - d) < 1.0 for s, d in zip(samples, deltas, strict=True))
        assert statistics.median(samples) >= 60_000
