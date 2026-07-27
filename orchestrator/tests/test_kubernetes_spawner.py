"""
Tests for the KubernetesSpawner.

Covers Job spawning, gateway session integration, restart tracking,
pipeline cleanup, and error handling.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import statistics
import subprocess
import tokenize
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
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
# TestContextDisciplineFlagForward (#3200)
# ---------------------------------------------------------------------------


class TestForwardedDisciplineEnvHelper:
    """The pure ``_forwarded_discipline_env`` selector."""

    def test_selects_only_set_non_blank_flags(self):
        from kubernetes_spawner import _forwarded_discipline_env

        # Non-blank filter, not truthiness: a "false" value is forwarded as-is
        # (and parsed OFF in-pod), while unrelated keys are dropped.
        source = {
            "EGG_CONTEXT_DISCIPLINE": "true",
            "EGG_SESSION_RESUME": "false",
            "UNRELATED": "x",
        }
        assert _forwarded_discipline_env(source) == {
            "EGG_CONTEXT_DISCIPLINE": "true",
            "EGG_SESSION_RESUME": "false",
        }

    def test_omits_unset_and_blank(self):
        from kubernetes_spawner import _forwarded_discipline_env

        # Blank / whitespace-only never forwards as an empty string — the pod's
        # default-OFF parse must be identical to the flag being absent.
        assert _forwarded_discipline_env({"EGG_CONTEXT_DISCIPLINE": "  "}) == {}
        assert _forwarded_discipline_env({}) == {}

    def test_covers_every_declared_key(self):
        from kubernetes_spawner import (
            _FORWARDED_DISCIPLINE_ENV_KEYS,
            _forwarded_discipline_env,
        )

        source = dict.fromkeys(_FORWARDED_DISCIPLINE_ENV_KEYS, "on")
        assert set(_forwarded_discipline_env(source)) == set(_FORWARDED_DISCIPLINE_ENV_KEYS)


class TestContextDisciplineFlagForward:
    """spawn_agent_job forwards the orchestrator's context-discipline flags."""

    def test_flags_forwarded_into_pod_env_when_set(
        self, spawner, mock_k8s_client, mock_gateway, monkeypatch
    ):
        monkeypatch.setenv("EGG_CONTEXT_DISCIPLINE", "true")
        monkeypatch.setenv("EGG_CONTEXT_MEASUREMENT", "true")
        monkeypatch.setenv("EGG_SESSION_RESUME", "1")
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-cd",
            agent_role=AgentRole.CODER,
            phase="implement",
            branch="egg/issue-cd",
            repos=["owner/repo"],
        )
        env = result.environment
        assert env["EGG_CONTEXT_DISCIPLINE"] == "true"
        # #3277: the measurement knob must ride along, else #3271's emit
        # surfaces no-op in-pod and the proving run captures zero metrics.
        assert env["EGG_CONTEXT_MEASUREMENT"] == "true"
        assert env["EGG_SESSION_RESUME"] == "1"

    def test_flags_absent_when_unset(self, spawner, mock_k8s_client, mock_gateway, monkeypatch):
        monkeypatch.delenv("EGG_CONTEXT_DISCIPLINE", raising=False)
        monkeypatch.delenv("EGG_CONTEXT_MEASUREMENT", raising=False)
        monkeypatch.delenv("EGG_SESSION_RESUME", raising=False)
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-cd2",
            agent_role=AgentRole.CODER,
            phase="implement",
            branch="egg/issue-cd2",
            repos=["owner/repo"],
        )
        env = result.environment
        assert "EGG_CONTEXT_DISCIPLINE" not in env
        assert "EGG_CONTEXT_MEASUREMENT" not in env
        assert "EGG_SESSION_RESUME" not in env

    def test_extra_env_overrides_forwarded_flag(
        self, spawner, mock_k8s_client, mock_gateway, monkeypatch
    ):
        # A per-spawn extra_env value wins over the orchestrator-global forward
        # (the forward runs before the extra_env merge).
        monkeypatch.setenv("EGG_CONTEXT_DISCIPLINE", "true")
        result = spawner.spawn_agent_job(
            pipeline_id="pipe-cd3",
            agent_role=AgentRole.CODER,
            phase="implement",
            branch="egg/issue-cd3",
            repos=["owner/repo"],
            extra_env={"EGG_CONTEXT_DISCIPLINE": "false"},
        )
        assert result.environment["EGG_CONTEXT_DISCIPLINE"] == "false"


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


class TestRemoveAgentJobLogCapture:
    """Pre-removal log capture into the agent-log store (#3547)."""

    @pytest.fixture(autouse=True)
    def _fakeredis_store(self):
        import agent_log_store
        import fakeredis
        from agent_log_store import AgentLogStore

        agent_log_store.set_agent_log_store(AgentLogStore(fakeredis.FakeRedis()))
        yield
        agent_log_store.reset_agent_log_store()

    def test_remove_persists_snapshot_before_deletion(self, spawner, mock_k8s_client):
        from agent_log_store import get_agent_log_store

        mock_k8s_client.read_job_log_snapshot.return_value = {
            "job_name": "job-name",
            "pod_name": "job-name-xyz",
            "pipeline_id": "issue-1",
            "agent_role": "coder",
            "slice_id": "slice-3",
            "exit_code": 137,
            "logs": "agent stdout\n",
        }
        spawner.remove_agent_job("job-name")
        mock_k8s_client.remove_container.assert_called_once_with("job-name", force=False)
        rec = get_agent_log_store().get("issue-1", "job-name")
        assert rec["logs"] == "agent stdout\n"
        assert rec["agent_role"] == "coder"
        assert rec["slice_id"] == "slice-3"
        assert rec["exit_code"] == 137

    def test_remove_proceeds_when_snapshot_unavailable(self, spawner, mock_k8s_client):
        """Pod already GC'd (snapshot None); removal is not blocked."""
        mock_k8s_client.read_job_log_snapshot.return_value = None
        spawner.remove_agent_job("job-name")
        mock_k8s_client.remove_container.assert_called_once_with("job-name", force=False)

    def test_remove_proceeds_when_snapshot_raises(self, spawner, mock_k8s_client):
        mock_k8s_client.read_job_log_snapshot.side_effect = RuntimeError("api down")
        spawner.remove_agent_job("job-name")
        mock_k8s_client.remove_container.assert_called_once_with("job-name", force=False)

    def test_no_capture_without_pipeline_label(self, spawner, mock_k8s_client):
        """A pod with no pipeline label has no operator-facing key; skip."""
        from agent_log_store import get_agent_log_store

        mock_k8s_client.read_job_log_snapshot.return_value = {
            "job_name": "job-name",
            "pipeline_id": None,
            "logs": "orphan logs",
        }
        spawner.remove_agent_job("job-name")
        assert get_agent_log_store().list_records("issue-1") == []
        mock_k8s_client.remove_container.assert_called_once()


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
# TestSpawnTimePhaseResolution (#3528)
# ---------------------------------------------------------------------------


class TestSpawnTimePhaseResolution:
    """Spawn-time phase resolution in create_concurrent_spawn_fn (#3528).

    The spawn callable closure-captures ``phase`` at wiring time, and the
    event-loop wiring holding it can outlive that phase (a stale driver
    thread, or a dual-role agent (the refine+plan simplifier) whose one-shot
    event spawns straddle a transition). Sessions minted through stale
    wiring carried the old phase, the gateway's commit gate denied every
    commit, and consensus deadlocked. With a ``phase_resolver`` wired, the
    phase is resolved live on every spawn.
    """

    def _spawn_fn(self, spawner, phase_resolver):
        return spawner.create_concurrent_spawn_fn(
            pipeline_id="issue-3523",
            issue_number=3523,
            repo_volumes=None,
            mode="public",
            repos=["owner/repo"],
            phase="refine",  # wiring-time phase, now stale
            phase_resolver=phase_resolver,
        )

    def test_pod_spawn_uses_live_phase(self, spawner, mock_k8s_client, mock_gateway):
        """A pod-mode spawn registers its session with the RESOLVED phase."""
        fn = self._spawn_fn(spawner, lambda: "plan")
        result = fn(AgentRole.SIMPLIFIER, branch="egg/issue-3523/work")
        assert result is not None
        assert mock_gateway.register_session.call_args.kwargs["phase"] == "plan"
        # The pod's env agrees with the session's phase.
        assert result.environment.get("EGG_PHASE") == "plan"

    def test_event_spawn_uses_live_phase(self, spawner, mock_k8s_client, mock_gateway):
        """The dual-role regression: a one-shot event spawn issued through
        wiring captured in refine mints its session with the pipeline's
        CURRENT phase, so its commits are permitted post-transition."""
        fn = self._spawn_fn(spawner, lambda: "plan")
        with patch.object(spawner, "spawn_event_job") as mock_event_job:
            fn(
                AgentRole.SIMPLIFIER,
                branch="egg/issue-3523/work",
                event_action="propose",
                event_dedupe_key="d" * 64,
            )
        assert mock_event_job.call_args.kwargs["phase"] == "plan"

    def test_resolver_failure_falls_back_to_captured_phase(
        self, spawner, mock_k8s_client, mock_gateway
    ):
        """A failing resolver degrades to the wiring-time phase, never wedges."""

        def _boom() -> str:
            raise RuntimeError("state store unavailable")

        fn = self._spawn_fn(spawner, _boom)
        result = fn(AgentRole.SIMPLIFIER, branch="egg/issue-3523/work")
        assert result is not None
        assert mock_gateway.register_session.call_args.kwargs["phase"] == "refine"

    def test_no_resolver_uses_captured_phase(self, spawner, mock_k8s_client, mock_gateway):
        """Without a resolver (legacy callers), behavior is unchanged."""
        fn = self._spawn_fn(spawner, None)
        result = fn(AgentRole.SIMPLIFIER, branch="egg/issue-3523/work")
        assert result is not None
        assert mock_gateway.register_session.call_args.kwargs["phase"] == "refine"

    def test_resolver_returning_enum_is_normalised(self, spawner, mock_k8s_client, mock_gateway):
        """A resolver returning a PipelinePhase enum resolves to its value."""
        from models import PipelinePhase

        fn = self._spawn_fn(spawner, lambda: PipelinePhase.PLAN)
        result = fn(AgentRole.SIMPLIFIER, branch="egg/issue-3523/work")
        assert result is not None
        assert mock_gateway.register_session.call_args.kwargs["phase"] == "plan"


# ---------------------------------------------------------------------------
# TestSyncSessionPhases (#3528)
# ---------------------------------------------------------------------------


