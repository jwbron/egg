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

  Fail-open also covers a narrow class of infrastructure faults *inside*
  check execution (#3417): the runner tags each red check whose combined
  output contains one of the exact, high-confidence infra signatures (the
  sandbox git wrapper's gateway-down / missing-env / session-auth errors,
  the kernel's ENOSPC message) or whose process died by SIGKILL (the OOM
  killer). When *every* red check in a verdict is infra-tagged, the gate
  fails open with a loud warning instead of blocking; when genuine reds
  and infra-tagged reds mix, the gate blocks on the genuine reds only.
  Signature matching runs over the check's **full** output, not the
  truncated verdict tail, so an early gateway error can't scroll out of
  detection.

  This classification is security-relevant: the signatures are matched
  against untrusted check output, so a check that *prints* a signature
  while genuinely failing could fail itself open. Two guards keep that
  surface tight. First, the allowlist is a handful of exact strings
  emitted only by egg's own plumbing, never fuzzy patterns. Second, the
  git-wrapper signatures are matched **whole-line** (the stripped output
  line must *equal* the signature), not as a substring: this PR puts
  those literals into egg's own ``test_slice_green_gate.py``, so a
  genuine regression there would print one via pytest assertion
  introspection — always mid-line behind an ``E``/``assert``/diff-marker
  prefix — and whole-line matching rejects that, so the gate can't mask
  its own red regression (see ``_INFRA_LINE_SIGNATURES``). The one
  residual, accepted, hole is the SIGKILL arm: an OOM caused by the
  slice's *own* memory-explosion bug is indistinguishable from an infra
  OOM and fails open. ``EGG_SLICE_GREEN_GATE_INFRA_FAIL_OPEN=off``
  restores the strict every-red-blocks behavior.

Rollout is staged via ``EGG_SLICE_GREEN_GATE``: ``off`` → ``log`` (run
the checks, log the verdict loudly, never block) → ``on`` (**the
default**: block PR-open on a definitive red).

``on`` is the default rather than ``off`` because a gate nobody runs
verifies nothing: the switch shipped in #3398 defaulting to ``off`` and
was never set in any deployment, so the check-runner path had not
executed once in the ~3 weeks after it landed. #3602 is the shape that
costs — a contract task marked ``complete`` while five tests failed on
the slice tip — and it is a shape only a *blocking* gate prevents.

``log`` stays available for the deployment that wants verdicts without
the blocking decision, and it is the right posture for a fleet, where a
false red stalls a pipeline whose owner is not the person watching the
rollout. It is not the default because its evidence is **passive**: a
verdict is a structured log line, with no metric, audit event, or PR
comment behind it (#3623). ``log`` therefore only informs an operator
who goes looking, and this switch's own history is that nobody does.
Under ``on`` a wrong verdict announces itself on the next slice close,
to the operator who can act on it — which makes ``on`` the *better*
instrument for measuring the false-red rate, not merely the stricter
one.

Expect the first reds to be the gate's own wiring rather than the
slice's code, and note that each of these reds *every* slice close
until it is fixed: a stale contract snapshot on the slice tip can red
contract-hygiene tests for reasons unrelated to the slice (#3301), a
declared-but-missing prebuilt-deps snapshot exits the runner non-zero,
and ``make test``'s changeset narrowing derives its baseline from ``git
merge-base`` inside a fresh worktree, so it sees the cumulative slice
diff. Recovery is bounded and self-documenting: the failure message
names the branch to fix, the slice restarts, and
``EGG_SLICE_GREEN_GATE=off`` is quoted inline as the bypass. The slice's
commits stay on the integration branch through all of it — a red gate
withholds the PR, it does not discard work.

The latency cost is identical under ``log`` and ``on`` — both spawn the
runner and wait for the pod — so it is the price of *running* the gate,
not of blocking on it. Slice-close latency grows by the check duration
(bounded by ``EGG_SLICE_GREEN_GATE_TIMEOUT_SECONDS``, default 1800s,
plus the ``_POD_SCHEDULING_GRACE_SECONDS`` the orchestrator's wait adds
on top — see the next paragraph — after which the gate fails open).
``off`` remains available for deployments that cannot absorb that.

Whoever watches the rollout should know the worst case is **not** a slow
check suite: it is a runner pod that never schedules. ``_wait_for_runner_pod``
waits ``timeout + _POD_SCHEDULING_GRACE_SECONDS`` (~32 min at the defaults)
before failing open, so a capacity-starved cluster pays that in dead time on
*every* slice close, in every deployment, from the moment this default lands.

A *partially* delayed pod is the quieter half of the same problem, and on a
busy cluster the more common one. The runner's deadline is the **Job's**
``activeDeadlineSeconds``, counted from the Job's ``startTime`` — before any
pod is bound — so time spent Pending or pulling comes out of the check budget
rather than being added to it (``_POD_SCHEDULING_GRACE_SECONDS`` widens only
the orchestrator's wait). A pod delayed N seconds gets N fewer seconds to run
checks, and a ``DeadlineExceeded`` kill emits no verdict line, so the gate
fails open with no verdict at all. Capacity starvation therefore raises the
rate of *spurious no-verdict fail-opens* as well as dead time — silently
narrowing how much of the slice stream the gate actually covers.

Which log line an operator sees for that is not fixed, so grep for both: on
``DeadlineExceeded`` the Job controller *deletes* the active pod rather than
leaving it terminal, so ``_wait_for_runner_pod`` usually never observes
``Succeeded``/``Failed``, polls out its own (larger) budget, and the gate logs
"runner pod did not reach a terminal state". If a poll happens to catch the
pod reporting ``Failed`` mid-termination, the log read returns partial output
and "no parseable verdict from runner" fires instead. Both fail open, and
neither is distinguishable from a runner-harness crash. Tracked in #3622,
which this default makes the top follow-up: under ``on`` a missing verdict
is a slice close you believed was gated and wasn't. Note the direction — a
no-verdict fail-open can only *under*-block, never produce a false red, so
it is a coverage gap rather than a correctness risk.

Someone should watch the first wave directly rather than discovering the cost
from a slice-throughput drop later; ``off`` is the escape hatch.

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
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from egg_logging import get_logger

if TYPE_CHECKING:
    from kubernetes_spawner import KubernetesSpawner

logger = get_logger("orchestrator.slice_green_gate")

# Operator switch for the green gate. Three-state, default "on":
# "off" → gate skipped entirely; "log" → checks run and a red verdict is
# logged loudly but never blocks; "on"/unset → a red verdict blocks the
# slice PR from opening.
GREEN_GATE_ENV_VAR = "EGG_SLICE_GREEN_GATE"

_ENABLED_VALUES = frozenset({"on", "1", "true", "yes"})
_LOG_ONLY_VALUES = frozenset({"log", "log-only", "log_only"})
_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})

# Mode used when the switch is unset or carries a value we do not
# recognise. Weakening the gate takes an explicit, correctly-spelled
# _DISABLED_VALUES or _LOG_ONLY_VALUES entry: a typo must not leave a
# deployment with less verification than the product default. Note the
# earlier "a typo must not start blocking slices" half of this rule
# retired with the "log" default — under an "on" default, setting
# nothing at all blocks, so a mistyped value resolving to "on" is no
# stricter than the deployment an operator gets by doing nothing. This
# is also the same rule _infra_fail_open_enabled() applies (unrecognised
# → the default, loudly), so both switches in this module degrade
# alike.
_DEFAULT_MODE: Literal["off", "log", "on"] = "on"

# Comma-separated check *names* (from repositories.yaml ``checks``) the
# gate skips. Default skips ``security``: the full scan belongs on the
# context PR / terminal verification, not on every slice close.
GREEN_GATE_SKIP_CHECKS_ENV_VAR = "EGG_SLICE_GREEN_GATE_SKIP_CHECKS"
_DEFAULT_SKIP_CHECKS = "security"

# Operator switch for the #3417 infra-red fail-open. Default "on": a
# red verdict where every failed check matches an infra signature fails
# open instead of blocking. "off" (or 0/false/no) restores the strict
# pre-#3417 behavior where every red blocks. Any other value degrades
# to the default *and logs a warning*, matching green_gate_mode's typo
# posture on both the resolution and the signal: the typo direction
# here is strict -> lenient, so it must not be silent.
GREEN_GATE_INFRA_FAIL_OPEN_ENV_VAR = "EGG_SLICE_GREEN_GATE_INFRA_FAIL_OPEN"
# Deliberately separate from _ENABLED_VALUES / _DISABLED_VALUES above,
# not a missed dedup: this is an independent operator switch, and the
# two are free to diverge (e.g. if the mode switch grows a fourth state).
# The alias sets happening to be equal today is a coincidence to
# preserve, not a duplication to collapse.
_INFRA_FAIL_OPEN_ENABLED_VALUES = frozenset({"on", "1", "true", "yes"})
_INFRA_FAIL_OPEN_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})

