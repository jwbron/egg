"""Unit tests for egg_agent_tools.handlers.phase.

Covers phase_get_context and phase_get_assigned_tasks.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import phase  # noqa: E402
from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402


def _fake_contract():
    return {
        "current_phase": "implement",
        "phases": [
            {
                "id": "phase-1",
                "name": "Shared handlers",
                "tasks": [
                    {
                        "id": "task-1-1",
                        "description": "handlers",
                        "status": "pending",
                        "role": "coder",
                        "acceptance": "done",
                    },
                    {
                        "id": "task-4-1",
                        "description": "tests",
                        "status": "pending",
                        "role": "tester",
                        "acceptance": "ok",
                    },
                ],
            }
        ],
    }


class TestPhaseGetContext:
    def test_returns_tasks_filtered_by_role(self):
        with (
            patch(
                "egg_agent_tools.handlers.phase.gateway_request",
                return_value={"success": True, "data": _fake_contract()},
            ),
            patch(
                "egg_agent_tools.handlers.phase.get_contract_identifier",
                return_value=1765,
            ),
            patch(
                "egg_agent_tools.handlers.phase.get_agent_role",
                return_value="tester",
            ),
            patch(
                "egg_agent_tools.handlers.phase.get_phase",
                return_value="implement",
            ),
            patch(
                "egg_agent_tools.handlers.phase.get_pipeline_id",
                return_value="issue-1765",
            ),
        ):
            resp = phase.phase_get_context({"include_artifacts": False})
        assert resp["ok"] is True
        assert resp["role"] == "tester"
        assert resp["phase"] == "implement"
        assert [t["id"] for t in resp["tasks"]] == ["task-4-1"]
        assert resp["contract_present"] is True

    def test_missing_contract_still_returns_context(self):
        """Best-effort — missing identifier doesn't blow up."""
        with (
            patch(
                "egg_agent_tools.handlers.phase.get_contract_identifier",
                return_value=None,
            ),
            patch("egg_agent_tools.handlers.phase.get_agent_role", return_value="coder"),
            patch("egg_agent_tools.handlers.phase.get_phase", return_value="implement"),
            patch(
                "egg_agent_tools.handlers.phase.get_pipeline_id",
                return_value="issue-1765",
            ),
        ):
            resp = phase.phase_get_context({"include_artifacts": False})
        assert resp["ok"] is True
        assert resp["contract_present"] is False
        assert resp["tasks"] == []

    def test_coder_default_for_unlabelled_task(self):
        """Tasks without an explicit role belong to the coder."""
        contract = {
            "current_phase": "implement",
            "phases": [{"id": "p", "name": "x", "tasks": [{"id": "t1", "status": "p"}]}],
        }
        with (
            patch(
                "egg_agent_tools.handlers.phase.gateway_request",
                return_value={"success": True, "data": contract},
            ),
            patch("egg_agent_tools.handlers.phase.get_contract_identifier", return_value=1),
            patch("egg_agent_tools.handlers.phase.get_agent_role", return_value="coder"),
            patch("egg_agent_tools.handlers.phase.get_phase", return_value="implement"),
            patch(
                "egg_agent_tools.handlers.phase.get_pipeline_id",
                return_value="issue-1765",
            ),
        ):
            resp = phase.phase_get_context({"include_artifacts": False})
        assert [t["id"] for t in resp["tasks"]] == ["t1"]

    def test_include_artifacts_returns_paths_when_state_dir_exists(self, tmp_path, monkeypatch):
        state = tmp_path / ".egg-state" / "drafts"
        state.mkdir(parents=True)
        (state / "1765-plan.md").write_text("x")
        (state / "1766-unrelated.md").write_text("x")
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
        with (
            patch("egg_agent_tools.handlers.phase.get_contract_identifier", return_value=None),
            patch("egg_agent_tools.handlers.phase.get_agent_role", return_value="coder"),
            patch("egg_agent_tools.handlers.phase.get_phase", return_value="implement"),
            patch(
                "egg_agent_tools.handlers.phase.get_pipeline_id",
                return_value="issue-1765",
            ),
        ):
            resp = phase.phase_get_context({})
        # Only the 1765 prefix is surfaced.
        assert any("1765-plan.md" in p for p in resp["artifacts"])
        assert not any("1766" in p for p in resp["artifacts"])

    def test_gateway_error_propagates_when_identifier_resolves(self):
        """Gateway errors must bubble up — infra problems are real, not
        silently masked with an empty context."""
        with (
            patch(
                "egg_agent_tools.handlers.phase.gateway_request",
                side_effect=GatewayError("server", status_code=500),
            ),
            patch("egg_agent_tools.handlers.phase.get_contract_identifier", return_value=1),
            patch("egg_agent_tools.handlers.phase.get_agent_role", return_value="coder"),
            patch("egg_agent_tools.handlers.phase.get_phase", return_value="implement"),
            patch(
                "egg_agent_tools.handlers.phase.get_pipeline_id",
                return_value="issue-1765",
            ),
        ):
            with pytest.raises(GatewayError):
                phase.phase_get_context({"include_artifacts": False})


