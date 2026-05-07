"""Tests for orchestrator-side impasse detection and routing (#2529)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent
_SHARED_DIR = _ORCHESTRATOR_DIR.parent / "shared"
for p in (_SHARED_DIR, _ORCHESTRATOR_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from egg_contracts.agent_roles import AgentRole as ContractAgentRole  # noqa: E402
from egg_contracts.impasse import Impasse, ImpasseCategory  # noqa: E402
from egg_contracts.loader import (  # noqa: E402
    create_contract,
    load_contract,
    save_contract,
)
from egg_contracts.models import Slice, Task  # noqa: E402
from impasse_routing import (  # noqa: E402
    DELEGATION_LIMIT,
    ImpasseAction,
    collect_impasses,
    route_impasses,
)


def _seed_contract(repo_root: Path, slice_id: str = "slice-1") -> str:
    """Create a contract with one slice + one coder task and return its
    pipeline id."""
    pipeline_id = "issue-9999"
    contract = create_contract(
        pipeline_id=pipeline_id,
        title="impasse routing fixture",
        repo_root=repo_root,
    )
    contract.slices = [
        Slice(
            id=slice_id,
            name="seed slice",
            tasks=[
                Task(
                    id="task-1-1",
                    description="edit conftest",
                    role="coder",
                    files_affected=["tests/conftest.py"],
                ),
            ],
        )
    ]
    save_contract(contract, repo_root)
    return pipeline_id


def _write_agent_output(
    repo_root: Path,
    pipeline_id: str,
    role: str,
    impasse: Impasse | None,
    handoff_data: dict | None = None,
) -> Path:
    out_dir = repo_root / ".egg-state" / "agent-outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    fp = out_dir / f"{pipeline_id}-{role}-output.json"
    payload: dict = {"role": role}
    if handoff_data is not None:
        payload["handoff_data"] = handoff_data
    if impasse is not None:
        payload["impasse"] = impasse.to_dict()
    fp.write_text(json.dumps(payload))
    return fp


class TestCollectImpasses:
    def test_returns_empty_when_no_outputs(self, tmp_path):
        pid = _seed_contract(tmp_path)
        result = collect_impasses(
            tmp_path, pid, [ContractAgentRole.CODER, ContractAgentRole.TESTER]
        )
        assert result == []

    def test_skips_outputs_without_impasse(self, tmp_path):
        pid = _seed_contract(tmp_path)
        _write_agent_output(tmp_path, pid, "coder", impasse=None, handoff_data={"foo": "bar"})
        result = collect_impasses(tmp_path, pid, [ContractAgentRole.CODER])
        assert result == []

    def test_picks_up_impasse(self, tmp_path):
        pid = _seed_contract(tmp_path)
        imp = Impasse(
            category=ImpasseCategory.WRONG_ROLE,
            reason="x",
            suggested_role="tester",
        )
        _write_agent_output(tmp_path, pid, "coder", impasse=imp)
        result = collect_impasses(tmp_path, pid, [ContractAgentRole.CODER])
        assert len(result) == 1
        role, found = result[0]
        assert role == ContractAgentRole.CODER
        assert found.suggested_role == "tester"

    def test_malformed_impasse_is_dropped(self, tmp_path):
        pid = _seed_contract(tmp_path)
        out_dir = tmp_path / ".egg-state" / "agent-outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        fp = out_dir / f"{pid}-coder-output.json"
        fp.write_text(json.dumps({"role": "coder", "impasse": {"category": "garbage"}}))
        result = collect_impasses(tmp_path, pid, [ContractAgentRole.CODER])
        assert result == []


class TestRouteImpassesDelegate:
    def test_first_wrong_role_impasse_delegates(self, tmp_path):
        pid = _seed_contract(tmp_path)
        imp = Impasse(
            category=ImpasseCategory.WRONG_ROLE,
            reason="cannot write tests/conftest.py",
            task_id="task-1-1",
            suggested_role="tester",
            blocked_files=["tests/conftest.py"],
        )
        decisions = route_impasses(
            repo_path=tmp_path,
            pipeline_id=pid,
            contract_identifier=pid,
            impasses=[(ContractAgentRole.CODER, imp)],
            slice_id="slice-1",
        )
        assert len(decisions) == 1
        d = decisions[0]
        assert d.action == ImpasseAction.DELEGATE
        assert d.role == "coder"
        assert d.new_role == "tester"
        assert d.task_id == "task-1-1"

        contract = load_contract(pid, tmp_path)
        task = contract.slices[0].tasks[0]
        assert task.role == "tester"
        assert task.delegation_attempts == 1

    def test_wrong_role_without_task_id_resolves_via_role_match(self, tmp_path):
        # When the agent omits task_id, the router falls back to "the
        # single task in this slice owned by this role". Confirm that
        # path mutates the right task.
        pid = _seed_contract(tmp_path)
        imp = Impasse(
            category=ImpasseCategory.WRONG_ROLE,
            reason="cannot write tests/conftest.py",
            suggested_role="tester",
        )
        decisions = route_impasses(
            repo_path=tmp_path,
            pipeline_id=pid,
            contract_identifier=pid,
            impasses=[(ContractAgentRole.CODER, imp)],
            slice_id="slice-1",
        )
        assert decisions[0].action == ImpasseAction.DELEGATE
        contract = load_contract(pid, tmp_path)
        assert contract.slices[0].tasks[0].role == "tester"


class TestRouteImpassesEscalate:
    def test_second_impasse_escalates(self, tmp_path):
        pid = _seed_contract(tmp_path)
        # Pre-bump the counter to simulate "we already delegated once".
        contract = load_contract(pid, tmp_path)
        contract.slices[0].tasks[0].delegation_attempts = DELEGATION_LIMIT
        save_contract(contract, tmp_path)

        imp = Impasse(
            category=ImpasseCategory.WRONG_ROLE,
            reason="still cannot write the file",
            task_id="task-1-1",
            suggested_role="documenter",
        )
        decisions = route_impasses(
            repo_path=tmp_path,
            pipeline_id=pid,
            contract_identifier=pid,
            impasses=[(ContractAgentRole.TESTER, imp)],
            slice_id="slice-1",
        )
        assert decisions[0].action == ImpasseAction.ESCALATE
        assert decisions[0].hitl_decision_id is not None

        contract = load_contract(pid, tmp_path)
        assert any(d.id == decisions[0].hitl_decision_id for d in contract.decisions)

    def test_plan_bug_always_escalates(self, tmp_path):
        pid = _seed_contract(tmp_path)
        imp = Impasse(
            category=ImpasseCategory.PLAN_BUG,
            reason="acceptance criteria contradict each other",
            task_id="task-1-1",
        )
        decisions = route_impasses(
            repo_path=tmp_path,
            pipeline_id=pid,
            contract_identifier=pid,
            impasses=[(ContractAgentRole.CODER, imp)],
            slice_id="slice-1",
        )
        assert decisions[0].action == ImpasseAction.ESCALATE
        contract = load_contract(pid, tmp_path)
        assert contract.slices[0].tasks[0].delegation_attempts == 0
        assert contract.slices[0].tasks[0].role == "coder"  # untouched

    def test_external_blocker_escalates_with_hitl_options(self, tmp_path):
        pid = _seed_contract(tmp_path)
        imp = Impasse(
            category=ImpasseCategory.EXTERNAL_BLOCKER,
            reason="upstream PR not merged",
            task_id="task-1-1",
            evidence={"blocking_pr": 1234},
        )
        decisions = route_impasses(
            repo_path=tmp_path,
            pipeline_id=pid,
            contract_identifier=pid,
            impasses=[(ContractAgentRole.CODER, imp)],
            slice_id="slice-1",
        )
        assert decisions[0].action == ImpasseAction.ESCALATE
        contract = load_contract(pid, tmp_path)
        decision = next(d for d in contract.decisions if d.id == decisions[0].hitl_decision_id)
        assert "external_blocker" in decision.question
        assert "upstream PR not merged" in decision.question

    def test_self_delegation_escalates(self, tmp_path):
        # Even though report_impasse rejects suggested_role==role at the
        # handler boundary, defense-in-depth: if a malformed payload
        # ever reaches the router, fall through to HITL rather than
        # silently looping forever.
        pid = _seed_contract(tmp_path)
        imp = Impasse(
            category=ImpasseCategory.WRONG_ROLE,
            reason="x",
            task_id="task-1-1",
            suggested_role="coder",  # same as impassed role
        )
        decisions = route_impasses(
            repo_path=tmp_path,
            pipeline_id=pid,
            contract_identifier=pid,
            impasses=[(ContractAgentRole.CODER, imp)],
            slice_id="slice-1",
        )
        assert decisions[0].action == ImpasseAction.ESCALATE

    def test_unknown_alternative_role_escalates(self, tmp_path):
        # suggested_role=overseer is non-producer; auto-delegate must
        # reject it.
        pid = _seed_contract(tmp_path)
        imp = Impasse(
            category=ImpasseCategory.WRONG_ROLE,
            reason="x",
            task_id="task-1-1",
            suggested_role="overseer",
        )
        decisions = route_impasses(
            repo_path=tmp_path,
            pipeline_id=pid,
            contract_identifier=pid,
            impasses=[(ContractAgentRole.CODER, imp)],
            slice_id="slice-1",
        )
        assert decisions[0].action == ImpasseAction.ESCALATE
        contract = load_contract(pid, tmp_path)
        assert contract.slices[0].tasks[0].role == "coder"

    def test_unresolved_task_id_escalates(self, tmp_path):
        pid = _seed_contract(tmp_path)
        imp = Impasse(
            category=ImpasseCategory.WRONG_ROLE,
            reason="x",
            task_id="task-9-9",  # nonexistent
            suggested_role="tester",
        )
        decisions = route_impasses(
            repo_path=tmp_path,
            pipeline_id=pid,
            contract_identifier=pid,
            impasses=[(ContractAgentRole.CODER, imp)],
            slice_id="slice-1",
        )
        assert decisions[0].action == ImpasseAction.ESCALATE
        assert "task_id" in decisions[0].reason


class TestEmptyInput:
    def test_no_impasses_returns_empty(self, tmp_path):
        pid = _seed_contract(tmp_path)
        decisions = route_impasses(
            repo_path=tmp_path,
            pipeline_id=pid,
            contract_identifier=pid,
            impasses=[],
            slice_id="slice-1",
        )
        assert decisions == []
        # Contract untouched
        contract = load_contract(pid, tmp_path)
        assert contract.slices[0].tasks[0].role == "coder"
        assert contract.slices[0].tasks[0].delegation_attempts == 0
