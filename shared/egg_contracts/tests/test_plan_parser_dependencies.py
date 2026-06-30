"""Tests for plan parser dependencies field propagation.

Covers:
- ParsedPhase.dependencies -> Slice.dependencies via to_contract_slice()
- Various dependency formats (slice-N, phase-N legacy, numeric, comma-separated)
- Empty/missing dependencies

#2137: Renamed Phase → Slice. The canonical output is ``slice-N``;
legacy ``phase-N`` input strings are normalised to ``slice-N`` so the
post-rename DAG resolves uniformly. The legacy method
``to_contract_phase()`` survives as an alias of ``to_contract_slice()``
and the assertions below cover both the canonical and legacy entry
points so a future caller flip doesn't regress either.
"""

from __future__ import annotations

from egg_contracts.plan_parser import ParsedPhase, ParsedTask


class TestToContractPhaseDependencies:
    """Tests for to_contract_slice() dependency propagation.

    The class name keeps the historical ``Phase`` token so external
    references (e.g., ``pytest -k Phase``) keep working during the
    transition window.
    """

    def test_empty_dependencies(self):
        """Slice with empty dependencies produces empty list."""
        phase = ParsedPhase(
            number=1,
            name="Phase 1",
            goal="Do something",
            tasks=[],
            dependencies="",
        )
        contract_slice = phase.to_contract_slice()
        assert contract_slice.dependencies == []

    def test_single_phase_id_dependency(self):
        """Legacy ``phase-N`` deps are normalised to canonical ``slice-N``."""
        phase = ParsedPhase(
            number=2,
            name="Phase 2",
            goal="Do something",
            tasks=[],
            dependencies="phase-1",
        )
        contract_slice = phase.to_contract_slice()
        # Post-#2137: the canonical form is ``slice-N``. ``phase-N``
        # input is migrated automatically so the post-rename DAG
        # resolves uniformly.
        assert contract_slice.dependencies == ["slice-1"]

    def test_canonical_slice_id_dependency_preserved(self):
        """Canonical ``slice-N`` deps pass through unchanged (#2137)."""
        phase = ParsedPhase(
            number=2,
            name="Phase 2",
            goal="Do something",
            tasks=[],
            dependencies="slice-1",
        )
        contract_slice = phase.to_contract_slice()
        assert contract_slice.dependencies == ["slice-1"]

    def test_multiple_comma_separated_dependencies(self):
        """Comma-separated dependencies are all parsed and normalised."""
        phase = ParsedPhase(
            number=4,
            name="Phase 4",
            goal="Do something",
            tasks=[],
            dependencies="phase-1, phase-2, phase-3",
        )
        contract_slice = phase.to_contract_slice()
        # All three legacy ``phase-N`` deps are rewritten to ``slice-N``.
        assert contract_slice.dependencies == ["slice-1", "slice-2", "slice-3"]

    def test_numeric_dependencies_normalized(self):
        """Numeric dependencies are normalised to ``slice-N`` (#2137)."""
        phase = ParsedPhase(
            number=3,
            name="Phase 3",
            goal="Do something",
            tasks=[],
            dependencies="1, 2",
        )
        contract_slice = phase.to_contract_slice()
        # Pre-#2137 this was ``phase-1`` / ``phase-2``; post-rename
        # the canonical form is ``slice-N``.
        assert contract_slice.dependencies == ["slice-1", "slice-2"]

    def test_contract_phase_id_format(self):
        """Contract slice ID follows ``slice-N`` format (#2137)."""
        phase = ParsedPhase(
            number=5,
            name="Phase 5",
            goal="Do something",
            tasks=[
                ParsedTask(
                    id="TASK-5-1",
                    phase_number=5,
                    task_number=1,
                    description="test",
                    acceptance_criteria="works",
                ),
            ],
            dependencies="phase-1",
        )
        contract_slice = phase.to_contract_slice()
        # Canonical post-rename id is ``slice-5``.
        assert contract_slice.id == "slice-5"

    def test_tasks_preserved_with_dependencies(self):
        """Tasks are correctly converted alongside dependencies."""
        phase = ParsedPhase(
            number=1,
            name="Phase 1",
            goal="Do something",
            tasks=[
                ParsedTask(
                    id="TASK-1-1",
                    phase_number=1,
                    task_number=1,
                    description="First task",
                    acceptance_criteria="passes",
                ),
                ParsedTask(
                    id="TASK-1-2",
                    phase_number=1,
                    task_number=2,
                    description="Second task",
                    acceptance_criteria="passes",
                ),
            ],
            dependencies="phase-2",
        )
        contract_slice = phase.to_contract_slice()
        assert len(contract_slice.tasks) == 2
        assert contract_slice.dependencies == ["slice-2"]

    def test_list_format_dependencies(self):
        """Dependencies provided as a list are handled and normalised."""
        phase = ParsedPhase(
            number=2,
            name="Phase 2",
            goal="Do something",
            tasks=[],
        )
        # Manually set dependencies as a list (as it might come from YAML)
        phase.dependencies = ["phase-1", "phase-3"]  # type: ignore[assignment]
        contract_slice = phase.to_contract_slice()
        # Post-#2137: legacy ``phase-N`` list entries are migrated.
        assert contract_slice.dependencies == ["slice-1", "slice-3"]


