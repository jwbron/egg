"""BRC handlers thread ``slice_id`` from ``EGG_SLICE_ID`` onto signals (#2403).

Per-slice agents must tag every consensus signal with their ``slice_id``
so the orchestrator routes ``CONSENSUS_*`` to the slice's tracker. The
spawn path sets ``EGG_SLICE_ID`` (and leaves ``EGG_PIPELINE_ID`` as the
bare pipeline id); the handlers in ``egg_agent_tools.handlers.brc`` are
the agent-side end of that contract.
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


_PROPOSE_REQ = {
    "pipeline_id": "issue-2403",
    "role": "coder",
    "summary": (
        "Implemented slice-2 work with substantive commit message "
        "well over the fifty-character validator threshold"
    ),
    "artifacts": ["src/a.py"],
    "tests_run": [],
    "tasks": [],
    "attestation": {},
}

_ACK_REQ = {
    "pipeline_id": "issue-2403",
    "role": "reviewer_code",
    "producer_role": "coder",
    "reason": "Reviewed src/a.py: substantive multi-file review well over fifty chars",
    "files_reviewed": ["src/a.py"],
    "ack_version": 1,
}

_NACK_REQ = {
    "pipeline_id": "issue-2403",
    "role": "reviewer_code",
    "producer_role": "coder",
    "reason": "src/a.py:42 raises on empty input — substantive blocker over fifty chars",
    "files_reviewed": ["src/a.py"],
    "nack_version": 1,
}

_CONFIRM_REQ = {"pipeline_id": "issue-2403", "role": "coder"}

_RESOLVE_REQ = {
    "pipeline_id": "issue-2403",
    "role": "tester",
    "reviewer_role": "reviewer_code",
    "producer_role": "coder",
    "note": "git mv old/path new/path satisfied in-cycle",
}


def _captured_data(mock_request: Any) -> dict[str, Any]:
    assert mock_request.called, "orchestrator_request was not invoked"
    return dict(mock_request.call_args.kwargs["data"])


class TestSliceIdAttachedFromEnv:
    """``EGG_SLICE_ID`` flows onto every CONSENSUS_* signal body."""

    @pytest.fixture(autouse=True)
    def _set_slice_env(self, monkeypatch):
        monkeypatch.setenv("EGG_SLICE_ID", "slice-2")

    def test_propose_attaches_slice_id(self):
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {"consensus": {"agents": {}}}},
        ) as mock_request:
            handlers.brc_propose(dict(_PROPOSE_REQ))
        assert _captured_data(mock_request)["slice_id"] == "slice-2"

    def test_ack_attaches_slice_id(self):
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as mock_request:
            handlers.brc_ack(dict(_ACK_REQ))
        assert _captured_data(mock_request)["slice_id"] == "slice-2"

    def test_nack_attaches_slice_id(self):
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as mock_request:
            handlers.brc_nack(dict(_NACK_REQ))
        assert _captured_data(mock_request)["slice_id"] == "slice-2"

    def test_confirm_attaches_slice_id(self):
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {"status": "confirmed"}},
        ) as mock_request:
            handlers.brc_confirm(dict(_CONFIRM_REQ))
        assert _captured_data(mock_request)["slice_id"] == "slice-2"

    def test_resolve_obligation_attaches_slice_id(self):
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as mock_request:
            handlers.brc_resolve_obligation(dict(_RESOLVE_REQ))
        assert _captured_data(mock_request)["slice_id"] == "slice-2"


class TestSliceIdAbsentWhenEnvUnset:
    """Pipeline-level agents (no ``EGG_SLICE_ID``) send no ``slice_id``."""

    @pytest.fixture(autouse=True)
    def _no_slice_env(self, monkeypatch):
        monkeypatch.delenv("EGG_SLICE_ID", raising=False)

    def test_propose_omits_slice_id(self):
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {"consensus": {"agents": {}}}},
        ) as mock_request:
            handlers.brc_propose(dict(_PROPOSE_REQ))
        assert "slice_id" not in _captured_data(mock_request)

    def test_ack_omits_slice_id(self):
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as mock_request:
            handlers.brc_ack(dict(_ACK_REQ))
        assert "slice_id" not in _captured_data(mock_request)


class TestSliceIdReqOverridesEnv:
    """A caller-supplied ``slice_id`` on the request takes precedence."""

    def test_req_slice_id_wins_over_env(self, monkeypatch):
        from egg_agent_tools.handlers import brc as handlers

        monkeypatch.setenv("EGG_SLICE_ID", "slice-9")

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {"consensus": {"agents": {}}}},
        ) as mock_request:
            handlers.brc_propose({**_PROPOSE_REQ, "slice_id": "slice-3"})
        assert _captured_data(mock_request)["slice_id"] == "slice-3"


class TestSliceIdValidation:
    """Defense-in-depth: malformed ``slice_id`` is rejected before the wire."""

    def test_invalid_slice_id_raises(self, monkeypatch):
        from egg_agent_tools.handlers import brc as handlers
        from egg_agent_tools.handlers.errors import HandlerError

        # Anything other than ``slice-<N>`` must be rejected — a trailing
        # path component would corrupt the orchestrator's tracker key.
        monkeypatch.setenv("EGG_SLICE_ID", "slice-2/../etc")

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {"consensus": {"agents": {}}}},
        ):
            with pytest.raises(HandlerError, match="slice_id"):
                handlers.brc_propose(dict(_PROPOSE_REQ))


class TestSliceIdHelper:
    """``get_slice_id`` reads ``EGG_SLICE_ID`` and returns None when unset."""

    def test_returns_value_when_set(self, monkeypatch):
        from egg_agent_tools.handlers._gateway import get_slice_id

        monkeypatch.setenv("EGG_SLICE_ID", "slice-7")
        assert get_slice_id() == "slice-7"

    def test_returns_none_when_unset(self, monkeypatch):
        from egg_agent_tools.handlers._gateway import get_slice_id

        monkeypatch.delenv("EGG_SLICE_ID", raising=False)
        assert get_slice_id() is None

    def test_returns_none_on_empty_string(self, monkeypatch):
        from egg_agent_tools.handlers._gateway import get_slice_id

        monkeypatch.setenv("EGG_SLICE_ID", "")
        assert get_slice_id() is None
