"""Integration regression tests for ``start_pipeline`` recovery (#2420).

The route-level unit tests in ``orchestrator/tests/test_start_pipeline.py``
monkeypatch ``routes.pipelines._count_live_pods_for_pipeline`` directly
and never exercise the chain that helper drives:

    route → _guard_live_pods_or_force
         → _count_live_pods_for_pipeline
         → _get_spawner().backend.list_containers(labels=...)
         → filter by ``_LIVE_POD_STATUSES``

That chain is what would actually run against a real k3s cluster. A
regression where the label query changes shape, the spawner-backend
contract drifts, or ``_LIVE_POD_STATUSES`` forgets a status (e.g.
``CREATING`` getting dropped after a spawner refactor) would slip past
the unit tier because the helper is replaced wholesale.

These tests stub only the spawner backend (the k8s SDK boundary) and
exercise the real ``_count_live_pods_for_pipeline``, the real
``_guard_live_pods_or_force``, and the real ``start_pipeline`` Flask
route end-to-end.
"""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Lazy imports — only resolved once orchestrator/ is on sys.path via the
# conftest path-setup block.
from models import (
    AgentExecution,
    ContainerInfo,
    ContainerStatus,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)

pytestmark = pytest.mark.integration


@contextmanager
def _noop_lock(*_args, **_kwargs):
    yield


def _failed_pipeline() -> Pipeline:
    """Build a FAILED pipeline whose REFINE phase has stale agent state.

    Mirrors the shape of the orphan scenario #2420 protects against:
    pipeline-level FAILED, current phase FAILED with persisted
    ``agents`` / ``containers`` / ``artifacts`` that the route would
    otherwise clear.
    """
    pipeline = Pipeline(
        id="issue-2420",
        issue_number=2420,
        repo="owner/repo",
        branch="egg/issue-2420/work",
        base_branch="main",
        mode="issue",
        status=PipelineStatus.FAILED,
        current_phase=PipelinePhase.REFINE,
        error="Container exited with code 1",
    )
    execution = pipeline.get_phase_execution(PipelinePhase.REFINE)
    execution.status = PipelineStatus.FAILED
    execution.started_at = datetime.now(UTC)
    execution.completed_at = datetime.now(UTC)
    execution.error = "Container exited with code 1"
    execution.agents = [AgentExecution(role="coder", container_id="old-container")]
    execution.artifacts = {"pr_url": "https://github.com/owner/repo/pull/99"}
    execution.containers = [
        ContainerInfo(
            container_id="old-container",
            container_name="egg-issue-2420-coder",
            status=ContainerStatus.RUNNING,
        )
    ]
    return pipeline


def _setup_route_mocks(mock_get_repo, mock_resolve, pipeline: Pipeline) -> MagicMock:
    mock_get_repo.return_value = Path("/repo")
    store = MagicMock()
    store.repo_path = Path("/repo")
    store.load_pipeline.return_value = pipeline
    mock_resolve.return_value = (store, pipeline)
    return store


def _stub_spawner(*containers: ContainerInfo) -> MagicMock:
    """Build a stub spawner whose ``backend.list_containers`` returns
    *containers* whenever invoked with the pipeline-label selector.
    """
    spawner = MagicMock()
    captured: dict = {}

    def _list(labels=None, **_kwargs):
        captured["labels"] = labels
        return list(containers)

    spawner.backend.list_containers.side_effect = _list
    spawner._captured = captured  # type: ignore[attr-defined]
    return spawner


@dataclass
class _RouteMocks:
    """The set of ``routes.pipelines`` patches every guard test needs.

    Returned by ``_patched_route`` so tests get named attributes instead
    of a 5-deep positional decorator stack that's easy to break when
    someone reorders.
    """

    spawner: MagicMock
    get_repo: MagicMock
    resolve: MagicMock
    run: MagicMock
    lock: MagicMock


@contextmanager
def _patched_route():
    """Patch the five ``routes.pipelines`` symbols every guard test needs.

    Replaces the per-test stack of five ``@patch`` decorators (whose
    positional ordering was easy to mis-thread on copy-paste) with a
    single ``with _patched_route() as m`` context that hands back named
    attributes.
    """
    with ExitStack() as stack:
        lock = stack.enter_context(
            patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
        )
        run = stack.enter_context(patch("routes.pipelines._run_pipeline"))
        resolve = stack.enter_context(patch("routes.pipelines._resolve_pipeline"))
        get_repo = stack.enter_context(patch("routes.pipelines.get_repo_path"))
        spawner = stack.enter_context(patch("routes.pipelines._get_spawner"))
        yield _RouteMocks(
            spawner=spawner,
            get_repo=get_repo,
            resolve=resolve,
            run=run,
            lock=lock,
        )