class TestSyncSessionPhases:
    """Phase-advance sweep over the cached gateway sessions (#3528).

    Sessions survive phase transitions (teardown only runs at pipeline end
    or arm exhaustion), so the advance paths sync every cached session of
    the pipeline to the new phase via the gateway's PATCH
    ``/sessions/<token>/phase`` route.
    """

    def test_syncs_only_matching_pipeline(self, spawner, mock_gateway):
        spawner._session_token_cache[("pipe-1", "simplifier", None, "egg-agent-pipe-1-s")] = "tok-a"
        spawner._session_token_cache[("pipe-1", "refiner", None, "egg-agent-pipe-1-r")] = "tok-b"
        spawner._session_token_cache[("pipe-2", "coder", None, "egg-agent-pipe-2-c")] = "tok-c"
        mock_gateway.update_session_phase.return_value = True

        updated = spawner.sync_session_phases("pipe-1", "plan")

        assert updated == 2
        synced_tokens = {c.args[0] for c in mock_gateway.update_session_phase.call_args_list}
        assert synced_tokens == {"tok-a", "tok-b"}
        for c in mock_gateway.update_session_phase.call_args_list:
            assert c.args[1] == "plan"

    def test_gateway_errors_are_swallowed(self, spawner, mock_gateway):
        """A failing gateway must never block the phase advance."""
        spawner._session_token_cache[("pipe-1", "simplifier", None, "egg-agent-pipe-1-s")] = "tok-a"
        spawner._session_token_cache[("pipe-1", "refiner", None, "egg-agent-pipe-1-r")] = "tok-b"
        mock_gateway.update_session_phase.side_effect = [RuntimeError("boom"), True]

        updated = spawner.sync_session_phases("pipe-1", "plan")

        assert updated == 1

    def test_empty_cache_is_noop(self, spawner, mock_gateway):
        assert spawner.sync_session_phases("pipe-1", "plan") == 0
        mock_gateway.update_session_phase.assert_not_called()


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
#   * set the event identity (EGG_EVENT_ACTION, EGG_EVENT_DEDUPE_KEY,
#     payload refs) in Job env (the EGG_EVENT_LOOP_OWNER flag was retired
#     in #3164 — the wrapper keys on EGG_EVENT_ACTION alone);
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
        # #3164 retired the EGG_EVENT_LOOP_OWNER ownership flag — the wrapper's
        # one-shot handler keys on EGG_EVENT_ACTION alone, so the event job no
        # longer injects an owner flag.
        assert "EGG_EVENT_LOOP_OWNER" not in env
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

    @staticmethod
    def _terminating_job(name="egg-agent-pipe-1-slice-2-coder-ev"):
        """A Job that has been deleted but has not gone away yet.

        Deliberately ``RUNNING``: that is the whole point — a Job under
        deletion keeps reporting its pre-delete status until its pods finish
        terminating, so only ``deletion_timestamp`` distinguishes it.
        """
        return ContainerInfo(
            container_id="uid-terminating",
            container_name=name,
            job_name=name,
            namespace="test-ns",
            status=ContainerStatus.RUNNING,
            deletion_timestamp=datetime(2026, 7, 25, 1, 49, 8, tzinfo=UTC),
        )

    def test_terminating_job_is_not_adopted(self, spawner, mock_k8s_client):
        """A Job under deletion must NOT be adopted (#3597).

        The incident: ``restart_agent`` deletes the role's one-shot Job and
        delegates the respawn to the event loop. Deletion is asynchronous, so
        for a few seconds the Job sits in ``Terminating`` still reporting
        ``RUNNING``. The next poll (~5s) matched it on the dedupe-key label,
        adopted it, and declined to spawn a replacement — then the adopted Job
        finished terminating. Net result: no pod, no Job, and because
        adoption re-arms the key in the loop's live set (where a missing Job
        reads as "still running"), the role stayed vanished indefinitely with
        the pipeline still reporting ``status: running``.
        """
        mock_k8s_client.list_jobs.return_value = [self._terminating_job()]
        mock_k8s_client.wait_for_job_gone.return_value = True

        spawner.spawn_event_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            action="propose",
            dedupe_key=self._KEY,
            slice_id="slice-2",
            phase="implement",
            repos=["owner/repo"],
        )

        assert mock_k8s_client.create_container.call_count == 1, (
            "a terminating Job must not be adopted — the role would vanish"
        )

    def test_terminating_job_is_waited_out_before_respawn(self, spawner, mock_k8s_client):
        """The replacement waits for the deleted Job's name to be free (#3597).

        A one-shot Job's name is derived from the dedupe key, so the
        replacement carries the SAME name as the Job being torn down.
        Creating into the deletion window returns 409 ``AlreadyExists`` (the
        #2655 race), so the spawn waits the corpse out first.
        """
        mock_k8s_client.list_jobs.return_value = [self._terminating_job()]
        mock_k8s_client.wait_for_job_gone.return_value = True
        call_order: list[str] = []
        mock_k8s_client.wait_for_job_gone.side_effect = lambda *a, **k: (
            call_order.append("wait"),
            True,
        )[1]
        created = mock_k8s_client.create_container.return_value
        mock_k8s_client.create_container.side_effect = lambda **kw: (
            call_order.append("create"),
            created,
        )[1]

        spawner.spawn_event_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            action="propose",
            dedupe_key=self._KEY,
            slice_id="slice-2",
            phase="implement",
            repos=["owner/repo"],
        )

        assert call_order == ["wait", "create"]
        waited_name = mock_k8s_client.wait_for_job_gone.call_args.args[0]
        assert waited_name == "egg-agent-pipe-1-slice-2-coder-ev"

    def test_spawn_proceeds_when_terminating_job_outlives_the_wait(self, spawner, mock_k8s_client):
        """A wait timeout is reported, never fatal (#3597).

        Overrunning the bounded wait degrades to "the create may 409 and the
        event loop retries next poll", which is recoverable; refusing to
        spawn would reproduce the silent-vanish this path exists to prevent.
        """
        mock_k8s_client.list_jobs.return_value = [self._terminating_job()]
        mock_k8s_client.wait_for_job_gone.return_value = False

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

    def test_spent_wait_budget_is_not_reported_as_an_observation(self, spawner, mock_k8s_client):
        """A Job the wait never ran for is "unobserved", not "still present" (#3597).

        "Terminating event Job still present" asserts a wait ran and found
        the Job there. When the shared budget is already spent, no wait ran
        at all, so that message is unsupportable — the same taxonomy the
        restart route applies on its side of this fix.
        """
        mock_k8s_client.list_jobs.return_value = [self._terminating_job()]
        # A waiter that WOULD confirm, to prove the budget check short-circuits
        # before it rather than the wait quietly succeeding.
        mock_k8s_client.wait_for_job_gone.return_value = True

        with (
            patch("kubernetes_spawner._events._EVENT_JOB_TERMINATION_WAIT_S", 0.0),
            patch("kubernetes_spawner._events.logger") as mock_logger,
        ):
            spawner.spawn_event_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                action="propose",
                dedupe_key=self._KEY,
                slice_id="slice-2",
                phase="implement",
                repos=["owner/repo"],
            )

        # Exhausting the budget never blocks the spawn — that is the whole point.
        assert mock_k8s_client.create_container.call_count == 1
        mock_k8s_client.wait_for_job_gone.assert_not_called()
        calls = mock_logger.warning.call_args_list
        messages = [c.args[0] for c in calls if c.args]
        assert any("teardown wait not performed" in m for m in messages)
        assert [c.kwargs.get("reason") for c in calls] == ["budget_exhausted"]
        assert not any("still present" in m for m in messages)

    def test_missing_wait_helper_is_logged_not_silently_skipped(self, spawner, mock_k8s_client):
        """A backend without the wait helper says so (#3597).

        The docstring promised "on timeout (or a k8s client without the wait
        helper) we log and let the spawn proceed"; the no-helper arm returned
        bare, so the spawn walked into a possible 409 with nothing in the log
        explaining why nothing waited.
        """
        mock_k8s_client.list_jobs.return_value = [self._terminating_job()]
        # ``getattr(..., None)`` only yields None if the attribute is really
        # absent — a bare MagicMock would hand back an auto-attribute.
        del mock_k8s_client.wait_for_job_gone

        with patch("kubernetes_spawner._events.logger") as mock_logger:
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
        calls = mock_logger.warning.call_args_list
        assert [c.kwargs.get("reason") for c in calls] == ["no_wait_helper"]
        assert not any("still present" in c.args[0] for c in calls if c.args)

    def test_unnamed_terminating_job_is_reported_not_silently_skipped(
        self, spawner, mock_k8s_client
    ):
        """A Job the listing did not name is a third no-observation path (#3597).

        It skipped the wait with no log at all, so the taxonomy the route
        applies held on two of the three skip-paths. It also must not be
        folded into the counts the other two report — nothing could ever have
        waited on it, whatever the backend's capabilities.
        """
        named = self._terminating_job()
        unnamed = ContainerInfo(
            container_id="uid-unnamed",
            container_name="",
            job_name=None,
            namespace="test-ns",
            status=ContainerStatus.RUNNING,
            deletion_timestamp=datetime(2026, 7, 25, 1, 49, 8, tzinfo=UTC),
        )
        mock_k8s_client.list_jobs.return_value = [named, unnamed]
        mock_k8s_client.wait_for_job_gone.return_value = True

        with patch("kubernetes_spawner._events.logger") as mock_logger:
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
        warnings = mock_logger.warning.call_args_list
        assert [c.kwargs.get("reason") for c in warnings] == ["unaddressable"]
        assert warnings[0].kwargs["terminating"] == 1
        # A bare count is not actionable: carry the id through so an operator
        # grepping ``reason=unaddressable`` gets the same handle the restart
        # route's counterpart line reports.
        assert warnings[0].kwargs["container_ids"] == ["uid-unnamed"]
        # Only the nameable Job was ever waitable, so only it is counted.
        infos = [
            c for c in mock_logger.info.call_args_list if c.args and "waiting for" in c.args[0]
        ]
        assert len(infos) == 1
        assert infos[0].kwargs["terminating"] == 1
        mock_k8s_client.wait_for_job_gone.assert_called_once()
        assert mock_k8s_client.wait_for_job_gone.call_args.args[0] == named.job_name

    def test_live_job_is_adopted_without_any_wait(self, spawner, mock_k8s_client):
        """The unchanged common path: a genuinely live Job is still adopted.

        Guards the #3597 fix against over-reach — only a Job carrying a
        deletion stamp loses adoptability; a healthy RUNNING one must still
        suppress the duplicate spawn (and must not pay for a wait).
        """
        live = ContainerInfo(
            container_id="uid-live",
            container_name="egg-agent-pipe-1-slice-2-coder-ev",
            job_name="egg-agent-pipe-1-slice-2-coder-ev",
            namespace="test-ns",
            status=ContainerStatus.RUNNING,
        )
        mock_k8s_client.list_jobs.return_value = [live]

        result = spawner.spawn_event_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            action="propose",
            dedupe_key=self._KEY,
            slice_id="slice-2",
            phase="implement",
            repos=["owner/repo"],
        )

        assert result is None, "a live Job must still be adopted"
        mock_k8s_client.create_container.assert_not_called()
        mock_k8s_client.wait_for_job_gone.assert_not_called()

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

    # -- auth-fatal exit code (#3373) --------------------------------------

    def _info_exit(self, status, exit_code):
        return ContainerInfo(
            container_id="uid-1",
            container_name="job-1",
            status=status,
            job_name="job-1",
            exit_code=exit_code,
        )

    def test_failed_job_with_auth_fatal_exit_code_is_fatal(self, spawner, mock_k8s_client):
        """A FAILED Job whose pod exited EX_AUTH_FATAL classifies as fatal."""
        import event_loop
        from egg_agent.auth_errors import EX_AUTH_FATAL

        mock_k8s_client.list_jobs.return_value = [self._info(ContainerStatus.FAILED)]
        mock_k8s_client.list_containers.return_value = [
            self._info_exit(ContainerStatus.FAILED, EX_AUTH_FATAL)
        ]
        view = spawner.create_event_job_status_view()
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_FATAL

    def test_auth_fatal_read_uses_dedupe_label(self, spawner, mock_k8s_client):
        """The exit-code read scopes to this event's dedupe-key label."""
        from egg_agent.auth_errors import EX_AUTH_FATAL

        mock_k8s_client.list_jobs.return_value = [self._info(ContainerStatus.FAILED)]
        mock_k8s_client.list_containers.return_value = [
            self._info_exit(ContainerStatus.FAILED, EX_AUTH_FATAL)
        ]
        spawner.create_event_job_status_view().outcome_for(self._KEY)
        _, kwargs = mock_k8s_client.list_containers.call_args
        assert kwargs["labels"] == {"egg.event.dedupe-key": self._KEY[:63]}

    def test_failed_job_with_other_exit_code_is_abnormal(self, spawner, mock_k8s_client):
        """A FAILED Job with an ordinary non-zero rc stays abnormal (retryable)."""
        import event_loop

        mock_k8s_client.list_jobs.return_value = [self._info(ContainerStatus.FAILED)]
        mock_k8s_client.list_containers.return_value = [self._info_exit(ContainerStatus.FAILED, 1)]
        view = spawner.create_event_job_status_view()
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_ABNORMAL

    def test_failed_job_exit_code_unreadable_falls_back_to_abnormal(self, spawner, mock_k8s_client):
        """If the pod exit code can't be read, classify abnormal (today's behaviour)."""
        import event_loop

        mock_k8s_client.list_jobs.return_value = [self._info(ContainerStatus.FAILED)]
        mock_k8s_client.list_containers.side_effect = RuntimeError("API down")
        view = spawner.create_event_job_status_view()
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_ABNORMAL

    def test_failed_job_no_pod_found_falls_back_to_abnormal(self, spawner, mock_k8s_client):
        """A FAILED Job whose pod is already GC'd stays abnormal, not fatal."""
        import event_loop

        mock_k8s_client.list_jobs.return_value = [self._info(ContainerStatus.FAILED)]
        mock_k8s_client.list_containers.return_value = []
        view = spawner.create_event_job_status_view()
        assert view.outcome_for(self._KEY) == event_loop.JOB_OUTCOME_ABNORMAL

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
        # Terminal sweep: pods have already exited, so background propagation.
        assert all(
            c.kwargs.get("force") is False for c in mock_k8s_client.remove_container.call_args_list
        )

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

    # -- reap (force, #3337 same-role supersession) ------------------------

    def test_reap_deletes_live_and_terminal_jobs(self, spawner, mock_k8s_client):
        """The force-reap removes a still-RUNNING Job too (unlike reap_terminated),
        so a superseded same-role sibling's live pod is actually torn down."""
        mock_k8s_client.list_jobs.return_value = [
            self._named(ContainerStatus.RUNNING, "live"),
            self._named(ContainerStatus.FAILED, "dead"),
            self._named(ContainerStatus.EXITED, "done"),
        ]
        view = spawner.create_event_job_status_view()
        assert view.reap(self._KEY) == 3
        removed = {c.args[0] for c in mock_k8s_client.remove_container.call_args_list}
        assert removed == {"live", "dead", "done"}
        # Supersession path tears down a still-RUNNING sibling, so it uses force
        # (foreground) propagation to drop the racing pod before returning.
        assert all(
            c.kwargs.get("force") is True for c in mock_k8s_client.remove_container.call_args_list
        )

    def test_reap_swallows_delete_errors(self, spawner, mock_k8s_client):
        """Force-reap is best-effort, like reap_terminated."""
        mock_k8s_client.list_jobs.return_value = [self._named(ContainerStatus.RUNNING, "live")]
        mock_k8s_client.remove_container.side_effect = RuntimeError("API down")
        view = spawner.create_event_job_status_view()
        assert view.reap(self._KEY) == 0

    def test_reap_no_jobs_is_noop(self, spawner, mock_k8s_client):
        """No matching Job reaps nothing and never deletes."""
        mock_k8s_client.list_jobs.return_value = []
        view = spawner.create_event_job_status_view()
        assert view.reap(self._KEY) == 0
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

    def wait_for_job_gone(self, name, namespace=None, timeout_s=0.0):
        """Model the reap completing: the Terminating Job finally disappears."""
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j.job_name != name]
        return len(self.jobs) < before or before == 0

    # --- test helpers -----------------------------------------------------
    def crash_all(self):
        self.jobs = [j.model_copy(update={"status": ContainerStatus.FAILED}) for j in self.jobs]

    def begin_delete_all(self):
        """Model an ACCEPTED but not-yet-complete k8s delete (#3597).

        This is what ``restart_agent``'s teardown looks like from the event
        loop's side for the seconds that follow: the Job is stamped with a
        ``deletionTimestamp`` and keeps reporting its pre-delete status
        until its pods finish terminating.
        """
        self.jobs = [
            j.model_copy(update={"deletion_timestamp": datetime(2026, 7, 25, 1, 49, 8, tzinfo=UTC)})
            for j in self.jobs
        ]

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
        mock_k8s_client.wait_for_job_gone.side_effect = store.wait_for_job_gone

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

    def test_restart_deleted_job_mid_termination_respawns(self, spawner, mock_k8s_client):
        """The restart race, end to end against the real spawner (#3597).

        ``restart_agent`` deletes the role's live Job and delegates the
        respawn to the event loop. k8s deletion is asynchronous, so for the
        next few seconds the Job is Terminating but still reports RUNNING.
        The loop's very next poll re-derives the same event — and used to
        adopt that corpse, create nothing, and leave the role with no pod
        and no Job while the pipeline still reported ``running``.

        Drives the real ``spawn_event_job`` against the stateful Job store so
        the adoption filter, the terminating-Job wait, and Job creation are
        exercised together rather than through a fake that always spawns.
        """
        store = _StatefulEventJobs()
        self._wire(store, mock_k8s_client)

        # 1. The role has a live one-shot Job.
        assert self._spawn(spawner) is not None
        assert store.statuses == [ContainerStatus.RUNNING]

        # 2. restart_agent deletes it; the delete is accepted but not complete.
        store.begin_delete_all()
        assert store.statuses == [ContainerStatus.RUNNING], "still reports RUNNING"

        # 3. The event loop's next poll lands inside the deletion window.
        assert self._spawn(spawner) is not None, "the respawn must not adopt a corpse"

        # A real replacement exists, and the corpse was waited out first so the
        # create could not 409 on the recycled Job name.
        assert mock_k8s_client.create_container.call_count == 2
        assert store.statuses == [ContainerStatus.RUNNING]
        assert all(j.deletion_timestamp is None for j in store.jobs)

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
    sync/reset target). Returns ``(repo_path, origin_path_or_None)``.
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

    def test_reattach_work_branch_head_validates(self, tmp_path):
        """The production shape (#3480): the gateway creates per-agent
        worktrees on ``egg/{container_id}/work``, not on the assigned branch,
        so a worktree with ``HEAD`` on the derived work branch must validate."""
        from kubernetes_spawner import _validate_worktree_for_reuse

        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", f"egg/{_WT_ID}/work")
        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            vols = _validate_worktree_for_reuse(_WT_ID, _REPOS, _BRANCH)

        assert vols is not None
        assert vols["owner/repo"] == str(repo)

    def test_reattach_other_agents_work_branch_falls_back(self, tmp_path):
        """A DIFFERENT agent's work branch is still a mismatch; only the work
        branch derived from this agent_worktree_id is accepted."""
        from kubernetes_spawner import _validate_worktree_for_reuse

        _make_worktree(tmp_path, _WT_ID, "repo", "egg/other-agent-id/work")
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

    def test_try_reuse_production_shape_work_branch(self, spawner, tmp_path):
        """End-to-end for the production shape (#3480): HEAD on the derived
        work branch, assigned branch only on origin. Validation accepts the
        work branch, and the sync KEEPS the clean-tree local commit strictly
        ahead of ``origin/{assigned}`` (fast-forward-aware, #3506)."""
        work_branch = f"egg/{_WT_ID}/work"
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", work_branch, with_origin=True)
        # Publish the assigned branch on origin (the sync target), then
        # advance the local work branch past it: the agent's own unpushed
        # multi-session work, committed on a clean exit.
        _git(repo, "push", "origin", f"{work_branch}:{_BRANCH}")
        (repo / "local.txt").write_text("unpushed\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "local unpushed commit")
        local_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            result = spawner._try_reuse_worktree(_WT_ID, _BRANCH, _REPOS)

        assert result is not None
        success, repo_volumes = result
        assert success
        assert repo_volumes["owner/repo"] == str(repo)
        # The agent's own clean fast-forward commit survives the re-attach
        # (#3506); the sync no longer hard-resets a strict descendant.
        assert (repo / "local.txt").read_text() == "unpushed\n"
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == local_head
        remote = _git(repo, "rev-parse", f"origin/{_BRANCH}").stdout.strip()
        assert (
            _git(repo, "merge-base", "--is-ancestor", remote, local_head, check=False).returncode
            == 0
        )


class TestReusePathHostPathTranslation:
    """Reuse-path ``repo_volumes`` must carry HOST paths (#3502).

    The create path hands the spawn path gateway-returned HOST paths; the
    re-attach path used to hand the validator's orchestrator-LOCAL paths
    (under ``WORKTREE_BASE_DIR``) straight into the Job spec's ``hostPath``
    mounts. On the node kubelet ``DirectoryOrCreate``d an empty root-owned
    dir at that local path, so every post-restart re-attach spawn booted
    into an empty worktree and no-oped with rc=0 — silently stalling slice
    consensus.
    """

    def test_local_to_host_path_translates_via_mount_mapping(self):
        from kubernetes_spawner import _local_to_host_path

        mapping = [
            ("/home/egg/.egg-worktrees", "/home/hostuser/.egg-worktrees"),
            ("/home/egg", "/home/hostuser"),
        ]
        assert (
            _local_to_host_path("/home/egg/.egg-worktrees/wt-1/repo", mapping)
            == "/home/hostuser/.egg-worktrees/wt-1/repo"
        )
        # Longest prefix wins (mapping is ordered longest-first).
        assert _local_to_host_path("/home/egg/other", mapping) == "/home/hostuser/other"
        # Exact mount-point match translates too.
        assert (
            _local_to_host_path("/home/egg/.egg-worktrees", mapping)
            == "/home/hostuser/.egg-worktrees"
        )
        # A sibling path that merely shares the string prefix does not match.
        assert _local_to_host_path("/home/egg-other/x", mapping[:1]) == "/home/egg-other/x"

    def test_local_to_host_path_falls_back_to_host_home(self, monkeypatch):
        from kubernetes_spawner import _local_to_host_path

        monkeypatch.setenv("HOST_HOME", "/home/hostuser")
        assert (
            _local_to_host_path("/home/egg/.egg-worktrees/wt-1/repo", [])
            == "/home/hostuser/.egg-worktrees/wt-1/repo"
        )
        # Non-/home/egg paths (already host paths) pass through unchanged.
        assert (
            _local_to_host_path("/home/hostuser/.egg-worktrees/wt-1/repo", [])
            == "/home/hostuser/.egg-worktrees/wt-1/repo"
        )

    def test_local_to_host_path_passthrough_without_translation(self, monkeypatch):
        from kubernetes_spawner import _local_to_host_path

        monkeypatch.delenv("HOST_HOME", raising=False)
        assert (
            _local_to_host_path("/home/egg/.egg-worktrees/wt-1/repo", [])
            == "/home/egg/.egg-worktrees/wt-1/repo"
        )

    def test_load_local_mount_mapping_skips_non_bind_entries(self, tmp_path):
        """Rootfs, tmpfs-style (root ``/``), and identity entries are skipped
        so translation is a no-op outside a container."""
        from kubernetes_spawner import _load_local_mount_mapping

        mountinfo = tmp_path / "mountinfo"
        mountinfo.write_text(
            # rootfs entry: mount_point "/" — skipped
            "22 1 0:20 / / rw - overlay overlay rw\n"
            # tmpfs /tmp: root "/" — skipped
            "40 22 0:33 / /tmp rw - tmpfs tmpfs rw\n"
            # identity mapping (subvolume mounted at its own path) — skipped
            "50 22 8:1 /home /home rw - btrfs /dev/sda1 rw\n"
            # genuine bind mount — kept
            "60 22 8:1 /home/hostuser/.egg-worktrees /home/egg/.egg-worktrees rw - btrfs x rw\n"
            # short/malformed line — ignored
            "61 22 8:1\n"
        )
        assert _load_local_mount_mapping(str(mountinfo)) == [
            ("/home/egg/.egg-worktrees", "/home/hostuser/.egg-worktrees")
        ]

    def test_try_reuse_returns_host_paths(self, spawner, tmp_path):
        """The #3502 regression: reuse-path repo_volumes are HOST paths."""
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        mapping = [(str(tmp_path), "/home/hostuser/.egg-worktrees")]
        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("kubernetes_spawner._worktree._LOCAL_MOUNT_MAPPING", mapping),
        ):
            result = spawner._try_reuse_worktree(_WT_ID, _BRANCH, _REPOS)

        assert result is not None
        _, repo_volumes = result
        assert repo_volumes["owner/repo"] == f"/home/hostuser/.egg-worktrees/{_WT_ID}/repo"

    def test_try_reuse_no_translation_is_identity(self, spawner, tmp_path):
        """Without a mount mapping or HOST_HOME the paths pass through
        unchanged (bare-host behavior; matches the pre-fix return)."""
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("kubernetes_spawner._worktree._LOCAL_MOUNT_MAPPING", []),
        ):
            result = spawner._try_reuse_worktree(_WT_ID, _BRANCH, _REPOS)

        assert result is not None
        assert result[1]["owner/repo"] == str(repo)

    def test_spawn_rejects_orchestrator_local_repo_volumes(self, spawner):
        """The #3502 tripwire: a translatable (i.e. orchestrator-local) path
        reaching the spawn path's hostPath mounts fails the spawn loudly
        instead of mounting an empty dir the agent no-ops in."""
        from kubernetes_spawner import KubernetesSpawnError

        mapping = [("/home/egg/.egg-worktrees", "/home/hostuser/.egg-worktrees")]
        with (
            patch("kubernetes_spawner._worktree._LOCAL_MOUNT_MAPPING", mapping),
            pytest.raises(KubernetesSpawnError, match="orchestrator-local"),
        ):
            spawner.spawn_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
                reuse_worktree_id="pipe-1-coder",
                repo_volumes={"owner/repo": "/home/egg/.egg-worktrees/pipe-1-coder/repo"},
            )

    def test_spawn_accepts_host_repo_volumes(self, spawner, mock_k8s_client):
        """Host paths (untranslatable) pass the tripwire and reach the mounts."""
        mapping = [("/home/egg/.egg-worktrees", "/home/hostuser/.egg-worktrees")]
        with patch("kubernetes_spawner._worktree._LOCAL_MOUNT_MAPPING", mapping):
            spawner.spawn_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                repos=["owner/repo"],
                reuse_worktree_id="pipe-1-coder",
                repo_volumes={"owner/repo": "/home/hostuser/.egg-worktrees/pipe-1-coder/repo"},
            )
        mounts = mock_k8s_client.create_container.call_args.kwargs["host_path_mounts"]
        assert mounts == [
            {
                "name": "repo-owner-repo",
                "host_path": "/home/hostuser/.egg-worktrees/pipe-1-coder/repo",
                "container_path": "/home/egg/repos/repo",
                "read_only": False,
            }
        ]


