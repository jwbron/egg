"""Tests for ``EpicApplyArtifact`` and ``Pipeline.jira_*`` fields (#1557 TASK-1-7).

Covers:

* ``EpicApplyArtifact`` Pydantic model
    - schema validation: version (>= 1), idempotency_seed (required, non-empty),
      refine_description_sha256, applied_edits, wont_do_batch,
      in_flight_gates, plan_node_to_jira_key mapping
    - JSON round-trip (``model_dump_json`` → ``model_validate_json``)
    - ``validate_assignment=True`` re-validates on attribute set
* ``Pipeline.jira_epic_key`` — regex validator parity with ``_validate_jira_ticket``
* ``Pipeline.jira_effective_mode`` — Literal accepts ``reassess`` / ``fresh`` / ``None``
* ``Pipeline.jira_parent_epic_key`` — same regex validator as the others
* ``PipelinePhase.PLAN_STOPPED`` — terminal-without-PR enum value usable as
  ``current_phase``

The tests load model classes by bare name through the
``orchestrator/tests/conftest.py`` sys.path bootstrapping.
"""

from __future__ import annotations

import json

import pytest
from egg_contracts.models import PipelinePhase
from models import (
    EpicApplyArtifact,
    EpicApplyEdit,
    EpicApplyInFlightGate,
    EpicApplyWontDoEntry,
    Pipeline,
)
from pydantic import ValidationError

# -----------------------------------------------------------------------------
# EpicApplyArtifact — schema + JSON round-trip
# -----------------------------------------------------------------------------


class TestEpicApplyArtifactSchema:
    """Field-level validation of the apply artifact."""

    def test_minimal_construction(self) -> None:
        art = EpicApplyArtifact(idempotency_seed="seed-1")
        assert art.version == 1
        assert art.idempotency_seed == "seed-1"
        assert art.refine_description_sha256 is None
        assert art.plan_node_to_jira_key == {}
        assert art.applied_edits == []
        assert art.wont_do_batch == []
        assert art.in_flight_gates == []

    def test_idempotency_seed_required(self) -> None:
        with pytest.raises(ValidationError):
            EpicApplyArtifact()  # type: ignore[call-arg]

    def test_idempotency_seed_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            EpicApplyArtifact(idempotency_seed="")

    def test_version_must_be_ge_one(self) -> None:
        with pytest.raises(ValidationError):
            EpicApplyArtifact(idempotency_seed="seed-1", version=0)
        with pytest.raises(ValidationError):
            EpicApplyArtifact(idempotency_seed="seed-1", version=-3)

    def test_version_accepts_future_values(self) -> None:
        """Schema is forward-compat — newer versions still validate so
        readers can detect unknown shapes via ``version > 1``."""
        art = EpicApplyArtifact(idempotency_seed="seed-1", version=2)
        assert art.version == 2

    def test_refine_description_sha256_optional(self) -> None:
        art = EpicApplyArtifact(
            idempotency_seed="seed-1",
            refine_description_sha256="abc123" * 10,
        )
        assert art.refine_description_sha256 == "abc123" * 10

    def test_plan_node_to_jira_key_mapping(self) -> None:
        art = EpicApplyArtifact(
            idempotency_seed="seed-1",
            plan_node_to_jira_key={"node-1": "ENG-100", "node-2": "ENG-101"},
        )
        assert art.plan_node_to_jira_key["node-1"] == "ENG-100"
        assert art.plan_node_to_jira_key["node-2"] == "ENG-101"

    def test_applied_edits_validated_as_edit_models(self) -> None:
        edit = EpicApplyEdit(kind="create", target="node-1")
        art = EpicApplyArtifact(
            idempotency_seed="seed-1",
            applied_edits=[edit],
        )
        assert len(art.applied_edits) == 1
        assert isinstance(art.applied_edits[0], EpicApplyEdit)
        assert art.applied_edits[0].kind == "create"
        assert art.applied_edits[0].target == "node-1"

    def test_applied_edits_invalid_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EpicApplyArtifact(
                idempotency_seed="seed-1",
                applied_edits=[{"kind": "delete", "target": "x"}],
            )

    def test_wont_do_batch_validated(self) -> None:
        entry = EpicApplyWontDoEntry(child_key="ENG-99", wont_do_reason="obsolete")
        art = EpicApplyArtifact(idempotency_seed="seed-1", wont_do_batch=[entry])
        assert art.wont_do_batch[0].child_key == "ENG-99"
        assert art.wont_do_batch[0].status == "pending"

    def test_wont_do_reason_required(self) -> None:
        with pytest.raises(ValidationError):
            EpicApplyWontDoEntry(child_key="ENG-1", wont_do_reason="")

    def test_in_flight_gates_validated(self) -> None:
        gate = EpicApplyInFlightGate(
            child_key="ENG-77",
            proposed_mutation="edit summary",
            signal_source=["remote_link"],
            signal_detail={"remote_link": "https://github.com/foo/bar/pull/1"},
        )
        art = EpicApplyArtifact(idempotency_seed="seed-1", in_flight_gates=[gate])
        assert art.in_flight_gates[0].child_key == "ENG-77"
        assert art.in_flight_gates[0].signal_source == ["remote_link"]

    def test_in_flight_gate_signal_source_enum(self) -> None:
        """Only the three documented signal sources (decision-8 OR semantics)
        are accepted."""
        # Valid sources individually.
        for src in ("jira_status", "orchestrator_pr_url", "remote_link"):
            EpicApplyInFlightGate(
                child_key="ENG-1",
                proposed_mutation="x",
                signal_source=[src],
            )
        # Garbage rejected.
        with pytest.raises(ValidationError):
            EpicApplyInFlightGate(
                child_key="ENG-1",
                proposed_mutation="x",
                signal_source=["confluence"],  # not in the enum
            )

    def test_assignment_revalidates(self) -> None:
        """``model_config = ConfigDict(validate_assignment=True)`` re-runs
        validation when fields are mutated post-construction."""
        art = EpicApplyArtifact(idempotency_seed="seed-1")
        # Setting an invalid version triggers ValidationError on assignment.
        with pytest.raises(ValidationError):
            art.version = 0  # type: ignore[assignment]
        # A valid update succeeds.
        art.version = 2
        assert art.version == 2


