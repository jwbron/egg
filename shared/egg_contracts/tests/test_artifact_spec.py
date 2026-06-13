"""Mandatory consistency suite for the artifact spec module (#3077 slice-2).

The artifact spec at :mod:`egg_contracts.artifact_spec` is the single
declarative registry of coordination artifacts that propose-time
validation, the gateway artifact-read endpoint, and prompt rendering all
derive from. The spec only matters if it stays in lockstep with the
existing path knowledge replicas that admit / construct those paths in
production:

* the gateway's phase-write gate
  (:class:`gateway.phase_filter.PhaseFilter`);
* the gateway-mirror in the sandbox
  (:func:`egg_restrictions.phase_patterns.phase_file_verdict`);
* the orchestrator's ``_get_draft_path`` helper in
  ``orchestrator/routes/pipelines.py`` (used by refine / plan
  propose validation in ``orchestrator/routes/signals.py``); and
* the prompt f-string literals in ``orchestrator/routes/pipelines.py``
  that name draft / agent-output paths to the agent.

This module is the refine-risk-1 mitigation from the #3077 plan: any
future drift in any of these replicas fails CI here instead of
reproducing #3016 (a draft committed to a path the gateway/validator
ignored). The mutation tests (see :class:`TestSpecMutationFailsGate`)
demonstrate that the gate-admission assertions are not trivially-green.

The pure registry / round-trip behaviour of the spec lives alongside
its consumers in this same file so that any breakage of the contract
the consumers depend on is caught in a single suite.
"""

from __future__ import annotations

import re
from pathlib import Path

# Cross-replica imports — these are the surfaces the spec must agree with.
# ``orchestrator`` and ``gateway`` sit on PYTHONPATH alongside ``shared``
# in the project's test runner (Makefile: ``PYTHONPATH := shared:gateway:
# orchestrator``). The imports below intentionally go through that path
# rather than vendoring duplicate logic — the whole point of the suite
# is to assert that the spec stays in lockstep with the real consumers.
#
# ``phase_filter`` is imported bare (not ``gateway.phase_filter``)
# because the gateway tests already use the bare form
# (see gateway/tests/test_phase_filter.py and the conftest comment that
# the gateway/ tree is loaded onto sys.path directly rather than as a
# package). ``routes.pipelines`` is reachable as a package because the
# orchestrator path itself is on sys.path under ``make test`` /
# ``make test-all``.
import phase_filter
import pytest
from egg_restrictions.phase_patterns import phase_file_verdict
from routes.pipelines import _get_draft_path

# The spec module is the unit under test. ``all_specs`` is aliased on
# import to avoid colliding with the pytest fixture of the same name —
# the fixture exists so dependent tests can name their parameter after
# the registry concept, while the underlying source of truth is the
# module function.
from egg_contracts.artifact_spec import (
    ArtifactSpec,
    resolve_artifact_path,
    spec_by_name,
    specs_for,
)
from egg_contracts.artifact_spec import all_specs as registered_specs
from egg_contracts.models import PipelinePhase

PhaseFilter = phase_filter.PhaseFilter

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


# Both identifier shapes the orchestrator threads into ``_get_draft_path``
# / ``_pipeline_identifier``: an integer issue number (the common case)
# and a string pipeline id (the qualifier-bearing re-run case, e.g.
# ``issue-1557-v2``).
_INT_IDENTIFIER: int = 3077
_STR_IDENTIFIER: str = "issue-1557-v2"

_IDENTIFIERS: tuple[int | str, ...] = (_INT_IDENTIFIER, _STR_IDENTIFIER)


@pytest.fixture(scope="module")
def all_specs() -> tuple[ArtifactSpec, ...]:
    """Return every registered ArtifactSpec row.

    Delegates to :func:`egg_contracts.artifact_spec.all_specs` (aliased
    here as ``registered_specs`` to avoid colliding with this fixture's
    name) so a future row added to the registry is automatically
    exercised by every consistency / mutation test that consumes the
    fixture, even if its producer role is outside the refine + plan
    roster slice-2 originally covered. ``specs_for`` projections are
    exercised independently by :meth:`TestResolutionRoundTrip.
    test_specs_for_plan_draft_is_singleton`.
    """
    specs = registered_specs()
    assert specs, "no specs registered — registry is empty"
    return specs