class TestReusePathWorktreeContainerIdBinding:
    """Reuse-path session registration binds the validated worktree (#3502).

    Without ``worktree_container_id``, a fresh registration on the re-attach
    path makes the gateway create an orphan worktree keyed by the session's
    ``container_id`` (the ``egg-agent-…`` Job base name) that no Job spec
    ever mounts — the naming split observed in the #3502 incident.
    """

    def test_get_or_create_session_forwards_worktree_container_id(self, spawner, mock_gateway):
        mock_gateway.heartbeat_session_by_container.return_value = False
        mock_gateway.register_session.return_value = _FakeSessionInfo(
            session_token="tok-bound-worktree",
            container_id="egg-agent-pipe-1-slice-4-coder",
        )

        session = spawner._get_or_create_session(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            slice_id="slice-4",
            mode="public",
            repos=["owner/repo"],
            worktree_container_id="pipe-1-slice-4-coder",
        )

        assert session is not None
        kwargs = mock_gateway.register_session.call_args.kwargs
        assert kwargs["container_id"] == "egg-agent-pipe-1-slice-4-coder"
        assert kwargs["worktree_container_id"] == "pipe-1-slice-4-coder"

    def test_event_reattach_registration_binds_validated_worktree(
        self, spawner, mock_k8s_client, mock_gateway, tmp_path
    ):
        """End-to-end: a re-attach spawn whose session registers fresh passes
        the validated worktree id to the gateway."""
        _make_worktree(tmp_path, "pipe-1-slice-4-coder", "repo", _BRANCH, with_origin=True)
        mock_k8s_client.list_jobs.return_value = []  # no live Job → no adoption
        mock_gateway.heartbeat_session_by_container.return_value = False
        mock_gateway.register_session.return_value = _FakeSessionInfo(
            session_token="tok-reattach",
            container_id="egg-agent-pipe-1-slice-4-coder",
        )

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            spawner.spawn_event_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                action="propose",
                dedupe_key="c" * 64,
                slice_id="slice-4",
                phase="implement",
                repos=["owner/repo"],
                branch=_BRANCH,
                wait_for_gateway=False,
            )

        kwargs = mock_gateway.register_session.call_args.kwargs
        assert kwargs["worktree_container_id"] == "pipe-1-slice-4-coder"

    def test_spawn_reuse_path_registration_binds_worktree_container_id(self, spawner, mock_gateway):
        """``spawn_agent_job``'s own registration (reuse path, no supplied
        token) also binds the reused worktree instead of ``None``."""
        spawner.spawn_agent_job(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repos=["owner/repo"],
            reuse_worktree_id="pipe-1-coder",
            repo_volumes={"owner/repo": "/home/hostuser/.egg-worktrees/pipe-1-coder/repo"},
        )
        kwargs = mock_gateway.register_session.call_args.kwargs
        assert kwargs["worktree_container_id"] == "pipe-1-coder"


