"""Tests for the slice-close evidence-reachability gate (#3125).

The slice integration branch only advances when a producer pushes
(``consensus_push`` at propose time). A commit recorded by the
prescribed post-confirmation unblock flow (``egg-contract complete-task
--commit <sha>``, #3124) lives only on the agent's local worktree
branch, so a slice could close and open its PR without a deliverable
its own task record cites as completion evidence.

Covers:

* ``contract_completeness.evidence_commits`` / ``format_evidence_rows``
  — row selection (any row citing a commit, regardless of status), the
  unknown-slice ``None`` sentinel, and the independent kill switch.
* ``GatewayClient.find_unreachable_evidence_commits`` — the tri-state
  merge-base mapping (exit 0 reachable, exit 1 / 128 unreachable,
  anything else → ``None`` skip), and the skip-don't-fail posture on
  tip-resolution and fetch failures.
* ``routes.pipelines._check_slice_evidence_reachability`` — gate
  wiring: kill switch, graceful degradation on contract/probe
  failures, and the failure string on a definitive unreachable
  verdict.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Mock heavy dependencies before importing routes.pipelines.
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))
_shared_path = _orchestrator_path.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

import contract_completeness as cc  # noqa: E402
from gateway_client import GatewayClient, GatewayError, SessionInfo  # noqa: E402
from routes.pipelines import _check_slice_evidence_reachability  # noqa: E402

PIPELINE_ID = "pipeline-evidence-test"
INTEGRATION_BRANCH = "egg/issue-3125/slice-2"
TIP_SHA = "f" * 40
PUSHED_SHA = "a" * 40
LATE_SHA = "b" * 40


# ----------------------------------------------------------------------
# Contract fixtures
# ----------------------------------------------------------------------


def _contract_dict() -> dict[str, Any]:
    """Two-slice contract.

    slice-2 rows:
      * task-2-1 coder      complete, commit PUSHED_SHA
      * task-2-2 documenter complete, commit LATE_SHA (the unblock-flow row)
      * task-2-3 coder      pending,  commit LATE_SHA (commit linked, not yet flipped)
      * task-2-4 (no role)  pending,  no commit
    """
    return {
        "schemaVersion": "1.0",
        "issue": {"number": 3125, "title": "evidence test", "url": "http://example"},
        "phases": [
            {
                "id": "slice-1",
                "name": "first",
                "tasks": [
                    {
                        "id": "task-1-1",
                        "description": "other slice",
                        "role": "coder",
                        "status": "complete",
                        "commit": "c" * 8,
                    },
                ],
            },
            {
                "id": "slice-2",
                "name": "second",
                "tasks": [
                    {
                        "id": "task-2-1",
                        "description": "pushed work",
                        "role": "coder",
                        "status": "complete",
                        "commit": PUSHED_SHA,
                    },
                    {
                        "id": "task-2-2",
                        "description": "late operator commit",
                        "role": "documenter",
                        "status": "complete",
                        "commit": LATE_SHA,
                    },
                    {
                        "id": "task-2-3",
                        "description": "commit linked before status flip",
                        "role": "coder",
                        "status": "pending",
                        "commit": LATE_SHA,
                    },
                    {
                        "id": "task-2-4",
                        "description": "no evidence cited",
                        "status": "pending",
                    },
                ],
            },
        ],
    }


def _write_contract(worktree: Path, identifier: str = PIPELINE_ID) -> Path:
    contracts_dir = worktree / ".egg-state" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    path = contracts_dir / f"{identifier}.json"
    path.write_text(json.dumps(_contract_dict()))
    return path


def _load(worktree: Path):
    from egg_contracts.loader import load_contract

    return load_contract(PIPELINE_ID, worktree)


# ----------------------------------------------------------------------
# contract_completeness helpers
# ----------------------------------------------------------------------


class TestEvidenceCommits:
    def test_rows_with_commits_only(self, tmp_path: Path) -> None:
        _write_contract(tmp_path)
        rows = cc.evidence_commits(_load(tmp_path), "slice-2")
        assert rows is not None
        assert [r["id"] for r in rows] == ["task-2-1", "task-2-2", "task-2-3"]

    def test_pending_row_with_commit_included(self, tmp_path: Path) -> None:
        _write_contract(tmp_path)
        rows = cc.evidence_commits(_load(tmp_path), "slice-2")
        assert rows is not None
        assert any(r["id"] == "task-2-3" for r in rows)

    def test_unknown_slice_returns_none_sentinel(self, tmp_path: Path) -> None:
        _write_contract(tmp_path)
        assert cc.evidence_commits(_load(tmp_path), "slice-9") is None

    def test_no_slice_id_scans_all_slices(self, tmp_path: Path) -> None:
        _write_contract(tmp_path)
        rows = cc.evidence_commits(_load(tmp_path), None)
        assert rows is not None
        assert {r["id"] for r in rows} == {"task-1-1", "task-2-1", "task-2-2", "task-2-3"}

    def test_rows_carry_id_role_commit(self, tmp_path: Path) -> None:
        _write_contract(tmp_path)
        rows = cc.evidence_commits(_load(tmp_path), "slice-2")
        assert rows is not None
        assert rows[1] == {"id": "task-2-2", "role": "documenter", "commit": LATE_SHA}

    def test_roleless_row_with_orphan_commit_excluded(self, tmp_path: Path) -> None:
        # #3339: a task the planner left unassigned (role=None) can still
        # acquire a commit (a stray add-commit, or _merge_preserved_slice_
        # runtime re-attaching one across a restart_phase re-fork). That SHA
        # is bookkeeping, not a producer deliverable, so the gate must not
        # see it — otherwise an orphan role=unassigned commit fails a
        # consensus-reached slice and cascades the whole phase.
        contract = _contract_dict()
        slice2 = contract["phases"][1]
        roleless = slice2["tasks"][3]
        assert roleless["id"] == "task-2-4"
        assert "role" not in roleless  # planner left it unassigned
        roleless["commit"] = "e" * 40  # the orphan SHA from #3339
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        (contracts_dir / f"{PIPELINE_ID}.json").write_text(json.dumps(contract))

        rows = cc.evidence_commits(_load(tmp_path), "slice-2")
        assert rows is not None
        ids = [r["id"] for r in rows]
        assert "task-2-4" not in ids
        assert ids == ["task-2-1", "task-2-2", "task-2-3"]

    def test_format_evidence_rows(self) -> None:
        text = cc.format_evidence_rows(
            [
                {"id": "task-2-2", "role": "documenter", "commit": "abc1234"},
                {"id": "task-2-4", "role": None, "commit": "def5678"},
            ]
        )
        assert "task-2-2 (role=documenter, commit=abc1234)" in text
        assert "task-2-4 (role=unassigned, commit=def5678)" in text


class TestEvidenceGateEnabled:
    def test_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(cc.EVIDENCE_GATE_ENV_VAR, raising=False)
        assert cc.evidence_gate_enabled() is True

    @pytest.mark.parametrize("value", ["off", "0", "false", "no", " OFF "])
    def test_kill_switch(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(cc.EVIDENCE_GATE_ENV_VAR, value)
        assert cc.evidence_gate_enabled() is False

    def test_independent_of_ack_gate_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(cc.GATE_ENV_VAR, "off")
        monkeypatch.delenv(cc.EVIDENCE_GATE_ENV_VAR, raising=False)
        assert cc.evidence_gate_enabled() is True


# ----------------------------------------------------------------------
# GatewayClient.find_unreachable_evidence_commits
# ----------------------------------------------------------------------


@pytest.fixture
def gateway_client() -> GatewayClient:
    return GatewayClient(
        gateway_host="localhost",
        gateway_port=19848,
        launcher_secret="test-secret",
        timeout=5,
    )


def _session_info(token: str = "synthetic-tok") -> SessionInfo:
    now = datetime.now()
    return SessionInfo(
        session_token=token,
        container_id="temp",
        container_ip=None,
        mode="public",
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


def _stub_helpers(
    client: GatewayClient,
    *,
    tip_sha: str | None = TIP_SHA,
    fetch_returns: bool = True,
):
    return (
        patch.object(client, "register_session", return_value=_session_info()),
        patch.object(client, "delete_session", return_value=True),
        patch.object(client, "get_remote_branch_sha", return_value=tip_sha),
        patch.object(client, "fetch_branch", return_value=fetch_returns),
    )


def _gateway_error(returncode: int | None) -> GatewayError:
    details = {"returncode": returncode} if returncode is not None else {}
    return GatewayError("git execute failed", details=details)


class TestFindUnreachableEvidenceCommits:
    def test_empty_input_short_circuits(self, gateway_client: GatewayClient) -> None:
        with patch.object(gateway_client, "register_session") as mock_register:
            result = gateway_client.find_unreachable_evidence_commits(
                PIPELINE_ID,
                "/repo",
                commit_shas=[],
                integration_branch=INTEGRATION_BRANCH,
            )
        assert result == []
        mock_register.assert_not_called()

    def test_empty_integration_branch_skips(self, gateway_client: GatewayClient) -> None:
        # An empty branch means we cannot probe reachability at all; skip
        # the gate rather than silently approve. The caller's
        # ``pipeline.repo`` guard makes this unreachable in production,
        # but the defensive default matters if a future caller passes
        # through with an empty branch.
        with patch.object(gateway_client, "register_session") as mock_register:
            result = gateway_client.find_unreachable_evidence_commits(
                PIPELINE_ID,
                "/repo",
                commit_shas=[PUSHED_SHA],
                integration_branch="",
            )
        assert result is None
        mock_register.assert_not_called()

    def test_all_reachable(self, gateway_client: GatewayClient) -> None:
        stubs = _stub_helpers(gateway_client)
        with (
            stubs[0],
            stubs[1],
            stubs[2],
            stubs[3],
            patch.object(gateway_client, "_make_request", return_value={"success": True}),
        ):
            result = gateway_client.find_unreachable_evidence_commits(
                PIPELINE_ID,
                "/repo",
                commit_shas=[PUSHED_SHA, LATE_SHA],
                integration_branch=INTEGRATION_BRANCH,
            )
        assert result == []

    @pytest.mark.parametrize("returncode", [1, 128])
    def test_not_ancestor_and_missing_object_flagged(
        self, gateway_client: GatewayClient, returncode: int
    ) -> None:
        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            assert endpoint == "/api/v1/git/execute"
            assert data["operation"] == "merge-base"
            assert data["args"][0] == "--is-ancestor"
            assert data["args"][2] == TIP_SHA
            if data["args"][1] == LATE_SHA:
                raise _gateway_error(returncode)
            return {"success": True}

        stubs = _stub_helpers(gateway_client)
        with (
            stubs[0],
            stubs[1],
            stubs[2],
            stubs[3],
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            result = gateway_client.find_unreachable_evidence_commits(
                PIPELINE_ID,
                "/repo",
                commit_shas=[PUSHED_SHA, LATE_SHA],
                integration_branch=INTEGRATION_BRANCH,
            )
        assert result == [LATE_SHA]

    def test_unexpected_merge_base_failure_skips(self, gateway_client: GatewayClient) -> None:
        stubs = _stub_helpers(gateway_client)
        with (
            stubs[0],
            stubs[1],
            stubs[2],
            stubs[3],
            patch.object(gateway_client, "_make_request", side_effect=_gateway_error(None)),
        ):
            result = gateway_client.find_unreachable_evidence_commits(
                PIPELINE_ID,
                "/repo",
                commit_shas=[LATE_SHA],
                integration_branch=INTEGRATION_BRANCH,
            )
        assert result is None

    def test_unresolvable_tip_skips(self, gateway_client: GatewayClient) -> None:
        stubs = _stub_helpers(gateway_client, tip_sha=None)
        with stubs[0], stubs[1], stubs[2], stubs[3]:
            result = gateway_client.find_unreachable_evidence_commits(
                PIPELINE_ID,
                "/repo",
                commit_shas=[LATE_SHA],
                integration_branch=INTEGRATION_BRANCH,
            )
        assert result is None

    def test_failed_fetch_skips(self, gateway_client: GatewayClient) -> None:
        stubs = _stub_helpers(gateway_client, fetch_returns=False)
        with stubs[0], stubs[1], stubs[2], stubs[3]:
            result = gateway_client.find_unreachable_evidence_commits(
                PIPELINE_ID,
                "/repo",
                commit_shas=[LATE_SHA],
                integration_branch=INTEGRATION_BRANCH,
            )
        assert result is None

    def test_session_registration_failure_skips(self, gateway_client: GatewayClient) -> None:
        with patch.object(gateway_client, "register_session", side_effect=GatewayError("down")):
            result = gateway_client.find_unreachable_evidence_commits(
                PIPELINE_ID,
                "/repo",
                commit_shas=[LATE_SHA],
                integration_branch=INTEGRATION_BRANCH,
            )
        assert result is None

    def test_session_cleaned_up(self, gateway_client: GatewayClient) -> None:
        stubs = _stub_helpers(gateway_client)
        with (
            stubs[0],
            stubs[1] as mock_delete,
            stubs[2],
            stubs[3],
            patch.object(gateway_client, "_make_request", return_value={"success": True}),
        ):
            gateway_client.find_unreachable_evidence_commits(
                PIPELINE_ID,
                "/repo",
                commit_shas=[PUSHED_SHA],
                integration_branch=INTEGRATION_BRANCH,
            )
        mock_delete.assert_called_once_with("synthetic-tok")


# ----------------------------------------------------------------------
# routes.pipelines._check_slice_evidence_reachability
# ----------------------------------------------------------------------


def _spawner(unreachable: list[str] | None) -> MagicMock:
    spawner = MagicMock()
    spawner.gateway.find_unreachable_evidence_commits.return_value = unreachable
    return spawner


@pytest.fixture
def gate_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(cc.EVIDENCE_GATE_ENV_VAR, raising=False)
    return monkeypatch


class TestCheckSliceEvidenceReachability:
    def test_kill_switch_skips_without_gateway_call(
        self, tmp_path: Path, gate_env: pytest.MonkeyPatch
    ) -> None:
        gate_env.setenv(cc.EVIDENCE_GATE_ENV_VAR, "off")
        _write_contract(tmp_path)
        spawner = _spawner([LATE_SHA])
        result = _check_slice_evidence_reachability(
            PIPELINE_ID, spawner, tmp_path, "slice-2", INTEGRATION_BRANCH
        )
        assert result is None
        spawner.gateway.find_unreachable_evidence_commits.assert_not_called()

    def test_missing_contract_skips(self, tmp_path: Path, gate_env) -> None:
        spawner = _spawner([LATE_SHA])
        result = _check_slice_evidence_reachability(
            PIPELINE_ID, spawner, tmp_path, "slice-2", INTEGRATION_BRANCH
        )
        assert result is None
        spawner.gateway.find_unreachable_evidence_commits.assert_not_called()

    def test_unknown_slice_skips(self, tmp_path: Path, gate_env) -> None:
        _write_contract(tmp_path)
        spawner = _spawner([LATE_SHA])
        result = _check_slice_evidence_reachability(
            PIPELINE_ID, spawner, tmp_path, "slice-9", INTEGRATION_BRANCH
        )
        assert result is None
        spawner.gateway.find_unreachable_evidence_commits.assert_not_called()

    def test_no_cited_commits_skips_probe(self, tmp_path: Path, gate_env) -> None:
        contract = _contract_dict()
        for task in contract["phases"][1]["tasks"]:
            task.pop("commit", None)
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        (contracts_dir / f"{PIPELINE_ID}.json").write_text(json.dumps(contract))

        spawner = _spawner([LATE_SHA])
        result = _check_slice_evidence_reachability(
            PIPELINE_ID, spawner, tmp_path, "slice-2", INTEGRATION_BRANCH
        )
        assert result is None
        spawner.gateway.find_unreachable_evidence_commits.assert_not_called()

    def test_all_reachable_passes(self, tmp_path: Path, gate_env) -> None:
        _write_contract(tmp_path)
        spawner = _spawner([])
        result = _check_slice_evidence_reachability(
            PIPELINE_ID, spawner, tmp_path, "slice-2", INTEGRATION_BRANCH
        )
        assert result is None
        call_kwargs = spawner.gateway.find_unreachable_evidence_commits.call_args.kwargs
        # task-2-2 and task-2-3 cite the same LATE_SHA — the probe input
        # is de-duplicated so each unique SHA is round-tripped once.
        # Order is first-seen by slice-task iteration.
        assert call_kwargs["commit_shas"] == [PUSHED_SHA, LATE_SHA]
        assert call_kwargs["integration_branch"] == INTEGRATION_BRANCH

    def test_probe_failure_skips(self, tmp_path: Path, gate_env) -> None:
        _write_contract(tmp_path)
        result = _check_slice_evidence_reachability(
            PIPELINE_ID, _spawner(None), tmp_path, "slice-2", INTEGRATION_BRANCH
        )
        assert result is None

    def test_unreachable_evidence_fails_with_rows(self, tmp_path: Path, gate_env) -> None:
        _write_contract(tmp_path)
        result = _check_slice_evidence_reachability(
            PIPELINE_ID, _spawner([LATE_SHA]), tmp_path, "slice-2", INTEGRATION_BRANCH
        )
        assert result is not None
        # Both rows citing the lost SHA are named; the pushed row is not.
        assert "task-2-2" in result
        assert "task-2-3" in result
        assert "task-2-1" not in result
        assert LATE_SHA in result
        assert INTEGRATION_BRANCH in result
        assert cc.EVIDENCE_GATE_ENV_VAR in result

    def test_roleless_orphan_commit_does_not_fail_slice(self, tmp_path: Path, gate_env) -> None:
        # #3339 regression: a slice whose only commit-bearing task is a
        # role=unassigned orphan must close cleanly. The gateway reports
        # the orphan SHA unreachable, but the roleless row is never put to
        # the probe, so the gate returns None instead of failing the
        # consensus-reached slice and cascading the phase.
        contract = _contract_dict()
        slice2 = contract["phases"][1]
        for task in slice2["tasks"]:
            task.pop("commit", None)  # drop the producer rows' commits
        slice2["tasks"][3]["commit"] = "e" * 40  # task-2-4, roleless orphan
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        (contracts_dir / f"{PIPELINE_ID}.json").write_text(json.dumps(contract))

        spawner = _spawner(["e" * 40])  # gateway would flag it unreachable
        result = _check_slice_evidence_reachability(
            PIPELINE_ID, spawner, tmp_path, "slice-2", INTEGRATION_BRANCH
        )
        assert result is None
        spawner.gateway.find_unreachable_evidence_commits.assert_not_called()

    def test_pre_loaded_contract_skips_internal_load(self, tmp_path: Path, gate_env) -> None:
        """When the caller pre-loads the contract (the close-path
        does this to reuse one load for both the gate and the slice
        PR data snapshot — #3125 review), the gate uses the supplied
        contract and does NOT re-read from disk.
        """
        # Write contract to disk so the on-disk fallback would also
        # work; we then load it ourselves to simulate the caller
        # pre-loading. The gate's internal load is monkey-patched to
        # raise so we can be sure the pre-loaded path is taken.
        _write_contract(tmp_path)
        preloaded = _load(tmp_path)

        with patch(
            "egg_contracts.loader.load_contract",
            side_effect=RuntimeError("internal load should not run"),
        ):
            result = _check_slice_evidence_reachability(
                PIPELINE_ID,
                _spawner([]),
                tmp_path,
                "slice-2",
                INTEGRATION_BRANCH,
                contract=preloaded,
            )
        assert result is None
