"""
Tests for Phase Transition module.

Post-#2777 slice-2 invariants:
* ``IMPLEMENT`` is the terminal pipeline phase (the ``PR`` phase was
  deleted by slice-2 task-2-2; ``PipelinePhase.PR`` no longer exists).
* ``VALID_TRANSITIONS[IMPLEMENT] == []`` — no outgoing edges from
  IMPLEMENT.
* ``get_next_phase(IMPLEMENT) is None``.
* The state-machine table contains no ``PR`` entries (default-deny on
  any ``target='pr'`` advance attempt).
"""

import pytest
from phase_filter import PipelinePhase
from phase_transition import (
    VALID_TRANSITIONS,
    TransitionRequest,
    TransitionResult,
    TransitionRole,
    can_transition_to,
    create_audit_entry,
    get_next_phase,
    validate_transition,
)


class TestTransitionResult:
    """Tests for TransitionResult class."""

    def test_allowed_result(self):
        """Create an allowed transition result."""
        result = TransitionResult.allowed(
            from_phase=PipelinePhase.REFINE,
            to_phase=PipelinePhase.PLAN,
            transitioned_by="james-in-a-box",
        )

        assert result.success is True
        assert result.from_phase == PipelinePhase.REFINE
        assert result.to_phase == PipelinePhase.PLAN
        assert result.transitioned_by == "james-in-a-box"
        assert result.transitioned_at is not None

    def test_denied_result(self):
        """Create a denied transition result."""
        result = TransitionResult.denied(
            message="Role 'implementer' cannot exit phase 'refine'",
            from_phase=PipelinePhase.REFINE,
            to_phase=PipelinePhase.PLAN,
        )

        assert result.success is False
        assert "implementer" in result.message
        assert result.from_phase == PipelinePhase.REFINE
        assert result.to_phase == PipelinePhase.PLAN


class TestTransitionRequest:
    """Tests for TransitionRequest class."""

    def test_from_dict(self):
        """Create TransitionRequest from dictionary."""
        data = {
            "from_phase": "refine",
            "to_phase": "plan",
            "role": "human",
            "actor": "test-user",
            "reason": "Analysis complete",
        }
        request = TransitionRequest.from_dict(data)

        assert request.from_phase == PipelinePhase.REFINE
        assert request.to_phase == PipelinePhase.PLAN
        assert request.role == TransitionRole.HUMAN
        assert request.actor == "test-user"
        assert request.reason == "Analysis complete"

    def test_from_dict_minimal(self):
        """Create TransitionRequest with minimal fields.

        Post-slice-2 the minimal exemplar uses the surviving
        ``PLAN → IMPLEMENT`` edge (``IMPLEMENT → PR`` no longer exists).
        """
        data = {
            "from_phase": "plan",
            "to_phase": "implement",
            "role": "reviewer",
        }
        request = TransitionRequest.from_dict(data)

        assert request.from_phase == PipelinePhase.PLAN
        assert request.to_phase == PipelinePhase.IMPLEMENT
        assert request.actor == "unknown"
        assert request.reason is None

    def test_from_dict_rejects_pr_target(self):
        """``to_phase='pr'`` must not deserialise — the value is gone.

        Default-deny: any caller passing the dead ``'pr'`` string
        (stale request payload, replayed audit-log entry) must fail
        loudly via ``ValueError`` from the enum-coercion path, not
        produce a silently-valid request.
        """
        data = {
            "from_phase": "implement",
            "to_phase": "pr",
            "role": "reviewer",
        }
        with pytest.raises(ValueError):
            TransitionRequest.from_dict(data)


