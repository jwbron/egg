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
    # Supply an explicit commit SHA so brc_propose does not fall back to
    # `git rev-parse HEAD`. These tests exercise slice_id routing, not HEAD
    # resolution, and the fallback shells out to git in EGG_REPO_PATH — which
    # need not exist (e.g. the CI runner), making the suite environment-dependent.
    "commit_sha": "0123456789abcdef0123456789abcdef01234567",
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


def _captured_endpoint(mock_request: Any) -> str:
    assert mock_request.called, "orchestrator_request was not invoked"
    return str(mock_request.call_args.args[0])


_STATE_REQ = {"pipeline_id": "issue-2403"}


class TestGetStateSliceScope:
    """``brc_get_state`` scopes the /status read to the agent's slice (#2761).

    A per-slice agent's BRC consensus lives in the per-slice tracker
    ``{pipeline_id}/{slice_id}``. ``brc_get_state`` must forward the
    slice scope to the status endpoint so ``mcp__brc__get_state`` /
    ``egg-orch consensus status`` report the agent's own slice rather
    than a pipeline-level (non-slice) reconstruction.
    """

    def test_get_state_appends_slice_id_from_env(self, monkeypatch):
        from egg_agent_tools.handlers import brc as handlers

        monkeypatch.setenv("EGG_SLICE_ID", "slice-2")
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"data": {}},
        ) as mock_request:
            resp = handlers.brc_get_state(dict(_STATE_REQ))

        assert (
            _captured_endpoint(mock_request)
            == "/api/v1/pipelines/issue-2403/status?slice_id=slice-2"
        )
        assert resp["slice_id"] == "slice-2"

    def test_get_state_omits_slice_id_when_env_unset(self, monkeypatch):
        from egg_agent_tools.handlers import brc as handlers

        monkeypatch.delenv("EGG_SLICE_ID", raising=False)
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"data": {}},
        ) as mock_request:
            resp = handlers.brc_get_state(dict(_STATE_REQ))

        endpoint = _captured_endpoint(mock_request)
        assert endpoint == "/api/v1/pipelines/issue-2403/status"
        assert "?" not in endpoint
        assert resp["slice_id"] is None

    def test_get_state_req_slice_id_overrides_env(self, monkeypatch):
        from egg_agent_tools.handlers import brc as handlers

        monkeypatch.setenv("EGG_SLICE_ID", "slice-9")
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"data": {}},
        ) as mock_request:
            handlers.brc_get_state({**_STATE_REQ, "slice_id": "slice-3"})

        assert "slice_id=slice-3" in _captured_endpoint(mock_request)

    def test_get_state_rejects_malformed_slice_id(self, monkeypatch):
        from egg_agent_tools.handlers import brc as handlers
        from egg_agent_tools.handlers.errors import HandlerError

        monkeypatch.setenv("EGG_SLICE_ID", "slice-2/../etc")
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"data": {}},
        ):
            with pytest.raises(HandlerError, match="slice_id"):
                handlers.brc_get_state(dict(_STATE_REQ))


def _slice_block(*, is_complete: bool, blocking: list[str]) -> dict[str, Any]:
    return {
        "agents": {"coder": {"producer_phase": "PROPOSED", "confirmed": is_complete}},
        "is_complete": is_complete,
        "blocking_agents": blocking,
        "has_unresolved_nacks": False,
        "unresolved_nacks": [],
        "protocol": "brc",
    }


