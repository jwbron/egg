"""Tests for the post-delegation impasse cleanup helper (#2553 review).

The slice-loop wrapper relies on
``_clear_stale_impasses_for_producers`` to drop the ``impasse`` field
from each producer's per-pipeline agent-output file before the next
BRC cycle. ``save_agent_output`` writes ``mode="w"`` so any producer
that respawns *and* reaches its handoff write will overwrite the
stale impasse on its own — but if a producer crashes pre-handoff in
iter-N+1 (or never spawns at all under a future contract-task-driven
roster), the iter-N file would otherwise persist into the next
``collect_impasses`` scan and mis-trigger a "second impasse on same
task" HITL escalation.

These tests lock that fragile-by-design behaviour: the cleanup drops
``impasse`` for every producer that has one, leaves the other
top-level fields intact, and is a no-op for producers without an
impasse on file.
"""

from __future__ import annotations

import json
from pathlib import Path

from egg_contracts.agent_roles import AgentRole as ContractAgentRole
from egg_contracts.orchestrator import save_agent_output
from routes.pipelines import _clear_stale_impasses_for_producers

PRODUCER_ROLES = [
    ContractAgentRole.CODER,
    ContractAgentRole.TESTER,
    ContractAgentRole.DOCUMENTER,
]


def _read_output(repo: Path, pipeline_id: str, role: ContractAgentRole) -> dict:
    fp = repo / ".egg-state" / "agent-outputs" / f"{pipeline_id}-{role.value}-output.json"
    return json.loads(fp.read_text())


class TestClearStaleImpasses:
    def test_drops_impasse_field_preserves_others(self, tmp_path):
        """The ``impasse`` key disappears; ``handoff_data`` and other
        top-level fields survive verbatim."""
        pipeline_id = "issue-1234"
        save_agent_output(
            tmp_path,
            ContractAgentRole.CODER,
            {
                "role": "coder",
                "handoff_data": {"files_modified": ["a.py"]},
                "impasse": {
                    "category": "wrong_role",
                    "reason": "stale",
                    "task_id": "task-1",
                },
            },
            identifier=pipeline_id,
        )

        _clear_stale_impasses_for_producers(
            tmp_path,
            pipeline_id,
            PRODUCER_ROLES,
            cleanup_reason="unit test",
        )

        cleaned = _read_output(tmp_path, pipeline_id, ContractAgentRole.CODER)
        assert "impasse" not in cleaned
        assert cleaned["role"] == "coder"
        assert cleaned["handoff_data"] == {"files_modified": ["a.py"]}

    def test_no_op_when_no_impasse_field(self, tmp_path):
        """Files without an ``impasse`` key are left byte-identical
        on disk — the helper short-circuits before re-writing."""
        pipeline_id = "issue-1234"
        original = {
            "role": "tester",
            "handoff_data": {"tests_added": 3},
        }
        save_agent_output(
            tmp_path,
            ContractAgentRole.TESTER,
            original,
            identifier=pipeline_id,
        )
        path = tmp_path / ".egg-state" / "agent-outputs" / f"{pipeline_id}-tester-output.json"
        before_bytes = path.read_bytes()

        _clear_stale_impasses_for_producers(
            tmp_path,
            pipeline_id,
            PRODUCER_ROLES,
            cleanup_reason="unit test",
        )

        assert path.read_bytes() == before_bytes

    def test_no_op_when_output_file_missing(self, tmp_path):
        """A producer that never wrote in iter-N (e.g. crashed pre-
        handoff) must not cause the helper to raise or to materialise
        an empty file."""
        pipeline_id = "issue-1234"
        out_dir = tmp_path / ".egg-state" / "agent-outputs"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Sanity: no producer files exist yet.
        assert list(out_dir.iterdir()) == []

        _clear_stale_impasses_for_producers(
            tmp_path,
            pipeline_id,
            PRODUCER_ROLES,
            cleanup_reason="unit test",
        )

        assert list(out_dir.iterdir()) == []

    def test_clears_across_multiple_producers(self, tmp_path):
        """When more than one producer emitted an impasse in the same
        iteration, every one of them is cleaned in a single pass."""
        pipeline_id = "issue-1234"
        for role in (ContractAgentRole.CODER, ContractAgentRole.DOCUMENTER):
            save_agent_output(
                tmp_path,
                role,
                {
                    "role": role.value,
                    "handoff_data": {"role-marker": role.value},
                    "impasse": {"category": "plan_bug", "reason": f"stale {role.value}"},
                },
                identifier=pipeline_id,
            )
        # Tester wrote a clean output (no impasse) — must stay intact.
        save_agent_output(
            tmp_path,
            ContractAgentRole.TESTER,
            {"role": "tester", "handoff_data": {"tests_added": 1}},
            identifier=pipeline_id,
        )

        _clear_stale_impasses_for_producers(
            tmp_path,
            pipeline_id,
            PRODUCER_ROLES,
            cleanup_reason="unit test",
        )

        for role in (ContractAgentRole.CODER, ContractAgentRole.DOCUMENTER):
            cleaned = _read_output(tmp_path, pipeline_id, role)
            assert "impasse" not in cleaned
            assert cleaned["handoff_data"] == {"role-marker": role.value}
        tester = _read_output(tmp_path, pipeline_id, ContractAgentRole.TESTER)
        assert tester == {"role": "tester", "handoff_data": {"tests_added": 1}}

    def test_does_not_touch_other_pipelines(self, tmp_path):
        """The cleanup is scoped to the named pipeline id — a
        concurrent pipeline's stale impasse on the same role must
        not be cleared by this call."""
        own_pipeline = "issue-1234"
        other_pipeline = "issue-9999"

        save_agent_output(
            tmp_path,
            ContractAgentRole.CODER,
            {"role": "coder", "impasse": {"category": "wrong_role", "reason": "own"}},
            identifier=own_pipeline,
        )
        save_agent_output(
            tmp_path,
            ContractAgentRole.CODER,
            {"role": "coder", "impasse": {"category": "wrong_role", "reason": "other"}},
            identifier=other_pipeline,
        )

        _clear_stale_impasses_for_producers(
            tmp_path,
            own_pipeline,
            PRODUCER_ROLES,
            cleanup_reason="unit test",
        )

        own = _read_output(tmp_path, own_pipeline, ContractAgentRole.CODER)
        assert "impasse" not in own
        other = _read_output(tmp_path, other_pipeline, ContractAgentRole.CODER)
        assert other["impasse"] == {"category": "wrong_role", "reason": "other"}