class TestValidTransitions:
    """Tests for the valid transitions graph."""

    def test_refine_to_plan(self):
        """Refine can only transition to plan."""
        assert PipelinePhase.PLAN in VALID_TRANSITIONS[PipelinePhase.REFINE]
        assert len(VALID_TRANSITIONS[PipelinePhase.REFINE]) == 1

    def test_plan_to_implement(self):
        """Plan can transition to implement (and to apply for epic pipelines).

        Issue #1557: ``PLAN`` gained ``APPLY`` as a second valid
        successor so epic-mode pipelines can route Jira mutations
        through a dedicated APPLY phase between PLAN and IMPLEMENT.
        Non-epic pipelines continue to use the IMPLEMENT edge —
        ``IMPLEMENT`` is listed first so ``get_next_phase`` keeps the
        pre-#1557 default."""
        assert PipelinePhase.IMPLEMENT in VALID_TRANSITIONS[PipelinePhase.PLAN]
        assert PipelinePhase.APPLY in VALID_TRANSITIONS[PipelinePhase.PLAN]
        assert len(VALID_TRANSITIONS[PipelinePhase.PLAN]) == 2
        # Default-first ordering invariant: epic-aware schedulers pick
        # APPLY by name; non-epic flows that take ``next_phases[0]``
        # must still see IMPLEMENT.
        assert VALID_TRANSITIONS[PipelinePhase.PLAN][0] == PipelinePhase.IMPLEMENT

    def test_plan_orderings_match_across_modules(self):
        """``VALID_TRANSITIONS[PLAN]`` ordering is mirrored in the
        orchestrator-side ``PHASE_TRANSITIONS`` table.

        Correctness of the epic vs non-epic routing relies on the
        position of ``IMPLEMENT`` in the list — ``get_next_phase`` (and
        any HITL path that reads ``VALID_TRANSITIONS`` directly) returns
        ``next_phases[0]``. If a future refactor reorders one table
        without the other, non-epic pipelines could silently start
        scheduling APPLY. This test catches the drift before it ships.
        """
        try:
            from orchestrator.routes.phases import (  # type: ignore[import-not-found]
                PHASE_TRANSITIONS,
            )
        except ImportError:  # pragma: no cover - module path varies in the sandbox
            try:
                from routes.phases import (
                    PHASE_TRANSITIONS,  # type: ignore[import-not-found, no-redef]
                )
            except ImportError:
                pytest.skip("orchestrator routes.phases not importable in this env")
        assert PHASE_TRANSITIONS[PipelinePhase.PLAN] == VALID_TRANSITIONS[PipelinePhase.PLAN]
        assert PHASE_TRANSITIONS[PipelinePhase.PLAN][0] == PipelinePhase.IMPLEMENT

    def test_apply_to_implement(self):
        """Apply (Jira-epic phase) advances only to implement.

        Issue #1557: the new ``APPLY`` phase is the second step in the
        epic-mode pipeline (PLAN → APPLY → IMPLEMENT).  The orchestrator-
        side scheduler in ``orchestrator.routes.pipelines.
        _next_phases_for_epic`` picks APPLY only when ``Pipeline.is_epic``
        is true; this transition is what carries the pipeline back into
        the standard IMPLEMENT phase once the applier has driven all Jira
        mutations and BRC consensus has confirmed."""
        assert PipelinePhase.IMPLEMENT in VALID_TRANSITIONS[PipelinePhase.APPLY]
        assert len(VALID_TRANSITIONS[PipelinePhase.APPLY]) == 1

    def test_implement_is_terminal(self):
        """IMPLEMENT is the terminal pipeline phase (#2777 slice-2).

        Pre-slice-2: ``IMPLEMENT → PR``. Post-slice-2: ``IMPLEMENT``
        has zero outgoing edges. Any consumer iterating outgoing edges
        from IMPLEMENT (state-machine renderer, DAG visualiser,
        terminal-detection logic) must see an empty list.
        """
        assert VALID_TRANSITIONS[PipelinePhase.IMPLEMENT] == [], (
            "VALID_TRANSITIONS[IMPLEMENT] must be empty after slice-2 "
            f"deletes the PR phase; got {VALID_TRANSITIONS[PipelinePhase.IMPLEMENT]!r}"
        )

    def test_no_pr_phase_in_transition_table(self):
        """``VALID_TRANSITIONS`` must not contain any ``PR`` entries.

        The deleted phase must leave no trace in the state machine — no
        outgoing edges keyed on PR, no PR appearing as a destination
        from any other phase.
        """
        # PipelinePhase.PR no longer exists as an enum member, so we
        # check by value string.
        pr_keys = [k for k in VALID_TRANSITIONS if k.value == "pr"]
        assert pr_keys == [], (
            f"VALID_TRANSITIONS must contain no PR keys; got {pr_keys!r}"
        )
        for from_phase, destinations in VALID_TRANSITIONS.items():
            pr_dests = [d for d in destinations if d.value == "pr"]
            assert pr_dests == [], (
                f"VALID_TRANSITIONS[{from_phase!r}] must not list PR as a "
                f"destination; got {destinations!r}"
            )