# Exact output signatures that identify an infrastructure fault inside a
# check rather than a genuine failure (#3417). Security-relevant: matched
# against untrusted check output, so a check that prints one fails itself
# open. Keep the list to exact strings emitted only by egg's own plumbing
# (sandbox/scripts/git) or the kernel; never add fuzzy patterns like
# "Killed" or "connection refused" that real test output can legitimately
# contain. The two groups differ only in match *mode*:
#
# ``_INFRA_LINE_SIGNATURES`` are matched **whole-line** (a stripped
# output line must equal the signature), not as a substring. The sandbox
# git wrapper emits each as a bare ``echo`` line, so whole-line matching
# still catches the real fault — but it closes a self-masking hole the
# #3417 review flagged: this PR puts these exact literals into egg's own
# ``test_slice_green_gate.py``, so a *genuine* regression in a green-gate
# test would print one via pytest assertion introspection (``assert None
# == 'GATEWAY SIDECAR NOT AVAILABLE'``, a source-repr fixture literal, a
# unified-diff ``-`` line). Every such form embeds the signature mid-line
# behind an ``E ``/``assert``/``- ``/quote prefix, so whole-line matching
# rejects it — the gate can no longer tag its own red regression as infra
# and fail open (which would hide the very failure the gate exists to
# catch, including a break in this tagging logic itself).
_INFRA_LINE_SIGNATURES = (
    # sandbox/scripts/git: GATEWAY_URL was not wired into the runner pod.
    "ERROR: GATEWAY_URL environment variable is not set.",
    # sandbox/scripts/git show_gateway_unavailable(): the wrapper's
    # gateway health probe failed (gateway restart / network blip).
    # Emitted inside a banner with leading whitespace — whole-line
    # matching strips it before comparing.
    "GATEWAY SIDECAR NOT AVAILABLE",
    # sandbox/scripts/git: session token missing from the environment.
    "ERROR: EGG_SESSION_TOKEN not set. Session required for gateway access",
    # sandbox/scripts/git: gateway returned HTTP 401, i.e. a mid-run
    # session-token expiry or revocation.
    "Authentication failed - check session token",
)

