"""Audit events for ``_populate_contract_from_plan`` (issue #2134).

The populate step has six possible outcomes (success + five silent
early-returns + one outer catch-all).  Each outcome must emit a
structured log event with a stable name and a discriminator field so a
recurrence of the #1931 empty-phases incident can be diagnosed from
logs alone.

Verified outcomes:

* ``contract_phases_populated`` — happy path
* ``contract_phases_ingest_failed`` with ``reason``:
  - ``egg_contracts_unavailable``
  - ``no_draft_path``
  - ``plan_draft_missing``
  - ``contract_load_failed``
  - ``parse_failed``
  - ``empty_result`` — parse succeeded but yielded no phases / no PR
    metadata (the #1931 failure mode)
  - ``unexpected_exception`` with ``source="parse_save"`` (inner catch)
    or ``source="safe_wrapper"`` (outer ``_safe`` wrapper)

structlog output bypasses pytest's ``caplog`` fixture in this codebase
(see ``test_decisions_routes.py``), so we patch the module-level
``logger`` and inspect ``call_args_list`` directly.
"""

from __future__ import annotations

import sys
import textwrap
from unittest.mock import MagicMock, patch

import pytest

# Match the heavy-dependency mocking pattern from test_diagnostic_logging_1633.py
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)


SAMPLE_PLAN = textwrap.dedent("""\
    # Plan: Test plan

    ## Implementation

    ### Phase 1: Implement

    Body.

    ```yaml
    # yaml-tasks
    pr:
      title: "Test PR"
      description: "Test description"
    phases:
      - id: 1
        name: Implement
        goal: Test goal
        tasks:
          - id: TASK-1-1
            description: "Task one"
            acceptance: "Done"
            files:
              - src/x.py
    ```
""")


def _populate_calls(mock_logger):
    """Return all logger calls from the populate function (any level)."""
    return (
        mock_logger.info.call_args_list
        + mock_logger.warning.call_args_list
        + mock_logger.error.call_args_list
    )


def _ingest_failed_calls(mock_logger):
    """Return calls whose first positional arg is the failure event name."""
    return [
        c
        for c in mock_logger.warning.call_args_list
        if c.args and c.args[0] == "contract_phases_ingest_failed"
    ]


class TestSuccessEvent:
    """Happy path emits ``contract_phases_populated``."""

    def test_emits_populated_event_on_success(self, tmp_path):
        """A populated plan draft + contract produces the success event."""
        from egg_contracts.loader import create_contract
        from routes.pipelines import _populate_contract_from_plan

        pipeline_id = "pipeline-success"

        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{pipeline_id}-plan.md").write_text(SAMPLE_PLAN)

        with patch("routes.pipelines.logger") as mock_logger:
            _populate_contract_from_plan(tmp_path, pipeline_id, "local")

        success_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and c.args[0] == "contract_phases_populated"
        ]
        assert len(success_calls) == 1, (
            f"Expected one contract_phases_populated event, got {_populate_calls(mock_logger)}"
        )

        kwargs = success_calls[0].kwargs
        assert kwargs["pipeline_id"] == pipeline_id
        assert kwargs["phase_count"] == 1
        assert kwargs["task_count"] == 1
        assert kwargs["has_pr_metadata"] is True

        # No failure events should have been emitted.
        assert _ingest_failed_calls(mock_logger) == []


