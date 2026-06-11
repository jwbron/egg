"""Reviewer attestations thread through the BRC MCP boundary (#3114).

Before #3114 the ``mcp__brc__ack`` / ``mcp__brc__nack`` handlers built
their signal payloads from a fixed field set and silently dropped any
attestation — making the orchestrator's reviewer-attestation validation
(and the contract-enforcer's ``tasks_verified`` cross-check)
structurally unreachable. These tests pin the threading and the
structured surfacing of the gate's 409 rejections.
"""

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Add sandbox to sys.path so egg_agent_tools is importable
_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)


_ATTESTATION = {"tasks_verified": ["task-2-1", "task-2-2"]}

_ACK_REQ = {
    "pipeline_id": "pipeline-3114",
    "role": "reviewer_contract",
    "producer_role": "coder",
    "reason": "Verified task-2-1 and task-2-2 against the diff — well over fifty chars",
    "files_reviewed": ["src/a.py"],
    "ack_version": 1,
    "attestation": _ATTESTATION,
}

_NACK_REQ = {
    "pipeline_id": "pipeline-3114",
    "role": "reviewer_contract",
    "producer_role": "documenter",
    "reason": "task-2-3 and task-2-8 are still pending — deliver or mark complete",
    "files_reviewed": ["docs/x.md"],
    "nack_version": 1,
    "attestation": _ATTESTATION,
}


def _captured_payload(mock_request: Any) -> dict[str, Any]:
    assert mock_request.called, "orchestrator_request was not invoked"
    return dict(mock_request.call_args.kwargs["data"]["payload"])


class TestAttestationThreading:
    def test_ack_threads_attestation(self):
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as mock_request:
            handlers.brc_ack(dict(_ACK_REQ))
        assert _captured_payload(mock_request)["attestation"] == _ATTESTATION

    def test_ack_without_attestation_omits_key(self):
        from egg_agent_tools.handlers import brc as handlers

        req = {k: v for k, v in _ACK_REQ.items() if k != "attestation"}
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as mock_request:
            handlers.brc_ack(req)
        assert "attestation" not in _captured_payload(mock_request)

    def test_ack_rejects_non_object_attestation(self):
        from egg_agent_tools.handlers import brc as handlers
        from egg_agent_tools.handlers.errors import HandlerError

        req = dict(_ACK_REQ, attestation="task-2-1")
        with pytest.raises(HandlerError, match="attestation"):
            handlers.brc_ack(req)

    def test_nack_threads_attestation(self):
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as mock_request:
            handlers.brc_nack(dict(_NACK_REQ))
        assert _captured_payload(mock_request)["attestation"] == _ATTESTATION


class TestStructuredGateRejections:
    """409 gate rejections surface as data, not raised GatewayError."""

    @pytest.mark.parametrize(
        "status",
        ["contract_incomplete", "attestation_required", "attestation_mismatch"],
    )
    def test_ack_surfaces_gate_rejection(self, status: str) -> None:
        from egg_agent_tools.handlers import brc as handlers
        from egg_agent_tools.handlers.errors import GatewayError

        err = GatewayError(
            "rejected by gate",
            status_code=409,
            details={"status": status, "incomplete_tasks": []},
        )

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            side_effect=err,
        ):
            result = handlers.brc_ack(dict(_ACK_REQ))
        assert result["ok"] is False
        assert result["status"] == status
        assert result["rejection"]["status"] == status

    def test_confirm_surfaces_contract_incomplete(self):
        from egg_agent_tools.handlers import brc as handlers
        from egg_agent_tools.handlers.errors import GatewayError

        err = GatewayError(
            "confirm rejected",
            status_code=409,
            details={
                "status": "contract_incomplete",
                "incomplete_tasks": [{"id": "task-2-3"}],
            },
        )

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            side_effect=err,
        ):
            result = handlers.brc_confirm(
                {"pipeline_id": "pipeline-3114", "role": "reviewer_contract"}
            )
        assert result["ok"] is False
        assert result["status"] == "contract_incomplete"
        assert result["rejection"]["incomplete_tasks"] == [{"id": "task-2-3"}]

    def test_confirm_other_errors_still_raise(self):
        from egg_agent_tools.handlers import brc as handlers
        from egg_agent_tools.handlers.errors import GatewayError

        err = GatewayError("boom", status_code=500, details={})

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            side_effect=err,
        ):
            with pytest.raises(GatewayError):
                handlers.brc_confirm({"pipeline_id": "pipeline-3114", "role": "reviewer_contract"})
