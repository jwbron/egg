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

    def test_plan_complete_raises_when_draft_missing_on_local_and_origin(self, tmp_path):
        """#2627: local AND origin missing → PlanDraftMissingOnLocalAndOriginError (no silent advance)."""
        from routes.pipelines import (
            PlanDraftMissingOnLocalAndOriginError,
            _populate_contract_from_plan_safe,
        )

        with (
            patch("routes.pipelines._origin_has_plan_draft", return_value=False) as mock_origin,
            patch("routes.pipelines._populate_contract_from_plan") as mock_inner,
            pytest.raises(PlanDraftMissingOnLocalAndOriginError),
        ):
            _populate_contract_from_plan_safe(
                tmp_path,
                "pipeline-2627",
                "issue",
                issue_number=999,
                source="plan_complete",
                branch="egg/issue-999",
            )
        # Origin probe ran with the right args
        mock_origin.assert_called_once()
        # Inner populator never ran — we short-circuited
        mock_inner.assert_not_called()

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


class TestEmptyContractFailureMetadata:
    """#2627 review: the ``_run_pipeline`` failure handler picks a
    ``(teardown_reason, log_event)`` pair based on which fail-loud
    exception (``PlanDraftMissing*`` or
    :class:`PopulateProducedEmptyContractError`) was raised.  Extracted
    into a helper so a typo that swapped the branches can be caught by
    a unit test rather than slipping through the populator-helper tests
    above (which only assert that the right exception type is raised,
    not that the call site dispatches on it correctly).

    Helper rename — was ``_plan_draft_missing_failure_metadata`` —
    because the dispatch now also handles
    :class:`PopulateProducedEmptyContractError`, which is not a
    "draft-missing" failure (the draft exists; it just produced
    nothing).  ``_empty_contract_failure_metadata`` parallels the rest
    of the #2627 surface (#2627 review).
    """

    def test_local_only_error_maps_to_local_strings(self):
        from routes.pipelines import (
            PlanDraftMissingOnLocalError,
            _empty_contract_failure_metadata,
        )

        reason, message = _empty_contract_failure_metadata(
            PlanDraftMissingOnLocalError("draft x missing")
        )
        assert reason == "plan draft missing on local"
        # #2627 review: use the OVERSEER_ALERT event-name convention so
        # operators' log filters surface this path alongside the slice-gate
        # and start_phase safety-net.  The event name matches the
        # wrapper-side pre-raise OVERSEER_ALERT so both legs share one
        # discriminator.
        assert message == "OVERSEER_ALERT plan_draft_missing_on_local_but_present_on_origin"

    def test_local_and_origin_error_maps_to_both_strings(self):
        from routes.pipelines import (
            PlanDraftMissingOnLocalAndOriginError,
            _empty_contract_failure_metadata,
        )

        reason, message = _empty_contract_failure_metadata(
            PlanDraftMissingOnLocalAndOriginError("draft x missing on both")
        )
        assert reason == "plan draft missing on local and origin"
        assert message == "OVERSEER_ALERT plan_draft_missing_on_local_and_origin"

    def test_local_and_local_and_origin_produce_distinct_metadata(self):
        """A swap between the two branches would make both classes return
        the same metadata — assert the helper distinguishes them."""
        from routes.pipelines import (
            PlanDraftMissingOnLocalAndOriginError,
            PlanDraftMissingOnLocalError,
            _empty_contract_failure_metadata,
        )

        local_meta = _empty_contract_failure_metadata(PlanDraftMissingOnLocalError("local"))
        both_meta = _empty_contract_failure_metadata(PlanDraftMissingOnLocalAndOriginError("both"))
        assert local_meta != both_meta
        # And the two reason strings differ at the operator-visible
        # phrase (teardown reason is the field the overseer sees).
        assert local_meta[0] != both_meta[0]

    def test_populate_produced_empty_contract_error_maps_to_outcome_strings(self):
        """#2627 follow-up: the helper also dispatches on
        :class:`PopulateProducedEmptyContractError` so the orthogonal
        "draft existed but populate yielded nothing" failure mode shares
        the same FAILED-cleanup handler as the draft-missing variants.
        """
        from routes.pipelines import (
            PopulateOutcome,
            PopulateProducedEmptyContractError,
            _empty_contract_failure_metadata,
        )

        reason, message = _empty_contract_failure_metadata(
            PopulateProducedEmptyContractError(PopulateOutcome.EMPTY_RESULT)
        )
        assert reason == "populate produced empty_result outcome"
        # OVERSEER_ALERT-prefixed event name; discriminator stays on the
        # ``reason`` (teardown_reason) string and the structured ``error``
        # kwarg the call site passes to the logger.
        assert message == "OVERSEER_ALERT plan_populate_produced_empty_contract"

        # parse_failed routes through the same branch — the helper
        # keys on the outcome, not the exception class, so a future
        # PopulateOutcome value doesn't need a new branch.
        reason2, message2 = _empty_contract_failure_metadata(
            PopulateProducedEmptyContractError(PopulateOutcome.PARSE_FAILED)
        )
        assert "parse_failed" in reason2
        assert message2 == "OVERSEER_ALERT plan_populate_produced_empty_contract"

    def test_all_three_log_messages_use_overseer_alert_prefix(self):
        """#2627 review: every plan-complete fail-loud branch must use
        the ``OVERSEER_ALERT`` prefix so log filters surface this path
        alongside the slice-gate and start_phase safety-net.  Asymmetry
        between the three legs would hide whichever leg is missing the
        prefix from the operator's monitoring."""
        from routes.pipelines import (
            PlanDraftMissingOnLocalAndOriginError,
            PlanDraftMissingOnLocalError,
            PopulateOutcome,
            PopulateProducedEmptyContractError,
            _empty_contract_failure_metadata,
        )

        for err in (
            PlanDraftMissingOnLocalError("x"),
            PlanDraftMissingOnLocalAndOriginError("x"),
            PopulateProducedEmptyContractError(PopulateOutcome.EMPTY_RESULT),
            PopulateProducedEmptyContractError(PopulateOutcome.PARSE_FAILED),
        ):
            _, message = _empty_contract_failure_metadata(err)
            assert message.startswith("OVERSEER_ALERT "), (
                f"log_event for {type(err).__name__} missing OVERSEER_ALERT prefix: {message!r}"
            )

    def test_populate_produced_empty_contract_error_handles_populated_outcome(self):
        """#2627 review: ``POPULATED`` with ``slice_count == 0`` is a
        valid failure mode for this exception — the populator returned
        "changed" (PR metadata populated or current_phase advanced)
        but produced no slices/tasks.  The exception message must not
        say "produced populated outcome" (which reads as a contradiction
        when the contract is actually empty)."""
        from routes.pipelines import (
            PopulateOutcome,
            PopulateProducedEmptyContractError,
        )

        err = PopulateProducedEmptyContractError(PopulateOutcome.POPULATED, slice_count=0)
        assert err.outcome == PopulateOutcome.POPULATED
        assert err.slice_count == 0
        # Message must name the actual divergence rather than "populated".
        message = str(err)
        assert "0 slices/tasks" in message
        assert "produced populated outcome" not in message


