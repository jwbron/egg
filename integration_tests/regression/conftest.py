"""Shared helpers for ``integration_tests/regression/`` (issue #2632).

These k3s regression guards pin invariants we've regressed historically
(slice spawn env threading from #2428, slice restart branch ref from
the #2410/#2428 follow-ups). They drive the real ``KubernetesSpawner``
against the locally-deployed egg stack and read pod specs back with
``kubectl get pod -o yaml``.

The fixtures here intentionally pick spawn parameters that do NOT
require a populated gateway test-repo: roles in
``_ROLES_WITHOUT_WORKTREE`` and ``repos=[]``. The env-threading and
slice-id-threading code paths in ``kubernetes_spawner.py`` are
role-independent (see lines 754-774 of that file at the time of
writing), so a worktree-free role exercises the same seam the
``coder`` regression in #2428 fired through. This keeps the test
green on a fresh CI runner where ``$HOME/repos`` is empty.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

# Ensure ``orchestrator/`` and ``shared/`` are importable so the test
# module can drive ``KubernetesSpawner`` directly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (_PROJECT_ROOT / "orchestrator", _PROJECT_ROOT / "shared", _PROJECT_ROOT):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


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

    # ``EGG_LAUNCHER_SECRET`` overrides any value in env; GatewayClient
    # reads it via os.environ when ``launcher_secret`` is omitted.
    prev_secret = os.environ.get("EGG_LAUNCHER_SECRET")
    os.environ["EGG_LAUNCHER_SECRET"] = egg_stack.launcher_secret
    try:
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
    finally:
        if prev_secret is None:
            os.environ.pop("EGG_LAUNCHER_SECRET", None)
        else:
            os.environ["EGG_LAUNCHER_SECRET"] = prev_secret
