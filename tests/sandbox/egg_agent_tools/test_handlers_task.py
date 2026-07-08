"""Unit tests for egg_agent_tools.handlers.task.

Covers task_complete (with and without commit SHA).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import task  # noqa: E402
from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402


def _contract(*phase_task_ids, key="slices"):
    """Build a served-contract read response body.

    Each positional arg is the list of task ids for one phase.  The
    orchestrator serves ``slices`` (``Contract.model_dump``); pass
    ``key="phases"`` to exercise the legacy raw-JSON fallback.
    """
    return {
        key: [
            {
                "id": f"phase-{i + 1}",
                "tasks": [{"id": tid, "status": "pending"} for tid in tids],
            }
            for i, tids in enumerate(phase_task_ids)
        ]
    }


def _gateway_seq(*responses):
    """Patch gateway_request to return the given responses in order."""
    seq = list(responses)
    return patch(
        "egg_agent_tools.handlers.task.gateway_request",
        side_effect=lambda *a, **kw: seq.pop(0),
    )


class TestTaskComplete:
    def test_happy_path_without_commit(self):
        with (
            _gateway_seq(
                {"success": True, "data": _contract(["task-1-1", "task-1-2"])},
                {"success": True, "data": {}},
            ) as req,
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1765),
        ):
            resp = task.task_complete({"task": "task-1-1"})
        assert resp["ok"] is True
        assert resp["task"] == "task-1-1"
        assert resp["commit"] is None
        # Two calls: contract read (id → index resolution), then the
        # status mutate.
        assert req.call_count == 2
        assert req.call_args_list[0].args[0] == "/api/v1/contract/1765"
        data = req.call_args.kwargs["data"]
        assert data["field_path"] == "phases.0.tasks.0.status"
        assert data["new_value"] == "complete"

    def test_happy_path_with_commit(self):
        contract = _contract(["task-1-1"], ["task-2-1", "task-2-2", "task-2-3"])
        with (
            _gateway_seq(
                {"success": True, "data": contract},
                {"success": True, "data": {}},
                {"success": True, "data": {}},
            ) as req,
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1765),
        ):
            resp = task.task_complete({"task": "task-2-3", "commit": "abcdef1234"})
        assert resp["ok"] is True
        assert resp["commit"] == "abcdef1234"
        # Three calls: read, then commit link FIRST, then status (safe
        # ordering: mid-way failure leaves task not-yet-complete with
        # commit populated, so the caller can retry).
        assert req.call_count == 3
        commit_call = req.call_args_list[1].kwargs["data"]
        assert commit_call["field_path"] == "phases.1.tasks.2.commit"
        assert commit_call["new_value"] == "abcdef1234"
        status_call = req.call_args_list[2].kwargs["data"]
        assert status_call["field_path"] == "phases.1.tasks.2.status"
        assert status_call["new_value"] == "complete"

    def test_rescoped_slice_resolves_by_id_not_suffix(self):
        """#3527 regression: slice-1 re-scoped to hold only
        [task-1-5, task-1-6] at list positions 0-1.  The completer must
        resolve task-1-5 to tasks[0] by id lookup; the old numeric-suffix
        derivation targeted tasks[4] and every completion failed with
        'list index out of range'."""
        with (
            _gateway_seq(
                {"success": True, "data": _contract(["task-1-5", "task-1-6"])},
                {"success": True, "data": {}},
                {"success": True, "data": {}},
            ) as req,
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=3364),
        ):
            resp = task.task_complete({"task": "task-1-5", "commit": "c2a77847a"})
        assert resp["ok"] is True
        commit_call = req.call_args_list[1].kwargs["data"]
        assert commit_call["field_path"] == "phases.0.tasks.0.commit"
        status_call = req.call_args_list[2].kwargs["data"]
        assert status_call["field_path"] == "phases.0.tasks.0.status"

    def test_single_segment_task_id_resolved_by_lookup(self):
        """'task-5' resolves to wherever that id lives in the contract,
        not positionally to phases.0.tasks.4."""
        with (
            _gateway_seq(
                {"success": True, "data": _contract(["task-4", "task-5"])},
                {"success": True, "data": {}},
            ) as req,
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1),
        ):
            task.task_complete({"task": "task-5"})
        data = req.call_args.kwargs["data"]
        assert data["field_path"] == "phases.0.tasks.1.status"

    def test_case_insensitive_id_match(self):
        """'TASK-1-2' matches the contract's lowercase 'task-1-2' id,
        preserving the old parser's case tolerance."""
        with (
            _gateway_seq(
                {"success": True, "data": _contract(["task-1-1", "task-1-2"])},
                {"success": True, "data": {}},
            ) as req,
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1),
        ):
            task.task_complete({"task": "TASK-1-2"})
        data = req.call_args.kwargs["data"]
        assert data["field_path"] == "phases.0.tasks.1.status"

    def test_legacy_phases_key_fallback(self):
        """Un-migrated raw JSON serves ``phases`` instead of ``slices``;
        the id lookup must read both."""
        with (
            _gateway_seq(
                {"success": True, "data": _contract(["task-1-1"], key="phases")},
                {"success": True, "data": {}},
            ) as req,
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1),
        ):
            task.task_complete({"task": "task-1-1"})
        assert req.call_args.kwargs["data"]["field_path"] == "phases.0.tasks.0.status"

    def test_task_not_found_raises_before_mutate(self):
        """An id absent from the contract fails loudly; no positional
        fallback that could mutate the wrong task."""
        with (
            _gateway_seq(
                {"success": True, "data": _contract(["task-1-5", "task-1-6"])},
            ) as req,
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1),
        ):
            with pytest.raises(HandlerError, match="not found"):
                task.task_complete({"task": "task-1-1"})
        # Only the read; no mutation was attempted.
        assert req.call_count == 1

    def test_missing_task_id(self):
        with pytest.raises(HandlerError):
            task.task_complete({})

    @pytest.mark.parametrize(
        "bad", ["", "x1", "task-", "task-0", "task-1-a", "task-a-b", "task-1-2-3"]
    )
    def test_invalid_task_id(self, bad):
        with pytest.raises(HandlerError):
            task.task_complete({"task": bad})

    def test_invalid_commit_sha(self):
        with pytest.raises(HandlerError):
            task.task_complete({"task": "task-1-1", "commit": "nothex!!"})

    def test_non_string_commit_rejected(self):
        """Non-string truthy commit value must raise HandlerError, not
        TypeError from the regex match."""
        with pytest.raises(HandlerError, match="'commit' must be a string"):
            task.task_complete({"task": "task-1-1", "commit": 123})

    def test_missing_identifier(self):
        with patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=None):
            with pytest.raises(HandlerError):
                task.task_complete({"task": "task-1-1"})

    def test_gateway_500_raises(self):
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=GatewayError("fail", status_code=500),
            ),
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1),
        ):
            with pytest.raises(GatewayError):
                task.task_complete({"task": "task-1-1"})

    def test_commit_link_failure_raises_gateway_error(self):
        """Commit-link is the first mutation; failure means status was
        never set; the task stays in its prior state, safe to retry."""
        with (
            _gateway_seq(
                {"success": True, "data": _contract(["task-1-1"])},
                {"success": False, "message": "commit link failed"},
            ) as req,
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1),
        ):
            with pytest.raises(GatewayError) as exc:
                task.task_complete({"task": "task-1-1", "commit": "a" * 40})
        assert "commit link failed" in str(exc.value).lower()
        # Read + failed commit link; status was never attempted.
        assert req.call_count == 2

    def test_unsuccessful_status_raises(self):
        with (
            _gateway_seq(
                {"success": True, "data": _contract(["task-1-1"])},
                {"success": False, "message": "denied"},
            ),
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1),
        ):
            with pytest.raises(GatewayError):
                task.task_complete({"task": "task-1-1"})


