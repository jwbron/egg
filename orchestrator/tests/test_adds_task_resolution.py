"""Tests for the executable ``adds_task`` decision option (#3428).

A ``register_open_question`` option that mandates a contract mutation
("Add a new task/slice to wire X as a dependency") used to be silently
inert: agents have no task-add verb, so resolving the decision recorded
the choice and materialized nothing — the reviewer that raised the
question kept withholding ACK and the slice re-deadlocked *after* the
human answered. Options now carry a structured ``adds_task`` payload
(attached at registration time) that the orchestrator executes on
resolve.

Covers:

* ``operator_actions.add_task_as_operator`` — the audited ``Role.HUMAN``
  executor: id allocation, persistence, audit trail, and failure modes.
* ``routes.decisions._resolution_selects_option`` — the unambiguous
  option-selection matcher the dispatch keys on.
* ``routes.decisions._maybe_add_task_from_resolution`` — the resolution
  dispatch hook, on both the contract-``Decision`` path and the bridged
  queue path (contract recovery via the bridge context fingerprint).
* The resolve endpoint end-to-end: resolving a contract ``cq-N`` whose
  selected option carries ``adds_task`` materializes the task on disk
  and surfaces ``executed_action`` in the response.
* The lifecycle-guarded ``POST /api/v1/contracts/<id>/tasks`` operator
  route — the direct path that replaces hand-editing the contract JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Mirror the docker/k8s mocking the other orchestrator tests rely on so
# lazy imports inside the handlers do not require a real docker SDK.
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

PIPELINE_ID = "pid-3428"


def _contract_dict(*, slice_id: str = "slice-1", task_ids: list[str] | None = None) -> dict:
    tasks = [
        {
            "id": tid,
            "description": f"existing work {tid}",
            "role": "coder",
            "status": "pending",
        }
        for tid in (task_ids if task_ids is not None else ["task-1-1"])
    ]
    return {
        "schemaVersion": "1.0",
        "pipeline_id": PIPELINE_ID,
        "issue": {"number": 42, "title": "adds_task test", "url": "http://example"},
        "phases": [{"id": slice_id, "name": "only", "tasks": tasks}],
    }


@pytest.fixture
def contract_worktree(tmp_path: Path) -> Path:
    contracts_dir = tmp_path / ".egg-state" / "contracts"
    contracts_dir.mkdir(parents=True)
    (contracts_dir / f"{PIPELINE_ID}.json").write_text(json.dumps(_contract_dict()))
    return tmp_path


# ---------------------------------------------------------------------------
# operator_actions.add_task_as_operator
# ---------------------------------------------------------------------------


class TestAddTaskAsOperator:
    def test_appends_validated_pending_task(self, contract_worktree: Path):
        from egg_contracts import load_contract
        from operator_actions import add_task_as_operator

        with patch("contract_store.resolve_pipeline_worktree", return_value=contract_worktree):
            result = add_task_as_operator(
                PIPELINE_ID,
                "slice-1",
                "Wire secondary-repo worktree creation as a dependency",
                acceptance_criteria="Worktree exists before slice-4 starts",
                files_affected=["orchestrator/worktrees.py"],
                role="coder",
                reason="cq-4 opt-1 materialization",
                actor="operator:test",
            )

        assert result["task_id"] == "task-1-2"
        assert result["slice_id"] == "slice-1"
        assert result["status"] == "pending"

        contract = load_contract(PIPELINE_ID, contract_worktree)
        task = contract.slices[0].tasks[-1]
        assert task.id == "task-1-2"
        assert task.description.startswith("Wire secondary-repo")
        assert task.acceptance_criteria == "Worktree exists before slice-4 starts"
        assert task.files_affected == ["orchestrator/worktrees.py"]
        assert task.role == "coder"
        assert str(task.status) == "pending"
        # Audited as the operator, not an agent role.
        audit_actors = {e.actor for e in contract.audit_log}
        assert "operator:test" in audit_actors

    def test_id_allocation_continues_past_highest_existing(self, tmp_path: Path):
        from egg_contracts import load_contract
        from operator_actions import add_task_as_operator

        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        (contracts_dir / f"{PIPELINE_ID}.json").write_text(
            json.dumps(_contract_dict(task_ids=["task-1-1", "task-1-5"]))
        )

        with patch("contract_store.resolve_pipeline_worktree", return_value=tmp_path):
            result = add_task_as_operator(PIPELINE_ID, "slice-1", "new work")

        assert result["task_id"] == "task-1-6"
        contract = load_contract(PIPELINE_ID, tmp_path)
        assert [t.id for t in contract.slices[0].tasks] == ["task-1-1", "task-1-5", "task-1-6"]

    def test_slice_id_matches_legacy_phase_prefix(self, tmp_path: Path):
        from operator_actions import add_task_as_operator

        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        (contracts_dir / f"{PIPELINE_ID}.json").write_text(
            json.dumps(_contract_dict(slice_id="phase-1"))
        )

        with patch("contract_store.resolve_pipeline_worktree", return_value=tmp_path):
            result = add_task_as_operator(PIPELINE_ID, "slice-1", "new work")

        assert result["task_id"] == "task-1-2"

    def test_unknown_slice_raises_404(self, contract_worktree: Path):
        from operator_actions import OperatorActionError, add_task_as_operator

        with (
            patch("contract_store.resolve_pipeline_worktree", return_value=contract_worktree),
            pytest.raises(OperatorActionError) as exc_info,
        ):
            add_task_as_operator(PIPELINE_ID, "slice-9", "new work")
        assert exc_info.value.status_code == 404

    def test_missing_worktree_raises_404(self):
        from operator_actions import OperatorActionError, add_task_as_operator

        with (
            patch("contract_store.resolve_pipeline_worktree", return_value=None),
            pytest.raises(OperatorActionError) as exc_info,
        ):
            add_task_as_operator("pid-gone", "slice-1", "new work")
        assert exc_info.value.status_code == 404

    def test_empty_description_raises_400(self, contract_worktree: Path):
        from operator_actions import OperatorActionError, add_task_as_operator

        with (
            patch("contract_store.resolve_pipeline_worktree", return_value=contract_worktree),
            pytest.raises(OperatorActionError) as exc_info,
        ):
            add_task_as_operator(PIPELINE_ID, "slice-1", "   ")
        assert exc_info.value.status_code == 400

    def test_malformed_slice_id_raises_400(self, contract_worktree: Path):
        from operator_actions import OperatorActionError, add_task_as_operator

        with (
            patch("contract_store.resolve_pipeline_worktree", return_value=contract_worktree),
            pytest.raises(OperatorActionError) as exc_info,
        ):
            add_task_as_operator(PIPELINE_ID, "slice-one", "new work")
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# routes.decisions._resolution_selects_option
# ---------------------------------------------------------------------------


class TestResolutionSelectsOption:
    def _option(self, opt_id: str = "opt-2", label: str = "Add a new task to slice-4"):
        return SimpleNamespace(id=opt_id, label=label)

    @pytest.mark.parametrize(
        "resolution",
        [
            "Add a new task to slice-4",
            "  add a NEW task to slice-4  ",
            "opt-2",
            "OPT-2",
            "option 2",
            "Option 2",
            "2",
        ],
    )
    def test_unambiguous_selections_match(self, resolution: str):
        from routes.decisions import _resolution_selects_option

        assert _resolution_selects_option(self._option(), resolution) is True

    @pytest.mark.parametrize(
        "resolution",
        [
            "",
            "opt-1",
            "1",
            "option 12",
            "Yes, please add a new task to slice-4 when convenient",
            "Add a new task",
        ],
    )
    def test_non_selections_do_not_match(self, resolution: str):
        from routes.decisions import _resolution_selects_option

        assert _resolution_selects_option(self._option(), resolution) is False


# ---------------------------------------------------------------------------
# routes.decisions._maybe_add_task_from_resolution
# ---------------------------------------------------------------------------


def _contract_decision(*, with_payload: bool = True):
    from egg_contracts.models import (
        AddsTaskPayload,
        Decision,
        DecisionOption,
        DecisionType,
        PipelinePhase,
    )

    payload = (
        AddsTaskPayload(
            slice_id="slice-1",
            description="Wire the dependency",
            acceptance_criteria="It works",
            files_affected=["a.py"],
            role="coder",
        )
        if with_payload
        else None
    )
    return Decision(
        id="cq-4",
        question="Slice-4 needs the secondary-repo worktree wired. How should we proceed?",
        type=DecisionType.HITL,
        phase=PipelinePhase.IMPLEMENT,
        options=[
            DecisionOption(
                id="opt-1",
                label="Add a new task to wire the dependency",
                adds_task=payload,
            ),
            DecisionOption(id="opt-2", label="Defer to a follow-up pipeline"),
        ],
    )


class TestMaybeAddTaskFromResolution:
    def _dispatch(self, decision, resolution, **patch_kwargs):
        from routes.decisions import _maybe_add_task_from_resolution

        with patch("operator_actions.add_task_as_operator", **patch_kwargs) as add_mock:
            result = _maybe_add_task_from_resolution(PIPELINE_ID, decision, resolution)
        return result, add_mock

    def test_selected_payload_option_executes(self):
        result, add_mock = self._dispatch(
            _contract_decision(),
            "Add a new task to wire the dependency",
            return_value={"task_id": "task-1-2", "slice_id": "slice-1", "status": "pending"},
        )
        assert result["success"] is True
        assert result["action"] == "add_task"
        assert result["task_id"] == "task-1-2"
        add_mock.assert_called_once()
        args, kwargs = add_mock.call_args
        assert args == (PIPELINE_ID, "slice-1", "Wire the dependency")
        assert kwargs["role"] == "coder"
        assert kwargs["actor"] == "operator:decision:cq-4"

    def test_option_id_selection_executes(self):
        result, add_mock = self._dispatch(
            _contract_decision(),
            "opt-1",
            return_value={"task_id": "task-1-2", "slice_id": "slice-1"},
        )
        assert result["success"] is True
        add_mock.assert_called_once()

    def test_selecting_payload_free_option_is_inert(self):
        result, add_mock = self._dispatch(_contract_decision(), "Defer to a follow-up pipeline")
        assert result is None
        add_mock.assert_not_called()

    def test_decision_without_payload_is_inert(self):
        result, add_mock = self._dispatch(
            _contract_decision(with_payload=False),
            "Add a new task to wire the dependency",
        )
        assert result is None
        add_mock.assert_not_called()

    def test_free_form_prose_does_not_execute(self):
        result, add_mock = self._dispatch(
            _contract_decision(),
            "I think we should add a new task to wire the dependency eventually",
        )
        assert result is None
        add_mock.assert_not_called()

    def test_execution_failure_is_surfaced_not_silent(self):
        from operator_actions import OperatorActionError

        result, _ = self._dispatch(
            _contract_decision(),
            "opt-1",
            side_effect=OperatorActionError("slice gone", status_code=404),
        )
        assert result["success"] is False
        assert "slice gone" in result["error"]
        assert result["slice_id"] == "slice-1"

    def test_bridged_queue_decision_recovers_contract_payload(self):
        # The bridged HITLDecision carries bare option labels; the hook
        # must recover the contract decision via the bridge's context
        # fingerprint and execute against its structured payload.
        queue_decision = SimpleNamespace(
            id="decision-7",
            context="Open contract question cq-4, registered by an agent during the implement phase.",
            options=[
                "Add a new task to wire the dependency",
                "Defer to a follow-up pipeline",
            ],
        )
        with patch(
            "routes.decisions._handlers._load_contract_decision",
            return_value=_contract_decision(),
        ) as load_mock:
            result, add_mock = self._dispatch(
                queue_decision,
                "Add a new task to wire the dependency",
                return_value={"task_id": "task-1-2", "slice_id": "slice-1"},
            )
        load_mock.assert_called_once_with(PIPELINE_ID, "cq-4")
        assert result["success"] is True
        add_mock.assert_called_once()

    def test_queue_decision_without_bridge_fingerprint_is_inert(self):
        queue_decision = SimpleNamespace(
            id="decision-8",
            context="Agent coder issue: something unrelated",
            options=["Restart agent", "Ignore"],
        )
        with patch("routes.decisions._handlers._load_contract_decision") as load_mock:
            result, add_mock = self._dispatch(queue_decision, "Restart agent")
        load_mock.assert_not_called()
        assert result is None
        add_mock.assert_not_called()


# ---------------------------------------------------------------------------
# End-to-end: resolve a contract cq-N whose option carries adds_task
# ---------------------------------------------------------------------------


class TestResolveContractDecisionMaterializesTask:
    @pytest.fixture
    def client(self):
        from flask import Flask
        from routes.decisions import decisions_bp

        app = Flask(__name__)
        app.register_blueprint(decisions_bp)
        app.config["TESTING"] = True
        return app.test_client()

    def _write_contract(self, worktree: Path):
        from egg_contracts import Contract, save_contract
        from egg_contracts.models import PipelinePhase, Slice, Task

        contract = Contract(pipeline_id=PIPELINE_ID, current_phase=PipelinePhase.IMPLEMENT)
        contract.slices = [
            Slice(
                id="slice-1",
                name="only",
                tasks=[Task(id="task-1-1", description="existing work", role="coder")],
            )
        ]
        contract.decisions = [_contract_decision()]
        save_contract(contract, worktree)

    def test_resolution_materializes_task_and_reports_executed_action(self, client, tmp_path: Path):
        from decision_queue import DecisionNotFoundError
        from egg_contracts import load_contract

        self._write_contract(tmp_path)

        store_mock = MagicMock(repo_path=tmp_path)
        pipeline_mock = MagicMock(issue_number=None)
        queue_mock = MagicMock()
        queue_mock.resolve_decision.side_effect = DecisionNotFoundError("not in queue")
        queue_mock.get_pending_decisions.return_value = []

        with (
            patch(
                "routes.decisions.get_state_store_for_pipeline",
                return_value=(store_mock, pipeline_mock),
            ),
            patch("routes.decisions.get_decision_queue", return_value=queue_mock),
            patch("contract_store.resolve_pipeline_worktree", return_value=tmp_path),
        ):
            resp = client.post(
                f"/api/v1/pipelines/{PIPELINE_ID}/decisions/cq-4/resolve",
                json={"resolution": "Add a new task to wire the dependency"},
            )

        assert resp.status_code == 200, resp.data
        payload = resp.get_json()
        executed = payload["data"]["executed_action"]
        assert executed["action"] == "add_task"
        assert executed["success"] is True
        assert executed["task_id"] == "task-1-2"

        contract = load_contract(PIPELINE_ID, tmp_path)
        # The mandated task is materialized — the precondition the blocked
        # reviewer stated is now satisfiable on the next contract poll.
        assert [t.id for t in contract.slices[0].tasks] == ["task-1-1", "task-1-2"]
        assert contract.slices[0].tasks[1].description == "Wire the dependency"
        decision = contract.decisions[0]
        assert decision.resolved is True
        assert decision.resolved_by == "human"


# ---------------------------------------------------------------------------
# Operator REST route: POST /api/v1/contracts/<id>/tasks
# ---------------------------------------------------------------------------


class TestOperatorAddTaskRoute:
    @pytest.fixture
    def route_client(self, monkeypatch):
        from flask import Flask
        from routes.contracts import contracts_bp

        monkeypatch.setenv("EGG_LIFECYCLE_SECRET", "test-secret")
        app = Flask(__name__)
        app.register_blueprint(contracts_bp)
        app.config["TESTING"] = True
        return app.test_client()

    def test_route_requires_lifecycle_secret(self, route_client):
        resp = route_client.post(f"/api/v1/contracts/{PIPELINE_ID}/tasks")
        assert resp.status_code == 401

    def test_route_appends_task(self, route_client, contract_worktree: Path):
        with patch("contract_store.resolve_pipeline_worktree", return_value=contract_worktree):
            resp = route_client.post(
                f"/api/v1/contracts/{PIPELINE_ID}/tasks",
                headers={"Authorization": "Bearer test-secret"},
                data=json.dumps(
                    {
                        "slice_id": "slice-1",
                        "description": "Wire the dependency",
                        "reason": "manual remediation",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200, resp.data
        data = json.loads(resp.data)
        assert data["data"]["task_id"] == "task-1-2"
        assert data["data"]["status"] == "pending"

    def test_route_rejects_missing_fields(self, route_client):
        resp = route_client.post(
            f"/api/v1/contracts/{PIPELINE_ID}/tasks",
            headers={"Authorization": "Bearer test-secret"},
            data=json.dumps({"description": "no slice"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_route_scopes_body_actor_under_operator_namespace(self, route_client):
        with patch("operator_actions.add_task_as_operator") as add_mock:
            add_mock.return_value = {"task_id": "task-1-2", "slice_id": "slice-1"}
            route_client.post(
                f"/api/v1/contracts/{PIPELINE_ID}/tasks",
                headers={"Authorization": "Bearer test-secret"},
                data=json.dumps({"slice_id": "slice-1", "description": "x", "actor": "coder"}),
                content_type="application/json",
            )
        assert add_mock.call_args[1]["actor"] == "operator:coder"
