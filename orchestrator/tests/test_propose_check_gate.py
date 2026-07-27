"""Tests for the propose-time check gate (#3669).

Covers:

* ``propose_check_gate_mode`` — the three-state operator switch, with
  ``off`` as the default (#3670 must land before that can flip) and an
  unrecognised value degrading to the default.
* ``_infra_fail_open_enabled`` — the #3417/#3621 fail-open switch.
* ``gate_checks`` — config-driven selection, the skip set, and the
  ``full_command`` resolution that keeps a changeset-narrowed command
  from ever being what the gate runs (constraint: a narrowed run is
  never evidence).
* ``parse_verdict`` — sentinel extraction from noisy pod logs.
* ``_RUNNER_PROGRAM`` — executed for real in a subprocess against a
  throwaway git repo: it runs the checks in the worktree exactly as the
  orchestrator left it (invoking no git at all — the sandbox ``git`` is
  the gateway wrapper, which refuses a detaching checkout in a
  branch-locked session), records the exact command per check, tags
  infra reds, and restores the matching prebuilt snapshot.
* ``_materialise_proposed_tree`` — the orchestrator-side detach that
  moved out of the pod: it checks out the *proposed* SHA (not a branch
  tip), reports the resolved HEAD, falls back to a gateway fetch for an
  object the worktree does not yet hold, never reaches a shell with the
  agent-supplied SHA, and returns an infra reason — no Job submitted —
  when the tree will not materialise.
* ``build_runner_job_manifest`` / ``_submit_runner_job`` — the #3622
  precedent: the check budget is a **pod-level** ``activeDeadlineSeconds``
  counted from pod start, the Job-level one is a strictly larger outer
  ceiling, and the pod-level field actually reaches the submitted body
  (the failure mode #3622 documents is a manifest field the submitter
  silently drops).
* The verdict ledger — one run per proposed tree regardless of how many
  producers propose it or how often, eviction that never drops a run
  still in flight (and warns rather than growing silently when nothing
  is evictable), and the TTL that stops a stale red rejecting the same
  tree for the orchestrator's process life.
* The deferral registry — a deferred propose holds that producer's
  event-loop arm (``propose_spawn_block_reason``) so the loop does not
  respawn it into the same 409 and misread the gate as a #3425 no-op
  park; the hold is producer-scoped and clears itself.
* ``_record_verdict`` — green/red/all-infra/mixed classification, and
  the strict posture under ``INFRA_FAIL_OPEN=off``.
* ``propose_check_rejection`` — the gate itself: every fail-open path,
  the red rejection (failing check named, command and output reachable),
  the pending state, log mode, and the attestation stamped onto an
  accepted proposal.
* Route integration — a red proposal is rejected before the tracker is
  touched; a green one takes the pre-gate path unchanged.
* Cross-module: the infra vocabulary is literally shared with
  ``slice_green_gate`` so the two gates cannot drift (#3621).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

import propose_check_gate as gate  # noqa: E402
import propose_check_runner as runner  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_ledger_and_env(monkeypatch):
    """Every test starts with an empty ledger and no gate env vars set."""
    gate.reset_ledger()
    for var in (
        gate.GATE_ENV_VAR,
        gate.SKIP_CHECKS_ENV_VAR,
        gate.PHASES_ENV_VAR,
        gate.INFRA_FAIL_OPEN_ENV_VAR,
        runner.TIMEOUT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    gate.reset_ledger()


def _check(name="test", command="make test-all", ok=True, **extra):
    entry = {
        "name": name,
        "command": command,
        "ok": ok,
        "exit_code": 0 if ok else 1,
        "output_tail": "output" if ok else "FAILED tests/test_x.py::test_y",
        "infra": None,
    }
    entry.update(extra)
    return entry


def _record(state="passed", **kwargs):
    rec = gate.CheckRun(
        pipeline_id=kwargs.pop("pipeline_id", "issue-42"),
        slice_id=kwargs.pop("slice_id", None),
        commit_sha=kwargs.pop("commit_sha", "abc1234def"),
        base_branch=kwargs.pop("base_branch", "egg/issue-42"),
        repo=kwargs.pop("repo", "owner/repo"),
        # Shaped like ``gate_checks()`` output, which always carries
        # ``narrowed`` — the record is never built by hand in production.
        checks=kwargs.pop(
            "checks", [{"name": "test", "command": "make test-all", "narrowed": "false"}]
        ),
    )
    rec.state = state
    for key, value in kwargs.items():
        setattr(rec, key, value)
    return rec


# ==========================================================================
# Operator switches
# ==========================================================================


class TestGateMode:
    def test_unset_defaults_off(self):
        """#3670: a gate enabled against a red baseline rejects everything."""
        assert gate.propose_check_gate_mode() == "off"

    @pytest.mark.parametrize("value", ["on", "1", "true", "yes", "ON", " on "])
    def test_enabled_values(self, monkeypatch, value):
        monkeypatch.setenv(gate.GATE_ENV_VAR, value)
        assert gate.propose_check_gate_mode() == "on"

    @pytest.mark.parametrize("value", ["log", "log-only", "log_only"])
    def test_log_values(self, monkeypatch, value):
        monkeypatch.setenv(gate.GATE_ENV_VAR, value)
        assert gate.propose_check_gate_mode() == "log"

    @pytest.mark.parametrize("value", ["off", "0", "false", "no"])
    def test_disabled_values(self, monkeypatch, value):
        monkeypatch.setenv(gate.GATE_ENV_VAR, value)
        assert gate.propose_check_gate_mode() == "off"

    def test_unrecognised_degrades_to_default_loudly(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "onn")
        with patch.object(gate.logger, "warning") as warn:
            assert gate.propose_check_gate_mode() == "off"
        assert warn.called


class TestInfraFailOpenSwitch:
    def test_default_on(self):
        assert gate._infra_fail_open_enabled() is True

    @pytest.mark.parametrize("value", ["off", "0", "false", "no"])
    def test_explicit_off(self, monkeypatch, value):
        monkeypatch.setenv(gate.INFRA_FAIL_OPEN_ENV_VAR, value)
        assert gate._infra_fail_open_enabled() is False

    def test_unrecognised_degrades_lenient_but_loudly(self, monkeypatch):
        """Typo direction here is strict -> lenient, so it must not be silent."""
        monkeypatch.setenv(gate.INFRA_FAIL_OPEN_ENV_VAR, "offf")
        with patch.object(gate.logger, "warning") as warn:
            assert gate._infra_fail_open_enabled() is True
        assert warn.called


class TestGateTimeout:
    def test_default(self):
        assert runner._gate_timeout_seconds() == runner._DEFAULT_TIMEOUT_SECONDS

    @pytest.mark.parametrize("value", ["nonsense", "0", "-5"])
    def test_invalid_falls_back(self, monkeypatch, value):
        monkeypatch.setenv(runner.TIMEOUT_ENV_VAR, value)
        assert runner._gate_timeout_seconds() == runner._DEFAULT_TIMEOUT_SECONDS

    def test_explicit(self, monkeypatch):
        monkeypatch.setenv(runner.TIMEOUT_ENV_VAR, "900")
        assert runner._gate_timeout_seconds() == 900


# ==========================================================================
# Check resolution — the narrowed-run constraint
# ==========================================================================


