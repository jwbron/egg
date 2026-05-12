"""Dedicated empty-contract HITL helper (#2627 follow-up).

The implement-start slice gate, ``start_phase=implement`` safety net,
and natural plan-complete path all emit the same dedicated HITL when
the contract is empty.  Plain ``Retry phase`` against the generic
post-failure decision respawns into the same empty-contract state
(documented loop on issue #2627), so the recovery options must map
each choice to a concrete operator action that actually changes
state.

This test file covers :func:`_emit_empty_contract_hitl` directly:
the helper persists a structured HITL on the pipeline so the SDLC
skill renders it alongside ``status: failed``.  Persistence goes
through :func:`_persist_hitl_decision`, mocked here so we don't need
a live state store.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)


class TestEmitEmptyContractHitl:
    def test_calls_persist_helper_with_recovery_options(self):
        from routes.pipelines import (
            _EMPTY_CONTRACT_HITL_OPTIONS,
            _emit_empty_contract_hitl,
        )

        pipeline = MagicMock()
        store = MagicMock()
        with patch(
            "routes.pipelines._persist_hitl_decision",
            return_value=MagicMock(),
        ) as mock_persist:
            _emit_empty_contract_hitl(
                "pipeline-1",
                pipeline,
                store,
                reason="slice_gate_blocked_monolithic_demotion",
                draft_slice_count=15,
                gate="slice_gate",
            )

        assert mock_persist.call_count == 1
        call = mock_persist.call_args
        # Positional args: pipeline_id, pipeline, store
        assert call.args == ("pipeline-1", pipeline, store)
        # Options are the dedicated recovery set, not the generic
        # Retry/Accept/Abort set.
        assert call.kwargs["options"] == list(_EMPTY_CONTRACT_HITL_OPTIONS)
        # Question carries the slice count and the gate name so the
        # operator doesn't have to dig.
        question = call.kwargs["question"]
        assert "15 slices" in question
        assert "slice_gate" in question
        # #2627 review: the recovery URL must interpolate the actual
        # pipeline id, not the literal ``{id}`` placeholder, so operators
        # can copy it verbatim.
        assert "POST /pipelines/pipeline-1/phase/populate-contract" in question
        assert "{id}" not in question

    def test_question_text_quotes_safety_net_gate(self):
        from routes.pipelines import _emit_empty_contract_hitl

        pipeline = MagicMock()
        store = MagicMock()
        with patch(
            "routes.pipelines._persist_hitl_decision",
            return_value=MagicMock(),
        ) as mock_persist:
            _emit_empty_contract_hitl(
                "pipeline-safety",
                pipeline,
                store,
                reason="empty_result",
                draft_slice_count=None,
                gate="start_phase_implement_safety_net",
            )

        question = mock_persist.call_args.kwargs["question"]
        assert "start_phase_implement_safety_net" in question
        # No slice count to quote when the draft is missing/unparseable —
        # the helper falls back to a generic divergence description
        # instead of claiming a fabricated count.
        assert "missing" in question or "unparseable" in question
        # Recovery URL must interpolate this gate's pipeline id too.
        assert "POST /pipelines/pipeline-safety/phase/populate-contract" in question
        assert "{id}" not in question

    def test_returns_none_when_persist_helper_fails(self):
        """Best-effort: a persistence failure must not block the
        surrounding FAILED-cleanup sequence."""
        from routes.pipelines import _emit_empty_contract_hitl

        pipeline = MagicMock()
        store = MagicMock()
        with patch("routes.pipelines._persist_hitl_decision", return_value=None):
            result = _emit_empty_contract_hitl(
                "pipeline-no-persist",
                pipeline,
                store,
                reason="empty_result",
                draft_slice_count=None,
                gate="plan_complete",
            )
        assert result is None

    def test_options_distinct_from_generic_phase_failure_set(self):
        """The dedicated options must not collide with the generic
        Retry/Accept/Abort set used by the consensus-timeout HITL —
        otherwise the SDLC skill may dedup the decisions and the
        operator never sees the empty-contract recovery path."""
        from routes.pipelines import _EMPTY_CONTRACT_HITL_OPTIONS

        generic = {"Retry phase", "Accept current state", "Abort phase"}
        dedicated = set(_EMPTY_CONTRACT_HITL_OPTIONS)
        assert dedicated.isdisjoint(generic), (
            "Empty-contract recovery options collide with the generic "
            "phase-failure set — operators would see a dedup instead "
            "of the dedicated decision."
        )


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
