"""Sandboxed check-runner for the propose-time check gate (#3669).

The execution half of ``propose_check_gate``: how the repo's configured
checks are run against a proposed tree, and nothing about *when* a
proposal should be gated on the result. The gate module owns the
operator switches, the verdict ledger, and the rejection decision; this
one owns the Job.

The configured commands execute untrusted repo code (``pytest`` /
``mypy`` import the tree), so they must never run in the orchestrator
process. The runner is a one-shot Kubernetes Job, sandboxed exactly like
an agent pod: default-deny egress except gateway/DNS, git routed through
the gateway via a session token, no service-account token, all
capabilities dropped.

Job lifecycle follows the two established precedents — the
network-isolation probe (``routes.deployment._network_probe``) and the
per-slice green gate (``slice_green_gate``): one-shot Job
(``backoffLimit`` 0, ``restartPolicy`` Never), poll for a terminal pod
phase, read the log with ``_preload_content=False`` (the k8s client's
default path ``json.loads``-then-``str()``s JSON-shaped bodies,
corrupting the verdict), parse the sentinel line, delete the Job in
``finally``.

Two things are deliberately *not* copied from the green gate:

* **The deadline lives on the PodSpec** (#3622). The green gate sets
  ``spec.activeDeadlineSeconds``, which Kubernetes counts from the Job's
  ``startTime`` — before any pod is bound — so scheduling and image-pull
  latency come out of the check budget. Here the budget is
  ``spec.template.spec.activeDeadlineSeconds``, counted by the kubelet
  from pod start, with the Job-level field kept only as a strictly
  larger outer ceiling so a pod that never schedules cannot leak.
* **``_submit_runner_job`` copies every manifest field.** The green
  gate's equivalent hand-copies a fixed field list and silently drops
  anything it does not name, which is precisely how the pod-level
  deadline came to be missing there.

The runner checks out the **proposed SHA**, not a branch tip: the
producer named a tree and that is what must be verified. A SHA that
cannot be materialised is an infrastructure failure — the runner exits
non-zero with no verdict and the gate fails open — never a red the
producer cannot act on.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from egg_logging import get_logger
from slice_green_gate import (
    _INFRA_LINE_SIGNATURES,
    _INFRA_SUBSTRING_SIGNATURES,
    _read_runner_log,
)

if TYPE_CHECKING:
    from kubernetes_spawner import KubernetesSpawner

logger = get_logger("orchestrator.propose_check_runner")

# In-pod wall-clock budget for the checks. Larger than the green gate's
# 1800s because this gate runs the *full* suite, not the narrowed one.
TIMEOUT_ENV_VAR = "EGG_PROPOSE_CHECK_GATE_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 3600

# Extra wall-clock the orchestrator's wait loop and the Job's outer
# ceiling allow on top of the in-pod budget, to absorb scheduling and
# image-pull latency. Unlike the green gate (#3622), this grace is
# *additive* to the check budget rather than deducted from it: the
# kubelet counts the pod-level deadline from pod start, so the pod
# always gets its full budget however long it waited to be bound.
_POD_SCHEDULING_GRACE_SECONDS = 300

# Per-check output tail retained in the verdict. The gate truncates
# further for the rejection envelope; the untruncated output stays in
# the runner pod log, which the envelope names by pod and gate id.
_VERDICT_OUTPUT_TAIL_CHARS = 4000

VERDICT_SENTINEL = "EGG_PROPOSE_CHECK_VERDICT:"

_GATE_ID_LABEL = "egg.io/propose-check-id"
_JOB_NAME_PREFIX = "egg-proposecheck-"


def _gate_timeout_seconds() -> int:
    raw = os.environ.get(TIMEOUT_ENV_VAR, "")
    try:
        value = int(raw)
    except ValueError:
        # ``raw`` is always a ``str`` (``os.environ.get(..., "")``), so
        # ``int()`` can only raise ``ValueError`` here.
        return _DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_TIMEOUT_SECONDS


# The runner program, executed as ``python3 -c`` in the pod.
#
# Deliberate near-duplicate of ``slice_green_gate._RUNNER_PROGRAM``'s
# prelude: both are embedded source strings that cannot import from each
# other, and the two programs diverge past the prelude (this one detaches
# to an exact SHA and has no autofix/final-verification stage). The
# *classification vocabulary* is not duplicated — the signature lists are
# injected from ``slice_green_gate`` so the two gates cannot drift on
# what counts as infrastructure (#3621).
#
# Always exits 0 when the harness itself worked: the verdict, not the pod
# exit code, is the pass/fail channel, so a non-zero pod exit
# unambiguously means runner infrastructure failure (fail-open).
_RUNNER_PROGRAM = """
import json, os, shutil, subprocess, sys, time