class TestGateChecks:
    def test_full_command_wins_over_narrowed_command(self):
        """The whole point: `make test` narrows, `make test-all` is ground truth."""
        configured = [
            {"name": "lint", "command": "make lint", "fix": "make lint-fix"},
            {"name": "test", "command": "make test", "full_command": "make test-all"},
        ]
        with patch("config.repo_config.get_repo_checks", return_value=configured):
            resolved = gate.gate_checks("owner/repo")

        by_name = {c["name"]: c for c in resolved}
        assert by_name["test"]["command"] == "make test-all"
        assert by_name["test"]["narrowed"] == "false"
        # No ground-truth form declared: the gate runs what it has and
        # says so, rather than pretending the run was full.
        assert by_name["lint"]["command"] == "make lint"
        assert by_name["lint"]["narrowed"] == "unknown"

    def test_security_skipped_by_default(self):
        configured = [
            {"name": "test", "command": "make test", "full_command": "make test-all"},
            {"name": "security", "command": "make security"},
        ]
        with patch("config.repo_config.get_repo_checks", return_value=configured):
            resolved = gate.gate_checks("owner/repo")
        assert [c["name"] for c in resolved] == ["test"]

    def test_skip_set_is_configurable(self, monkeypatch):
        monkeypatch.setenv(gate.SKIP_CHECKS_ENV_VAR, "lint, test")
        configured = [
            {"name": "lint", "command": "make lint"},
            {"name": "test", "command": "make test"},
            {"name": "security", "command": "make security"},
        ]
        with patch("config.repo_config.get_repo_checks", return_value=configured):
            assert [c["name"] for c in gate.gate_checks("owner/repo")] == ["security"]

    def test_config_failure_returns_empty(self):
        """A config problem must never reject a proposal."""
        with patch("config.repo_config.get_repo_checks", side_effect=RuntimeError("boom")):
            assert gate.gate_checks("owner/repo") == []


class TestValidateChecksFullCommand:
    """``full_command`` survives config validation on both copies."""

    def test_shared_validator_retains_full_command(self):
        from egg_config.validators import validate_checks

        out = validate_checks(
            [{"name": "test", "command": "make test", "full_command": "make test-all"}]
        )
        assert out == [{"name": "test", "command": "make test", "full_command": "make test-all"}]

    def test_shared_validator_drops_empty_full_command(self):
        from egg_config.validators import validate_checks

        out = validate_checks([{"name": "test", "command": "make test", "full_command": ""}])
        assert out == [{"name": "test", "command": "make test"}]


# ==========================================================================
# Verdict parsing
# ==========================================================================


class TestParseVerdict:
    def test_extracts_sentinel_line(self):
        log = "noise\n" + runner.VERDICT_SENTINEL + json.dumps({"checks": [_check()]}) + "\nmore\n"
        parsed = runner.parse_verdict(log)
        assert parsed is not None
        assert parsed["checks"][0]["name"] == "test"

    def test_scans_from_the_end(self):
        """Check output that mimics the sentinel cannot shadow the verdict."""
        fake = runner.VERDICT_SENTINEL + json.dumps({"checks": [_check(name="fake")]})
        real = runner.VERDICT_SENTINEL + json.dumps({"checks": [_check(name="real")]})
        parsed = runner.parse_verdict(f"{fake}\n{real}\n")
        assert parsed["checks"][0]["name"] == "real"

    @pytest.mark.parametrize(
        "log",
        [
            "",
            "no sentinel at all",
            runner.VERDICT_SENTINEL + "{not json",
            runner.VERDICT_SENTINEL + json.dumps({"checks": "not a list"}),
            runner.VERDICT_SENTINEL + json.dumps(["not", "a", "dict"]),
        ],
    )
    def test_unparseable_returns_none(self, log):
        assert runner.parse_verdict(log) is None