class TestOriginHasPlanDraft:
    """#2337: ``_origin_has_plan_draft`` probes ``origin/{branch}:{path}``
    via ``git cat-file -e`` to decide whether the silent-failure mode
    (local missing the draft, origin has it) is in play.  All other
    callers mock the helper; a subprocess-arg regression — swapping
    ``cat-file -e`` for ``rev-parse``, dropping ``origin/``, mis-quoting
    the path — would slip past unit tests entirely.

    These tests directly exercise the helper and assert on the exact
    argv passed to ``subprocess.run`` so any of those regressions trip a
    test failure even though the sandbox blocks ``git init`` (preventing
    a real local-clone fixture).
    """

    @staticmethod
    def _make_subprocess_result(returncode: int = 0):
        import subprocess as _sp

        return _sp.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")

    def test_uses_cat_file_e_with_origin_branch_path(self, tmp_path):
        """argv MUST contain ``cat-file -e origin/{branch}:{draft_rel}``.

        Catches: swapping ``cat-file`` for ``rev-parse`` (different
        semantics), dropping ``-e`` (would print blob contents instead
        of just exit 0/1), dropping the ``origin/`` prefix (would
        resolve against local refs), or mis-quoting the path.
        """
        from routes.pipelines import _origin_has_plan_draft

        captured: list[list[str]] = []

        def _record(argv, *args, **kwargs):
            captured.append(list(argv))
            return self._make_subprocess_result(returncode=0)

        with patch("routes.pipelines.subprocess.run", side_effect=_record):
            assert (
                _origin_has_plan_draft(
                    tmp_path,
                    "egg/issue-2337",
                    ".egg-state/drafts/pipeline-2337-plan.md",
                )
                is True
            )

        assert len(captured) == 1, f"expected one subprocess call, got {captured}"
        argv = captured[0]
        # Ordered structural assertions — each catches a distinct regression.
        assert "cat-file" in argv, "must use cat-file (not rev-parse) — see helper docstring"
        assert "-e" in argv, "must use -e flag (existence-check, not blob-dump)"
        assert "origin/egg/issue-2337:.egg-state/drafts/pipeline-2337-plan.md" in argv, (
            "object spec must be exactly origin/{branch}:{path} (no leading-slash, no escapes)"
        )
        # cat-file must come before -e which must come before the object spec
        assert (
            argv.index("cat-file")
            < argv.index("-e")
            < argv.index("origin/egg/issue-2337:.egg-state/drafts/pipeline-2337-plan.md")
        ), "argv order regressed"
        # -C {repo_path} threads the working directory through.
        assert "-C" in argv
        assert str(tmp_path) in argv

    def test_returns_true_on_zero_exit(self, tmp_path):
        """returncode 0 from cat-file -e → helper returns True."""
        from routes.pipelines import _origin_has_plan_draft

        with patch(
            "routes.pipelines.subprocess.run",
            return_value=self._make_subprocess_result(returncode=0),
        ):
            assert _origin_has_plan_draft(tmp_path, "egg/x", "draft.md") is True

    def test_returns_false_on_nonzero_exit(self, tmp_path):
        """returncode non-zero (object missing) → helper returns False."""
        from routes.pipelines import _origin_has_plan_draft

        with patch(
            "routes.pipelines.subprocess.run",
            return_value=self._make_subprocess_result(returncode=1),
        ):
            assert _origin_has_plan_draft(tmp_path, "egg/x", "draft.md") is False

    def test_swallows_subprocess_exception(self, tmp_path):
        """Subprocess exception → helper returns False rather than raising.

        The contract is that callers can treat False as "couldn't
        confirm origin has it" without distinguishing exception from
        non-zero exit.
        """
        import subprocess as _sp

        from routes.pipelines import _origin_has_plan_draft

        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=_sp.TimeoutExpired(cmd="git", timeout=10),
        ):
            assert _origin_has_plan_draft(tmp_path, "egg/x", "draft.md") is False

    def test_passes_repo_path_via_minus_C(self, tmp_path):
        """The repo path threads through as ``git -C {repo_path}``.

        Catches accidentally running cat-file in cwd or the orchestrator
        repo instead of the worktree.
        """
        from routes.pipelines import _origin_has_plan_draft

        captured: list[list[str]] = []

        def _record(argv, *args, **kwargs):
            captured.append(list(argv))
            return self._make_subprocess_result(returncode=0)

        repo = tmp_path / "some" / "nested" / "worktree"
        repo.mkdir(parents=True)
        with patch("routes.pipelines.subprocess.run", side_effect=_record):
            _origin_has_plan_draft(repo, "egg/x", "draft.md")

        assert "-C" in captured[0]
        c_idx = captured[0].index("-C")
        # The argument right after -C must be the repo path
        assert captured[0][c_idx + 1] == str(repo), "git -C must point at the worktree, not cwd"


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


