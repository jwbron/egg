"""``validate_network_isolation`` — throwaway-probe Job isolation check (#3312).

Spawns a short-lived, secret-free probe Job in the agents namespace, waits
for it to terminate, parses its JSON verdict, and cleans the Job up. The
probe lifecycle helpers (`_submit_probe_job`, `_wait_for_probe_pod`,
`_read_probe_log`, `_delete_probe_job`) and the CNI detector are reached via
``_pkg`` so the route tests' ``patch("routes.deployment.<seam>")`` stubs stay
effective.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from typing import Any

import routes.deployment as _pkg
from flask import Response, jsonify, request

from . import logger
from ._runtime import _not_available_on_runtime

# Kubernetes label values must match this regex (RFC 1123-ish) or the
# apiserver rejects Job creation with a 422.
_K8S_LABEL_VALUE_RE = re.compile(r"^[a-z0-9A-Z]([-._a-z0-9A-Z]{0,61}[a-z0-9A-Z])?$")

PROBE_COMMAND_TEMPLATE = r"""
set -u
gateway_url="${GATEWAY_URL:-http://gateway.egg-system.svc.cluster.local:9848}" # noqa: EGG002
orchestrator_url="${EGG_ORCHESTRATOR_URL:-http://orchestrator.egg-system.svc.cluster.local:9849}"

probe() {
  local url="$1"
  curl --silent --max-time 3 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true
}

gw=$(probe "$gateway_url/api/v1/health")
internet=$(probe "https://example.com/")
orch=$(probe "$orchestrator_url/api/v1/health")
peer=$(probe "http://1.2.3.4:80/")