class TestLivePodGuardBackendIntegration:
    """End-to-end exercise of the live-pod guard through the real helper.

    Stubs only the spawner backend — the route, ``_guard_live_pods_or_force``,
    ``_count_live_pods_for_pipeline``, the label constant, and the
    ``_LIVE_POD_STATUSES`` filter are the real implementations.
    """

    def test_running_pod_blocks_reset_with_409(self, client):
        """A single RUNNING pod labeled to the pipeline => 409
        ``live_pods_present`` with ``live_pod_count=1``, and the route
        passes the correct label selector to the backend.
        """
        pipeline = _failed_pipeline()
        spawner = _stub_spawner(
            ContainerInfo(
                container_id="pod-1",
                container_name="egg-issue-2420-coder",
                status=ContainerStatus.RUNNING,
            )
        )
        with _patched_route() as m:
            _setup_route_mocks(m.get_repo, m.resolve, pipeline)
            m.spawner.return_value = spawner

            resp = client.post("/api/v1/pipelines/issue-2420/start")

            assert resp.status_code == 409, resp.data
            body = json.loads(resp.data)
            assert body["reason"] == "live_pods_present"
            assert body["details"]["live_pod_count"] == 1

            # The label selector reached the backend untouched. The literal
            # comes from ``kubernetes_client.LABEL_PIPELINE_ID`` — assert by
            # value so a rename surfaces here too.
            from kubernetes_client import LABEL_PIPELINE_ID

            assert spawner._captured["labels"] == {LABEL_PIPELINE_ID: "issue-2420"}

            # Phase state is intact — no orphaning happened.
            exec_ = pipeline.get_phase_execution(PipelinePhase.REFINE)
            assert exec_.status == PipelineStatus.FAILED
            assert exec_.agents != []
            assert exec_.artifacts != {}
            m.run.assert_not_called()

    @pytest.mark.parametrize(
        "live_status",
        [ContainerStatus.PENDING, ContainerStatus.CREATING, ContainerStatus.RUNNING],
    )
    def test_each_live_status_blocks_reset(self, client, live_status):
        """All three statuses in ``_LIVE_POD_STATUSES`` must count as live.

        Each status is its own pod — the regression we guard against is
        "spawner refactor drops one of these from the enum or
        ``_LIVE_POD_STATUSES`` forgets to include it." The unit tests
        mock the helper wholesale and miss that class entirely.
        """
        pipeline = _failed_pipeline()
        spawner = _stub_spawner(
            ContainerInfo(
                container_id="pod-x",
                container_name="egg-issue-2420-coder",
                status=live_status,
            )
        )
        with _patched_route() as m:
            _setup_route_mocks(m.get_repo, m.resolve, pipeline)
            m.spawner.return_value = spawner

            resp = client.post("/api/v1/pipelines/issue-2420/start")
            assert resp.status_code == 409, (live_status, resp.data)
            body = json.loads(resp.data)
            assert body["reason"] == "live_pods_present"
            assert body["details"]["live_pod_count"] == 1

    @pytest.mark.parametrize(
        "terminal_status",
        [ContainerStatus.FAILED, ContainerStatus.EXITED, ContainerStatus.REMOVED],
    )
    def test_terminal_pods_do_not_block_reset(self, client, terminal_status):
        """Terminal pods (Failed/Exited/Removed) must NOT count as live.

        k8s Jobs default to ``ttlSecondsAfterFinished=600`` so a terminal
        pod object can survive for up to 10 minutes — exactly the
        window where ``start_pipeline`` is most commonly called for
        recovery. If the guard counted these as "live" the reset path
        would 409 forever until the TTL elapsed.
        """
        pipeline = _failed_pipeline()
        spawner = _stub_spawner(
            ContainerInfo(
                container_id="pod-terminal",
                container_name="egg-issue-2420-coder",
                status=terminal_status,
            )
        )
        with _patched_route() as m:
            _setup_route_mocks(m.get_repo, m.resolve, pipeline)
            m.spawner.return_value = spawner

            resp = client.post("/api/v1/pipelines/issue-2420/start")
            # Reset proceeds — no orphan risk.
            assert resp.status_code == 200, (terminal_status, resp.data)
            exec_ = pipeline.get_phase_execution(PipelinePhase.REFINE)
            assert exec_.status == PipelineStatus.PENDING
            assert exec_.agents == []
            assert exec_.artifacts == {}

    def test_mixed_terminal_and_live_counts_only_live(self, client):
        """A mixed listing must surface only live pods in ``live_pod_count``."""
        pipeline = _failed_pipeline()
        spawner = _stub_spawner(
            ContainerInfo(
                container_id="pod-running",
                container_name="egg-coder",
                status=ContainerStatus.RUNNING,
            ),
            ContainerInfo(
                container_id="pod-pending",
                container_name="egg-tester",
                status=ContainerStatus.PENDING,
            ),
            ContainerInfo(
                container_id="pod-exited",
                container_name="egg-reviewer",
                status=ContainerStatus.EXITED,
            ),
            ContainerInfo(
                container_id="pod-failed",
                container_name="egg-orphan",
                status=ContainerStatus.FAILED,
            ),
        )
        with _patched_route() as m:
            _setup_route_mocks(m.get_repo, m.resolve, pipeline)
            m.spawner.return_value = spawner

            resp = client.post("/api/v1/pipelines/issue-2420/start")
            assert resp.status_code == 409, resp.data
            body = json.loads(resp.data)
            assert body["reason"] == "live_pods_present"
            # Two live (RUNNING + PENDING), two terminal — only live count.
            assert body["details"]["live_pod_count"] == 2

    def test_backend_exception_fails_safe_with_check_failed_reason(self, client):
        """A k8s API exception => 409 ``live_pod_check_failed``, not a 500.

        The route must not 500 on transient label-query failures —
        operators should see an actionable 409 with the override hint
        rather than an opaque error.
        """
        pipeline = _failed_pipeline()
        spawner = MagicMock()
        spawner.backend.list_containers.side_effect = RuntimeError(
            "ApiException: Connection refused"
        )
        with _patched_route() as m:
            _setup_route_mocks(m.get_repo, m.resolve, pipeline)
            m.spawner.return_value = spawner

            resp = client.post("/api/v1/pipelines/issue-2420/start")
            assert resp.status_code == 409, resp.data
            body = json.loads(resp.data)
            assert body["reason"] == "live_pod_check_failed"

            # Phase state preserved despite the unknown live count.
            exec_ = pipeline.get_phase_execution(PipelinePhase.REFINE)
            assert exec_.status == PipelineStatus.FAILED

    def test_force_true_overrides_live_pods_and_runs_reset(self, client):
        """``force=true`` proceeds with the reset even when pods are live.

        Validates the documented operator override path — required
        when the pods are wedged in a state ``cancel_task(cleanup=true)``
        can't recover from.
        """
        pipeline = _failed_pipeline()
        spawner = _stub_spawner(
            ContainerInfo(
                container_id="wedged",
                container_name="egg-stuck",
                status=ContainerStatus.RUNNING,
            )
        )
        with _patched_route() as m:
            _setup_route_mocks(m.get_repo, m.resolve, pipeline)
            m.spawner.return_value = spawner

            resp = client.post(
                "/api/v1/pipelines/issue-2420/start",
                json={"force": True, "force_reason": "wedged pod, manual recovery"},
            )

            assert resp.status_code == 200, resp.data
            exec_ = pipeline.get_phase_execution(PipelinePhase.REFINE)
            assert exec_.status == PipelineStatus.PENDING
            assert exec_.agents == []
            assert exec_.containers == []
            assert exec_.artifacts == {}

    @pytest.mark.parametrize(
        "non_true_force",
        # The route comment at routes/pipelines.py:21088-21091 calls out
        # `"true"`, `1`, `[]`, and `{}` as motivations for the `is True`
        # strict-bool predicate. All four are truthy under
        # ``bool(body.get("force"))`` but none equal ``True``; a refactor
        # to the loose predicate would silently flip every one of them
        # into an override.
        ["true", 1, [], {}],
        ids=["string-true", "int-one", "empty-list", "empty-dict"],
    )
    def test_non_true_force_values_do_not_bypass_guard(self, client, non_true_force):
        """Strict-bool: only literal ``True`` bypasses the guard.

        Every other truthy value — string ``"true"``, ``1``, ``[]``,
        ``{}`` — must still trip the live-pod guard. The unit tier
        (which mocks the helper) wouldn't catch a regression to
        ``bool(body.get("force"))`` because the route still routes to
        the live-pod branch — only the predicate runs in the wrong
        mode and the helper would never see the override-skip path.
        """
        pipeline = _failed_pipeline()
        spawner = _stub_spawner(
            ContainerInfo(
                container_id="pod-1",
                container_name="egg-coder",
                status=ContainerStatus.RUNNING,
            )
        )
        with _patched_route() as m:
            _setup_route_mocks(m.get_repo, m.resolve, pipeline)
            m.spawner.return_value = spawner

            resp = client.post(
                "/api/v1/pipelines/issue-2420/start",
                json={"force": non_true_force},
            )
            # The guard must still fire for every non-True truthy value.
            assert resp.status_code == 409, (non_true_force, resp.data)
            body = json.loads(resp.data)
            assert body["reason"] == "live_pods_present"

    @pytest.mark.parametrize(
        # (force_reason payload, expected status, expected reason)
        "payload,status,reason",
        [
            # None — no force_reason supplied is allowed.
            ({"force": True}, 200, None),
            # Valid string — accepted.
            ({"force": True, "force_reason": "ops override"}, 200, None),
            # Empty / whitespace string — coerced to None (treated as
            # "not provided"), still accepted.
            ({"force": True, "force_reason": ""}, 200, None),
            ({"force": True, "force_reason": "   "}, 200, None),
            # Non-string — 400 with invalid_force_reason.
            ({"force": True, "force_reason": 123}, 400, "invalid_force_reason"),
            ({"force": True, "force_reason": ["nope"]}, 400, "invalid_force_reason"),
        ],
        ids=[
            "none",
            "valid-string",
            "empty-string",
            "whitespace-string",
            "non-string-int",
            "non-string-list",
        ],
    )
    def test_force_reason_validation_paths(self, client, payload, status, reason):
        """``force_reason`` validation has three branches that the unit
        tier doesn't exercise:

        1. ``None`` / missing — allowed.
        2. Non-string — 400 ``invalid_force_reason``.
        3. Empty / whitespace string — coerced to ``None`` server-side.

        A regression that swapped the type check for ``str(...)``
        coercion would pass the route through with bogus reasons; a
        regression that rejected None would break the documented
        "force without reason" path. Parametrize all three to lock the
        contract.
        """
        pipeline = _failed_pipeline()
        spawner = _stub_spawner(
            ContainerInfo(
                container_id="wedged",
                container_name="egg-stuck",
                status=ContainerStatus.RUNNING,
            )
        )
        with _patched_route() as m:
            _setup_route_mocks(m.get_repo, m.resolve, pipeline)
            m.spawner.return_value = spawner

            resp = client.post("/api/v1/pipelines/issue-2420/start", json=payload)
            assert resp.status_code == status, (payload, resp.data)
            if reason is not None:
                body = json.loads(resp.data)
                assert body["reason"] == reason