class TestPopulateResultReturnValue:
    """#2627 follow-up: ``_populate_contract_from_plan`` returns a
    :class:`PopulateResult` whose ``outcome`` discriminates success
    from each silent-failure mode so callers can fail-fast on an
    empty contract instead of advancing to implement with nothing
    to do.
    """

    def test_returns_populated_outcome_on_success(self, tmp_path):
        from egg_contracts.loader import create_contract
        from routes.pipelines import (
            PopulateOutcome,
            PopulateResult,
            _populate_contract_from_plan,
        )

        pipeline_id = "pipeline-result-success"
        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{pipeline_id}-plan.md").write_text(SAMPLE_PLAN)

        result = _populate_contract_from_plan(tmp_path, pipeline_id, "local")
        assert isinstance(result, PopulateResult)
        assert result.outcome == PopulateOutcome.POPULATED
        assert result.slice_count == 1
        assert result.task_count == 1

    def test_returns_egg_contracts_unavailable_outcome(self, tmp_path):
        from routes.pipelines import PopulateOutcome, _populate_contract_from_plan

        with patch.dict(sys.modules, {"egg_contracts.loader": None}):
            result = _populate_contract_from_plan(tmp_path, "pipeline-no-loader", "local")
        assert result.outcome == PopulateOutcome.EGG_CONTRACTS_UNAVAILABLE
        assert result.slice_count == 0
        assert result.task_count == 0

    def test_returns_no_draft_path_outcome(self, tmp_path):
        from routes.pipelines import PopulateOutcome, _populate_contract_from_plan

        with patch("routes.pipelines._get_draft_path", return_value=None):
            result = _populate_contract_from_plan(tmp_path, "pipeline-no-path", "local")
        assert result.outcome == PopulateOutcome.NO_DRAFT_PATH

    def test_returns_draft_missing_outcome(self, tmp_path):
        from routes.pipelines import PopulateOutcome, _populate_contract_from_plan

        result = _populate_contract_from_plan(tmp_path, "pipeline-no-draft", "local")
        assert result.outcome == PopulateOutcome.DRAFT_MISSING

    def test_returns_contract_load_failed_outcome(self, tmp_path):
        from routes.pipelines import PopulateOutcome, _populate_contract_from_plan

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / "pipeline-bad-contract-plan.md").write_text(SAMPLE_PLAN)

        with patch(
            "egg_contracts.loader.load_contract",
            side_effect=RuntimeError("contract corrupt"),
        ):
            result = _populate_contract_from_plan(tmp_path, "pipeline-bad-contract", "local")
        assert result.outcome == PopulateOutcome.CONTRACT_LOAD_FAILED

    def test_returns_parse_failed_outcome(self, tmp_path):
        from egg_contracts.loader import create_contract
        from routes.pipelines import PopulateOutcome, _populate_contract_from_plan

        pipeline_id = "pipeline-bad-parse-result"
        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{pipeline_id}-plan.md").write_text("# Plan\n")

        fake_result = MagicMock()
        fake_result.success = False
        fake_result.error = "yaml-tasks missing"

        with patch("egg_contracts.plan_parser.parse_plan", return_value=fake_result):
            result = _populate_contract_from_plan(tmp_path, pipeline_id, "local")
        assert result.outcome == PopulateOutcome.PARSE_FAILED

    def test_returns_empty_result_outcome(self, tmp_path):
        from egg_contracts.loader import create_contract
        from routes.pipelines import PopulateOutcome, _populate_contract_from_plan

        pipeline_id = "pipeline-empty-result"
        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{pipeline_id}-plan.md").write_text("# Plan\n")

        fake_result = MagicMock()
        fake_result.success = True
        fake_result.warnings = []
        fake_result.to_contract_slices.return_value = []
        fake_result.pr_title = None

        with patch("egg_contracts.plan_parser.parse_plan", return_value=fake_result):
            result = _populate_contract_from_plan(tmp_path, pipeline_id, "local")
        assert result.outcome == PopulateOutcome.EMPTY_RESULT

    def test_returns_unexpected_exception_outcome(self, tmp_path):
        from egg_contracts.loader import create_contract
        from routes.pipelines import PopulateOutcome, _populate_contract_from_plan

        pipeline_id = "pipeline-exc-result"
        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{pipeline_id}-plan.md").write_text(SAMPLE_PLAN)

        with patch(
            "egg_contracts.plan_parser.parse_plan",
            side_effect=RuntimeError("kaboom"),
        ):
            result = _populate_contract_from_plan(tmp_path, pipeline_id, "local")
        assert result.outcome == PopulateOutcome.UNEXPECTED_EXCEPTION


class TestSafeWrapperReturnValue:
    """The safe wrapper forwards the inner ``PopulateResult`` and
    translates raises into structured outcomes.  Force-advance and
    HITL plan-gate approval call sites rely on this to log without
    raising (#1941)."""

    def test_safe_wrapper_returns_inner_populated_result(self, tmp_path):
        from routes.pipelines import (
            PopulateOutcome,
            PopulateResult,
            _populate_contract_from_plan_safe,
        )

        sentinel = PopulateResult(PopulateOutcome.POPULATED, slice_count=3, task_count=7)
        with patch(
            "routes.pipelines._populate_contract_from_plan",
            return_value=sentinel,
        ):
            result = _populate_contract_from_plan_safe(tmp_path, "pipeline-wrap-ok", "local")
        assert result == sentinel

    def test_safe_wrapper_returns_forest_violation_after_catching_raise(self, tmp_path):
        from routes.pipelines import (
            ForestValidationError,
            PopulateOutcome,
            _populate_contract_from_plan_safe,
        )

        with patch(
            "routes.pipelines._populate_contract_from_plan",
            side_effect=ForestValidationError("nope", errors=["bad"]),
        ):
            result = _populate_contract_from_plan_safe(tmp_path, "pipeline-wrap-forest", "local")
        assert result.outcome == PopulateOutcome.FOREST_VIOLATION

    def test_safe_wrapper_returns_unexpected_exception_outcome(self, tmp_path):
        from routes.pipelines import PopulateOutcome, _populate_contract_from_plan_safe

        with patch(
            "routes.pipelines._populate_contract_from_plan",
            side_effect=RuntimeError("inner blew up"),
        ):
            result = _populate_contract_from_plan_safe(tmp_path, "pipeline-wrap-exc", "local")
        assert result.outcome == PopulateOutcome.UNEXPECTED_EXCEPTION