checks = json.loads(os.environ["EGG_PROPOSE_CHECK_CHECKS"])
repo_dir = os.environ["EGG_PROPOSE_CHECK_REPO_DIR"]
target_sha = os.environ["EGG_PROPOSE_CHECK_COMMIT_SHA"]
tail = int(os.environ.get("EGG_PROPOSE_CHECK_OUTPUT_TAIL", "4000"))
infra_signatures = json.loads(os.environ.get("EGG_PROPOSE_CHECK_INFRA_SIGNATURES", "{}"))
infra_line_signatures = infra_signatures.get("line", [])
infra_substring_signatures = infra_signatures.get("substring", [])


def classify_infra(rc, out):
    # Identical semantics to slice_green_gate.classify_infra: SIGKILL is
    # the OOM killer (no test runner signals failure that way); the
    # git-wrapper signatures match whole-line so a check that merely
    # *prints* one mid-line (pytest assertion introspection of egg's own
    # gate tests) cannot fail its own red open; the kernel ENOSPC
    # strerror matches as a substring because it surfaces embedded.
    if rc in (-9, 137):
        return "check process died by SIGKILL (exit %s): OOM killer" % rc
    stripped_lines = None
    for sig in infra_line_signatures:
        if stripped_lines is None:
            stripped_lines = {ln.strip() for ln in out.splitlines()}
        if sig in stripped_lines:
            return sig
    for sig in infra_substring_signatures:
        if sig in out:
            return sig
    return None


def restore_prebuilt(target_dir):
    # Mirror sandbox.entrypoint._worktrees.restore_prebuilt_deps: copy
    # /opt/prebuilt-deps/<owner--repo>/* (the persist_dirs snapshot from
    # the repo's build_commands, e.g. .venv) into the mounted worktree,
    # skipping paths that already exist.
    base = os.environ.get("EGG_PROPOSE_CHECK_PREBUILT_BASE", "/opt/prebuilt-deps")
    name = os.path.basename(os.path.normpath(target_dir))
    if not os.path.isdir(base):
        return None

    def copy_if_missing(src, dst, **kwargs):
        if os.path.exists(dst) or os.path.islink(dst):
            return
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst, **kwargs)

    for entry in sorted(os.listdir(base)):
        if entry == "__egg_system_dirs__" or not entry.endswith("--" + name):
            continue
        shutil.copytree(
            os.path.join(base, entry),
            target_dir,
            copy_function=copy_if_missing,
            dirs_exist_ok=True,
            symlinks=False,
        )
        return entry
    return None


def run_cmd(command, cwd=None):
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=cwd or repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        return proc.returncode, proc.stdout or ""
    except Exception as exc:
        return -1, f"runner failed to execute command: {exc}"


def run_git(*args):
    # List form, never `bash -c`: ``target_sha`` reaches this program
    # from an agent-supplied propose payload. The orchestrator already
    # refuses a non-hex sha before the runner is spawned, but the shell
    # must not be the thing standing between a payload field and
    # execution — the configured check commands are the *only* strings
    # that get a shell here.
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        return proc.returncode, proc.stdout or ""
    except Exception as exc:
        return -1, f"runner failed to execute git: {exc}"


# Materialise the *proposed* tree, not a branch tip: the producer named
# a SHA and that is what must be checked. A failure here is
# infrastructure (the object is unreachable, the gateway git wrapper is
# down) — exit non-zero with no verdict so the orchestrator fails open
# rather than reporting a red the producer cannot act on.
rc, out = run_git("rev-parse", "--verify", "--quiet", target_sha + "^{commit}")
if rc != 0:
    rc, out = run_git("fetch", "--quiet", "origin", target_sha)
