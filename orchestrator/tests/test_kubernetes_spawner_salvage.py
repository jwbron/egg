"""Verify cleanup_pipeline auto-salvages before deleting worktrees (#2429)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def mock_k8s_client():
    from kubernetes_client import PodNotFoundError
    from models import ContainerInfo, ContainerStatus

    client = MagicMock()
    client.delete_job.side_effect = PodNotFoundError("No existing job")
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


class TestCleanupSalvageHook:
    def test_salvage_runs_before_worktree_deletion(self, spawner, mock_gateway):
        """auto_salvage_pipeline is invoked before any delete_worktrees call."""
        call_order: list[str] = []

        def fake_salvage(*_args, **_kwargs):
            call_order.append("salvage")
            return []

        def fake_delete(*_args, **_kwargs):
            call_order.append("delete")
            return _FakeWorktreeResult(worktrees={})

        mock_gateway.delete_worktrees.side_effect = fake_delete

        with (
            patch("agent_salvage.auto_salvage_pipeline", side_effect=fake_salvage),
            patch(
                "kubernetes_spawner.WORKTREE_BASE_DIR",
                MagicMock(exists=MagicMock(return_value=False)),
            ),
        ):
            spawner.cleanup_pipeline("pipe-1")

        # Salvage must come before any deletion.
        assert "salvage" in call_order
        if "delete" in call_order:
            assert call_order.index("salvage") < call_order.index("delete")

    def test_salvage_failure_does_not_block_deletion(self, spawner, mock_gateway):
        """A salvage exception logs and continues — worktrees are still deleted."""
        with (
            patch(
                "agent_salvage.auto_salvage_pipeline",
                side_effect=RuntimeError("salvage exploded"),
            ),
            patch(
                "kubernetes_spawner.WORKTREE_BASE_DIR",
                MagicMock(exists=MagicMock(return_value=False)),
            ),
        ):
            # Must not raise.
            spawner.cleanup_pipeline("pipe-1")
        # delete_worktrees still called for the pipeline-level worktree id.
        mock_gateway.delete_worktrees.assert_any_call(container_id="pipe-1", force=True)

    def test_salvage_skipped_when_preserve_worktrees(self, spawner, mock_gateway):
        """preserve_worktrees=True takes the early-return; no salvage attempted."""
        with patch("agent_salvage.auto_salvage_pipeline") as mock_salvage:
            spawner.cleanup_pipeline("pipe-1", preserve_worktrees=True)
        mock_salvage.assert_not_called()
        mock_gateway.delete_worktrees.assert_not_called()

    def test_salvage_filter_includes_pipeline_level_id(self, spawner, mock_gateway):
        """The salvage filter receives the same set used for deletion."""
        captured: dict[str, object] = {}

        def fake_salvage(_gateway, pipeline_id, *, worktree_filter=None, **_kw):
            captured["pipeline_id"] = pipeline_id
            captured["filter"] = worktree_filter
            return []

        with (
            patch("agent_salvage.auto_salvage_pipeline", side_effect=fake_salvage),
            patch(
                "kubernetes_spawner.WORKTREE_BASE_DIR",
                MagicMock(exists=MagicMock(return_value=False)),
            ),
        ):
            spawner.cleanup_pipeline("pipe-1")
        assert captured["pipeline_id"] == "pipe-1"
        # At minimum, the pipeline-level worktree id is in scope for salvage.
        assert "pipe-1" in (captured["filter"] or set())

    def test_salvage_mode_and_base_branch_threaded_through(self, spawner, mock_gateway):
        """``salvage_mode`` / ``salvage_base_branch`` reach the hook (#2429 review).

        Regression test for the blocking review feedback: cleanup_pipeline
        used to default to ``mode="public"`` for every pipeline, which
        could silently lose private-mode work the hook is supposed to
        save. The caller now passes the running-pipeline's mode + base
        branch.
        """
        captured: dict[str, object] = {}

        def fake_salvage(_gateway, _pipeline_id, *, mode=None, base_branch=None, **_kw):
            captured["mode"] = mode
            captured["base_branch"] = base_branch
            return []

        with (
            patch("agent_salvage.auto_salvage_pipeline", side_effect=fake_salvage),
            patch(
                "kubernetes_spawner.WORKTREE_BASE_DIR",
                MagicMock(exists=MagicMock(return_value=False)),
            ),
        ):
            spawner.cleanup_pipeline(
                "pipe-1",
                salvage_mode="private",
                salvage_base_branch="main",
            )
        assert captured["mode"] == "private"
        assert captured["base_branch"] == "main"

    def test_salvage_mode_omitted_falls_back_to_public(self, spawner, mock_gateway):
        """``salvage_mode=None`` keeps the historical default (``"public"``).

        Callers that cannot load the Pipeline (legacy in-flight callers)
        are still safe — only public-repo work, which is the original
        behavior — because the hook passes no ``mode`` kwarg and
        ``auto_salvage_pipeline`` defaults to ``"public"``. The contract
        here is "if ``salvage_mode is None``, ``mode=`` is not forwarded."
        """
        captured: dict[str, object] = {}

        def fake_salvage(_gateway, _pipeline_id, *, mode="public", **_kw):
            captured["mode"] = mode
            return []

        with (
            patch("agent_salvage.auto_salvage_pipeline", side_effect=fake_salvage),
            patch(
                "kubernetes_spawner.WORKTREE_BASE_DIR",
                MagicMock(exists=MagicMock(return_value=False)),
            ),
        ):
            spawner.cleanup_pipeline("pipe-1")  # no salvage_mode kwarg
        assert captured["mode"] == "public"
