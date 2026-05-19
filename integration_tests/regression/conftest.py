"""Shared fixtures for ``integration_tests/regression/``.

This directory hosts five orthogonal regression tiers:

* **Pipeline recovery / unpushed-commit salvage** (issue #2633) — sits
  between the unit-tier (``orchestrator/tests/``) and the k3s-tier
  (``integration_tests/test_*.py`` against a live cluster): imports
  the real ``pipelines_bp`` blueprint and exercises routes through an
  in-process Flask test client (same shape as
  ``integration_tests/test_babysit_pr/``), uses the real
  ``agent_salvage`` module, the real git binary, and real worktree
  directories on ``tmp_path``. Only the gateway HTTP client and the
  spawner backend (the network/k8s boundaries) are stubbed out. Catches
  regressions in the *wiring* between the route layer, the salvage
  helpers, and the git plumbing — exactly the seams that the per-module
  unit tests in ``orchestrator/tests/`` mock out.

* **Message store + event bus routing** (issue #2640) — exercises the
  load-bearing seams between the orchestrator's Flask blueprints, the
  inter-agent message store (in-memory + Redis Streams), and the
  in-process ``EventBus``. Real ``Flask`` blueprint and real
  ``EventBus`` so the routing path under test is the same one
  production runs. Dual-backend parametrization mirrors the AC pattern
  from ``orchestrator/tests/test_pipelines_status_wait_route.py``:
  every test that touches the message store runs against both
  ``MessageStore`` (in-memory) and ``RedisMessageStore`` backed by
  ``fakeredis.FakeRedis`` so a regression in either backend surfaces.

* **HITL HTTP round-trip helpers** (issues #2474, #2634) — pin the
  ``/api/v1/pipelines/<id>/decisions/...`` HTTP surface against the
  locally-deployed egg stack. The tier uses
  :func:`deterministic_pipeline_id` to derive a syntactically valid
  ``pipeline-{8 hex chars}`` id from each test's pytest nodeid so
  re-runs reuse the same id (and 404 assertions don't silently turn
  into 400 ``InvalidPipelineIdError`` ones), and
  :func:`lifecycle_secret` / :func:`lifecycle_bearer` to read the
  orchestrator's lifecycle bearer from
  ``gateway-secrets/lifecycle-secret`` in ``egg-system``. Happy-path
  tests skip cleanly when the secret is unreachable; auth-rejection
  tests don't need it.

* **BRC consensus** (issue #2635) — exercises ``PeerConsensusTracker``
  and the timeout-handler entry points in-process. Does NOT require
  k3s and never calls into the ``egg_stack`` fixture — drives the
  orchestrator's Python API at the integration boundary (the shape
  #2474 recommends after the ScriptedProvider pod-injection avenue
  was ruled out).

* **k3s slice-spawn / restart guards** (issue #2632) — drives the real
  ``KubernetesSpawner`` against the locally-deployed egg stack and
  reads pod specs back with ``kubectl get pod -o yaml``. Pins
  invariants we've regressed historically (slice spawn env threading
  from #2428, slice restart branch ref from the #2410/#2428 follow-ups).
  The k3s fixtures only fire when a test takes the ``spawner`` /
  ``egg_stack`` fixtures, and intentionally pick spawn parameters that
  do NOT require a populated gateway test-repo: roles in
  ``_ROLES_WITHOUT_WORKTREE`` and ``repos=[]``. The env-threading and
  slice-id-threading code paths in ``kubernetes_spawner.py`` are
  role-independent (see lines 754-774 of that file at the time of
  writing), so a worktree-free role exercises the same seam the
  ``coder`` regression in #2428 fired through. This keeps the test
  green on a fresh CI runner where ``$HOME/repos`` is empty.

All five tiers are marked ``integration`` (via module-level
``pytestmark`` in each test file) and run under
``make test-integration`` / the ``Test / integration`` CI required
check. The k3s fixtures only fire when a test takes the ``spawner`` /
``egg_stack`` fixtures; the message-bus tests use ``fakeredis`` and
``unittest.mock.patch`` for the pipeline state-store and the inner
context-PR hook; the BRC fixtures are either autouse (tracker
registry) or opt-in; the HITL fixtures are opt-in via
``lifecycle_bearer`` / ``regression_pipeline_id``.

Plain helper functions (``make_tracker``, ``propose_payload``,
``filter_events``, the git/worktree builders, …) live in
``_helpers.py``; pytest's conftest discovery only surfaces fixtures
cross-module, so helpers usable in ``import`` statements have to live
next door.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# sys.path setup: include _REGRESSION_DIR so ``_helpers`` is importable,
# plus orchestrator + shared + project root so test modules can import
# the orchestrator's internal modules (events, message_store,
# redis_message_store, routes.pipelines, peer_consensus, review_graph).
# Mirrors integration_tests/test_babysit_pr/conftest.py and stubs the
# kubernetes / docker SDKs so the blueprint loads without those packages
# installed.
_REGRESSION_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _REGRESSION_DIR.parent.parent
for _p in (
    _REGRESSION_DIR,
    _PROJECT_ROOT / "orchestrator",
    _PROJECT_ROOT / "shared",
    _PROJECT_ROOT,
):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

import pytest  # noqa: E402
from _helpers import EventFilter, filter_events  # noqa: E402
from events import Event, get_event_bus  # noqa: E402
from peer_consensus import _trackers as _global_trackers  # noqa: E402
from peer_consensus import _trackers_lock as _global_trackers_lock  # noqa: E402
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph  # noqa: E402

_TEST_LIFECYCLE_SECRET = "test-lifecycle-secret-regression"


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``no_lifecycle_auth`` opt-out marker."""
    config.addinivalue_line(
        "markers",
        (
            "no_lifecycle_auth: opt out of the autouse Authorization-header "
            "injection (use for tests that want to assert the route's own "
            "auth handling, e.g. rejecting requests missing the header)."
        ),
    )