@pytest.fixture(scope="module")
def _gateway_phase_filter() -> PhaseFilter:
    # Module-scoped so consistency-A and mutation tests share one
    # instance — ``PhaseFilter()`` is cheap but its glob compilation
    # touches the filesystem, and we'd rather not re-do that per test.
    return PhaseFilter()


# ---------------------------------------------------------------------------
# Registry shape / round-trip
# ---------------------------------------------------------------------------


class TestRegistryShape:
    """Every spec row is well-formed per the ``task-2-1`` contract."""

    def test_every_row_is_frozen(self, all_specs: tuple[ArtifactSpec, ...]) -> None:
        # The contract specifies frozen dataclass rows. We probe by
        # attempting to mutate ``path_template`` — a frozen dataclass
        # raises ``FrozenInstanceError`` (a ``dataclasses`` subclass of
        # ``AttributeError``); anything else (e.g. a silent overwrite)
        # means the rows are not immutable.
        for spec in all_specs:
            with pytest.raises(AttributeError):
                spec.path_template = "tampered"  # type: ignore[misc]

    def test_every_row_has_single_producer_and_nonempty_consumers(
        self, all_specs: tuple[ArtifactSpec, ...]
    ) -> None:
        for spec in all_specs:
            assert isinstance(spec.producer_role, str) and spec.producer_role, (
                f"{spec.name}: producer_role must be a non-empty string"
            )
            assert spec.consumer_roles, f"{spec.name}: consumer_roles must be non-empty"
            # Defensive: producer should not list itself as a consumer
            # via the same row (the row exists precisely because there
            # is an inter-role handoff).
            assert spec.producer_role not in tuple(spec.consumer_roles), (
                f"{spec.name}: producer cannot be its own consumer in the same row"
            )

    def test_every_row_targets_refine_or_plan(self, all_specs: tuple[ArtifactSpec, ...]) -> None:
        # ``task-2-1`` registers analysis-draft (refine) + plan-draft,
        # architect-output, architect-slices, risk-analyst-output (plan).
        # Slice-2 adds nothing else; later slices may extend, but they
        # must still pass the gate-admission tests below — which
        # currently only cover refine + plan phase gates.
        for spec in all_specs:
            assert spec.phase in {"refine", "plan"}, (
                f"{spec.name}: phase {spec.phase!r} outside refine/plan"
            )

    def test_template_uses_identifier_placeholder(
        self, all_specs: tuple[ArtifactSpec, ...]
    ) -> None:
        for spec in all_specs:
            assert "{identifier}" in spec.path_template, (
                f"{spec.name}: path_template must key on {{identifier}}"
            )

    def test_expected_rows_are_registered(self, all_specs: tuple[ArtifactSpec, ...]) -> None:
        # Pin the registered names so a silent drop (e.g. a refactor
        # that removes architect-slices) fails here loudly rather than
        # weakening every per-row test into a no-op iteration.
        expected = {
            "analysis-draft",
            "plan-draft",
            "architect-output",
            "architect-slices",
            "risk-analyst-output",
        }
        registered = {spec.name for spec in all_specs}
        missing = expected - registered
        assert not missing, f"missing spec rows: {sorted(missing)}"