# ``_INFRA_SUBSTRING_SIGNATURES`` are matched as a substring: the kernel
# ENOSPC strerror surfaces embedded in a larger message (``[Errno 28] No
# space left on device``) rather than on its own line, so whole-line
# matching would miss it. Disk pressure is infrastructure however it
# surfaces, and this string is far less likely than the git-wrapper lines
# to appear as a bare test literal (the #3417 review's own assessment —
# it called the four git-wrapper signatures the fragile ones and this arm
# robust), so keeping it substring-matched is a deliberate, narrow risk.
_INFRA_SUBSTRING_SIGNATURES = ("No space left on device",)

# Wall-clock budget for the runner pod (spawn-to-terminal). A slice's
# changeset-narrowed ``make test`` normally finishes well inside this;
# the ceiling exists so a hung suite degrades to fail-open instead of
# wedging the slice close.
GREEN_GATE_TIMEOUT_ENV_VAR = "EGG_SLICE_GREEN_GATE_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 1800

# Extra wall-clock the orchestrator's wait loop allows on top of the
# in-pod check budget, to absorb pod scheduling + image-pull latency.
# The wait clock starts at Job submit, before the pod is scheduled or
# pulled — so on a cold node that latency would otherwise be charged to
# the orchestrator's own timeout and trip a spurious fail-open even when
# the checks would have passed.
#
# The grace widens the *orchestrator's* wait only; it does not protect
# the check budget. The deadline this module sets is the **Job's**
# ``activeDeadlineSeconds`` (``spec.activeDeadlineSeconds``, see
# ``_build_runner_job_manifest`` / ``_submit_runner_job``), which Kubernetes
# counts from the Job's ``status.startTime`` — set by the controller
# *before* any pod is bound. ``PodSpec.activeDeadlineSeconds``, the field
# the kubelet would count from pod start on the node, is never set. So
# scheduling and image-pull time count against the deadline: a pod that
# waits N seconds for capacity gets N fewer seconds to run checks, and
# with ``backoffLimit: 0`` / ``restartPolicy: Never`` a ``DeadlineExceeded``
# kill prints no verdict line at all, so the gate fails open with no
# verdict. It fails open via one of *two* branches, depending on timing:
# the Job controller deletes the active pod on ``DeadlineExceeded``
# rather than leaving it terminal, so ``_wait_for_runner_pod`` normally
# never sees ``Succeeded``/``Failed`` and times out ("runner pod did not
# reach a terminal state"); a poll that catches the pod reporting
# ``Failed`` mid-termination instead reads partial output and lands on
# "no parseable verdict from runner". Tracked in #3622; text here
# describes what the code does today, not what it should do.
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
# Infra classification (#3417) happens here, runner-side, because only
# the runner sees a check's *full* output: the verdict carries a
# truncated tail, and an early gateway error (e.g. the test selector's
# first git call failing) could scroll out of it. Each red check gets
# an ``infra`` field: the matched signature string, a SIGKILL note, or
# None for a genuine failure. The orchestrator decides what to do with
# the tags; the runner only reports.
_RUNNER_PROGRAM = """
import json, os, shutil, subprocess, sys, time

checks = json.loads(os.environ["EGG_GREEN_GATE_CHECKS"])
repo_dir = os.environ["EGG_GREEN_GATE_REPO_DIR"]
tail = int(os.environ.get("EGG_GREEN_GATE_OUTPUT_TAIL", "4000"))
infra_signatures = json.loads(os.environ.get("EGG_GREEN_GATE_INFRA_SIGNATURES", "{}"))
infra_line_signatures = infra_signatures.get("line", [])
infra_substring_signatures = infra_signatures.get("substring", [])


def classify_infra(rc, out):
    # A SIGKILLed check (rc -9 when bash itself dies, 137 when bash
    # reports a killed child) is the OOM killer: no test runner signals
    # failure via SIGKILL. Caveat (#3417 review): an OOM caused by the
    # *slice's own* code — a memory-explosion bug in the code under test —
    # is indistinguishable here from an infra OOM and is accepted as
    # fail-open; the SIGKILL arm is the broad one. A pod-deadline kill
    # takes down the runner (PID 1 python), not a check subprocess, so it
    # never surfaces as a per-check 137 — that case fails open via the
    # orchestrator's missing-verdict path, not here.
    if rc in (-9, 137):
        return "check process died by SIGKILL (exit %s): OOM killer" % rc
    # Whole-line match for the git-wrapper signatures: they are emitted as
    # standalone lines, so requiring the full stripped line to equal the
    # signature (not a substring) keeps a check that merely *prints* the
    # literal mid-line — e.g. pytest assertion introspection of egg's own
    # green-gate tests — from tagging itself infra and failing its own red
    # open (#3417 review).
    stripped_lines = None
    for sig in infra_line_signatures:
        if stripped_lines is None:
            stripped_lines = {ln.strip() for ln in out.splitlines()}
        if sig in stripped_lines:
            return sig
    # Substring match for the kernel ENOSPC strerror, which surfaces
    # embedded in a larger message rather than on its own line.
    for sig in infra_substring_signatures:
        if sig in out:
            return sig
    return None


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

results = []
for check in checks:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["bash", "-c", check["command"]],
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        rc, out = proc.returncode, proc.stdout or ""
    except Exception as exc:
        rc, out = -1, f"runner failed to execute check: {exc}"
    results.append(
        {
            "name": check["name"],
            "ok": rc == 0,
            "exit_code": rc,
            "duration_seconds": round(time.monotonic() - started, 1),
            "output_tail": out[-tail:],
            "infra": classify_infra(rc, out) if rc != 0 else None,
        }
    )

print("EGG_GREEN_GATE_VERDICT:" + json.dumps({"checks": results}), flush=True)
"""


