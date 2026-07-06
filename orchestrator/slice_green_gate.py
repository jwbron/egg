"""Per-slice green gate — execute repo checks at the slice tip before PR-open (#3398).

Every PR the ``issue-2270-overhaul`` pipeline opened was red: nothing on
the slice-close path ever *ran* ``make lint`` / ``make test`` against the
slice's integration-branch tip. The propose-time validator
(``routes.signals._validation._validate_tester_check_coverage``) only
compares the tester's self-reported ``checks_passed`` *names* against the
configured checks — it never executes anything, so a tester whose claim
does not match reality (scoped-down test run, unfinished format step)
sails through and CI becomes the first honest verdict, after the PR is
already open.

This module closes that trust-vs-verify gap. At slice close — after BRC
consensus, after the #3125 evidence-reachability gate, before any close
side effect — the orchestrator spawns a sandboxed one-shot **check-runner
Job** that checks out the integration-branch tip and executes the repo's
configured checks (``config.repo_config.get_repo_checks``; never
hardcoded commands). The runner prints a structured verdict to stdout;
the orchestrator parses it and blocks the slice PR from opening while any
check is red. The configured commands execute untrusted repo code
(pytest/mypy import the tree), so they must never run in the orchestrator
process — the runner pod is sandboxed exactly like an agent pod
(default-deny egress except gateway/DNS; git routes through the gateway
via a session token).

Mechanics mirror the two established precedents:

* Job lifecycle is modeled on the network-isolation probe
  (``routes.deployment._network_probe``): one-shot Job (``backoffLimit``
  0, ``restartPolicy`` Never), poll for a terminal pod phase, read the
  log with ``_preload_content=False`` (the k8s client's default path
  json.loads-then-str()s JSON-shaped bodies, corrupting them), parse the
  sentinel verdict line, delete the Job in ``finally``.
* Gate posture mirrors the #3125 evidence gate: **fail-open on
  infrastructure errors** (worktree/session/spawn failure, pod timeout,
  unparseable verdict — close proceeds with a warning), **fail-closed
  only on a definitive red** (a parsed verdict reporting a check
  failed). The caller records the slice failure, which routes through
  the existing cascade + ``OVERSEER_ALERT`` machinery.

  The fail-open guarantee only covers infrastructure failures the
  *orchestrator* observes before/around check execution. Once the runner
  is executing the checks, an infrastructure fault *inside* that
  execution — a transient gateway hiccup on a git call, a mid-run
  session-token expiry, an OOM-killed test worker, disk pressure —
  exits the check non-zero and surfaces as ``ok:false``, i.e. a
  definitive red that ``on`` mode blocks on. This is inherent to
  shelling out to checks (CI has the same property); the staged
  ``off → log → on`` rollout de-risks it, but ``on`` mode does not
  distinguish an infra-induced red inside a check from a genuine
  check failure.

Rollout is staged via ``EGG_SLICE_GREEN_GATE``: ``off`` (default) →
``log`` (run checks, log the verdict loudly, never block — the soak mode
while #3301 contract-single-writer is still landing, since a stale
contract snapshot on the slice tip can red contract-hygiene tests for
reasons unrelated to the slice's code) → ``on`` (block).

Stage A (#3409) adds config-driven auto-remediation on top of the Stage
B block: a check in ``repositories.yaml`` may carry an optional ``fix``
command (e.g. egg's ``lint`` check gets ``fix: make lint-fix``). When
the gate finds such a check red, the runner executes the fix inside its
worktree and re-runs the check. If every failed check re-ran green, the
orchestrator stages the fix from the shared hostPath worktree, commits
it as ``egg-green-gate``, and pushes it to the slice integration branch
through the launcher-authed gateway push route before any close side
effect, then lets the slice close: the runner already re-validated the
identical tree, so no second runner pass is needed, and dependent
slices fork from the remote tip after the fix commit. In ``log`` mode
the fix still runs in the runner (soak signal) but nothing is committed
or pushed. Checks without a ``fix``, or whose re-run stays red, block
exactly like Stage B.

The check toolchain is the **repo-defined** one, not the sandbox
image's: ``repositories.yaml::build_commands`` builds the repo's pinned
dev environment at image build (e.g. egg's ``make sandbox-deps`` →
``uv sync --extra dev``) and persists the dirs it names (``.venv``) to
``/opt/prebuilt-deps/<owner--repo>/``. The runner restores those into
its worktree before executing checks, so the tools that run are exactly
the versions the repo pins — toolchain-identical to CI by construction.
(The restore requires the venv to be relocatable; ``make sandbox-deps``
creates it so.) When the repo config declares ``persist_dirs`` but the
image carries no prebuilt snapshot, the runner exits non-zero — an
infrastructure failure that fails open, never a false red from
missing tools. ``make test``'s changeset-aware narrowing derives its
baseline via ``git merge-base HEAD origin/<default>`` inside the
runner's fresh worktree — the *cumulative* slice diff, deliberately
wider than the tester's own-files scope that let the #3398 class-3
failures through.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from egg_logging import get_logger

if TYPE_CHECKING:
    from kubernetes_spawner import KubernetesSpawner

logger = get_logger("orchestrator.slice_green_gate")

# Operator switch for the green gate. Three-state, default off during
# rollout (#3398): "off"/unset → gate skipped entirely; "log" → checks
# run and a red verdict is logged loudly but never blocks; "on" → a red
# verdict blocks the slice PR from opening.
GREEN_GATE_ENV_VAR = "EGG_SLICE_GREEN_GATE"

_ENABLED_VALUES = frozenset({"on", "1", "true", "yes"})
_LOG_ONLY_VALUES = frozenset({"log", "log-only", "log_only"})

# Comma-separated check *names* (from repositories.yaml ``checks``) the
# gate skips. Default skips ``security``: the full scan belongs on the
# context PR / terminal verification, not on every slice close.
GREEN_GATE_SKIP_CHECKS_ENV_VAR = "EGG_SLICE_GREEN_GATE_SKIP_CHECKS"
_DEFAULT_SKIP_CHECKS = "security"

# Wall-clock budget for the runner pod (spawn-to-terminal). A slice's
# changeset-narrowed ``make test`` normally finishes well inside this;
# the ceiling exists so a hung suite degrades to fail-open instead of
# wedging the slice close.
GREEN_GATE_TIMEOUT_ENV_VAR = "EGG_SLICE_GREEN_GATE_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 1800

# Extra wall-clock the orchestrator's wait loop allows on top of the
# in-pod check budget, to absorb pod scheduling + image-pull latency.
# The wait clock starts at Job submit, before the pod is scheduled/pulled
# — so on a cold node a long image pull would otherwise eat into the
# check budget and trip a spurious fail-open timeout even when the checks
# would have passed. The pod's own ``activeDeadlineSeconds`` (which the
# kubelet counts from pod *start*, i.e. after scheduling) still caps the
# actual check duration, so a genuinely hung check is killed by the pod
# deadline rather than lingering for the full grace-padded wait.
_POD_SCHEDULING_GRACE_SECONDS = 120

# Per-check output tail retained in the verdict (runner side) and the
# smaller slice carried into the failure string routed to the cascade /
# OVERSEER_ALERT payload.
_VERDICT_OUTPUT_TAIL_CHARS = 4000
_FAILURE_MESSAGE_TAIL_CHARS = 1500

# Sentinel prefixing the runner's single-line JSON verdict on stdout.
# Parsed from the end of the pod log so check output that happens to be
# JSON-shaped can never be mistaken for the verdict.
VERDICT_SENTINEL = "EGG_GREEN_GATE_VERDICT:"

# Label carrying the per-invocation gate id; the wait loop locates the
# runner pod by this selector (mirrors ``egg.io/probe-id``).
_GATE_ID_LABEL = "egg.io/green-gate-id"

# Cap on the informational ``changed_files`` list a fix result carries
# in the verdict; a repo-wide format sweep can touch hundreds of files
# and the verdict must stay a single parseable log line.
_FIX_CHANGED_FILES_CAP = 100

# Identity for the orchestrator-authored autofix commit (#3409).
# Precedent: agent_salvage's ``egg-salvage`` system identity and the
# git-route orchestrator attribution from #2919; the commit must read
# as pipeline infrastructure in the history, not as a phantom coder.
_AUTOFIX_COMMIT_NAME = "egg-green-gate"
_AUTOFIX_COMMIT_EMAIL = "egg-green-gate@localhost"

# Per-git-invocation ceiling for the autofix stage/commit sequence. The
# operations are local (no network); a format sweep staging hundreds of
# files finishes in single-digit seconds.
_AUTOFIX_GIT_TIMEOUT_SECONDS = 120

# The runner program, executed as ``python3 -c`` in the pod. Restores
# the repo's prebuilt build_commands artifacts (its pinned ``.venv``)
# into the worktree, then reads the check list (JSON) and repo dir from
# env, runs each check sequentially with combined output capture, and
# prints the sentinel verdict line. Always exits 0 when the harness
# itself worked: the verdict — not the pod exit code — is the pass/fail
# channel, so a non-zero pod exit unambiguously means runner
# infrastructure failure (fail-open). Missing/failed prebuilt restore
# when the repo config requires one is exactly such an infra failure —
# proceeding would red every check with "command not found" and block
# the slice for a toolchain-packaging problem that is not its fault.
#
# #3409 Stage A: when a check fails and carries a configured ``fix``
# command, the runner executes the fix in the worktree and re-runs the
# check, reporting a ``fix`` sub-object in that check's verdict entry.
# The check's ``ok`` stays false: the slice tip as pushed is still red;
# only the orchestrator (the sanctioned writer) may turn the fixed tree
# into a commit on the integration branch. The runner itself never
# pushes; the fix mutates the hostPath-mounted worktree, which the
# orchestrator stages and commits after the pod exits.
_RUNNER_PROGRAM = """
import json, os, shutil, subprocess, sys, time