class TestResolutionRoundTrip:
    """``resolve_artifact_path`` and ``spec_by_name`` round-trip every row."""

    def test_spec_by_name_round_trips(self, all_specs: tuple[ArtifactSpec, ...]) -> None:
        # Identity (``is``) is the right check here, not structural
        # equality: ``_BY_NAME`` stores the same instance built at
        # module import time, so a rebuilt spec with matching fields
        # would compare ``==`` (frozen dataclasses get structural eq for
        # free) but would *not* be ``is`` — and that case would defeat
        # the round-trip's intent of pinning the registry as the source
        # of truth.
        for spec in all_specs:
            assert spec_by_name(spec.name) is spec

    @pytest.mark.parametrize("identifier", _IDENTIFIERS, ids=("int", "str"))
    def test_resolve_artifact_path_matches_template(
        self,
        all_specs: tuple[ArtifactSpec, ...],
        identifier: int | str,
    ) -> None:
        for spec in all_specs:
            assert resolve_artifact_path(spec.name, identifier) == spec.path_template.format(
                identifier=identifier
            )

    def test_resolve_artifact_path_unknown_name_raises(self) -> None:
        # The strict-resolution contract is what #3077 calls out as the
        # blocking prerequisite of #3002: unknown names must fail loudly
        # rather than return a None / empty string consumers can swallow.
        with pytest.raises((KeyError, ValueError)):
            resolve_artifact_path("not-a-real-artifact", _INT_IDENTIFIER)

    def test_known_concrete_paths(self) -> None:
        # The acceptance criterion in ``task-2-1`` pins two concrete
        # outputs explicitly; this test would have caught the path
        # drift in #3016 (draft committed at one path, validator
        # reading another).
        assert resolve_artifact_path("plan-draft", "3077") == ".egg-state/drafts/3077-plan.md"
        assert (
            resolve_artifact_path("analysis-draft", "3077") == ".egg-state/drafts/3077-analysis.md"
        )

    def test_specs_for_plan_draft_is_singleton(self) -> None:
        # ``task-2-1`` acceptance: ``specs_for("plan", "task_planner")``
        # returns exactly the plan-draft row.
        rows = tuple(specs_for("plan", "task_planner"))
        assert len(rows) == 1
        assert rows[0].name == "plan-draft"

    def test_specs_for_artifactless_role_returns_empty(self) -> None:
        # Roles that have no registered artifact must return an empty
        # iterable (used by slice-3 propose validation to skip cleanly
        # — see ``task-3-1`` "roles with no registered artifact
        # validate nothing").
        assert tuple(specs_for("implement", "coder")) == ()
        assert tuple(specs_for("plan", "coder")) == ()


# ---------------------------------------------------------------------------
# Consistency (a): every spec path is admitted by BOTH phase-gate replicas
# ---------------------------------------------------------------------------


class TestConsistencyA_PhaseFilterAdmits:
    """Every spec path is admitted by ``gateway/phase_filter.py``.

    The gateway is the authoritative phase-write gate; if a spec path
    is rejected here, producer pushes of that artifact will be silently
    refused — the exact #3016 failure mode the spec is designed to
    eliminate.
    """

    @pytest.mark.parametrize("identifier", _IDENTIFIERS, ids=("int", "str"))
    def test_gateway_phase_filter_admits_every_spec_path(
        self,
        all_specs: tuple[ArtifactSpec, ...],
        _gateway_phase_filter: PhaseFilter,
        identifier: int | str,
    ) -> None:
        for spec in all_specs:
            path = resolve_artifact_path(spec.name, identifier)
            phase = PipelinePhase(spec.phase)
            result = _gateway_phase_filter.check_phase_file_restrictions(phase, [path])
            assert result.allowed, (
                f"{spec.name}: gateway phase filter rejected {path!r} for "
                f"phase {spec.phase!r}: {getattr(result, 'message', None)!r}"
            )


class TestConsistencyA_PhasePatternsAdmits:
    """Every spec path is admitted by the sandbox-side gateway mirror."""

    @pytest.mark.parametrize("identifier", _IDENTIFIERS, ids=("int", "str"))
    def test_phase_patterns_mirror_admits_every_spec_path(
        self,
        all_specs: tuple[ArtifactSpec, ...],
        identifier: int | str,
    ) -> None:
        for spec in all_specs:
            path = resolve_artifact_path(spec.name, identifier)
            allowed, reason = phase_file_verdict(spec.phase, path)
            assert allowed, (
                f"{spec.name}: phase_patterns mirror rejected {path!r} for "
                f"phase {spec.phase!r}: {reason!r}"
            )


# ---------------------------------------------------------------------------
# Consistency (b): ``_get_draft_path`` equals spec resolution
# ---------------------------------------------------------------------------