rc, out = run_git("checkout", "--detach", "--force", target_sha)
if rc != 0:
    print(
        "propose-check runner: could not check out proposed sha %s: %s"
        % (target_sha, out[-2000:]),
        file=sys.stderr,
    )
    sys.exit(1)

rc, head_out = run_git("rev-parse", "HEAD")
head_sha = head_out.strip() if rc == 0 else ""

try:
    restored = restore_prebuilt(repo_dir)
except Exception as exc:
    print(f"propose-check runner: prebuilt deps restore failed: {exc}", file=sys.stderr)
    sys.exit(1)
if os.environ.get("EGG_PROPOSE_CHECK_REQUIRE_PREBUILT") == "1" and restored is None:
    print(
        "propose-check runner: repo config declares persist_dirs but no prebuilt "
        "snapshot exists under /opt/prebuilt-deps — rebuild the sandbox image",
        file=sys.stderr,
    )
    sys.exit(1)

results = []
for check in checks:
    started = time.monotonic()
    rc, out = run_cmd(check["command"])
    results.append(
        {
            "name": check["name"],
            # The exact string that ran, recorded so a narrowed run is
            # visibly narrow in the attestation rather than silently
            # incomplete (#3669).
            "command": check["command"],
            "ok": rc == 0,
            "exit_code": rc,
            "output_tail": out[-tail:],
            "infra": classify_infra(rc, out) if rc != 0 else None,
            "duration_seconds": round(time.monotonic() - started, 1),
        }
    )

print(
    "EGG_PROPOSE_CHECK_VERDICT:"
    + json.dumps({"commit_sha": head_sha, "checks": results}),
    flush=True,
)
"""


def _repo_requires_prebuilt(repo: str) -> bool:
    """True when ``repo``'s build_commands persist a toolchain snapshot."""
    try:
        from config.repo_config import get_repo_build_commands
    except ImportError:
        try:
            from repo_config import get_repo_build_commands  # type: ignore[no-redef]
        except ImportError:
            return False
    try:
        return bool((get_repo_build_commands(repo) or {}).get("persist_dirs"))
    except Exception:  # noqa: BLE001 — config load is best-effort
        return False


# --------------------------------------------------------------------------
# Runner Job lifecycle
# --------------------------------------------------------------------------