class TestEpicApplyArtifactRoundTrip:
    """Serialise + deserialise → equivalent artifact."""

    def test_round_trip_minimal(self) -> None:
        art = EpicApplyArtifact(idempotency_seed="seed-1")
        as_json = art.model_dump_json()
        rebuilt = EpicApplyArtifact.model_validate_json(as_json)
        assert rebuilt == art

    def test_round_trip_with_all_fields(self) -> None:
        edit = EpicApplyEdit(
            kind="create",
            target="node-1",
            payload={"summary": "hi", "project_key": "ENG"},
            summary_hash="deadbeef",
            status="applied",
        )
        wont_do = EpicApplyWontDoEntry(
            child_key="ENG-50",
            wont_do_reason="replaced by ENG-60",
            status="pending",
        )
        gate = EpicApplyInFlightGate(
            child_key="ENG-77",
            proposed_mutation="edit summary",
            signal_source=["jira_status", "remote_link"],
            signal_detail={"jira_status": "In Progress"},
            linked_pr_url="https://github.com/foo/bar/pull/42",
            decision_id="dec-1",
        )
        art = EpicApplyArtifact(
            version=1,
            idempotency_seed="seed-xyz",
            refine_description_sha256="a" * 64,
            plan_node_to_jira_key={"node-1": "ENG-100"},
            applied_edits=[edit],
            wont_do_batch=[wont_do],
            in_flight_gates=[gate],
        )

        as_json = art.model_dump_json()
        # Verify the JSON shape is a real string and decodes to a dict.
        decoded = json.loads(as_json)
        assert decoded["idempotency_seed"] == "seed-xyz"
        assert decoded["plan_node_to_jira_key"] == {"node-1": "ENG-100"}

        rebuilt = EpicApplyArtifact.model_validate_json(as_json)
        assert rebuilt == art
        # Nested models are real model instances, not dicts.
        assert isinstance(rebuilt.applied_edits[0], EpicApplyEdit)
        assert isinstance(rebuilt.wont_do_batch[0], EpicApplyWontDoEntry)
        assert isinstance(rebuilt.in_flight_gates[0], EpicApplyInFlightGate)
        assert rebuilt.in_flight_gates[0].signal_source == [
            "jira_status",
            "remote_link",
        ]

    def test_round_trip_through_pipeline_set_get(self) -> None:
        """``Pipeline.set_epic_apply`` + ``get_epic_apply`` is the operational
        round-trip path — pin that the serialise / parse pair stays
        symmetric."""
        art = EpicApplyArtifact(
            idempotency_seed="seed-2",
            plan_node_to_jira_key={"n1": "ENG-1"},
        )
        pipeline = Pipeline(id="issue-1557")
        pipeline.set_epic_apply(art)
        recovered = pipeline.get_epic_apply()
        assert recovered is not None
        assert recovered == art