class TestConsistencyB_GetDraftPathEquality:
    """``_get_draft_path`` must equal ``resolve_artifact_path`` for refine + plan.

    Slice-3 replaces ``_get_draft_path``'s body with a spec call; this
    test pins the pre-condition (and continues to pin it post-rewrite
    so a regression to a separate path table fails here).

    Both identifier shapes are exercised because
    ``_pipeline_identifier`` prefers the integer issue number when the
    pipeline id is the bare ``issue-<N>`` form and falls back to the
    pipeline id when a qualifier is present (#3068's versioned-rerun
    case). The spec must handle both.
    """

    def _identifier_from(self, identifier: int | str) -> tuple[int | None, str | None]:
        # Map the test identifier onto the (issue_number, pipeline_id)
        # kwargs that ``_get_draft_path`` accepts. An int identifier
        # is fed as ``issue_number``; a string identifier is fed as
        # ``pipeline_id`` (the ``_pipeline_identifier`` qualifier branch
        # — ``issue-N-suffix`` keys on pipeline_id).
        if isinstance(identifier, int):
            return identifier, None
        return None, identifier

    @pytest.mark.parametrize(
        "phase_to_spec",
        [("refine", "analysis-draft"), ("plan", "plan-draft")],
        ids=("refine", "plan"),
    )
    @pytest.mark.parametrize("identifier", _IDENTIFIERS, ids=("int", "str"))
    def test_get_draft_path_equals_spec_resolution(
        self,
        phase_to_spec: tuple[str, str],
        identifier: int | str,
    ) -> None:
        phase, spec_name = phase_to_spec
        issue_number, pipeline_id = self._identifier_from(identifier)
        legacy = _get_draft_path(phase, issue_number=issue_number, pipeline_id=pipeline_id)
        spec_resolved = resolve_artifact_path(spec_name, identifier)
        assert legacy == spec_resolved, (
            f"{spec_name}: _get_draft_path({phase!r}) → {legacy!r} but "
            f"spec resolved to {spec_resolved!r} (identifier={identifier!r})"
        )


# ---------------------------------------------------------------------------
# Consistency (c): prompt f-string literals match spec resolution
# ---------------------------------------------------------------------------


class TestConsistencyC_PromptDerivesFromSpec:
    """The agent-facing prompts in pipelines.py derive their agent-output
    paths via :func:`resolve_artifact_path` instead of inlining literals.

    Pre-slice-3 of #3077 this test asserted that the prompt f-string
    literals (``f".egg-state/agent-outputs/{_identifier}-architect-output.json"``)
    matched the spec resolution. Slice-3 retires those literals and
    replaces them with assignments at the top of the prompt builder
    (``_architect_output_path = resolve_artifact_path("architect-output", _identifier)``,
    etc.), then interpolates the variables into the prose
    (``f"Write your analysis to `{_architect_output_path}`."``).  The
    spec is now the single source of truth that the prompt
    construction *calls into*, not a parallel copy of the path knowledge.

    This test pins the new invariant: every registered
    ``agent-outputs/`` spec must appear as a ``resolve_artifact_path("<name>", …)``
    call in pipelines.py, and no literal ``.egg-state/agent-outputs/{_identifier}-…``
    string may sneak back in (the ratchet against #3016-style drift).

    Drafts under ``.egg-state/drafts/`` are constructed via
    ``_get_draft_path``, which itself routes through the spec
    (covered by Consistency-B above).
    """

    PIPELINES_PATH = (
        Path(__file__).resolve().parents[3] / "orchestrator" / "routes" / "pipelines.py"
    )

    # Ratchet against a regression: forbid raw
    # ``.egg-state/agent-outputs/{_identifier}-…`` f-string literals
    # from creeping back into pipelines.py once the slice-3 rewrite has
    # landed. The check intentionally matches only the templated form
    # (with the literal ``{_identifier}`` placeholder); resolved paths
    # appearing in test fixtures or doc strings — e.g. a sample
    # ``3077-architect-output.json`` — would not match and remain free
    # to be referenced.
    _BANNED_LITERAL_RE = re.compile(
        r"\.egg-state/agent-outputs/\{_identifier\}-[A-Za-z0-9_.-]+\.(?:json|yaml)"
    )

    @pytest.fixture(scope="class")
    def pipelines_text(self) -> str:
        return self.PIPELINES_PATH.read_text()

    def test_pipelines_py_is_readable(self) -> None:
        assert self.PIPELINES_PATH.exists(), f"missing: {self.PIPELINES_PATH} — has the file moved?"

    def test_no_raw_agent_output_literals_remain(self, pipelines_text: str) -> None:
        # Slice-3 of #3077 removed every
        # ``.egg-state/agent-outputs/{_identifier}-…`` literal from the
        # prompt-construction code; new ones must not be reintroduced
        # without first registering the artifact in the spec AND
        # consuming the path via ``resolve_artifact_path``. A literal
        # here is the slice-1 / slice-2 #3016-style drift symptom.
        offenders = self._BANNED_LITERAL_RE.findall(pipelines_text)
        assert not offenders, (
            "pipelines.py reintroduced raw agent-output path literals; "
            "use resolve_artifact_path(<name>, identifier) instead so the "
            "spec stays the single source of truth: "
            f"{sorted(set(offenders))!r}"
        )

    def test_every_agent_output_spec_has_resolve_call(
        self,
        all_specs: tuple[ArtifactSpec, ...],
        pipelines_text: str,
    ) -> None:
        # Reverse direction: every registered ``agent-outputs/`` spec
        # must appear as a ``resolve_artifact_path("<name>", …)`` call
        # in pipelines.py. Drafts under ``.egg-state/drafts/`` are
        # constructed via ``_get_draft_path`` (Consistency-B above), so
        # this only governs the ``agent-outputs/`` rows.
        for spec in all_specs:
            if not spec.path_template.startswith(".egg-state/agent-outputs/"):
                continue
            # Accept either quoting style so a future formatter pass
            # (single → double quotes or back) doesn't break the
            # ratchet for cosmetic reasons.
            needles = (
                f'resolve_artifact_path("{spec.name}"',
                f"resolve_artifact_path('{spec.name}'",
            )
            assert any(n in pipelines_text for n in needles), (
                f"{spec.name}: expected `resolve_artifact_path(<name>, …)` "
                f"call in pipelines.py — drift between spec and prompt "
                f"rendering will silently land at the agent"
            )