def build_runner_job_manifest(
    *,
    gate_id: str,
    pipeline_id: str,
    commit_sha: str,
    image: str,
    checks: list[dict[str, str]],
    repo_mounts: dict[str, str],
    repo_dir: str,
    env: dict[str, str],
    timeout_seconds: int,
    host_uid: int,
    host_gid: int,
) -> dict[str, Any]:
    """Construct the check-runner V1Job body as a plain dict.

    Two deadlines, per #3622:

    * ``spec.template.spec.activeDeadlineSeconds`` — the **PodSpec**
      field, counted by the kubelet from pod start. This is the check
      budget, and it is not eroded by scheduling or image-pull latency.
    * ``spec.activeDeadlineSeconds`` — the Job-level ceiling, counted
      from the Job's ``startTime`` (before any pod is bound). It exists
      only so a pod that never schedules cannot leak forever, and is
      therefore set generously: budget + scheduling grace + slack.

    Labels carry ``app.kubernetes.io/component: agent`` because the
    NetworkPolicies select on it (without it, default-deny-egress blocks
    even DNS), but deliberately **not** the orchestrator/agent-role/slice
    labels the monitor and running-agent views enumerate: the runner is
    ephemeral infrastructure, not an agent, and must not surface in
    supervision or trip heartbeat-silence tripwires.
    """
    full_env = dict(env)
    full_env["EGG_PROPOSE_CHECK_CHECKS"] = json.dumps(
        [{"name": c["name"], "command": c["command"]} for c in checks]
    )
    full_env["EGG_PROPOSE_CHECK_REPO_DIR"] = repo_dir
    full_env["EGG_PROPOSE_CHECK_COMMIT_SHA"] = commit_sha
    full_env["EGG_PROPOSE_CHECK_OUTPUT_TAIL"] = str(_VERDICT_OUTPUT_TAIL_CHARS)
    full_env["EGG_PROPOSE_CHECK_INFRA_SIGNATURES"] = json.dumps(
        {
            "line": list(_INFRA_LINE_SIGNATURES),
            "substring": list(_INFRA_SUBSTRING_SIGNATURES),
        }
    )

    volumes = []
    volume_mounts = []
    for i, (container_path, host_path) in enumerate(sorted(repo_mounts.items())):
        volumes.append({"name": f"repo-{i}", "hostPath": {"path": host_path, "type": "Directory"}})
        volume_mounts.append({"name": f"repo-{i}", "mountPath": container_path})

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"{_JOB_NAME_PREFIX}{gate_id}",
            "labels": {
                "app.kubernetes.io/component": "agent",
                "app.kubernetes.io/part-of": "egg",
                "egg.propose-check": "true",
                _GATE_ID_LABEL: gate_id,
                "egg.pipeline.id": pipeline_id,
            },
        },
        "spec": {
            "ttlSecondsAfterFinished": 300,
            # Outer ceiling only — see the docstring. Generous by
            # construction so it never truncates the pod-level budget.
            "activeDeadlineSeconds": timeout_seconds + _POD_SCHEDULING_GRACE_SECONDS + 120,
            "backoffLimit": 0,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/component": "agent",
                        "egg.propose-check": "true",
                        _GATE_ID_LABEL: gate_id,
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    # The check budget (#3622): counted from pod start.
                    "activeDeadlineSeconds": timeout_seconds,
                    "securityContext": {
                        "runAsUser": host_uid,
                        "runAsGroup": host_gid,
                        "fsGroup": host_gid,
                    },
                    "containers": [
                        {
                            "name": "propose-check",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python3", "-c", _RUNNER_PROGRAM],
                            "env": [{"name": k, "value": v} for k, v in sorted(full_env.items())],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": volume_mounts,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }


def _submit_runner_job(k8s: Any, namespace: str, manifest: dict[str, Any]) -> None:
    """Convert the manifest dict to V1 objects and create the Job.

    Every field is read *from the manifest*. The green gate's equivalent
    hand-copies a fixed field list and silently drops anything it does
    not name — which is exactly how ``PodSpec.activeDeadlineSeconds``
    came to be missing there (#3622). ``test_every_manifest_field_reaches_the_body``
    fails on any manifest key this function does not copy.
    """
    from kubernetes import client as k8s_client_pkg

    container = manifest["spec"]["template"]["spec"]["containers"][0]
    pod_spec = manifest["spec"]["template"]["spec"]
    container_security = container["securityContext"]
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
                    restart_policy=pod_spec["restartPolicy"],
                    automount_service_account_token=pod_spec["automountServiceAccountToken"],
                    # #3622: the field the green gate never sets.
                    active_deadline_seconds=pod_spec["activeDeadlineSeconds"],
                    security_context=k8s_client_pkg.V1PodSecurityContext(
                        run_as_user=pod_spec["securityContext"]["runAsUser"],
                        run_as_group=pod_spec["securityContext"]["runAsGroup"],
                        fs_group=pod_spec["securityContext"]["fsGroup"],
                    ),
                    containers=[
                        k8s_client_pkg.V1Container(
                            name=container["name"],
                            image=container["image"],
                            image_pull_policy=container["imagePullPolicy"],
                            command=container["command"],
                            env=[
                                k8s_client_pkg.V1EnvVar(name=e["name"], value=e["value"])
                                for e in container["env"]
                            ],
                            security_context=k8s_client_pkg.V1SecurityContext(
                                allow_privilege_escalation=container_security[
                                    "allowPrivilegeEscalation"
                                ],
                                capabilities=k8s_client_pkg.V1Capabilities(
                                    drop=container_security["capabilities"]["drop"],
                                ),
                            ),
                            volume_mounts=[
                                k8s_client_pkg.V1VolumeMount(
                                    name=m["name"], mount_path=m["mountPath"]
                                )
                                for m in container["volumeMounts"]
                            ],
                        )
                    ],
                    volumes=[
                        k8s_client_pkg.V1Volume(
                            name=v["name"],
                            host_path=k8s_client_pkg.V1HostPathVolumeSource(
                                path=v["hostPath"]["path"],
                                type=v["hostPath"]["type"],
                            ),
                        )
                        for v in pod_spec["volumes"]
                    ],
                ),
            ),
        ),
    )
    k8s.batch_api.create_namespaced_job(namespace=namespace, body=body)