class TestFailureEvents:
    """Each silent early-return path emits a discriminated failure event."""

    def test_egg_contracts_unavailable(self, tmp_path):
        """ImportError on egg_contracts.loader emits the matching reason."""
        from routes.pipelines import _populate_contract_from_plan

        # Force ``from egg_contracts.loader import ...`` to raise ImportError
        # by setting the module to None in sys.modules.
        with (
            patch.dict(sys.modules, {"egg_contracts.loader": None}),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _populate_contract_from_plan(tmp_path, "pipeline-no-loader", "local")

        failures = _ingest_failed_calls(mock_logger)
        assert len(failures) == 1
        assert failures[0].kwargs["reason"] == "egg_contracts_unavailable"
        assert failures[0].kwargs["pipeline_id"] == "pipeline-no-loader"

    def test_no_draft_path(self, tmp_path):
        """``_get_draft_path`` returning None emits no_draft_path."""
        from routes.pipelines import _populate_contract_from_plan

        with (
            patch("routes.pipelines._get_draft_path", return_value=None),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _populate_contract_from_plan(tmp_path, "pipeline-no-path", "local")

        failures = _ingest_failed_calls(mock_logger)
        assert len(failures) == 1
        assert failures[0].kwargs["reason"] == "no_draft_path"
        assert failures[0].kwargs["pipeline_id"] == "pipeline-no-path"

    def test_plan_draft_missing(self, tmp_path):
        """Plan file not on disk emits plan_draft_missing with the path."""
        from routes.pipelines import _populate_contract_from_plan

        with patch("routes.pipelines.logger") as mock_logger:
            _populate_contract_from_plan(tmp_path, "pipeline-no-draft", "local")

        failures = _ingest_failed_calls(mock_logger)
        assert len(failures) == 1
        assert failures[0].kwargs["reason"] == "plan_draft_missing"
        assert failures[0].kwargs["pipeline_id"] == "pipeline-no-draft"
        assert "pipeline-no-draft-plan.md" in failures[0].kwargs["path"]

    def test_contract_load_failed(self, tmp_path):
        """``load_contract`` raising emits contract_load_failed with the error."""
        from routes.pipelines import _populate_contract_from_plan

        # Plan draft must exist so we get past the plan_draft_missing gate.
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / "pipeline-bad-contract-plan.md").write_text(SAMPLE_PLAN)

        with (
            patch(
                "egg_contracts.loader.load_contract",
                side_effect=RuntimeError("contract corrupt"),
            ),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _populate_contract_from_plan(tmp_path, "pipeline-bad-contract", "local")

        failures = _ingest_failed_calls(mock_logger)
        assert len(failures) == 1
        assert failures[0].kwargs["reason"] == "contract_load_failed"
        assert "contract corrupt" in failures[0].kwargs["error"]

    def test_parse_failed(self, tmp_path):
        """A plan whose yaml-tasks block doesn't parse emits parse_failed."""
        from egg_contracts.loader import create_contract
        from routes.pipelines import _populate_contract_from_plan

        pipeline_id = "pipeline-bad-parse"
        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)

        # Force parse_plan to return success=False.
        fake_result = MagicMock()
        fake_result.success = False
        fake_result.error = "yaml-tasks block missing required field 'phases'"

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{pipeline_id}-plan.md").write_text("# Plan with no yaml-tasks\n")

        with (
            patch("egg_contracts.plan_parser.parse_plan", return_value=fake_result),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _populate_contract_from_plan(tmp_path, pipeline_id, "local")

        failures = _ingest_failed_calls(mock_logger)
        assert len(failures) == 1
        assert failures[0].kwargs["reason"] == "parse_failed"
        assert "yaml-tasks" in failures[0].kwargs["error"]

    def test_unexpected_exception_inner_catch(self, tmp_path):
        """Anything raising inside the parse block is caught with unexpected_exception."""
        from egg_contracts.loader import create_contract
        from routes.pipelines import _populate_contract_from_plan

        pipeline_id = "pipeline-explode"
        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{pipeline_id}-plan.md").write_text(SAMPLE_PLAN)

        with (
            patch(
                "egg_contracts.plan_parser.parse_plan",
                side_effect=RuntimeError("parser exploded"),
            ),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _populate_contract_from_plan(tmp_path, pipeline_id, "local")

        failures = _ingest_failed_calls(mock_logger)
        assert len(failures) == 1
        assert failures[0].kwargs["reason"] == "unexpected_exception"
        # source distinguishes inner catch from the outer _safe wrapper.
        assert failures[0].kwargs["source"] == "parse_save"
        assert "parser exploded" in failures[0].kwargs["error"]
        # Tracebacks must be preserved for unexpected exceptions.
        assert failures[0].kwargs.get("exc_info") is True

    def test_empty_result_emits_discriminator(self, tmp_path):
        """Parse success with no phases and no PR metadata emits empty_result.

        This is the #1931 failure mode: ``parse_plan`` returns
        ``success=True`` but ``to_contract_phases()`` yields ``[]`` and
        ``pr_title`` is None — the contract stays empty.  Without this
        event the gap is invisible to operators.
        """
        from egg_contracts.loader import create_contract
        from routes.pipelines import _populate_contract_from_plan

        pipeline_id = "pipeline-empty-parse"
        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{pipeline_id}-plan.md").write_text("# Plan\n")

        # Force parse_plan to return success with no phases or PR metadata.
        fake_result = MagicMock()
        fake_result.success = True
        fake_result.warnings = []
        fake_result.to_contract_slices.return_value = []
        fake_result.pr_title = None
        fake_result.pr_description = None
        fake_result.pr_test_plan = None
        fake_result.pr_manual_steps = None

        with (
            patch("egg_contracts.plan_parser.parse_plan", return_value=fake_result),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _populate_contract_from_plan(tmp_path, pipeline_id, "local")

        failures = _ingest_failed_calls(mock_logger)
        assert len(failures) == 1
        assert failures[0].kwargs["reason"] == "empty_result"
        assert failures[0].kwargs["pipeline_id"] == pipeline_id
        # Success event must NOT be emitted in this case.
        success_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and c.args[0] == "contract_phases_populated"
        ]
        assert success_calls == []


class TestSafeWrapper:
    """``_populate_contract_from_plan_safe`` is the outer backstop."""

    def test_safe_wrapper_emits_unexpected_exception_on_inner_raise(self, tmp_path):
        """If the inner function raises, the wrapper emits unexpected_exception."""
        from routes.pipelines import _populate_contract_from_plan_safe

        with (
            patch(
                "routes.pipelines._populate_contract_from_plan",
                side_effect=RuntimeError("inner blew up"),
            ),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _populate_contract_from_plan_safe(tmp_path, "pipeline-wrap", "local")

        failures = _ingest_failed_calls(mock_logger)
        assert len(failures) == 1
        assert failures[0].kwargs["reason"] == "unexpected_exception"
        # source distinguishes the outer wrapper from the inner catch.
        assert failures[0].kwargs["source"] == "safe_wrapper"
        assert failures[0].kwargs["pipeline_id"] == "pipeline-wrap"
        assert "inner blew up" in failures[0].kwargs["error"]
        # Tracebacks must be preserved for unexpected exceptions.
        assert failures[0].kwargs.get("exc_info") is True

    def test_safe_wrapper_does_not_propagate(self, tmp_path):
        """Inner failures must not escape — HITL gate must remain reachable (#1890)."""
        from routes.pipelines import _populate_contract_from_plan_safe

        with patch(
            "routes.pipelines._populate_contract_from_plan",
            side_effect=RuntimeError("boom"),
        ):
            # No assertion — just must not raise.
            _populate_contract_from_plan_safe(tmp_path, "pipeline-quiet", "local")


class TestNaturalSourceLoudFail:
    """#2337: source="plan_complete" raises when local-missing + origin-has draft."""

    def test_plan_complete_raises_when_origin_has_draft(self, tmp_path):
        """Local missing the draft, origin has it → PlanDraftMissingOnLocalError."""
        from routes.pipelines import (
            PlanDraftMissingOnLocalError,
            _populate_contract_from_plan_safe,
        )

        # No local draft file in tmp_path/.egg-state/drafts/
        with (
            patch("routes.pipelines._origin_has_plan_draft", return_value=True) as mock_origin,
            patch("routes.pipelines._populate_contract_from_plan") as mock_inner,
            pytest.raises(PlanDraftMissingOnLocalError),
        ):
            _populate_contract_from_plan_safe(
                tmp_path,
                "pipeline-2337",
                "issue",
                issue_number=2261,
                source="plan_complete",
                branch="egg/issue-2261",
            )
        # Origin probe ran with the right args
        mock_origin.assert_called_once()
        # Inner populator never ran — we short-circuited
        mock_inner.assert_not_called()

    def test_plan_complete_swallows_when_origin_does_not_have_draft(self, tmp_path):
        """Local missing AND origin missing → fall through to inner (warn-and-return)."""
        from routes.pipelines import _populate_contract_from_plan_safe

        with (
            patch("routes.pipelines._origin_has_plan_draft", return_value=False),
            patch("routes.pipelines._populate_contract_from_plan") as mock_inner,
        ):
            # Should not raise.
            _populate_contract_from_plan_safe(
                tmp_path,
                "pipeline-2337-no-origin",
                "issue",
                issue_number=999,
                source="plan_complete",
                branch="egg/issue-999",
            )
            # Inner ran (which itself logs plan_draft_missing and returns).
            mock_inner.assert_called_once()

    def test_advance_phase_force_swallows_even_when_origin_has_draft(self, tmp_path):
        """Force-advance source keeps the swallow-everything contract from #1941."""
        from routes.pipelines import _populate_contract_from_plan_safe

        with (
            patch("routes.pipelines._origin_has_plan_draft", return_value=True) as mock_origin,
            patch("routes.pipelines._populate_contract_from_plan") as mock_inner,
        ):
            # Must not raise even though origin has the draft — force-advance
            # is the recovery hammer, blocking it would defeat the purpose.
            _populate_contract_from_plan_safe(
                tmp_path,
                "pipeline-2337-force",
                "issue",
                issue_number=2261,
                source="advance_phase_force",
                branch="egg/issue-2261",
            )
            # Origin probe is skipped entirely under force-advance.
            mock_origin.assert_not_called()
            mock_inner.assert_called_once()

    def test_plan_complete_without_branch_skips_origin_probe(self, tmp_path):
        """source="plan_complete" but branch=None → skip probe, fall through."""
        from routes.pipelines import _populate_contract_from_plan_safe

        with (
            patch("routes.pipelines._origin_has_plan_draft", return_value=True) as mock_origin,
            patch("routes.pipelines._populate_contract_from_plan") as mock_inner,
        ):
            _populate_contract_from_plan_safe(
                tmp_path,
                "pipeline-2337-nobranch",
                "issue",
                issue_number=42,
                source="plan_complete",
                branch=None,
            )
            mock_origin.assert_not_called()
            mock_inner.assert_called_once()


class TestRegressionEmptyPhases:
    """Direct regression for the #1931 incident referenced by #2134.

    A pipeline whose plan has a known-good yaml-tasks block, fed
    through the same wrapper used at the post-plan persist step, must
    leave ``contract.phases`` populated.
    """

    def test_known_good_plan_populates_phases(self, tmp_path):
        from egg_contracts.loader import create_contract, load_contract
        from routes.pipelines import _populate_contract_from_plan_safe

        pipeline_id = "pipeline-1931-regression"
        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{pipeline_id}-plan.md").write_text(SAMPLE_PLAN)

        _populate_contract_from_plan_safe(tmp_path, pipeline_id, "local")

        contract = load_contract(pipeline_id, tmp_path)
        assert len(contract.phases) == 1, (
            "Contract phases must not be empty after populate when plan "
            "yaml-tasks parses cleanly — see issue #2134 / #1931."
        )
        assert len(contract.phases[0].tasks) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
