"""Tests for the contract-task completeness gate (#3114).

Covers:

* ``contract_completeness`` module — slice/role scoping, the
  unknown-slice ``None`` sentinel, the kill switch, and graceful
  contract loading.
* ``routes.signals._contract_completeness_rejection`` — the three
  checks (enforcer ACK, enforcer CONFIRM, no-op propose), the
  attestation requirement/cross-check on passing ACKs, and the
  fail-open posture on orchestrator-side read failures.
* ``review_graph`` — the enforcer's CRITICAL edges to every producer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

# Add orchestrator and shared to path (matches test_signals.py)
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

import contract_completeness as cc  # noqa: E402
from egg_contracts.models import Contract  # noqa: E402

PIPELINE_ID = "pipeline-gate-test"


def _contract_dict(
    *,
    slice2_complete: bool = False,
) -> dict[str, Any]:
    """Two-slice contract: slice-1 fully complete, slice-2 mixed.

    slice-2 rows (when ``slice2_complete`` is False):
      * task-2-1 coder      complete
      * task-2-2 coder      pending
      * task-2-3 documenter pending
      * task-2-4 (no role)  pending
    """
    slice2_status = "complete" if slice2_complete else "pending"
    return {
        "schemaVersion": "1.0",
        "issue": {"number": 42, "title": "gate test", "url": "http://example"},
        "phases": [
            {
                "id": "slice-1",
                "name": "first",
                "tasks": [
                    {
                        "id": "task-1-1",
                        "description": "done work",
                        "role": "coder",
                        "status": "complete",
                        "commit": "a" * 8,
                    },
                ],
            },
            {
                "id": "slice-2",
                "name": "second",
                "tasks": [
                    {
                        "id": "task-2-1",
                        "description": "delivered",
                        "role": "coder",
                        "status": "complete",
                        "commit": "b" * 8,
                    },
                    {
                        "id": "task-2-2",
                        "description": "open coder work",
                        "role": "coder",
                        "status": slice2_status,
                    },
                    {
                        "id": "task-2-3",
                        "description": "open documenter work",
                        "role": "documenter",
                        "status": slice2_status,
                    },
                    {
                        "id": "task-2-4",
                        "description": "unassigned row",
                        "status": slice2_status,
                    },
                ],
            },
        ],
    }


def _make_contract(**kwargs: Any) -> Contract:
    return Contract.model_validate(_contract_dict(**kwargs))


def _write_contract(
    worktree: Path,
    identifier: str = PIPELINE_ID,
    **kwargs: Any,
) -> Path:
    contracts_dir = worktree / ".egg-state" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    path = contracts_dir / f"{identifier}.json"
    path.write_text(json.dumps(_contract_dict(**kwargs)))
    return path


class TestIncompleteTasks:
    def test_slice_and_role_scoping(self) -> None:
        contract = _make_contract()
        rows = cc.incomplete_tasks(contract, "slice-2", role="coder")
        assert [r["id"] for r in rows] == ["task-2-2"]

    def test_role_none_includes_unassigned_rows(self) -> None:
        contract = _make_contract()
        rows = cc.incomplete_tasks(contract, "slice-2")
        assert {r["id"] for r in rows} == {"task-2-2", "task-2-3", "task-2-4"}

    def test_complete_slice_returns_empty(self) -> None:
        contract = _make_contract()
        assert cc.incomplete_tasks(contract, "slice-1") == []

    def test_unknown_slice_returns_none_sentinel(self) -> None:
        contract = _make_contract()
        assert cc.incomplete_tasks(contract, "slice-99") is None

    def test_no_slice_id_scans_all_slices(self) -> None:
        contract = _make_contract()
        rows = cc.incomplete_tasks(contract, None)
        assert {r["id"] for r in rows} == {"task-2-2", "task-2-3", "task-2-4"}

    def test_rows_carry_id_role_status_commit(self) -> None:
        contract = _make_contract()
        (row,) = cc.incomplete_tasks(contract, "slice-2", role="coder")
        assert row == {
            "id": "task-2-2",
            "role": "coder",
            "status": "pending",
            "commit": None,
        }


class TestTaskIdHelpers:
    def test_task_ids_for_role(self) -> None:
        contract = _make_contract()
        assert cc.task_ids_for_role(contract, "slice-2", "coder") == {
            "task-2-1",
            "task-2-2",
        }

    def test_task_ids_for_role_unknown_slice(self) -> None:
        contract = _make_contract()
        assert cc.task_ids_for_role(contract, "slice-99", "coder") is None

    def test_all_task_ids(self) -> None:
        contract = _make_contract()
        assert cc.all_task_ids(contract, "slice-2") == {
            "task-2-1",
            "task-2-2",
            "task-2-3",
            "task-2-4",
        }


class TestGateEnabled:
    def test_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(cc.GATE_ENV_VAR, raising=False)
        assert cc.gate_enabled() is True

    @pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "no"])
    def test_kill_switch(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(cc.GATE_ENV_VAR, value)
        assert cc.gate_enabled() is False

    def test_other_values_stay_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(cc.GATE_ENV_VAR, "on")
        assert cc.gate_enabled() is True


class TestLoadLiveContract:
    def test_loads_first_resolving_candidate(self, tmp_path: Path) -> None:
        _write_contract(tmp_path)
        contract = cc.load_live_contract(tmp_path, ["missing-id", PIPELINE_ID])
        assert contract is not None
        assert contract.slices[1].id == "slice-2"

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        assert cc.load_live_contract(tmp_path, [PIPELINE_ID, 42]) is None

    def test_returns_none_on_invalid_json(self, tmp_path: Path) -> None:
        path = _write_contract(tmp_path)
        path.write_text("{not json")
        assert cc.load_live_contract(tmp_path, [PIPELINE_ID]) is None

    def test_skips_empty_identifiers(self, tmp_path: Path) -> None:
        _write_contract(tmp_path)
        contract = cc.load_live_contract(tmp_path, ["", None, PIPELINE_ID])
        assert contract is not None


@pytest.fixture
def app():
    from flask import Flask

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    yield flask_app


@pytest.fixture
def signals_module():
    from routes import signals

    return signals


@pytest.fixture
def gate_env(monkeypatch: pytest.MonkeyPatch, signals_module, tmp_path: Path):
    """Patch state-store + worktree resolution onto a tmp worktree.

    Returns the worktree path; tests write a contract into it (or not)
    per scenario.
    """
    pipeline_state = SimpleNamespace(
        current_phase=SimpleNamespace(value="implement"),
        issue_number=42,
    )
    store = MagicMock()
    store.load_pipeline.return_value = pipeline_state
    monkeypatch.setattr(signals_module, "get_state_store", lambda _repo: store)
    monkeypatch.setattr(signals_module, "resolve_worktree_path", lambda _pid, _repo: tmp_path)
    monkeypatch.delenv(cc.GATE_ENV_VAR, raising=False)
    return tmp_path


def _reject(
    signals_module,
    app,
    *,
    check: str,
    enforcer_role: str | None = None,
    producer_role: str | None = None,
    payload: dict[str, Any] | None = None,
    slice_id: str | None = "slice-2",
    current_phase: str | None = None,
):
    with app.app_context():
        result = signals_module._contract_completeness_rejection(
            pipeline_id=PIPELINE_ID,
            repo_path=Path("/unused"),
            slice_id=slice_id,
            check=check,
            enforcer_role=enforcer_role,
            producer_role=producer_role,
            payload=payload,
            current_phase=current_phase,
        )
        if result is None:
            return None
        response, status = result
        return status, response.get_json()


FULL_ATTESTATION = {"tasks_verified": ["task-2-1", "task-2-2"]}


class TestAckGate:
    def test_non_enforcer_skipped(self, signals_module, app, gate_env) -> None:
        _write_contract(gate_env)
        assert (
            _reject(
                signals_module,
                app,
                check="ack",
                enforcer_role="reviewer_code",
                producer_role="coder",
            )
            is None
        )

    def test_kill_switch_skips(
        self, signals_module, app, gate_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(cc.GATE_ENV_VAR, "off")
        _write_contract(gate_env)
        assert (
            _reject(
                signals_module,
                app,
                check="ack",
                enforcer_role="reviewer_contract",
                producer_role="coder",
            )
            is None
        )

    def test_incomplete_rows_rejected_409(self, signals_module, app, gate_env) -> None:
        _write_contract(gate_env)
        status, body = _reject(
            signals_module,
            app,
            check="ack",
            enforcer_role="reviewer_contract",
            producer_role="coder",
        )
        assert status == 409
        assert body["details"]["status"] == "contract_incomplete"
        assert body["details"]["producer"] == "coder"
        assert [r["id"] for r in body["details"]["incomplete_tasks"]] == ["task-2-2"]
        assert "task-2-2" in body["message"]

    def test_complete_rows_without_attestation_rejected(
        self, signals_module, app, gate_env
    ) -> None:
        _write_contract(gate_env, slice2_complete=True)
        status, body = _reject(
            signals_module,
            app,
            check="ack",
            enforcer_role="reviewer_contract",
            producer_role="coder",
        )
        assert status == 409
        assert body["details"]["status"] == "attestation_required"
        assert body["details"]["expected_tasks"] == ["task-2-1", "task-2-2"]

    def test_attestation_missing_and_unknown_ids_rejected(
        self, signals_module, app, gate_env
    ) -> None:
        _write_contract(gate_env, slice2_complete=True)
        status, body = _reject(
            signals_module,
            app,
            check="ack",
            enforcer_role="reviewer_contract",
            producer_role="coder",
            payload={"attestation": {"tasks_verified": ["task-2-1", "task-9-9"]}},
        )
        assert status == 409
        assert body["details"]["status"] == "attestation_mismatch"
        assert body["details"]["missing_tasks"] == ["task-2-2"]
        assert body["details"]["unknown_tasks"] == ["task-9-9"]

    def test_covering_attestation_passes(self, signals_module, app, gate_env) -> None:
        _write_contract(gate_env, slice2_complete=True)
        assert (
            _reject(
                signals_module,
                app,
                check="ack",
                enforcer_role="reviewer_contract",
                producer_role="coder",
                payload={"attestation": FULL_ATTESTATION},
            )
            is None
        )

    def test_attestation_may_cover_extra_slice_rows(self, signals_module, app, gate_env) -> None:
        """Verifying peers' rows in the same slice is allowed (superset)."""
        _write_contract(gate_env, slice2_complete=True)
        assert (
            _reject(
                signals_module,
                app,
                check="ack",
                enforcer_role="reviewer_contract",
                producer_role="coder",
                payload={
                    "attestation": {
                        "tasks_verified": [
                            "task-2-1",
                            "task-2-2",
                            "task-2-3",
                        ]
                    }
                },
            )
            is None
        )

    def test_producer_with_no_owned_rows_needs_no_attestation(
        self, signals_module, app, gate_env
    ) -> None:
        _write_contract(gate_env)
        assert (
            _reject(
                signals_module,
                app,
                check="ack",
                enforcer_role="reviewer_contract",
                producer_role="tester",
            )
            is None
        )

    def test_plan_phase_skipped(
        self, signals_module, app, gate_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_contract(gate_env)
        store = MagicMock()
        store.load_pipeline.return_value = SimpleNamespace(
            current_phase=SimpleNamespace(value="plan"),
            issue_number=42,
        )
        monkeypatch.setattr(signals_module, "get_state_store", lambda _repo: store)
        assert (
            _reject(
                signals_module,
                app,
                check="ack",
                enforcer_role="reviewer_contract",
                producer_role="coder",
            )
            is None
        )


class TestConfirmGate:
    def test_any_incomplete_row_rejects_confirm(self, signals_module, app, gate_env) -> None:
        _write_contract(gate_env)
        status, body = _reject(
            signals_module,
            app,
            check="confirm",
            enforcer_role="reviewer_contract",
        )
        assert status == 409
        assert body["details"]["status"] == "contract_incomplete"
        assert {r["id"] for r in body["details"]["incomplete_tasks"]} == {
            "task-2-2",
            "task-2-3",
            "task-2-4",
        }

    def test_complete_slice_confirm_passes(self, signals_module, app, gate_env) -> None:
        _write_contract(gate_env, slice2_complete=True)
        assert (
            _reject(
                signals_module,
                app,
                check="confirm",
                enforcer_role="reviewer_contract",
            )
            is None
        )

    def test_non_enforcer_confirm_skipped(self, signals_module, app, gate_env) -> None:
        _write_contract(gate_env)
        assert (
            _reject(
                signals_module,
                app,
                check="confirm",
                enforcer_role="coder",
            )
            is None
        )


class TestNoopProposeGate:
    def test_producer_with_open_rows_rejected_400(self, signals_module, app, gate_env) -> None:
        _write_contract(gate_env)
        status, body = _reject(
            signals_module,
            app,
            check="noop_propose",
            producer_role="documenter",
            current_phase="implement",
        )
        assert status == 400
        assert "task-2-3" in body["message"]

    def test_producer_without_rows_passes(self, signals_module, app, gate_env) -> None:
        _write_contract(gate_env)
        assert (
            _reject(
                signals_module,
                app,
                check="noop_propose",
                producer_role="tester",
                current_phase="implement",
            )
            is None
        )


class TestGracefulDegradation:
    def test_missing_contract_skips(self, signals_module, app, gate_env) -> None:
        # No contract written into the worktree.
        assert (
            _reject(
                signals_module,
                app,
                check="ack",
                enforcer_role="reviewer_contract",
                producer_role="coder",
            )
            is None
        )

    def test_unknown_slice_skips(self, signals_module, app, gate_env) -> None:
        _write_contract(gate_env)
        assert (
            _reject(
                signals_module,
                app,
                check="ack",
                enforcer_role="reviewer_contract",
                producer_role="coder",
                slice_id="slice-99",
            )
            is None
        )

    def test_state_store_failure_skips(
        self, signals_module, app, gate_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_contract(gate_env)

        def _boom(_repo):
            raise RuntimeError("state store down")

        monkeypatch.setattr(signals_module, "get_state_store", _boom)
        assert (
            _reject(
                signals_module,
                app,
                check="ack",
                enforcer_role="reviewer_contract",
                producer_role="coder",
            )
            is None
        )

    def test_worktree_failure_skips(
        self, signals_module, app, gate_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_contract(gate_env)

        def _boom(_pid, _repo):
            raise RuntimeError("no worktree")

        monkeypatch.setattr(signals_module, "resolve_worktree_path", _boom)
        assert (
            _reject(
                signals_module,
                app,
                check="ack",
                enforcer_role="reviewer_contract",
                producer_role="coder",
            )
            is None
        )


class TestEnforcerReviewEdges:
    """#3114: the enforcer holds a CRITICAL edge to EVERY producer."""

    def test_reviewer_contract_reviews_every_producer(self) -> None:
        from review_graph import ReviewCriticality, get_default_implement_graph

        graph = get_default_implement_graph()
        for producer in ("coder", "tester", "documenter"):
            edge = graph.get_edge("reviewer_contract", producer)
            assert edge is not None, f"missing edge to {producer}"
            assert edge.criticality == ReviewCriticality.CRITICAL

    def test_documenter_has_critical_reviewer(self) -> None:
        from review_graph import get_default_implement_graph

        graph = get_default_implement_graph()
        assert "reviewer_contract" in graph.critical_reviewers_for("documenter")
