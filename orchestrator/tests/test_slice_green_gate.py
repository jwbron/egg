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
  #3409 fix flow (fix executed only on a red check, re-run verdict,
  changed-files reporting + cap, no-git degrade), and the #3417 infra
  tagging (signature match over full output, SIGKILL exit, green checks
  never tagged).
* ``_commit_and_push_autofix`` — real-git stage/commit + gateway push
  wiring, the no-tracked-changes refusal, and push-failure reporting.
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


def _verdict_line(checks: list[dict[str, Any]], **extra: Any) -> str:
    return sgg.VERDICT_SENTINEL + json.dumps({"checks": checks, **extra})


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
    extra_env: dict[str, str] | None = None,
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
    env.update(extra_env or {})
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


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(exist_ok=True)
    _git(repo_dir, "init", "-q", ".")
    (repo_dir / "file.txt").write_text("bad\n")
    _git(repo_dir, "add", "file.txt")
    _git(repo_dir, "commit", "-q", "-m", "init")


class TestRunnerFixFlow:
    """#3409 — the runner's fix execution + re-run reporting."""

    FIXABLE_CHECK = {
        "name": "lint",
        "command": "grep -q good file.txt",
        "fix": "printf 'good\\n' > file.txt",
    }

    def test_red_check_with_fix_reports_fix_result(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "egg")
        (tmp_path / "egg" / "junk.log").write_text("untracked check dropping")
        proc = _run_runner(tmp_path, [dict(self.FIXABLE_CHECK)])
        assert proc.returncode == 0
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        entry = verdict["checks"][0]
        # The tip as pushed is still red; only the orchestrator commit
        # may turn the verdict green.
        assert entry["ok"] is False
        fix = entry["fix"]
        assert fix["command"] == self.FIXABLE_CHECK["fix"]
        assert fix["exit_code"] == 0
        assert fix["check_ok_after_fix"] is True
        # Tracked modification reported; untracked droppings are not.
        assert fix["changed_files"] == ["file.txt"]
        assert fix["changed_file_count"] == 1

    def test_fix_that_does_not_repair_reports_red_rerun(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "egg")
        proc = _run_runner(
            tmp_path,
            [{"name": "lint", "command": "grep -q good file.txt", "fix": "true"}],
        )
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        fix = verdict["checks"][0]["fix"]
        assert fix["check_ok_after_fix"] is False
        assert fix["changed_files"] == []

    def test_green_check_never_runs_fix(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "egg")
        proc = _run_runner(
            tmp_path,
            [{"name": "lint", "command": "true", "fix": "touch fix-ran.marker"}],
        )
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        assert "fix" not in verdict["checks"][0]
        assert not (tmp_path / "egg" / "fix-ran.marker").exists()

    def test_red_check_without_fix_reports_plain_red(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "egg")
        proc = _run_runner(tmp_path, [{"name": "lint", "command": "false"}])
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        entry = verdict["checks"][0]
        assert entry["ok"] is False
        assert "fix" not in entry

    def test_changed_files_capped_but_count_exact(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "egg"
        _init_git_repo(repo_dir)
        for i in range(3):
            (repo_dir / f"extra{i}.txt").write_text("bad\n")
            _git(repo_dir, "add", f"extra{i}.txt")
        _git(repo_dir, "commit", "-q", "-m", "more files")
        proc = _run_runner(
            tmp_path,
            [
                {
                    "name": "lint",
                    "command": "grep -q good file.txt",
                    "fix": "for f in file.txt extra0.txt extra1.txt extra2.txt; "
                    "do printf 'good\\n' > \"$f\"; done",
                }
            ],
            extra_env={"EGG_GREEN_GATE_CHANGED_FILES_CAP": "2"},
        )
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        fix = verdict["checks"][0]["fix"]
        assert fix["check_ok_after_fix"] is True
        assert len(fix["changed_files"]) == 2
        assert fix["changed_file_count"] == 4

    def test_no_git_repo_degrades_changed_files_to_none(self, tmp_path: Path) -> None:
        # No git init: the gateway-routed git diff is best-effort and a
        # failure must degrade to an unreported list, not a crash.
        #
        # Assumption: pytest's ``tmp_path`` (under the system temp root,
        # e.g. ``/tmp``) is NOT nested inside any git repository, so the
        # runner's ``git diff`` / ``git ls-files`` genuinely fail. If the
        # tmp root ever moves under a checkout, git would succeed against
        # the enclosing repo and these ``is None`` assertions would flip —
        # a confusing failure that this note is here to explain.
        repo_dir = tmp_path / "egg"
        repo_dir.mkdir(exist_ok=True)
        (repo_dir / "file.txt").write_text("bad\n")
        proc = _run_runner(tmp_path, [dict(self.FIXABLE_CHECK)])
        assert proc.returncode == 0
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        fix = verdict["checks"][0]["fix"]
        assert fix["check_ok_after_fix"] is True
        assert fix["changed_files"] is None
        assert fix["changed_file_count"] is None
        # Best-effort git failed, so the untracked delta is unknown and
        # the orchestrator will refuse autofix (fail-safe).
        final = verdict["final_verification"]
        assert final["new_untracked_count"] is None

    def test_final_verification_green_for_fixed_tree(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "egg")
        proc = _run_runner(tmp_path, [dict(self.FIXABLE_CHECK)])
        assert proc.returncode == 0
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        final = verdict["final_verification"]
        assert final["ran"] is True
        # The final full re-run of every check against the fixed tree is
        # green, and the fix only touched a tracked file.
        assert final["all_ok"] is True
        assert final["failed"] == []
        assert final["new_untracked_count"] == 0

    def test_final_verification_flags_fix_created_untracked_file(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "egg")
        # The fix repairs file.txt AND emits a new, non-ignored source
        # file that git add -u would never stage.
        proc = _run_runner(
            tmp_path,
            [
                {
                    "name": "lint",
                    "command": "grep -q good file.txt",
                    "fix": "printf 'good\\n' > file.txt; printf 'x\\n' > generated.py",
                }
            ],
        )
        assert proc.returncode == 0
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        final = verdict["final_verification"]
        assert final["all_ok"] is True
        assert final["new_untracked_count"] == 1
        assert final["new_untracked_files"] == ["generated.py"]

    def test_no_fix_applied_omits_final_verification(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "egg")
        proc = _run_runner(tmp_path, [{"name": "lint", "command": "true"}])
        verdict = sgg.parse_verdict(proc.stdout)
        assert verdict is not None
        # No fix ran, so there is no combined tree to re-validate.
        assert "final_verification" not in verdict


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
        assert env["EGG_GREEN_GATE_CHANGED_FILES_CAP"] == str(sgg._FIX_CHANGED_FILES_CAP)
        assert json.loads(env["EGG_GREEN_GATE_CHECKS"]) == CHECKS


# ----------------------------------------------------------------------
# _commit_and_push_autofix (#3409)
# ----------------------------------------------------------------------


def _autofix_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "egg"
    _init_git_repo(repo_dir)
    return repo_dir


def _run_autofix(repo_dir: Path, gateway: MagicMock) -> str | None:
    return sgg._commit_and_push_autofix(
        gateway,
        pipeline_id=PIPELINE_ID,
        slice_id=SLICE_ID,
        worktree_path=str(repo_dir),
        integration_branch=INTEGRATION_BRANCH,
        gateway_mode="public",
        fixed_checks=[{"name": "lint", "fix": {"check_ok_after_fix": True}}],
    )


class TestCommitAndPushAutofix:
    def test_stages_commits_and_pushes(self, tmp_path: Path) -> None:
        repo_dir = _autofix_repo(tmp_path)
        (repo_dir / "file.txt").write_text("good\n")
        (repo_dir / "junk.log").write_text("untracked check dropping")
        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = SimpleNamespace(ok=True)

        assert _run_autofix(repo_dir, gateway) is None

        head = _git(repo_dir, "log", "-1", "--format=%an|%ae|%s")
        author, email, subject = head.stdout.strip().split("|")
        assert author == "egg-green-gate"
        assert email == "egg-green-gate@localhost"
        assert "lint" in subject
        # Untracked droppings never enter the commit.
        shown = _git(repo_dir, "show", "--name-only", "--format=", "HEAD")
        assert shown.stdout.split() == ["file.txt"]
        gateway.push_worktree_branch.assert_called_once_with(
            PIPELINE_ID,
            repo_path=str(repo_dir),
            branch=INTEGRATION_BRANCH,
            mode="public",
        )

    def test_no_tracked_changes_refuses_without_push(self, tmp_path: Path) -> None:
        repo_dir = _autofix_repo(tmp_path)
        gateway = MagicMock()
        error = _run_autofix(repo_dir, gateway)
        assert error is not None
        assert "no tracked" in error
        gateway.push_worktree_branch.assert_not_called()

    def test_push_failure_is_reported(self, tmp_path: Path) -> None:
        repo_dir = _autofix_repo(tmp_path)
        (repo_dir / "file.txt").write_text("good\n")
        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = SimpleNamespace(
            ok=False, category="auth_failed", detail="denied"
        )
        error = _run_autofix(repo_dir, gateway)
        assert error is not None
        assert "auth_failed" in error
        assert "denied" in error

    def test_push_raising_is_reported(self, tmp_path: Path) -> None:
        repo_dir = _autofix_repo(tmp_path)
        (repo_dir / "file.txt").write_text("good\n")
        gateway = MagicMock()
        gateway.push_worktree_branch.side_effect = RuntimeError("gateway down")
        error = _run_autofix(repo_dir, gateway)
        assert error is not None
        assert "gateway down" in error

    def test_not_a_git_repo_is_reported(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "not-a-repo"
        repo_dir.mkdir()
        gateway = MagicMock()
        error = _run_autofix(repo_dir, gateway)
        assert error is not None
        gateway.push_worktree_branch.assert_not_called()


# ----------------------------------------------------------------------
# run_slice_green_gate — #3409 autofix wiring
# ----------------------------------------------------------------------


def _fixed(ok: bool = True) -> dict[str, Any]:
    return {
        "command": "make lint-fix",
        "exit_code": 0,
        "check_ok_after_fix": ok,
        "changed_files": ["a.py"],
        "changed_file_count": 1,
        "output_tail": "",
        "recheck_output_tail": "",
    }


def _final_verification(
    *,
    all_ok: bool = True,
    failed: list[str] | None = None,
    new_untracked_count: int | None = 0,
    new_untracked_files: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ran": True,
        "all_ok": all_ok,
        "failed": failed or [],
        "new_untracked_files": (new_untracked_files if new_untracked_files is not None else []),
        "new_untracked_count": new_untracked_count,
    }


def _red_lint_verdict(*, fix: dict[str, Any] | None, final: Any = "default") -> str:
    entry: dict[str, Any] = {
        "name": "lint",
        "ok": False,
        "exit_code": 1,
        "output_tail": "would reformat a.py",
    }
    if fix is not None:
        entry["fix"] = fix
    checks = [entry, {"name": "test", "ok": True, "exit_code": 0, "output_tail": ""}]
    # A fixable verdict carries the runner's final full re-run by default;
    # pass ``final=None`` to model an old/degraded runner that omitted it.
    if final == "default":
        final = _final_verification() if fix is not None else None
    if final is not None:
        return _verdict_line(checks, final_verification=final)
    return _verdict_line(checks)


class TestGreenGateAutofixWiring:
    def test_fixed_red_verdict_pushes_and_passes(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=_red_lint_verdict(fix=_fixed())),
            patch.object(sgg, "_delete_runner_job"),
            patch.object(sgg, "_commit_and_push_autofix", return_value=None) as autofix,
        ):
            assert _run_gate(spawner) is None
        autofix.assert_called_once()
        kwargs = autofix.call_args.kwargs
        # The autofix stages the SAME hostPath worktree the runner
        # mutated, and pushes to the slice integration branch.
        assert kwargs["worktree_path"] == "/home/host/.egg-worktrees/runner/egg"
        assert kwargs["integration_branch"] == INTEGRATION_BRANCH
        assert kwargs["gateway_mode"] == "public"
        assert [c["name"] for c in kwargs["fixed_checks"]] == ["lint"]
        # Cleanup still runs after the autofix path.
        spawner.gateway.delete_session_by_container.assert_called_once()
        spawner.gateway.delete_worktrees.assert_called_once()

    def test_autofix_failure_blocks_with_note(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=_red_lint_verdict(fix=_fixed())),
            patch.object(sgg, "_delete_runner_job"),
            patch.object(sgg, "_commit_and_push_autofix", return_value="push exploded"),
        ):
            failure = _run_gate(spawner)
        assert failure is not None
        assert "lint" in failure
        assert "push exploded" in failure

    def test_rerun_still_red_blocks_without_autofix(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(
                sgg, "_read_runner_log", return_value=_red_lint_verdict(fix=_fixed(ok=False))
            ),
            patch.object(sgg, "_delete_runner_job"),
            patch.object(sgg, "_commit_and_push_autofix") as autofix,
        ):
            failure = _run_gate(spawner)
        assert failure is not None
        autofix.assert_not_called()

    def test_partially_fixable_verdict_blocks_without_autofix(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        spawner = _spawner()
        log = _verdict_line(
            [
                {"name": "lint", "ok": False, "exit_code": 1, "output_tail": "", "fix": _fixed()},
                {"name": "test", "ok": False, "exit_code": 2, "output_tail": "FAILED"},
            ]
        )
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job"),
            patch.object(sgg, "_commit_and_push_autofix") as autofix,
        ):
            failure = _run_gate(spawner)
        assert failure is not None
        autofix.assert_not_called()

    def test_log_mode_never_pushes_a_fix(
        self, gate_env: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        gate_env.setenv(sgg.GREEN_GATE_ENV_VAR, "log")
        spawner = _spawner()
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=_red_lint_verdict(fix=_fixed())),
            patch.object(sgg, "_delete_runner_job"),
            patch.object(sgg, "_commit_and_push_autofix") as autofix,
        ):
            assert _run_gate(spawner) is None
        autofix.assert_not_called()

    def _assert_blocks_without_autofix(self, spawner: MagicMock, log: str) -> None:
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job"),
            patch.object(sgg, "_commit_and_push_autofix") as autofix,
        ):
            failure = _run_gate(spawner)
        assert failure is not None
        autofix.assert_not_called()

    def test_final_rerun_red_blocks_without_autofix(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        # #3409: every failed check's own re-run went green, but the final
        # full re-run of all checks against the combined tree is red (a
        # fix broke another check) — the committed tip would be red.
        log = _red_lint_verdict(
            fix=_fixed(), final=_final_verification(all_ok=False, failed=["test"])
        )
        self._assert_blocks_without_autofix(_spawner(), log)

    def test_fix_created_untracked_files_blocks_without_autofix(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        # #3409: the fix emitted a new source file. ``git add -u`` would
        # drop it, so the pushed tip omits it and the check is red as
        # pushed even though the runner's on-disk re-run was green.
        log = _red_lint_verdict(
            fix=_fixed(),
            final=_final_verification(new_untracked_count=1, new_untracked_files=["gen/new.py"]),
        )
        self._assert_blocks_without_autofix(_spawner(), log)

    def test_unknown_untracked_count_blocks_without_autofix(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        # #3409: a best-effort git failure left the untracked delta
        # unknown; the gate refuses rather than risk a red pushed tip.
        log = _red_lint_verdict(fix=_fixed(), final=_final_verification(new_untracked_count=None))
        self._assert_blocks_without_autofix(_spawner(), log)

    def test_missing_final_verification_blocks_without_autofix(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        # #3409: an old/degraded runner that omitted final_verification
        # cannot prove the committed tree is green — refuse autofix.
        log = _red_lint_verdict(fix=_fixed(), final=None)
        self._assert_blocks_without_autofix(_spawner(), log)


def _infra_plus_fixable_verdict(*, final: dict[str, Any]) -> str:
    """An infra-tagged red (#3417) co-occurring with a fixable red (#3409)."""
    return _verdict_line(
        [
            {
                "name": "test",
                "ok": False,
                "exit_code": 137,
                "output_tail": "GATEWAY SIDECAR NOT AVAILABLE",
                "infra": "GATEWAY SIDECAR NOT AVAILABLE",
            },
            {
                "name": "lint",
                "ok": False,
                "exit_code": 1,
                "output_tail": "would reformat a.py",
                "infra": None,
                "fix": _fixed(),
            },
        ],
        final_verification=final,
    )


class TestInfraFailOpenAutofixComposition:
    """#3417 infra fail-open composed with #3409 autofix.

    The gate narrows ``failed`` to ``genuine_failed`` *before* the autofix
    decision, so ``_autofix_ready`` and ``_commit_and_push_autofix`` both
    see only the non-infra reds. Neither #3417 nor #3409 alone exercises
    this: the infra tests build verdicts with no ``fix`` block and the
    autofix tests build verdicts with no ``infra`` field.
    """

    def test_infra_red_alongside_fixable_red_pushes_only_the_genuine_fix(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        # The infra-tagged red is filtered out, the genuine red's fix went
        # green, and the final full re-run — which covers *every* check,
        # infra-tagged ones included — is green, so the tip is provably
        # green and the autofix pushes.
        log = _infra_plus_fixable_verdict(final=_final_verification())
        spawner = _spawner()
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job"),
            patch.object(sgg, "_commit_and_push_autofix", return_value=None) as autofix,
        ):
            assert _run_gate(spawner) is None
        autofix.assert_called_once()
        # Only the genuine red is reported as fixed — the infra red never
        # reaches the commit path even though it was red at the tip.
        assert [c["name"] for c in autofix.call_args.kwargs["fixed_checks"]] == ["lint"]

    def test_infra_red_still_red_in_final_rerun_blocks_the_fixable_red(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        # The infra red is filtered from the *presented* failures, but the
        # final full re-run still covers it — so a persistent infra fault
        # blocks the push rather than letting a tree only partly proven
        # green reach the integration branch.
        log = _infra_plus_fixable_verdict(final=_final_verification(all_ok=False, failed=["test"]))
        spawner = _spawner()
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job"),
            patch.object(sgg, "_commit_and_push_autofix") as autofix,
        ):
            failure = _run_gate(spawner)
        autofix.assert_not_called()
        assert failure is not None
        # #3409: every red the operator is shown had a working fix, so the
        # message must say why the gate refused to self-heal anyway —
        # otherwise they re-run `make lint-fix`, watch it succeed, and see
        # no reason for the block. The hidden check is named as the cause
        # of the *autofix refusal*, not routed as a slice failure: its
        # output tail stays out of the presented failure list (#3417).
        assert "did not self-heal" in failure
        assert "final full re-run of all checks was not green (red: test)" in failure
        assert "GATEWAY SIDECAR NOT AVAILABLE" not in failure

    def test_no_note_when_the_genuine_red_had_no_fix(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        # Contrast: the genuine red carries no fix at all, so the reds in
        # the message are the operator's own to fix and the failure text
        # is self-explanatory. No autofix explanation is appended.
        log = _verdict_line(
            [
                {
                    "name": "lint",
                    "ok": False,
                    "exit_code": 1,
                    "output_tail": "infra",
                    "infra": "ENOSPC",
                },
                {"name": "test", "ok": False, "exit_code": 2, "output_tail": "FAILED"},
            ]
        )
        spawner = _spawner()
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job"),
        ):
            failure = _run_gate(spawner)
        assert failure is not None
        assert "did not self-heal" not in failure

    def test_fail_open_switch_off_makes_the_infra_red_block_the_autofix(
        self, enabled_gate: pytest.MonkeyPatch, configured_checks: None
    ) -> None:
        # With the #3417 switch off there is no narrowing, so the
        # unfixable infra-tagged red stays in the set `_autofix_ready`
        # judges and the verdict is only partially fixable — no push.
        # This pins the narrowing itself as what enables the push above.
        enabled_gate.setenv(sgg.GREEN_GATE_INFRA_FAIL_OPEN_ENV_VAR, "off")
        log = _infra_plus_fixable_verdict(final=_final_verification())
        spawner = _spawner()
        with (
            patch.object(sgg, "_submit_runner_job"),
            patch.object(sgg, "_wait_for_runner_pod", return_value=_terminal_pod()),
            patch.object(sgg, "_read_runner_log", return_value=log),
            patch.object(sgg, "_delete_runner_job"),
            patch.object(sgg, "_commit_and_push_autofix") as autofix,
        ):
            failure = _run_gate(spawner)
        autofix.assert_not_called()
        assert failure is not None
        assert "test" in failure


class TestAutofixReady:
    """#3409 — ``_autofix_ready`` gating on the final full re-run."""

    LINT_FAILED = [{"name": "lint", "fix": {"check_ok_after_fix": True}}]

    def test_ready_when_final_green_and_no_new_untracked(self) -> None:
        verdict = {"checks": [], "final_verification": _final_verification()}
        ready, reason = sgg._autofix_ready(verdict, self.LINT_FAILED)
        assert ready is True
        assert reason == ""

    def test_not_ready_when_a_failed_check_is_unfixable(self) -> None:
        failed = [{"name": "lint", "fix": {"check_ok_after_fix": True}}, {"name": "test"}]
        verdict = {"checks": [], "final_verification": _final_verification()}
        ready, reason = sgg._autofix_ready(verdict, failed)
        assert ready is False
        assert "no fix" in reason

    def test_not_ready_when_final_missing(self) -> None:
        ready, reason = sgg._autofix_ready({"checks": []}, self.LINT_FAILED)
        assert ready is False
        assert "final full re-run" in reason

    def test_not_ready_when_final_red(self) -> None:
        verdict = {
            "checks": [],
            "final_verification": _final_verification(all_ok=False, failed=["test"]),
        }
        ready, reason = sgg._autofix_ready(verdict, self.LINT_FAILED)
        assert ready is False
        assert "test" in reason

    def test_not_ready_when_untracked_created(self) -> None:
        verdict = {
            "checks": [],
            "final_verification": _final_verification(
                new_untracked_count=2, new_untracked_files=["a.py", "b.py"]
            ),
        }
        ready, reason = sgg._autofix_ready(verdict, self.LINT_FAILED)
        assert ready is False
        assert "untracked" in reason
        assert "a.py" in reason

    def test_not_ready_when_untracked_count_unknown(self) -> None:
        verdict = {
            "checks": [],
            "final_verification": _final_verification(new_untracked_count=None),
        }
        ready, reason = sgg._autofix_ready(verdict, self.LINT_FAILED)
        assert ready is False
        assert "untracked" in reason
