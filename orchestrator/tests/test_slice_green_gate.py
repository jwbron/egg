"""Tests for the per-slice green gate (#3398).

Covers:

* ``green_gate_mode`` — the three-state operator switch (off default,
  log soak mode, on; unknown values degrade to off).
* ``_gate_checks`` / ``_repo_requires_prebuilt`` — config-driven check
  selection (skip set, default ``security``) and the prebuilt-toolchain
  requirement derived from ``build_commands.persist_dirs``.
* ``parse_verdict`` — sentinel-line extraction from noisy pod logs.
* ``_RUNNER_PROGRAM`` — executed for real in a subprocess: check
  execution + verdict shape, output tails, the prebuilt-deps restore
  (copy-if-missing), the required-but-missing infra exit, and the
  #3417 infra tagging (signature match over full output, SIGKILL exit,
  green checks never tagged).
* ``_build_runner_job_manifest`` — labels (NetworkPolicy component
  label present; monitor/agent-supervision labels absent), env, mounts,
  deadline.
* ``run_slice_green_gate`` — gate wiring: kill switch, fail-open on
  every infrastructure failure (worktree, session, submit, timeout,
  unparseable verdict), fail-closed only on a definitive red verdict,
  log-mode never blocking, cleanup (job/session/worktree) on every
  path that created the resource, and the #3417 infra-red fail-open
  (all-infra reds fail open, mixed reds block on the genuine ones,
  ``EGG_SLICE_GREEN_GATE_INFRA_FAIL_OPEN=off`` restores strict
  blocking).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))
_shared_path = _orchestrator_path.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

import slice_green_gate as sgg  # noqa: E402
from egg_config.constants import TEST_GATEWAY_PORT  # noqa: E402
from gateway_client import SessionInfo, WorktreeResult  # noqa: E402

PIPELINE_ID = "pipeline-green-gate-test"
SLICE_ID = "slice-2"
INTEGRATION_BRANCH = "egg/issue-3398/work/slice-2"
REPO = "jwbron/egg"

CHECKS = [
    {"name": "lint", "command": "make lint"},
    {"name": "test", "command": "make test"},
]
CHECKS_WITH_SECURITY = [*CHECKS, {"name": "security", "command": "make security"}]


@pytest.fixture
def gate_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for var in (
        sgg.GREEN_GATE_ENV_VAR,
        sgg.GREEN_GATE_SKIP_CHECKS_ENV_VAR,
        sgg.GREEN_GATE_TIMEOUT_ENV_VAR,
        sgg.GREEN_GATE_INFRA_FAIL_OPEN_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# ----------------------------------------------------------------------
# green_gate_mode
# ----------------------------------------------------------------------


class TestGreenGateMode:
    def test_default_is_off(self, gate_env: pytest.MonkeyPatch) -> None:
        assert sgg.green_gate_mode() == "off"

    @pytest.mark.parametrize("value", ["on", "ON", " true ", "1", "yes"])
    def test_enabled_values(self, gate_env: pytest.MonkeyPatch, value: str) -> None:
        gate_env.setenv(sgg.GREEN_GATE_ENV_VAR, value)
        assert sgg.green_gate_mode() == "on"

    @pytest.mark.parametrize("value", ["log", "LOG", "log-only", "log_only"])
    def test_log_values(self, gate_env: pytest.MonkeyPatch, value: str) -> None:
        gate_env.setenv(sgg.GREEN_GATE_ENV_VAR, value)
        assert sgg.green_gate_mode() == "log"

    @pytest.mark.parametrize("value", ["off", "0", "false", "", "banana", "enabled"])
    def test_everything_else_is_off(self, gate_env: pytest.MonkeyPatch, value: str) -> None:
        gate_env.setenv(sgg.GREEN_GATE_ENV_VAR, value)
        assert sgg.green_gate_mode() == "off"


# ----------------------------------------------------------------------
# _gate_checks / _repo_requires_prebuilt / _gate_timeout_seconds
# ----------------------------------------------------------------------


class TestGateChecks:
    def test_default_skips_security(self, gate_env: pytest.MonkeyPatch) -> None:
        with patch("config.repo_config.get_repo_checks", return_value=CHECKS_WITH_SECURITY):
            assert sgg._gate_checks(REPO) == CHECKS

    def test_custom_skip_set(self, gate_env: pytest.MonkeyPatch) -> None:
        gate_env.setenv(sgg.GREEN_GATE_SKIP_CHECKS_ENV_VAR, "security, TEST")
        with patch("config.repo_config.get_repo_checks", return_value=CHECKS_WITH_SECURITY):
            assert sgg._gate_checks(REPO) == [{"name": "lint", "command": "make lint"}]

    def test_empty_skip_env_runs_everything(self, gate_env: pytest.MonkeyPatch) -> None:
        gate_env.setenv(sgg.GREEN_GATE_SKIP_CHECKS_ENV_VAR, "")
        with patch("config.repo_config.get_repo_checks", return_value=CHECKS_WITH_SECURITY):
            assert sgg._gate_checks(REPO) == CHECKS_WITH_SECURITY

    def test_no_configured_checks(self, gate_env: pytest.MonkeyPatch) -> None:
        with patch("config.repo_config.get_repo_checks", return_value=[]):
            assert sgg._gate_checks(REPO) == []

    def test_config_error_fails_open(self, gate_env: pytest.MonkeyPatch) -> None:
        with patch("config.repo_config.get_repo_checks", side_effect=RuntimeError("boom")):
            assert sgg._gate_checks(REPO) == []


class TestRepoRequiresPrebuilt:
    def test_persist_dirs_requires(self) -> None:
        with patch(
            "config.repo_config.get_repo_build_commands",
            return_value={"persist_dirs": [".venv"]},
        ):
            assert sgg._repo_requires_prebuilt(REPO) is True

    def test_no_build_commands(self) -> None:
        with patch("config.repo_config.get_repo_build_commands", return_value={}):
            assert sgg._repo_requires_prebuilt(REPO) is False

    def test_empty_persist_dirs(self) -> None:
        with patch(
            "config.repo_config.get_repo_build_commands",
            return_value={"persist_dirs": [], "commands": ["npm ci"]},
        ):
            assert sgg._repo_requires_prebuilt(REPO) is False

    def test_config_error_fails_open(self) -> None:
        with patch(
            "config.repo_config.get_repo_build_commands",
            side_effect=RuntimeError("boom"),
        ):
            assert sgg._repo_requires_prebuilt(REPO) is False


class TestInfraFailOpenEnabled:
    def test_default_is_on(self, gate_env: pytest.MonkeyPatch) -> None:
        assert sgg._infra_fail_open_enabled() is True

    @pytest.mark.parametrize("value", ["off", "OFF", " 0 ", "false", "no"])
    def test_disabled_values(self, gate_env: pytest.MonkeyPatch, value: str) -> None:
        gate_env.setenv(sgg.GREEN_GATE_INFRA_FAIL_OPEN_ENV_VAR, value)
        assert sgg._infra_fail_open_enabled() is False

    @pytest.mark.parametrize("value", ["on", "1", "true", "", "banana"])
    def test_everything_else_is_on(self, gate_env: pytest.MonkeyPatch, value: str) -> None:
        gate_env.setenv(sgg.GREEN_GATE_INFRA_FAIL_OPEN_ENV_VAR, value)
        assert sgg._infra_fail_open_enabled() is True


class TestGateTimeout:
    def test_default(self, gate_env: pytest.MonkeyPatch) -> None:
        assert sgg._gate_timeout_seconds() == sgg._DEFAULT_TIMEOUT_SECONDS

    def test_override(self, gate_env: pytest.MonkeyPatch) -> None:
        gate_env.setenv(sgg.GREEN_GATE_TIMEOUT_ENV_VAR, "600")
        assert sgg._gate_timeout_seconds() == 600

    @pytest.mark.parametrize("value", ["-5", "0", "soon", ""])
    def test_invalid_falls_back(self, gate_env: pytest.MonkeyPatch, value: str) -> None:
        gate_env.setenv(sgg.GREEN_GATE_TIMEOUT_ENV_VAR, value)
        assert sgg._gate_timeout_seconds() == sgg._DEFAULT_TIMEOUT_SECONDS


# ----------------------------------------------------------------------
# parse_verdict
# ----------------------------------------------------------------------


def _verdict_line(checks: list[dict[str, Any]]) -> str:
    return sgg.VERDICT_SENTINEL + json.dumps({"checks": checks})


class TestParseVerdict:
    def test_parses_sentinel_line(self) -> None:
        log = "make output\nmore output\n" + _verdict_line([{"name": "lint", "ok": True}])
        verdict = sgg.parse_verdict(log)
        assert verdict is not None
        assert verdict["checks"][0]["name"] == "lint"

    def test_json_shaped_check_output_is_not_the_verdict(self) -> None:
        log = (
            _verdict_line([{"name": "lint", "ok": False}])
            + "\n"
            + json.dumps({"checks": [{"name": "lint", "ok": True}]})
        )
        verdict = sgg.parse_verdict(log)
        assert verdict is not None
        assert verdict["checks"][0]["ok"] is False

    def test_empty_log(self) -> None:
        assert sgg.parse_verdict("") is None

    def test_no_sentinel(self) -> None:
        assert sgg.parse_verdict("ruff.....ok\nall tests passed\n") is None

    def test_malformed_json_after_sentinel(self) -> None:
        assert sgg.parse_verdict(sgg.VERDICT_SENTINEL + "{not json") is None

    def test_verdict_without_checks_list(self) -> None:
        assert sgg.parse_verdict(sgg.VERDICT_SENTINEL + json.dumps({"ok": True})) is None


# ----------------------------------------------------------------------
# _RUNNER_PROGRAM — executed for real in a subprocess
# ----------------------------------------------------------------------


def _run_runner(
    tmp_path: Path,
    checks: list[dict[str, str]],
    *,
    require_prebuilt: str = "0",
    prebuilt_base: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    repo_dir = tmp_path / "egg"
    repo_dir.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "EGG_GREEN_GATE_CHECKS": json.dumps(checks),
            "EGG_GREEN_GATE_REPO_DIR": str(repo_dir),
            "EGG_GREEN_GATE_OUTPUT_TAIL": "200",
            "EGG_GREEN_GATE_INFRA_SIGNATURES": json.dumps(
                {
                    "line": list(sgg._INFRA_LINE_SIGNATURES),
                    "substring": list(sgg._INFRA_SUBSTRING_SIGNATURES),
                }
            ),
            "EGG_GREEN_GATE_REQUIRE_PREBUILT": require_prebuilt,
            "EGG_GREEN_GATE_PREBUILT_BASE": str(
                prebuilt_base if prebuilt_base is not None else tmp_path / "no-prebuilt"
            ),
        }
    )
    return subprocess.run(
        [sys.executable, "-c", sgg._RUNNER_PROGRAM],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


class TestRunnerProgram:
    def test_green_and_red_checks(self, tmp_path: Path) -> None:
        proc = _run_runner(
            tmp_path,
            [
                {"name": "ok-check", "command": "echo all good"},
                {"name": "red-check", "command": "echo boom; exit 3"},
            ],
        )
        assert proc.returncode == 0
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        by_name = {c["name"]: c for c in verdict["checks"]}
        assert by_name["ok-check"]["ok"] is True
        assert by_name["ok-check"]["exit_code"] == 0
        assert "all good" in by_name["ok-check"]["output_tail"]
        assert by_name["red-check"]["ok"] is False
        assert by_name["red-check"]["exit_code"] == 3
        assert "boom" in by_name["red-check"]["output_tail"]

    def test_output_tail_is_capped(self, tmp_path: Path) -> None:
        proc = _run_runner(
            tmp_path,
            [{"name": "noisy", "command": "yes filler | head -n 2000; true"}],
        )
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        assert len(verdict["checks"][0]["output_tail"]) <= 200

    def test_checks_run_in_repo_dir(self, tmp_path: Path) -> None:
        (tmp_path / "egg").mkdir(exist_ok=True)
        (tmp_path / "egg" / "marker.txt").write_text("present")
        proc = _run_runner(tmp_path, [{"name": "cwd", "command": "cat marker.txt"}])
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        assert "present" in verdict["checks"][0]["output_tail"]

    def test_red_check_with_infra_signature_is_tagged(self, tmp_path: Path) -> None:
        proc = _run_runner(
            tmp_path,
            [
                {
                    "name": "infra-red",
                    "command": "echo 'GATEWAY SIDECAR NOT AVAILABLE'; exit 1",
                }
            ],
        )
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        check = verdict["checks"][0]
        assert check["ok"] is False
        assert check["infra"] == "GATEWAY SIDECAR NOT AVAILABLE"

    def test_genuine_red_check_is_not_tagged(self, tmp_path: Path) -> None:
        proc = _run_runner(
            tmp_path,
            [{"name": "genuine-red", "command": "echo 'FAILED test_x'; exit 2"}],
        )
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        assert verdict["checks"][0]["infra"] is None

    def test_green_check_never_tagged_even_with_signature_output(self, tmp_path: Path) -> None:
        proc = _run_runner(
            tmp_path,
            [
                {
                    "name": "green-with-noise",
                    "command": "echo 'GATEWAY SIDECAR NOT AVAILABLE'; exit 0",
                }
            ],
        )
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        check = verdict["checks"][0]
        assert check["ok"] is True
        assert check["infra"] is None

    def test_sigkilled_check_is_tagged(self, tmp_path: Path) -> None:
        # bash kills itself with SIGKILL, so subprocess reports rc -9:
        # the same shape as an OOM kill of the check shell.
        proc = _run_runner(tmp_path, [{"name": "oom", "command": "kill -9 $$"}])
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        check = verdict["checks"][0]
        assert check["ok"] is False
        assert check["infra"] is not None
        assert "SIGKILL" in check["infra"]

    def test_signature_scrolled_out_of_tail_is_still_detected(self, tmp_path: Path) -> None:
        # The infra error appears early, then enough output follows to
        # push it past the 200-char tail. Detection must run over the
        # full output, not the truncated tail (#3417).
        proc = _run_runner(
            tmp_path,
            [
                {
                    "name": "early-infra",
                    "command": (
                        "echo 'GATEWAY SIDECAR NOT AVAILABLE'; yes filler-line | head -50; exit 1"
                    ),
                }
            ],
        )
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        check = verdict["checks"][0]
        assert "GATEWAY SIDECAR NOT AVAILABLE" not in check["output_tail"]
        assert check["infra"] == "GATEWAY SIDECAR NOT AVAILABLE"

    def test_signature_printed_midline_is_not_tagged(self, tmp_path: Path) -> None:
        # The #3417-review self-masking guard: egg's own green-gate tests
        # contain these literals, so a genuine regression prints them via
        # pytest assertion introspection — always mid-line, behind an
        # ``E``/``assert``/quote prefix. Whole-line matching must NOT tag
        # such output as infra, or the gate would fail its own red open.
        proc = _run_runner(
            tmp_path,
            [
                {
                    "name": "regressed-green-gate-test",
                    "command": (
                        "echo \"E       assert None == 'GATEWAY SIDECAR NOT AVAILABLE'\"; exit 1"
                    ),
                }
            ],
        )
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        check = verdict["checks"][0]
        assert check["ok"] is False
        assert check["infra"] is None

    def test_indented_signature_line_is_tagged(self, tmp_path: Path) -> None:
        # sandbox/scripts/git emits GATEWAY SIDECAR NOT AVAILABLE inside a
        # banner with leading whitespace; whole-line matching strips the
        # line before comparing, so the real fault still tags.
        proc = _run_runner(
            tmp_path,
            [
                {
                    "name": "infra-red-banner",
                    "command": "printf '  GATEWAY SIDECAR NOT AVAILABLE\\n'; exit 1",
                }
            ],
        )
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        assert verdict["checks"][0]["infra"] == "GATEWAY SIDECAR NOT AVAILABLE"

    def test_enospc_is_tagged_as_substring_midline(self, tmp_path: Path) -> None:
        # ENOSPC surfaces embedded in a larger strerror, so it matches as
        # a substring (not whole-line) — disk pressure is infra either way
        # (#3417 review).
        proc = _run_runner(
            tmp_path,
            [
                {
                    "name": "disk-red",
                    "command": "echo 'OSError: [Errno 28] No space left on device'; exit 1",
                }
            ],
        )
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        assert verdict["checks"][0]["infra"] == "No space left on device"

    def test_prebuilt_restore_copy_if_missing(self, tmp_path: Path) -> None:
        prebuilt = tmp_path / "prebuilt" / "jwbron--egg"
        (prebuilt / ".venv" / "bin").mkdir(parents=True)
        (prebuilt / ".venv" / "bin" / "tool").write_text("prebuilt tool")
        (prebuilt / "existing.txt").write_text("prebuilt version")

        repo_dir = tmp_path / "egg"
        repo_dir.mkdir()
        (repo_dir / "existing.txt").write_text("worktree version")

        proc = _run_runner(
            tmp_path,
            [{"name": "check", "command": "cat .venv/bin/tool existing.txt"}],
            require_prebuilt="1",
            prebuilt_base=tmp_path / "prebuilt",
        )
        assert proc.returncode == 0
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        tail = verdict["checks"][0]["output_tail"]
        assert "prebuilt tool" in tail
        # copy-if-missing: worktree files are never clobbered.
        assert "worktree version" in tail

    def test_required_prebuilt_missing_is_infra_exit(self, tmp_path: Path) -> None:
        proc = _run_runner(
            tmp_path,
            [{"name": "check", "command": "true"}],
            require_prebuilt="1",
        )
        assert proc.returncode == 1
        assert sgg.parse_verdict(proc.stdout) is None
        assert "prebuilt" in proc.stderr

    def test_optional_prebuilt_missing_proceeds(self, tmp_path: Path) -> None:
        proc = _run_runner(
            tmp_path,
            [{"name": "check", "command": "true"}],
            require_prebuilt="0",
        )
        assert proc.returncode == 0
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        assert verdict["checks"][0]["ok"] is True


# ----------------------------------------------------------------------
# _build_runner_job_manifest
# ----------------------------------------------------------------------


def _manifest(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "gate_id": "abc123def456",
        "pipeline_id": PIPELINE_ID,
        "slice_id": SLICE_ID,
        "image": "egg:latest",
        "checks": CHECKS,
        "repo_mounts": {"/home/egg/repos/egg": "/home/host/.egg-worktrees/x/egg"},
        "repo_dir": "/home/egg/repos/egg",
        "env": {
            "GATEWAY_URL": f"http://gw:{TEST_GATEWAY_PORT}",
            "EGG_GREEN_GATE_REQUIRE_PREBUILT": "1",
        },
        "timeout_seconds": 1800,
        "host_uid": 1000,
        "host_gid": 1000,
    }
    kwargs.update(overrides)
    return sgg._build_runner_job_manifest(**kwargs)


class TestBuildRunnerJobManifest:
    def test_network_policy_label_present(self) -> None:
        manifest = _manifest()
        for labels in (
            manifest["metadata"]["labels"],
            manifest["spec"]["template"]["metadata"]["labels"],
        ):
            assert labels["app.kubernetes.io/component"] == "agent"
            assert labels[sgg._GATE_ID_LABEL] == "abc123def456"

    def test_no_agent_supervision_labels(self) -> None:
        # The runner is infrastructure, not an agent: it must not carry
        # the labels the monitor / running-agent views / list_slice_jobs
        # enumerate, or it would surface in supervision and trip
        # heartbeat-silence tripwires.
        manifest = _manifest()
        all_labels = {
            **manifest["metadata"]["labels"],
            **manifest["spec"]["template"]["metadata"]["labels"],
        }
        for forbidden in ("egg.orchestrator", "egg.agent.role", "egg.slice.id"):
            assert forbidden not in all_labels

    def test_env_carries_infra_signatures(self) -> None:
        manifest = _manifest()
        env = {
            e["name"]: e["value"]
            for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert json.loads(env["EGG_GREEN_GATE_INFRA_SIGNATURES"]) == {
            "line": list(sgg._INFRA_LINE_SIGNATURES),
            "substring": list(sgg._INFRA_SUBSTRING_SIGNATURES),
        }

    def test_env_carries_checks_and_repo_dir(self) -> None:
        manifest = _manifest()
        env = {
            e["name"]: e["value"]
            for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert json.loads(env["EGG_GREEN_GATE_CHECKS"]) == CHECKS
        assert env["EGG_GREEN_GATE_REPO_DIR"] == "/home/egg/repos/egg"
        assert env["EGG_GREEN_GATE_REQUIRE_PREBUILT"] == "1"
        assert env["GATEWAY_URL"] == f"http://gw:{TEST_GATEWAY_PORT}"

    def test_worktree_mount(self) -> None:
        manifest = _manifest()
        pod = manifest["spec"]["template"]["spec"]
        assert pod["volumes"][0]["hostPath"]["path"] == "/home/host/.egg-worktrees/x/egg"
        mount = pod["containers"][0]["volumeMounts"][0]
        assert mount["mountPath"] == "/home/egg/repos/egg"
        assert mount["name"] == pod["volumes"][0]["name"]

    def test_one_shot_job_shape(self) -> None:
        manifest = _manifest(timeout_seconds=600)
        assert manifest["spec"]["backoffLimit"] == 0
        assert manifest["spec"]["activeDeadlineSeconds"] == 660
        assert manifest["spec"]["template"]["spec"]["restartPolicy"] == "Never"
        command = manifest["spec"]["template"]["spec"]["containers"][0]["command"]
        assert command[:2] == ["python3", "-c"]

    def test_runs_as_host_uid(self) -> None:
        manifest = _manifest(host_uid=1234, host_gid=5678)
        ctx = manifest["spec"]["template"]["spec"]["securityContext"]
        assert ctx == {"runAsUser": 1234, "runAsGroup": 5678, "fsGroup": 5678}


# ----------------------------------------------------------------------
# run_slice_green_gate
# ----------------------------------------------------------------------


def _session_info() -> SessionInfo:
    return SessionInfo(
        session_token="tok-123",
        container_id="egg-greengate-x",
        container_ip=None,
        mode="public",
        created_at=datetime.now(),
        expires_at=datetime.now() + timedelta(hours=24),
    )


def _spawner(
    *,
    worktrees: dict[str, str] | None = None,
    worktree_exc: Exception | None = None,
    session_exc: Exception | None = None,
) -> MagicMock:
    spawner = MagicMock()
    if worktree_exc is not None:
        spawner.gateway.create_worktrees.side_effect = worktree_exc
    else:
        spawner.gateway.create_worktrees.return_value = WorktreeResult(
            success=True,
            worktrees=(
                worktrees
                if worktrees is not None
                else {REPO: "/home/host/.egg-worktrees/runner/egg"}
            ),
            errors=[],
        )
    if session_exc is not None:
        spawner.gateway.register_session.side_effect = session_exc
    else:
        spawner.gateway.register_session.return_value = _session_info()
    return spawner


def _terminal_pod(phase: str = "Succeeded") -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name="egg-greengate-pod"),
        status=SimpleNamespace(phase=phase),
    )


def _run_gate(spawner: MagicMock) -> str | None:
    return sgg.run_slice_green_gate(
        PIPELINE_ID,
        spawner,
        SLICE_ID,
        INTEGRATION_BRANCH,
        REPO,
        gateway_mode="public",
    )


@pytest.fixture
def enabled_gate(gate_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    gate_env.setenv(sgg.GREEN_GATE_ENV_VAR, "on")
    return gate_env


@pytest.fixture
def configured_checks():
    with (
        patch("config.repo_config.get_repo_checks", return_value=CHECKS),
        patch(
            "config.repo_config.get_repo_build_commands",
            return_value={"persist_dirs": [".venv"]},
        ),
    ):
        yield


class TestRunSliceGreenGate:
    def test_kill_switch_off_skips_everything(
        self, gate_env: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        assert _run_gate(spawner) is None
        spawner.gateway.create_worktrees.assert_not_called()

    def test_no_configured_checks_skips(self, enabled_gate: pytest.MonkeyPatch) -> None:
        spawner = _spawner()
        with patch("config.repo_config.get_repo_checks", return_value=[]):
            assert _run_gate(spawner) is None
        spawner.gateway.create_worktrees.assert_not_called()

    def test_worktree_failure_fails_open(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner(worktree_exc=RuntimeError("gateway down"))
        assert _run_gate(spawner) is None
        spawner.gateway.register_session.assert_not_called()

    def test_worktree_unsuccessful_fails_open(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        spawner.gateway.create_worktrees.return_value = WorktreeResult(
            success=False, worktrees={}, errors=["no branch"]
        )
        assert _run_gate(spawner) is None
        spawner.gateway.register_session.assert_not_called()

    def test_session_failure_fails_open_and_cleans_worktree(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner(session_exc=RuntimeError("no session"))
        assert _run_gate(spawner) is None
        spawner.gateway.delete_worktrees.assert_called_once()
        spawner.gateway.delete_session_by_container.assert_not_called()

    def test_submit_failure_fails_open_and_cleans_up(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        with (
            patch.object(sgg, "_submit_runner_job", side_effect=RuntimeError("api error")),
            patch.object(sgg, "_delete_runner_job") as delete_job,
        ):
            assert _run_gate(spawner) is None
        delete_job.assert_not_called()
        runner_id = spawner.gateway.register_session.call_args.kwargs["container_id"]
        spawner.gateway.delete_session_by_container.assert_called_once_with(runner_id)
        spawner.gateway.delete_worktrees.assert_called_once()

    def test_pod_timeout_fails_open_and_cleans_up(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        with (
            patch.object(sgg, "_submit_runner_job") as submit,
            patch.object(sgg, "_wait_for_runner_pod", return_value=None) as wait,
            patch.object(sgg, "_delete_runner_job") as delete_job,
        ):
            assert _run_gate(spawner) is None
        submit.assert_called_once()
        # The orchestrator-side wait budget must include the scheduling grace so a
        # cold-node image pull does not eat the check timeout and trip a spurious
        # fail-open (#3398). Pin the caller-side wiring so a refactor that drops the
        # grace can't stay green.
        assert (
            wait.call_args.kwargs["timeout"]
            == sgg._DEFAULT_TIMEOUT_SECONDS + sgg._POD_SCHEDULING_GRACE_SECONDS
        )
        delete_job.assert_called_once()
        spawner.gateway.delete_session_by_container.assert_called_once()
        spawner.gateway.delete_worktrees.assert_called_once()

    def test_unparseable_verdict_fails_open(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod("Failed")),
            patch.object(sgg, "_read_runner_log", return_value="crash traceback"),
            patch.object(sgg, "_delete_runner_job"),
        ):
            assert _run_gate(spawner) is None

    def test_green_verdict_proceeds(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        log = _verdict_line([{"name": "lint", "ok": True}, {"name": "test", "ok": True}])
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job") as delete_job,
        ):
            assert _run_gate(spawner) is None
        delete_job.assert_called_once()
        spawner.gateway.delete_session_by_container.assert_called_once()
        spawner.gateway.delete_worktrees.assert_called_once()

    def test_red_verdict_blocks_with_actionable_message(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        log = "make output\n" + _verdict_line(
            [
                {"name": "lint", "ok": True, "exit_code": 0, "output_tail": ""},
                {
                    "name": "test",
                    "ok": False,
                    "exit_code": 2,
                    "output_tail": "FAILED orchestrator/tests/test_x.py::test_y",
                },
            ]
        )
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job") as delete_job,
        ):
            failure = _run_gate(spawner)
        assert failure is not None
        assert SLICE_ID in failure
        assert INTEGRATION_BRANCH in failure
        assert "test" in failure
        assert "FAILED orchestrator/tests/test_x.py::test_y" in failure
        assert sgg.GREEN_GATE_ENV_VAR in failure
        assert "lint" not in failure.split("green gate failed")[1].split("\n")[0]
        # Cleanup still runs on the blocking path.
        delete_job.assert_called_once()
        spawner.gateway.delete_session_by_container.assert_called_once()
        spawner.gateway.delete_worktrees.assert_called_once()

    def test_red_verdict_in_log_mode_does_not_block(
        self, gate_env: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        gate_env.setenv(sgg.GREEN_GATE_ENV_VAR, "log")
        spawner = _spawner()
        log = _verdict_line([{"name": "test", "ok": False, "exit_code": 2, "output_tail": "x"}])
        with (
            patch.object(sgg, "_submit_runner_job") as submit,
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job"),
        ):
            assert _run_gate(spawner) is None
        # Log mode still runs the checks — it only skips the block.
        submit.assert_called_once()

    def test_all_infra_reds_fail_open(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        log = _verdict_line(
            [
                {"name": "lint", "ok": True, "exit_code": 0, "output_tail": "", "infra": None},
                {
                    "name": "test",
                    "ok": False,
                    "exit_code": 1,
                    "output_tail": "GATEWAY SIDECAR NOT AVAILABLE",
                    "infra": "GATEWAY SIDECAR NOT AVAILABLE",
                },
            ]
        )
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job") as delete_job,
        ):
            assert _run_gate(spawner) is None
        # Fail-open still cleans up everything it created.
        delete_job.assert_called_once()
        spawner.gateway.delete_session_by_container.assert_called_once()
        spawner.gateway.delete_worktrees.assert_called_once()

    def test_mixed_reds_block_on_genuine_only(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        log = _verdict_line(
            [
                {
                    "name": "lint",
                    "ok": False,
                    "exit_code": 1,
                    "output_tail": "GATEWAY SIDECAR NOT AVAILABLE",
                    "infra": "GATEWAY SIDECAR NOT AVAILABLE",
                },
                {
                    "name": "test",
                    "ok": False,
                    "exit_code": 2,
                    "output_tail": "FAILED tests/test_x.py::test_y",
                    "infra": None,
                },
            ]
        )
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job"),
        ):
            failure = _run_gate(spawner)
        assert failure is not None
        assert "test" in failure
        assert "FAILED tests/test_x.py::test_y" in failure
        # The infra-tagged red must not be presented as a slice failure:
        # its name and output stay out of the cascade-routed message.
        assert "GATEWAY SIDECAR NOT AVAILABLE" not in failure
        assert "lint" not in failure

    def test_infra_fail_open_switch_off_blocks_on_infra_reds(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        enabled_gate.setenv(sgg.GREEN_GATE_INFRA_FAIL_OPEN_ENV_VAR, "off")
        spawner = _spawner()
        log = _verdict_line(
            [
                {
                    "name": "test",
                    "ok": False,
                    "exit_code": 1,
                    "output_tail": "GATEWAY SIDECAR NOT AVAILABLE",
                    "infra": "GATEWAY SIDECAR NOT AVAILABLE",
                }
            ]
        )
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job"),
        ):
            failure = _run_gate(spawner)
        assert failure is not None
        assert "test" in failure

    def test_pre_3417_verdict_without_infra_field_blocks(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        # A verdict whose checks carry no ``infra`` key (an in-flight
        # runner from before the #3417 rollout) is treated as genuinely
        # red: absence of the tag must never fail open.
        spawner = _spawner()
        log = _verdict_line([{"name": "test", "ok": False, "exit_code": 2, "output_tail": "x"}])
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job"),
        ):
            assert _run_gate(spawner) is not None

    def test_worktree_forked_from_integration_branch(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        log = _verdict_line([{"name": "lint", "ok": True}])
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job"),
        ):
            _run_gate(spawner)
        wt_kwargs = spawner.gateway.create_worktrees.call_args.kwargs
        assert wt_kwargs["base_branch"] == INTEGRATION_BRANCH
        assert wt_kwargs["assigned_branch"] == INTEGRATION_BRANCH
        assert wt_kwargs["repos"] == [REPO]
        sess_kwargs = spawner.gateway.register_session.call_args.kwargs
        assert sess_kwargs["agent_role"] == "tester"
        assert sess_kwargs["pipeline_id"] == PIPELINE_ID

    def test_manifest_env_marks_prebuilt_required(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        log = _verdict_line([{"name": "lint", "ok": True}])
        with (
            patch.object(sgg, "_submit_runner_job") as submit,
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job"),
        ):
            _run_gate(spawner)
        manifest = submit.call_args.args[2]
        env = {
            e["name"]: e["value"]
            for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["EGG_GREEN_GATE_REQUIRE_PREBUILT"] == "1"
        assert env["EGG_SESSION_TOKEN"] == "tok-123"
        assert json.loads(env["EGG_GREEN_GATE_CHECKS"]) == CHECKS
