"""Propose-time check gate — execute the repo's configured checks before
a proposal becomes reviewable (#3669).

BRC consensus validates *coherence*, not *correspondence* (#3595 root
cause 6). Run 5 of the ``laguna-s-2.1`` assessment reached full 8-of-8
consensus on both slices with zero unresolved NACKs while carrying three
purely mechanical defects: a file 84 lines over the hard cap, a missing
``FINDING_CLASS_REMEDIATIONS`` key whose test already existed and named
the exact failure in its own assertion message, and a SIGTERM/143
reclassification that left a contradicting test in the tree. None of the
three needs judgment; all three are found by running the commands the
repo already configures.

The remedy is **not** more reviewers, and **not** a mandate that each
reviewer run the checks. The same run's five NACK rounds caught four
genuine semantic defects — an unregistered ``detect_heartbeat_stall``, a
data source ``cq-1`` had ruled out, a container-ID-vs-role-name key
error, a wrong ``tool_calls_by_role`` lookup — that no linter finds.
Reviewer attention is the one resource that demonstrably catches those,
and spending it on something a script does perfectly costs 5x the
compute for one bit of information. The seam between "reason over a
diff" and "execute against the environment" is unmanned, and it should
be manned by the **system**: once per proposal, before any reviewer is
dispatched.

Relationship to the per-slice green gate (#3398)
------------------------------------------------
This gate does **not** replace it, and must not be read as weakening it.
Run 5 proves propose-time alone is insufficient: ``slice-2`` introduced
none of the three defects and inherited all of them from ``slice-1``'s
integration. Only a check at the *integration tip* sees that. The two
gates are complementary and differ deliberately:

============  ===================================  =========================
              propose gate (this module, #3669)    green gate (#3398)
============  ===================================  =========================
when          before a proposal becomes reviewable  before a slice PR opens
tree          the proposed ``commit_sha``           the integration-branch tip
test command  ``full_command`` (``make test-all``)  ``command`` (``make test``)
on red        proposal rejected; producer fixes     PR withheld; HITL decision
============  ===================================  =========================

Narrowed runs are not evidence
------------------------------
``make test`` is changeset-aware by design and narrows to the tests
statically reachable from the diff; ``make test-all`` is the CI ground
truth (root ``CLAUDE.md``). An agent following the repo's own documented
guidance therefore gets a green *narrow* result and reports "tests pass"
with the confidence of a full run. This is not hypothetical: the run-5
handoff reported "3751 passed, 1 failed" where a full run at the same
tip reports 8833 passed and surfaces the third defect — which lives in
an unrelated file about rate limiting, exactly the kind the import graph
will not reach.

So this gate never runs the narrowed form. Each configured check is
resolved to its ``full_command`` when ``repositories.yaml`` declares one
(egg: ``test`` → ``make test-all``) and to ``command`` otherwise, and
the **exact command string** plus the **SHA it ran against** are
recorded in the verdict and stamped onto the accepted proposal's
``attestation.checks_verified``. A narrowed run is then visibly narrow
rather than silently incomplete.

Fail open on infra, closed on real failures
-------------------------------------------
A check that could not run is not a check that passed. The vocabulary is
#3621's, shared literally: the infra signatures are imported from
``slice_green_gate`` so the two gates cannot drift on what counts as an
infrastructure fault, and the same *uniform* classification is applied
to every red this gate produces (#3621's complaint about the green gate
was precisely an asymmetry — ``classify_infra`` applied to one run and
not another). Fail-open covers: no configured checks, unresolvable
pipeline state, worktree/session/spawn failure, a pod that never reaches
a terminal state, an unparseable verdict, a checkout that cannot
materialise the proposed SHA, and a red verdict whose every failed check
carries an infra tag. It never covers a check that ran and failed.

Budget and timeout
------------------
Per #3622: the runner's deadline is set on **``spec.template.spec``**
(the ``PodSpec`` field the kubelet counts from pod start), not on the
Job, so pod scheduling and image-pull latency are not charged to the
check budget. The Job keeps an outer ``activeDeadlineSeconds`` ceiling
so a pod that never schedules cannot leak, and the orchestrator's own
wait loop is the outermost bound. ``_submit_runner_job`` copies the
pod-level field explicitly — the green gate's equivalent hand-copies a
fixed field list and silently drops anything it does not name, which is
the trap #3622 documents.

Asynchrony, and why a proposal can be "pending"
-----------------------------------------------
The sandbox posts ``CONSENSUS_PROPOSE`` over HTTP with a **15-second**
timeout (``egg_agent_tools.handlers._gateway.orchestrator_request``), so
the checks cannot run inside the request. They run in a background
thread against a sandboxed runner Job, and the verdict is recorded in a
process-local ledger keyed by ``(pipeline_id, slice_id, commit_sha)`` —
so a tree is checked **once**, no matter how many producers propose it
or how many times a propose is retried.

A propose that arrives before its tree has a verdict is therefore
answered ``409 checks_running``: the proposal is *not* recorded, no
reviewer is dispatched, and the producer re-proposes once the run
finishes. That is the one place the shape bends: a green proposal is
rejected once with a "wait" before it is accepted. Everything after the
verdict lands is untouched — a green verdict makes the propose path
byte-for-byte what it was before this gate existed.

Rollout
-------
``EGG_PROPOSE_CHECK_GATE``: ``off`` (**the default**) → ``log`` (run the
checks, log the verdict loudly, never reject) → ``on`` (reject on a
definitive red). It ships ``off`` deliberately: #3670 records that a
clean ``origin/main`` is red on two non-hermetic tests off-container,
and a gate enabled against a red baseline rejects every proposal on day
one. Flip the default only once #3670 is green — the invariant that
makes a check result load-bearing anywhere is "the suite is red" being
unambiguous evidence.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from egg_logging import get_logger
from slice_green_gate import (
    _INFRA_LINE_SIGNATURES,
    _INFRA_SUBSTRING_SIGNATURES,
    _read_runner_log,
)

if TYPE_CHECKING:
    from kubernetes_spawner import KubernetesSpawner

logger = get_logger("orchestrator.propose_check_gate")

# Operator switch. Three-state, default "off" — see the module docstring
# ("Rollout"): #3670 must land before this can default on, or the gate
# rejects every proposal against a red baseline.
GATE_ENV_VAR = "EGG_PROPOSE_CHECK_GATE"

_ENABLED_VALUES = frozenset({"on", "1", "true", "yes"})
_LOG_ONLY_VALUES = frozenset({"log", "log-only", "log_only"})
_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})

# Unset or unrecognised resolves here. Unlike the green gate — whose
# default is "on", so a typo can only over-verify — this gate's default
# is the *weakest* mode, so an unrecognised value cannot silently
# strengthen a deployment either. Both directions log.
_DEFAULT_MODE: Literal["off", "log", "on"] = "off"

# Comma-separated check *names* the gate skips. Same default as the
# green gate: the full security scan belongs on the context PR /
# terminal verification, not on every proposal.
SKIP_CHECKS_ENV_VAR = "EGG_PROPOSE_CHECK_GATE_SKIP_CHECKS"
_DEFAULT_SKIP_CHECKS = "security"

# Comma-separated pipeline phases the gate applies to. Refine and plan
# producers author *drafts*; running a repo's build/test suite against a
# prose analysis is pure cost and would red on unrelated baseline noise.
# The mechanical-defect class this gate exists for is an implement-phase
# class.
PHASES_ENV_VAR = "EGG_PROPOSE_CHECK_GATE_PHASES"
_DEFAULT_PHASES = "implement"

# Infra-red fail-open switch (#3417 / #3621 vocabulary). Default on: a
# red verdict whose every failed check matches an infra signature fails
# open instead of rejecting. "off" restores strict every-red-rejects.
# Anything unrecognised degrades to the default *and logs* — the typo
# direction here is strict → lenient, so it must not be silent.
INFRA_FAIL_OPEN_ENV_VAR = "EGG_PROPOSE_CHECK_GATE_INFRA_FAIL_OPEN"
_INFRA_FAIL_OPEN_ENABLED_VALUES = frozenset({"on", "1", "true", "yes"})
_INFRA_FAIL_OPEN_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})

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

_VERDICT_OUTPUT_TAIL_CHARS = 4000
# Smaller slice carried into the rejection envelope the producer reads.
# The full output stays reachable via the runner pod log, named by
# ``gate_id`` / ``pod`` in the envelope and every log line.
_REJECTION_OUTPUT_TAIL_CHARS = 1500

VERDICT_SENTINEL = "EGG_PROPOSE_CHECK_VERDICT:"

_GATE_ID_LABEL = "egg.io/propose-check-id"
_JOB_NAME_PREFIX = "egg-proposecheck-"

# Bound on the process-local verdict ledger. Entries are small (a few KB
# of output tails); the cap exists so a long-lived orchestrator serving
# many pipelines cannot grow it without limit.
_LEDGER_MAX_ENTRIES = 256

# A git object name and nothing else. ``ProposalPayload.commit_sha`` is
# an unconstrained agent-supplied string; anything that is not a hex
# object name cannot be a tree the runner could check out, so the gate
# declines rather than manufacturing a red out of a malformed field.
_COMMIT_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}")


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


# --------------------------------------------------------------------------
# Operator switches
# --------------------------------------------------------------------------


def propose_check_gate_mode() -> Literal["off", "log", "on"]:
    """Resolve ``EGG_PROPOSE_CHECK_GATE`` to ``off`` / ``log`` / ``on``.

    Unset resolves to ``_DEFAULT_MODE`` (``off``, see the module
    docstring's Rollout note). An unrecognised value also resolves to
    the default and logs a warning: strengthening a gate by typo is as
    much a surprise as weakening one.
    """
    raw = os.environ.get(GATE_ENV_VAR, "").strip().lower()
    if not raw:
        return _DEFAULT_MODE
    if raw in _ENABLED_VALUES:
        return "on"
    if raw in _LOG_ONLY_VALUES:
        return "log"
    if raw in _DISABLED_VALUES:
        return "off"
    logger.warning(
        "Unrecognised propose-check-gate switch value; falling back to the default mode",
        env_var=GATE_ENV_VAR,
        value=raw,
        mode=_DEFAULT_MODE,
    )
    return _DEFAULT_MODE


def _infra_fail_open_enabled() -> bool:
    """Resolve the infra-red fail-open switch (default on).

    Mirrors ``slice_green_gate._infra_fail_open_enabled`` exactly,
    including the loud degrade: a mistyped value resolves to the
    *lenient* posture, which must never be silent.
    """
    raw = os.environ.get(INFRA_FAIL_OPEN_ENV_VAR, "on").strip().lower()
    if not raw or raw in _INFRA_FAIL_OPEN_ENABLED_VALUES:
        return True
    if raw in _INFRA_FAIL_OPEN_DISABLED_VALUES:
        return False
    logger.warning(
        "Unrecognised propose-check-gate infra-fail-open switch value; "
        "falling back to the default (fail open on all-infra reds)",
        env_var=INFRA_FAIL_OPEN_ENV_VAR,
        value=raw,
        fail_open=True,
    )
    return True


def _gate_timeout_seconds() -> int:
    raw = os.environ.get(TIMEOUT_ENV_VAR, "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_TIMEOUT_SECONDS


def _gate_phases() -> set[str]:
    raw = os.environ.get(PHASES_ENV_VAR, _DEFAULT_PHASES)
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def gate_checks(repo: str) -> list[dict[str, str]]:
    """Return the checks this gate runs for ``repo``, in ground-truth form.

    Config-driven: exactly ``get_repo_checks(repo)`` minus the skip set,
    with each entry's command resolved to its ``full_command`` when the
    repo declares one (#3669) and to ``command`` otherwise. The returned
    entries carry the *resolved* ``command`` plus a ``narrowed`` flag
    recording whether the repo had a ground-truth form to offer, so the
    attestation can say which one ran.

    Returns ``[]`` — gate skips — when the repo configures no checks or
    config loading fails. A config problem must never reject a
    proposal.
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
            "Propose check gate skipped: failed to load repo checks config (#3669)",
            repo=repo,
            error=str(exc),
        )
        return []

    raw_skip = os.environ.get(SKIP_CHECKS_ENV_VAR, _DEFAULT_SKIP_CHECKS)
    skip = {name.strip().lower() for name in raw_skip.split(",") if name.strip()}

    resolved: list[dict[str, str]] = []
    for check in configured:
        if check["name"].strip().lower() in skip:
            continue
        full = check.get("full_command")
        resolved.append(
            {
                "name": check["name"],
                "command": full or check["command"],
                # "narrowed" is about *evidence quality*, not about the
                # command's content: the gate cannot know whether a repo's
                # ``command`` narrows, only whether the repo declared a
                # ground-truth form it could have run instead. Recording
                # the honest answer is the point — see the module
                # docstring's "Narrowed runs are not evidence".
                "narrowed": "false" if full else "unknown",
            }
        )
    return resolved


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


# --------------------------------------------------------------------------
# Verdict ledger
# --------------------------------------------------------------------------


@dataclass
class CheckRun:
    """One execution of the configured checks against one tree.

    Keyed by ``(pipeline_id, slice_id, commit_sha)``, **not** by role:
    the SHA determines the tree, so two producers proposing the same
    tree share one run and a retried propose never re-spawns a runner.
    That is the "runs ONCE per proposal" property, slightly stronger.
    """

    pipeline_id: str
    slice_id: str | None
    commit_sha: str
    base_branch: str
    repo: str
    checks: list[dict[str, str]]
    state: Literal["running", "passed", "failed", "infra"] = "running"
    verdict: dict[str, Any] | None = None
    failed: list[dict[str, Any]] = field(default_factory=list)
    infra_reason: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


_LEDGER: dict[tuple[str, str, str], CheckRun] = {}
_LEDGER_LOCK = threading.Lock()


def _ledger_key(pipeline_id: str, slice_id: str | None, commit_sha: str) -> tuple[str, str, str]:
    return (pipeline_id, slice_id or "", commit_sha)


def reset_ledger() -> None:
    """Drop every recorded run. Test seam; not called in production."""
    with _LEDGER_LOCK:
        _LEDGER.clear()


def _evict_locked() -> None:
    """Trim finished entries oldest-first once over the cap.

    Only finished entries are evictable: dropping a ``running`` record
    would let a second propose spawn a duplicate runner for a tree
    already in flight.
    """
    if len(_LEDGER) <= _LEDGER_MAX_ENTRIES:
        return
    finished = sorted(
        (k for k, v in _LEDGER.items() if v.state != "running"),
        key=lambda k: _LEDGER[k].finished_at or _LEDGER[k].started_at,
    )
    for key in finished[: len(_LEDGER) - _LEDGER_MAX_ENTRIES]:
        _LEDGER.pop(key, None)


def _record_verdict(
    record: CheckRun, verdict: dict[str, Any] | None, infra_reason: str | None
) -> None:
    """Fold a runner result into ``record``, applying infra classification.

    Uniformly — every red this gate produces is classified, there is no
    second run carrying untagged entries (#3621's asymmetry complaint).

    ``state`` is assigned **last** in every branch. The gate reads a
    record without taking the ledger lock (a propose must not queue
    behind a check run finishing), and ``state`` is what it branches on,
    so every field the chosen branch reads must already be in place when
    the state that selects it becomes visible.
    """
    with _LEDGER_LOCK:
        record.finished_at = time.time()
        if verdict is None:
            record.infra_reason = infra_reason or "runner produced no verdict"
            record.state = "infra"
            return
        record.verdict = verdict
        failed = [c for c in verdict.get("checks", []) if not c.get("ok")]
        if not failed:
            record.state = "passed"
            return
        genuine = failed
        if _infra_fail_open_enabled():
            infra_failed = [c for c in failed if c.get("infra")]
            genuine = [c for c in failed if not c.get("infra")]
            if infra_failed and not genuine:
                record.infra_reason = (
                    "every red check matched an infrastructure signature: "
                    + "; ".join(f"{c.get('name')}: {c.get('infra')}" for c in infra_failed)
                )
                record.state = "infra"
                return
        record.failed = genuine
        record.state = "failed"


def _run_and_record(record: CheckRun) -> None:
    """Background body: execute the checks and fold the result in.

    Never raises — a thread that dies with an exception would leave the
    record ``running`` forever and wedge every propose for that tree.
    """
    try:
        from kubernetes_spawner import get_kubernetes_spawner

        spawner = get_kubernetes_spawner()
        verdict, infra_reason = run_propose_checks(
            pipeline_id=record.pipeline_id,
            commit_sha=record.commit_sha,
            base_branch=record.base_branch,
            repo=record.repo,
            checks=record.checks,
            spawner=spawner,
        )
    except Exception as exc:  # noqa: BLE001 — any failure here is infrastructure
        verdict, infra_reason = None, f"propose check runner raised: {exc}"

    _record_verdict(record, verdict, infra_reason)
    logger.info(
        "Propose check verdict recorded (#3669)",
        pipeline_id=record.pipeline_id,
        slice_id=record.slice_id,
        commit_sha=record.commit_sha,
        state=record.state,
        failed_checks=[c.get("name") for c in record.failed] or None,
        infra_reason=record.infra_reason,
        duration_seconds=round((record.finished_at or time.time()) - record.started_at, 1),
    )


def _get_or_start_run(
    *,
    pipeline_id: str,
    slice_id: str | None,
    commit_sha: str,
    base_branch: str,
    repo: str,
    checks: list[dict[str, str]],
) -> CheckRun:
    """Return the ledger record for this tree, starting a run if absent."""
    key = _ledger_key(pipeline_id, slice_id, commit_sha)
    with _LEDGER_LOCK:
        existing = _LEDGER.get(key)
        if existing is not None:
            return existing
        record = CheckRun(
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            commit_sha=commit_sha,
            base_branch=base_branch,
            repo=repo,
            checks=checks,
        )
        _LEDGER[key] = record
        _evict_locked()

    logger.info(
        "Propose check gate: starting configured checks for proposed tree (#3669)",
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        commit_sha=commit_sha,
        base_branch=base_branch,
        commands=[c["command"] for c in checks],
    )
    threading.Thread(
        target=_run_and_record,
        args=(record,),
        name=f"propose-check-{commit_sha[:12]}",
        daemon=True,
    ).start()
    return record


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def checks_verified_attestation(record: CheckRun) -> dict[str, Any]:
    """Build the ``attestation.checks_verified`` block for a proposal.

    Records, per check, the **exact command** that ran and the **SHA it
    ran against** — the sub-requirement of #3669 that makes a narrowed
    run visibly narrow rather than silently incomplete. ``verified_by``
    is ``"system"`` so this can never be confused with an agent's
    self-report, which is what ``checks_passed`` has always been.

    Each entry also carries ``narrowed``, which is the honest answer to
    a question the gate genuinely cannot decide. ``"false"`` means the
    repo declared a ``full_command`` and the gate ran it. ``"unknown"``
    means it did not, so the gate ran the only form configured — which
    may be changeset-narrowed, and if so this evidence is *not* the
    full-suite claim it otherwise looks like. Recording "unknown"
    instead of asserting "full" is what keeps a repo that forgot to
    declare ``full_command`` from silently inheriting the stronger
    claim.
    """
    verdict = record.verdict or {}
    narrowed_by_name = {c.get("name"): c.get("narrowed", "unknown") for c in record.checks}
    return {
        "status": record.state,
        "verified_by": "system",
        "gate": "propose-check-gate (#3669)",
        # The SHA the runner actually had checked out, when it reported
        # one; the proposed SHA otherwise. They agree unless the runner
        # could not resolve HEAD, in which case the proposed SHA is the
        # honest claim.
        "commit_sha": verdict.get("commit_sha") or record.commit_sha,
        "gate_id": verdict.get("gate_id"),
        "checks": [
            {
                "name": c.get("name"),
                "command": c.get("command"),
                "ok": bool(c.get("ok")),
                "exit_code": c.get("exit_code"),
                "narrowed": narrowed_by_name.get(c.get("name"), "unknown"),
            }
            for c in verdict.get("checks", [])
        ],
        "infra_reason": record.infra_reason,
    }


def _pending_envelope(record: CheckRun) -> tuple[str, int, dict[str, Any]]:
    commands = ", ".join(c["command"] for c in record.checks)
    elapsed = int(time.time() - record.started_at)
    return (
        f"Propose deferred: the configured checks are running against your "
        f"proposed tree {record.commit_sha[:12]} ({commands}). Your proposal "
        f"has NOT been recorded and no reviewer has been dispatched — the "
        f"system runs these once per tree so reviewers never spend a round on "
        f"code that does not build (#3669). Wait and propose again with the "
        f"same commit_sha; the answer will be an acceptance or a named failing "
        f"check. Started {elapsed}s ago.",
        409,
        {
            "status": "checks_running",
            "commit_sha": record.commit_sha,
            "commands": [c["command"] for c in record.checks],
            "elapsed_seconds": elapsed,
        },
    )


def _red_envelope(record: CheckRun) -> tuple[str, int, dict[str, Any]]:
    names = ", ".join(str(c.get("name")) for c in record.failed)
    blocks = []
    for check in record.failed:
        tail = str(check.get("output_tail") or "")[-_REJECTION_OUTPUT_TAIL_CHARS:]
        blocks.append(
            f"[{check.get('name')}] `{check.get('command')}` exited "
            f"{check.get('exit_code')}:\n{tail}".strip()
        )
    verdict = record.verdict or {}
    return (
        f"Propose rejected: the configured checks are red at your proposed "
        f"commit {record.commit_sha[:12]}: {names}. Consensus cannot certify "
        f"code that does not build, so this proposal is not reviewable (#3669). "
        f"Fix the named checks, commit, push, and propose again.\n\n"
        + "\n\n".join(blocks)
        + (
            f"\n\nFull output: runner pod {verdict.get('pod')} "
            f"(gate {verdict.get('gate_id')}). Set "
            f"{GATE_ENV_VAR}=off to bypass the gate entirely."
        ),
        409,
        {
            "status": "checks_red",
            "commit_sha": record.commit_sha,
            "gate_id": verdict.get("gate_id"),
            "pod": verdict.get("pod"),
            "failed_checks": [
                {
                    "name": c.get("name"),
                    "command": c.get("command"),
                    "exit_code": c.get("exit_code"),
                    "output_tail": str(c.get("output_tail") or "")[-_REJECTION_OUTPUT_TAIL_CHARS:],
                }
                for c in record.failed
            ],
        },
    )


def propose_check_rejection(
    *,
    pipeline_id: str,
    repo: str,
    slice_id: str | None,
    producer_role: str,
    commit_sha: str,
    branch: str,
    current_phase: str | None,
    payload: dict[str, Any] | None = None,
) -> tuple[str, int, dict[str, Any]] | None:
    """Gate a ``CONSENSUS_PROPOSE`` on the configured checks (#3669).

    Returns ``None`` when the proposal may proceed, or a
    ``(message, status_code, details)`` rejection the caller renders.
    Called before the tracker records anything, so a rejected proposal
    mutates no consensus state and dispatches no reviewer.

    On a passing verdict the ``payload``'s attestation is stamped with
    ``checks_verified`` (see :func:`checks_verified_attestation`) so the
    recorded proposal carries the system's evidence — command and SHA —
    rather than only the producer's self-report.

    Fail-open (returns ``None``) on: gate off, a phase outside the
    configured set, a no-SHA propose, a non-hex SHA, no configured
    checks, an unresolvable branch, and an ``infra`` verdict.
    Fail-closed only on a verdict with at least one genuine red.
    """
    mode = propose_check_gate_mode()
    if mode == "off":
        return None
    if not commit_sha:
        # A no-op propose (#3027) carries no tree, so there is nothing
        # to check. The contract-completeness gate already prevents a
        # no-op from being an escape hatch.
        return None
    if not _COMMIT_SHA_RE.fullmatch(commit_sha):
        # ``commit_sha`` is an agent-supplied payload field and
        # ``ProposalPayload`` does not constrain its shape. The runner
        # only ever passes it to git in list form, so this is defence in
        # depth rather than the sole guard — but a value that cannot be
        # a commit is also a value the runner cannot check out, and
        # "could not run" is a fail-open, not a red.
        logger.warning(
            "Propose check gate skipped: commit_sha is not a hex object name (#3669)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            role=producer_role,
        )
        return None
    if (current_phase or "").strip().lower() not in _gate_phases():
        return None
    if not repo or not branch:
        logger.warning(
            "Propose check gate skipped: no repo/branch to resolve the proposed tree (#3669)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            repo=repo or None,
            branch=branch or None,
        )
        return None

    checks = gate_checks(repo)
    if not checks:
        logger.info(
            "Propose check gate skipped: no configured checks for repo (#3669)",
            pipeline_id=pipeline_id,
            repo=repo,
        )
        return None

    # Slice proposals are checked against the slice's integration
    # branch; phase-level ones against the pipeline branch. Either way
    # the runner detaches to ``commit_sha``, so the branch only has to
    # make the object reachable.
    base_branch = f"{branch}/{slice_id}" if slice_id else branch

    record = _get_or_start_run(
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        commit_sha=commit_sha,
        base_branch=base_branch,
        repo=repo,
        checks=checks,
    )

    if record.state == "running":
        if mode == "log":
            # Log mode must not change the flow at all: the run is now
            # in flight for its soak signal and the propose proceeds.
            return None
        return _pending_envelope(record)

    if record.state == "infra":
        # A check that could not run is not a check that passed, and it
        # is also not a check that failed. Proceed, loudly, and record
        # the honest status on the proposal.
        logger.warning(
            "Propose check gate failing open: checks could not run (#3669)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            role=producer_role,
            commit_sha=commit_sha,
            infra_reason=record.infra_reason,
            mode=mode,
        )
        _stamp_attestation(payload, record, producer_role)
        return None

    if record.state == "passed":
        logger.info(
            "Propose check gate passed (#3669)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            role=producer_role,
            commit_sha=commit_sha,
            commands=[c["command"] for c in checks],
        )
        _stamp_attestation(payload, record, producer_role)
        return None

    # state == "failed"
    logger.error(
        "Propose check gate red: configured checks failed at the proposed commit (#3669)",
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        role=producer_role,
        commit_sha=commit_sha,
        failed_checks=[c.get("name") for c in record.failed],
        mode=mode,
    )
    if mode == "log":
        _stamp_attestation(payload, record, producer_role)
        return None
    return _red_envelope(record)


def _stamp_attestation(
    payload: dict[str, Any] | None, record: CheckRun, producer_role: str = ""
) -> None:
    """Record the system's check evidence on the proposal payload.

    Two refusals, both about never turning a propose that would have
    succeeded into a 400:

    * A non-dict ``attestation`` is left exactly as it is. It is already
      malformed and ``handle_propose`` will fail on it; the failure
      should stay where it belongs rather than move here.
    * An **absent or empty** attestation is only created for a role that
      has a registered producer schema. ``handle_propose`` calls
      ``validate_attestation`` iff ``proposal.attestation`` is truthy,
      and that function *raises* for a role with no schema (the
      ``simplifier``, for one). Creating a dict to hold evidence would
      then reject an otherwise-valid proposal — evidence recording must
      never be load-bearing on acceptance. Such a role keeps its verdict
      in the structured log instead.
    """
    if not isinstance(payload, dict):
        return
    attestation = payload.get("attestation")
    if attestation is not None and not isinstance(attestation, dict):
        return
    if not attestation:
        try:
            from attestation_schemas import PRODUCER_ATTESTATION_MODELS
        except ImportError:  # pragma: no cover — packaging guard
            return
        if producer_role not in PRODUCER_ATTESTATION_MODELS:
            logger.info(
                "Propose check gate: verdict not stamped (role has no producer "
                "attestation schema); see the structured verdict log (#3669)",
                role=producer_role or None,
                commit_sha=record.commit_sha,
                state=record.state,
            )
            return
        attestation = {}
        payload["attestation"] = attestation
    attestation["checks_verified"] = checks_verified_attestation(record)