@pytest.fixture(autouse=True)
def _set_lifecycle_secret_env():
    """Set ``EGG_LIFECYCLE_SECRET`` so lifecycle-gated routes don't 503.

    Function-scoped per the same reasoning as
    ``test_babysit_pr/conftest.py``: a session-scoped autouse would leak
    the env var to any sibling suite that happens to read it directly.
    """
    overrides = {
        "EGG_LIFECYCLE_SECRET": _TEST_LIFECYCLE_SECRET,
        # In-process Flask client never talks to a real gateway; short-
        # circuit the gateway-readiness gate that ``create_pipeline``
        # otherwise waits 60s for.
        "EGG_GATEWAY_READY_TIMEOUT_SECONDS": "0",
    }
    prev = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        os.environ[k] = v
    yield
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _inject_lifecycle_auth(monkeypatch, request):
    """Auto-attach ``Authorization: Bearer …`` to every FlaskClient request.

    Tests can opt out per-test with ``@pytest.mark.no_lifecycle_auth``
    so the route's own auth-rejection paths can be exercised without
    the wrapper transparently injecting the header. The per-call
    ``_lifecycle_auth=False`` kwarg is still honored when the wrapper
    is active.
    """
    if request.node.get_closest_marker("no_lifecycle_auth") is not None:
        yield
        return

    try:
        from flask.testing import FlaskClient
    except ImportError:
        yield
        return

    original_open = FlaskClient.open

    def wrapper(self, *args, **kwargs):
        if kwargs.pop("_lifecycle_auth", True):
            headers = kwargs.get("headers")
            auth_header = ("Authorization", f"Bearer {_TEST_LIFECYCLE_SECRET}")
            if headers is None:
                kwargs["headers"] = dict([auth_header])
            else:
                existing: dict[str, object] = {}
                if hasattr(headers, "items"):
                    existing = dict(headers.items())
                elif isinstance(headers, (list, tuple)):
                    existing = dict(headers)
                if not any(k.lower() == "authorization" for k in existing):
                    if isinstance(headers, dict):
                        kwargs["headers"] = {**headers, auth_header[0]: auth_header[1]}
                    else:
                        kwargs["headers"] = list(headers) + [auth_header]
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(FlaskClient, "open", wrapper)
    yield