class TestPopulateResultIsEmptyContract:
    """#2627 review: the call-site empty-contract check is extracted into
    :func:`_populate_result_is_empty_contract` so the two callers (the
    natural plan-complete handler and the ``start_phase=implement``
    safety net) share a single source of truth.  The prior PR enumerated
    four outcomes; this widens to "every non-success outcome plus the
    POPULATED-with-no-slices case" so the silent-advance gap the
    reviewer flagged is closed at both call sites.
    """

    def test_populated_with_slices_is_not_empty(self):
        from routes.pipelines import (
            PopulateOutcome,
            PopulateResult,
            _populate_result_is_empty_contract,
        )

        result = PopulateResult(PopulateOutcome.POPULATED, slice_count=3, task_count=7)
        assert _populate_result_is_empty_contract(result) is False

    def test_populated_with_zero_slices_is_empty(self):
        """#2627 review: the populator returns POPULATED whenever *any* of
        contract.slices / PR metadata / current_phase advanced, so a plan
        with PR metadata but no slices/tasks lands as POPULATED with
        slice_count=0.  Prior to this PR the call-site outcome-only check
        passed it through and the implement phase spawned with no work."""
        from routes.pipelines import (
            PopulateOutcome,
            PopulateResult,
            _populate_result_is_empty_contract,
        )

        result = PopulateResult(PopulateOutcome.POPULATED, slice_count=0, task_count=0)
        assert _populate_result_is_empty_contract(result) is True

    def test_every_non_populated_outcome_is_empty(self):
        """#2627 review: prior to widening, only EMPTY_RESULT / PARSE_FAILED
        / DRAFT_MISSING / NO_DRAFT_PATH were caught.  CONTRACT_LOAD_FAILED,
        EGG_CONTRACTS_UNAVAILABLE, FOREST_VIOLATION, and UNEXPECTED_EXCEPTION
        silently passed the check.  Assert all of them now route to the
        fail-fast branch."""
        from routes.pipelines import (
            PopulateOutcome,
            PopulateResult,
            _populate_result_is_empty_contract,
        )

        for outcome in PopulateOutcome:
            if outcome == PopulateOutcome.POPULATED:
                continue
            result = PopulateResult(outcome, slice_count=0, task_count=0)
            assert _populate_result_is_empty_contract(result) is True, (
                f"outcome={outcome.value} must be treated as empty-contract"
            )

    def test_helper_covers_all_outcome_values(self):
        """Defensive: future PopulateOutcome values must be handled by
        explicit consideration — assert every existing value flows through
        the helper without TypeError or unexpected truthy/falsy result.
        Catches a regression where the helper's outcome comparison was
        accidentally narrowed to a subset."""
        from routes.pipelines import (
            PopulateOutcome,
            PopulateResult,
            _populate_result_is_empty_contract,
        )

        for outcome in PopulateOutcome:
            result = PopulateResult(outcome, slice_count=2, task_count=4)
            value = _populate_result_is_empty_contract(result)
            assert isinstance(value, bool)
            # POPULATED with slices=2 must NOT be flagged empty; every
            # other outcome with slices=2 still must (the outcome itself
            # is the failure signal).
            if outcome == PopulateOutcome.POPULATED:
                assert value is False
            else:
                assert value is True


class TestPlanCompleteEmptyContractRaisesAfterPopulate:
    """#2627 follow-up: source="plan_complete" plus populate-yielded-empty
    triggers a :class:`PopulateProducedEmptyContractError` at the
    natural call site so the FAILED-cleanup handler that already
    catches :class:`PlanDraftMissingError` covers the orthogonal
    "draft existed but produced zero tasks" failure mode too.

    The wrapper itself doesn't raise this; the
    ``_run_pipeline`` call site translates the non-raising
    ``EMPTY_RESULT``/``PARSE_FAILED`` outcome into the new
    exception so a single ``except`` clause handles all
    empty-contract paths.
    """

    def test_exception_class_is_runtime_error_subclass(self):
        from routes.pipelines import (
            PopulateOutcome,
            PopulateProducedEmptyContractError,
        )

        err = PopulateProducedEmptyContractError(PopulateOutcome.EMPTY_RESULT)
        assert isinstance(err, RuntimeError)
        assert err.outcome == PopulateOutcome.EMPTY_RESULT
        assert "empty_result" in str(err)