# ---------------------------------------------------------------------------
# Iter-2 (#1917): task_add_commit + task_update_notes + task_mark_gap
# ---------------------------------------------------------------------------


class TestTaskAddCommit:
    def _gw(self, contract=None):
        """Sequenced gateway mock: contract read first, then mutate ok."""
        return _gateway_seq(
            {"success": True, "data": contract or _contract(["task-1-1"])},
            {"success": True, "data": {}},
        )

    def _id(self, value=42):
        return patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=value)

    def test_happy_path(self):
        contract = _contract(["task-1-1"], ["task-2-1", "task-2-2", "task-2-3"])
        with self._gw(contract) as gr, self._id():
            resp = task.task_add_commit({"task": "task-2-3", "commit": "a" * 40})
        assert resp == {"ok": True, "task": "task-2-3", "commit": "a" * 40}
        data = gr.call_args.kwargs["data"]
        assert data["field_path"] == "phases.1.tasks.2.commit"
        assert data["new_value"] == "a" * 40

    def test_rescoped_slice_resolves_by_id_not_suffix(self):
        """#3527 regression: task-1-5 at list position 0 must resolve to
        tasks[0], not tasks[4]."""
        with self._gw(_contract(["task-1-5", "task-1-6"])) as gr, self._id():
            task.task_add_commit({"task": "task-1-6", "commit": "a" * 40})
        assert gr.call_args.kwargs["data"]["field_path"] == "phases.0.tasks.1.commit"

    def test_task_not_found(self):
        with (
            _gateway_seq({"success": True, "data": _contract(["task-1-5"])}) as gr,
            self._id(),
        ):
            with pytest.raises(HandlerError, match="not found"):
                task.task_add_commit({"task": "task-1-1", "commit": "a" * 40})
        assert gr.call_count == 1

    def test_missing_task_id(self):
        with pytest.raises(HandlerError):
            task.task_add_commit({"commit": "a" * 40})

    def test_missing_commit(self):
        with pytest.raises(HandlerError):
            task.task_add_commit({"task": "task-1-1"})

    def test_invalid_commit(self):
        with self._id():
            with pytest.raises(HandlerError):
                task.task_add_commit({"task": "task-1-1", "commit": "not-hex!"})

    def test_short_sha_7_hex_accepted(self):
        with self._gw(), self._id():
            resp = task.task_add_commit({"task": "task-1-1", "commit": "1234567"})
        assert resp["commit"] == "1234567"

    def test_invalid_task_id(self):
        with pytest.raises(HandlerError):
            task.task_add_commit({"task": "bogus", "commit": "a" * 40})

    def test_gateway_failure(self):
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                return_value={"success": False, "message": "denied"},
            ),
            self._id(),
        ):
            with pytest.raises(GatewayError):
                task.task_add_commit({"task": "task-1-1", "commit": "a" * 40})

    def test_gateway_exception_propagates(self):
        def boom(*a, **kw):
            raise GatewayError("timeout")

        with patch("egg_agent_tools.handlers.task.gateway_request", side_effect=boom), self._id():
            with pytest.raises(GatewayError):
                task.task_add_commit({"task": "task-1-1", "commit": "a" * 40})