# -----------------------------------------------------------------------------
# Pipeline.get_epic_apply — malformed artifact logs structured warning (#5)
# -----------------------------------------------------------------------------


class TestGetEpicApplyMalformedWarning:
    """v5 reviewer_code v3 #5 mitigation — ``get_epic_apply`` MUST log a
    structured ``epic_apply_artifact_invalid`` warning and return None
    when the persisted artifact is malformed, rather than silently
    swallowing as None. A silent swallow would let the apply step
    re-run from scratch on a malformed (rather than absent) artifact
    and re-issue every ``createJiraIssue`` against an already-partially-
    applied epic.
    """

    def _pipeline_with_raw_artifact(self, raw: str) -> Pipeline:
        from models import PhaseExecution, PipelinePhase

        pipeline = Pipeline(id="issue-1557")
        # Stash the malformed string straight into the artifacts dict
        # (bypassing set_epic_apply which would validate).
        plan = PhaseExecution(phase=PipelinePhase.PLAN)
        plan.artifacts["epic_apply"] = raw
        pipeline.phases[PipelinePhase.PLAN.value] = plan
        return pipeline

    def test_malformed_json_logs_warning_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The orchestrator uses egg_logging (a structlog wrapper) which
        # bypasses stdlib caplog; patch the module-level logger and
        # inspect the recorded calls directly.
        import models

        recorded: list[tuple[str, dict[str, object]]] = []

        def fake_warning(event: str, **kwargs: object) -> None:
            recorded.append((event, kwargs))

        monkeypatch.setattr(models._models_logger, "warning", fake_warning)

        pipeline = self._pipeline_with_raw_artifact("not valid json {{{")
        result = pipeline.get_epic_apply()
        assert result is None
        # Exactly one structured warning was emitted with the right
        # event name and reason.
        assert recorded, "expected an epic_apply_artifact_invalid warning"
        events = [e for e, _ in recorded]
        assert "epic_apply_artifact_invalid" in events
        kwargs = next(kw for e, kw in recorded if e == "epic_apply_artifact_invalid")
        assert kwargs["reason"] == "json_decode_failed"
        assert kwargs["pipeline_id"] == "issue-1557"
        assert "error" in kwargs

    def test_json_but_schema_invalid_logs_warning_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Decodes as JSON but fails Pydantic validation: bad kind value.
        import models

        recorded: list[tuple[str, dict[str, object]]] = []

        def fake_warning(event: str, **kwargs: object) -> None:
            recorded.append((event, kwargs))

        monkeypatch.setattr(models._models_logger, "warning", fake_warning)

        bad_artifact = json.dumps(
            {
                "version": 1,
                "idempotency_seed": "seed-x",
                "applied_edits": [
                    {
                        "kind": "this_is_not_a_valid_kind",
                        "target": "node-1",
                        "payload": {},
                        "summary_hash": "deadbeef",
                        "status": "pending",
                    }
                ],
            }
        )
        pipeline = self._pipeline_with_raw_artifact(bad_artifact)
        result = pipeline.get_epic_apply()
        assert result is None
        events = [e for e, _ in recorded]
        assert "epic_apply_artifact_invalid" in events
        kwargs = next(kw for e, kw in recorded if e == "epic_apply_artifact_invalid")
        assert kwargs["reason"] == "pydantic_validation_failed"

    def test_absent_artifact_does_not_log_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The "no artifact present" path is NOT an error — it's the
        first-run / pre-apply state and must not emit a warning.
        """
        import models

        recorded: list[tuple[str, dict[str, object]]] = []

        def fake_warning(event: str, **kwargs: object) -> None:
            recorded.append((event, kwargs))

        monkeypatch.setattr(models._models_logger, "warning", fake_warning)

        pipeline = Pipeline(id="issue-1557")
        assert pipeline.get_epic_apply() is None
        events = [e for e, _ in recorded]
        assert "epic_apply_artifact_invalid" not in events


# -----------------------------------------------------------------------------
# Pipeline jira_ticket / jira_epic_key mutual exclusivity validator (#11)
# -----------------------------------------------------------------------------


class TestJiraTicketAndEpicKeyMutualExclusivity:
    """v5 reviewer_code v3 #11 mitigation — the ``@model_validator(mode='after')``
    at ``orchestrator/models.py:1243-1263`` raises a ValueError when
    BOTH ``jira_ticket`` and ``jira_epic_key`` are set, because the
    issuetype-detection probe persists exactly one of the two based on
    the Atlassian issuetype. A pipeline carrying both is structurally
    ambiguous — downstream branches that gate on either field would
    each fire incorrectly.
    """

    def test_both_set_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            Pipeline(
                id="issue-1557",
                jira_ticket="ENG-1",
                jira_epic_key="ENG-2",
            )
        msg = str(exc.value)
        # The error message must name the constraint so the operator
        # understands what went wrong.
        assert "mutually exclusive" in msg
        # And surface both offending values so the operator can
        # diagnose which side was wrong.
        assert "ENG-1" in msg
        assert "ENG-2" in msg

    def test_only_jira_ticket_allowed(self) -> None:
        # The legacy single-ticket flow must keep working.
        Pipeline(id="issue-x", jira_ticket="ENG-1")

    def test_only_jira_epic_key_allowed(self) -> None:
        Pipeline(id="issue-x", jira_epic_key="ENG-1")

    def test_neither_set_allowed(self) -> None:
        # ISSUE-mode pipelines without a Jira key are still valid.
        Pipeline(id="issue-x")

    def test_both_set_with_jira_parent_epic_key_still_rejected(self) -> None:
        """Defence in depth: a pipeline with ``jira_parent_epic_key``
        (a child pipeline's provenance pointer) must STILL refuse to
        set both ``jira_ticket`` AND ``jira_epic_key`` — the parent
        pointer doesn't sidestep the mutual-exclusivity rule.
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Pipeline(
                id="issue-x",
                jira_ticket="ENG-1",
                jira_epic_key="ENG-2",
                jira_parent_epic_key="ENG-3",
            )


# -----------------------------------------------------------------------------
# Pipeline.jira_epic_key
# -----------------------------------------------------------------------------


class TestPipelineJiraEpicKey:
    """Same regex validator as ``_validate_jira_ticket`` — pinned here so a
    drift between the two would surface."""

    @pytest.mark.parametrize(
        "key",
        ["ENG-1", "ENG-1234", "K-1", "A1-7", "PROJ_X-42", "PROJ1-9999"],
    )
    def test_valid_keys_accepted(self, key: str) -> None:
        p = Pipeline(id="issue-1", jira_epic_key=key)
        assert p.jira_epic_key == key

    def test_default_is_none(self) -> None:
        p = Pipeline(id="issue-1")
        assert p.jira_epic_key is None

    def test_explicit_none(self) -> None:
        p = Pipeline(id="issue-1", jira_epic_key=None)
        assert p.jira_epic_key is None

    def test_empty_string_collapses_to_none(self) -> None:
        """Trimmed-empty strings normalise to None (matches
        ``_validate_jira_ticket``)."""
        p = Pipeline(id="issue-1", jira_epic_key="")
        assert p.jira_epic_key is None

    def test_whitespace_trimmed(self) -> None:
        p = Pipeline(id="issue-1", jira_epic_key="  ENG-1  ")
        assert p.jira_epic_key == "ENG-1"

    @pytest.mark.parametrize(
        "bad",
        [
            "lowercase-1",
            "ENG_1",  # underscore instead of dash
            "ENG-",
            "-1",
            "ENG-abc",
            "123-4",  # leading digit on project key
            "ЕNG-1",  # Cyrillic E
            "ENG 1",  # space
            "ENG-1-2",  # extra suffix
        ],
    )
    def test_invalid_shapes_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            Pipeline(id="issue-1", jira_epic_key=bad)

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Pipeline(id="issue-1", jira_epic_key=42)  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# Pipeline.jira_effective_mode
# -----------------------------------------------------------------------------


class TestPipelineJiraEffectiveMode:
    """``Literal['fresh', 'reassess'] | None`` — pin the three accepted
    values and reject anything else."""

    def test_default_is_none(self) -> None:
        p = Pipeline(id="issue-1")
        assert p.jira_effective_mode is None

    @pytest.mark.parametrize("mode", ["fresh", "reassess"])
    def test_valid_modes_accepted(self, mode: str) -> None:
        p = Pipeline(id="issue-1", jira_effective_mode=mode)
        assert p.jira_effective_mode == mode

    def test_none_accepted(self) -> None:
        p = Pipeline(id="issue-1", jira_effective_mode=None)
        assert p.jira_effective_mode is None

    def test_auto_not_a_valid_effective_mode(self) -> None:
        """``auto`` is the *input* knob to ``submit_task`` — once the probe
        resolves, the persisted value is always one of {fresh, reassess, None}.
        Storing ``auto`` would be a schema bug."""
        with pytest.raises(ValidationError):
            Pipeline(id="issue-1", jira_effective_mode="auto")

    @pytest.mark.parametrize("bad", ["FRESH", "Reassess", "", "bogus", "in_progress"])
    def test_invalid_modes_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            Pipeline(id="issue-1", jira_effective_mode=bad)

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Pipeline(id="issue-1", jira_effective_mode=1)  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# Pipeline.jira_parent_epic_key
# -----------------------------------------------------------------------------


class TestPipelineJiraParentEpicKey:
    """Same regex validator as ``jira_ticket`` / ``jira_epic_key``."""

    def test_default_is_none(self) -> None:
        p = Pipeline(id="issue-1")
        assert p.jira_parent_epic_key is None

    @pytest.mark.parametrize("key", ["ENG-1234", "K-1", "PROJ_X-42"])
    def test_valid_keys_accepted(self, key: str) -> None:
        p = Pipeline(id="issue-1", jira_parent_epic_key=key)
        assert p.jira_parent_epic_key == key

    def test_whitespace_trimmed(self) -> None:
        p = Pipeline(id="issue-1", jira_parent_epic_key=" ENG-2 ")
        assert p.jira_parent_epic_key == "ENG-2"

    def test_empty_collapses_to_none(self) -> None:
        p = Pipeline(id="issue-1", jira_parent_epic_key="")
        assert p.jira_parent_epic_key is None

    @pytest.mark.parametrize(
        "bad",
        ["lowercase-1", "ENG-", "ENG-abc", "ЕNG-1", "ENG_1"],
    )
    def test_invalid_keys_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            Pipeline(id="issue-1", jira_parent_epic_key=bad)

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Pipeline(
                id="issue-1",
                description="t",
                jira_parent_epic_key=["ENG-1"],  # type: ignore[arg-type]
            )

    def test_independent_from_jira_epic_key(self) -> None:
        """``jira_parent_epic_key`` and ``jira_epic_key`` validate
        independently — both may be set (a child pipeline knows both its
        own epic AND its parent)."""
        p = Pipeline(
            id="issue-1",
            description="t",
            jira_epic_key="ENG-100",
            jira_parent_epic_key="ENG-50",
        )
        assert p.jira_epic_key == "ENG-100"
        assert p.jira_parent_epic_key == "ENG-50"


# -----------------------------------------------------------------------------
# PipelinePhase.PLAN_STOPPED
# -----------------------------------------------------------------------------


class TestPlanStoppedEnum:
    """``PLAN_STOPPED`` is the terminal-without-PR phase for Jira-epic
    pipelines that chose Stop-after-plan."""

    def test_enum_member_exists(self) -> None:
        assert hasattr(PipelinePhase, "PLAN_STOPPED")

    def test_enum_value_is_plan_stopped(self) -> None:
        assert PipelinePhase.PLAN_STOPPED.value == "plan_stopped"

    def test_str_enum_round_trip(self) -> None:
        """``PipelinePhase`` is a ``StrEnum`` — value <-> member round-trip."""
        assert PipelinePhase("plan_stopped") is PipelinePhase.PLAN_STOPPED

    def test_distinct_from_other_phases(self) -> None:
        """``PLAN_STOPPED`` must NOT collide with the other phase values."""
        others = {
            PipelinePhase.REFINE,
            PipelinePhase.PLAN,
            PipelinePhase.IMPLEMENT,
            PipelinePhase.PR,
        }
        assert PipelinePhase.PLAN_STOPPED not in others

    def test_usable_as_pipeline_current_phase(self) -> None:
        """Operators flip ``current_phase`` to ``PLAN_STOPPED`` when the
        pipeline terminates without a PR — pin that the Pipeline model
        accepts the value."""
        p = Pipeline(
            id="issue-1557",
            description="t",
            current_phase=PipelinePhase.PLAN_STOPPED,
        )
        assert p.current_phase == PipelinePhase.PLAN_STOPPED
        # The StrEnum value survives a string round-trip.
        assert p.current_phase.value == "plan_stopped"

    def test_constructed_from_string_value(self) -> None:
        """JSON deserialisation flows pass the bare string value — the
        Pydantic field must accept it."""
        p = Pipeline(
            id="issue-1557",
            description="t",
            current_phase="plan_stopped",  # type: ignore[arg-type]
        )
        assert p.current_phase == PipelinePhase.PLAN_STOPPED
