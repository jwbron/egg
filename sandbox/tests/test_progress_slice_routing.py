"""Progress error-signal handler threads ``slice_id`` from ``EGG_SLICE_ID`` (#2422).

Per-slice agents must tag the error signal with their ``slice_id`` so the
orchestrator's "agent already COMPLETE" suppression check can scope by
``(role, slice_id)`` instead of role alone — without it, a slice-2 coder
finishing first would silently swallow a slice-3 coder's error because
both ``AgentExecution`` records share ``phase_exec.agents``.
"""

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)


_ERROR_REQ = {
    "pipeline_id": "issue-2422",
    "role": "coder",
    "error": "Build failed",
    "recoverable": False,
}


def _captured_data(mock_request: Any) -> dict[str, Any]:
    assert mock_request.called, "orchestrator_request was not invoked"
    return dict(mock_request.call_args.kwargs["data"])


class TestErrorSignalSliceId:
    """``EGG_SLICE_ID`` flows onto the error signal body (#2422)."""

    def test_error_signal_attaches_slice_id_from_env(self, monkeypatch):
        from egg_agent_tools.handlers import progress as handlers

        monkeypatch.setenv("EGG_SLICE_ID", "slice-3")

        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as mock_request:
            handlers.progress_signal_error(dict(_ERROR_REQ))

        assert _captured_data(mock_request)["slice_id"] == "slice-3"

    def test_request_slice_id_overrides_env(self, monkeypatch):
        from egg_agent_tools.handlers import progress as handlers

        monkeypatch.setenv("EGG_SLICE_ID", "slice-9")

        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as mock_request:
            handlers.progress_signal_error({**_ERROR_REQ, "slice_id": "slice-3"})

        assert _captured_data(mock_request)["slice_id"] == "slice-3"

    def test_no_slice_id_omits_field(self, monkeypatch):
        from egg_agent_tools.handlers import progress as handlers

        monkeypatch.delenv("EGG_SLICE_ID", raising=False)

        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as mock_request:
            handlers.progress_signal_error(dict(_ERROR_REQ))

        assert "slice_id" not in _captured_data(mock_request)

    def test_invalid_slice_id_rejected(self, monkeypatch):
        from egg_agent_tools.handlers import progress as handlers
        from egg_agent_tools.handlers.errors import HandlerError

        # Path-separator values must not reach the wire — defense in depth
        # against a malformed env var.
        monkeypatch.setenv("EGG_SLICE_ID", "slice-2/../etc")

        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {}},
        ):
            with pytest.raises(HandlerError, match="slice_id"):
                handlers.progress_signal_error(dict(_ERROR_REQ))