class TestTaskUpdateNotes:
    def _gw(self, contract=None):
        """Gateway mock: contract read on the first call, mutate-ok after."""
        served = {"success": True, "data": contract or _contract(["task-1-1"])}
        ok = {"success": True, "data": {}}
        state = {"calls": 0}

        def fake(*a, **kw):
            state["calls"] += 1
            return served if state["calls"] == 1 else ok

        return patch("egg_agent_tools.handlers.task.gateway_request", side_effect=fake)

    def _id(self, value=42):
        return patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=value)

    def test_happy_path(self):
        with self._gw() as gr, self._id():
            resp = task.task_update_notes({"task": "task-1-1", "notes": "hello"})
        assert resp == {"ok": True, "task": "task-1-1"}
        data = gr.call_args.kwargs["data"]
        assert data["field_path"] == "phases.0.tasks.0.notes"
        assert data["new_value"] == "hello"

    def test_rescoped_slice_resolves_by_id_not_suffix(self):
        """#3527 regression: id lookup, not numeric-suffix position."""
        with self._gw(_contract(["task-1-5", "task-1-6"])) as gr, self._id():
            task.task_update_notes({"task": "task-1-5", "notes": "n"})
        assert gr.call_args.kwargs["data"]["field_path"] == "phases.0.tasks.0.notes"

    def test_empty_notes_allowed(self):
        """Clearing notes is a valid operation — notes='' shouldn't raise."""
        with self._gw() as gr, self._id():
            task.task_update_notes({"task": "task-1-1", "notes": ""})
        data = gr.call_args.kwargs["data"]
        assert data["new_value"] == ""

    def test_missing_notes_field(self):
        with pytest.raises(HandlerError):
            task.task_update_notes({"task": "task-1-1"})

    def test_none_notes_rejected(self):
        with pytest.raises(HandlerError):
            task.task_update_notes({"task": "task-1-1", "notes": None})

    def test_non_string_notes_rejected(self):
        with pytest.raises(HandlerError):
            task.task_update_notes({"task": "task-1-1", "notes": 123})

    def test_missing_task(self):
        with pytest.raises(HandlerError):
            task.task_update_notes({"notes": "x"})

    def test_gateway_failure(self):
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                return_value={"success": False, "message": "nope"},
            ),
            self._id(),
        ):
            with pytest.raises(GatewayError):
                task.task_update_notes({"task": "task-1-1", "notes": "x"})

    def test_jira_action_status_prefix_projects_to_typed_field(self):
        """Apply-phase notes-prefix projection (issue #1557).

        When the notes start with ``jira_action_status=<value>``, a
        follow-up gateway call propagates the typed
        ``Task.jira_action_status`` field so the apply-phase reviewer
        and the wontdo drain's idempotency gate see a single coherent
        surface.
        """
        with self._gw() as gr, self._id():
            task.task_update_notes(
                {"task": "task-1-1", "notes": "jira_action_status=applied\nall good"}
            )
        # Read + notes mutate + projection mutate.
        assert gr.call_count == 3
        mutates = gr.call_args_list[1:]
        field_paths = [c.kwargs["data"]["field_path"] for c in mutates]
        new_values = [c.kwargs["data"]["new_value"] for c in mutates]
        assert "phases.0.tasks.0.notes" in field_paths
        assert "phases.0.tasks.0.jira_action_status" in field_paths
        assert "applied" in new_values

    def test_jira_key_prefix_projects_to_typed_field(self):
        """Two-line prefix: status + key both projected."""
        with self._gw() as gr, self._id():
            task.task_update_notes(
                {
                    "task": "task-1-1",
                    "notes": "jira_action_status=applied\njira_key=ENG-456\nnarrative",
                }
            )
        # Read + notes mutate + two projection mutates.
        assert gr.call_count == 4
        field_paths = {c.kwargs["data"]["field_path"] for c in gr.call_args_list[1:]}
        assert "phases.0.tasks.0.notes" in field_paths
        assert "phases.0.tasks.0.jira_action_status" in field_paths
        assert "phases.0.tasks.0.jira_key" in field_paths

    def test_no_prefix_skips_projection(self):
        """Notes without a structured prefix make only the notes mutation."""
        with self._gw() as gr, self._id():
            task.task_update_notes({"task": "task-1-1", "notes": "just regular notes"})
        # Read + notes mutate only.
        assert gr.call_count == 2
        assert gr.call_args.kwargs["data"]["field_path"] == "phases.0.tasks.0.notes"

    def test_invalid_prefix_value_skips_projection(self):
        """Unknown ``jira_action_status`` value falls through to notes-only."""
        with self._gw() as gr, self._id():
            task.task_update_notes({"task": "task-1-1", "notes": "jira_action_status=bogus\nrest"})
        assert gr.call_count == 2