class TestGetStateSliceConsensusResolution:
    """Slice-id-less queries resolve live slice trackers (#3487).

    In a slice-DAG implement phase the trackers are keyed
    ``{pipeline_id}/{slice_id}`` and the status route surfaces them
    under ``concurrent.slice_consensus`` (#3481). ``brc_get_state``
    must mirror the orchestrator MCP ``get_consensus_status``
    semantics: a single active slice is served directly, multiple stay
    keyed per-slice and are never merged (#2761).
    """

    @pytest.fixture(autouse=True)
    def _no_slice_env(self, monkeypatch):
        monkeypatch.delenv("EGG_SLICE_ID", raising=False)

    def test_single_active_slice_resolves_directly(self):
        from egg_agent_tools.handlers import brc as handlers

        block = _slice_block(is_complete=False, blocking=["coder"])
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"data": {"concurrent": {"slice_consensus": {"slice-3": block}}}},
        ):
            resp = handlers.brc_get_state(dict(_STATE_REQ))

        assert resp["resolved_slice_id"] == "slice-3"
        assert resp["slice_id"] is None
        assert resp["consensus"]["blocking_agents"] == ["coder"]
        assert "note" in resp["consensus"]
        assert resp["is_complete"] is False
        assert resp["blocking_agents"] == ["coder"]
        assert "slice_consensus" not in resp

    def test_multiple_active_slices_stay_keyed_per_slice(self):
        from egg_agent_tools.handlers import brc as handlers

        slice_map = {
            "slice-5": _slice_block(is_complete=True, blocking=[]),
            "slice-3": _slice_block(is_complete=False, blocking=["tester"]),
        }
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"data": {"concurrent": {"slice_consensus": slice_map}}},
        ):
            resp = handlers.brc_get_state(dict(_STATE_REQ))

        assert resp["active_slice_ids"] == ["slice-3", "slice-5"]
        assert resp["slice_consensus"]["slice-3"]["blocking_agents"] == ["tester"]
        assert resp["slice_consensus"]["slice-5"]["is_complete"] is True
        # The top-level block is never a merged cross-slice view (#2761).
        assert resp["consensus"] == {}
        assert resp["is_complete"] is False
        assert resp["blocking_agents"] == []
        assert "resolved_slice_id" not in resp

    def test_agentless_pipeline_block_falls_through_to_slices(self):
        # A truthy-but-agent-less pipeline block must not short-circuit and
        # serve empty state; it falls through to slice resolution, mirroring
        # the #3485 orchestrator guard (`consensus.get("agents")`).
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={
                "data": {
                    "concurrent": {
                        "consensus": {"protocol": "brc", "agents": {}},
                        "slice_consensus": {
                            "slice-3": _slice_block(is_complete=False, blocking=["coder"])
                        },
                    }
                }
            },
        ):
            resp = handlers.brc_get_state(dict(_STATE_REQ))

        assert resp["resolved_slice_id"] == "slice-3"
        assert resp["blocking_agents"] == ["coder"]

    def test_pipeline_level_consensus_takes_precedence(self):
        from egg_agent_tools.handlers import brc as handlers

        pipeline_block = _slice_block(is_complete=True, blocking=[])
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={
                "data": {
                    "concurrent": {
                        "consensus": pipeline_block,
                        "slice_consensus": {
                            "slice-3": _slice_block(is_complete=False, blocking=["coder"])
                        },
                    }
                }
            },
        ):
            resp = handlers.brc_get_state(dict(_STATE_REQ))

        assert resp["consensus"] == pipeline_block
        assert resp["is_complete"] is True
        assert "resolved_slice_id" not in resp
        assert "slice_consensus" not in resp

    def test_explicit_slice_id_skips_resolution(self, monkeypatch):
        from egg_agent_tools.handlers import brc as handlers

        monkeypatch.setenv("EGG_SLICE_ID", "slice-3")
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={
                "data": {
                    "concurrent": {
                        "slice_consensus": {
                            "slice-9": _slice_block(is_complete=False, blocking=["coder"])
                        }
                    }
                }
            },
        ):
            resp = handlers.brc_get_state(dict(_STATE_REQ))

        # A slice-scoped query reports only its own (empty here) block;
        # the surfaced map for other slices is not resolved into it.
        assert resp["slice_id"] == "slice-3"
        assert resp["consensus"] == {}
        assert "resolved_slice_id" not in resp
        assert "slice_consensus" not in resp


class TestListBlockingSliceAware:
    """``brc_list_blocking`` unions blockers across live slices (#3487).

    Kept consistent with ``brc_get_state`` so an overseer keying off
    ``mcp__brc__list_blocking`` in a slice-DAG phase isn't told nothing
    is blocking while a per-slice round is wedged.
    """

    @pytest.fixture(autouse=True)
    def _no_slice_env(self, monkeypatch):
        monkeypatch.delenv("EGG_SLICE_ID", raising=False)

    def test_pipeline_level_block_used_when_present(self):
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={
                "data": {
                    "concurrent": {
                        "consensus": _slice_block(is_complete=False, blocking=["coder"]),
                        "slice_consensus": {
                            "slice-3": _slice_block(is_complete=False, blocking=["tester"])
                        },
                    }
                }
            },
        ):
            resp = handlers.brc_list_blocking(dict(_STATE_REQ))

        # Pipeline-level block (has agents) wins; slices are not unioned in.
        assert resp["blocking_agents"] == ["coder"]

    def test_unions_blockers_across_slices(self):
        from egg_agent_tools.handlers import brc as handlers

        slice_map = {
            "slice-5": _slice_block(is_complete=False, blocking=["coder"]),
            "slice-3": _slice_block(is_complete=False, blocking=["tester", "coder"]),
        }
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"data": {"concurrent": {"slice_consensus": slice_map}}},
        ):
            resp = handlers.brc_list_blocking(dict(_STATE_REQ))

        # De-duplicated union across slices; first-seen order preserved.
        assert resp["blocking_agents"] == ["coder", "tester"]

    def test_explicit_slice_id_reads_only_that_slice(self, monkeypatch):
        from egg_agent_tools.handlers import brc as handlers

        monkeypatch.setenv("EGG_SLICE_ID", "slice-3")
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={
                "data": {
                    "concurrent": {
                        "consensus": _slice_block(is_complete=False, blocking=["tester"])
                    }
                }
            },
        ) as mock_request:
            resp = handlers.brc_list_blocking(dict(_STATE_REQ))

        assert "slice_id=slice-3" in _captured_endpoint(mock_request)
        assert resp["blocking_agents"] == ["tester"]