class TestValidateTransition:
    """Tests for validate_transition function."""

    def test_valid_transition_with_human(self):
        """Human can transition between any valid phases."""
        request = TransitionRequest(
            from_phase=PipelinePhase.REFINE,
            to_phase=PipelinePhase.PLAN,
            role=TransitionRole.HUMAN,
            actor="test-human",
        )
        result = validate_transition(request)

        assert result.success is True
        assert result.from_phase == PipelinePhase.REFINE
        assert result.to_phase == PipelinePhase.PLAN

    def test_invalid_transition_path(self):
        """Cannot skip phases in the pipeline."""
        request = TransitionRequest(
            from_phase=PipelinePhase.REFINE,
            to_phase=PipelinePhase.IMPLEMENT,  # Invalid - must go through plan
            role=TransitionRole.HUMAN,
            actor="test-human",
        )
        result = validate_transition(request)

        assert result.success is False
        assert "Invalid transition" in result.message

    def test_backwards_transition_blocked(self):
        """Cannot transition backwards in the pipeline."""
        request = TransitionRequest(
            from_phase=PipelinePhase.IMPLEMENT,
            to_phase=PipelinePhase.PLAN,
            role=TransitionRole.HUMAN,
            actor="test-human",
        )
        result = validate_transition(request)

        assert result.success is False
        assert "Invalid transition" in result.message

    def test_implementer_cannot_exit_refine(self):
        """Implementer cannot exit refine phase (requires human)."""
        request = TransitionRequest(
            from_phase=PipelinePhase.REFINE,
            to_phase=PipelinePhase.PLAN,
            role=TransitionRole.IMPLEMENTER,
            actor="james-in-a-box",
        )
        result = validate_transition(request)

        assert result.success is False
        assert "cannot exit" in result.message.lower()

    def test_implementer_cannot_exit_plan(self):
        """Implementer cannot exit plan phase (requires human)."""
        request = TransitionRequest(
            from_phase=PipelinePhase.PLAN,
            to_phase=PipelinePhase.IMPLEMENT,
            role=TransitionRole.IMPLEMENTER,
            actor="james-in-a-box",
        )
        result = validate_transition(request)

        assert result.success is False

    def test_implement_has_no_valid_exit_transition(self):
        """Post-slice-2, IMPLEMENT is terminal — no exit transition validates.

        Pre-slice-2 ``IMPLEMENT → PR`` was the canonical reviewer exit.
        Post-slice-2 IMPLEMENT has no successor; any caller attempting
        to advance from IMPLEMENT (any role, any to_phase) must be
        rejected.
        """
        # Construct a request that would have been valid pre-slice-2.
        # We can't reference ``PipelinePhase.PR`` directly (the member
        # is gone), so we drive it via the dict-based constructor that
        # surfaces the ValueError loudly.
        request = TransitionRequest(
            from_phase=PipelinePhase.IMPLEMENT,
            to_phase=PipelinePhase.REFINE,  # any valid phase — none should accept
            role=TransitionRole.HUMAN,
            actor="test-human",
        )
        result = validate_transition(request)
        assert result.success is False, (
            "After slice-2, IMPLEMENT must have no outgoing transitions; "
            f"validate_transition unexpectedly accepted: {result!r}"
        )