# ==========================================================================
# The runner program, executed for real
# ==========================================================================


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def runner_repo(tmp_path):
    """A throwaway repo with two commits, so 'the proposed SHA' is not HEAD.

    The directory is named for the **bare repo name**, matching
    production: the gateway names each worktree
    ``<worktree_base>/<container_id>/<repo_name>`` where ``repo_name``
    is ``repo.split("/")[-1]``, and both ``run_propose_checks``' mount
    path and the runner's ``restore_prebuilt`` derive their key from
    that basename (``entry.endswith("--" + name)`` matches a
    ``/opt/prebuilt-deps/<owner>--<repo>`` snapshot). A fixture named
    ``owner--repo`` would make the prebuilt matcher unmatchable here
    while it matches fine in production.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "marker.txt").write_text("first\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    first = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "marker.txt").write_text("second\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    second = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return SimpleNamespace(path=repo, first=first, second=second)


def _run_runner(repo_dir: Path, sha: str, checks: list[dict[str, str]], **env_extra):
    env = dict(os.environ)
    env.update(
        {
            "EGG_PROPOSE_CHECK_CHECKS": json.dumps(checks),
            "EGG_PROPOSE_CHECK_REPO_DIR": str(repo_dir),
            "EGG_PROPOSE_CHECK_COMMIT_SHA": sha,
            "EGG_PROPOSE_CHECK_PREBUILT_BASE": str(repo_dir / "__no_such_prebuilt__"),
            "EGG_PROPOSE_CHECK_REQUIRE_PREBUILT": "0",
            "EGG_PROPOSE_CHECK_INFRA_SIGNATURES": json.dumps(
                {
                    "line": list(runner._INFRA_LINE_SIGNATURES),
                    "substring": list(runner._INFRA_SUBSTRING_SIGNATURES),
                }
            ),
        }
    )
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", runner._RUNNER_PROGRAM],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


class TestRunnerProgram:
    def test_checks_run_in_the_mounted_worktree_as_the_orchestrator_left_it(self, runner_repo):
        """The pod runs the checks; it does not move the tree.

        The orchestrator has already detached the worktree to the
        proposed SHA (``_materialise_proposed_tree``), so the runner
        just executes against whatever is mounted — here, the second
        commit, because nothing checked anything out.
        """
        proc = _run_runner(
            runner_repo.path,
            runner_repo.second,
            [{"name": "marker", "command": "cat marker.txt"}],
        )
        assert proc.returncode == 0, proc.stderr
        verdict = runner.parse_verdict(proc.stdout)
        assert verdict is not None
        assert verdict["checks"][0]["ok"] is True
        assert "second" in verdict["checks"][0]["output_tail"]

    def test_verdict_reports_the_orchestrator_resolved_sha(self, runner_repo):
        """The gate names the tree the orchestrator actually checked out."""
        proc = _run_runner(
            runner_repo.path,
            runner_repo.first,
            [{"name": "test", "command": "true"}],
        )
        verdict = runner.parse_verdict(proc.stdout)
        assert verdict["commit_sha"] == runner_repo.first

    def test_program_invokes_no_git_at_all(self):
        """Regression guard for the blocking defect this fix closes.

        The pod's ``git`` is the gateway wrapper, which cannot perform a
        detaching checkout in a branch-locked pipeline session — it 403s
        on the branch lock and has no ``--detach`` in the ``checkout``
        policy's allowed-flag set. Any git the runner program grows back
        would fail there while passing a host-git test fixture, and the
        gate would silently degrade to checking the branch tip.
        """
        program = runner._RUNNER_PROGRAM
        assert '"git"' not in program
        assert "'git'" not in program
        assert "def run_git" not in program

    def test_verdict_records_the_exact_command(self, runner_repo):
        """A narrowed run must be readable as narrowed (#3669)."""
        proc = _run_runner(
            runner_repo.path,
            runner_repo.second,
            [{"name": "test", "command": "echo make-test-all-would-run-here"}],
        )
        verdict = runner.parse_verdict(proc.stdout)
        assert verdict["checks"][0]["command"] == "echo make-test-all-would-run-here"

    def test_red_check_reported_with_exit_code_and_output(self, runner_repo):
        proc = _run_runner(
            runner_repo.path,
            runner_repo.second,
            [{"name": "test", "command": "echo 'FAILED test_x' && exit 3"}],
        )
        assert proc.returncode == 0, "the harness worked; the verdict carries the red"
        verdict = runner.parse_verdict(proc.stdout)
        entry = verdict["checks"][0]
        assert entry["ok"] is False
        assert entry["exit_code"] == 3
        assert "FAILED test_x" in entry["output_tail"]
        assert entry["infra"] is None

    def test_infra_signature_tags_the_red(self, runner_repo):
        sig = runner._INFRA_LINE_SIGNATURES[0]
        proc = _run_runner(
            runner_repo.path,
            runner_repo.second,
            [{"name": "test", "command": f"echo '{sig}' && exit 1"}],
        )
        verdict = runner.parse_verdict(proc.stdout)
        assert verdict["checks"][0]["infra"] == sig

    def test_signature_printed_mid_line_does_not_tag(self, runner_repo):
        """Whole-line matching: the gate must not fail its own red open."""
        sig = runner._INFRA_LINE_SIGNATURES[0]
        proc = _run_runner(
            runner_repo.path,
            runner_repo.second,
            [{"name": "test", "command": f"echo \"E  assert x == '{sig}'\" && exit 1"}],
        )
        verdict = runner.parse_verdict(proc.stdout)
        assert verdict["checks"][0]["infra"] is None

    def test_green_check_is_never_tagged_infra(self, runner_repo):
        sig = runner._INFRA_SUBSTRING_SIGNATURES[0]
        proc = _run_runner(
            runner_repo.path,
            runner_repo.second,
            [{"name": "test", "command": f"echo '{sig}'"}],
        )
        verdict = runner.parse_verdict(proc.stdout)
        assert verdict["checks"][0]["ok"] is True
        assert verdict["checks"][0]["infra"] is None

    def test_missing_required_prebuilt_exits_nonzero(self, runner_repo):
        proc = _run_runner(
            runner_repo.path,
            runner_repo.second,
            [{"name": "test", "command": "true"}],
            EGG_PROPOSE_CHECK_REQUIRE_PREBUILT="1",
        )
        assert proc.returncode != 0
        assert runner.parse_verdict(proc.stdout) is None
        assert "prebuilt" in proc.stderr

    def test_checks_run_sequentially_and_all_are_reported(self, runner_repo):
        proc = _run_runner(
            runner_repo.path,
            runner_repo.second,
            [
                {"name": "lint", "command": "true"},
                {"name": "test", "command": "false"},
            ],
        )
        verdict = runner.parse_verdict(proc.stdout)
        assert [c["name"] for c in verdict["checks"]] == ["lint", "test"]
        assert [c["ok"] for c in verdict["checks"]] == [True, False]

    def test_prebuilt_snapshot_actually_lands_in_the_worktree(self, runner_repo, tmp_path):
        """The matcher is only useful if a matching snapshot is restored.

        ``restore_prebuilt`` keys on ``entry.endswith("--" + name)``
        where ``name`` is the worktree's basename — so a
        ``/opt/prebuilt-deps/owner--repo`` snapshot restores into a
        worktree named ``repo``. Only asserting the *negative* (a
        missing snapshot is tolerated) would leave the matcher free to
        never match anything.
        """
        prebuilt = tmp_path / "prebuilt"
        (prebuilt / "owner--repo" / ".venv" / "bin").mkdir(parents=True)
        (prebuilt / "owner--repo" / ".venv" / "bin" / "pytest").write_text("#!/bin/sh\n")
        # A snapshot for a *different* repo must not be restored here.
        (prebuilt / "owner--other").mkdir()
        (prebuilt / "owner--other" / "STRAY").write_text("no\n")

        proc = _run_runner(
            runner_repo.path,
            runner_repo.second,
            [{"name": "test", "command": "true"}],
            EGG_PROPOSE_CHECK_PREBUILT_BASE=str(prebuilt),
            EGG_PROPOSE_CHECK_REQUIRE_PREBUILT="1",
        )
        assert proc.returncode == 0, proc.stderr
        assert runner.parse_verdict(proc.stdout) is not None
        assert (runner_repo.path / ".venv" / "bin" / "pytest").exists()
        assert not (runner_repo.path / "STRAY").exists()


# ==========================================================================
# _materialise_proposed_tree — the orchestrator-side detach
# ==========================================================================


class TestMaterialiseProposedTree:
    def _call(self, repo_path, sha, spawner=None):
        return runner._materialise_proposed_tree(
            worktree_path=str(repo_path),
            commit_sha=sha,
            pipeline_id="issue-42",
            spawner=spawner or MagicMock(),
            gateway_mode="public",
        )

    def test_detaches_to_the_proposed_sha_not_the_branch_tip(self, runner_repo):
        """The producer named a SHA; that is the tree that must be checked."""
        head, reason = self._call(runner_repo.path, runner_repo.first)
        assert reason is None
        assert head == runner_repo.first
        assert (runner_repo.path / "marker.txt").read_text() == "first\n"

    def test_returns_the_full_resolved_sha_for_an_abbreviation(self, runner_repo):
        head, reason = self._call(runner_repo.path, runner_repo.first[:10])
        assert reason is None
        assert head == runner_repo.first

    def test_unresolvable_sha_is_infra_not_red(self, runner_repo):
        """An unmaterialisable tree fails the gate open, never red."""
        head, reason = self._call(runner_repo.path, "0" * 40)
        assert head is None
        assert "could not materialise proposed sha" in reason

    def test_missing_object_triggers_one_gateway_fetch(self, runner_repo):
        """Only the gateway holds credentials, so the fetch goes there."""
        spawner = MagicMock()
        self._call(runner_repo.path, "0" * 40, spawner=spawner)
        spawner.gateway.fetch_worktree_branch.assert_called_once_with(
            "issue-42", str(runner_repo.path), mode="public"
        )

    def test_present_object_skips_the_fetch(self, runner_repo):
        spawner = MagicMock()
        self._call(runner_repo.path, runner_repo.first, spawner=spawner)
        spawner.gateway.fetch_worktree_branch.assert_not_called()

    def test_gateway_fetch_failure_is_infra(self, runner_repo):
        spawner = MagicMock()
        spawner.gateway.fetch_worktree_branch.side_effect = RuntimeError("gateway down")
        head, reason = self._call(runner_repo.path, "0" * 40, spawner=spawner)
        assert head is None
        assert "gateway fetch" in reason and "gateway down" in reason

    def test_git_calls_never_reach_a_shell(self, runner_repo):
        """``commit_sha`` is an agent-supplied payload field.

        If the checkout went through ``bash -c`` this payload would
        create the sentinel. The gate also refuses a non-hex SHA before
        calling this, so it is the second of two guards.
        """
        sentinel = runner_repo.path / "pwned.txt"
        head, reason = self._call(runner_repo.path, f"{runner_repo.second}; touch {sentinel}")
        assert not sentinel.exists()
        assert head is None
        assert reason


# ==========================================================================
# Runner Job manifest — the #3622 budget precedent
# ==========================================================================


def _manifest(**overrides):
    kwargs: dict[str, Any] = {
        "gate_id": "abc123",
        "pipeline_id": "issue-42",
        "commit_sha": "deadbeef",
        "image": "egg:test",
        "checks": [{"name": "test", "command": "make test-all", "narrowed": "false"}],
        "repo_mounts": {"/home/egg/repos/owner--repo": "/host/wt/owner--repo"},
        "repo_dir": "/home/egg/repos/owner--repo",
        "env": {"GATEWAY_URL": "http://gw"},
        "timeout_seconds": 1800,
        "host_uid": 1000,
        "host_gid": 1000,
    }
    kwargs.update(overrides)
    return runner.build_runner_job_manifest(**kwargs)


class TestRunnerJobManifest:
    def test_check_budget_is_pod_level(self):
        """#3622: the kubelet counts this from pod start, not from Job start."""
        manifest = _manifest(timeout_seconds=1800)
        pod_spec = manifest["spec"]["template"]["spec"]
        assert pod_spec["activeDeadlineSeconds"] == 1800

    def test_job_deadline_is_a_strictly_larger_outer_ceiling(self):
        """Scheduling latency is added to the wait, never deducted from the budget."""
        manifest = _manifest(timeout_seconds=1800)
        assert manifest["spec"]["activeDeadlineSeconds"] > 1800
        assert (
            manifest["spec"]["activeDeadlineSeconds"] >= 1800 + runner._POD_SCHEDULING_GRACE_SECONDS
        )

    def test_network_policy_label_present_supervision_labels_absent(self):
        manifest = _manifest()
        labels = manifest["metadata"]["labels"]
        assert labels["app.kubernetes.io/component"] == "agent"
        assert "egg.agent.role" not in labels
        assert "egg.slice.id" not in labels

    def test_env_carries_commands_sha_and_shared_infra_signatures(self):
        manifest = _manifest()
        env = {
            e["name"]: e["value"]
            for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert json.loads(env["EGG_PROPOSE_CHECK_CHECKS"]) == [
            {"name": "test", "command": "make test-all"}
        ]
        assert env["EGG_PROPOSE_CHECK_COMMIT_SHA"] == "deadbeef"
        signatures = json.loads(env["EGG_PROPOSE_CHECK_INFRA_SIGNATURES"])
        assert signatures["line"] == list(runner._INFRA_LINE_SIGNATURES)
        assert signatures["substring"] == list(runner._INFRA_SUBSTRING_SIGNATURES)

    def test_mounts_map_container_path_to_host_worktree(self):
        manifest = _manifest()
        pod_spec = manifest["spec"]["template"]["spec"]
        assert pod_spec["volumes"][0]["hostPath"]["path"] == "/host/wt/owner--repo"
        container = pod_spec["containers"][0]
        assert container["volumeMounts"][0]["mountPath"] == "/home/egg/repos/owner--repo"


class TestSubmitRunnerJob:
    def test_pod_level_deadline_reaches_the_submitted_body(self):
        """The exact drop #3622 documents on the green gate's submitter."""
        k8s = MagicMock()
        runner._submit_runner_job(k8s, "egg-agents", _manifest(timeout_seconds=1234))
        body = k8s.batch_api.create_namespaced_job.call_args.kwargs["body"]
        assert body.spec.template.spec.active_deadline_seconds == 1234
        assert body.spec.active_deadline_seconds > 1234

    def test_every_manifest_field_reaches_the_body(self):
        """Reflection guard: a manifest key the submitter forgets is silent.

        Walk the manifest and assert every leaf value shows up somewhere
        in the submitted V1 object graph. Catches the whole class of
        drop, not just the one #3622 found.
        """
        k8s = MagicMock()
        manifest = _manifest()
        runner._submit_runner_job(k8s, "egg-agents", manifest)
        body = k8s.batch_api.create_namespaced_job.call_args.kwargs["body"]
        rendered = repr(body.to_dict() if hasattr(body, "to_dict") else body)

        missing = []

        def walk(node, path="") -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    walk(value, f"{path}[{i}]")
            else:
                # The runner program is embedded verbatim as a command
                # arg; comparing its repr is noise, and it is covered by
                # the command assertion below.
                if isinstance(node, str) and node is manifest_program:
                    return
                if str(node) not in rendered:
                    missing.append((path, node))

        manifest_program = manifest["spec"]["template"]["spec"]["containers"][0]["command"][2]
        walk(manifest)
        assert not missing, f"manifest fields dropped by _submit_runner_job: {missing}"
        assert body.spec.template.spec.containers[0].command[2] == manifest_program


# ==========================================================================
# Verdict ledger
# ==========================================================================


class TestLedger:
    def test_one_run_per_tree_however_many_proposes(self):
        """Runs ONCE per proposed tree — not per reviewer, not per retry."""
        started = []
        with patch.object(gate.threading, "Thread") as thread_cls:
            thread_cls.side_effect = lambda **kw: SimpleNamespace(
                start=lambda: started.append(kw["args"][0])
            )
            args = {
                "pipeline_id": "issue-42",
                "slice_id": "slice-1",
                "commit_sha": "abc1234",
                "base_branch": "egg/issue-42/slice-1",
                "repo": "owner/repo",
                "checks": [{"name": "test", "command": "make test-all"}],
            }
            first = gate._get_or_start_run(**args)
            second = gate._get_or_start_run(**args)

        assert first is second
        assert len(started) == 1

    def test_distinct_trees_get_distinct_runs(self):
        with patch.object(gate.threading, "Thread") as thread_cls:
            thread_cls.return_value = SimpleNamespace(start=lambda: None)
            base = {
                "pipeline_id": "issue-42",
                "slice_id": None,
                "base_branch": "egg/issue-42",
                "repo": "owner/repo",
                "checks": [{"name": "test", "command": "make test-all"}],
            }
            a = gate._get_or_start_run(commit_sha="aaa1111", **base)
            b = gate._get_or_start_run(commit_sha="bbb2222", **base)
        assert a is not b

    def test_eviction_never_drops_a_run_in_flight(self):
        for i in range(gate._LEDGER_MAX_ENTRIES + 10):
            key = ("p", "", f"sha{i}")
            rec = _record(state="passed", commit_sha=f"sha{i}")
            rec.finished_at = float(i)
            gate._LEDGER[key] = rec
        running_key = ("p", "", "in-flight")
        gate._LEDGER[running_key] = _record(state="running", commit_sha="in-flight")

        with gate._LEDGER_LOCK:
            gate._evict_locked()

        assert running_key in gate._LEDGER
        assert len(gate._LEDGER) <= gate._LEDGER_MAX_ENTRIES + 1

    def test_all_running_over_cap_warns_instead_of_silently_growing(self, caplog):
        """Nothing is evictable, so the operator gets told rather than nothing.

        Dropping a running entry would orphan its thread's verdict; the
        only honest options are to exceed the cap and say so.
        """
        for i in range(gate._LEDGER_MAX_ENTRIES + 5):
            gate._LEDGER[("p", "", f"sha{i}")] = _record(state="running", commit_sha=f"sha{i}")

        with caplog.at_level("WARNING"), gate._LEDGER_LOCK:
            gate._evict_locked()

        assert len(gate._LEDGER) == gate._LEDGER_MAX_ENTRIES + 5
        assert "no finished entries to evict" in caplog.text

    def test_a_green_record_is_never_expired(self):
        rec = _record(state="passed")
        rec.finished_at = time.time() - (gate._FAILED_RECORD_TTL_SECONDS * 10)
        assert gate._failed_record_expired(rec) is False

    def test_a_stale_red_record_expires(self):
        rec = _record(state="failed")
        rec.finished_at = time.time() - (gate._FAILED_RECORD_TTL_SECONDS + 1)
        assert gate._failed_record_expired(rec) is True

    def test_a_fresh_red_record_does_not_expire(self):
        rec = _record(state="failed")
        rec.finished_at = time.time()
        assert gate._failed_record_expired(rec) is False

    def test_an_expired_red_is_re_run_rather_than_replayed(self):
        """A red is a snapshot of the world, and the world moves.

        A flaky check, a fixed upstream dependency, or a restored
        network can all turn the same tree green. Replaying a
        process-lifetime-cached red would wedge the producer against a
        verdict that is no longer true and that they cannot invalidate.
        """
        args = {
            "pipeline_id": "issue-42",
            "slice_id": "slice-1",
            "commit_sha": "abc1234",
            "base_branch": "egg/issue-42/slice-1",
            "repo": "owner/repo",
            "checks": [{"name": "test", "command": "make test-all"}],
        }
        stale = _record(state="failed", slice_id="slice-1", commit_sha="abc1234")
        stale.finished_at = time.time() - (gate._FAILED_RECORD_TTL_SECONDS + 1)
        gate._LEDGER[gate._ledger_key("issue-42", "slice-1", "abc1234")] = stale

        with patch.object(gate.threading, "Thread") as thread_cls:
            thread_cls.return_value = SimpleNamespace(start=lambda: None)
            fresh = gate._get_or_start_run(**args)

        assert fresh is not stale
        assert fresh.state == "running"


# ==========================================================================
# Deferral registry — the producer arm the event loop must hold (#3425)
# ==========================================================================


class TestDeferralRegistry:
    def test_no_deferral_means_no_block(self):
        assert gate.propose_spawn_block_reason("issue-42", "slice-1", "coder") is None

    def test_a_pending_reject_blocks_the_producers_next_spawn(self, monkeypatch):
        """Without this the event loop respawns straight into the same 409.

        Three no-op spawns in a row is the #3425 park threshold, so a
        check run longer than three polls would look like a wedged agent
        rather than a gate doing its job.
        """
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            _seed("running")
            rejection = _reject()

        assert rejection is not None
        assert rejection[2]["status"] == "checks_running"
        assert (
            gate.propose_spawn_block_reason("issue-42", "slice-1", "coder")
            == gate.PROPOSE_BLOCK_REASON
        )

    def test_the_block_is_scoped_to_the_deferred_producer(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            _seed("running")
            _reject()

        assert gate.propose_spawn_block_reason("issue-42", "slice-1", "tester") is None
        assert gate.propose_spawn_block_reason("issue-42", "slice-2", "coder") is None
        assert gate.propose_spawn_block_reason("issue-99", "slice-1", "coder") is None

    def test_the_block_clears_itself_once_the_verdict_lands(self, monkeypatch):
        """Self-cleaning: nothing else has to remember to release the arm."""
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            record = _seed("running")
            _reject()

        record.state = "passed"
        assert gate.propose_spawn_block_reason("issue-42", "slice-1", "coder") is None
        # …and the registry entry is dropped, not merely ignored.
        assert not gate._DEFERRED_PROPOSES

    def test_a_vanished_ledger_record_does_not_block_forever(self):
        """Eviction must not leave the producer arm permanently held."""
        gate._record_deferral("issue-42", "slice-1", "coder", ("issue-42", "slice-1", "gone"))
        assert gate.propose_spawn_block_reason("issue-42", "slice-1", "coder") is None

    def test_reset_ledger_clears_deferrals_too(self):
        gate._record_deferral("issue-42", "slice-1", "coder", ("issue-42", "slice-1", "abc"))
        gate.reset_ledger()
        assert not gate._DEFERRED_PROPOSES


class TestRecordVerdict:
    def test_all_green_passes(self):
        rec = _record(state="running")
        gate._record_verdict(rec, {"checks": [_check(), _check(name="lint")]}, None)
        assert rec.state == "passed"
        assert rec.failed == []

    def test_genuine_red_fails(self):
        rec = _record(state="running")
        gate._record_verdict(rec, {"checks": [_check(ok=False)]}, None)
        assert rec.state == "failed"
        assert [c["name"] for c in rec.failed] == ["test"]

    def test_all_infra_red_fails_open(self):
        """A check that could not run is not a check that passed — or failed."""
        rec = _record(state="running")
        gate._record_verdict(
            rec,
            {"checks": [_check(ok=False, infra="GATEWAY SIDECAR NOT AVAILABLE")]},
            None,
        )
        assert rec.state == "infra"
        assert "GATEWAY SIDECAR NOT AVAILABLE" in (rec.infra_reason or "")

    def test_mixed_reds_block_on_the_genuine_ones_only(self):
        rec = _record(state="running")
        gate._record_verdict(
            rec,
            {
                "checks": [
                    _check(name="lint", ok=False, infra="GATEWAY SIDECAR NOT AVAILABLE"),
                    _check(name="test", ok=False),
                ]
            },
            None,
        )
        assert rec.state == "failed"
        assert [c["name"] for c in rec.failed] == ["test"]

    def test_infra_fail_open_off_blocks_on_every_red(self, monkeypatch):
        monkeypatch.setenv(gate.INFRA_FAIL_OPEN_ENV_VAR, "off")
        rec = _record(state="running")
        gate._record_verdict(
            rec,
            {"checks": [_check(ok=False, infra="GATEWAY SIDECAR NOT AVAILABLE")]},
            None,
        )
        assert rec.state == "failed"

    def test_no_verdict_is_infra(self):
        rec = _record(state="running")
        gate._record_verdict(rec, None, "runner pod did not reach a terminal state")
        assert rec.state == "infra"
        assert rec.infra_reason == "runner pod did not reach a terminal state"

    def test_run_and_record_never_leaves_a_record_running(self):
        """A thread dying with an exception would wedge every propose for that tree."""
        rec = _record(state="running")
        with patch.object(
            gate._runner, "run_propose_checks", side_effect=RuntimeError("spawner exploded")
        ):
            gate._run_and_record(rec)
        assert rec.state == "infra"
        assert "spawner exploded" in (rec.infra_reason or "")


# ==========================================================================
# run_propose_checks — infra fail-open at every step
# ==========================================================================


def _spawner_ok(tmp_path):
    spawner = MagicMock()
    # The gateway names the worktree ``<base>/<container>/<repo_name>``
    # with ``repo_name = repo.split("/")[-1]`` — the bare basename.
    spawner.gateway.create_worktrees.return_value = SimpleNamespace(
        success=True, worktrees={"owner/repo": str(tmp_path / "repo")}, errors=None
    )
    spawner.gateway.register_session.return_value = SimpleNamespace(session_token="tok")
    return spawner


class TestRunProposeChecks:
    def _call(self, spawner, materialise=("abc1234", None)):
        # The orchestrator-side detach runs real git against the mounted
        # worktree; it has its own coverage in
        # ``TestMaterialiseProposedTree``. Stub it here so these tests
        # stay about the Job / session / cleanup path.
        with patch.object(runner, "_materialise_proposed_tree", return_value=materialise):
            return runner.run_propose_checks(
                pipeline_id="issue-42",
                commit_sha="abc1234",
                base_branch="egg/issue-42",
                repo="owner/repo",
                checks=[{"name": "test", "command": "make test-all"}],
                spawner=spawner,
            )

    def test_unmaterialisable_sha_is_infra_and_submits_no_job(self, tmp_path):
        """The gate fails open *before* a pod exists — never a red.

        A SHA that cannot be checked out is an orchestrator-side
        infrastructure failure. Submitting the Job anyway would run the
        checks against whatever tree the worktree happened to hold and
        report a verdict about the wrong code.
        """
        spawner = _spawner_ok(tmp_path)
        with patch.object(runner, "_submit_runner_job") as submit:
            verdict, reason = self._call(spawner, materialise=(None, "detach exploded"))
        assert verdict is None
        assert reason == "detach exploded"
        submit.assert_not_called()
        spawner.gateway.register_session.assert_not_called()
        spawner.gateway.delete_worktrees.assert_called_once()

    def test_job_is_pinned_to_the_resolved_head_not_the_proposed_string(self, tmp_path):
        """An abbreviated propose SHA must not become the verdict's identity."""
        spawner = _spawner_ok(tmp_path)
        with (
            patch.object(runner, "build_runner_job_manifest") as build,
            patch.object(runner, "_submit_runner_job"),
            patch.object(runner, "_wait_for_runner_pod", return_value=None),
        ):
            self._call(spawner, materialise=("f" * 40, None))
        assert build.call_args.kwargs["commit_sha"] == "f" * 40

    def test_worktree_failure_is_infra(self, tmp_path):
        spawner = _spawner_ok(tmp_path)
        spawner.gateway.create_worktrees.side_effect = RuntimeError("no space")
        verdict, reason = self._call(spawner)
        assert verdict is None
        assert "worktree creation failed" in reason

    def test_empty_worktree_result_is_infra(self, tmp_path):
        spawner = _spawner_ok(tmp_path)
        spawner.gateway.create_worktrees.return_value = SimpleNamespace(
            success=False, worktrees={}, errors=["nope"]
        )
        verdict, reason = self._call(spawner)
        assert verdict is None
        assert "no paths" in reason

    def test_session_failure_is_infra_and_cleans_up(self, tmp_path):
        spawner = _spawner_ok(tmp_path)
        spawner.gateway.register_session.side_effect = RuntimeError("gateway down")
        verdict, reason = self._call(spawner)
        assert verdict is None
        assert "session registration failed" in reason
        spawner.gateway.delete_worktrees.assert_called_once()

    def test_submit_failure_is_infra(self, tmp_path):
        spawner = _spawner_ok(tmp_path)
        with patch.object(runner, "_submit_runner_job", side_effect=RuntimeError("apiserver")):
            verdict, reason = self._call(spawner)
        assert verdict is None
        assert "job submit failed" in reason

    def test_pod_never_terminal_is_infra(self, tmp_path):
        spawner = _spawner_ok(tmp_path)
        with (
            patch.object(runner, "_submit_runner_job"),
            patch.object(runner, "_wait_for_runner_pod", return_value=None),
        ):
            verdict, reason = self._call(spawner)
        assert verdict is None
        assert "did not reach a terminal state" in reason

    def test_unparseable_verdict_is_infra(self, tmp_path):
        spawner = _spawner_ok(tmp_path)
        pod = SimpleNamespace(metadata=SimpleNamespace(name="pod-1"))
        with (
            patch.object(runner, "_submit_runner_job"),
            patch.object(runner, "_wait_for_runner_pod", return_value=pod),
            patch.object(runner, "_read_runner_log", return_value="garbage"),
        ):
            verdict, reason = self._call(spawner)
        assert verdict is None
        assert "no parseable verdict" in reason

    def test_verdict_carries_gate_id_and_pod_for_output_reachability(self, tmp_path):
        spawner = _spawner_ok(tmp_path)
        pod = SimpleNamespace(metadata=SimpleNamespace(name="pod-1"))
        log = runner.VERDICT_SENTINEL + json.dumps({"commit_sha": "abc1234", "checks": [_check()]})
        with (
            patch.object(runner, "_submit_runner_job"),
            patch.object(runner, "_wait_for_runner_pod", return_value=pod),
            patch.object(runner, "_read_runner_log", return_value=log),
        ):
            verdict, reason = self._call(spawner)
        assert reason is None
        assert verdict["pod"] == "pod-1"
        assert verdict["gate_id"]

    def test_job_and_session_cleaned_up_after_a_verdict(self, tmp_path):
        spawner = _spawner_ok(tmp_path)
        pod = SimpleNamespace(metadata=SimpleNamespace(name="pod-1"))
        log = runner.VERDICT_SENTINEL + json.dumps({"checks": [_check()]})
        with (
            patch.object(runner, "_submit_runner_job"),
            patch.object(runner, "_wait_for_runner_pod", return_value=pod),
            patch.object(runner, "_read_runner_log", return_value=log),
            patch.object(runner, "_delete_runner_job") as delete_job,
        ):
            self._call(spawner)
        delete_job.assert_called_once()
        spawner.gateway.delete_session_by_container.assert_called_once()
        spawner.gateway.delete_worktrees.assert_called_once()


# ==========================================================================
# The gate itself
# ==========================================================================


_CHECKS = [{"name": "test", "command": "make test-all", "narrowed": "false"}]


def _reject(payload=None, **overrides):
    kwargs: dict[str, Any] = {
        "pipeline_id": "issue-42",
        "repo": "owner/repo",
        "slice_id": "slice-1",
        "producer_role": "coder",
        "commit_sha": "abc1234def",
        "branch": "egg/issue-42",
        "current_phase": "implement",
        "payload": payload,
    }
    kwargs.update(overrides)
    return gate.propose_check_rejection(**kwargs)


def _seed(state, **kwargs):
    """Put a finished record in the ledger for the default gate args."""
    rec = _record(state=state, slice_id="slice-1", **kwargs)
    gate._LEDGER[gate._ledger_key("issue-42", "slice-1", "abc1234def")] = rec
    return rec


class TestProposeCheckRejectionFailOpen:
    def test_gate_off_by_default(self):
        assert _reject() is None

    def test_no_commit_sha_skips(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        assert _reject(commit_sha="") is None

    @pytest.mark.parametrize(
        "sha", ["HEAD", "abc", "abc1234; rm -rf /", "$(whoami)", "main", "a" * 65]
    )
    def test_non_hex_commit_sha_skips(self, monkeypatch, sha):
        """An agent-supplied field that cannot be a tree is not a red."""
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        with patch.object(gate.threading, "Thread") as thread_cls:
            assert _reject(commit_sha=sha) is None
            thread_cls.assert_not_called()

    def test_non_implement_phase_skips(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        for phase in ("refine", "plan", "pr", None):
            assert _reject(current_phase=phase) is None

    def test_phase_scope_is_configurable(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        monkeypatch.setenv(gate.PHASES_ENV_VAR, "implement,plan")
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            _seed("failed", failed=[_check(ok=False)])
            assert _reject(current_phase="plan") is not None

    def test_missing_repo_or_branch_skips(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        assert _reject(repo="") is None
        assert _reject(branch="") is None

    def test_no_configured_checks_skips(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        with patch.object(gate, "gate_checks", return_value=[]):
            assert _reject() is None

    def test_infra_verdict_fails_open_and_records_honestly(self, monkeypatch):
        """A check that could not run is not a check that passed."""
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        payload: dict[str, Any] = {}
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            _seed("infra", infra_reason="runner pod did not reach a terminal state")
            assert _reject(payload=payload) is None
        verified = payload["attestation"]["checks_verified"]
        assert verified["status"] == "infra"
        assert verified["infra_reason"] == "runner pod did not reach a terminal state"


class TestProposeCheckRejectionVerdicts:
    def test_green_verdict_proceeds_and_stamps_the_attestation(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        payload: dict[str, Any] = {"summary": "x", "attestation": {"tests_run": 3}}
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            _seed(
                "passed",
                verdict={
                    "commit_sha": "abc1234def",
                    "gate_id": "g1",
                    "checks": [_check(name="test", command="make test-all")],
                },
            )
            assert _reject(payload=payload) is None

        verified = payload["attestation"]["checks_verified"]
        assert verified["status"] == "passed"
        assert verified["verified_by"] == "system"
        assert verified["commit_sha"] == "abc1234def"
        # command + SHA recorded, so a narrowed run would be visibly narrow
        assert verified["checks"] == [
            {
                "name": "test",
                "command": "make test-all",
                "ok": True,
                "exit_code": 0,
                "narrowed": "false",
            }
        ]
        # pre-existing attestation fields survive
        assert payload["attestation"]["tests_run"] == 3

    def test_red_verdict_rejects_naming_the_check_and_reaching_the_output(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            _seed(
                "failed",
                failed=[
                    _check(
                        name="test",
                        command="make test-all",
                        ok=False,
                        exit_code=1,
                        output_tail="FAILED orchestrator/tests/test_x.py::test_y",
                    )
                ],
                verdict={"gate_id": "g1", "pod": "egg-proposecheck-g1-xyz", "checks": []},
            )
            rejection = _reject()

        assert rejection is not None
        message, status, details = rejection
        assert status == 409
        assert details["status"] == "checks_red"
        assert "test" in message
        assert "make test-all" in message
        assert "FAILED orchestrator/tests/test_x.py::test_y" in message
        # full output is reachable from the envelope
        assert details["pod"] == "egg-proposecheck-g1-xyz"
        assert details["failed_checks"][0]["command"] == "make test-all"
        assert details["failed_checks"][0]["exit_code"] == 1

    def test_red_message_does_not_dangle_the_operator_bypass(self, monkeypatch):
        """The gate switch is an orchestrator env var; agents cannot set it.

        Naming ``EGG_PROPOSE_CHECK_GATE`` in an agent-facing rejection
        offers a way out that does not exist inside the pod, and invites
        a producer to burn a turn trying to disable the gate instead of
        fixing the red check.
        """
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            _seed(
                "failed",
                failed=[_check(name="test", command="make test-all", ok=False, exit_code=1)],
            )
            rejection = _reject()

        assert rejection is not None
        message, _status, _details = rejection
        assert gate.GATE_ENV_VAR not in message

    def test_pending_run_defers_without_recording_the_proposal(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            _seed("running")
            rejection = _reject()

        assert rejection is not None
        message, status, details = rejection
        assert status == 409
        assert details["status"] == "checks_running"
        assert details["commands"] == ["make test-all"]
        assert "has NOT been recorded" in message

    def test_log_mode_never_rejects(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "log")
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            _seed("failed", failed=[_check(ok=False)], verdict={"checks": []})
            assert _reject() is None

    def test_log_mode_starts_the_run_but_does_not_defer(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "log")
        with (
            patch.object(gate, "gate_checks", return_value=_CHECKS),
            patch.object(gate.threading, "Thread") as thread_cls,
        ):
            thread_cls.return_value = SimpleNamespace(start=lambda: None)
            assert _reject() is None
            assert thread_cls.called

    def test_slice_proposals_use_the_slice_integration_branch(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        with (
            patch.object(gate, "gate_checks", return_value=_CHECKS),
            patch.object(gate.threading, "Thread") as thread_cls,
        ):
            thread_cls.return_value = SimpleNamespace(start=lambda: None)
            _reject()
        record = gate._LEDGER[gate._ledger_key("issue-42", "slice-1", "abc1234def")]
        assert record.base_branch == "egg/issue-42/slice-1"

    def test_phase_level_proposals_use_the_pipeline_branch(self, monkeypatch):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        with (
            patch.object(gate, "gate_checks", return_value=_CHECKS),
            patch.object(gate.threading, "Thread") as thread_cls,
        ):
            thread_cls.return_value = SimpleNamespace(start=lambda: None)
            _reject(slice_id=None)
        record = gate._LEDGER[gate._ledger_key("issue-42", None, "abc1234def")]
        assert record.base_branch == "egg/issue-42"


class TestChecksVerifiedAttestation:
    def test_undeclared_full_command_is_recorded_as_unknown_not_full(self):
        """A repo that forgot `full_command` must not inherit the stronger claim."""
        rec = _record(
            state="passed",
            checks=[{"name": "lint", "command": "make lint", "narrowed": "unknown"}],
            verdict={"checks": [_check(name="lint", command="make lint")]},
        )
        assert gate.checks_verified_attestation(rec)["checks"][0]["narrowed"] == "unknown"

    def test_declared_full_command_is_recorded_as_not_narrowed(self):
        rec = _record(
            state="passed",
            checks=[{"name": "test", "command": "make test-all", "narrowed": "false"}],
            verdict={"checks": [_check(name="test", command="make test-all")]},
        )
        assert gate.checks_verified_attestation(rec)["checks"][0]["narrowed"] == "false"

    def test_falls_back_to_the_proposed_sha_when_the_runner_reported_none(self):
        rec = _record(state="passed", verdict={"checks": []})
        assert gate.checks_verified_attestation(rec)["commit_sha"] == "abc1234def"

    def test_prefers_the_sha_the_runner_actually_had_checked_out(self):
        rec = _record(state="passed", verdict={"commit_sha": "ffff999", "checks": []})
        assert gate.checks_verified_attestation(rec)["commit_sha"] == "ffff999"

    def test_stamp_creates_a_missing_attestation_dict(self):
        payload: dict[str, Any] = {"summary": "x"}
        gate._stamp_attestation(payload, _record(state="passed", verdict={"checks": []}), "coder")
        assert payload["attestation"]["checks_verified"]["verified_by"] == "system"

    def test_stamp_leaves_a_non_dict_attestation_alone(self):
        """Already malformed; the failure belongs where it already was."""
        payload: dict[str, Any] = {"attestation": "not-a-dict"}
        gate._stamp_attestation(payload, _record(state="passed", verdict={"checks": []}), "coder")
        assert payload["attestation"] == "not-a-dict"

    def test_stamp_does_not_create_one_for_a_role_with_no_producer_schema(self):
        """Recording evidence must never turn a valid propose into a 400.

        ``handle_propose`` calls ``validate_attestation`` iff the payload's
        attestation is truthy, and that raises for a role with no
        registered schema — so creating a dict just to hold the verdict
        would reject the simplifier's proposal outright.
        """
        from attestation_schemas import PRODUCER_ATTESTATION_MODELS

        assert "simplifier" not in PRODUCER_ATTESTATION_MODELS
        payload: dict[str, Any] = {"summary": "x"}
        gate._stamp_attestation(
            payload, _record(state="passed", verdict={"checks": []}), "simplifier"
        )
        assert "attestation" not in payload

    def test_stamp_extends_an_existing_attestation_for_any_role(self):
        """A role that already attests keeps its fields and gains the verdict."""
        payload: dict[str, Any] = {"attestation": {"draft_reviewed": True}}
        gate._stamp_attestation(
            payload, _record(state="passed", verdict={"checks": []}), "simplifier"
        )
        assert payload["attestation"]["draft_reviewed"] is True
        assert payload["attestation"]["checks_verified"]["verified_by"] == "system"

    def test_stamp_on_a_non_dict_payload_is_a_no_op(self):
        gate._stamp_attestation(None, _record(state="passed", verdict={"checks": []}), "coder")


# ==========================================================================
# ACK-schema attestation: command + SHA (#3669 sub-requirement)
# ==========================================================================


class TestCheckRunAttestation:
    def test_records_command_and_sha(self):
        from attestation_schemas import CheckRunAttestation

        entry = CheckRunAttestation(
            name="test", command="make test-all", commit_sha="abc1234def", passed=True
        )
        assert entry.command == "make test-all"
        assert entry.commit_sha == "abc1234def"

    def test_rejects_a_bare_check_name_as_the_command(self):
        """'test' is not a command; 'make test-all' is."""
        from attestation_schemas import CheckRunAttestation

        with pytest.raises(ValueError, match="exact command"):
            CheckRunAttestation(name="test", command="   ", commit_sha="abc1234def")

    def test_rejects_a_missing_or_bogus_sha(self):
        from attestation_schemas import CheckRunAttestation

        for sha in ("", "  ", "HEAD~1", "abc"):
            with pytest.raises(ValueError, match="commit SHA"):
                CheckRunAttestation(name="test", command="make test-all", commit_sha=sha)

    def test_rejects_an_empty_name(self):
        from attestation_schemas import CheckRunAttestation

        with pytest.raises(ValueError, match="check name"):
            CheckRunAttestation(name=" ", command="make test-all", commit_sha="abc1234def")

    @pytest.mark.parametrize(
        "model_name",
        ["ReviewerCodeAttestation", "ReviewerContractAttestation", "TesterAttestation"],
    )
    def test_checks_run_is_optional_everywhere(self, model_name):
        """Constraint: reviewers are never required to run checks."""
        import attestation_schemas

        model = getattr(attestation_schemas, model_name)
        assert model().checks_run == []

    @pytest.mark.parametrize(
        "model_name", ["ReviewerCodeAttestation", "ReviewerContractAttestation"]
    )
    def test_reviewer_ack_can_carry_a_structured_check_claim(self, model_name):
        import attestation_schemas

        model = getattr(attestation_schemas, model_name)
        instance = model(
            checks_run=[{"name": "test", "command": "make test-all", "commit_sha": "abc1234def"}]
        )
        assert instance.checks_run[0].command == "make test-all"

    def test_strict_reviewer_validation_still_ignores_checks_run(self):
        """Adding the field must not make an ACK harder to submit."""
        from attestation_schemas import (
            AttestationStrictness,
            validate_attestation,
        )

        validated = validate_attestation(
            "reviewer_code",
            {"files_reviewed": ["orchestrator/health_monitor.py"]},
            strictness=AttestationStrictness.STRICT,
            is_producer=False,
        )
        assert validated.checks_run == []

    def test_system_stamped_checks_verified_survives_attestation_validation(self):
        """The gate writes into the same dict ``validate_attestation`` reads.

        If a producer schema rejected the extra key, every accepted
        proposal under the gate would 400 on its own evidence.
        """
        from attestation_schemas import AttestationStrictness, validate_attestation

        rec = _record(state="passed", verdict={"commit_sha": "abc1234", "checks": [_check()]})
        payload: dict[str, Any] = {
            "attestation": {"commit_shas": ["abc1234"], "files_changed": ["a.py"]}
        }
        gate._stamp_attestation(payload, rec, "coder")

        validated = validate_attestation(
            "coder",
            payload["attestation"],
            strictness=AttestationStrictness.STRICT,
            is_producer=True,
        )
        assert validated.commit_shas == ["abc1234"]
        # The evidence stays on the raw payload — which is what the
        # tracker records and the message bus carries.
        assert payload["attestation"]["checks_verified"]["verified_by"] == "system"

    def test_narrowed_command_is_recorded_verbatim_not_normalised(self):
        """The field's whole value is that `make test` reads as `make test`."""
        from attestation_schemas import TesterAttestation

        instance = TesterAttestation(
            tests_run=3,
            checks_passed=["test"],
            checks_run=[{"name": "test", "command": "make test", "commit_sha": "abc1234def"}],
        )
        assert instance.checks_run[0].command == "make test"


# ==========================================================================
# Route integration — CONSENSUS_PROPOSE
# ==========================================================================


@pytest.fixture
def app():
    from flask import Flask
    from routes.signals import signals_bp

    flask_app = Flask(__name__)
    flask_app.register_blueprint(signals_bp)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def implement_pipeline():
    from models import Pipeline, PipelinePhase

    return Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch="egg/issue-42",
        current_phase=PipelinePhase.IMPLEMENT,
    )


def _propose(app, pipeline, tracker, payload=None):
    """Drive ``handle_consensus_propose_signal`` past everything but this gate."""
    store = MagicMock()
    store.load_pipeline.return_value = pipeline

    body = payload or {
        "summary": (
            "Implemented the detection plane wiring and its unit tests; "
            "verified the tick invokes run_detection_plane."
        ),
        "artifacts": ["orchestrator/health_monitor.py"],
        "commit_sha": "abc1234def",
    }

    with (
        app.app_context(),
        patch("routes.signals.get_state_store", return_value=store),
        patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt")),
        patch("routes.signals._verify_commit_on_branch", return_value=True),
        patch("routes.signals._validate_producer_artifacts"),
        patch("routes.signals._contract_completeness_rejection", return_value=None),
        patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
        patch("message_store.get_message_store", return_value=MagicMock()),
    ):
        from routes.signals import handle_consensus_propose_signal

        return handle_consensus_propose_signal(
            "issue-42",
            {"agent_role": "coder", "payload": body},
            Path("/tmp/repo"),
        )


class TestProposeSignalIntegration:
    """Acceptance: red is rejected, green is unaffected."""

    def _tracker(self):
        tracker = MagicMock()
        tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "abc1234def",
            "reviewers": [],
            "stale_reviewers": [],
            "newly_ready": [],
        }
        return tracker

    def test_red_proposal_is_rejected_before_the_tracker_is_touched(
        self, monkeypatch, app, implement_pipeline
    ):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        tracker = self._tracker()
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            gate._LEDGER[gate._ledger_key("issue-42", None, "abc1234def")] = _record(
                state="failed",
                slice_id=None,
                failed=[
                    _check(
                        name="test",
                        command="make test-all",
                        ok=False,
                        output_tail="FAILED test_stray_exit_code_falls_through_to_abnormal[143]",
                    )
                ],
                verdict={"gate_id": "g1", "pod": "pod-1", "checks": []},
            )
            response, status = _propose(app, implement_pipeline, tracker)

        assert status == 409
        data = json.loads(response.data)
        assert data["details"]["status"] == "checks_red"
        assert "test" in data["message"]
        assert "test_stray_exit_code_falls_through_to_abnormal" in data["message"]
        # A red proposal is not reviewable at all: no consensus state moved.
        tracker.handle_propose.assert_not_called()

    def test_green_proposal_takes_the_pre_gate_path_unchanged(
        self, monkeypatch, app, implement_pipeline
    ):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        tracker = self._tracker()
        payload = {
            "summary": (
                "Implemented the detection plane wiring and its unit tests; "
                "verified the tick invokes run_detection_plane."
            ),
            "artifacts": ["orchestrator/health_monitor.py"],
            "commit_sha": "abc1234def",
        }
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            gate._LEDGER[gate._ledger_key("issue-42", None, "abc1234def")] = _record(
                state="passed",
                slice_id=None,
                verdict={
                    "commit_sha": "abc1234def",
                    "gate_id": "g1",
                    "checks": [_check(name="test", command="make test-all")],
                },
            )
            response, status = _propose(app, implement_pipeline, tracker, payload)

        assert status == 200
        tracker.handle_propose.assert_called_once()
        # ... and the system's evidence rode along on the recorded proposal.
        recorded = tracker.handle_propose.call_args[0][1]
        assert recorded["attestation"]["checks_verified"]["status"] == "passed"
        assert recorded["attestation"]["checks_verified"]["checks"][0]["command"] == (
            "make test-all"
        )

    def test_gate_off_leaves_the_propose_path_untouched(self, app, implement_pipeline):
        """Default posture until #3670 is green."""
        tracker = self._tracker()
        with patch.object(gate, "gate_checks") as checks:
            response, status = _propose(app, implement_pipeline, tracker)
        assert status == 200
        tracker.handle_propose.assert_called_once()
        checks.assert_not_called()

    def test_pending_run_defers_the_propose(self, monkeypatch, app, implement_pipeline):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        tracker = self._tracker()
        with patch.object(gate, "gate_checks", return_value=_CHECKS):
            gate._LEDGER[gate._ledger_key("issue-42", None, "abc1234def")] = _record(
                state="running", slice_id=None
            )
            response, status = _propose(app, implement_pipeline, tracker)

        assert status == 409
        assert json.loads(response.data)["details"]["status"] == "checks_running"
        tracker.handle_propose.assert_not_called()

    def test_gate_exception_never_blocks_a_propose(self, monkeypatch, app, implement_pipeline):
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        tracker = self._tracker()
        with patch.object(gate, "propose_check_rejection", side_effect=RuntimeError("gate bug")):
            response, status = _propose(app, implement_pipeline, tracker)
        assert status == 200
        tracker.handle_propose.assert_called_once()

    def test_no_op_propose_is_not_gated(self, monkeypatch, app, implement_pipeline):
        """A no-op carries no tree; there is nothing to check."""
        monkeypatch.setenv(gate.GATE_ENV_VAR, "on")
        tracker = self._tracker()
        payload = {
            "summary": (
                "No documentation surface is touched by this slice's diff, so "
                "this producer has no work to contribute here."
            ),
            "artifacts": [],
            "commit_sha": "",
            "no_changes_needed": True,
            "no_changes_reason": "no doc surface in this slice",
        }
        with patch.object(gate, "gate_checks") as checks:
            response, status = _propose(app, implement_pipeline, tracker, payload)
        assert status == 200
        checks.assert_not_called()


# ==========================================================================
# Cross-module: one infra vocabulary (#3621)
# ==========================================================================


class TestSharedInfraVocabulary:
    def test_signatures_are_the_same_objects_as_the_green_gate(self):
        """Two gates, one definition of "this could not run"."""
        import slice_green_gate

        assert runner._INFRA_LINE_SIGNATURES is slice_green_gate._INFRA_LINE_SIGNATURES
        assert runner._INFRA_SUBSTRING_SIGNATURES is slice_green_gate._INFRA_SUBSTRING_SIGNATURES

    def test_green_gate_default_is_untouched(self):
        """This change must not weaken or bypass the #3398 green gate."""
        import slice_green_gate

        assert slice_green_gate._DEFAULT_MODE == "on"
        assert slice_green_gate.green_gate_mode() == "on"

    def test_green_gate_still_runs_the_narrowed_command(self):
        """The two gates deliberately run different forms of the same check."""
        import slice_green_gate

        configured = [{"name": "test", "command": "make test", "full_command": "make test-all"}]
        with patch("config.repo_config.get_repo_checks", return_value=configured):
            assert slice_green_gate._gate_checks("owner/repo")[0]["command"] == "make test"
            assert gate.gate_checks("owner/repo")[0]["command"] == "make test-all"