class TestProjectNotesPrefix:
    """Direct unit tests for the prefix-parsing projector."""

    def test_no_prefix(self):
        assert task._project_notes_prefix("plain notes\nrest") == (None, None)

    def test_status_only(self):
        assert task._project_notes_prefix("jira_action_status=applied\nrest") == (
            "applied",
            None,
        )

    def test_status_plus_key(self):
        assert task._project_notes_prefix("jira_action_status=applied\njira_key=ENG-456\nrest") == (
            "applied",
            "ENG-456",
        )

    def test_key_only(self):
        assert task._project_notes_prefix("jira_key=ENG-1\nrest") == (None, "ENG-1")

    def test_invalid_status(self):
        assert task._project_notes_prefix("jira_action_status=bogus\nrest") == (None, None)

    def test_invalid_key_format(self):
        assert task._project_notes_prefix("jira_key=lowercase-1\nrest") == (None, None)

    def test_only_first_two_lines_inspected(self):
        """Prefix lines beyond line 2 are ignored — they're narrative."""
        assert task._project_notes_prefix(
            "narrative line 1\nnarrative line 2\njira_action_status=applied\n"
        ) == (None, None)


class TestTaskFieldMutateHelper:
    """Exercise the shared helper directly so its shape is pinned."""

    def test_builds_correct_field_path_and_calls_gateway_once(self):
        with patch(
            "egg_agent_tools.handlers.task.gateway_request",
            return_value={"success": True, "data": {}},
        ) as gr:
            task._task_field_mutate(
                identifier=17,
                repo_path="/r",
                phase_idx=2,
                task_idx=4,
                field="commit",
                value="abcdef1",
                reason="test",
            )
        assert gr.call_count == 1
        data = gr.call_args.kwargs["data"]
        assert data["field_path"] == "phases.2.tasks.4.commit"
        assert data["new_value"] == "abcdef1"
        assert data["identifier"] == 17
        assert data["repo_path"] == "/r"
        assert data["actor"] == "egg"
        assert data["reason"] == "test"

    def test_gateway_failure_raises(self):
        with patch(
            "egg_agent_tools.handlers.task.gateway_request",
            return_value={"success": False, "message": "oops"},
        ):
            with pytest.raises(GatewayError):
                task._task_field_mutate(
                    identifier=1,
                    repo_path="/",
                    phase_idx=0,
                    task_idx=0,
                    field="notes",
                    value="x",
                    reason="r",
                )


