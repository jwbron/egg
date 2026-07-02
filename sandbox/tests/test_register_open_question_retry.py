"""``register_open_question`` retries a rejected stale-index write (#3427).

The mutate route rejects a whole-entry write to an existing
``decisions[]`` index with 409 (the append-only guard). The sandbox
``gateway_request`` RAISES ``GatewayError`` on non-2xx, so the handler's
TOCTOU retry loop must catch it and feed the server message through the
retryable check — before #3427 the loop only inspected returned dicts and
an HTTP rejection escaped on the first attempt, so the loop never
actually retried.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# The host-side test harness mirrors the sandbox-image layout via
# PYTHONPATH; insert here for IDE / pytest -m runs that don't go
# through the Makefile.
_SANDBOX_DIR = Path(__file__).resolve().parent.parent
_SHARED_DIR = _SANDBOX_DIR.parent / "shared"
for p in (_SHARED_DIR, _SANDBOX_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from egg_agent_tools.handlers import sdlc  # noqa: E402
from egg_agent_tools.handlers.errors import GatewayError  # noqa: E402


def _req() -> dict:
    return {
        "question": "Should we do X?",
        "pipeline_id": "issue-3427",
        "repo_path": "/tmp/repo",
        "phase": "implement",
    }


def test_conflict_rejection_is_retried_with_fresh_read() -> None:
    """A 409 "already exists" rejection re-reads the contract and
    re-mints the next free index/id instead of propagating.
    """
    contracts = [
        {"decisions": [], "current_phase": "implement"},
        # The fresh read reveals the decision another writer landed at
        # index 0 while we were minting.
        {
            "decisions": [
                {
                    "id": "cq-1",
                    "question": "a different question",
                    "type": "hitl",
                    "phase": "implement",
                    "resolved": False,
                }
            ],
            "current_phase": "implement",
        },
    ]
    mutate_calls: list[dict] = []

    def fake_gateway_request(endpoint, *, method="GET", data=None, **_kw):
        mutate_calls.append(data)
        if len(mutate_calls) == 1:
            raise GatewayError(
                "Decision at index 0 already exists (id=cq-1); decisions "
                "are append-only — re-read the contract and mint a fresh "
                "index and id (#3427)",
                status_code=409,
            )
        return {"success": True}

    with (
        patch.object(sdlc, "_fetch_contract", side_effect=contracts),
        patch.object(sdlc, "gateway_request", side_effect=fake_gateway_request),
    ):
        result = sdlc.register_open_question(_req())

    assert result["ok"] is True
    assert result["id"] == "cq-2"
    assert mutate_calls[0]["field_path"] == "decisions.0"
    assert mutate_calls[1]["field_path"] == "decisions.1"


def test_non_retryable_gateway_error_raises_on_first_attempt() -> None:
    with (
        patch.object(
            sdlc,
            "_fetch_contract",
            return_value={"decisions": [], "current_phase": "implement"},
        ),
        patch.object(
            sdlc,
            "gateway_request",
            side_effect=GatewayError("forbidden", status_code=403),
        ) as gateway_mock,
        pytest.raises(GatewayError, match="forbidden"),
    ):
        sdlc.register_open_question(_req())

    assert gateway_mock.call_count == 1


def test_retries_are_bounded() -> None:
    """A persistent conflict gives up after ``_DECISION_RETRY_ATTEMPTS``."""
    with (
        patch.object(
            sdlc,
            "_fetch_contract",
            return_value={"decisions": [], "current_phase": "implement"},
        ),
        patch.object(
            sdlc,
            "gateway_request",
            side_effect=GatewayError("Decision at index 0 already exists", status_code=409),
        ) as gateway_mock,
        pytest.raises(GatewayError, match="already exists"),
    ):
        sdlc.register_open_question(_req())

    assert gateway_mock.call_count == sdlc._DECISION_RETRY_ATTEMPTS