checks = json.loads(os.environ["EGG_GREEN_GATE_CHECKS"])
repo_dir = os.environ["EGG_GREEN_GATE_REPO_DIR"]
tail = int(os.environ.get("EGG_GREEN_GATE_OUTPUT_TAIL", "4000"))
changed_files_cap = int(os.environ.get("EGG_GREEN_GATE_CHANGED_FILES_CAP", "100"))


def restore_prebuilt(target_dir):
    # Mirror sandbox.entrypoint._worktrees.restore_prebuilt_deps: copy
    # /opt/prebuilt-deps/<owner--repo>/* (the persist_dirs snapshot from
    # the repo's build_commands, e.g. .venv) into the mounted worktree,
    # skipping paths that already exist. No chown needed — this pod runs
    # as the worktree's owning uid, unlike the root entrypoint.
    base = os.environ.get("EGG_GREEN_GATE_PREBUILT_BASE", "/opt/prebuilt-deps")
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


try:
    restored = restore_prebuilt(repo_dir)
except Exception as exc:
    print(f"green-gate runner: prebuilt deps restore failed: {exc}", file=sys.stderr)
    sys.exit(1)
if os.environ.get("EGG_GREEN_GATE_REQUIRE_PREBUILT") == "1" and restored is None:
    print(
        "green-gate runner: repo config declares persist_dirs but no prebuilt "
        "snapshot exists under /opt/prebuilt-deps — rebuild the sandbox image",
        file=sys.stderr,
    )
    sys.exit(1)