class TestTaskMarkGap:
    """task_mark_gap fetches the contract first (to compute the gap index)
    then writes to phases.P.tasks.T.gaps.N via mutate."""

    @staticmethod
    def _contract_with_task(existing_gaps: int = 0):
        return {
            "phases": [
                {
                    "id": "phase-1",
                    "tasks": [
                        {
                            "id": "task-1-1",
                            "gaps": [
                                {
                                    "id": f"gap-{i}",
                                    "from_role": "tester",
                                    "to_role": "coder",
                                    "description": f"old-{i}",
                                    "created_at": "2026-01-01T00:00:00Z",
                                    "resolved": False,
                                }
                                for i in range(existing_gaps)
                            ],
                        }
                    ],
                }
            ]
        }

    def _id(self, value=42):
        return patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=value)

    def _role(self, value="tester"):
        return patch("egg_agent_tools.handlers.task.get_agent_role", return_value=value)

    def test_happy_path_appends_gap_to_empty_list(self):
        contract = self._contract_with_task(existing_gaps=0)
        responses = [
            {"success": True, "data": contract},  # read
            {"success": True, "data": {}},  # mutate
        ]
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            self._id(),
            self._role(),
        ):
            resp = task.task_mark_gap(
                {
                    "task": "task-1-1",
                    "description": "missing error-path test",
                }
            )
        assert resp["ok"] is True
        assert resp["task"] == "task-1-1"
        assert resp["gap_id"].startswith("gap-")
        mutate_call = gr.call_args_list[1].kwargs["data"]
        assert mutate_call["field_path"] == "phases.0.tasks.0.gaps.0"
        record = mutate_call["new_value"]
        assert record["description"] == "missing error-path test"
        assert record["from_role"] == "tester"
        assert record["to_role"] == "coder"  # default
        assert record["resolved"] is False
        # created_at is ISO-8601-ish.
        assert record["created_at"].endswith("Z")

    def test_custom_to_role_and_from_role(self):
        contract = self._contract_with_task()
        responses = [
            {"success": True, "data": contract},
            {"success": True, "data": {}},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            self._id(),
            self._role("tester"),
        ):
            task.task_mark_gap(
                {
                    "task": "task-1-1",
                    "description": "x",
                    "to_role": "documenter",
                    "from_role": "reviewer_code",
                }
            )
        record = gr.call_args_list[1].kwargs["data"]["new_value"]
        assert record["to_role"] == "documenter"
        assert record["from_role"] == "reviewer_code"

    def test_gap_id_monotonic_with_existing_gaps(self):
        """Reviewer NACK #5: gap_id is deterministic ``gap-<N>`` where
        N = max existing gap number + 1.  With two pre-existing gaps
        (gap-0, gap-1 in the fixture), the new id must be gap-2."""
        contract = self._contract_with_task(existing_gaps=2)
        responses = [
            {"success": True, "data": contract},
            {"success": True, "data": {}},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            self._id(),
            self._role("tester"),
        ):
            resp = task.task_mark_gap({"task": "task-1-1", "description": "new gap"})
        data = gr.call_args_list[1].kwargs["data"]
        assert data["field_path"] == "phases.0.tasks.0.gaps.2"  # appended
        # Fixture gap ids are gap-0/gap-1; _next_gap_id computes max+1.
        assert resp["gap_id"] == "gap-2"
        assert data["new_value"]["id"] == "gap-2"

    def test_gap_id_skips_non_numeric_suffix(self):
        """Legacy / hand-edited gaps with non-``gap-<int>`` ids must
        NOT confuse the numeric-suffix counter."""
        contract = {
            "phases": [
                {
                    "id": "phase-1",
                    "tasks": [
                        {
                            "id": "task-1-1",
                            "gaps": [
                                {"id": "custom-xyz", "description": "legacy"},
                                {"id": "gap-7", "description": "numbered"},
                            ],
                        }
                    ],
                }
            ]
        }
        responses = [
            {"success": True, "data": contract},
            {"success": True, "data": {}},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            self._id(),
            self._role("tester"),
        ):
            resp = task.task_mark_gap({"task": "task-1-1", "description": "x"})
        # max numeric suffix is 7 → next is gap-8.  The non-matching
        # 'custom-xyz' id is ignored by the regex.
        assert resp["gap_id"] == "gap-8"
        data = gr.call_args_list[1].kwargs["data"]
        # Appends at len(existing_gaps) == 2.
        assert data["field_path"] == "phases.0.tasks.0.gaps.2"

    def test_toctou_retry_on_index_conflict(self):
        """Reviewer NACK #5: a concurrent writer racing on the same
        gap index must trip the retry path.  Simulated by a first
        mutate failing with 'index out of range', then succeeding on
        the retry (after a fresh read picks up the new gap)."""
        first_contract = self._contract_with_task(existing_gaps=0)
        second_contract = self._contract_with_task(existing_gaps=1)
        responses = [
            {"success": True, "data": first_contract},  # attempt 1 read
            {"success": False, "message": "Array index 0 out of range"},
            {"success": True, "data": second_contract},  # attempt 2 read
            {"success": True, "data": {}},  # attempt 2 mutate succeeds
        ]
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            self._id(),
            self._role("tester"),
        ):
            resp = task.task_mark_gap({"task": "task-1-1", "description": "x"})
        # Four calls: read, mutate-fail, re-read, mutate-ok.
        assert gr.call_count == 4
        final_mutate = gr.call_args_list[3].kwargs["data"]
        assert final_mutate["field_path"] == "phases.0.tasks.0.gaps.1"
        # second_contract fixture has one existing gap with id gap-0,
        # so _next_gap_id (max numeric suffix + 1) returns gap-1.
        assert resp["gap_id"] == "gap-1"

    def test_toctou_non_retryable_error_bails_immediately(self):
        """A gateway error that does NOT look like a TOCTOU collision
        (e.g. auth denied) must NOT be retried — retry would mask a
        real problem."""
        contract = self._contract_with_task()
        responses = [
            {"success": True, "data": contract},
            {"success": False, "message": "role not authorized"},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            self._id(),
            self._role("tester"),
        ):
            with pytest.raises(GatewayError):
                task.task_mark_gap({"task": "task-1-1", "description": "x"})
        # Exactly two calls — no retry.
        assert gr.call_count == 2

    def test_missing_description_rejected(self):
        with pytest.raises(HandlerError):
            task.task_mark_gap({"task": "task-1-1"})

    def test_missing_task_rejected(self):
        with pytest.raises(HandlerError):
            task.task_mark_gap({"description": "x"})

    def test_empty_to_role_rejected(self):
        with self._id(), self._role("tester"):
            with pytest.raises(HandlerError):
                task.task_mark_gap({"task": "task-1-1", "description": "x", "to_role": ""})

    def test_missing_from_role_rejected(self):
        with self._id(), patch("egg_agent_tools.handlers.task.get_agent_role", return_value=None):
            with pytest.raises(HandlerError) as exc:
                task.task_mark_gap({"task": "task-1-1", "description": "x"})
        assert "Sender role" in str(exc.value)

    def test_empty_contract_task_not_found(self):
        responses = [{"success": True, "data": {"phases": []}}]
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ),
            self._id(),
            self._role("tester"),
        ):
            with pytest.raises(HandlerError) as exc:
                task.task_mark_gap({"task": "task-3-1", "description": "x"})
        assert "not found" in str(exc.value)

    def test_unknown_task_id_not_found(self):
        responses = [
            {
                "success": True,
                "data": {"phases": [{"id": "p1", "tasks": [{"id": "t1"}]}]},
            }
        ]
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ),
            self._id(),
            self._role("tester"),
        ):
            with pytest.raises(HandlerError) as exc:
                task.task_mark_gap({"task": "task-1-7", "description": "x"})
        assert "not found" in str(exc.value)

    def test_rescoped_slice_resolves_by_id_not_suffix(self):
        """#3527 regression: the gap write targets the task's actual
        list position, not the position implied by its numeric suffix."""
        contract = {
            "slices": [
                {
                    "id": "slice-1",
                    "tasks": [{"id": "task-1-5", "gaps": []}, {"id": "task-1-6", "gaps": []}],
                }
            ]
        }
        responses = [
            {"success": True, "data": contract},
            {"success": True, "data": {}},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            self._id(),
            self._role("tester"),
        ):
            resp = task.task_mark_gap({"task": "task-1-6", "description": "x"})
        assert resp["ok"] is True
        data = gr.call_args_list[1].kwargs["data"]
        assert data["field_path"] == "phases.0.tasks.1.gaps.0"

    def test_contract_fetch_failure_raises_gateway_error(self):
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                return_value={"success": False, "message": "denied"},
            ),
            self._id(),
            self._role("tester"),
        ):
            with pytest.raises(GatewayError):
                task.task_mark_gap({"task": "task-1-1", "description": "x"})

    def test_mutate_failure_raises_gateway_error(self):
        contract = self._contract_with_task()
        responses = [
            {"success": True, "data": contract},
            {"success": False, "message": "write refused"},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ),
            self._id(),
            self._role("tester"),
        ):
            with pytest.raises(GatewayError):
                task.task_mark_gap({"task": "task-1-1", "description": "x"})

    def test_docstring_mentions_no_cli_rationale(self):
        """decision-13: every cli_command=None handler must explain why
        in its docstring so the rule-doc drift gate (assertion C) is
        satisfied."""
        doc = task.task_mark_gap.__doc__ or ""
        lower = doc.lower()
        assert "no cli" in lower or "no-cli" in lower

    def test_resolves_real_serialized_contract(self):
        """Review feedback item C (#3029): cross the serialise→consume
        seam with the *real* contract codec.

        Mirrors ``test_resolves_real_serialized_contract`` for the
        ``_tasks_for_role`` path in #3029: the orchestrator serves
        contracts as ``Contract.model_dump`` → ``slices``, never
        ``phases``.  Pre-fix this handler read ``contract.get("phases")``
        and raised "Phase index out of range" for every mark-gap call
        against a modern contract.  Feed a legacy ``phases`` input
        through the codec so we also exercise the #2137 migration on the
        way in, then assert the gap is appended at the correct field
        path.  Tests that hand-key ``phases`` fixtures (every other test
        in this class) mask the bug — only the real codec catches it.
        """
        from egg_contracts.loader import export_contract  # local import: shared deps
        from egg_contracts.models import Contract

        served = export_contract(
            Contract.model_validate(
                {
                    "pipeline_id": "issue-3023",
                    "phases": [
                        {
                            "id": "phase-1",
                            "name": "code only",
                            "tasks": [
                                {
                                    "id": "task-1-1",
                                    "description": "implement the thing",
                                    "status": "pending",
                                    "acceptance_criteria": "it works",
                                    "files_affected": ["foo.py"],
                                }
                            ],
                        }
                    ],
                }
            ),
            include_audit_log=False,
        )
        assert "phases" not in served and "slices" in served

        responses = [
            {"success": True, "data": served},  # read returns the real shape
            {"success": True, "data": {}},  # mutate succeeds
        ]
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            self._id(),
            self._role("tester"),
        ):
            resp = task.task_mark_gap(
                {
                    "task": "task-1-1",
                    "description": "missing edge-case test",
                }
            )

        assert resp["ok"] is True
        assert resp["task"] == "task-1-1"
        # The mutate writes to ``phases.{...}`` regardless of read-side
        # key (``Contract.phases`` is a ``@property`` alias for
        # ``self.slices``; the orchestrator's mutate handler navigates
        # via ``hasattr``).
        mutate_call = gr.call_args_list[1].kwargs["data"]
        assert mutate_call["field_path"] == "phases.0.tasks.0.gaps.0"