class TestSpawnEventJobDirtyWorktree:
    """Dirty-state policy (architect R6, #3506) for re-attached worktrees.

    On every successful re-attach the spawner discards uncommitted changes and
    untracked artifacts (reset --hard + clean -fd) and syncs to the role
    branch tip. The sync is fast-forward-aware (#3506): a clean-tree local
    HEAD that is a strict descendant of the origin tip is the agent's own
    durable multi-session work and is KEPT. A predecessor's residue (a dirty
    tree, or commits accompanied by dirt: the killed-mid-event signature)
    must never reach a successor, nor may a diverged HEAD. If discard, fetch,
    or hard-sync fails, the spawner falls back to recreate.
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

    def test_reattach_fetch_failure_falls_back(self, spawner, tmp_path):
        """Fetch failure is FATAL to reuse (#3064 review): recreate instead.

        The worktree is clean and on the right branch, but has no reachable
        ``origin`` remote, so ``git fetch origin <branch>`` fails. Because the
        fetch is what supplies the ``origin/<branch>`` tip the sync decision
        (keep vs. reset) relies on, a failure must fall back to recreate rather
        than continue on the current HEAD (which could still carry residue
        ahead of origin).
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

    def test_reattach_keeps_clean_fast_forward_commits(self, spawner, tmp_path):
        """A clean-tree local HEAD strictly ahead of the origin tip is KEPT (#3506).

        This is the multi-session accumulation case: the previous session
        committed durable work, exited cleanly, and had not pushed yet. The
        sync must not orphan that commit (the Sisyphus loop of #3506).
        """
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        origin_head = _git(repo, "rev-parse", f"origin/{_BRANCH}").stdout.strip()
        (repo / "work.txt").write_text("durable multi-session work\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "unpushed baseline (clean session exit)")
        local_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert local_head != origin_head

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS)

        assert cleaned is True
        # The agent's own fast-forward commit survives the re-attach.
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == local_head
        assert (repo / "work.txt").read_text() == "durable multi-session work\n"
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    def test_reattach_dirty_tree_disqualifies_fast_forward_keep(self, spawner, tmp_path):
        """A fast-forward commit accompanied by tracked dirt is still discarded.

        Tracked modifications alongside a local commit are the
        killed-mid-event signature, so the R6 hard-reset applies even though
        the commit alone would qualify as a clean fast-forward.
        """
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        origin_head = _git(repo, "rev-parse", f"origin/{_BRANCH}").stdout.strip()
        (repo / "work.txt").write_text("committed mid-session\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "commit left by a killed pod")
        (repo / "seed.txt").write_text("DIRTY tracked edit in flight\n")

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
        assert not (repo / "work.txt").exists()
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    def test_reattach_diverged_head_resets_to_origin(self, spawner, tmp_path):
        """A clean-tree local HEAD that DIVERGED from origin hard-resets (R6)."""
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        # Advance origin past the common base ...
        (repo / "remote.txt").write_text("landed on origin\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "advance origin")
        _git(repo, "push", "origin", _BRANCH)
        origin_head = _git(repo, "rev-parse", f"origin/{_BRANCH}").stdout.strip()
        # ... then rewind and commit divergent local work (clean tree).
        _git(repo, "reset", "--hard", "HEAD~1")
        (repo / "local.txt").write_text("divergent local work\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "divergent local commit")
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() != origin_head

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
        assert not (repo / "local.txt").exists()
        assert (repo / "remote.txt").read_text() == "landed on origin\n"

    def test_reattach_behind_tip_fast_forwards_to_origin(self, spawner, tmp_path):
        """A clean-tree local HEAD BEHIND the origin tip resets forward to it."""
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        (repo / "remote.txt").write_text("landed on origin\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "advance origin")
        _git(repo, "push", "origin", _BRANCH)
        origin_head = _git(repo, "rev-parse", f"origin/{_BRANCH}").stdout.strip()
        _git(repo, "reset", "--hard", "HEAD~1")  # fall behind the tip

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
        assert (repo / "remote.txt").read_text() == "landed on origin\n"


class _FakePushResult:
    """Minimal PushResult stand-in (the fixture stubs out gateway_client)."""

    def __init__(self, ok=True, detail="denied"):
        self.ok = ok
        self._detail = detail

    def describe(self):
        return self._detail


_PIPE_CTX = {"pipeline_id": "pipe-1", "agent_role": "coder", "slice_id": "slice-4"}


class TestDirtyDiscardAutoSalvage:
    """Auto-salvage + durable record on the R6 discard path (#3509).

    When the re-attach hard-reset is about to discard unpushed local
    commits, the doomed tip must be pushed to an ``egg/recovered/...``
    ref BEFORE the reset (afterwards it exists only in the object store)
    and recorded on the message bus where a memory-less resuming agent
    will find it. Both steps are best-effort: their failure must never
    block the re-attach.
    """

    def _seed_orphan(self, tmp_path, *, dirty=True):
        """Worktree with an unpushed commit (plus tracked dirt when *dirty*)."""
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        origin_head = _git(repo, "rev-parse", f"origin/{_BRANCH}").stdout.strip()
        (repo / "work.txt").write_text("orphaned work\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "unpushed stack tip")
        orphan_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        if dirty:
            (repo / "seed.txt").write_text("DIRTY tracked edit\n")
        return repo, origin_head, orphan_head

    def test_discard_salvages_tip_and_records_message(self, spawner, mock_gateway, tmp_path):
        repo, origin_head, orphan_head = self._seed_orphan(tmp_path)
        head_at_push = {}

        def _push(**kwargs):
            # The push must land while the doomed tip is still HEAD.
            head_at_push["sha"] = _git(repo, "rev-parse", "HEAD").stdout.strip()
            return _FakePushResult(ok=True)

        mock_gateway.push_worktree_branch.side_effect = _push

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head

        # #3639: the dirt is committed before the reset, so the doomed tip is
        # the WIP snapshot sitting on top of the predecessor's own commit.
        wip = head_at_push["sha"]
        assert wip != orphan_head
        assert _git(repo, "rev-parse", f"{wip}^").stdout.strip() == orphan_head

        expected_ref = f"egg/recovered/pipe-1/slice-4-coder/{wip[:12]}"
        mock_gateway.push_worktree_branch.assert_called_once()
        kwargs = mock_gateway.push_worktree_branch.call_args.kwargs
        assert kwargs["pipeline_id"] == "pipe-1"
        assert kwargs["repo_path"] == str(repo)
        assert kwargs["branch"] == expected_ref
        assert kwargs["ref"] is None

        msg = get_store.return_value.add_message.call_args.args[0]
        assert msg.pipeline_id == "pipe-1"
        assert msg.from_role == "orchestrator"
        assert msg.to_role == "coder"
        assert msg.metadata["discarded_tip"] == wip
        assert msg.metadata["remote_tip"] == origin_head
        assert msg.metadata["recovery_ref"] == expected_ref
        # The predecessor's own commit AND the WIP snapshot above it.
        assert msg.metadata["discarded_commit_count"] == 2
        assert msg.metadata["wip_commit"] == wip
        assert expected_ref in msg.body
        assert wip in msg.body
        # A real commit was lost alongside the snapshot, so the message keeps
        # the imperative ask and names the snapshot within the count.
        assert "one of which is an automatic snapshot" in msg.body
        assert "inspect it before starting work" in msg.body
        assert "nothing was lost" in msg.body

    def test_salvage_failure_still_resets_and_records_tip(self, spawner, mock_gateway, tmp_path):
        repo, origin_head, orphan_head = self._seed_orphan(tmp_path)
        mock_gateway.push_worktree_branch.side_effect = RuntimeError("gateway down")

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
        # The tip is still recorded durably even though the push failed.
        msg = get_store.return_value.add_message.call_args.args[0]
        discarded = msg.metadata["discarded_tip"]
        assert _git(repo, "rev-parse", f"{discarded}^").stdout.strip() == orphan_head
        assert msg.metadata["recovery_ref"] is None
        assert "gateway down" in msg.metadata["salvage_error"]
        # #3639: the body must NOT reassure a memory-less agent that nothing
        # was lost on the one path where the snapshot was never pushed — that
        # would suppress the escalation the preceding sentence just asked for.
        assert "Nothing was lost" not in msg.body
        assert "nothing was lost" not in msg.body
        assert "was NOT" in msg.body and "local object store" in msg.body
        assert "Escalate" in msg.body
        # And it must not point at a tool that provably cannot see the sha.
        assert "salvage_agent_commits cannot recover them" in msg.body

    def test_record_failure_does_not_block_reuse(self, spawner, mock_gateway, tmp_path):
        repo, origin_head, _ = self._seed_orphan(tmp_path)
        mock_gateway.push_worktree_branch.return_value = _FakePushResult(ok=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store", side_effect=RuntimeError("redis down")),
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head

    def test_legacy_call_without_context_is_log_only(self, spawner, mock_gateway, tmp_path):
        repo, origin_head, _orphan_head = self._seed_orphan(tmp_path)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
        mock_gateway.push_worktree_branch.assert_not_called()
        get_store.return_value.add_message.assert_not_called()

    def test_kept_fast_forward_does_not_salvage(self, spawner, mock_gateway, tmp_path):
        # Clean-tree strict descendant is KEPT (#3506): nothing is
        # discarded, so nothing may be salvaged or recorded.
        repo, _, orphan_head = self._seed_orphan(tmp_path, dirty=False)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == orphan_head
        mock_gateway.push_worktree_branch.assert_not_called()
        get_store.return_value.add_message.assert_not_called()

    def test_discard_salvage_forwards_private_mode(self, spawner, mock_gateway, tmp_path):
        # The salvage push MUST carry the pipeline's real network mode: a
        # "public" push on a private-mode pipeline over a private repo is
        # denied by the gateway's private-repo policy, silently degrading
        # auto-salvage to record-only (#3509).
        repo, _origin_head, _orphan_head = self._seed_orphan(tmp_path)
        mock_gateway.push_worktree_branch.return_value = _FakePushResult(ok=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store"),
        ):
            cleaned = spawner._clean_reused_worktree(
                _WT_ID, _BRANCH, _REPOS, mode="private", **_PIPE_CTX
            )

        assert cleaned is True
        assert mock_gateway.push_worktree_branch.call_args.kwargs["mode"] == "private"

    def test_discard_salvage_mode_defaults_to_public(self, spawner, mock_gateway, tmp_path):
        # Legacy callers that omit ``mode`` keep the historical public
        # default (the parameter must not silently become required).
        repo, _origin_head, _orphan_head = self._seed_orphan(tmp_path)
        mock_gateway.push_worktree_branch.return_value = _FakePushResult(ok=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store"),
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        assert mock_gateway.push_worktree_branch.call_args.kwargs["mode"] == "public"

    def test_spawn_event_job_threads_pipeline_context(
        self, spawner, mock_k8s_client, mock_gateway, tmp_path
    ):
        """End-to-end: a re-attach spawn with orphaned commits salvages them
        under the pipeline/slice/role scope taken from the spawn call."""
        repo, _ = _make_worktree(
            tmp_path, "pipe-1-slice-4-coder", "repo", _BRANCH, with_origin=True
        )
        (repo / "work.txt").write_text("orphaned work\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "unpushed stack tip")
        orphan_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "seed.txt").write_text("DIRTY tracked edit\n")

        mock_k8s_client.list_jobs.return_value = []  # no live Job → no adoption
        mock_gateway.heartbeat_session_by_container.return_value = False
        mock_gateway.register_session.return_value = _FakeSessionInfo(
            session_token="tok-reattach",
            container_id="egg-agent-pipe-1-slice-4-coder",
        )
        mock_gateway.push_worktree_branch.return_value = _FakePushResult(ok=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store"),
        ):
            spawner.spawn_event_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                action="propose",
                dedupe_key="c" * 64,
                slice_id="slice-4",
                phase="implement",
                repos=["owner/repo"],
                branch=_BRANCH,
                wait_for_gateway=False,
            )

        # The salvaged tip is the #3639 WIP snapshot of the dirty tree, which
        # sits directly on the predecessor's own unpushed commit.
        kwargs = mock_gateway.push_worktree_branch.call_args.kwargs
        prefix = "egg/recovered/pipe-1/slice-4-coder/"
        assert kwargs["branch"].startswith(prefix)
        salvaged = kwargs["branch"][len(prefix) :]
        assert _git(repo, "rev-parse", f"{salvaged}^").stdout.strip() == orphan_head

    def test_spawn_event_job_threads_private_mode(
        self, spawner, mock_k8s_client, mock_gateway, tmp_path
    ):
        """End-to-end: a private-mode re-attach spawn forwards ``mode`` all the
        way to the salvage push, so private-repo pipelines are not silently
        degraded to record-only (#3509)."""
        repo, _ = _make_worktree(
            tmp_path, "pipe-1-slice-4-coder", "repo", _BRANCH, with_origin=True
        )
        (repo / "work.txt").write_text("orphaned work\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "unpushed stack tip")
        (repo / "seed.txt").write_text("DIRTY tracked edit\n")

        mock_k8s_client.list_jobs.return_value = []  # no live Job → no adoption
        mock_gateway.heartbeat_session_by_container.return_value = False
        mock_gateway.register_session.return_value = _FakeSessionInfo(
            session_token="tok-reattach",
            container_id="egg-agent-pipe-1-slice-4-coder",
        )
        mock_gateway.push_worktree_branch.return_value = _FakePushResult(ok=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store"),
        ):
            spawner.spawn_event_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                action="propose",
                dedupe_key="c" * 64,
                slice_id="slice-4",
                phase="implement",
                repos=["owner/repo"],
                branch=_BRANCH,
                mode="private",
                wait_for_gateway=False,
            )

        assert mock_gateway.push_worktree_branch.call_args.kwargs["mode"] == "private"


class TestDirtyTreePreservedBeforeReset:
    """Uncommitted work survives the re-attach reset (#3639).

    The #3506/#3509 machinery preserves *commits*: a killed-mid-event
    worktree whose session never committed had its entire working set
    erased by ``reset --hard`` + ``clean -fd``, with the orphan detector
    finding nothing to salvage (the #3639 incident: 110 minutes across 33
    modified files). The dirty tree is now committed BEFORE the reset, so
    it becomes an ordinary orphan the existing salvage path recovers,
    without relaxing the R6 rule that the successor starts at the origin
    tip.
    """

    _MEMORY_FILE = ".egg-state/agent-outputs/coder/brc-memory-pipe-1.md"

    def _seed_dirty(self, tmp_path, *, files=2, machine_state_only=False):
        """Worktree with dirt only; no commits ahead of the origin tip.

        ``commit.gpgsign`` is turned on in the repo's own config so the
        production closure's ``-c commit.gpgsign=false`` is load-bearing:
        without it every snapshot below fails to commit (there is no signing
        key in the orchestrator image), which is exactly the regression that
        would silently un-fix #3639.

        ``machine_state_only`` seeds the one shape the message is allowed to
        soften for: an untracked ``brc-memory-<pipeline-id>.md`` and nothing
        else.
        """
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        _git(repo, "config", "commit.gpgsign", "true")
        origin_head = _git(repo, "rev-parse", f"origin/{_BRANCH}").stdout.strip()
        if machine_state_only:
            memory = repo / self._MEMORY_FILE
            memory.parent.mkdir(parents=True)
            memory.write_text("# BRC memory\n\nRound 1: ACKed coder.\n")
            return repo, origin_head
        (repo / "seed.txt").write_text("hours of uncommitted edits\n")  # tracked
        if files > 1:
            (repo / "new_module.py").write_text("def added():\n    return 1\n")  # untracked
        return repo, origin_head

    def test_uncommitted_work_is_salvaged_not_destroyed(self, spawner, mock_gateway, tmp_path):
        """The #3639 regression: dirt with zero commits is preserved."""
        repo, origin_head = self._seed_dirty(tmp_path)
        mock_gateway.push_worktree_branch.return_value = _FakePushResult(ok=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        # R6 unchanged: the successor still starts at the origin tip with a
        # clean tree; none of the residue is visible to it.
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""
        assert (repo / "seed.txt").read_text() == "seed\n"
        assert not (repo / "new_module.py").exists()

        # ... but the work now exists as a pushed snapshot commit.
        mock_gateway.push_worktree_branch.assert_called_once()
        msg = get_store.return_value.add_message.call_args.args[0]
        wip = msg.metadata["wip_commit"]
        assert wip == msg.metadata["discarded_tip"]
        assert msg.metadata["discarded_commit_count"] == 1
        assert _git(repo, "rev-parse", f"{wip}^").stdout.strip() == origin_head
        # Both the tracked edit and the untracked file are in the snapshot.
        assert _git(repo, "show", f"{wip}:seed.txt").stdout == "hours of uncommitted edits\n"
        assert "def added():" in _git(repo, "show", f"{wip}:new_module.py").stdout
        # The resuming agent is told the top commit is a machine snapshot.
        assert wip in msg.body
        assert "AUTOMATIC snapshot" in msg.body
        # This is the #3639 shape (multi-file working set, zero commits), so
        # it keeps the imperative ask AND the actionable instruction: being
        # snapshot-only must not by itself soften the message.
        assert "2 file(s) of uncommitted work" in msg.body
        assert "inspect it before starting work" in msg.body
        assert "build on it (cherry-pick or reset)" in msg.body

    def test_machine_state_only_snapshot_softens_the_ask(self, spawner, mock_gateway, tmp_path):
        """A pure state-file snapshot reads as "read it if you need it".

        ``brc-memory-<pipeline-id>.md`` is rewritten into the worktree on
        every ``brc_ack``/``brc_nack``, so a respawn that trips this path over
        that file alone is routine. Keeping the imperative there is how
        #3509's message gets trained into background noise — but the
        softening keys off *which* files were captured, never off a heuristic
        applied to whether the snapshot is taken at all.
        """
        repo, _origin_head = self._seed_dirty(tmp_path, machine_state_only=True)
        mock_gateway.push_worktree_branch.return_value = _FakePushResult(ok=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            assert spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX) is True

        msg = get_store.return_value.add_message.call_args.args[0]
        assert msg.metadata["discarded_commit_count"] == 1
        assert msg.metadata["wip_files"] == 1
        # The file is named outright: a memory-less agent cannot evaluate "is
        # anything missing?", so the message must not ask it to.
        assert f"only `{self._MEMORY_FILE}`" in msg.body
        assert "read it if you need it" in msg.body
        # The softening must survive the whole body, not just its first half.
        assert "inspect it before starting work" not in msg.body
        assert "Treat it as a WIP checkpoint" not in msg.body
        # The record is still emitted and the ref still pushed — wording only.
        mock_gateway.push_worktree_branch.assert_called_once()
        assert repo.exists()

    def test_one_substantial_file_keeps_the_imperative(self, spawner, mock_gateway, tmp_path):
        """A single rewritten source file is #3639 one file wide, not noise."""
        repo, _origin_head = self._seed_dirty(tmp_path, files=1)
        mock_gateway.push_worktree_branch.return_value = _FakePushResult(ok=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            assert spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX) is True

        msg = get_store.return_value.add_message.call_args.args[0]
        assert msg.metadata["wip_files"] == 1
        assert "1 file(s) of uncommitted work" in msg.body
        assert "inspect it before starting work" in msg.body
        assert repo.exists()

    def test_snapshot_only_salvage_failure_escalates(self, spawner, mock_gateway, tmp_path):
        """#3639 during a gateway outage: the worst cell of the 2x2.

        ``recovery_ref is None`` *and* snapshot-only — uncommitted work with
        no commits behind it, and the push that would have preserved it
        failed. The message must escalate rather than point at a ref that
        does not exist.
        """
        repo, origin_head = self._seed_dirty(tmp_path)
        mock_gateway.push_worktree_branch.side_effect = RuntimeError("gateway down")

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head

        msg = get_store.return_value.add_message.call_args.args[0]
        assert msg.metadata["recovery_ref"] is None
        assert msg.metadata["wip_commit"] == msg.metadata["discarded_tip"]
        assert msg.metadata["discarded_commit_count"] == 1
        # No false reassurance, and no pointer at a ref that was never pushed.
        assert "nothing was lost" not in msg.body.lower()
        assert "egg/recovered/" not in msg.body
        assert "was NOT" in msg.body and "local object store" in msg.body
        assert "Escalate" in msg.body
        # The size is named here too, so the operator being escalated to
        # knows what is at stake before touching the reflog.
        assert "2 file(s) of uncommitted work" in msg.body

    def test_snapshot_commit_identity_matches_the_restart_path(
        self, spawner, mock_gateway, tmp_path
    ):
        """The snapshot is greppable by ``[salvage]`` + the #2807 identity.

        ``docs/reference/agent-recovery.md`` promises one ``[salvage]`` grep
        finds every machine-made working-tree snapshot regardless of which
        path took it. Pin both halves: the constants agree with
        ``agent_salvage``'s, and the commit git actually produces carries
        them.
        """
        import agent_salvage
        from kubernetes_spawner import _worktree

        assert _worktree._WIP_COMMIT_AUTHOR_NAME == agent_salvage._SALVAGE_COMMIT_NAME
        assert _worktree._WIP_COMMIT_AUTHOR_EMAIL == agent_salvage._SALVAGE_COMMIT_EMAIL
        assert _worktree._WIP_COMMIT_MESSAGE.startswith("[salvage]")

        repo, _ = self._seed_dirty(tmp_path)
        mock_gateway.push_worktree_branch.return_value = _FakePushResult(ok=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            assert spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX) is True

        wip = get_store.return_value.add_message.call_args.args[0].metadata["wip_commit"]
        ident = _git(repo, "show", "-s", "--format=%an|%ae|%s", wip).stdout.strip()
        author, email, subject = ident.split("|")
        assert author == agent_salvage._SALVAGE_COMMIT_NAME
        assert email == agent_salvage._SALVAGE_COMMIT_EMAIL
        assert subject.startswith("[salvage]")

    def test_partial_suffixes_share_one_grep_token(self):
        """One search must find a truncated snapshot from either path (R7 N3).

        The two ``INCOMPLETE:`` suffixes are deliberate near-duplicates —
        they differ only in naming whose working tree was truncated — and
        both comment blocks instruct "change one, change the other". A
        comment does not fail when someone edits one of them, and
        ``docs/reference/agent-recovery.md`` tells triagers to grep for this
        exact token, so pin the shared prefix instead.

        The runbook is pinned too (R8 B2). Coupling the two constants to
        each other but not to the doc is what let the doc's copy of the
        token drift into a pattern that matches nothing — a false negative
        that reads exactly like "no truncated snapshots", which is the one
        conclusion that paragraph exists to prevent.
        """
        import agent_salvage
        from kubernetes_spawner import _worktree

        shared = "\n\nINCOMPLETE: `git add -A` did not complete cleanly while staging, so\nfiles "
        assert _worktree._WIP_COMMIT_PARTIAL_SUFFIX.startswith(shared)
        assert agent_salvage._UNCOMMITTED_SALVAGE_PARTIAL_SUFFIX.startswith(shared)
        # Near-duplicate, not duplicate: the provenance clause is the one
        # thing a triager reading a lone commit message cannot infer.
        assert (
            _worktree._WIP_COMMIT_PARTIAL_SUFFIX
            != agent_salvage._UNCOMMITTED_SALVAGE_PARTIAL_SUFFIX
        )

        # The runbook must quote the token verbatim — backticks included.
        # ``git log --grep`` matches the commit message, so a copy that lost
        # them in transit (a single-backtick code span cannot contain a
        # backtick; backslash escapes do not work inside code spans) renders
        # a command that silently finds nothing.
        runbook = (
            Path(__file__).resolve().parents[2] / "docs" / "reference" / "agent-recovery.md"
        ).read_text()
        quoted = shared.strip().split(", so")[0]
        assert quoted in runbook, f"agent-recovery.md no longer quotes: {quoted!r}"
        assert "git log --all --grep 'INCOMPLETE: `git add -A`'" in runbook
        # ``--all`` does include ``refs/remotes/``, which is precisely why the
        # fetch matters: both snapshot paths push to origin, so a fresh clone
        # has no ref under ``refs/remotes/origin/egg/recovered/`` until the
        # namespace is fetched and ``--all`` walks nothing. The runbook must
        # name the fetch or the grep is a false negative there (R9 NB-4).
        assert "refs/heads/egg/recovered/*:refs/remotes/origin/egg/recovered/*" in runbook

    def test_no_branch_takes_no_snapshot(self, spawner, mock_gateway, tmp_path):
        """``branch is None`` ⇒ no snapshot commit, and HEAD does not move.

        With no branch there is no origin tip to reset to and no salvage
        target, so a snapshot would simply become the successor's HEAD:
        un-vetted residue promoted to committed state, which is what R6
        exists to prevent. The dirt is discarded as it was pre-#3639.
        """
        repo, _origin_head = self._seed_dirty(tmp_path)
        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, None, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        # No snapshot was committed: HEAD is unmoved and the tree is clean.
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""
        assert not (repo / "new_module.py").exists()
        mock_gateway.push_worktree_branch.assert_not_called()
        get_store.return_value.add_message.assert_not_called()

    def test_snapshot_is_pushed_to_a_recovery_ref(self, spawner, mock_gateway, tmp_path):
        """The snapshot rides the existing #3509 recovery-ref namespace."""
        repo, _origin_head = self._seed_dirty(tmp_path)
        pushed_head = {}

        def _push(**kwargs):
            pushed_head["sha"] = _git(repo, "rev-parse", "HEAD").stdout.strip()
            return _FakePushResult(ok=True)

        mock_gateway.push_worktree_branch.side_effect = _push

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store"),
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        kwargs = mock_gateway.push_worktree_branch.call_args.kwargs
        # Pushed while the snapshot was still HEAD, before the reset.
        assert kwargs["branch"] == (f"egg/recovered/pipe-1/slice-4-coder/{pushed_head['sha'][:12]}")
        assert kwargs["ref"] is None

    def test_no_committable_change_takes_no_snapshot(self, tmp_path):
        """The ``not staged`` branch: an empty index ⇒ no commit at all.

        Reached in production by dirt that ``git add -A`` cannot stage — a
        dirty submodule whose gitlink is unchanged shows as ` M sub` in
        ``status --porcelain`` yet stages nothing. Driven here with a git
        closure whose ``diff --cached`` is empty, because the end-to-end
        ignored-files case never reaches this helper at all (ignored files do
        not appear in ``status --porcelain``, so ``was_dirty`` is False).
        """
        from kubernetes_spawner._worktree import _preserve_dirty_tree

        calls = []

        def _fake_git(_repo_dir, *args, **_kwargs):
            calls.append(args)
            return SimpleNamespace(stdout="", returncode=0)

        assert (
            _preserve_dirty_tree(
                _fake_git, tmp_path, agent_worktree_id=_WT_ID, repo="repo", n_entries=1
            )
            is None
        )
        assert not any("commit" in a for a in calls)

    def test_partial_add_failure_still_commits_what_was_staged(self, tmp_path):
        """A non-zero ``git add`` must not discard the files that did stage.

        ``git-add(1)`` aborts on the first unindexable entry and exits
        non-zero with a partially populated index; returning early there
        would throw away the other N-1 files this helper exists to save.

        The genuinely partial index (N-1 of N files present) is asserted only
        at this unit level, by a git closure that reports a smaller staged set
        than the entry count: producing a real unindexable entry needs a fifo
        or an unreadable file, which is too environment-dependent for CI. What
        the real-git counterpart pins is the surrounding behaviour, not the
        partial staging itself.
        """
        from kubernetes_spawner._worktree import _preserve_dirty_tree

        seen = []

        def _flaky_git(_repo_dir, *args, **_kwargs):
            seen.append(args)
            if args[0] == "add":
                assert "--ignore-errors" in args
                raise RuntimeError("error: unable to index file 'broken.sock'")
            if args[0] == "diff":
                assert "-z" in args
                return SimpleNamespace(stdout="a.py\0b.py\0", returncode=0)
            return SimpleNamespace(stdout="deadbeefcafe\n", returncode=0)

        snapshot = _preserve_dirty_tree(
            _flaky_git, tmp_path, agent_worktree_id=_WT_ID, repo="repo", n_entries=3
        )
        assert snapshot is not None
        assert snapshot.sha == "deadbeefcafe"
        # The file set is what the bus message is worded off, so it must
        # reflect what actually reached the index, not the pre-add entry count.
        assert snapshot.n_files == 2
        assert snapshot.paths == ("a.py", "b.py")
        # ``partial`` is derived from the ``add`` raising, NOT from comparing
        # ``len(paths)`` against ``n_entries`` — a count cross-check would be
        # unsound anyway, since porcelain collapses an untracked directory to
        # one entry, so ``len(paths) > n_entries`` is normal. What it pins:
        # the failed add is carried forward, because an agent that
        # cherry-picks a silently-truncated snapshot believes it recovered
        # everything.
        assert snapshot.partial is True
        commit_args = next(a for a in seen if a[0] == "commit" or "commit" in a)
        assert any("INCOMPLETE" in a for a in commit_args if isinstance(a, str))
        assert any("commit" in a for a in seen)

    def test_complete_snapshot_is_not_marked_partial(self, tmp_path):
        """The clean path must not carry the truncation warning."""
        from kubernetes_spawner._worktree import _preserve_dirty_tree

        seen = []

        def _fake_git(_repo_dir, *args, **_kwargs):
            seen.append(args)
            if args[0] == "diff":
                return SimpleNamespace(stdout="a.py\0b.py\0", returncode=0)
            return SimpleNamespace(stdout="deadbeefcafe\n", returncode=0)

        snapshot = _preserve_dirty_tree(
            _fake_git, tmp_path, agent_worktree_id=_WT_ID, repo="repo", n_entries=2
        )
        assert snapshot is not None
        assert snapshot.partial is False
        commit_args = next(a for a in seen if "commit" in a)
        assert not any("INCOMPLETE" in a for a in commit_args if isinstance(a, str))

    def test_unusual_path_bytes_survive_the_staged_file_parse(self, tmp_path):
        """``wip_paths`` must be real paths, not C-quoted tokens (R6 #1).

        ``git diff --cached --name-only`` without ``-z`` honours
        ``core.quotePath`` (default true), so a non-ASCII name comes back
        double-quoted and backslash-escaped, and a name containing a newline
        splits across two ``splitlines()`` entries. Both corruptions flow
        into the ``wip_paths`` bus metadata, which exists so a consumer can
        match paths structurally instead of regexing the body. ``-z`` emits
        the bytes unmunged with NUL terminators, so the field means what its
        name says.
        """
        from kubernetes_spawner._worktree import _preserve_dirty_tree

        seen = []
        raw = (
            ".egg-state/agent-outputs/coder/brc-memory-café.md",
            "src/we\nird.py",
        )

        def _fake_git(_repo_dir, *args, **_kwargs):
            seen.append(args)
            if args[0] == "diff":
                return SimpleNamespace(stdout="\0".join(raw) + "\0", returncode=0)
            return SimpleNamespace(stdout="deadbeefcafe\n", returncode=0)

        snapshot = _preserve_dirty_tree(
            _fake_git, tmp_path, agent_worktree_id=_WT_ID, repo="repo", n_entries=2
        )
        assert snapshot is not None
        assert snapshot.paths == raw
        assert snapshot.n_files == 2
        # The flag has to be on the request, not just implied by the parse:
        # dropping it would silently reintroduce the quoting.
        diff_args = next(a for a in seen if a[0] == "diff")
        assert "-z" in diff_args

    def test_undecodable_staged_path_does_not_cost_the_commit(self, tmp_path):
        """An undecodable filename degrades the metadata, never the snapshot.

        The unit half of R7 B1. ``-z`` is what makes ``wip_paths`` mean real
        paths, but it also hands raw bytes to the caller's
        ``subprocess.run(..., text=True)``, which decodes as strict UTF-8 —
        so a filename that is not valid UTF-8 raises ``UnicodeDecodeError``
        *inside* ``run``, before the ``split("\\0")``. Letting that reach the
        outer handler would abandon the commit and hand the whole working
        tree to the reset: #3639 reintroduced, over a filename.

        The closure below raises exactly what ``subprocess.run`` raises, so
        the assertion is about the layer the ``-z`` change moved the failure
        into, not about a pre-decoded fixture.
        """
        from kubernetes_spawner._worktree import _preserve_dirty_tree

        seen = []

        def _undecodable_git(_repo_dir, *args, **_kwargs):
            seen.append(args)
            if args[0] == "diff":
                raise UnicodeDecodeError(
                    "utf-8", b"src/caf\xe9.md", 7, 8, "invalid continuation byte"
                )
            return SimpleNamespace(stdout="deadbeefcafe\n", returncode=0)

        snapshot = _preserve_dirty_tree(
            _undecodable_git, tmp_path, agent_worktree_id=_WT_ID, repo="repo", n_entries=33
        )

        # The commit is the point: it must exist.
        assert snapshot is not None
        assert snapshot.sha == "deadbeefcafe"
        assert any("commit" in a for a in seen)
        # Only the path metadata degrades — and it degrades to "unknown",
        # which ``_record_discarded_tip`` reads as "take the imperative".
        assert snapshot.paths is None
        assert snapshot.n_files is None
        # An unreadable path list is not a truncated capture: ``partial``
        # means ``git add -A`` did not complete cleanly, and it did here.
        assert snapshot.partial is False

    @pytest.mark.parametrize(
        "read_error",
        [
            pytest.param(
                subprocess.TimeoutExpired(cmd=["git", "diff", "--cached"], timeout=60),
                id="timeout",
            ),
            pytest.param(
                subprocess.CalledProcessError(
                    returncode=128, cmd=["git", "diff", "--cached"], stderr="index.lock exists"
                ),
                id="nonzero-exit",
            ),
            pytest.param(RuntimeError("closure blew up"), id="unexpected"),
        ],
    )
    def test_any_failed_staged_path_read_keeps_the_commit(self, tmp_path, read_error):
        """*Any* failure of the metadata read degrades metadata, not the tree.

        R8 B1. The R7 fix caught ``UnicodeDecodeError`` specifically, which
        left two production triggers still costing the whole working tree:
        the read is ``git diff --cached --name-only -z`` with ``timeout=60``
        run immediately after ``git add -A`` staged the entire dirty tree
        (33 files was the small case), and it inherits ``check=True`` while
        the preceding add's own failure is swallowed into ``partial`` — so a
        contended node or a still-held ``index.lock`` reaches the outer
        handler, which abandons the commit and hands the tree to
        ``reset --hard``. That is #3639 with the trigger moved from "one bad
        filename" to "the metadata read was slow".

        Parametrised over the classes rather than pinned to one: the
        invariant is about the *category* of failure (this read is metadata,
        the commit is the point), and a test named for the invariant that
        covers a single exception class is what made the gap invisible.
        """
        from kubernetes_spawner._worktree import _preserve_dirty_tree

        seen = []

        def _failing_read_git(_repo_dir, *args, **_kwargs):
            seen.append(args)
            if args[0] == "diff":
                raise read_error
            return SimpleNamespace(stdout="deadbeefcafe\n", returncode=0)

        snapshot = _preserve_dirty_tree(
            _failing_read_git, tmp_path, agent_worktree_id=_WT_ID, repo="repo", n_entries=33
        )

        assert snapshot is not None
        assert snapshot.sha == "deadbeefcafe"
        assert any("commit" in a for a in seen)
        # "Could not tell", not "nothing was there" — the record then takes
        # the imperative rather than reassuring the agent.
        assert snapshot.paths is None
        assert snapshot.n_files is None
        assert snapshot.partial is False

    def test_undecodable_bytes_cost_one_name_not_the_path_set(self, tmp_path):
        """A latin-1 filename degrades its own entry and no others (R8 NB-2).

        The read passes ``errors="replace"``, so the strict-UTF-8 decode
        that used to raise inside ``subprocess.run`` now yields U+FFFD for
        the bad bytes. Discarding all 33 paths for one bad byte was
        avoidable; ``routes/pipelines/_worktree_sync`` already reads its own
        ``-z`` output this way. The replacement does not move the softening
        decision either way (R9 NB-2): every non-``*`` character in
        ``_MACHINE_STATE_FILE_GLOBS`` is ASCII and replacement only ever
        substitutes non-ASCII for non-ASCII, so a replaced path matches
        exactly the globs its raw bytes would.
        """
        from kubernetes_spawner._worktree import _preserve_dirty_tree

        # What ``subprocess.run(..., text=True, errors="replace")`` hands
        # back for ``b"caf\\xe9.md\\0good.py\\0"``.
        replaced = b"caf\xe9.md\0good.py\0".decode("utf-8", errors="replace")

        def _replacing_git(_repo_dir, *args, **kwargs):
            if args[0] == "diff":
                assert kwargs.get("errors") == "replace", "the read must not decode strictly"
                return SimpleNamespace(stdout=replaced, returncode=0)
            return SimpleNamespace(stdout="deadbeefcafe\n", returncode=0)

        snapshot = _preserve_dirty_tree(
            _replacing_git, tmp_path, agent_worktree_id=_WT_ID, repo="repo", n_entries=2
        )

        assert snapshot is not None
        assert snapshot.n_files == 2
        assert snapshot.paths is not None
        # The good name survives intact — that is the whole point.
        assert "good.py" in snapshot.paths
        assert snapshot.paths[0].startswith("caf") and snapshot.paths[0].endswith(".md")
        assert "�" in snapshot.paths[0]

    def test_add_decode_is_non_strict(self, tmp_path):
        """The ``add`` arm must not decode strictly either (R9 NB-2).

        Sibling pin for the ``diff`` arm above, which had none: every fake
        ``git`` closure in this class takes ``**_kwargs`` and swallows the
        keyword, so the production call site could lose it and stay green.

        ``git add``'s stderr echoes the path **raw** in three messages
        ``core.quotePath`` does not cover, so a strict decode raises inside
        ``run`` on a non-UTF-8 name. ``git`` is a *parameter* of
        :func:`_preserve_dirty_tree`, so what pins the decode is the keyword
        at the call site, not the caller closure's default — hence an
        assertion on the argv rather than on the closure.
        """
        from kubernetes_spawner._worktree import _preserve_dirty_tree

        seen: dict[str, object] = {}

        def _recording_git(_repo_dir, *args, **kwargs):
            seen[args[0]] = kwargs.get("errors")
            if args[0] == "diff":
                return SimpleNamespace(stdout="seed.txt\0", returncode=0)
            return SimpleNamespace(stdout="deadbeefcafe\n", returncode=0)

        snapshot = _preserve_dirty_tree(
            _recording_git, tmp_path, agent_worktree_id=_WT_ID, repo="repo", n_entries=1
        )

        assert snapshot is not None
        assert snapshot.partial is False
        assert seen["add"] == "replace", "git add's stderr echoes raw paths"
        assert seen["diff"] == "replace"

    def test_embedded_repo_warning_does_not_forge_a_partial_snapshot(
        self, spawner, mock_gateway, tmp_path
    ):
        """A raw-path message on a *successful* add must not stamp INCOMPLETE (R9 NB-2).

        Real git, and deliberately not either of the two messages the
        docstrings above cite: ``unable to index file`` and ``does not have a
        commit checked out`` both accompany a **non-zero** exit, so a snapshot
        that trips them is honestly partial under a strict decode too. The
        message that manufactures a *false* ``INCOMPLETE:`` is the third
        (``check_embedded_repo`` in git's ``builtin/add.c``)::

            warning: adding embedded git repository: <raw path>

        It echoes the path unquoted and exits **0**. So a nested repo that
        *does* have a commit checked out, under a latin-1 directory name,
        gives a complete capture that a strict decode would nonetheless turn
        into an ``UnicodeDecodeError`` → ``partial=True`` → an
        ``INCOMPLETE:``-stamped commit and a soft branch disqualified, over a
        filename git only mentioned in passing.
        """
        repo, _origin_head = self._seed_dirty(tmp_path)
        nested = repo / os.fsdecode(b"nested-caf\xe9")
        nested.mkdir()
        _git(nested, "init", "-b", "main")
        (nested / "inner.txt").write_text("inner\n")
        _git(nested, "add", "-A")
        _git(nested, "commit", "-m", "inner")
        mock_gateway.push_worktree_branch.return_value = _FakePushResult(ok=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            assert spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX) is True

        msg = get_store.return_value.add_message.call_args.args[0]
        wip = msg.metadata["wip_commit"]
        assert wip is not None, "the tree was lost to a warning"
        # The work is in the snapshot, and the add really did succeed...
        assert _git(repo, "show", f"{wip}:seed.txt").stdout == "hours of uncommitted edits\n"
        assert "def added():" in _git(repo, "show", f"{wip}:new_module.py").stdout
        # ...so nothing may claim otherwise: not the metadata, not the commit
        # message, not the bus body.
        assert msg.metadata["wip_partial"] is False
        assert "INCOMPLETE" not in _git(repo, "log", "-1", "--format=%B", wip).stdout
        assert "may be INCOMPLETE" not in msg.body

    def test_undecodable_filename_is_salvaged_end_to_end(self, spawner, mock_gateway, tmp_path):
        """Real git, real non-UTF-8 filename: the 33 files still survive.

        The regression R7 B1 describes needs no exotic setup — one file whose
        name is latin-1 (an extracted archive, a fixture written with raw
        bytes) alongside hours of ordinary work. ``status --porcelain``
        honours ``core.quotePath`` so the entry point still sees ASCII and
        enters the dirty path; the ``-z`` read is where the bytes escape.
        This is the only real-git coverage of ``-z``: both other seeds are
        ASCII, so a fixture-level test cannot fail on this. Since R8 NB-2
        the read decodes with ``errors="replace"``, so the assertion moved
        from "the path list degrades to ``None``" to "the *other* names
        survive and only the bad one is replaced" — a strictly smaller
        degradation for the same commit.
        """
        repo, origin_head = self._seed_dirty(tmp_path)
        # ``os.fsdecode`` of invalid UTF-8 yields surrogate escapes, which the
        # filesystem round-trips back to the original bytes on Linux.
        (repo / os.fsdecode(b"caf\xe9.md")).write_bytes(b"latin-1 name\n")
        mock_gateway.push_worktree_branch.return_value = _FakePushResult(ok=True)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        msg = get_store.return_value.add_message.call_args.args[0]
        wip = msg.metadata["wip_commit"]
        # The snapshot exists and holds the work, not just the odd filename.
        assert wip is not None
        assert _git(repo, "show", f"{wip}:seed.txt").stdout == "hours of uncommitted edits\n"
        assert "def added():" in _git(repo, "show", f"{wip}:new_module.py").stdout
        # ... and it was pushed to a recovery ref like any other snapshot.
        mock_gateway.push_worktree_branch.assert_called_once()
        # Only the bad name degrades: the other two are reported as-is and
        # the count is complete, so the agent is told what it actually has.
        paths = msg.metadata["wip_paths"]
        assert msg.metadata["wip_files"] == 3
        assert set(paths) >= {"seed.txt", "new_module.py"}
        assert any("�" in p for p in paths), paths
        # The imperative here is earned by ``seed.txt``/``new_module.py``,
        # which match no softening glob — NOT by the replaced name, which
        # matches whatever its raw bytes would (R9 NB-2). This asserts the
        # end-to-end default is unchanged, not the replacement's effect;
        # ``test_replacement_does_not_move_the_softening_decision`` covers that.
        assert msg.metadata["wip_machine_state_only"] is False
        assert msg.metadata["wip_softened"] is False
        assert "inspect it before starting work" in msg.body
        assert "3 file(s) of uncommitted work" in msg.body
        # R6 still holds: the successor starts clean at the origin tip.
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    def test_discard_warning_carries_snapshot_completeness(self, spawner, mock_gateway, tmp_path):
        """The discard WARNING's ``wip_*`` fields are the query surface (R8 NB-4).

        "Which recovery refs are truncated, and which hold an unknown file
        set" has to be answerable from this one line — that is why the
        completeness fields ride with the sha instead of living on the
        earlier ``_preserve_dirty_tree`` line. Two things have to hold for
        that to work: the names must match the bus record's metadata keys
        (``wip_files``, not ``preserved_files``), and "unknown" must be
        distinguishable from "zero", since a bare ``None`` renders as
        ``wip_files=`` and reads as *nothing was preserved*.

        The snapshot is stubbed rather than provoked: the read-failure path
        it represents is covered by
        :meth:`test_any_failed_staged_path_read_keeps_the_commit`, and what
        is under test here is the log line, not how the metadata went
        missing.
        """
        from kubernetes_spawner import _worktree

        repo, _origin_head = self._seed_dirty(tmp_path)
        # The WARNING is gated on `if orphans:`, so the worktree needs a real
        # commit ahead of origin as well as the dirt.
        (repo / "ahead.txt").write_text("committed but unpushed\n")
        _git(repo, "add", "ahead.txt")
        _git(repo, "commit", "-m", "ahead of origin")
        mock_gateway.push_worktree_branch.return_value = _FakePushResult(ok=True)

        unknown = _worktree._DirtySnapshot(
            sha="beefbeefbeef", n_files=None, paths=None, partial=True
        )
        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store"),
            patch("kubernetes_spawner._worktree._preserve_dirty_tree", return_value=unknown),
            patch("kubernetes_spawner._worktree.logger") as log,
        ):
            assert spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX) is True

        discard = next(
            c for c in log.warning.call_args_list if "unpushed local commits" in c.args[0]
        )
        assert discard.kwargs["wip_commit"] == "beefbeefbeef"
        assert discard.kwargs["wip_partial"] is True
        assert discard.kwargs["wip_files"] is None
        # The whole point: unknown, not zero.
        assert discard.kwargs["wip_files_unknown"] is True
        # The off-pattern name must not come back — a consumer querying
        # `wip_files` on the bus and `preserved_files` in the log is the
        # failure this rename fixed.
        assert "preserved_files" not in discard.kwargs

    def test_ignored_only_dirt_is_discarded_without_a_commit(self, spawner, mock_gateway, tmp_path):
        """Build output is not agent work: no snapshot, nothing salvaged.

        Ignored files are absent from ``status --porcelain``, so this asserts
        the outer ``if was_dirty:`` guard never fires — the helper is not
        reached. The helper's own empty-index branch is pinned by
        :meth:`test_no_committable_change_takes_no_snapshot`.
        """
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        (repo / ".gitignore").write_text("build/\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "add gitignore")
        _git(repo, "push", "origin", _BRANCH)
        origin_head = _git(repo, "rev-parse", f"origin/{_BRANCH}").stdout.strip()
        (repo / "build").mkdir()
        (repo / "build" / "artifact.o").write_text("binary\n")

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
        mock_gateway.push_worktree_branch.assert_not_called()
        get_store.return_value.add_message.assert_not_called()

    def test_preservation_failure_still_resets(self, spawner, mock_gateway, tmp_path):
        """A failed snapshot must not block reuse; the reset still runs.

        ``_preserve_dirty_tree`` reports failure by returning ``None`` (it
        swallows its own git errors, asserted below); the discard must then
        proceed exactly as it did pre-#3639 rather than fall back to
        recreate, which would destroy the same state with less visibility.
        """
        repo, origin_head = self._seed_dirty(tmp_path)

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("kubernetes_spawner._worktree._preserve_dirty_tree", return_value=None),
            patch("message_store.get_message_store") as get_store,
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""
        mock_gateway.push_worktree_branch.assert_not_called()
        get_store.return_value.add_message.assert_not_called()

    def test_preserve_helper_swallows_git_failures(self, tmp_path):
        """``_preserve_dirty_tree`` never raises: callers must reach the reset."""
        from kubernetes_spawner._worktree import _preserve_dirty_tree

        def _boom(*_args, **_kwargs):
            raise RuntimeError("git index locked")

        assert (
            _preserve_dirty_tree(
                _boom, tmp_path, agent_worktree_id=_WT_ID, repo="repo", n_entries=3
            )
            is None
        )

    def test_clean_tree_is_not_snapshotted(self, spawner, mock_gateway, tmp_path):
        """No dirt ⇒ no snapshot commit (the common path is untouched)."""
        repo, _ = _make_worktree(tmp_path, _WT_ID, "repo", _BRANCH, with_origin=True)
        origin_head = _git(repo, "rev-parse", f"origin/{_BRANCH}").stdout.strip()

        with (
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
            patch("message_store.get_message_store") as get_store,
        ):
            cleaned = spawner._clean_reused_worktree(_WT_ID, _BRANCH, _REPOS, **_PIPE_CTX)

        assert cleaned is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
        mock_gateway.push_worktree_branch.assert_not_called()
        get_store.return_value.add_message.assert_not_called()


class TestDiscardedTipMessageWording:
    """The 2x2 the resuming agent actually reads (#3639 / #3509).

    ``_record_discarded_tip`` composes off two independent axes — did the
    salvage push succeed (``recovery_ref``), and is the discard nothing but
    the machine-made snapshot (``n_commits``/``wip_paths``). The end-to-end
    tests above drive real git through three of the cells; these pin all
    four plus the machine-state discriminator directly, so a wording
    regression is caught without a worktree.
    """

    _MEMORY_FILE = ".egg-state/agent-outputs/coder/brc-memory-pipe-1.md"

    _BASE = {
        "pipeline_id": "pipe-1",
        "agent_worktree_id": _WT_ID,
        "repo": "repo",
        "branch": _BRANCH,
        "agent_role": "coder",
        "slice_id": "slice-4",
        "discarded_tip": "aaaa1111",
        "remote_tip": "bbbb2222",
        "was_dirty": True,
    }

    def _message(self, **overrides):
        from kubernetes_spawner._worktree import _record_discarded_tip

        kwargs = {**self._BASE, "salvage_error": None, **overrides}
        with patch("message_store.get_message_store") as get_store:
            _record_discarded_tip(**kwargs)
        return get_store.return_value.add_message.call_args.args[0]

    def _body(self, **overrides):
        return self._message(**overrides).body

    def test_multi_file_snapshot_keeps_the_imperative(self):
        body = self._body(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=33,
            wip_paths=tuple(f"src/mod_{i}.py" for i in range(33)),
        )
        # The #3639 incident itself is snapshot-only; being snapshot-only must
        # not be what softens the ask.
        assert "33 file(s) of uncommitted work" in body
        assert "inspect it before starting work" in body
        assert "build on it (cherry-pick or reset)" in body

    def test_machine_state_only_snapshot_is_softened(self):
        """A capture that is nothing but regenerated state relaxes.

        The softening has to hold across the *whole* body: a trailing "treat
        it as a WIP checkpoint to review" would put an imperative in the last
        sentence the agent reads and undo the branch entirely.
        """
        body = self._body(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=1,
            wip_paths=(self._MEMORY_FILE,),
        )
        assert f"only `{self._MEMORY_FILE}`" in body
        assert "read it if you need it" in body
        assert "inspect it before starting work" not in body
        assert "Treat it as a WIP checkpoint" not in body
        # And the opening clause does not restate it a third time.
        assert "an automatic snapshot of uncommitted work" not in body

    def test_one_substantial_file_keeps_the_imperative(self):
        """A single rewritten module is #3639 one file wide, not noise.

        This is why the discriminator matches the noise source by name
        instead of counting files: on a count threshold this case scores
        identically to the memory file above and gets talked out of fetching
        real work.
        """
        body = self._body(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=1,
            wip_paths=("orchestrator/kubernetes_spawner/_worktree.py",),
        )
        assert "1 file(s) of uncommitted work" in body
        assert "inspect it before starting work" in body
        assert "Treat it as a WIP checkpoint" in body

    def test_mixed_snapshot_keeps_the_imperative(self):
        """One real file alongside the memory file is not a trivial capture."""
        body = self._body(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=2,
            wip_paths=(self._MEMORY_FILE, "orchestrator/agent_salvage.py"),
        )
        assert "inspect it before starting work" in body

    def test_unknown_paths_are_treated_as_substantial(self):
        """No path set ⇒ the imperative. Soft wording is opt-in, never a default.

        This is a live production path, not merely defensive (R7 B1): a
        filename whose bytes are not valid UTF-8 makes the staged-path read
        undecodable, and ``_preserve_dirty_tree`` commits blind rather than
        lose the tree over a name — so the record knows the sha but not the
        contents. It must degrade to the loud branch rather than the quiet
        one. The end-to-end path is pinned by
        ``TestDirtyTreePreservedBeforeReset``'s undecodable-filename cases.
        """
        body = self._body(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=None,
            wip_paths=None,
        )
        assert "inspect it before starting work" in body
        assert "read it if you need it" not in body

    def test_multi_commit_discard_describes_the_stack(self):
        body = self._body(
            n_commits=3,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=2,
            wip_paths=("a.py", "b.py"),
        )
        assert "The full commit stack is preserved" in body
        assert "one of which is an automatic snapshot" in body
        assert "nothing was lost" in body

    def test_snapshot_only_push_failure_escalates(self):
        """The untested cell: #3639 during a gateway outage."""
        body = self._body(
            n_commits=1,
            recovery_ref=None,
            salvage_error="gateway down",
            wip_commit="aaaa1111",
            wip_files=33,
            wip_paths=tuple(f"src/mod_{i}.py" for i in range(33)),
        )
        assert "nothing was lost" not in body.lower()
        assert "egg/recovered/" not in body
        assert "33 file(s) of uncommitted work" in body
        assert "was NOT" in body and "local object store" in body
        assert "Escalate" in body
        assert "salvage_agent_commits cannot recover them" in body

    def test_partial_snapshot_is_flagged_to_the_reader(self):
        """A truncated capture must not look identical to a complete one.

        The WARNING that records the failed ``add`` goes to orchestrator
        logs, which the resuming agent never reads — so an agent that
        cherry-picks a silently-truncated snapshot believes it recovered
        everything.
        """
        msg = self._message(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=5,
            wip_paths=tuple(f"src/mod_{i}.py" for i in range(5)),
            wip_partial=True,
        )
        assert "INCOMPLETE" in msg.body
        assert msg.metadata["wip_partial"] is True

    def test_partial_machine_state_snapshot_keeps_the_imperative(self):
        """A truncated capture cannot earn the soft branch (R4 blocking #1).

        When ``git add -A`` does not complete cleanly, ``wip_paths`` is by construction
        only the subset that reached the index — whatever failed to stage is
        absent from it. "Every captured path is a state file" then says
        nothing about the working tree, so this is the same missing evidence
        as ``wip_paths=None`` and must degrade the same way. Without this the
        body contradicts itself: "holds only X, not agent work" followed by
        "files may be missing from it", on the one input where the captured
        set is least representative.
        """
        body = self._body(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=1,
            wip_paths=(self._MEMORY_FILE,),
            wip_partial=True,
        )
        assert "inspect it before starting work" in body
        assert "Treat it as a WIP checkpoint" in body
        assert "INCOMPLETE" in body
        assert "read it if you need it" not in body

    def test_plural_machine_state_snapshot_names_each_path(self):
        """Two memory files must not read as a singular apposition (R4 #3).

        The descriptor stays out of the sentence's grammar so the clause
        survives both a plural subject and a second entry in
        ``_MACHINE_STATE_FILE_GLOBS`` whose provenance is not BRC ack/nack.
        """
        paths = (
            self._MEMORY_FILE,
            ".egg-state/agent-outputs/tester/brc-memory-pipe-1.md",
        )
        body = self._body(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=2,
            wip_paths=paths,
        )
        assert "only 2 files" in body
        for path in paths:
            assert f"`{path}`" in body
        assert "a state file the orchestrator rewrites" not in body
        assert "inspect it before starting work" not in body

    def test_wide_roster_snapshot_states_a_count_instead_of_naming_paths(self):
        """Past ``_SOFT_BRANCH_MAX_NAMED_PATHS`` the body stops enumerating (R5 #3).

        This is the branch a real roster hits: five BRC roles each leaving a
        memory file is five paths, and inlining a dozen of them to say
        "nothing here" spends the reader's context budget on noise. The full
        list still rides in the metadata.
        """
        paths = tuple(
            f".egg-state/agent-outputs/{role}/brc-memory-pipe-1.md"
            for role in ("coder", "tester", "reviewer_code", "reviewer_design", "documenter")
        )
        msg = self._message(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=len(paths),
            wip_paths=paths,
        )
        assert "holds only 5 files — machine-maintained coordination state" in msg.body
        for path in paths:
            assert f"`{path}`" not in msg.body
        assert "read it if you need it" in msg.body
        assert msg.metadata["wip_paths"] == list(paths)
        # Dropping the enumeration is a rendering choice inside the soft
        # branch, not an exit from it: the verdict the metadata reports has
        # to still say softened (R6 #2).
        assert msg.metadata["wip_softened"] is True
        assert msg.metadata["wip_machine_state_only"] is True

    def test_exactly_max_named_paths_still_enumerates(self):
        """``len(paths) == _SOFT_BRANCH_MAX_NAMED_PATHS`` is the last inlining case.

        The cap is a ``<=``, so four paths enumerate and five do not. 1, 2,
        and 5 are covered elsewhere; this pins the boundary itself so an
        off-by-one in either direction shows up as a test failure rather than
        as one path silently dropped from — or a wide roster silently
        inlined into — the body (R6 #2).
        """
        from kubernetes_spawner._worktree import _SOFT_BRANCH_MAX_NAMED_PATHS

        paths = tuple(
            f".egg-state/agent-outputs/{role}/brc-memory-pipe-1.md"
            for role in ("coder", "tester", "reviewer_code", "documenter")
        )
        assert len(paths) == _SOFT_BRANCH_MAX_NAMED_PATHS
        msg = self._message(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=len(paths),
            wip_paths=paths,
        )
        assert f"only {len(paths)} files (" in msg.body
        for path in paths:
            assert f"`{path}`" in msg.body
        assert "read it if you need it" in msg.body
        assert msg.metadata["wip_softened"] is True

    def test_flat_orchestrator_state_files_are_machine_state(self):
        """``agent-outputs/`` residue is not only the per-role memory file.

        ``consensus-confirmed`` and the applier's *input* handoff are written
        by orchestrator code too, so a respawn whose only dirt is a memory
        file plus one of them is the same noise the softening exists for.
        """
        body = self._body(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=3,
            wip_paths=(
                self._MEMORY_FILE,
                ".egg-state/agent-outputs/consensus-confirmed",
                ".egg-state/agent-outputs/pipe-1-apply-handoff.json",
            ),
        )
        assert "read it if you need it" in body
        assert "inspect it before starting work" not in body

    def test_agent_written_outputs_are_not_machine_state(self):
        """The applier's and tester's own outputs are agent work, not residue.

        They sit in the same directory and look alike, which is exactly why
        the discriminator is an explicit allowlist rather than a prefix
        match on ``.egg-state/agent-outputs/``.
        """
        for path in (
            ".egg-state/agent-outputs/pipe-1-wontdo.json",
            ".egg-state/agent-outputs/pipe-1-tester-output.json",
        ):
            body = self._body(
                n_commits=1,
                recovery_ref="egg/recovered/x",
                wip_commit="aaaa1111",
                wip_files=1,
                wip_paths=(path,),
            )
            assert "inspect it before starting work" in body, path

    def test_glob_star_does_not_cross_a_path_separator(self):
        """The discriminator matches by name, so it must match precisely (R4 #5).

        ``fnmatch``'s ``*`` crosses ``/``, so a deeper path or a lookalike
        directory would slip onto the soft branch — the first two cases are
        what the old primitive genuinely got wrong. The third pins
        case-sensitivity as intended semantics on every platform rather than
        as a regression: ``os.path.normcase`` is the identity on POSIX, so
        ``fnmatch`` already rejected it on the deployment platform. Nothing
        writes any of these today — the point is that the primitive cannot be
        the thing that lets one through later.
        """
        for path in (
            ".egg-state/agent-outputs/a/b/c/brc-memory-x.md",
            ".egg-state/agent-outputs/coder/brc-memory-p1.md/evil.md",
            ".egg-state/Agent-Outputs/coder/brc-memory-p1.md",
        ):
            body = self._body(
                n_commits=1,
                recovery_ref="egg/recovered/x",
                wip_commit="aaaa1111",
                wip_files=1,
                wip_paths=(path,),
            )
            assert "inspect it before starting work" in body, path

    def test_replacement_does_not_move_the_softening_decision(self):
        """A U+FFFD in a path matches exactly what its raw bytes would (R9 NB-2).

        The comment on the ``errors="replace"`` read used to claim a replaced
        name "still fails every glob", which is false — the ``*`` in
        ``brc-memory*.md`` swallows a U+FFFD happily, and the first case here
        softens. The property that actually holds is neutrality: every
        non-``*`` character in ``_MACHINE_STATE_FILE_GLOBS`` is ASCII and
        replacement only ever substitutes non-ASCII (U+FFFD) for non-ASCII
        (bytes >= 0x80), so a literal position can neither gain nor lose a
        match and ``*`` regions are length-agnostic. Segment count survives
        for the same reason: ``/`` is 0x2F and never appears inside an
        invalid sequence.

        The rule a new entry inherits is "ASCII **literals and ``*``**", not
        "ASCII" (R9 NB-1). Replacement is not length-preserving — a truncated
        multi-byte sequence collapses to a *single* U+FFFD — and ``?`` /
        ``[...]`` are ASCII but length-sensitive, so ``brc-memory-??.md``
        would satisfy a bare "keep it ASCII" and still lose a match its raw
        bytes had::

            fnmatchcase("x\\udcf0\\udc9f\\udc98.md", "x???.md")  -> True
            fnmatchcase("x\\ufffd.md",               "x???.md")  -> False

        So this asserts the glob set holds to literals-and-``*`` rather than
        leaving it to a comment.
        """
        from kubernetes_spawner._worktree import _MACHINE_STATE_FILE_GLOBS

        for glob in _MACHINE_STATE_FILE_GLOBS:
            assert "?" not in glob and "[" not in glob, (
                f"{glob!r} uses a length-sensitive wildcard; replacement collapses a "
                "truncated multi-byte sequence to one U+FFFD, so this glob can lose a "
                "match the raw bytes had (see this test's docstring)"
            )
            assert glob.replace("*", "").isascii(), glob

        replaced_memory = b".egg-state/agent-outputs/coder/brc-memory-caf\xe9.md".decode(
            "utf-8", errors="replace"
        )
        replaced_output = b".egg-state/agent-outputs/pipe-caf\xe9-wontdo.json".decode(
            "utf-8", errors="replace"
        )
        assert "�" in replaced_memory and "�" in replaced_output

        # A state file whose name went through replacement still softens...
        soft = self._body(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=1,
            wip_paths=(replaced_memory,),
        )
        assert "read it if you need it" in soft
        assert "inspect it before starting work" not in soft

        # ...and agent output whose name did still takes the imperative.
        hard = self._body(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=1,
            wip_paths=(replaced_output,),
        )
        assert "inspect it before starting work" in hard

    def test_trivial_snapshot_with_failed_push_still_names_the_snapshot(self):
        """The opening-clause suppression is conditional on ``recovery_ref`` (R4 #4).

        It is justified by ``recovery_text`` already naming the snapshot —
        true only on the pushed branch. With the salvage push failed,
        ``recovery_text`` is the escalation prose, which never names it, so
        dropping the clarifier would leave the reader with a bare commit
        count and no statement of what was discarded.
        """
        body = self._body(
            n_commits=1,
            recovery_ref=None,
            salvage_error="gateway down",
            wip_commit="aaaa1111",
            wip_files=1,
            wip_paths=(self._MEMORY_FILE,),
        )
        assert "(an automatic snapshot of uncommitted work)" in body
        assert "Escalate" in body

    def test_metadata_carries_the_size_and_completeness_claims(self):
        """The body makes both claims; a consumer must not have to regex prose."""
        msg = self._message(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=33,
            wip_paths=tuple(f"src/mod_{i}.py" for i in range(33)),
        )
        assert msg.metadata["wip_files"] == 33
        assert msg.metadata["wip_partial"] is False

    def test_metadata_carries_the_softening_inputs(self):
        """A consumer seeing a softened body must be able to reconstruct why.

        ``wip_files``/``wip_partial`` describe the snapshot; ``wip_paths``,
        ``wip_machine_state_only`` and ``wip_softened`` are what the wording
        was *decided from* and what it decided, and without them the
        softening is unauditable downstream.
        """
        soft = self._message(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=1,
            wip_paths=(self._MEMORY_FILE,),
        )
        assert soft.metadata["wip_paths"] == [self._MEMORY_FILE]
        assert soft.metadata["wip_paths_truncated"] is False
        assert soft.metadata["wip_machine_state_only"] is True
        assert soft.metadata["wip_softened"] is True

        loud = self._message(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=1,
            wip_paths=("src/feature.py",),
        )
        assert loud.metadata["wip_machine_state_only"] is False
        assert loud.metadata["wip_softened"] is False

    def test_metadata_predicate_and_verdict_diverge(self):
        """The path predicate and the wording verdict are separate fields (R5 #1).

        Each of the three cases below has machine-state-only paths but a body
        that is *not* softened, for a different reason. Reporting the verdict
        under ``wip_machine_state_only`` would contradict ``wip_paths`` in the
        same dict; reporting the predicate as the verdict would tell a triage
        query filtering for softened records that the escalation body below
        was softened. Both fields, both honest.
        """
        machine_state = {
            "recovery_ref": "egg/recovered/x",
            "wip_commit": "aaaa1111",
            "wip_files": 1,
            "wip_paths": (self._MEMORY_FILE,),
        }

        # A stack of the agent's own commits rode along with the snapshot:
        # the paths are still state files, the discard is not trivial.
        multi = self._message(**{**machine_state, "n_commits": 3})
        assert multi.metadata["wip_machine_state_only"] is True
        assert multi.metadata["wip_softened"] is False
        assert "inspect it before starting work" in multi.body

        # The capture is truncated, so the path list does not describe the
        # tree — softening cannot be earned from it.
        partial = self._message(**{**machine_state, "n_commits": 1, "wip_partial": True})
        assert partial.metadata["wip_machine_state_only"] is True
        assert partial.metadata["wip_softened"] is False
        assert "inspect it before starting work" in partial.body

        # The salvage push failed: the body is the loudest one this function
        # emits, so the verdict must not read as softened.
        unpushed = self._message(
            **{
                **machine_state,
                "n_commits": 1,
                "recovery_ref": None,
                "salvage_error": "gateway down",
            }
        )
        assert unpushed.metadata["wip_machine_state_only"] is True
        assert unpushed.metadata["wip_softened"] is False
        assert "Escalate to an operator" in unpushed.body

    def test_metadata_path_list_is_capped(self):
        """A pathological working tree must not inline itself into the bus.

        ``wip_files`` still carries the untruncated count, and the truncation
        is flagged so a consumer does not read the capped list as complete.
        """
        msg = self._message(
            n_commits=1,
            recovery_ref="egg/recovered/x",
            wip_commit="aaaa1111",
            wip_files=200,
            wip_paths=tuple(f"src/mod_{i}.py" for i in range(200)),
        )
        assert len(msg.metadata["wip_paths"]) == 50
        assert msg.metadata["wip_paths_truncated"] is True
        assert msg.metadata["wip_files"] == 200

    def test_no_snapshot_body_is_unchanged(self):
        """A pure commit discard says nothing about snapshots."""
        body = self._body(n_commits=2, recovery_ref="egg/recovered/x", wip_commit=None)
        assert "snapshot" not in body
        assert "The full commit stack is preserved" in body


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

    def test_reuse_syncs_phase_across_transition(self, spawner, mock_gateway):
        """Reuse across a phase boundary syncs the session's phase (#3528).

        Sessions are NOT torn down at phase end (cleanup_pipeline runs at
        pipeline end and phase advances skip it via the run_epoch bump), so
        a session registered in refine used to be reused in plan with its
        gateway-side phase still 'refine', and the commit gate denied the
        agent for the rest of the pipeline. The reuse path must PATCH the
        phase before handing out the stub.
        """
        cache_key = ("pipe-1", "simplifier", None, "egg-agent-pipe-1-simplifier")
        spawner._session_token_cache[cache_key] = "tok-refine-era"
        mock_gateway.heartbeat_session_by_container.return_value = True
        mock_gateway.update_session_phase.return_value = True

        session = spawner._get_or_create_session(
            pipeline_id="pipe-1",
            agent_role=AgentRole.SIMPLIFIER,
            mode="public",
            repos=["owner/repo"],
            phase="plan",
        )

        assert session is not None
        assert session.session_token == "tok-refine-era"
        mock_gateway.update_session_phase.assert_called_once_with("tok-refine-era", "plan")
        mock_gateway.register_session.assert_not_called()

    def test_reuse_phase_sync_failure_re_registers(self, spawner, mock_gateway):
        """A failed phase sync must not hand out a session the gateway will
        deny; drop it and register fresh (with the current phase)."""
        cache_key = ("pipe-1", "simplifier", None, "egg-agent-pipe-1-simplifier")
        spawner._session_token_cache[cache_key] = "tok-stale-phase"
        mock_gateway.heartbeat_session_by_container.return_value = True
        mock_gateway.update_session_phase.return_value = False
        mock_gateway.register_session.return_value = _FakeSessionInfo(
            session_token="tok-fresh-phase",
            container_id="egg-agent-pipe-1-simplifier",
        )

        session = spawner._get_or_create_session(
            pipeline_id="pipe-1",
            agent_role=AgentRole.SIMPLIFIER,
            mode="public",
            repos=["owner/repo"],
            phase="plan",
        )

        assert session is not None
        assert session.session_token == "tok-fresh-phase"
        # The stale-phase session was dropped, not left to shadow the
        # fresh registration under the same container id.
        mock_gateway.delete_session.assert_called_once_with("tok-stale-phase")
        assert mock_gateway.register_session.call_args.kwargs["phase"] == "plan"
        assert spawner._session_token_cache[cache_key] == "tok-fresh-phase"

    def test_reuse_without_phase_skips_sync(self, spawner, mock_gateway):
        """No phase supplied ⇒ no sync round-trip; pre-#3528 reuse behavior."""
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
        mock_gateway.update_session_phase.assert_not_called()
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


# ---------------------------------------------------------------------------
# TestOverseerSpawnNormalization (#2270 §1.5, slice-3)
# ---------------------------------------------------------------------------
#
# Slice-3 folds ``spawn_overseer_job`` into ``spawn_agent_job(agent_role=
# OVERSEER, …)`` so the overseer runs through the SAME spawn/entrypoint path as
# every other agent — "just a particular agent with different permissions and a
# different prompt" (task description §1.5). The bespoke ``EGG_OVERSEER_*`` env
# and the trust-and-run ``overseer_monitor.py --once`` bootstrap (the direct
# cause of the §1 self-injection loop) are deleted.
#
# These tests encode the slice-3 acceptance gate:
#   * the overseer spawns via the generic ``spawn_agent_job`` path (passes today),
#   * the generic path injects NO bespoke ``EGG_OVERSEER_*`` env (passes today —
#     guards against re-introducing special-case plumbing on this path),
#   * the bespoke ``spawn_overseer_job`` / ``spawn_overseer_container`` helpers
#     are gone (red until coder task-3-1 lands; green at slice integration),
#   * the baked-in ``sandbox/overseer_monitor.py`` script and any reference to it
#     in the spawner are gone (red until coder task-3-1/task-3-2 land).
# The deletion-regression rows go red→green exactly like the slice-2 tester
# contract did; they are strict (NOT xfail) because the coder work that flips
# them green lands inside THIS slice, not a downstream one.


class TestOverseerSpawnNormalization:
    """The overseer is a normal agent — no parallel spawn plumbing (#2270 §1.5)."""

    def _spawn_overseer(self, spawner, monkeypatch):
        """Spawn the overseer through the generic agent path.

        Mirrors ``test_spawn_reviewer_without_repos_succeeds``: the overseer is
        in ``_ROLES_WITHOUT_WORKTREE`` so it needs no ``repos``; undo the
        conftest autouse stub so the real worktree guard runs.
        """
        import kubernetes_spawner

        monkeypatch.setattr(
            kubernetes_spawner,
            "_role_needs_worktree",
            lambda role: role not in kubernetes_spawner._ROLES_WITHOUT_WORKTREE,
        )
        return spawner.spawn_agent_job(
            pipeline_id="pipe-overseer",
            agent_role=AgentRole.OVERSEER,
            phase="implement",
        )

    def test_overseer_spawns_through_spawn_agent_job(self, spawner, monkeypatch):
        """Overseer spawns via the generic ``spawn_agent_job`` path.

        The role resolves to a normal SpawnedContainer with the standard agent
        env — proving there is no special-case spawn entrypoint for the
        overseer (the §1.5 hard constraint).
        """
        result = self._spawn_overseer(spawner, monkeypatch)

        assert result.agent_role == AgentRole.OVERSEER
        env = result.environment
        assert env["EGG_AGENT_ROLE"] == "overseer"
        assert env["EGG_PIPELINE_ID"] == "pipe-overseer"
        # Standard agent wiring — same as every other role.
        assert "EGG_SESSION_TOKEN" in env
        assert "GATEWAY_URL" in env

    def test_overseer_spawn_has_no_bespoke_overseer_env(self, spawner, monkeypatch):
        """The generic spawn path injects no ``EGG_OVERSEER_*`` env.

        ``EGG_OVERSEER_MODE`` / ``EGG_OVERSEER_POLL_INTERVAL`` /
        ``EGG_OVERSEER_DECISION_MODEL`` were the symptoms of the special-case
        shape. The folded path must carry none of them.
        """
        result = self._spawn_overseer(spawner, monkeypatch)
        env = result.environment

        bespoke = [k for k in env if k.startswith("EGG_OVERSEER_")]
        assert bespoke == [], f"bespoke overseer env leaked into generic spawn: {bespoke}"
        for key in (
            "EGG_OVERSEER_MODE",
            "EGG_OVERSEER_POLL_INTERVAL",
            "EGG_OVERSEER_DECISION_MODEL",
        ):
            assert key not in env

    def test_spawn_overseer_job_helper_removed(self, spawner):
        """The bespoke ``spawn_overseer_job`` helper + alias are deleted.

        Deletion regression for coder task-3-1: folding into
        ``spawn_agent_job`` removes the dedicated method and its
        ``spawn_overseer_container`` back-compat alias. Red until the fold
        lands; green at slice integration.
        """
        assert not hasattr(spawner, "spawn_overseer_job"), (
            "spawn_overseer_job must be folded into spawn_agent_job(OVERSEER) (#2270 §1.5)"
        )
        assert not hasattr(spawner, "spawn_overseer_container"), (
            "the spawn_overseer_container alias must be removed alongside spawn_overseer_job"
        )

    def test_spawner_source_has_no_monitor_script_bootstrap(self):
        """The spawner no longer references the baked-in monitor script.

        Acceptance: "no monitor-script reference". The
        ``overseer_monitor.py --once`` trust-and-run bootstrap (and the prompt
        that injected it) is removed from ``kubernetes_spawner.py``.
        """
        import kubernetes_spawner

        source = Path(kubernetes_spawner.__file__).read_text(encoding="utf-8")
        assert "overseer_monitor" not in source, (
            "kubernetes_spawner.py must not reference the deleted overseer_monitor.py script"
        )

    def test_overseer_monitor_script_deleted(self):
        """The baked-in ``sandbox/overseer_monitor.py`` is deleted (net-negative).

        Deletion regression for coder task-3-2. ``sandbox/`` is COPY-baked into
        the image via the blanket ``COPY . /opt/egg-runtime/`` layer, so the
        only thing to remove is the source file itself.
        """
        repo_root = Path(__file__).resolve().parents[2]
        monitor = repo_root / "sandbox" / "overseer_monitor.py"
        assert not monitor.exists(), (
            f"{monitor} must be deleted — the on-demand overseer monitors via MCP/tools, "
            "not a trust-and-run baked-in script (#2270 §1.5)"
        )


# ---------------------------------------------------------------------------
# Multi-repo agent environment (#3393 slice-3, task-3-3)
# ---------------------------------------------------------------------------


class TestMultiRepoAgentEnv:
    """The agent environment exposes the FULL owner/repo-keyed worktree map.

    #3393 slice-3 removes the ``repos[0]`` collapse in the spawner: instead of
    deriving a single repo from ``repos[0]``, the spawner exposes the whole
    ``owner/repo -> container worktree path`` map as ``EGG_PIPELINE_REPOS``
    (JSON) so a per-slice agent can select ITS slice's repo worktree rather
    than being collapsed onto the primary. ``EGG_PIPELINE_REPO`` stays
    populated with the primary (first) repo for back-compat.

    The map is keyed by FULL ``owner/repo`` (operator ruling #6) so two repos
    with the same short name under different owners (``ownerA/foo`` vs
    ``ownerB/foo``) get distinct KEYS instead of collapsing to one bare-name
    entry.
    """

    def test_agent_env_exposes_full_owner_repo_worktree_map(self, spawner, mock_gateway):
        """A multi-repo spawn exposes every repo's worktree, keyed by owner/repo.

        The canonical motivating pattern (a schema repo + its consumer repo)
        must surface BOTH repos in ``EGG_PIPELINE_REPOS`` — the full owner/repo
        -> container-path map — while ``EGG_PIPELINE_REPO`` resolves to the
        primary (first) repo for back-compat. Distinct bare names here so both
        keys and paths are unambiguous (the same-bare-name key case is covered
        separately below).
        """
        mock_gateway.create_worktrees.return_value = _FakeWorktreeResult(
            worktrees={
                "ownerA/schema": "/home/egg/.egg-worktrees/pipe-multi/ownerA-schema",
                "ownerB/consumer": "/home/egg/.egg-worktrees/pipe-multi/ownerB-consumer",
            }
        )

        result = spawner.spawn_agent_job(
            pipeline_id="pipe-multi",
            agent_role=AgentRole.CODER,
            repos=["ownerA/schema", "ownerB/consumer"],
        )
        env = result.environment

        # Full owner/repo-keyed map: an entry per repo, keyed by full owner/repo,
        # valued by the container worktree path.
        repo_map = json.loads(env["EGG_PIPELINE_REPOS"])
        assert repo_map == {
            "ownerA/schema": "/home/egg/repos/schema",
            "ownerB/consumer": "/home/egg/repos/consumer",
        }

        # Back-compat: the singleton still resolves to the primary (first) repo.
        assert env["EGG_PIPELINE_REPO"] == "ownerA/schema"

    def test_agent_env_same_name_repos_get_distinct_map_keys(self, spawner, mock_gateway):
        """Same short name under different owners → distinct owner/repo KEYS.

        Operator ruling #6: the worktree map is re-keyed by full ``owner/repo``
        so ``ownerA/foo`` and ``ownerB/foo`` do NOT collapse to a single
        bare-name (``foo``) entry — the map has two distinct keys.

        NOTE (non-blocking, flagged to reviewers/coder): the map *values* are
        currently derived as ``/home/egg/repos/<bare-name>``, so two
        same-bare-name repos share the same container mount target. That is a
        property of the container mount-path scheme (``host_path_mounts``),
        broader than this slice's owner/repo re-key of the map KEYS; this test
        deliberately asserts only key-distinctness and does not bless the
        value collision.
        """
        mock_gateway.create_worktrees.return_value = _FakeWorktreeResult(
            worktrees={
                "ownerA/foo": "/home/egg/.egg-worktrees/pipe-multi/ownerA-foo",
                "ownerB/foo": "/home/egg/.egg-worktrees/pipe-multi/ownerB-foo",
            }
        )

        result = spawner.spawn_agent_job(
            pipeline_id="pipe-multi",
            agent_role=AgentRole.CODER,
            repos=["ownerA/foo", "ownerB/foo"],
        )
        repo_map = json.loads(result.environment["EGG_PIPELINE_REPOS"])

        assert set(repo_map.keys()) == {"ownerA/foo", "ownerB/foo"}
        assert len(repo_map) == 2, "same-short-name repos must not collapse to one key"

    def test_agent_env_single_repo_backcompat(self, spawner, mock_gateway):
        """N=1 pipelines still expose a one-entry map and the singleton env var.

        The map-shaped env exposure must be behaviour-identical for the
        single-repo case: a one-element ``EGG_PIPELINE_REPOS`` map plus
        ``EGG_PIPELINE_REPO`` pointing at that repo.
        """
        mock_gateway.create_worktrees.return_value = _FakeWorktreeResult(
            worktrees={"owner/solo": "/home/egg/.egg-worktrees/pipe-solo/owner-solo"}
        )

        result = spawner.spawn_agent_job(
            pipeline_id="pipe-solo",
            agent_role=AgentRole.TESTER,
            repos=["owner/solo"],
        )
        env = result.environment

        assert json.loads(env["EGG_PIPELINE_REPOS"]) == {"owner/solo": "/home/egg/repos/solo"}
        assert env["EGG_PIPELINE_REPO"] == "owner/solo"


# ---------------------------------------------------------------------------
# repos[0]-collapse ratchet (#3393 slice-3, task-3-3)
# ---------------------------------------------------------------------------


def _bare_repos_index_zero_sites(paths):
    """Return ``(relpath, lineno, source_line)`` for every bare ``repos[0]`` /
    ``pipeline_repos[0]`` *code* token in ``paths``.

    Uses ``tokenize`` (not a text grep) so comments and docstrings that mention
    ``repos[0]`` are ignored — only executable tokens count. A leading ``.``
    is excluded so the intentional primary accessor (``self.repos[0]`` /
    ``pipeline.repos[0]``, i.e. the ``primary_repo`` property internals) is NOT
    flagged; only the *collapse* shape — indexing a bare local ``repos`` /
    ``pipeline_repos`` list — is caught.
    """
    hits = []
    for path in paths:
        if not path.exists():
            continue
        try:
            tokens = list(tokenize.tokenize(io.BytesIO(path.read_bytes()).readline))
        except SyntaxError, tokenize.TokenError:
            continue
        for i, tok in enumerate(tokens):
            if tok.type != tokenize.NAME or tok.string not in ("repos", "pipeline_repos"):
                continue
            prev = tokens[i - 1] if i > 0 else None
            if prev is not None and prev.type == tokenize.OP and prev.string == ".":
                # ``self.repos[0]`` / ``pipeline.repos[0]`` — the primary
                # accessor, not a collapse.
                continue
            nxt = tokens[i + 1 : i + 4]
            if (
                len(nxt) >= 3
                and nxt[0].string == "["
                and nxt[1].string == "0"
                and nxt[2].string == "]"
            ):
                hits.append((path, tok.start[0], tok.line.strip()))
    return hits


class TestReposZeroCollapseRatchet:
    """Regression ratchet: no ``repos[0]`` collapse in orchestrator source.

    #3393 slice-3 removes the three enumerated collapse sites
    (``kubernetes_spawner/_spawn.py``, ``commit_authorship_store.py``,
    ``routes/pipelines.py``) that assumed a single repo by indexing
    ``repos[0]``. This test fails if any of them reappears — or if a NEW
    un-enumerated collapse site is introduced anywhere in the orchestrator
    package.

    The one intentionally-allowlisted site is
    ``sandbox/egg_lib/sdlc_hitl.py`` where ``repos[0]`` is guarded by an
    explicit ``len(repos) == 1`` check and is therefore NOT a collapse.
    """

    # Paths (repo-relative) permitted to contain a bare ``repos[0]`` token.
    _ALLOWLIST = {"sandbox/egg_lib/sdlc_hitl.py"}

    def _repo_root(self):
        return Path(__file__).resolve().parents[2]

    def _scanned_paths(self):
        root = self._repo_root()
        orchestrator = root / "orchestrator"
        paths = [
            p
            for p in orchestrator.rglob("*.py")
            # Test modules legitimately reference ``repos[0]`` (and this very
            # ratchet's fixtures do too) — scan only orchestrator *source*.
            if not str(p.relative_to(root)).startswith("orchestrator/tests/")
        ]
        # Explicitly include the allowlisted guarded site so the allowlist is
        # meaningful (task-3-3): if the guard is ever removed and the site
        # multiplies, we still see it in the sweep.
        paths.append(root / "sandbox" / "egg_lib" / "sdlc_hitl.py")
        return paths

    def test_no_repos_zero_collapse_in_orchestrator_source(self):
        root = self._repo_root()
        hits = _bare_repos_index_zero_sites(self._scanned_paths())
        offenders = [
            (str(path.relative_to(root)), lineno, line)
            for (path, lineno, line) in hits
            if str(path.relative_to(root)) not in self._ALLOWLIST
        ]
        assert not offenders, (
            "`repos[0]` collapse reintroduced (indexing a bare repo list assumes "
            "a single repo — use `pipeline.primary_repo` or the full owner/repo "
            "map instead). Offending sites:\n"
            + "\n".join(f"  {p}:{n}  {ln}" for (p, n, ln) in offenders)
        )

    def test_ratchet_detects_a_planted_collapse(self):
        """The detector itself works — a bare ``repos[0]`` in a code token is
        found, while ``self.repos[0]`` and a docstring mention are not.

        Guards against the ratchet silently rotting into a no-op (e.g. a
        tokenizer change) and passing even when a real collapse exists.
        """
        planted = (
            "def f(repos):\n"
            '    """Docstring mentioning repos[0] must be ignored."""\n'
            "    x = repos[0]\n"  # <- the only real collapse
            "    y = self.repos[0]\n"  # <- primary accessor, ignored
            "    return x, y\n"
        )
        tmp = self._repo_root() / "orchestrator" / "__ratchet_probe__.py"
        try:
            tmp.write_bytes(planted.encode("utf-8"))
            hits = _bare_repos_index_zero_sites([tmp])
        finally:
            tmp.unlink(missing_ok=True)
        assert len(hits) == 1, hits
        assert hits[0][2] == "x = repos[0]"

    def test_allowlisted_guarded_site_is_present_and_exempt(self):
        """The guarded ``sdlc_hitl.py`` site exists and is exempt.

        Documents WHY the allowlist entry exists: the site is real (so the
        allowlist isn't dead) but is guarded by ``len(repos) == 1`` and is
        therefore not a single-repo collapse.
        """
        root = self._repo_root()
        sdlc_hitl = root / "sandbox" / "egg_lib" / "sdlc_hitl.py"
        hits = _bare_repos_index_zero_sites([sdlc_hitl])
        assert hits, "expected the guarded repos[0] site in sdlc_hitl.py"
        assert "sandbox/egg_lib/sdlc_hitl.py" in self._ALLOWLIST