class TestEmptyContractHitl:
    """#2627 follow-up: the dedicated HITL emitted from the slice-gate,
    safety-net, and plan-complete paths names the empty-contract root
    cause inline and offers recovery options that won't loop the
    pipeline back into the same broken state."""

    def test_question_text_lists_three_distinct_recovery_options(self):
        from routes.pipelines import (
            _EMPTY_CONTRACT_HITL_OPTIONS,
            _empty_contract_hitl_question,
        )

        # Plain ``Retry phase`` against the generic HITL respawns into
        # the same empty-contract state — the operator-incident loop
        # documented on #2627.  These options must be distinct from the
        # generic Retry/Accept/Abort set so the SDLC skill renders them
        # as a fresh decision, not a collapsed duplicate.
        assert "Repopulate contract from plan draft and retry" in _EMPTY_CONTRACT_HITL_OPTIONS
        assert "Restart plan phase" in _EMPTY_CONTRACT_HITL_OPTIONS
        assert "Abort pipeline" in _EMPTY_CONTRACT_HITL_OPTIONS
        # Disjoint from the generic phase-failure options to ensure
        # operators see the dedicated HITL rather than a dedup.
        assert "Retry phase" not in _EMPTY_CONTRACT_HITL_OPTIONS
        assert "Accept current state" not in _EMPTY_CONTRACT_HITL_OPTIONS

        # The question text must name the root cause and the gate that
        # detected the divergence so operators don't have to dig.
        question = _empty_contract_hitl_question(
            pipeline_id="p-question",
            reason="empty_result",
            draft_slice_count=3,
            gate="plan_complete",
        )
        assert "3 slices" in question
        assert "plan_complete" in question
        assert "empty_result" in question

    def test_question_text_handles_missing_slice_count(self):
        from routes.pipelines import _empty_contract_hitl_question

        # When the draft is missing entirely, the gate has no parsed
        # slice count to quote; the question should still describe the
        # divergence without crashing or claiming a fake count.
        question = _empty_contract_hitl_question(
            pipeline_id="p-missing",
            reason="plan_draft_missing",
            draft_slice_count=None,
            gate="plan_complete",
        )
        assert "missing" in question or "unparseable" in question
        assert "plan_draft_missing" in question

    def test_question_text_interpolates_pipeline_id_into_recovery_url(self):
        """#2627 review: the recovery URL must show the actual pipeline id,
        not a literal ``{id}`` placeholder, so operators can copy the
        ``POST /pipelines/<id>/phase/populate-contract`` URL verbatim."""
        from routes.pipelines import _empty_contract_hitl_question

        question = _empty_contract_hitl_question(
            pipeline_id="p-interpolation-test",
            reason="empty_result",
            draft_slice_count=None,
            gate="plan_complete",
        )
        # Real id present, literal placeholder absent.
        assert "POST /pipelines/p-interpolation-test/phase/populate-contract" in question
        assert "{id}" not in question
        assert "/pipelines/{" not in question

    def test_question_text_uses_pipeline_blocked_phrasing(self):
        """#2627 review: the opening phrase must be "Pipeline blocked at
        {gate}" (not "Implement-phase blocked at {gate}").  At
        ``gate=plan_complete`` the implement phase has not yet been
        spawned, so the implement-specific phrasing reads oddly against
        ``pipeline.error`` and the phase-execution status which both
        still say "plan" at that point."""
        from routes.pipelines import _empty_contract_hitl_question

        for gate in ("plan_complete", "slice_gate", "start_phase_implement_safety_net"):
            question = _empty_contract_hitl_question(
                pipeline_id="p-phrasing",
                reason="empty_result",
                draft_slice_count=None,
                gate=gate,
            )
            assert f"Pipeline blocked at {gate}" in question, (
                f"question for {gate} must use 'Pipeline blocked at' phrasing, got: {question!r}"
            )
            assert "Implement-phase blocked at" not in question, (
                "implement-specific phrasing should not appear — "
                "plan_complete fires before implement spawns"
            )


class TestEmptyContractHitlReason:
    """#2627 review: the plan-complete call-site's HITL-reason dispatch
    is extracted into :func:`_empty_contract_hitl_reason` so a typo
    that narrows the dispatch back (e.g. dropping the
    ``POPULATED with slice_count==0 → populated_but_empty_slices``
    branch and letting it fall through to ``err.outcome.value``) is
    caught by a unit test rather than slipping past the
    populator-helper tests.
    """

    def test_local_only_error_maps_to_plan_draft_missing_on_local(self):
        from routes.pipelines import (
            PlanDraftMissingOnLocalError,
            _empty_contract_hitl_reason,
        )

        reason = _empty_contract_hitl_reason(PlanDraftMissingOnLocalError("draft x"))
        assert reason == "plan_draft_missing_on_local"

    def test_local_and_origin_error_maps_to_local_and_origin(self):
        from routes.pipelines import (
            PlanDraftMissingOnLocalAndOriginError,
            _empty_contract_hitl_reason,
        )

        reason = _empty_contract_hitl_reason(
            PlanDraftMissingOnLocalAndOriginError("draft x missing on both")
        )
        assert reason == "plan_draft_missing_on_local_and_origin"

    def test_populated_with_zero_slices_maps_to_populated_but_empty_slices(self):
        """#2627 review: ``POPULATED`` with ``slice_count == 0`` must NOT
        fall through to ``err.outcome.value`` (which would yield the bare
        string ``populated`` — contradicting the HITL when the contract
        is actually empty)."""
        from routes.pipelines import (
            PopulateOutcome,
            PopulateProducedEmptyContractError,
            _empty_contract_hitl_reason,
        )

        reason = _empty_contract_hitl_reason(
            PopulateProducedEmptyContractError(PopulateOutcome.POPULATED, slice_count=0)
        )
        assert reason == "populated_but_empty_slices"
        # And it MUST NOT be the bare outcome.value — that's the
        # contradiction the helper exists to prevent.
        assert reason != "populated"

    def test_non_populated_outcomes_use_outcome_value(self):
        """For every non-POPULATED outcome the reason is the outcome's
        string value — covers EMPTY_RESULT, PARSE_FAILED, etc."""
        from routes.pipelines import (
            PopulateOutcome,
            PopulateProducedEmptyContractError,
            _empty_contract_hitl_reason,
        )

        for outcome in PopulateOutcome:
            if outcome == PopulateOutcome.POPULATED:
                continue
            err = PopulateProducedEmptyContractError(outcome)
            reason = _empty_contract_hitl_reason(err)
            assert reason == outcome.value, (
                f"non-POPULATED outcome {outcome} must dispatch to "
                f"outcome.value ({outcome.value!r}), got {reason!r}"
            )

    def test_all_three_exception_classes_produce_distinct_reasons(self):
        """A swap between branches in the dispatch would collide reasons.
        Assert every distinguishable case yields a unique reason."""
        from routes.pipelines import (
            PlanDraftMissingOnLocalAndOriginError,
            PlanDraftMissingOnLocalError,
            PopulateOutcome,
            PopulateProducedEmptyContractError,
            _empty_contract_hitl_reason,
        )

        reasons = {
            _empty_contract_hitl_reason(PlanDraftMissingOnLocalError("x")),
            _empty_contract_hitl_reason(PlanDraftMissingOnLocalAndOriginError("x")),
            _empty_contract_hitl_reason(
                PopulateProducedEmptyContractError(PopulateOutcome.POPULATED, slice_count=0)
            ),
            _empty_contract_hitl_reason(
                PopulateProducedEmptyContractError(PopulateOutcome.EMPTY_RESULT)
            ),
            _empty_contract_hitl_reason(
                PopulateProducedEmptyContractError(PopulateOutcome.PARSE_FAILED)
            ),
        }
        # Five distinct inputs → five distinct reasons.
        assert len(reasons) == 5