class TestLegacyToContractPhaseAlias:
    """``to_contract_phase()`` survives as an alias of ``to_contract_slice()``.

    Added in #2137 — guarantees that callers who haven't yet flipped
    to the canonical method name keep working. The output is
    indistinguishable from ``to_contract_slice()``.
    """

    def test_alias_returns_canonical_slice(self):
        phase = ParsedPhase(
            number=2,
            name="Phase 2",
            goal="Do something",
            tasks=[],
            dependencies="phase-1",
        )
        legacy = phase.to_contract_phase()
        canonical = phase.to_contract_slice()
        # Same shape, same canonical ids, same migrated deps.
        assert legacy.id == canonical.id == "slice-2"
        assert legacy.dependencies == canonical.dependencies == ["slice-1"]


class TestSliceRepoParsing:
    """#3393: per-slice ``repo`` key flows plan → ParsedPhase → Slice."""

    def test_repo_propagates_to_contract_slice(self):
        phase = ParsedPhase(number=1, name="s", goal="g", repo="owner/schema")
        assert phase.to_contract_slice().repo == "owner/schema"

    def test_blank_repo_normalises_to_none(self):
        assert ParsedPhase(number=1, name="s", goal="g", repo="  ").to_contract_slice().repo is None

    def test_absent_repo_defaults_to_none(self):
        assert ParsedPhase(number=1, name="s", goal="g").to_contract_slice().repo is None

    def test_repo_key_parsed_from_yaml_tasks_fence(self):
        from egg_contracts.plan_parser import parse_plan

        plan = (
            "# Plan\n\n"
            "```yaml\n"
            "# yaml-tasks\n"
            "slices:\n"
            "  - id: slice-1\n"
            "    name: schema bump\n"
            "    goal: add v2 schema\n"
            "    repo: owner/schema\n"
            "    tasks:\n"
            "      - id: task-1-1\n"
            "        description: add schema\n"
            "        acceptance_criteria: it exists\n"
            "        role: coder\n"
            "  - id: slice-2\n"
            "    name: migrate consumer\n"
            "    goal: cut over\n"
            "    dependencies: slice-1\n"
            "    tasks:\n"
            "      - id: task-2-1\n"
            "        description: migrate\n"
            "        acceptance_criteria: done\n"
            "        role: coder\n"
            "```\n"
        )
        result = parse_plan(plan)
        assert result.success, result.error
        by_id = {s.id: s for s in result.to_contract_slices()}
        assert by_id["slice-1"].repo == "owner/schema"
        assert by_id["slice-2"].repo is None
        # `repo` is a recognised key — no unknown-key warning.
        assert not any("repo" in w.message.lower() for w in result.warnings)