class TestLivePodGuardAwaitingHumanRequestChanges:
    """The AWAITING_HUMAN → request_changes branch also resets phase
    state (issue body explicitly flags it). End-to-end exercise here so
    a route refactor that re-orders the guard placement is caught.
    """

    def test_request_changes_with_live_pods_is_blocked(self, client):
        from models import DecisionStatus, HITLDecision

        pipeline = Pipeline(
            id="issue-2420",
            issue_number=2420,
            repo="owner/repo",
            branch="egg/issue-2420/work",
            base_branch="main",
            mode="issue",
            status=PipelineStatus.AWAITING_HUMAN,
            current_phase=PipelinePhase.REFINE,
        )
        exec_ = pipeline.get_phase_execution(PipelinePhase.REFINE)
        exec_.status = PipelineStatus.COMPLETE
        exec_.completed_at = datetime.now(UTC)
        exec_.agents = [AgentExecution(role="coder", container_id="old")]
        exec_.containers = [
            ContainerInfo(
                container_id="old",
                container_name="egg-coder",
                status=ContainerStatus.RUNNING,
            )
        ]
        # Resolved decision asking for changes.
        pipeline.decisions.append(
            HITLDecision(
                id="d-1",
                question="Refine phase gate",
                decision_type="phase_gate",
                status=DecisionStatus.RESOLVED,
                resolution='{"action": "request_changes", "feedback": "fix tests"}',
                created_at=datetime.now(UTC),
                resolved_at=datetime.now(UTC),
            )
        )
        spawner = _stub_spawner(
            ContainerInfo(
                container_id="live-pod",
                container_name="egg-coder",
                status=ContainerStatus.RUNNING,
            )
        )
        with _patched_route() as m:
            _setup_route_mocks(m.get_repo, m.resolve, pipeline)
            m.spawner.return_value = spawner

            resp = client.post("/api/v1/pipelines/issue-2420/start")
            assert resp.status_code == 409, resp.data
            body = json.loads(resp.data)
            assert body["reason"] == "live_pods_present"
            # Phase must NOT have been reset.
            assert exec_.status == PipelineStatus.COMPLETE
            assert exec_.agents != []
            m.run.assert_not_called()