class TestPlanCompleteCallSiteWireUp:
    """#2627 review: the plan-complete call site ties three helpers
    together:

        result = _populate_contract_from_plan_safe(...)
        if _populate_result_is_empty_contract(result):
            logger.error("OVERSEER_ALERT plan_populate_produced_empty_contract", ...)
            raise PopulateProducedEmptyContractError(result.outcome, slice_count=result.slice_count)
        ... except (...) as missing_err:
            _hitl_reason = _empty_contract_hitl_reason(missing_err)
            _emit_empty_contract_hitl(..., reason=_hitl_reason, gate="plan_complete", ...)
            teardown_reason, log_event = _empty_contract_failure_metadata(missing_err)
            logger.error(log_event, ...)

    The helper-level tests above cover each piece independently.  These
    tests verify the *connection* — a regression that swaps the helper
    for the old narrow check, or drops the OVERSEER_ALERT pre-raise log,
    or wires the wrong helper into the call site, would slip past every
    other test.  Source-inspection on the ``_run_pipeline`` body is the
    least-painful way to catch this without rebuilding the full
    ``_run_pipeline`` integration setup just for the plan-complete
    branch.

    Fragility note (#2261 slice-15): when ``_run_pipeline`` is
    decomposed into per-phase handlers (``_run_plan.py``,
    ``_run_implement.py``, etc. — see ``orchestrator/CLAUDE.md``), the
    plan-complete branch body will live in a different function and
    ``inspect.getsource(_run_pipeline)`` will no longer cover it.  All
    seven assertions below will then need to be re-pointed at whichever
    submodule owns the plan-complete branch.  The assertions also use
    string-membership against the full function body, so a hypothetical
    new use of these tokens elsewhere in ``_run_pipeline`` could let a
    regression at the actual call site slip past — tightening the
    assertions to pin to the ``except`` block (regex anchored on the
    surrounding context) would close that gap if the call site grows
    siblings before the decomposition lands.
    """

    @staticmethod
    def _run_pipeline_source() -> str:
        import inspect

        from routes.pipelines import _run_pipeline

        return inspect.getsource(_run_pipeline)

    def test_call_site_uses_populate_result_is_empty_contract_helper(self):
        """The call-site condition must route through the shared helper,
        not duplicate the outcome-set check inline (which would let it
        drift away from the safety-net check)."""
        source = self._run_pipeline_source()
        # The shared helper is the one named symbol the call site needs.
        # A regression that inlines the check loses this reference.
        assert "_populate_result_is_empty_contract(_plan_complete_populate_result)" in source, (
            "plan-complete call site must invoke _populate_result_is_empty_contract "
            "to share the empty-contract check with the safety net (#2627 review)"
        )

    def test_call_site_raises_populate_produced_empty_contract_error(self):
        """When the helper returns True, the call site must raise so the
        existing ``except (PlanDraftMissing*, PopulateProducedEmptyContractError)``
        handler downstream runs the FAILED-cleanup sequence."""
        source = self._run_pipeline_source()
        assert "raise PopulateProducedEmptyContractError(" in source, (
            "plan-complete call site must raise PopulateProducedEmptyContractError "
            "so the FAILED-cleanup handler catches it (#2627)"
        )

    def test_call_site_emits_overseer_alert_before_raise(self):
        """#2627 review: the third fail-loud branch (populate produced
        empty) must emit an ``OVERSEER_ALERT plan_populate_produced_empty_contract``
        log BEFORE the raise so its discriminator parallels the two
        ``PlanDraftMissing*`` wrapper-side pre-raise emits.  Without this
        the FAILED-cleanup log line was the only mention of the
        discriminator, asymmetric with the other two branches."""
        source = self._run_pipeline_source()
        # The OVERSEER_ALERT string and the raise must both appear in
        # the plan-complete branch; order-checking is brittle so we
        # assert co-occurrence and that the alert text comes before the
        # raise text in the source.
        alert = '"OVERSEER_ALERT plan_populate_produced_empty_contract"'
        raise_text = "raise PopulateProducedEmptyContractError("
        assert alert in source, (
            "plan-complete call site must emit a pre-raise OVERSEER_ALERT with "
            "the plan_populate_produced_empty_contract discriminator (#2627 review)"
        )
        assert source.index(alert) < source.index(raise_text), (
            "OVERSEER_ALERT must be logged BEFORE the raise so the discriminator "
            "is visible in the audit trail even if the FAILED-cleanup logger fails"
        )

    def test_call_site_dispatches_hitl_reason_through_helper(self):
        """The call site must call _empty_contract_hitl_reason(missing_err)
        rather than inline isinstance() dispatch — that's what the helper
        was extracted for (#2627 review)."""
        source = self._run_pipeline_source()
        assert "_empty_contract_hitl_reason(missing_err)" in source, (
            "plan-complete except-block must dispatch via _empty_contract_hitl_reason "
            "so the HITL reason is unit-testable independent of _run_pipeline"
        )

    def test_call_site_dispatches_failure_metadata_through_helper(self):
        """The call site must call _empty_contract_failure_metadata(missing_err)
        rather than inline isinstance() dispatch on the (teardown_reason,
        log_event) pair."""
        source = self._run_pipeline_source()
        assert "_empty_contract_failure_metadata(missing_err)" in source, (
            "plan-complete except-block must dispatch via _empty_contract_failure_metadata "
            "so the metadata pair is unit-testable independent of _run_pipeline"
        )

    def test_except_clause_catches_all_three_fail_loud_exceptions(self):
        """The ``except`` tuple must list all three #2627 fail-loud
        exception classes.  Dropping any one of them would let the
        corresponding branch propagate to the outer pipeline ``except``
        and bypass the dedicated empty-contract HITL."""
        source = self._run_pipeline_source()
        # The exact except clause varies in whitespace but the three
        # class names must all appear in a single nearby block.
        assert "PlanDraftMissingOnLocalError," in source
        assert "PlanDraftMissingOnLocalAndOriginError," in source
        assert "PopulateProducedEmptyContractError," in source

    def test_emit_empty_contract_hitl_called_with_plan_complete_gate(self):
        """The plan-complete except-block must call _emit_empty_contract_hitl
        with gate="plan_complete" so the HITL question text identifies
        which guard fired."""
        source = self._run_pipeline_source()
        # Find the plan-complete _emit_empty_contract_hitl call by gate string.
        assert 'gate="plan_complete"' in source, (
            'plan-complete except-block must pass gate="plan_complete" to '
            "_emit_empty_contract_hitl so the HITL names which guard fired (#2627)"
        )