python3 - <<PY
import json
print(json.dumps({
    "gateway_reachable": "$gw".startswith("2") or "$gw".startswith("3"),
    "internet_blocked": "$internet" == "000",
    "agent_pods_unreachable": "$peer" == "000",
    # allow-agent-to-orchestrator (k8s/base/network-policies.yaml)
    # deliberately permits agent->orchestrator:9849 so agents can
    # heartbeat. So on a correctly-configured cluster this is True;
    # False is the regression signal (heartbeat path is broken). The
    # field was previously named orchestrator_direct_blocked with
    # inverted polarity, which read backwards from intent (#2652).
    # NOTE: this heredoc is unquoted (<<PY, not <<'PY') so the shell
    # performs command substitution on backticks. Do not introduce
    # backticks anywhere in the body — they will execute under sh
    # before python3 ever sees the source.
    "orchestrator_api_reachable": "$orch".startswith("2") or "$orch".startswith("3"),
    "raw": {
        "gateway_status": "$gw",
        "internet_status": "$internet",
        "orchestrator_status": "$orch",
        "peer_status": "$peer",
    },
}))
PY
"""


def _build_probe_env() -> dict[str, str]:
    """Build the env dict for the throwaway probe Job.

    Explicitly omits secrets: no lifecycle secret, no session token,
    no gateway bearer.  Uses ``PROTECTED_ENV_KEYS`` from ``redaction``
    as the single source of truth for the denylist.
    """
    from redaction import PROTECTED_ENV_KEYS as _PROTECTED_ENV_KEYS

    # We only expose the two URLs the probe script needs. Both happen to
    # also appear in _PROTECTED_ENV_KEYS (they're locked against agent
    # override in production); here the probe legitimately needs them.
    # All other environment is discarded.
    safe: dict[str, str] = {
        "GATEWAY_URL": os.environ.get("GATEWAY_URL", ""),
        "EGG_ORCHESTRATOR_URL": os.environ.get("EGG_ORCHESTRATOR_URL", ""),
    }
    # Double-check: nothing else sensitive sneaks in.
    for key in list(safe.keys()):
        if key in _PROTECTED_ENV_KEYS and key not in {
            "GATEWAY_URL",
            "EGG_ORCHESTRATOR_URL",
        }:
            safe.pop(key, None)
    return safe


def _build_probe_job_manifest(
    pipeline_id: str,
    role: str,
    probe_id: str,
    image: str,
) -> dict[str, Any]:
    """Construct the V1Job body for the isolation probe as a plain dict.

    Returning a dict rather than a ``V1Job`` keeps the function unit-testable
    without depending on the kubernetes SDK.  The caller converts to a
    V1 object when submitting.
    """
    env = _build_probe_env()
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"egg-probe-{probe_id}",
            "labels": {
                "app.kubernetes.io/component": "agent",
                "egg.probe": "true",
                "egg.io/probe-id": probe_id,
                "egg.pipeline.id": pipeline_id,
                "egg.agent.role": role,
            },
        },
        "spec": {
            # 0 raced with _wait_for_probe_pod's 1s poll: the Job could
            # complete and be GC'd by the TTL-after-finished controller
            # before the next poll observed the pod's terminal phase,
            # leaving the wait loop scanning an empty list until its 75s
            # ceiling (seen as the bimodal ~10s-or-75s distribution in
            # https://github.com/jwbron/egg/actions/runs/25817353877).
            # 30s guarantees the wait loop sees Succeeded/Failed and
            # _read_probe_log still has a pod to read from; the route's
            # try/finally _delete_probe_job remains the primary cleanup
            # path, so this only extends lifetime when the route crashed
            # before reaching finally.
            "ttlSecondsAfterFinished": 30,
            "activeDeadlineSeconds": 30,
            "backoffLimit": 0,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/component": "agent",
                        "egg.probe": "true",
                        "egg.io/probe-id": probe_id,
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "name": "probe",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-c", PROBE_COMMAND_TEMPLATE],
                            "env": [{"name": k, "value": v} for k, v in env.items()],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }


def _submit_probe_job(k8s: Any, namespace: str, probe_id: str, manifest: dict[str, Any]) -> None:
    from kubernetes import client as k8s_client_pkg

    body = k8s_client_pkg.V1Job(
        api_version=manifest["apiVersion"],
        kind=manifest["kind"],
        metadata=k8s_client_pkg.V1ObjectMeta(
            name=manifest["metadata"]["name"],
            labels=manifest["metadata"]["labels"],
        ),
        spec=k8s_client_pkg.V1JobSpec(
            ttl_seconds_after_finished=manifest["spec"]["ttlSecondsAfterFinished"],
            active_deadline_seconds=manifest["spec"]["activeDeadlineSeconds"],
            backoff_limit=manifest["spec"]["backoffLimit"],
            template=k8s_client_pkg.V1PodTemplateSpec(
                metadata=k8s_client_pkg.V1ObjectMeta(
                    labels=manifest["spec"]["template"]["metadata"]["labels"],
                ),
                spec=k8s_client_pkg.V1PodSpec(
                    restart_policy="Never",
                    automount_service_account_token=False,
                    containers=[
                        k8s_client_pkg.V1Container(
                            name="probe",
                            image=manifest["spec"]["template"]["spec"]["containers"][0]["image"],
                            image_pull_policy="IfNotPresent",
                            command=manifest["spec"]["template"]["spec"]["containers"][0][
                                "command"
                            ],
                            env=[
                                k8s_client_pkg.V1EnvVar(name=e["name"], value=e["value"])
                                for e in manifest["spec"]["template"]["spec"]["containers"][0][
                                    "env"
                                ]
                            ],
                            security_context=k8s_client_pkg.V1SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s_client_pkg.V1Capabilities(drop=["ALL"]),
                            ),
                        )
                    ],
                ),
            ),
        ),
    )
    k8s.batch_api.create_namespaced_job(namespace=namespace, body=body)


def _wait_for_probe_pod(k8s: Any, namespace: str, probe_id: str, *, timeout: float) -> Any:
    """Return the probe pod once it is Succeeded/Failed or None on timeout."""
    selector = f"egg.io/probe-id={probe_id}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pods = k8s.core_api.list_namespaced_pod(namespace=namespace, label_selector=selector)
        except Exception:
            time.sleep(1.0)
            continue
        for pod in getattr(pods, "items", []) or []:
            phase = (getattr(pod, "status", None) and pod.status.phase) or ""
            if phase in {"Succeeded", "Failed"}:
                return pod
        time.sleep(1.0)
    return None


def _read_probe_log(k8s: Any, namespace: str, pod_name: str) -> str:
    # The probe writes JSON to stdout. The kubernetes-python client's
    # ApiClient.deserialize() runs json.loads() on every response body
    # before coercing to the declared response_type, so a JSON-shaped
    # pod log gets parsed to a dict and then str()'d back, yielding the
    # Python dict repr (single quotes, ``True``) instead of the
    # original JSON. _preload_content=False bypasses that path and
    # returns the urllib3 HTTPResponse so we can decode the raw bytes.
    #
    # With ``_preload_content=False`` the actual network read happens at
    # ``.data`` access (urllib3 reads-to-EOF lazily and caches), so the
    # ``try/except`` must wrap the ``.data`` access too — otherwise a
    # mid-stream connection reset or malformed transfer-encoding would
    # propagate up and 500 the route handler.
    try:
        raw = k8s.core_api.read_namespaced_pod_log(
            name=pod_name, namespace=namespace, _preload_content=False
        )
        if raw is None:
            return ""
        data = getattr(raw, "data", raw)
    except Exception as exc:
        logger.warning("probe log read failed", pod=pod_name, error=str(exc))
        return ""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _delete_probe_job(k8s: Any, namespace: str, probe_id: str) -> None:
    """Best-effort cleanup. Never raises."""
    name = f"egg-probe-{probe_id}"
    try:
        from kubernetes import client as k8s_client_pkg

        k8s.batch_api.delete_namespaced_job(
            name=name,
            namespace=namespace,
            body=k8s_client_pkg.V1DeleteOptions(
                propagation_policy="Background", grace_period_seconds=0
            ),
        )
    except Exception as exc:
        logger.info("probe job cleanup skipped", probe=name, error=str(exc))


def _parse_probe_output(raw_log: str) -> dict[str, Any]:
    """Extract the JSON emitted by the probe pod.

    The probe script prints a single JSON object.  Log drivers
    sometimes add a trailing newline; tolerate that.
    """
    import json

    if not raw_log:
        return {"error": "no_probe_output"}
    for line in reversed(raw_log.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"error": "probe_output_unparseable", "raw": raw_log[-512:]}


def validate_network_isolation() -> tuple[Response, int]:
    """Spawn a throwaway probe Job and report isolation results."""
    if _pkg._current_runtime() != "kubernetes":
        return _not_available_on_runtime()

    body = request.get_json(silent=True) or {}
    pipeline_id = str(body.get("pipeline_id") or "manual")
    role = str(body.get("role") or "coder")
    namespace = os.environ.get("EGG_AGENTS_NAMESPACE", "egg-agents")

    # Guard against invalid K8s label values.  Labels must match
    # ^[a-z0-9A-Z]([-._a-z0-9A-Z]{0,61}[a-z0-9A-Z])?$ — failing Job
    # creation otherwise returns an opaque 400 from the apiserver.
    if not _K8S_LABEL_VALUE_RE.match(pipeline_id):
        return (
            jsonify(
                {
                    "success": False,
                    "message": (
                        "pipeline_id is not a valid Kubernetes label value "
                        "(must match [a-z0-9A-Z]([-._a-z0-9A-Z]{0,61}[a-z0-9A-Z])?)"
                    ),
                }
            ),
            400,
        )
    if not _K8S_LABEL_VALUE_RE.match(role):
        return (
            jsonify(
                {
                    "success": False,
                    "message": "role is not a valid Kubernetes label value",
                }
            ),
            400,
        )

    try:
        from kubernetes_client import get_kubernetes_client
    except Exception as exc:
        return jsonify({"success": False, "message": f"kubernetes unavailable: {exc}"}), 503

    try:
        k8s = get_kubernetes_client()
    except Exception as exc:
        return jsonify({"success": False, "message": f"kubernetes init failed: {exc}"}), 503

    # CNI gating per DEP-3: refuse to run the probe when enforcement is
    # not detected so we don't return misleading results.
    _cni, enforcement = _pkg._detect_cni(k8s)
    if not enforcement:
        return (
            jsonify(
                {
                    "success": True,
                    "data": {
                        "error": "network_policy_enforcement_not_detected",
                        "cni": _cni,
                    },
                }
            ),
            200,
        )

    probe_id = uuid.uuid4().hex[:12]
    image = os.environ.get("EGG_SANDBOX_IMAGE", "egg:latest")
    manifest = _build_probe_job_manifest(
        pipeline_id=pipeline_id, role=role, probe_id=probe_id, image=image
    )

    try:
        _pkg._submit_probe_job(k8s, namespace, probe_id, manifest)
    except Exception as exc:
        logger.error("probe submit failed", error=str(exc))
        return jsonify({"success": False, "message": f"probe submit failed: {exc}"}), 500

    try:
        # 30s was too tight: probe-pod scheduling on the k3s integration
        # cluster intermittently exceeded the deadline. 75s sits under the
        # require_lifecycle_secret route's 90s HTTP-timeout ceiling.
        pod = _pkg._wait_for_probe_pod(k8s, namespace, probe_id, timeout=75.0)
        if pod is None:
            return (
                jsonify(
                    {
                        "success": True,
                        "data": {
                            "error": "probe_timeout",
                            "probe_id": probe_id,
                        },
                    }
                ),
                200,
            )

        pod_name = (getattr(pod, "metadata", None) and pod.metadata.name) or ""
        log = _pkg._read_probe_log(k8s, namespace, pod_name)
        parsed = _parse_probe_output(log)

        return (
            jsonify(
                {
                    "success": True,
                    "data": {
                        "probe_id": probe_id,
                        "namespace": namespace,
                        "result": parsed,
                    },
                }
            ),
            200,
        )
    finally:
        _pkg._delete_probe_job(k8s, namespace, probe_id)