# ---------------------------------------------------------------------------
# Mutation: a deliberate template mutation must fail (a)
# ---------------------------------------------------------------------------


def _mutate_template(template: str, mutation: str) -> str:
    """Return ``template`` with a structural mutation applied.

    The mutations target the dimensions the phase gate actually
    discriminates on — directory prefix and (where present) the
    ``analysis``/``plan`` token in the filename. A no-op mutation
    (renaming a non-discriminating segment) would let the test pass
    spuriously, defeating its purpose as the refine-risk-1 ratchet.
    """
    if mutation == "wrong_directory":
        # Move the artifact out of ``.egg-state/drafts/`` /
        # ``.egg-state/agent-outputs/`` entirely.
        return template.replace(".egg-state/drafts/", ".egg-state/wrongdir/").replace(
            ".egg-state/agent-outputs/", ".egg-state/wrongdir/"
        )
    if mutation == "wrong_token":
        # Strip the ``analysis`` / ``plan`` token from drafts so the
        # refine glob ``*analysis*`` / plan glob ``*plan*`` no longer
        # matches. Agent-output rows have no such token in the gate, so
        # we strip the directory instead (already covered above);
        # callers parametrize over rows where this is meaningful.
        return template.replace("-analysis", "-misnamed").replace("-plan", "-misnamed")
    raise AssertionError(f"unknown mutation {mutation!r}")


class TestSpecMutationFailsGate:
    """A mutated spec template is demonstrably rejected by the gate.

    Without this test, every gate-admission assertion above could be
    silently green even if the gate were a permissive default. By
    showing that *some* mutation flips the verdict, we pin the
    admission tests as load-bearing rather than tautological.
    """

    @pytest.mark.parametrize(
        "mutation",
        ["wrong_directory"],
        ids=["wrong_directory"],
    )
    @pytest.mark.parametrize("identifier", _IDENTIFIERS, ids=("int", "str"))
    def test_wrong_directory_is_rejected_by_gateway(
        self,
        all_specs: tuple[ArtifactSpec, ...],
        _gateway_phase_filter: PhaseFilter,
        mutation: str,
        identifier: int | str,
    ) -> None:
        for spec in all_specs:
            mutated_template = _mutate_template(spec.path_template, mutation)
            assert mutated_template != spec.path_template, (
                f"{spec.name}: mutation {mutation!r} was a no-op — adjust "
                f"the mutation list so the test cannot pass spuriously"
            )
            mutated_path = mutated_template.format(identifier=identifier)
            phase = PipelinePhase(spec.phase)
            result = _gateway_phase_filter.check_phase_file_restrictions(phase, [mutated_path])
            assert not result.allowed, (
                f"{spec.name}: mutated path {mutated_path!r} was wrongly "
                f"admitted by the gateway phase filter — gate-admission "
                f"assertions above would be trivially green"
            )

    @pytest.mark.parametrize(
        "mutation",
        ["wrong_directory"],
        ids=["wrong_directory"],
    )
    @pytest.mark.parametrize("identifier", _IDENTIFIERS, ids=("int", "str"))
    def test_wrong_directory_is_rejected_by_phase_patterns(
        self,
        all_specs: tuple[ArtifactSpec, ...],
        mutation: str,
        identifier: int | str,
    ) -> None:
        for spec in all_specs:
            mutated_template = _mutate_template(spec.path_template, mutation)
            mutated_path = mutated_template.format(identifier=identifier)
            allowed, _reason = phase_file_verdict(spec.phase, mutated_path)
            assert not allowed, (
                f"{spec.name}: mutated path {mutated_path!r} was wrongly "
                f"admitted by phase_patterns — mirror assertions would "
                f"be trivially green"
            )

    @pytest.mark.parametrize("identifier", _IDENTIFIERS, ids=("int", "str"))
    def test_wrong_token_is_rejected_by_gateway_for_drafts(
        self,
        all_specs: tuple[ArtifactSpec, ...],
        _gateway_phase_filter: PhaseFilter,
        identifier: int | str,
    ) -> None:
        # The ``analysis`` / ``plan`` token only discriminates within
        # ``.egg-state/drafts/`` (the gate's allowed_patterns key on
        # ``*analysis*`` / ``*plan*``). Skip agent-output rows here:
        # the ``wrong_directory`` test above covers their gate
        # discrimination.
        drafts_specs = [s for s in all_specs if s.path_template.startswith(".egg-state/drafts/")]
        assert drafts_specs, "expected at least one drafts/ row"
        for spec in drafts_specs:
            mutated_template = _mutate_template(spec.path_template, "wrong_token")
            mutated_path = mutated_template.format(identifier=identifier)
            phase = PipelinePhase(spec.phase)
            result = _gateway_phase_filter.check_phase_file_restrictions(phase, [mutated_path])
            assert not result.allowed, (
                f"{spec.name}: token-mutated path {mutated_path!r} was wrongly "
                f"admitted by the gateway — the analysis/plan token does "
                f"not discriminate as expected"
            )