def green_gate_mode() -> Literal["off", "log", "on"]:
    """Resolve the operator switch to one of ``off`` / ``log`` / ``on``.

    Unset resolves to ``_DEFAULT_MODE`` (``on``). Weakening the gate
    requires an explicit, correctly-spelled value — ``off`` / ``0`` /
    ``false`` / ``no`` to skip it, ``log`` / ``log-only`` / ``log_only``
    to run it without blocking. Any other value resolves to ``on`` and
    logs a warning, so a typo cannot silently drop the deployment below
    the product default.
    """
    raw = os.environ.get(GREEN_GATE_ENV_VAR, "").strip().lower()
    if not raw:
        return _DEFAULT_MODE
    if raw in _ENABLED_VALUES:
        return "on"
    if raw in _LOG_ONLY_VALUES:
        return "log"
    if raw in _DISABLED_VALUES:
        return "off"
    logger.warning(
        "Unrecognised green-gate switch value; falling back to the default mode",
        env_var=GREEN_GATE_ENV_VAR,
        value=raw,
        mode=_DEFAULT_MODE,
    )
    return _DEFAULT_MODE


def _infra_fail_open_enabled() -> bool:
    """Resolve the #3417 infra-red fail-open switch (default on).

    Only the exact disabled values turn it off; anything else degrades
    to the default *and logs a warning*. Mirrors ``green_gate_mode``'s
    posture on both counts: an operator typo resolves to the documented
    default behavior, and it never does so silently. The warning matters
    more here than on the mode switch, because the two typos point in
    opposite directions. A mistyped ``EGG_SLICE_GREEN_GATE`` resolves to
    ``on``, the strictest mode, so it can only over-verify. A mistyped
    value here resolves to fail-open, so an operator reaching for ``off``
    and typing ``offf`` gets the *lenient* posture — silently, without
    the warning.
    """
    raw = os.environ.get(GREEN_GATE_INFRA_FAIL_OPEN_ENV_VAR, "on").strip().lower()
    if not raw or raw in _INFRA_FAIL_OPEN_ENABLED_VALUES:
        return True
    if raw in _INFRA_FAIL_OPEN_DISABLED_VALUES:
        return False
    logger.warning(
        "Unrecognised green-gate infra-fail-open switch value; "
        "falling back to the default (fail open on all-infra reds)",
        env_var=GREEN_GATE_INFRA_FAIL_OPEN_ENV_VAR,
        value=raw,
        # Structured resolved value, mirroring green_gate_mode's
        # ``mode=`` kwarg, so both typo warnings are greppable the
        # same way rather than carrying the resolution in prose only.
        fail_open=True,
    )
    return True


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
    full_env["EGG_GREEN_GATE_INFRA_SIGNATURES"] = json.dumps(
        {
            "line": list(_INFRA_LINE_SIGNATURES),
            "substring": list(_INFRA_SUBSTRING_SIGNATURES),
        }
    )

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
            # Job-level deadline: counted from the Job's ``startTime``,
            # i.e. from before the pod is bound, so scheduling and pull
            # latency come out of the check budget (#3622 — see
            # ``_POD_SCHEDULING_GRACE_SECONDS``). It bounds a hung check
            # rather than sizing it; the orchestrator's wait loop is the
            # outer ceiling.
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
    """Convert the manifest dict to V1 objects and create the Job.

    Every field is read *from the manifest* rather than restated here.
    Three of them used to be hardcoded (``automountServiceAccountToken``,
    ``allowPrivilegeEscalation``, ``capabilities.drop``) while the
    manifest also declared them: the values agreed, so there was no live
    bug, but the dict was not the source of truth it looks like, and a
    test asserting on the submitted body could not tell the two apart.
    Keep new fields flowing through the dict —
    ``test_every_manifest_field_reaches_the_body`` fails on any manifest
    key this function does not copy.
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
    close side effect. Returns ``None`` when the slice may close: checks
    green, gate off/log-mode, an infrastructure failure (fail-open), or
    a red verdict where every failed check carries an infra tag (#3417).
    Otherwise returns a human-readable failure string naming the
    genuinely red checks; the caller records the slice failure with it,
    routing through the existing cascade + OVERSEER_ALERT machinery
    instead of opening a red PR.

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
        for host_path in wt_result.worktrees.values():
            container_path = f"/home/egg/repos/{os.path.basename(host_path)}"
            repo_mounts[container_path] = host_path
            repo_dir = container_path

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
            # never a *check* failure), unparseable output, and a Job
            # ``activeDeadlineSeconds`` kill caught mid-termination, where
            # the checks may have been about to pass and the budget was
            # simply cut short by scheduling delay (#3622 — see
            # ``_POD_SCHEDULING_GRACE_SECONDS``). A deadline kill more
            # often lands on the "did not reach a terminal state" branch
            # above, since the Job controller deletes the pod; check both
            # when diagnosing a missing verdict.
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

        # Infra-red fail-open (#3417): a red check the runner tagged with
        # an infra signature is an infrastructure fault inside check
        # execution, not a verdict on the slice. Fail open when every red
        # is infra-tagged; when genuine and infra reds mix, block on the
        # genuine reds only so the failure routed to the cascade doesn't
        # send anyone chasing an infra ghost.
        genuine_failed = failed
        if _infra_fail_open_enabled():
            infra_failed = [c for c in failed if c.get("infra")]
            genuine_failed = [c for c in failed if not c.get("infra")]
            if infra_failed:
                logger.warning(
                    "Green gate: red checks match infrastructure signatures (#3417)",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    gate_id=gate_id,
                    infra_checks={str(c.get("name")): str(c.get("infra")) for c in infra_failed},
                )
            if not genuine_failed:
                logger.warning(
                    "Green gate skipped: every red check is infra-induced, failing open (#3417)",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    gate_id=gate_id,
                    integration_branch=integration_branch,
                    mode=mode,
                )
                return None

        failed_names = ", ".join(str(c.get("name")) for c in genuine_failed)
        logger.error(
            "Green gate red: configured checks failed at the slice tip (#3398)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            gate_id=gate_id,
            integration_branch=integration_branch,
            failed_checks=failed_names,
            mode=mode,
        )
        if mode == "log":
            return None
        return (
            f"slice {slice_id}: green gate failed — configured checks are red "
            f"at integration branch {integration_branch} tip: {failed_names}.\n\n"
            f"{_format_failed_checks(genuine_failed)}\n\n"
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