class TestPhaseGetAssignedTasks:
    def test_happy_path(self):
        with (
            patch(
                "egg_agent_tools.handlers.phase.gateway_request",
                return_value={"success": True, "data": _fake_contract()},
            ),
            patch("egg_agent_tools.handlers.phase.get_contract_identifier", return_value=1),
            patch("egg_agent_tools.handlers.phase.get_agent_role", return_value="tester"),
        ):
            resp = phase.phase_get_assigned_tasks({})
        assert resp["count"] == 1
        assert resp["tasks"][0]["id"] == "task-4-1"

    def test_status_filter(self):
        contract = {
            "current_phase": "implement",
            "phases": [
                {
                    "id": "p",
                    "name": "x",
                    "tasks": [
                        {
                            "id": "t1",
                            "status": "complete",
                            "role": "tester",
                            "acceptance": "",
                        },
                        {
                            "id": "t2",
                            "status": "pending",
                            "role": "tester",
                            "acceptance": "",
                        },
                    ],
                }
            ],
        }
        with (
            patch(
                "egg_agent_tools.handlers.phase.gateway_request",
                return_value={"success": True, "data": contract},
            ),
            patch("egg_agent_tools.handlers.phase.get_contract_identifier", return_value=1),
            patch("egg_agent_tools.handlers.phase.get_agent_role", return_value="tester"),
        ):
            resp = phase.phase_get_assigned_tasks({"status": "pending"})
        assert resp["count"] == 1
        assert resp["tasks"][0]["id"] == "t2"

    def test_gateway_error(self):
        with (
            patch(
                "egg_agent_tools.handlers.phase.gateway_request",
                side_effect=GatewayError("timeout"),
            ),
            patch("egg_agent_tools.handlers.phase.get_contract_identifier", return_value=1),
        ):
            with pytest.raises(GatewayError):
                phase.phase_get_assigned_tasks({})

    def test_missing_identifier(self):
        with patch("egg_agent_tools.handlers.phase.get_contract_identifier", return_value=None):
            with pytest.raises(HandlerError):
                phase.phase_get_assigned_tasks({})


# ---------------------------------------------------------------------------
# Iter-2 (#1917): phase_complete_phase
# ---------------------------------------------------------------------------