# ---------------------------------------------------------------------------
# Defensive: spec stays decoupled from the orchestrator (task-2-1)
# ---------------------------------------------------------------------------


class TestSpecModuleIsPure:
    """The spec module must not pull orchestrator/gateway into its import graph.

    ``task-2-1`` is explicit: ``Pure Python, no orchestrator/gateway
    imports, no new config format``. The slice-3 propose validator
    imports the spec at module load (``signals.py``), and pulling the
    orchestrator into the spec's import graph would make the spec
    unloadable from sandbox / gateway processes.
    """

    def test_spec_module_imports_are_pure(self) -> None:
        import egg_contracts.artifact_spec as mod

        # Walk the module's globals for imported submodules. A clean
        # module exposes ``ArtifactSpec``, the resolver helpers, and
        # whatever stdlib / egg_contracts internals it needs — no
        # references to ``orchestrator.*`` / ``routes.*`` /
        # ``gateway.*`` / ``egg_restrictions.*``.
        forbidden_prefixes = ("orchestrator", "routes", "gateway", "egg_restrictions")
        offenders: list[tuple[str, str]] = []
        for attr_name, attr_value in vars(mod).items():
            module_name = getattr(attr_value, "__module__", None) or getattr(
                attr_value, "__name__", ""
            )
            if not isinstance(module_name, str):
                continue
            for prefix in forbidden_prefixes:
                if module_name == prefix or module_name.startswith(prefix + "."):
                    offenders.append((attr_name, module_name))
                    break
        assert not offenders, f"egg_contracts.artifact_spec imports forbidden modules: {offenders}"


# ---------------------------------------------------------------------------
# Misc safety
# ---------------------------------------------------------------------------


def test_artifact_spec_type_is_a_dataclass() -> None:
    # The contract says ``frozen`` dataclass rows; assert the type
    # exposes the dataclass machinery rather than relying on duck-typing.
    import dataclasses

    assert dataclasses.is_dataclass(ArtifactSpec), (
        "ArtifactSpec must be a dataclass (task-2-1 contract)"
    )
    params = getattr(ArtifactSpec, "__dataclass_params__", None)
    assert params is not None and getattr(params, "frozen", False), (
        "ArtifactSpec must be frozen=True (task-2-1 contract)"
    )


def test_module_exports_are_callable() -> None:
    # Smoke test on the public surface so a typo in the helper signature
    # (e.g. positional-only vs keyword-only) surfaces here rather than
    # at every consumer call site.
    assert callable(resolve_artifact_path)
    assert callable(specs_for)
    assert callable(spec_by_name)