class TestSafetyNetForestViolationLandsOnEmptyContractHitl:
    """#2627 review: the ``start_phase=implement`` safety net previously
    called ``_populate_contract_from_plan`` (the inner) directly, so a
    ``ForestValidationError`` raised by the inner propagated past the
    empty-contract check and landed on the outer pipeline ``except``
    handler — bypassing the dedicated empty-contract HITL.  The
    plan-complete path uses the safe wrapper which translates
    ``ForestValidationError`` to ``PopulateResult(FOREST_VIOLATION)``
    and routes through the HITL.  Symmetry: the safety net now wraps
    the inner call in ``try: ... except ForestValidationError:`` and
    synthesizes the same ``PopulateResult`` so both paths converge.

    Fragility note (#2261 slice-15): same caveat as
    :class:`TestPlanCompleteCallSiteWireUp` — the safety-net branch will
    move out of ``_run_pipeline`` when it is decomposed into per-phase
    handlers; ``inspect.getsource(_run_pipeline)`` will no longer cover
    the safety-net body and the assertions below will need to be
    re-pointed at the new owning submodule.
    """

    @staticmethod
    def _run_pipeline_source() -> str:
        import inspect

        from routes.pipelines import _run_pipeline

        return inspect.getsource(_run_pipeline)

    def test_safety_net_catches_forest_validation_error(self):
        """The safety-net inner call must be wrapped in a try/except for
        ForestValidationError, otherwise the asymmetry the reviewer
        flagged returns."""
        source = self._run_pipeline_source()
        # The safety-net block is identified by the safety-net populate
        # result variable name.  Look for the ForestValidationError
        # catch in the same vicinity.
        # The safety-net call uses _populate_contract_from_plan (not
        # _safe), so the only ForestValidationError catch in
        # _run_pipeline that produces a PopulateResult is the safety-net's.
        assert "except ForestValidationError as forest_err:" in source, (
            "safety-net inner call must catch ForestValidationError to land on "
            "the dedicated empty-contract HITL (#2627 review)"
        )
        # And it must synthesize a FOREST_VIOLATION PopulateResult so the
        # downstream _populate_result_is_empty_contract check routes it.
        assert "PopulateOutcome.FOREST_VIOLATION" in source, (
            "safety-net forest-violation catch must synthesize "
            "PopulateResult(FOREST_VIOLATION) so the empty-contract check fires"
        )

    def test_safety_net_uses_dedicated_logger_warning_source(self):
        """The forest-violation catch must log with source=\"safety_net\"
        so the audit trail can distinguish the safety-net path from the
        wrapper's translation (which uses source=\"safe_wrapper\")."""
        source = self._run_pipeline_source()
        # Both occurrences exist; we just verify the safety_net source
        # token is present.  The wrapper's source="safe_wrapper" is
        # already verified by other tests in this file.
        assert 'source="safety_net"' in source, (
            'safety-net forest-violation catch must log source="safety_net" '
            "to distinguish from the wrapper-side translation"
        )

    def test_safety_net_dispatches_reason_through_shared_helper(self):
        """#2627 review follow-up: the safety-net's reason dispatch must
        route through :func:`_populate_outcome_to_hitl_reason` so the
        ``POPULATED → populated_but_empty_slices`` translation (and any
        future special-cased outcome) can't drift from the plan-complete
        path.  The prior shape inlined the ``if outcome == POPULATED``
        check, which was the exact drift surface the helper was extracted
        to remove."""
        source = self._run_pipeline_source()
        # The safety-net call site must invoke the shared primitive on
        # the safety-net populate result.
        assert "_populate_outcome_to_hitl_reason(" in source, (
            "safety-net must dispatch reason via _populate_outcome_to_hitl_reason "
            "so the POPULATED-with-no-slices translation is shared with the "
            "plan-complete path (#2627 review follow-up)"
        )