def run_cmd(command):
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        return proc.returncode, proc.stdout or ""
    except Exception as exc:
        return -1, f"runner failed to execute command: {exc}"


def tracked_changed_files():
    # Informational only (#3409): tracked modifications the fix left in
    # the worktree, via the sandbox's gateway-routed git. The
    # orchestrator stages from the shared worktree itself, so a failure
    # here (git wrapper hiccup) degrades to an unreported file list,
    # never a wrong commit.
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return [line for line in (proc.stdout or "").splitlines() if line.strip()]


results = []
for check in checks:
    started = time.monotonic()
    rc, out = run_cmd(check["command"])
    entry = {
        "name": check["name"],
        "ok": rc == 0,
        "exit_code": rc,
        "output_tail": out[-tail:],
    }
    fix_cmd = check.get("fix")
    if rc != 0 and fix_cmd:
        # #3409: attempt the configured auto-remediation, then re-run
        # the check against the fixed tree. The re-run verdict, not the
        # fix command's exit code, decides success: a fixer may exit
        # non-zero while still having repaired everything the check
        # tests (or exit zero while leaving unfixable findings).
        fix_rc, fix_out = run_cmd(fix_cmd)
        rerun_rc, rerun_out = run_cmd(check["command"])
        changed = tracked_changed_files()
        entry["fix"] = {
            "command": fix_cmd,
            "exit_code": fix_rc,
            "check_ok_after_fix": rerun_rc == 0,
            "changed_files": (changed[:changed_files_cap] if changed is not None else None),
            "changed_file_count": (len(changed) if changed is not None else None),
            "output_tail": fix_out[-tail:],
            "recheck_output_tail": rerun_out[-tail:],
        }
    entry["duration_seconds"] = round(time.monotonic() - started, 1)
    results.append(entry)