# ---------------------------------------------------------------------------
# Message-bus routing fixtures (#2640)
# ---------------------------------------------------------------------------
#
# Lifecycle-secret env injection + ``Authorization: Bearer …`` header
# wrapping live in the pipeline-recovery section above (the
# ``_set_lifecycle_secret_env`` / ``_inject_lifecycle_auth`` autouse
# fixtures and ``_TEST_LIFECYCLE_SECRET`` constant). Both tiers need
# the same machinery; the function-scoped ``monkeypatch`` /
# ``os.environ`` override is deliberate so a mixed-tree pytest session
# doesn't race the orchestrator's own session-scoped lifecycle-secret
# fixture in ``orchestrator/tests/conftest.py``.


@pytest.fixture
def lifecycle_auth_headers() -> dict[str, str]:
    """Valid ``Authorization`` header for lifecycle-control endpoints."""
    return {"Authorization": f"Bearer {_TEST_LIFECYCLE_SECRET}"}


# ---------------------------------------------------------------------------
# HITL HTTP round-trip helpers (#2474, #2634)
# ---------------------------------------------------------------------------

_LIFECYCLE_SECRET_NAMESPACE = "egg-system"
_LIFECYCLE_SECRET_NAME = "gateway-secrets"
_LIFECYCLE_SECRET_KEY = "lifecycle-secret"


def deterministic_pipeline_id(test_nodeid: str) -> str:
    """Return a stable, **syntactically valid** pipeline id from a nodeid.

    The id matches the ``pipeline-{8 hex chars}`` arm of
    ``state_store.PIPELINE_ID_PATTERN`` — any other shape (e.g. the
    ``regression-<hex>`` shape from #2474's recovered attempt) trips
    ``InvalidPipelineIdError`` → 400 before the 404 path runs, masking
    "pipeline not found" assertions.

    SHA-1 is used as a stable digest, not a cryptographic hash, so the
    Bandit warning is suppressed.
    """
    digest = hashlib.sha1(test_nodeid.encode("utf-8")).hexdigest()  # noqa: S324
    return f"pipeline-{digest[:8]}"