class TestPopulateOutcomeToHitlReason:
    """#2627 review follow-up: the populate-outcome → HITL-reason
    primitive is shared by :func:`_empty_contract_hitl_reason` (called
    from the plan-complete handler) and the ``start_phase=implement``
    safety-net inline dispatch.  Unit-test it so the shared translation
    stays consistent.
    """

    def test_populated_maps_to_populated_but_empty_slices(self):
        from routes.pipelines import (
            PopulateOutcome,
            _populate_outcome_to_hitl_reason,
        )

        # POPULATED with no slices is the orthogonal "populator ran but
        # produced nothing" case.  The bare "populated" reason would
        # contradict the empty-contract HITL — the helper exists to
        # prevent that.
        assert _populate_outcome_to_hitl_reason(PopulateOutcome.POPULATED) == (
            "populated_but_empty_slices"
        )

    def test_non_populated_outcomes_dispatch_to_outcome_value(self):
        from routes.pipelines import (
            PopulateOutcome,
            _populate_outcome_to_hitl_reason,
        )

        for outcome in PopulateOutcome:
            if outcome == PopulateOutcome.POPULATED:
                continue
            assert _populate_outcome_to_hitl_reason(outcome) == outcome.value, (
                f"non-POPULATED outcome {outcome} must map to outcome.value ({outcome.value!r})"
            )

    def test_helper_dispatches_through_outcome_primitive(self):
        """:func:`_empty_contract_hitl_reason` must delegate the
        ``PopulateProducedEmptyContractError`` branch to the shared
        primitive so the two call sites can't disagree about the
        outcome → reason mapping."""
        import inspect

        from routes.pipelines import _empty_contract_hitl_reason

        helper_source = inspect.getsource(_empty_contract_hitl_reason)
        assert "_populate_outcome_to_hitl_reason(" in helper_source, (
            "_empty_contract_hitl_reason must delegate the outcome branch to "
            "_populate_outcome_to_hitl_reason so the safety net and plan-complete "
            "paths share the dispatch (#2627 review follow-up)"
        )


class TestEmptyContractHitlQuestionReasonAwareDivergence:
    """#2627 review follow-up: when ``draft_slice_count is None`` the
    HITL question previously hardcoded "the plan draft is missing,
    unparseable, or yielded no tasks".  The widened
    :func:`_populate_result_is_empty_contract` check now routes
    ``FOREST_VIOLATION`` / ``CONTRACT_LOAD_FAILED`` /
    ``EGG_CONTRACTS_UNAVAILABLE`` / ``UNEXPECTED_EXCEPTION`` plus the
    orthogonal ``populated_but_empty_slices`` case through this same
    question, where the generic prose contradicts the ``reason=`` field
    operators see in ``pipeline.error``.  Reason-aware divergence lines
    keep the prose and the reason field consistent."""

    def test_forest_violation_uses_dag_specific_wording(self):
        from routes.pipelines import _empty_contract_hitl_question

        question = _empty_contract_hitl_question(
            pipeline_id="p-forest",
            reason="forest_violation",
            draft_slice_count=None,
            gate="plan_complete",
        )
        # The forest-violation case is "the draft parsed but the DAG was
        # rejected" — must NOT claim the draft is missing/unparseable.
        assert "slice DAG" in question, (
            "forest_violation question must name the slice-DAG rejection "
            "instead of the generic 'missing/unparseable/yielded no tasks' line"
        )
        assert "missing, unparseable, or yielded no tasks" not in question, (
            "forest_violation must NOT use the generic draft-missing prose — "
            "that contradicts the reason field operators see"
        )
        assert "forest_violation" in question

    def test_contract_load_failed_uses_deserialize_specific_wording(self):
        from routes.pipelines import _empty_contract_hitl_question

        question = _empty_contract_hitl_question(
            pipeline_id="p-load",
            reason="contract_load_failed",
            draft_slice_count=None,
            gate="plan_complete",
        )
        assert "deserialize" in question, (
            "contract_load_failed question must name the deserialize failure "
            "instead of the generic draft-missing prose"
        )
        assert "missing, unparseable, or yielded no tasks" not in question
        assert "contract_load_failed" in question

    def test_populated_but_empty_slices_uses_populator_specific_wording(self):
        from routes.pipelines import _empty_contract_hitl_question

        question = _empty_contract_hitl_question(
            pipeline_id="p-populated",
            reason="populated_but_empty_slices",
            draft_slice_count=None,
            gate="plan_complete",
        )
        assert "produced 0 slices" in question or "produced 0 slices/tasks" in question, (
            "populated_but_empty_slices question must name the 0-slices outcome "
            "instead of the generic draft-missing prose"
        )
        assert "missing, unparseable, or yielded no tasks" not in question
        assert "populated_but_empty_slices" in question

    def test_egg_contracts_unavailable_uses_library_specific_wording(self):
        from routes.pipelines import _empty_contract_hitl_question

        question = _empty_contract_hitl_question(
            pipeline_id="p-lib",
            reason="egg_contracts_unavailable",
            draft_slice_count=None,
            gate="plan_complete",
        )
        assert "egg-contracts" in question
        assert "missing, unparseable, or yielded no tasks" not in question

    def test_unexpected_exception_uses_exception_specific_wording(self):
        from routes.pipelines import _empty_contract_hitl_question

        question = _empty_contract_hitl_question(
            pipeline_id="p-exc",
            reason="unexpected_exception",
            draft_slice_count=None,
            gate="plan_complete",
        )
        assert "unexpected exception" in question
        assert "missing, unparseable, or yielded no tasks" not in question

    def test_draft_missing_reasons_still_use_generic_prose(self):
        """Reasons where the generic wording IS accurate (the draft really
        is missing or yielded no tasks) must keep the original line —
        we're not regressing the wording that was right to begin with."""
        from routes.pipelines import _empty_contract_hitl_question

        for accurate_reason in (
            "empty_result",
            "parse_failed",
            "draft_missing",
            "no_draft_path",
            "plan_draft_missing_on_local",
            "plan_draft_missing_on_local_and_origin",
        ):
            question = _empty_contract_hitl_question(
                pipeline_id="p-generic",
                reason=accurate_reason,
                draft_slice_count=None,
                gate="plan_complete",
            )
            assert "missing" in question or "unparseable" in question, (
                f"reason {accurate_reason!r} should fall through to the generic "
                f"draft-missing line (it accurately describes that root cause)"
            )
            assert accurate_reason in question


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
