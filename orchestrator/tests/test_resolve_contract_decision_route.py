"""Tests for the contract-decision fallback on POST .../decisions/<id>/resolve (#3071).

Agents register HITL questions on the SDLC contract (``cq-N``, via
``register_open_question`` or the impasse-escalation router).  Those
decisions are bridged into the orchestrator queue only *after* phase_gate
approval, so an agent blocked pre-propose deadlocked with no operator
channel: ``provide_input`` 404'd (not in the queue) and ``answer_feedback``
covers only ``contract.feedback`` (#3007).  The resolve endpoint now falls
back to contract-resident decisions when the queue misses, writing the
resolution fields straight onto the contract.

These tests exercise the real contract load/save against a temp worktree
so the on-disk write-back is covered end to end — mirroring
``test_answer_feedback_route.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mirror the docker/k8s mocking the other orchestrator tests rely on so the
# lazy ``from routes.pipelines import _pipeline_identifier`` import inside the
# handler does not require a real docker SDK.
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


PIPELINE_ID = "test-pipeline"


@pytest.fixture
def client():
    from flask import Flask
    from routes.decisions import decisions_bp

    app = Flask(__name__)
    app.register_blueprint(decisions_bp)
    app.config["TESTING"] = True
    return app.test_client()


def _write_contract(worktree: Path, *, with_decision=True, resolved=False):
    """Create + persist a contract with (optionally) a pending cq decision."""
    from egg_contracts import Contract, save_contract
    from egg_contracts.models import Decision, DecisionOption, DecisionType, PipelinePhase

    contract = Contract(pipeline_id=PIPELINE_ID, current_phase=PipelinePhase.PLAN)
    if with_decision:
        contract.decisions = [
            Decision(
                id="cq-1",
                question=(
                    "Producer task_planner reported an impasse: the analysis "
                    "draft referenced by the task description is missing."
                ),
                type=DecisionType.HITL,
                phase=PipelinePhase.PLAN,
                options=[
                    DecisionOption(id="opt-1", label="Cancel the slice and re-plan"),
                    DecisionOption(
                        id="opt-2", label="Resolve the underlying blocker manually, then resume"
                    ),
                ],
                resolved=resolved,
            )
        ]
    save_contract(contract, worktree)
    return contract


def _patch_resolution(tmp_path, *, issue_number=None, pending_queue_decisions=None):
    """Patch store + worktree resolution for the route to the temp worktree.

    ``get_decision_queue`` is patched to a queue that always raises
    DecisionNotFoundError — the pre-#3071 404 path — so requests exercise
    the contract fallback.  The post-gate guard (#3071 review) scans
    ``queue.get_pending_decisions()``; default it to an empty list so the
    fallback proceeds, and inject mirrors via ``pending_queue_decisions``
    when exercising the stranding guard.
    """
    from decision_queue import DecisionNotFoundError

    store_mock = MagicMock(repo_path=tmp_path)
    pipeline_mock = MagicMock(issue_number=issue_number)
    queue_mock = MagicMock()
    queue_mock.resolve_decision.side_effect = DecisionNotFoundError("not in queue")
    queue_mock.get_pending_decisions.return_value = pending_queue_decisions or []
    return (
        patch(
            "routes.decisions.get_state_store_for_pipeline",
            return_value=(store_mock, pipeline_mock),
        ),
        patch("routes.decisions.get_decision_queue", return_value=queue_mock),
        patch("contract_store.resolve_pipeline_worktree", return_value=tmp_path),
    )


def _post(client, decision_id, body):
    return client.post(
        f"/api/v1/pipelines/{PIPELINE_ID}/decisions/{decision_id}/resolve",
        json=body,
    )


def test_resolves_contract_decision_and_writes_contract(client, tmp_path):
    from egg_contracts import load_contract

    _write_contract(tmp_path)
    store_patch, queue_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, queue_patch, worktree_patch:
        resp = _post(client, "cq-1", {"resolution": "Resolve the underlying blocker manually"})

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["data"]["decision"]["id"] == "cq-1"
    assert payload["data"]["decision"]["status"] == "resolved"
    assert payload["data"]["decision"]["scope"] == "contract"

    # The write-back must hit disk so the agent's next contract poll sees
    # it.  Field shape matches the post-gate bridge's write-back
    # (resolved_by="human", raw resolution string).
    contract = load_contract(PIPELINE_ID, tmp_path)
    decision = contract.decisions[0]
    assert decision.resolved is True
    assert decision.resolution == "Resolve the underlying blocker manually"
    assert decision.resolved_by == "human"
    assert decision.resolved_at is not None


def test_dict_resolution_serialized_like_queue_path(client, tmp_path):
    """A dict resolution body is serialized to a JSON string (#1635 parity)."""
    import json

    from egg_contracts import load_contract

    _write_contract(tmp_path)
    store_patch, queue_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, queue_patch, worktree_patch:
        resp = _post(client, "cq-1", {"resolution": {"action": "select", "selected": "opt-2"}})

    assert resp.status_code == 200
    contract = load_contract(PIPELINE_ID, tmp_path)
    assert json.loads(contract.decisions[0].resolution) == {
        "action": "select",
        "selected": "opt-2",
    }


def test_unknown_decision_returns_404_mentioning_both_misses(client, tmp_path):
    """An id in neither the queue nor the contract still 404s."""
    _write_contract(tmp_path)
    store_patch, queue_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, queue_patch, worktree_patch:
        resp = _post(client, "cq-99", {"resolution": "x"})

    assert resp.status_code == 404
    msg = resp.get_json()["message"]
    assert "cq-99" in msg
    assert "queue" in msg.lower() and "contract" in msg.lower()


def test_already_resolved_contract_decision_returns_409(client, tmp_path):
    _write_contract(tmp_path, resolved=True)
    store_patch, queue_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, queue_patch, worktree_patch:
        resp = _post(client, "cq-1", {"resolution": "x"})

    assert resp.status_code == 409
    assert "already" in resp.get_json()["message"].lower()


def test_no_contract_returns_404(client, tmp_path):
    """No contract on disk at all — the fallback degrades to a 404."""
    store_patch, queue_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, queue_patch, worktree_patch:
        resp = _post(client, "cq-1", {"resolution": "x"})

    assert resp.status_code == 404


def test_worktree_not_found_returns_404(client, tmp_path):
    from decision_queue import DecisionNotFoundError

    store_mock = MagicMock(repo_path=tmp_path)
    pipeline_mock = MagicMock(issue_number=None)
    queue_mock = MagicMock()
    queue_mock.resolve_decision.side_effect = DecisionNotFoundError("not in queue")
    with (
        patch(
            "routes.decisions.get_state_store_for_pipeline",
            return_value=(store_mock, pipeline_mock),
        ),
        patch("routes.decisions.get_decision_queue", return_value=queue_mock),
        patch("contract_store.resolve_pipeline_worktree", return_value=None),
    ):
        resp = _post(client, "cq-1", {"resolution": "x"})

    assert resp.status_code == 404
    assert "worktree" in resp.get_json()["message"].lower()


def test_queue_decision_still_takes_precedence(client, tmp_path):
    """A decision that IS in the queue resolves there — the contract
    fallback never runs and the contract is untouched."""
    from egg_contracts import load_contract

    _write_contract(tmp_path)

    store_mock = MagicMock(repo_path=tmp_path)
    pipeline_mock = MagicMock(issue_number=None)
    queue_mock = MagicMock()
    resolved_decision = MagicMock()
    resolved_decision.id = "cq-1"
    resolved_decision.status.value = "resolved"
    resolved_decision.resolution = "approve"
    resolved_decision.resolved_at = None
    resolved_decision.context = ""
    resolved_decision.question = "q"
    queue_mock.resolve_decision.return_value = resolved_decision

    with (
        patch(
            "routes.decisions.get_state_store_for_pipeline",
            return_value=(store_mock, pipeline_mock),
        ),
        patch("routes.decisions.get_decision_queue", return_value=queue_mock),
        patch("contract_store.resolve_pipeline_worktree", return_value=tmp_path),
    ):
        resp = _post(client, "cq-1", {"resolution": "approve"})

    assert resp.status_code == 200
    payload = resp.get_json()
    # Queue path now tags ``scope: "queue"`` for parity with the contract
    # fallback's ``scope: "contract"`` (#3071 review item 5).
    assert payload["data"]["decision"]["scope"] == "queue"
    contract = load_contract(PIPELINE_ID, tmp_path)
    assert contract.decisions[0].resolved is False


def test_emits_decision_resolved_event(client, tmp_path):
    _write_contract(tmp_path)
    store_patch, queue_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, queue_patch, worktree_patch:
        with patch("routes.decisions.emit_event") as mock_emit:
            resp = _post(client, "cq-1", {"resolution": "opt-1"})

    assert resp.status_code == 200
    assert mock_emit.called
    call_kwargs = mock_emit.call_args[1]
    assert call_kwargs["pipeline_id"] == PIPELINE_ID
    assert call_kwargs["data"]["decision_id"] == "cq-1"
    assert call_kwargs["data"]["scope"] == "contract"


def test_resolution_stripped_for_bridge_parity(client, tmp_path):
    """Whitespace-padded resolutions are stripped to match the post-gate
    bridge's ``(resolved.resolution or "").strip()`` write-back (#3071
    review item 2).  Without this, free-form ``Other (explain in reply)``
    answers would land on disk with different shapes depending on which
    write path (pre-gate fallback vs. post-gate bridge) handled them."""
    from egg_contracts import load_contract

    _write_contract(tmp_path)
    store_patch, queue_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, queue_patch, worktree_patch:
        resp = _post(client, "cq-1", {"resolution": "   trimmed answer  \n"})

    assert resp.status_code == 200
    contract = load_contract(PIPELINE_ID, tmp_path)
    assert contract.decisions[0].resolution == "trimmed answer"


def test_post_gate_mirror_returns_409_with_pointer(client, tmp_path):
    """If the post-gate bridge has already mirrored this ``cq-N`` into the
    queue, the fallback must 409 with the queue id so the operator
    resolves the queue side (which unblocks both the agent's contract poll
    AND the bridge's ``wait_for_decision``). Without this guard the
    bridge thread would be stranded indefinitely (#3071 review item 1)."""
    _write_contract(tmp_path)

    # Build a pending queue decision whose context carries the bridge's
    # fingerprint: ``f"Open contract question {cq_id}, registered by an
    # agent during the {phase} phase."`` (orchestrator/routes/pipelines.py).
    mirror = MagicMock()
    mirror.id = "decision-42"
    mirror.context = "Open contract question cq-1, registered by an agent during the plan phase."

    store_patch, queue_patch, worktree_patch = _patch_resolution(
        tmp_path, pending_queue_decisions=[mirror]
    )
    with store_patch, queue_patch, worktree_patch:
        resp = _post(client, "cq-1", {"resolution": "opt-1"})

    assert resp.status_code == 409
    msg = resp.get_json()["message"]
    assert "decision-42" in msg
    assert "bridged" in msg.lower()


def test_post_gate_mirror_for_other_cq_does_not_block(client, tmp_path):
    """The post-gate guard must be precise: a pending mirror for a
    *different* ``cq-N`` must not 409 this request."""
    from egg_contracts import load_contract

    _write_contract(tmp_path)

    other_mirror = MagicMock()
    other_mirror.id = "decision-7"
    other_mirror.context = (
        "Open contract question cq-2, registered by an agent during the plan phase."
    )

    store_patch, queue_patch, worktree_patch = _patch_resolution(
        tmp_path, pending_queue_decisions=[other_mirror]
    )
    with store_patch, queue_patch, worktree_patch:
        resp = _post(client, "cq-1", {"resolution": "opt-1"})

    assert resp.status_code == 200
    contract = load_contract(PIPELINE_ID, tmp_path)
    assert contract.decisions[0].resolved is True


def test_auto_decision_rejected_with_400(client, tmp_path):
    """Only HITL decisions are operator-resolvable; AUTO decisions are
    auto-resolved by the orchestrator and must not be overwritable here
    (#3071 review item 3, defense-in-depth)."""
    from egg_contracts import Contract, save_contract
    from egg_contracts.models import Decision, DecisionType, PipelinePhase

    contract = Contract(pipeline_id=PIPELINE_ID, current_phase=PipelinePhase.PLAN)
    contract.decisions = [
        Decision(
            id="cq-1",
            question="auto-resolved question",
            type=DecisionType.AUTO,
            phase=PipelinePhase.PLAN,
        )
    ]
    save_contract(contract, tmp_path)

    store_patch, queue_patch, worktree_patch = _patch_resolution(tmp_path)
    with store_patch, queue_patch, worktree_patch:
        resp = _post(client, "cq-1", {"resolution": "x"})

    assert resp.status_code == 400
    assert "HITL" in resp.get_json()["message"]


def test_contract_validation_error_returns_500(client, tmp_path):
    """A malformed contract on disk surfaces as a 500 with a clear
    message (#3071 review item 4) — mirrors how
    ``test_answer_feedback_route.py`` covers the same branch."""
    from egg_contracts import ContractValidationError

    store_patch, queue_patch, worktree_patch = _patch_resolution(tmp_path)
    with (
        store_patch,
        queue_patch,
        worktree_patch,
        patch(
            "egg_contracts.load_contract",
            side_effect=ContractValidationError(PIPELINE_ID, ["bad schema"]),
        ),
    ):
        resp = _post(client, "cq-1", {"resolution": "x"})

    assert resp.status_code == 500
    assert "validation" in resp.get_json()["message"].lower()


def test_requires_lifecycle_secret(client, tmp_path):
    """Agents (no bearer token) must not be able to resolve their own cq."""
    _write_contract(tmp_path)
    store_patch, queue_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, queue_patch, worktree_patch:
        resp = client.post(
            f"/api/v1/pipelines/{PIPELINE_ID}/decisions/cq-1/resolve",
            json={"resolution": "x"},
            _lifecycle_auth=False,
        )

    assert resp.status_code == 401


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