def lifecycle_secret() -> str | None:
    """Return the orchestrator's ``EGG_LIFECYCLE_SECRET`` if reachable.

    Reads ``gateway-secrets/lifecycle-secret`` from the ``egg-system``
    namespace. Returns ``None`` if kubectl is missing, the secret is
    absent, or the value cannot be decoded — callers should
    ``pytest.skip`` rather than fail in that case so happy-path tests
    are skipped cleanly when run by a developer without read access on
    the secret (CI has it).
    """
    cmd = [
        "kubectl",
        "-n",
        _LIFECYCLE_SECRET_NAMESPACE,
        "get",
        "secret",
        _LIFECYCLE_SECRET_NAME,
        "-o",
        f"jsonpath={{.data.{_LIFECYCLE_SECRET_KEY}}}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        # ``.strip()`` because ``kubectl create secret --from-file`` keeps
        # every byte of the source file including the trailing newline;
        # a ``\n`` inside ``f"Bearer {secret}"`` is rejected by
        # ``http.client.putheader``.
        return base64.b64decode(result.stdout).decode("utf-8").strip()
    except ValueError, UnicodeDecodeError:
        return None


@pytest.fixture(scope="session")
def lifecycle_bearer() -> str:
    """Return an ``Authorization: Bearer ...`` value or skip the test.

    Used by happy-path tests that need to call
    ``@require_lifecycle_secret`` endpoints. When the secret is not
    reachable from the test runner (developer laptop without rbac on
    the secret) the test is skipped, not failed.

    Session-scoped: the lifecycle secret is a singleton per cluster, so
    we read it once per pytest session instead of per parametrized
    case. ``TestHitlResolvePayloadEdgeCases`` alone fans out to 7
    cases, each of which would otherwise re-shell-out to ``kubectl``
    with a 15-second timeout — tens of seconds of pure subprocess
    overhead per run on a slow cluster.
    """
    secret = lifecycle_secret()
    if not secret:
        pytest.skip(
            "lifecycle-secret not readable from gateway-secrets in "
            f"namespace {_LIFECYCLE_SECRET_NAMESPACE} — happy-path "
            "lifecycle endpoint tests skipped"
        )
    return f"Bearer {secret}"


@pytest.fixture
def regression_pipeline_id(request: pytest.FixtureRequest) -> str:
    """Stable pipeline id derived from the calling test's pytest nodeid."""
    return deterministic_pipeline_id(request.node.nodeid)


# ---------------------------------------------------------------------------
# k3s slice-spawn helpers (#2632)
# ---------------------------------------------------------------------------


def kubectl_get_pod_yaml(
    namespace: str,
    label_selector: str,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Return the first pod matching ``label_selector`` as a parsed dict.

    Polls ``kubectl get pods -l <selector>`` until at least one pod
    exists or the timeout expires. The pod spec (including the env
    var list) is populated as soon as the Job's pod template is
    materialized — we do NOT wait for ``Running`` because a session
    with a token-only gateway registration will still produce a pod
    spec whether or not its image entrypoint succeeds.

    Args:
        namespace: k8s namespace.
        label_selector: passed verbatim to ``kubectl -l``.
        timeout_s: pod-appearance deadline.

    Raises:
        AssertionError: if no pod appears within ``timeout_s``.
    """
    deadline = time.monotonic() + timeout_s
    last_err: str | None = None
    while time.monotonic() < deadline:
        proc = subprocess.run(
            [
                "kubectl",
                "-n",
                namespace,
                "get",
                "pods",
                "-l",
                label_selector,
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            last_err = proc.stderr
            time.sleep(1)
            continue
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            last_err = f"{e}: {proc.stdout[:200]}"
            time.sleep(1)
            continue
        items = data.get("items") or []
        if items:
            return items[0]
        time.sleep(1)
    raise AssertionError(
        f"No pod matched selector {label_selector!r} in {namespace} "
        f"within {timeout_s}s (last error: {last_err})"
    )


def env_from_pod(pod: dict[str, Any]) -> dict[str, str]:
    """Flatten the agent container's literal ``env`` list to a dict.

    Skips ``valueFrom`` entries — they don't have a literal value at
    the pod-spec level. ``EGG_BRANCH`` / ``EGG_SLICE_ID`` are always
    set as literals by ``KubernetesSpawner`` so this is sufficient
    for the invariants this directory pins.
    """
    containers = pod.get("spec", {}).get("containers") or []
    if not containers:
        raise AssertionError(f"Pod has no containers: {pod.get('metadata', {}).get('name')}")
    out: dict[str, str] = {}
    for entry in containers[0].get("env") or []:
        if "value" in entry:
            out[entry["name"]] = entry["value"]
    return out


@pytest.fixture
def spawner(egg_stack: Any) -> Generator[Any]:
    """Yield a ``KubernetesSpawner`` bound to the test agent namespace.

    Uses the same launcher secret + gateway URL the rest of the
    integration suite discovers via ``egg_stack``. The spawner's
    ``KubernetesClient`` loads the local kubeconfig (the test process
    runs out-of-cluster).
    """
    try:
        from gateway_client import GatewayClient
        from kubernetes_client import KubernetesClient
        from kubernetes_spawner import KubernetesSpawner
    except ImportError as e:
        pytest.skip(f"Could not import orchestrator modules: {e}")

    # Pin GatewayClient at the discovered gateway URL. ``egg_stack``
    # already validated the gateway is reachable.
    gateway_url = egg_stack.gateway_url.rstrip("/")
    # ``gateway_url`` is ``http://<host>:<port>``; split it for the
    # client's host/port kwargs.
    parsed = gateway_url.removeprefix("http://").removeprefix("https://")
    if ":" in parsed:
        host, port_s = parsed.rsplit(":", 1)
        port = int(port_s)
    else:
        host = parsed
        port = egg_stack.gateway_port

    # ``launcher_secret`` is passed explicitly to ``GatewayClient`` —
    # the env-var fallback inside the client never fires here, so no
    # ``os.environ`` mutation is needed.
    gateway = GatewayClient(
        gateway_host=host,
        gateway_port=port,
        launcher_secret=egg_stack.launcher_secret,
    )
    k8s = KubernetesClient(namespace=egg_stack.isolated_network)
    s = KubernetesSpawner(
        k8s_client=k8s,
        gateway_client=gateway,
        namespace=egg_stack.isolated_network,
    )
    yield s


# ---------------------------------------------------------------------------
# BRC consensus fixtures (#2635)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_tracker_registry() -> Generator[None]:
    """Snapshot + restore the global ``_trackers`` registry around each test.

    ``create_peer_consensus_tracker`` stores trackers in a module-level
    dict keyed by pipeline_id.  Without cleanup these survive across
    tests and a later test's ``get_peer_consensus_tracker(same_id)``
    can find leftover state from a previous test (the surfaced gap
    in #2635 PR review).  Snapshot+restore is safer than ``clear()``
    in case a parent test suite seeded trackers we shouldn't remove.
    """
    with _global_trackers_lock:
        snapshot = dict(_global_trackers)
    try:
        yield
    finally:
        with _global_trackers_lock:
            _global_trackers.clear()
            _global_trackers.update(snapshot)


@pytest.fixture
def app():
    """A Flask app with the real pipelines blueprint mounted."""
    from flask import Flask
    from routes.pipelines import pipelines_bp

    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def fake_gateway() -> MagicMock:
    """A gateway stub whose ``push_worktree_branch`` records every call.

    Shared across the salvage regression tests so the stub shape stays
    uniform — every test asserts against ``push_worktree_branch`` calls
    with the same kwargs contract.
    """
    from gateway_client import PushResult

    gw = MagicMock()
    gw.push_worktree_branch.return_value = PushResult(ok=True)
    return gw


@pytest.fixture
def event_capture() -> Generator[Callable[[], list[Event]]]:
    """Snapshot events published after the fixture is acquired.

    The orchestrator's event bus runs with ``async_delivery=True``,
    so handlers are dispatched on a worker thread — subscribing and
    immediately reading the buffer races the delivery loop and is
    flaky.  ``get_history()`` is updated **synchronously** inside the
    publish path's lock, so reading it gives a deterministic snapshot.

    The fixture captures the bus's sequence-tip before the test runs
    and returns a callable that yields only events appended since,
    isolating the test from events emitted by unrelated tests in the
    same session.

    Note: ``get_history()`` is bounded by ``EventBus._max_history``
    (default 100 — see ``orchestrator/events.py:154``).  All tests in
    this folder stay well under that bound between fixture entry and
    snapshot, but a future test that emits >100 events would lose its
    earliest ones to history eviction; widen ``max_history`` on the
    bus or capture more granularly if you anticipate larger volumes.
    """
    bus = get_event_bus()
    seq_before = bus.current_sequence()

    def snapshot() -> list[Event]:
        history = bus.get_history()  # newest first per the bus contract
        # Restore publish order and filter to events emitted during this test.
        return [e for e in reversed(history) if e.sequence > seq_before]

    yield snapshot


@pytest.fixture(name="filter_events")
def filter_events_fixture() -> EventFilter:
    """``filter_events`` as a fixture so tests can take it via injection.

    Exposed under the bare name ``filter_events`` so tests read naturally
    (``def test_x(self, event_capture, filter_events): ...``).  The
    underlying helper is also importable from ``_helpers`` for sites
    that don't want fixture injection.
    """
    return filter_events


@pytest.fixture
def single_reviewer_graph() -> ReviewGraph:
    """1 producer, 1 critical reviewer — the minimal BRC topology."""
    return ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])


@pytest.fixture
def two_reviewer_graph() -> ReviewGraph:
    """1 producer, 2 critical reviewers — exercises disagreement paths."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def advisory_blocker_graph() -> ReviewGraph:
    """1 producer, 1 critical + 1 advisory reviewer — timeout-handler triage path."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.ADVISORY),
        ]
    )


# ---------------------------------------------------------------------------
# Substrate parametrization (#2623 — Claude Code substrate spike, slice-1)
# ---------------------------------------------------------------------------
#
# The substrate fixture parametrizes a test across the two substrate
# implementations: ``"k3s"`` (the existing k3s-native stack) and
# ``"claude-code"`` (the in-process Claude Code substrate from #2623).
# Both legs run pure-Python in-process — neither dimension shells out
# to ``kubectl`` or spawns a real subagent. The k3s leg uses
# ``K3sSpawnerAdapter`` with a mocked ``create_concurrent_spawn_fn``;
# the claude-code leg uses ``ClaudeCodeSpawner`` with the Claude Code
# Agent-tool dispatch monkey-patched (the parent session is what would
# normally provide it).
#
# The claude-code dimension is skipped when running inside an
# in-sandbox-agent trust context (``EGG_AGENT_ROLE`` set), because the
# spike is for orchestrator-side execution, not for agents recursively
# spawning agents. Pinned per task-1-8 acceptance criterion.


def _in_sandbox_agent_context() -> bool:
    """Return True when the test runner is itself an egg sandbox agent.

    The orchestrator sets ``EGG_AGENT_ROLE`` on every spawned agent
    container; tests running inside that context should skip the
    claude-code substrate dimension because the in-process substrate is
    only meant to run from the parent Claude Code session, not from a
    sandboxed agent that's already been dispatched.
    """
    return bool(os.environ.get("EGG_AGENT_ROLE"))


@pytest.fixture(params=["k3s", "claude-code"])
def substrate(request: pytest.FixtureRequest) -> str:
    """Parametrize a test over both substrate implementations.

    Tests taking this fixture run twice (once per dimension). The
    claude-code dimension is skipped inside an egg sandbox-agent trust
    context (``EGG_AGENT_ROLE`` set) per task-1-8 acceptance criterion.

    The fixture returns the substrate name string. The test body is
    expected to call ``select_substrate({"EGG_SUBSTRATE": substrate})``
    (from ``orchestrator/substrate/__init__.py``) to obtain the wired
    bundle and exercise it.
    """
    if request.param == "claude-code" and _in_sandbox_agent_context():
        pytest.skip(
            "claude-code substrate dimension skipped inside an egg "
            "sandbox-agent context (EGG_AGENT_ROLE set) — the in-process "
            "substrate is for orchestrator-side execution only"
        )
    return request.param


__all__ = [
    # HITL HTTP round-trip helpers (#2474, #2634).
    "deterministic_pipeline_id",
    "lifecycle_bearer",
    "lifecycle_secret",
    "regression_pipeline_id",
    # k3s slice-spawn helpers (#2632). The ``spawner`` fixture is
    # consumed via pytest injection rather than a direct import, but is
    # listed here so the public surface mirrors what ``import *`` would
    # expose and IDE auto-imports / ``dir(conftest)`` stay honest.
    "env_from_pod",
    "kubectl_get_pod_yaml",
    "spawner",
    # BRC consensus fixtures (#2635). Listed for the same reason as
    # ``spawner`` — these are pytest-injected, not directly imported,
    # but belong in the public surface so ``dir(conftest)`` and ``import
    # *`` reflect the full set. ``_reset_tracker_registry`` is autouse
    # and ``filter_events`` is both the bare helper from ``_helpers`` and
    # the fixture name it's exposed under via ``name="filter_events"``.
    "_reset_tracker_registry",
    "advisory_blocker_graph",
    "event_capture",
    "filter_events",
    "single_reviewer_graph",
    "two_reviewer_graph",
    # Substrate parametrization (#2623 slice-1).
    "substrate",
]