def _wait_for_runner_pod(k8s: Any, namespace: str, gate_id: str, *, timeout: float) -> Any:
    """Return the runner pod once Succeeded/Failed, or None on timeout."""
    selector = f"{_GATE_ID_LABEL}={gate_id}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pods = k8s.core_api.list_namespaced_pod(namespace=namespace, label_selector=selector)
        except Exception:  # noqa: BLE001 — transient list errors are retried
            time.sleep(5.0)
            continue
        for pod in getattr(pods, "items", []) or []:
            phase = (getattr(pod, "status", None) and pod.status.phase) or ""
            if phase in {"Succeeded", "Failed"}:
                return pod
        time.sleep(5.0)
    return None


def _delete_runner_job(k8s: Any, namespace: str, gate_id: str) -> None:
    """Best-effort Job cleanup. Never raises."""
    name = f"{_JOB_NAME_PREFIX}{gate_id}"
    try:
        from kubernetes import client as k8s_client_pkg

        k8s.batch_api.delete_namespaced_job(
            name=name,
            namespace=namespace,
            body=k8s_client_pkg.V1DeleteOptions(
                propagation_policy="Background", grace_period_seconds=0
            ),
        )
    except Exception as exc:  # noqa: BLE001 — cleanup must never wedge a propose
        logger.info("Propose check job cleanup skipped", job=name, error=str(exc))


def parse_verdict(raw_log: str) -> dict[str, Any] | None:
    """Extract the sentinel-prefixed JSON verdict from the pod log.

    Scans from the end so check output can never shadow the verdict.
    Returns ``None`` when no parseable verdict line exists — an
    infrastructure failure, which fails open.
    """
    if not raw_log:
        return None
    for line in reversed(raw_log.splitlines()):
        line = line.strip()
        if not line.startswith(VERDICT_SENTINEL):
            continue
        try:
            parsed = json.loads(line[len(VERDICT_SENTINEL) :])
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and isinstance(parsed.get("checks"), list):
            return parsed
        return None
    return None