class TestRoleHierarchy:
    """Tests for role hierarchy in transitions."""

    def test_human_can_satisfy_any_requirement(self):
        """Human role can satisfy any exit requirement."""
        # Human can exit refine (requires human)
        result = can_transition_to(PipelinePhase.REFINE, PipelinePhase.PLAN, TransitionRole.HUMAN)
        assert result.success is True

        # Human can exit plan (requires human)
        result = can_transition_to(PipelinePhase.PLAN, PipelinePhase.IMPLEMENT, TransitionRole.HUMAN)
        assert result.success is True

    def test_reviewer_cannot_satisfy_human_requirement(self):
        """Reviewer cannot satisfy human requirement."""
        result = can_transition_to(
            PipelinePhase.REFINE, PipelinePhase.PLAN, TransitionRole.REVIEWER
        )
        assert result.success is False


class TestGetNextPhase:
    """Tests for get_next_phase function."""

    def test_refine_next_is_plan(self):
        """Next phase after refine is plan."""
        assert get_next_phase(PipelinePhase.REFINE) == PipelinePhase.PLAN

    def test_plan_next_is_implement(self):
        """Next phase after plan is implement (non-epic default)."""
        assert get_next_phase(PipelinePhase.PLAN) == PipelinePhase.IMPLEMENT

    def test_implement_next_is_none(self):
        """IMPLEMENT is terminal after slice-2 — no next phase."""
        assert get_next_phase(PipelinePhase.IMPLEMENT) is None, (
            "get_next_phase(IMPLEMENT) must return None after slice-2 "
            "deletes the PR phase; got something else."
        )


class TestCanTransitionTo:
    """Tests for can_transition_to convenience function."""

    def test_with_strings(self):
        """Function accepts string arguments."""
        result = can_transition_to("refine", "plan", "human", "test-actor")

        assert result.success is True
        assert result.from_phase == PipelinePhase.REFINE
        assert result.to_phase == PipelinePhase.PLAN

    def test_with_enums(self):
        """Function accepts enum arguments (surviving PLAN → IMPLEMENT edge)."""
        result = can_transition_to(
            PipelinePhase.PLAN,
            PipelinePhase.IMPLEMENT,
            TransitionRole.HUMAN,
            "test-human",
        )

        assert result.success is True

    def test_pr_string_rejected_in_strings_form(self):
        """``to_phase='pr'`` string form must default-deny.

        The convenience helper accepts strings so a stale caller (an
        old script, a CI hook from before slice-2 landed) might still
        pass ``"pr"``. The enum-coercion path inside the function must
        reject it rather than coerce to a phantom value.
        """
        with pytest.raises(ValueError):
            can_transition_to("implement", "pr", "reviewer", "reviewer-agent")


class TestCreateAuditEntry:
    """Tests for create_audit_entry function."""

    def test_creates_valid_entry(self):
        """Create a valid audit entry from transition result."""
        result = TransitionResult.allowed(
            from_phase=PipelinePhase.REFINE,
            to_phase=PipelinePhase.PLAN,
            transitioned_by="james-in-a-box",
        )
        entry = create_audit_entry(result, TransitionRole.HUMAN, "Analysis approved")

        assert entry["action"] == "transition"
        assert entry["field_path"] == "current_phase"
        assert entry["old_value"] == "refine"
        assert entry["new_value"] == "plan"
        assert entry["role"] == "human"
        assert entry["actor"] == "james-in-a-box"
        assert entry["reason"] == "Analysis approved"
        assert "timestamp" in entry

    def test_handles_none_values(self):
        """Handle None values in result."""
        result = TransitionResult.denied("Test denial")
        entry = create_audit_entry(result, TransitionRole.IMPLEMENTER)

        assert entry["old_value"] is None
        assert entry["new_value"] is None
        assert entry["actor"] == "unknown"
        assert entry["reason"] is None