class TestPhaseCompletePhase:
    def _ok(self):
        return patch(
            "egg_agent_tools.handlers.phase.gateway_request",
            return_value={"success": True, "data": {}},
        )

    def _id(self, value=42):
        return patch("egg_agent_tools.handlers.phase.get_contract_identifier", return_value=value)

    def test_happy_path_without_commit(self):
        with self._ok() as gr, self._id():
            resp = phase.phase_complete_phase({"phase": "phase-2"})
        assert resp == {"ok": True, "phase": "phase-2", "commit": None}
        assert gr.call_count == 1
        data = gr.call_args.kwargs["data"]
        assert data["field_path"] == "phases.1.status"
        assert data["new_value"] == "complete"

    def test_happy_path_with_commit(self):
        """Commit-link lands FIRST (idempotent + retryable) then the
        status flip — reviewer NACK #6 atomicity fix."""
        with self._ok() as gr, self._id():
            resp = phase.phase_complete_phase({"phase": "phase-1", "commit": "a" * 40})
        assert resp == {"ok": True, "phase": "phase-1", "commit": "a" * 40}
        # Two calls: commit-link first, then status flip.
        assert gr.call_count == 2
        first = gr.call_args_list[0].kwargs["data"]
        second = gr.call_args_list[1].kwargs["data"]
        assert first["field_path"] == "phases.0.commit"
        assert first["new_value"] == "a" * 40
        assert second["field_path"] == "phases.0.status"
        assert second["new_value"] == "complete"

    def test_missing_phase(self):
        with pytest.raises(HandlerError):
            phase.phase_complete_phase({})

    @pytest.mark.parametrize("bad", ["", "p-1", "phase-", "phase-0", "phase-a", "phase-1-2"])
    def test_invalid_phase_id(self, bad):
        with pytest.raises(HandlerError):
            phase.phase_complete_phase({"phase": bad})

    def test_invalid_commit_sha(self):
        with self._id():
            with pytest.raises(HandlerError):
                phase.phase_complete_phase({"phase": "phase-1", "commit": "zzz"})

    def test_non_string_commit(self):
        with self._id():
            with pytest.raises(HandlerError):
                phase.phase_complete_phase({"phase": "phase-1", "commit": 123})

    def test_missing_identifier(self):
        with patch("egg_agent_tools.handlers.phase.get_contract_identifier", return_value=None):
            with pytest.raises(HandlerError):
                phase.phase_complete_phase({"phase": "phase-1"})

    def test_gateway_500_on_status_raises(self):
        with (
            patch(
                "egg_agent_tools.handlers.phase.gateway_request",
                side_effect=GatewayError("boom", status_code=500),
            ),
            self._id(),
        ):
            with pytest.raises(GatewayError):
                phase.phase_complete_phase({"phase": "phase-1"})

    def test_commit_link_failure_raises_and_does_not_proceed_to_status(self):
        """reviewer NACK #6 atomicity: commit link first; if it fails,
        status must NOT flip (caller can retry with the same request).
        The resulting error is a plain GatewayError — no special
        'marked complete but failed to link commit' string because the
        phase was never marked complete."""
        responses = [
            {"success": False, "message": "locked"},  # commit link fails
        ]
        with (
            patch(
                "egg_agent_tools.handlers.phase.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            self._id(),
        ):
            with pytest.raises(GatewayError):
                phase.phase_complete_phase({"phase": "phase-1", "commit": "a" * 40})
        # Only one call — the commit link failed; the status flip must
        # NOT have been attempted.
        assert gr.call_count == 1

    def test_status_failure_after_commit_linked_is_retryable(self):
        """If the commit link succeeds but the status flip fails, the
        caller can retry the same request — the commit-link step is
        idempotent.  The error must still surface as a GatewayError."""
        responses = [
            {"success": True, "data": {}},  # commit link ok
            {"success": False, "message": "conflict"},  # status fails
        ]
        with (
            patch(
                "egg_agent_tools.handlers.phase.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ),
            self._id(),
        ):
            with pytest.raises(GatewayError):
                phase.phase_complete_phase({"phase": "phase-1", "commit": "a" * 40})

    def test_unsuccessful_status_response_raises(self):
        with (
            patch(
                "egg_agent_tools.handlers.phase.gateway_request",
                return_value={"success": False, "message": "denied"},
            ),
            self._id(),
        ):
            with pytest.raises(GatewayError):
                phase.phase_complete_phase({"phase": "phase-1"})