def run_propose_checks(
    *,
    pipeline_id: str,
    commit_sha: str,
    base_branch: str,
    repo: str,
    checks: list[dict[str, str]],
    spawner: KubernetesSpawner,
    gateway_mode: Literal["public", "private"] = "public",
) -> tuple[dict[str, Any] | None, str | None]:
    """Execute ``checks`` against ``commit_sha`` in a sandboxed runner Job.

    Returns ``(verdict, infra_reason)``. Exactly one is non-``None``:
    a parsed verdict on success, or a human-readable infrastructure
    reason on every failure that is not a check verdict. The
    configured commands execute untrusted repo code (pytest/mypy import
    the tree), so they must never run in the orchestrator process — the
    runner pod is sandboxed exactly like an agent pod.

    The runner gets its own gateway worktree forked from ``base_branch``
    and its own gateway session (role ``tester``: read-side work only,
    it never pushes), then detaches to ``commit_sha`` — the *proposed*
    tree, not a branch tip that may have moved.
    """
    namespace = os.environ.get("EGG_AGENTS_NAMESPACE", "egg-agents")
    image = os.environ.get("EGG_SANDBOX_IMAGE", "egg:latest")
    timeout = _gate_timeout_seconds()
    host_uid = int(os.environ.get("HOST_UID", 1000))
    host_gid = int(os.environ.get("HOST_GID", 1000))
    gate_id = uuid.uuid4().hex[:12]
    runner_id = f"{_JOB_NAME_PREFIX}{gate_id}"

    try:
        wt_result = spawner.gateway.create_worktrees(
            container_id=runner_id,
            repos=[repo],
            uid=host_uid,
            gid=host_gid,
            base_branch=base_branch,
            assigned_branch=base_branch,
        )
    except Exception as exc:  # noqa: BLE001 — infra failure fails open
        return None, f"runner worktree creation failed: {exc}"
    if not (wt_result and wt_result.success and wt_result.worktrees):
        return None, (
            f"runner worktree creation returned no paths "
            f"(errors: {getattr(wt_result, 'errors', None)})"
        )

    session_token: str | None = None
    job_submitted = False
    try:
        try:
            session_info = spawner.gateway.register_session(
                container_id=runner_id,
                mode=gateway_mode,
                repos=[repo],
                uid=host_uid,
                gid=host_gid,
                phase="implement",
                pipeline_id=pipeline_id,
                agent_role="tester",
                branch=base_branch,
                base_branch=base_branch,
                retry_transient=True,
            )
            session_token = session_info.session_token
        except Exception as exc:  # noqa: BLE001 — infra failure fails open
            return None, f"runner session registration failed: {exc}"

        repo_mounts: dict[str, str] = {}
        repo_dir = ""
        for host_path in wt_result.worktrees.values():
            container_path = f"/home/egg/repos/{os.path.basename(host_path)}"
            repo_mounts[container_path] = host_path
            repo_dir = container_path

        from kubernetes_spawner import GATEWAY_K8S_URL

        env = {
            "GATEWAY_URL": GATEWAY_K8S_URL,
            "CONTAINER_ID": runner_id,
            "EGG_SESSION_TOKEN": session_token,
            "EGG_PROPOSE_CHECK_REQUIRE_PREBUILT": ("1" if _repo_requires_prebuilt(repo) else "0"),
        }

        manifest = build_runner_job_manifest(
            gate_id=gate_id,
            pipeline_id=pipeline_id,
            commit_sha=commit_sha,
            image=image,
            checks=checks,
            repo_mounts=repo_mounts,
            repo_dir=repo_dir,
            env=env,
            timeout_seconds=timeout,
            host_uid=host_uid,
            host_gid=host_gid,
        )

        try:
            _submit_runner_job(spawner.k8s, namespace, manifest)
            job_submitted = True
        except Exception as exc:  # noqa: BLE001 — infra failure fails open
            return None, f"runner job submit failed: {exc}"

        logger.info(
            "Propose check runner spawned (#3669)",
            pipeline_id=pipeline_id,
            gate_id=gate_id,
            commit_sha=commit_sha,
            base_branch=base_branch,
            commands=[c["command"] for c in checks],
            timeout_seconds=timeout,
        )

        pod = _wait_for_runner_pod(
            spawner.k8s,
            namespace,
            gate_id,
            timeout=timeout + _POD_SCHEDULING_GRACE_SECONDS,
        )
        if pod is None:
            return None, (
                f"runner pod did not reach a terminal state within "
                f"{timeout + _POD_SCHEDULING_GRACE_SECONDS}s (gate {gate_id})"
            )

        pod_name = (getattr(pod, "metadata", None) and pod.metadata.name) or ""
        raw_log = _read_runner_log(spawner.k8s, namespace, pod_name)
        verdict = parse_verdict(raw_log)
        if verdict is None:
            # The verdict is printed even when checks are red, so a
            # missing verdict is never a *check* failure: the runner
            # harness crashed, could not materialise the SHA, or was
            # killed by the pod deadline.
            return None, (
                f"no parseable verdict from runner (gate {gate_id}, pod {pod_name}); "
                f"log tail: {raw_log[-500:]}"
            )
        verdict["gate_id"] = gate_id
        verdict["pod"] = pod_name
        return verdict, None
    finally:
        if job_submitted:
            _delete_runner_job(spawner.k8s, namespace, gate_id)
        if session_token is not None:
            try:
                spawner.gateway.delete_session_by_container(runner_id)
            except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                logger.info(
                    "Propose check session cleanup skipped", runner_id=runner_id, error=str(exc)
                )
        try:
            spawner.gateway.delete_worktrees(runner_id, force=True)
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            logger.info(
                "Propose check worktree cleanup skipped", runner_id=runner_id, error=str(exc)
            )
