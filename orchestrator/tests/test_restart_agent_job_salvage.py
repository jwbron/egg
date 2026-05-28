"""Verify restart_agent_job auto-salvages before respawning (#2807).

When an agent crashes mid-task with uncommitted edits, the worktree
persists on disk. Before respawning a fresh container (which starts
from branch HEAD), restart_agent_job must salvage any unpushed commits
to egg/recovered/<pipeline>/<scope>/<sha> so operators can triage them.

Without this hook, uncommitted work sits in the worktree until pipeline
cleanup (which may never happen if the pipeline keeps running), and the
new agent never sees it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def mock_k8s_client():
    from kubernetes_client import PodNotFoundError
    from models import ContainerInfo, ContainerStatus

    client = MagicMock()
    client.delete_job.side_effect = PodNotFoundError("No existing job")
    client.wait_for_job_gone.return_value = True
    client.list_containers.return_value = []
    client.remove_container.return_value = None
    client.create_container.return_value = ContainerInfo(
        container_id="uid-x",
        container_name="egg-x",
        job_name="egg-x",
        status=ContainerStatus.PENDING,
    )
    return client


class _FakeWorktreeResult:
    def __init__(self, **kwargs):
        self.worktrees = kwargs.get("worktrees", {})
        self.errors = kwargs.get("errors", [])
        self.success = kwargs.get("success", True)


class _FakeGatewayHealth:
    healthy = True
    details = ""


class _FakeSessionInfo:
    container_id = "x"
    session_token = "tok"
    container_ip = "127.0.0.1"


class _FakeGatewayError(Exception):
    pass


@pytest.fixture()
def mock_gateway():
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
    from kubernetes_spawner import KubernetesSpawner

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
        return KubernetesSpawner(k8s_client=mock_k8s_client, gateway_client=mock_gateway)


class TestRestartAgentJobSalvageHook:
    """restart_agent_job invokes auto_salvage_pipeline before respawning (#2807)."""

    def test_salvage_runs_before_respawn(self, spawner, mock_gateway):
        """auto_salvage_pipeline is invoked before spawn_agent_job."""
        call_order: list[str] = []

        def fake_salvage(*_args, **_kwargs):
            call_order.append("salvage")
            return []

        def fake_spawn(*_args, **_kwargs):
            call_order.append("spawn")
            from kubernetes_spawner import SpawnedContainer
            from models import ContainerInfo, ContainerStatus

            return SpawnedContainer(
                container_info=ContainerInfo(
                    container_id="new-container",
                    container_name="egg-new-container",
                    status=ContainerStatus.RUNNING,
                ),
                session_info=None,
                agent_role=None,
                pipeline_id="pipe-1",
                environment={},
            )

        with (
            patch("agent_salvage.auto_salvage_pipeline", side_effect=fake_salvage),
            patch.object(spawner, "spawn_agent_job", side_effect=fake_spawn),
        ):
            from models import AgentRole

            spawner.restart_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                mode="public",
            )

        # Salvage must come before spawn.
        assert "salvage" in call_order
        assert "spawn" in call_order
        assert call_order.index("salvage") < call_order.index("spawn")

    def test_salvage_failure_does_not_block_respawn(self, spawner, mock_gateway):
        """A salvage exception logs and continues — respawn still proceeds."""
        spawn_called = False

        def fake_spawn(*_args, **_kwargs):
            nonlocal spawn_called
            spawn_called = True
            from kubernetes_spawner import SpawnedContainer
            from models import ContainerInfo, ContainerStatus

            return SpawnedContainer(
                container_info=ContainerInfo(
                    container_id="new-container",
                    container_name="egg-new-container",
                    status=ContainerStatus.RUNNING,
                ),
                session_info=None,
                agent_role=None,
                pipeline_id="pipe-1",
                environment={},
            )

        with (
            patch(
                "agent_salvage.auto_salvage_pipeline",
                side_effect=RuntimeError("salvage exploded"),
            ),
            patch.object(spawner, "spawn_agent_job", side_effect=fake_spawn),
        ):
            from models import AgentRole

            # Must not raise.
            result = spawner.restart_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                mode="public",
            )

        assert spawn_called
        assert result is not None

    def test_salvage_filter_scoped_to_agent_worktree(self, spawner, mock_gateway):
        """The salvage filter receives exactly the agent's worktree id."""
        captured: dict[str, object] = {}

        def fake_salvage(_gateway, pipeline_id, *, worktree_filter=None, **_kw):
            captured["pipeline_id"] = pipeline_id
            captured["filter"] = worktree_filter
            return []

        def fake_spawn(*_args, **_kwargs):
            from kubernetes_spawner import SpawnedContainer
            from models import ContainerInfo, ContainerStatus

            return SpawnedContainer(
                container_info=ContainerInfo(
                    container_id="new-container",
                    container_name="egg-new-container",
                    status=ContainerStatus.RUNNING,
                ),
                session_info=None,
                agent_role=None,
                pipeline_id="pipe-1",
                environment={},
            )

        with (
            patch("agent_salvage.auto_salvage_pipeline", side_effect=fake_salvage),
            patch.object(spawner, "spawn_agent_job", side_effect=fake_spawn),
        ):
            from models import AgentRole

            spawner.restart_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                mode="public",
            )

        assert captured["pipeline_id"] == "pipe-1"
        assert captured["filter"] == {"pipe-1-coder"}

    def test_salvage_filter_scoped_to_slice_worktree(self, spawner, mock_gateway):
        """Slice-scoped restart scopes the salvage filter to the slice worktree."""
        captured: dict[str, object] = {}

        def fake_salvage(_gateway, pipeline_id, *, worktree_filter=None, **_kw):
            captured["pipeline_id"] = pipeline_id
            captured["filter"] = worktree_filter
            return []

        def fake_spawn(*_args, **_kwargs):
            from kubernetes_spawner import SpawnedContainer
            from models import ContainerInfo, ContainerStatus

            return SpawnedContainer(
                container_info=ContainerInfo(
                    container_id="new-container",
                    container_name="egg-new-container",
                    status=ContainerStatus.RUNNING,
                ),
                session_info=None,
                agent_role=None,
                pipeline_id="pipe-1",
                environment={},
            )

        with (
            patch("agent_salvage.auto_salvage_pipeline", side_effect=fake_salvage),
            patch.object(spawner, "spawn_agent_job", side_effect=fake_spawn),
        ):
            from models import AgentRole

            spawner.restart_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                slice_id="slice-2",
                mode="public",
            )

        assert captured["pipeline_id"] == "pipe-1"
        assert captured["filter"] == {"pipe-1-slice-2-coder"}

    def test_salvage_mode_and_base_branch_threaded_through(self, spawner, mock_gateway):
        """mode and base_branch reach the salvage hook."""
        captured: dict[str, object] = {}

        def fake_salvage(_gateway, _pipeline_id, *, mode=None, base_branch=None, **_kw):
            captured["mode"] = mode
            captured["base_branch"] = base_branch
            return []

        def fake_spawn(*_args, **_kwargs):
            from kubernetes_spawner import SpawnedContainer
            from models import ContainerInfo, ContainerStatus

            return SpawnedContainer(
                container_info=ContainerInfo(
                    container_id="new-container",
                    container_name="egg-new-container",
                    status=ContainerStatus.RUNNING,
                ),
                session_info=None,
                agent_role=None,
                pipeline_id="pipe-1",
                environment={},
            )

        with (
            patch("agent_salvage.auto_salvage_pipeline", side_effect=fake_salvage),
            patch.object(spawner, "spawn_agent_job", side_effect=fake_spawn),
        ):
            from models import AgentRole

            spawner.restart_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                mode="private",
                base_branch="main",
            )

        assert captured["mode"] == "private"
        assert captured["base_branch"] == "main"

    def test_salvage_uncommitted_flag_forwarded(self, spawner, mock_gateway):
        """The restart path requests uncommitted-work capture (#2807).

        Without salvage_uncommitted=True the hook only salvages
        committed-but-unpushed work; the modal #2807 crash window is
        mid-Edit with nothing committed, so the flag is what makes the
        dirty tree survive the respawn's reset --hard.
        """
        captured: dict[str, object] = {}

        def fake_salvage(_gateway, _pipeline_id, *, salvage_uncommitted=False, **_kw):
            captured["salvage_uncommitted"] = salvage_uncommitted
            return []

        def fake_spawn(*_args, **_kwargs):
            from kubernetes_spawner import SpawnedContainer
            from models import ContainerInfo, ContainerStatus

            return SpawnedContainer(
                container_info=ContainerInfo(
                    container_id="new-container",
                    container_name="egg-new-container",
                    status=ContainerStatus.RUNNING,
                ),
                session_info=None,
                agent_role=None,
                pipeline_id="pipe-1",
                environment={},
            )

        with (
            patch("agent_salvage.auto_salvage_pipeline", side_effect=fake_salvage),
            patch.object(spawner, "spawn_agent_job", side_effect=fake_spawn),
        ):
            from models import AgentRole

            spawner.restart_agent_job(
                pipeline_id="pipe-1",
                agent_role=AgentRole.CODER,
                mode="public",
            )

        assert captured["salvage_uncommitted"] is True