print("EGG_GREEN_GATE_VERDICT:" + json.dumps({"checks": results}), flush=True)
"""


def green_gate_mode() -> Literal["off", "log", "on"]:
    """Resolve the operator switch to one of ``off`` / ``log`` / ``on``.

    Unknown values resolve to ``off``: during rollout an operator typo
    must degrade to "gate does nothing", never to "gate blocks slices".
    """
    raw = os.environ.get(GREEN_GATE_ENV_VAR, "off").strip().lower()
    if raw in _ENABLED_VALUES:
        return "on"
    if raw in _LOG_ONLY_VALUES:
        return "log"
    return "off"


def _gate_checks(repo: str) -> list[dict[str, str]]:
    """Return the configured checks the gate runs for ``repo``.

    Config-driven: exactly ``get_repo_checks(repo)`` minus the names in
    the skip set (default ``security``). Returns ``[]`` — gate skips —
    when the repo has no checks configured or config loading fails
    (fail-open: a config problem must not block a consensus-reached
    slice).
    """
    try:
        from config.repo_config import get_repo_checks
    except ImportError:
        try:
            from repo_config import get_repo_checks  # type: ignore[no-redef]
        except ImportError:
            return []

    try:
        configured = get_repo_checks(repo)
    except Exception as exc:  # noqa: BLE001 — config load is best-effort
        logger.warning(
            "Green gate skipped: failed to load repo checks config (#3398)",
            repo=repo,
            error=str(exc),
        )
        return []

    raw_skip = os.environ.get(GREEN_GATE_SKIP_CHECKS_ENV_VAR, _DEFAULT_SKIP_CHECKS)
    skip = {name.strip().lower() for name in raw_skip.split(",") if name.strip()}
    return [c for c in configured if c["name"].strip().lower() not in skip]


def _repo_requires_prebuilt(repo: str) -> bool:
    """True when ``repo``'s build_commands persist a toolchain snapshot.

    A repo that declares ``persist_dirs`` (e.g. ``.venv``) defines its
    check toolchain via ``build_commands`` — the runner must restore the
    prebuilt snapshot or fail as infrastructure, never run checks
    against whatever happens to be on the image PATH.
    """
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


def _gate_timeout_seconds() -> int:
    raw = os.environ.get(GREEN_GATE_TIMEOUT_ENV_VAR, "")
    try:
        value = int(raw)
    except ValueError:
        # ``raw`` is always a ``str`` (``os.environ.get(..., "")``), so
        # ``int()`` can only raise ``ValueError`` here — ``TypeError`` is
        # unreachable.
        return _DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_TIMEOUT_SECONDS


def _build_runner_job_manifest(
    *,
    gate_id: str,
    pipeline_id: str,
    slice_id: str,
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

    Returning a dict keeps the builder unit-testable without the
    kubernetes SDK (mirrors ``_build_probe_job_manifest``). Labels
    deliberately carry ``app.kubernetes.io/component: agent`` — the
    NetworkPolicies select on it, and without it default-deny-egress
    blocks even DNS — but **not** the orchestrator/agent-role/slice
    labels the monitor and running-agent views enumerate: the runner is
    ephemeral infrastructure, not an agent, and must not surface in
    supervision or trip heartbeat-silence tripwires.

    ``repo_mounts`` maps container mount path → host worktree path.
    """
    full_env = dict(env)
    full_env["EGG_GREEN_GATE_CHECKS"] = json.dumps(checks)
    full_env["EGG_GREEN_GATE_REPO_DIR"] = repo_dir
    full_env["EGG_GREEN_GATE_OUTPUT_TAIL"] = str(_VERDICT_OUTPUT_TAIL_CHARS)
    full_env["EGG_GREEN_GATE_CHANGED_FILES_CAP"] = str(_FIX_CHANGED_FILES_CAP)

    volumes = []
    volume_mounts = []
    for i, (container_path, host_path) in enumerate(sorted(repo_mounts.items())):
        volumes.append(
            {
                "name": f"repo-{i}",
                "hostPath": {"path": host_path, "type": "Directory"},
            }
        )
        volume_mounts.append({"name": f"repo-{i}", "mountPath": container_path})

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"egg-greengate-{gate_id}",
            "labels": {
                "app.kubernetes.io/component": "agent",
                "app.kubernetes.io/part-of": "egg",
                "egg.green-gate": "true",
                _GATE_ID_LABEL: gate_id,
                "egg.pipeline.id": pipeline_id,
                "egg.green-gate.slice": slice_id,
            },
        },
        "spec": {
            # Primary cleanup is the caller's finally-delete; the TTL
            # only extends lifetime when the orchestrator crashed before
            # reaching it (probe precedent).
            "ttlSecondsAfterFinished": 300,
            # Give the in-pod checks the full budget; the orchestrator's
            # wait loop enforces the same ceiling, and the deadline
            # guarantees a hung check terminates rather than lingering.
            "activeDeadlineSeconds": timeout_seconds + 60,
            "backoffLimit": 0,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/component": "agent",
                        "egg.green-gate": "true",
                        _GATE_ID_LABEL: gate_id,
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    # Match agent pods so the runner can write test/lint
                    # caches and selection JSON into the worktree the
                    # gateway just chowned to the host uid/gid.
                    "securityContext": {
                        "runAsUser": host_uid,
                        "runAsGroup": host_gid,
                        "fsGroup": host_gid,
                    },
                    "containers": [
                        {
                            "name": "green-gate",
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
    """Convert the manifest dict to V1 objects and create the Job."""
    from kubernetes import client as k8s_client_pkg

    container = manifest["spec"]["template"]["spec"]["containers"][0]
    pod_spec = manifest["spec"]["template"]["spec"]
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
                    automount_service_account_token=False,
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
                                allow_privilege_escalation=False,
                                capabilities=k8s_client_pkg.V1Capabilities(drop=["ALL"]),
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


def _read_runner_log(k8s: Any, namespace: str, pod_name: str) -> str:
    """Read the runner pod's stdout as raw text.

    ``_preload_content=False`` is load-bearing: the kubernetes client's
    default path runs ``json.loads`` on JSON-shaped response bodies and
    ``str()``s the dict back, corrupting the verdict line into a Python
    dict repr (probe precedent, ``_read_probe_log``). The lazy ``.data``
    read must stay inside the try — that is where the network I/O
    actually happens.
    """
    try:
        raw = k8s.core_api.read_namespaced_pod_log(
            name=pod_name, namespace=namespace, _preload_content=False
        )
        if raw is None:
            return ""
        data = getattr(raw, "data", raw)
    except Exception as exc:  # noqa: BLE001 — log read is best-effort
        logger.warning("Green gate runner log read failed", pod=pod_name, error=str(exc))
        return ""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _delete_runner_job(k8s: Any, namespace: str, gate_id: str) -> None:
    """Best-effort Job cleanup. Never raises."""
    name = f"egg-greengate-{gate_id}"
    try:
        from kubernetes import client as k8s_client_pkg

        k8s.batch_api.delete_namespaced_job(
            name=name,
            namespace=namespace,
            body=k8s_client_pkg.V1DeleteOptions(
                propagation_policy="Background", grace_period_seconds=0
            ),
        )
    except Exception as exc:  # noqa: BLE001 — cleanup must never wedge the close
        logger.info("Green gate job cleanup skipped", job=name, error=str(exc))


def parse_verdict(raw_log: str) -> dict[str, Any] | None:
    """Extract the sentinel-prefixed JSON verdict from the pod log.

    Scans from the end so check output can never shadow the verdict.
    Returns ``None`` when no parseable verdict line exists (an
    infrastructure failure — the caller fails open).
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


def _format_failed_checks(failed: list[dict[str, Any]]) -> str:
    """Render failing checks + output tails into the failure string."""
    parts = []
    for check in failed:
        tail = str(check.get("output_tail") or "")[-_FAILURE_MESSAGE_TAIL_CHARS:]
        parts.append(f"[{check.get('name')}] exit {check.get('exit_code')}:\n{tail}".strip())
    return "\n\n".join(parts)


def _commit_and_push_autofix(
    gateway: Any,
    *,
    pipeline_id: str,
    slice_id: str,
    worktree_path: str,
    integration_branch: str,
    gateway_mode: Literal["public", "private"],
    fixed_checks: list[dict[str, Any]],
) -> str | None:
    """Commit the runner's fix output and push it to the integration branch (#3409).

    The runner executed each failed check's configured ``fix`` command
    inside the hostPath-mounted gateway worktree and re-ran the checks
    green, so the tree at ``worktree_path`` is exactly the tree the
    checks validated. This stages the tracked modifications
    (``git add -u``: untracked check droppings such as caches and
    selection JSON are never picked up), commits them under the
    orchestrator's green-gate identity with ``--no-verify`` (state-store
    precedent; the sandbox commit path suppresses hooks the same way),
    and pushes via the launcher-authed gateway push route, the
    sanctioned writer: the runner never pushes. Because the check
    re-run happened against this identical tree inside the runner's
    pinned toolchain, the commit needs no second runner pass to be
    trusted green.

    The push lands before any slice-close side effect, so dependent
    slices (which fork from the integration branch's remote tip when
    they start) fork after the format commit and cannot inherit
    unformatted code that would re-trip the gate.

    Returns ``None`` on success, or a human-readable error string; the
    caller then blocks the slice exactly like an unfixed red (Stage B
    behavior) with the error appended to the failure message.
    """

    def _git(*args: str, identity: bool = False) -> subprocess.CompletedProcess[str]:
        cmd = ["git", "-C", worktree_path]
        if identity:
            cmd += [
                "-c",
                f"user.name={_AUTOFIX_COMMIT_NAME}",
                "-c",
                f"user.email={_AUTOFIX_COMMIT_EMAIL}",
            ]
        cmd += list(args)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_AUTOFIX_GIT_TIMEOUT_SECONDS,
        )

    fixed_names = ", ".join(str(c.get("name")) for c in fixed_checks)
    try:
        add = _git("add", "-u")
        if add.returncode != 0:
            return f"git add -u failed: {(add.stderr or add.stdout or '').strip()}"

        staged = _git("diff", "--cached", "--name-only")
        if staged.returncode != 0:
            return (
                "git diff --cached --name-only failed: "
                f"{(staged.stderr or staged.stdout or '').strip()}"
            )
        staged_files = [line for line in (staged.stdout or "").splitlines() if line.strip()]
        if not staged_files:
            # The re-run went green without the fix modifying any
            # tracked file (a flaky first run). Nothing committable can
            # make the remote tip green, so refuse rather than pass a
            # tip whose red verdict stands as pushed.
            return (
                "fix commands re-ran the checks green but left no tracked modifications to commit"
            )

        message = (
            f"Apply configured check autofix at the green gate: {fixed_names}\n\n"
            f"Automated commit for pipeline {pipeline_id}, slice {slice_id} "
            f"(#3409). The green-gate runner found the named checks red at "
            f"the {integration_branch} tip, ran their configured fix "
            f"commands, and re-ran the checks green."
        )
        commit = _git("commit", "--no-verify", "-m", message, identity=True)
        if commit.returncode != 0:
            return f"git commit failed: {(commit.stderr or commit.stdout or '').strip()}"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"autofix git operation raised: {exc}"

    try:
        push = gateway.push_worktree_branch(
            pipeline_id,
            repo_path=worktree_path,
            branch=integration_branch,
            mode=gateway_mode,
        )
    except Exception as exc:  # noqa: BLE001 - push failure blocks like an unfixed red
        return f"autofix push to {integration_branch} raised: {exc}"
    if not getattr(push, "ok", False):
        return (
            f"autofix push to {integration_branch} failed "
            f"({getattr(push, 'category', 'unknown')}): {getattr(push, 'detail', '')}"
        )

    logger.info(
        "Green gate autofix committed and pushed (#3409)",
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        integration_branch=integration_branch,
        fixed_checks=fixed_names,
        staged_file_count=len(staged_files),
    )
    return None


def run_slice_green_gate(
    pipeline_id: str,
    spawner: "KubernetesSpawner",  # noqa: UP037
    slice_id: str,
    integration_branch: str,
    repo: str,
    *,
    gateway_mode: Literal["public", "private"] = "public",
) -> str | None:
    """Execute the repo's configured checks at the slice tip; gate PR-open (#3398).

    Runs after slice consensus and the #3125 evidence gate, before any
    close side effect. Returns ``None`` when the slice may close (checks
    green, gate off/log-mode, an infrastructure failure — fail-open —
    or a red verdict fully remediated by the #3409 autofix commit), or
    a human-readable failure string naming the red checks — the caller
    records the slice failure with it, routing through the existing
    cascade + OVERSEER_ALERT machinery instead of opening a red PR.

    The runner gets its own gateway worktree forked from
    ``origin/<integration_branch>`` (both ``base_branch`` and
    ``assigned_branch`` point at it so the assigned-branch fork-point
    override, #3068, resolves to the same tip) and its own gateway
    session so the sandbox git wrapper works — ``make test``'s selector
    shells out to gateway-routed git for its merge-base/diff. The
    session role is ``tester``: the runner does exactly the read-side
    work a tester session is scoped for, and never pushes.
    """
    mode = green_gate_mode()
    if mode == "off":
        logger.info(
            "Green gate disabled by kill switch (#3398)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
        )
        return None

    checks = _gate_checks(repo)
    if not checks:
        logger.info(
            "Green gate skipped: no configured checks for repo (#3398)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            repo=repo,
        )
        return None

    namespace = os.environ.get("EGG_AGENTS_NAMESPACE", "egg-agents")
    image = os.environ.get("EGG_SANDBOX_IMAGE", "egg:latest")
    timeout = _gate_timeout_seconds()
    host_uid = int(os.environ.get("HOST_UID", 1000))
    host_gid = int(os.environ.get("HOST_GID", 1000))
    gate_id = uuid.uuid4().hex[:12]
    runner_id = f"egg-greengate-{gate_id}"

    # --- materialize the slice tip: gateway worktree at the integration branch
    try:
        wt_result = spawner.gateway.create_worktrees(
            container_id=runner_id,
            repos=[repo],
            uid=host_uid,
            gid=host_gid,
            base_branch=integration_branch,
            assigned_branch=integration_branch,
        )
    except Exception as exc:  # noqa: BLE001 — infra failure fails open
        logger.warning(
            "Green gate skipped: runner worktree creation failed (#3398)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            error=str(exc),
        )
        return None
    if not (wt_result and wt_result.success and wt_result.worktrees):
        logger.warning(
            "Green gate skipped: runner worktree creation returned no paths (#3398)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            errors=getattr(wt_result, "errors", None),
        )
        return None

    session_token: str | None = None
    job_submitted = False
    try:
        # --- gateway session so the sandbox git wrapper (test selector) works
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
                branch=integration_branch,
                base_branch=integration_branch,
                retry_transient=True,
            )
            session_token = session_info.session_token
        except Exception as exc:  # noqa: BLE001 — infra failure fails open
            logger.warning(
                "Green gate skipped: runner session registration failed (#3398)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(exc),
            )
            return None

        repo_mounts: dict[str, str] = {}
        repo_dir = ""
        repo_host_dir = ""
        for host_path in wt_result.worktrees.values():
            container_path = f"/home/egg/repos/{os.path.basename(host_path)}"
            repo_mounts[container_path] = host_path
            repo_dir = container_path
            # Orchestrator-side path of the same worktree: the #3409
            # autofix stages/commits here after the runner pod exits.
            repo_host_dir = host_path

        from kubernetes_spawner import GATEWAY_K8S_URL

        env = {
            "GATEWAY_URL": GATEWAY_K8S_URL,
            "CONTAINER_ID": runner_id,
            "EGG_SESSION_TOKEN": session_token,
            "EGG_GREEN_GATE_REQUIRE_PREBUILT": ("1" if _repo_requires_prebuilt(repo) else "0"),
        }

        manifest = _build_runner_job_manifest(
            gate_id=gate_id,
            pipeline_id=pipeline_id,
            slice_id=slice_id,
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
            logger.warning(
                "Green gate skipped: runner job submit failed (#3398)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(exc),
            )
            return None

        logger.info(
            "Green gate runner spawned (#3398)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            gate_id=gate_id,
            integration_branch=integration_branch,
            checks=[c["name"] for c in checks],
            timeout_seconds=timeout,
            mode=mode,
        )

        pod = _wait_for_runner_pod(
            spawner.k8s,
            namespace,
            gate_id,
            timeout=timeout + _POD_SCHEDULING_GRACE_SECONDS,
        )
        if pod is None:
            logger.warning(
                "Green gate skipped: runner pod did not reach a terminal state (#3398)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                gate_id=gate_id,
                timeout_seconds=timeout,
                wait_budget_seconds=timeout + _POD_SCHEDULING_GRACE_SECONDS,
            )
            return None

        pod_name = (getattr(pod, "metadata", None) and pod.metadata.name) or ""
        phase = (getattr(pod, "status", None) and pod.status.phase) or ""
        raw_log = _read_runner_log(spawner.k8s, namespace, pod_name)
        verdict = parse_verdict(raw_log)

        if verdict is None:
            # Covers pod Failed (runner harness crashed — the verdict is
            # printed even when checks are red, so a missing verdict is
            # never a check failure) and unparseable output.
            logger.warning(
                "Green gate skipped: no parseable verdict from runner (#3398)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                gate_id=gate_id,
                pod_phase=phase,
                log_tail=raw_log[-500:],
            )
            return None

        failed = [c for c in verdict["checks"] if not c.get("ok")]
        if not failed:
            logger.info(
                "Green gate passed: slice tip green on configured checks (#3398)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                gate_id=gate_id,
                checks=[c.get("name") for c in verdict["checks"]],
            )
            return None

        failed_names = ", ".join(str(c.get("name")) for c in failed)
        # #3409 Stage A: the gate can self-heal when EVERY failed check
        # carries a fix result whose re-run went green. A partially
        # fixable verdict (some failed check has no fix, or its re-run
        # stayed red) routes to the slice team unchanged: committing a
        # partial fix would re-run the gate against a tip that is still
        # red by construction.
        autofix_ready = all(
            isinstance(c.get("fix"), dict) and c["fix"].get("check_ok_after_fix") for c in failed
        )
        logger.error(
            "Green gate red: configured checks failed at the slice tip (#3398)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            gate_id=gate_id,
            integration_branch=integration_branch,
            failed_checks=failed_names,
            autofix_ready=autofix_ready,
            mode=mode,
        )
        autofix_note = ""
        if autofix_ready and mode == "on" and repo_host_dir:
            autofix_error = _commit_and_push_autofix(
                spawner.gateway,
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                worktree_path=repo_host_dir,
                integration_branch=integration_branch,
                gateway_mode=gateway_mode,
                fixed_checks=failed,
            )
            if autofix_error is None:
                # The fixed tree the runner re-validated green is now
                # the integration-branch tip; the slice may close.
                return None
            logger.warning(
                "Green gate autofix failed; blocking slice like an unfixed red (#3409)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                gate_id=gate_id,
                integration_branch=integration_branch,
                error=autofix_error,
            )
            autofix_note = (
                f"\n\nThe configured fix commands turned the checks green in "
                f"the runner, but committing/pushing the fix failed: "
                f"{autofix_error}"
            )
        elif autofix_ready and mode == "log":
            logger.info(
                "Green gate log mode: autofix available but not applied (#3409)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                gate_id=gate_id,
                failed_checks=failed_names,
            )
        if mode == "log":
            return None
        return (
            f"slice {slice_id}: green gate failed — configured checks are red "
            f"at integration branch {integration_branch} tip: {failed_names}.\n\n"
            f"{_format_failed_checks(failed)}"
            f"{autofix_note}\n\n"
            f"Fix the failures on {integration_branch} and restart the slice; "
            f"set {GREEN_GATE_ENV_VAR}=off to bypass."
        )
    finally:
        if job_submitted:
            _delete_runner_job(spawner.k8s, namespace, gate_id)
        if session_token is not None:
            try:
                spawner.gateway.delete_session_by_container(runner_id)
            except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                logger.info(
                    "Green gate session cleanup skipped",
                    runner_id=runner_id,
                    error=str(exc),
                )
        try:
            spawner.gateway.delete_worktrees(runner_id, force=True)
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            logger.info(
                "Green gate worktree cleanup skipped",
                runner_id=runner_id,
                error=str(exc),
            )
