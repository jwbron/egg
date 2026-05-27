"""Tests for egg_contracts.plan_parser module."""

import pytest
from egg_contracts.plan_parser import (
    ParsedPhase,
    ParsedTask,
    ParseResult,
    ParseWarning,
    PlanPreflightError,
    _normalize_optional_string,
    extract_pr_metadata_from_yaml,
    format_warnings_for_comment,
    parse_phases_from_markdown,
    parse_phases_from_yaml,
    parse_plan,
    parse_tasks_from_markdown,
    parse_tasks_from_yaml,
    parse_yaml_code_fence,
    validate_plan_for_implement_phase,
)


class TestParsedTask:
    """Tests for ParsedTask dataclass."""

    def test_to_contract_task(self):
        """Test converting ParsedTask to contract Task model."""
        parsed = ParsedTask(
            id="TASK-1-1",
            phase_number=1,
            task_number=1,
            description="Create schema",
            acceptance_criteria="Schema validates",
            files_affected=["schema.json"],
        )
        task = parsed.to_contract_task()
        assert task.id == "task-1-1"
        assert task.description == "Create schema"
        assert task.acceptance_criteria == "Schema validates"
        assert task.files_affected == ["schema.json"]

    def test_parsed_task_role_defaults_to_none(self):
        """Test that ParsedTask role defaults to None when not specified."""
        parsed = ParsedTask(
            id="TASK-1-1",
            phase_number=1,
            task_number=1,
            description="Create schema",
            acceptance_criteria="Schema validates",
        )
        assert parsed.role is None

    def test_to_contract_task_with_role(self):
        """Test converting ParsedTask with role to contract Task passes role through."""
        parsed = ParsedTask(
            id="TASK-1-1",
            phase_number=1,
            task_number=1,
            description="Write tests",
            acceptance_criteria="Tests pass",
            role="tester",
        )
        task = parsed.to_contract_task()
        assert task.role == "tester"

    def test_to_contract_task_without_role(self):
        """Test converting ParsedTask without role produces Task with role=None."""
        parsed = ParsedTask(
            id="TASK-1-1",
            phase_number=1,
            task_number=1,
            description="Create schema",
            acceptance_criteria="Schema validates",
        )
        task = parsed.to_contract_task()
        assert task.role is None


class TestParsedPhase:
    """Tests for ParsedPhase dataclass."""

    def test_to_contract_phase(self):
        """Test converting ParsedPhase to contract Phase model."""
        parsed = ParsedPhase(
            number=1,
            name="Setup",
            goal="Initialize the project",
            tasks=[
                ParsedTask(
                    id="TASK-1-1",
                    phase_number=1,
                    task_number=1,
                    description="Create schema",
                    acceptance_criteria="Schema validates",
                )
            ],
        )
        phase = parsed.to_contract_phase()
        assert phase.id == "slice-1"
        assert phase.name == "Setup"
        assert len(phase.tasks) == 1


class TestTaskPatternMatching:
    """Tests for task pattern matching in markdown."""

    def test_basic_task_pattern(self):
        """Test parsing basic task pattern."""
        content = "- [TASK-1-1] Create schema — Acceptance: Schema validates"
        tasks, warnings = parse_tasks_from_markdown(content)
        assert len(tasks) == 1
        assert tasks[0].id == "TASK-1-1"
        assert tasks[0].phase_number == 1
        assert tasks[0].task_number == 1
        assert tasks[0].description == "Create schema"
        assert tasks[0].acceptance_criteria == "Schema validates"

    def test_task_with_double_dash(self):
        """Test parsing task with -- instead of em dash."""
        content = "- [TASK-2-3] Add tests -- Acceptance: Tests pass"
        tasks, _ = parse_tasks_from_markdown(content)
        assert len(tasks) == 1
        assert tasks[0].phase_number == 2
        assert tasks[0].task_number == 3

    def test_task_with_single_dash(self):
        """Test parsing task with single dash."""
        content = "- [TASK-1-2] Update docs - Acceptance: Docs render"
        tasks, _ = parse_tasks_from_markdown(content)
        assert len(tasks) == 1
        assert tasks[0].description == "Update docs"

    def test_multiple_tasks(self):
        """Test parsing multiple tasks."""
        content = """
- [TASK-1-1] First task — Acceptance: First done
- [TASK-1-2] Second task — Acceptance: Second done
- [TASK-2-1] Third task — Acceptance: Third done
"""
        tasks, _ = parse_tasks_from_markdown(content)
        assert len(tasks) == 3
        assert tasks[0].id == "TASK-1-1"
        assert tasks[1].id == "TASK-1-2"
        assert tasks[2].id == "TASK-2-1"

    def test_non_list_items_ignored(self):
        """Test that non-list items are ignored."""
        content = """
Some text [TASK-1-1] not in list — Acceptance: ignored

- [TASK-1-2] Valid task — Acceptance: Valid
"""
        tasks, _ = parse_tasks_from_markdown(content)
        assert len(tasks) == 1
        assert tasks[0].id == "TASK-1-2"

    def test_case_insensitive(self):
        """Test that TASK matching is case insensitive."""
        content = "- [task-1-1] Lowercase task — acceptance: works"
        tasks, _ = parse_tasks_from_markdown(content)
        assert len(tasks) == 1


class TestPhasePatternMatching:
    """Tests for phase pattern matching in markdown."""

    def test_basic_phase_header(self):
        """Test parsing basic phase header."""
        content = "### Phase 1: Setup\n\n**Goal**: Initialize everything"
        phases = parse_phases_from_markdown(content)
        assert len(phases) == 1
        assert phases[0].number == 1
        assert phases[0].name == "Setup"
        assert phases[0].goal == "Initialize everything"

    def test_h2_phase_header(self):
        """Test parsing h2 phase header."""
        content = "## Phase 2: Implementation\n\n**Goal**: Build it"
        phases = parse_phases_from_markdown(content)
        assert len(phases) == 1
        assert phases[0].number == 2
        assert phases[0].name == "Implementation"

    def test_multiple_phases(self):
        """Test parsing multiple phases."""
        content = """
### Phase 1: Setup

**Goal**: Set things up

### Phase 2: Build

**Goal**: Build things

### Phase 3: Test

**Goal**: Test things
"""
        phases = parse_phases_from_markdown(content)
        assert len(phases) == 3
        assert phases[0].name == "Setup"
        assert phases[1].name == "Build"
        assert phases[2].name == "Test"


class TestParsePlan:
    """Tests for the main parse_plan function."""

    def test_empty_content(self):
        """Test that empty content fails."""
        result = parse_plan("")
        assert not result.success
        assert "empty" in result.error.lower()

    def test_no_tasks_found(self):
        """Test that document with no tasks fails."""
        result = parse_plan("# Plan\n\nJust some text without tasks.")
        assert not result.success
        assert "no tasks" in result.error.lower()

    def test_complete_plan(self):
        """Test parsing a complete plan document."""
        content = """
# Plan: Test Feature

## Summary

This is a test plan.

### Phase 1: Setup

**Goal**: Initialize the project

**Tasks**:
- [TASK-1-1] Create schema — Acceptance: Schema validates
- [TASK-1-2] Add models — Acceptance: Models work

### Phase 2: Implementation

**Goal**: Build the feature

**Tasks**:
- [TASK-2-1] Implement feature — Acceptance: Feature works
"""
        result = parse_plan(content)
        assert result.success
        assert len(result.phases) == 2
        assert len(result.phases[0].tasks) == 2
        assert len(result.phases[1].tasks) == 1

    def test_phase_without_tasks_gets_placeholder(self):
        """Test that phases without tasks get placeholder."""
        content = """
### Phase 1: Setup

**Goal**: Do something

- [TASK-1-1] A task — Acceptance: Done

### Phase 2: Empty Phase

**Goal**: Nothing here

"""
        result = parse_plan(content)
        assert result.success
        # Phase 2 should have a placeholder task
        assert len(result.phases) == 2
        phase2_tasks = result.phases[1].tasks
        assert len(phase2_tasks) == 1
        assert (
            "placeholder" in phase2_tasks[0].description.lower()
            or "manually" in phase2_tasks[0].description.lower()
        )
        # Should have a warning
        assert any("Phase 2" in w.message for w in result.warnings)

    def test_tasks_assigned_to_correct_phases(self):
        """Test that tasks are correctly assigned to phases."""
        content = """
### Phase 1: First

- [TASK-1-1] Task for phase 1 — Acceptance: Done

### Phase 2: Second

- [TASK-2-1] Task for phase 2 — Acceptance: Done
"""
        result = parse_plan(content)
        assert result.success
        assert result.phases[0].tasks[0].phase_number == 1
        assert result.phases[1].tasks[0].phase_number == 2


class TestYamlFrontMatter:
    """Tests for YAML front matter parsing."""

    def test_yaml_tasks(self):
        """Test parsing tasks from YAML front matter."""
        content = """---
tasks:
  - id: TASK-1-1
    description: Create schema
    acceptance: Schema validates
    files:
      - schema.json
  - id: TASK-1-2
    description: Add models
    acceptance: Models work
---

# Plan

Some content here.
"""
        result = parse_plan(content)
        assert result.success
        assert result.raw_yaml is not None
        # When YAML is present, tasks come from YAML
        total_tasks = sum(len(p.tasks) for p in result.phases)
        assert total_tasks >= 2

    def test_yaml_takes_precedence(self):
        """Test that YAML tasks take precedence over markdown."""
        content = """---
tasks:
  - id: TASK-1-1
    description: From YAML
    acceptance: YAML wins
---

# Plan

- [TASK-1-1] From markdown — Acceptance: Markdown loses
"""
        result = parse_plan(content)
        assert result.success
        # Should find the YAML task
        found_yaml = False
        for phase in result.phases:
            for task in phase.tasks:
                if task.description == "From YAML":
                    found_yaml = True
        assert found_yaml

    def test_legacy_files_as_string_converted_to_list(self):
        """Test that single file string in legacy format is converted to list."""
        content = """---
tasks:
  - id: TASK-1-1
    description: Single file test
    acceptance: Done
    files: single-file.py
---

# Plan
"""
        result = parse_plan(content)
        assert result.success
        # Find the task and check its files_affected
        task = result.phases[0].tasks[0]
        assert task.files_affected == ["single-file.py"]


class TestParseResult:
    """Tests for ParseResult methods."""

    def test_to_contract_phases(self):
        """Test converting all phases to contract format."""
        result = ParseResult(
            success=True,
            phases=[
                ParsedPhase(
                    number=1,
                    name="Setup",
                    goal="Initialize",
                    tasks=[
                        ParsedTask(
                            id="TASK-1-1",
                            phase_number=1,
                            task_number=1,
                            description="Do thing",
                            acceptance_criteria="Thing done",
                        )
                    ],
                )
            ],
        )
        contract_phases = result.to_contract_phases()
        assert len(contract_phases) == 1
        assert contract_phases[0].id == "slice-1"


class TestFormatWarnings:
    """Tests for warning formatting."""

    def test_empty_warnings(self):
        """Test formatting empty warnings list."""
        result = format_warnings_for_comment([])
        assert result == ""

    def test_single_warning(self):
        """Test formatting single warning."""
        warnings = [
            ParseWarning(
                line_number=10,
                message="Phase 2 has no tasks",
                context="Placeholder created",
            )
        ]
        result = format_warnings_for_comment(warnings)
        assert "Line 10" in result
        assert "Phase 2" in result
        assert "Placeholder" in result

    def test_warning_without_line(self):
        """Test formatting warning without line number."""
        warnings = [
            ParseWarning(
                line_number=None,
                message="General warning",
            )
        ]
        result = format_warnings_for_comment(warnings)
        assert "General warning" in result
        assert "Line" not in result


class TestEdgeCases:
    """Tests for edge cases in plan parsing."""

    def test_duplicate_task_ids_in_same_phase(self):
        """Test handling of duplicate task IDs in the same phase."""
        content = """
### Phase 1: Setup

- [TASK-1-1] First task — Acceptance: Done
- [TASK-1-1] Duplicate task — Acceptance: Also done
"""
        result = parse_plan(content)
        assert result.success
        # Both tasks should be parsed (parser doesn't dedupe)
        assert len(result.phases[0].tasks) == 2

    def test_task_references_nonexistent_phase(self):
        """Test that tasks referencing nonexistent phases create the phase."""
        content = """
### Phase 1: Setup

- [TASK-3-1] Task for phase 3 — Acceptance: Done
"""
        result = parse_plan(content)
        assert result.success
        # Phase 3 should be created for the orphan task
        phase_numbers = [p.number for p in result.phases]
        assert 3 in phase_numbers

    def test_large_phase_and_task_numbers(self):
        """Test handling of large phase and task numbers."""
        content = """
### Phase 99: Large Phase

- [TASK-99-999] Large numbered task — Acceptance: Done
"""
        result = parse_plan(content)
        assert result.success
        assert result.phases[0].number == 99
        assert result.phases[0].tasks[0].task_number == 999

    def test_whitespace_only_content(self):
        """Test that whitespace-only content fails."""
        result = parse_plan("   \n\t\n   ")
        assert not result.success
        assert "empty" in result.error.lower()

    def test_malformed_yaml_frontmatter(self):
        """Test handling of malformed YAML frontmatter."""
        content = """---
tasks:
  - id: TASK-1-1
    description: Valid task
  - invalid yaml here: [
---

# Plan

- [TASK-1-2] Fallback task — Acceptance: Done
"""
        # Malformed YAML should fall back to markdown parsing
        result = parse_plan(content)
        # Should still succeed by falling back to markdown
        assert result.success

    def test_task_with_special_characters_in_description(self):
        """Test parsing task with special characters in description."""
        content = "- [TASK-1-1] Handle `code` & <html> chars — Acceptance: Works"
        tasks, _ = parse_tasks_from_markdown(content)
        assert len(tasks) == 1
        assert "`code`" in tasks[0].description

    def test_placeholder_task_uses_valid_numbering(self):
        """Test that placeholder tasks use valid 1-based numbering."""
        content = """
### Phase 1: Empty Phase

**Goal**: Nothing to do here

"""
        result = parse_plan(content)
        assert result.success
        # Placeholder should use task_number=1, not 0
        placeholder = result.phases[0].tasks[0]
        assert placeholder.task_number == 1
        assert placeholder.id == "TASK-1-1"

    def test_task_id_generated_includes_phase_number(self):
        """Test that generated contract task IDs include phase number."""
        parsed = ParsedTask(
            id="TASK-2-3",
            phase_number=2,
            task_number=3,
            description="Test task",
            acceptance_criteria="Pass",
        )
        contract_task = parsed.to_contract_task()
        assert contract_task.id == "task-2-3"

    def test_empty_phases_list(self):
        """Test parsing document with tasks but no phase headers."""
        content = """
# Plan Document

- [TASK-1-1] Orphan task — Acceptance: Done
"""
        result = parse_plan(content)
        assert result.success
        # Should create a phase for the orphan task
        assert len(result.phases) == 1
        assert result.phases[0].tasks[0].description == "Orphan task"


class TestParserCLIIntegration:
    """Integration tests verifying parser output matches CLI expectations.

    These tests ensure that task IDs produced by the plan parser are compatible
    with the parse_task_id() function in the contract CLI.
    """

    def test_parsed_task_id_matches_cli_format(self):
        """Test that ParsedTask.to_contract_task() produces IDs the CLI can parse."""
        # Import the CLI's parse_task_id function
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "sandbox"))
        from egg_lib.contract_cli import parse_task_id

        # Create a parsed task as the parser would
        parsed = ParsedTask(
            id="TASK-1-1",
            phase_number=1,
            task_number=1,
            description="Create schema",
            acceptance_criteria="Schema validates",
        )

        # Convert to contract format
        contract_task = parsed.to_contract_task()

        # The CLI should be able to parse this ID
        phase_idx, task_idx = parse_task_id(contract_task.id)
        assert phase_idx == 0  # 0-based index for phase 1
        assert task_idx == 0  # 0-based index for task 1

    def test_parsed_task_id_uppercase_preserved_in_markdown(self):
        """Test that markdown stores uppercase IDs but contract uses lowercase."""
        content = "- [TASK-2-3] Test task — Acceptance: Done"
        tasks, _ = parse_tasks_from_markdown(content)

        # Markdown parsing preserves original format (uppercase)
        assert tasks[0].id == "TASK-2-3"

        # Contract conversion produces lowercase
        contract_task = tasks[0].to_contract_task()
        assert contract_task.id == "task-2-3"

    def test_full_plan_to_cli_integration(self):
        """Test complete flow: parse plan -> contract tasks -> CLI can parse IDs."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "sandbox"))
        from egg_lib.contract_cli import parse_task_id

        content = """
### Phase 1: Setup

- [TASK-1-1] First task — Acceptance: Done
- [TASK-1-2] Second task — Acceptance: Done

### Phase 2: Build

- [TASK-2-1] Build task — Acceptance: Done
"""
        result = parse_plan(content)
        assert result.success

        # Convert all phases to contract format
        contract_phases = result.to_contract_phases()

        # Verify all task IDs can be parsed by CLI
        for phase in contract_phases:
            for task in phase.tasks:
                # This should not raise
                phase_idx, task_idx = parse_task_id(task.id)
                # Verify indices are sensible (non-negative)
                assert phase_idx >= 0
                assert task_idx >= 0

    def test_yaml_parsed_tasks_compatible_with_cli(self):
        """Test that YAML-parsed tasks produce CLI-compatible IDs."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "sandbox"))
        from egg_lib.contract_cli import parse_task_id

        content = """---
tasks:
  - id: TASK-3-5
    description: YAML task
    acceptance: Works
---

# Plan
"""
        result = parse_plan(content)
        assert result.success

        # Find the task and verify CLI compatibility
        contract_phases = result.to_contract_phases()
        found_task = None
        for phase in contract_phases:
            for task in phase.tasks:
                if task.description == "YAML task":
                    found_task = task
                    break

        assert found_task is not None
        phase_idx, task_idx = parse_task_id(found_task.id)
        assert phase_idx == 2  # Phase 3 -> index 2
        assert task_idx == 4  # Task 5 -> index 4


class TestYamlCodeFence:
    """Tests for yaml-tasks code fence parsing (Option C structured appendix)."""

    def test_basic_yaml_code_fence(self):
        """Test parsing a basic yaml-tasks code fence."""
        content = """
# Plan Document

Some prose explaining the plan.

```yaml
# yaml-tasks
phases:
  - id: 1
    name: Setup
    goal: Initialize project
    tasks:
      - id: TASK-1-1
        description: Create schema
        acceptance: Schema validates
        files:
          - schema.json
```
"""
        yaml_data, remaining, warnings = parse_yaml_code_fence(content)
        assert yaml_data is not None
        assert "phases" in yaml_data
        assert len(yaml_data["phases"]) == 1
        assert yaml_data["phases"][0]["name"] == "Setup"
        assert len(warnings) == 0

    def test_yaml_fence_with_yml_extension(self):
        """Test that ```yml fences also work."""
        content = """
```yml
# yaml-tasks
phases:
  - id: 1
    name: Test
    tasks:
      - id: TASK-1-1
        description: Test task
        acceptance: Done
```
"""
        yaml_data, _, warnings = parse_yaml_code_fence(content)
        assert yaml_data is not None
        assert yaml_data["phases"][0]["name"] == "Test"

    def test_yaml_fence_marker_case_insensitive(self):
        """Test that yaml-tasks marker is case-insensitive."""
        content = """
```yaml
# YAML-TASKS
phases:
  - id: 1
    name: Test
    tasks:
      - id: TASK-1-1
        description: Test
        acceptance: Done
```
"""
        yaml_data, _, warnings = parse_yaml_code_fence(content)
        assert yaml_data is not None

    def test_no_yaml_fence_returns_none(self):
        """Test that missing yaml-tasks fence returns None."""
        content = """
# Plan without yaml-tasks

Just prose here.
"""
        yaml_data, remaining, warnings = parse_yaml_code_fence(content)
        assert yaml_data is None
        assert remaining == content
        assert len(warnings) == 0

    def test_yaml_fence_without_marker_ignored(self):
        """Test that yaml fence without yaml-tasks marker is ignored."""
        content = """
```yaml
# This is just regular YAML, not yaml-tasks
key: value
```
"""
        yaml_data, remaining, warnings = parse_yaml_code_fence(content)
        assert yaml_data is None

    def test_malformed_yaml_falls_back(self):
        """Test that malformed YAML generates warning and falls back."""
        content = """
```yaml
# yaml-tasks
phases:
  - id: 1
    name: Test
    tasks: [invalid yaml here: {
```
"""
        yaml_data, _, warnings = parse_yaml_code_fence(content)
        assert yaml_data is None
        assert len(warnings) == 1
        assert "Invalid YAML" in warnings[0].message

    def test_empty_yaml_fence_warns(self):
        """Test that empty yaml-tasks fence generates warning."""
        content = """
```yaml
# yaml-tasks
```
"""
        yaml_data, _, warnings = parse_yaml_code_fence(content)
        assert yaml_data is None
        assert len(warnings) == 1
        assert "empty" in warnings[0].message.lower()

    def test_yaml_fence_removes_from_remaining(self):
        """Test that yaml fence is removed from remaining content."""
        content = """Before

```yaml
# yaml-tasks
phases:
  - id: 1
    name: Test
    tasks:
      - id: TASK-1-1
        description: Test
        acceptance: Done
```

After"""
        _, remaining, _ = parse_yaml_code_fence(content)
        assert "yaml-tasks" not in remaining
        assert "Before" in remaining
        assert "After" in remaining

    def test_nested_code_fence_inside_block_scalar_not_truncated(self):
        """Regression for #2743 (pipeline-f4c7d780): a nested ``` fenced
        block inside a YAML block scalar (e.g. an inline example in a
        slice's ``goal``) must not terminate the outer yaml-tasks fence.

        The pre-fix non-greedy regex stopped at the first inner ``` and
        silently truncated the contract to whatever slices had been
        parsed up to that point — 7 of 15 in the offending pipeline.
        """
        content = """# Plan

```yaml
# yaml-tasks
slices:
  - id: 1
    name: First
    tasks:
      - id: TASK-1-1
        description: First task
        acceptance: ok
  - id: 2
    name: Second
    goal: |
      Show an inline example:
      ```
      $ run-it
      ```
    dependencies: slice-1
    tasks:
      - id: TASK-2-1
        description: Second task
        acceptance: ok
  - id: 3
    name: Third
    dependencies: slice-2
    tasks:
      - id: TASK-3-1
        description: Third task
        acceptance: ok
```

Trailing prose."""
        yaml_data, _, warnings = parse_yaml_code_fence(content)
        assert yaml_data is not None
        assert "slices" in yaml_data
        assert [s["id"] for s in yaml_data["slices"]] == [1, 2, 3]
        # Block scalar preserves the inner ``` lines verbatim.
        assert "$ run-it" in yaml_data["slices"][1]["goal"]
        assert len(warnings) == 0

    def test_nested_code_fence_full_parse_pipeline(self):
        """End-to-end #2743: ``parse_plan`` returns all slices and
        preserves their declared dependencies despite a nested ``` in
        one slice's goal block scalar.
        """
        content = """# Plan

```yaml
# yaml-tasks
pr:
  title: Test PR
  description: Test
  test_plan: Test
  manual_steps: None
slices:
  - id: 1
    name: First
    tasks:
      - id: TASK-1-1
        description: First task
        acceptance: ok
  - id: 2
    name: Second
    goal: |
      Inline example:
      ```
      $ demo
      ```
    dependencies: slice-1
    tasks:
      - id: TASK-2-1
        description: Second task
        acceptance: ok
  - id: 3
    name: Third
    dependencies: slice-2
    tasks:
      - id: TASK-3-1
        description: Third task
        acceptance: ok
```
"""
        result = parse_plan(content)
        assert result.success, result.error
        assert [p.number for p in result.phases] == [1, 2, 3]
        # Dependencies survive the parse.
        deps_by_id = {p.number: p.dependencies for p in result.phases}
        assert deps_by_id[2] == "slice-1"
        assert deps_by_id[3] == "slice-2"
        # And the contract conversion preserves them.
        slices = result.to_contract_slices()
        assert [s.id for s in slices] == ["slice-1", "slice-2", "slice-3"]
        assert slices[1].dependencies == ["slice-1"]
        assert slices[2].dependencies == ["slice-2"]

    def test_depends_on_alias_with_int_value(self):
        """Regression for #2743 (pipeline-8b81ed32 follow-up): the
        planner emitted ``depends_on: <int>`` on every slice and the
        contract came back with empty dependencies because the parser
        only consulted ``dependencies`` and never coerced a bare int.
        ``depends_on`` is now accepted as an alias and integer values
        normalise to ``slice-<N>``.
        """
        yaml_data, _, _ = parse_yaml_code_fence(
            """```yaml
# yaml-tasks
slices:
  - id: 1
    name: First
    tasks:
      - id: TASK-1-1
        description: First
        acceptance: ok
  - id: 2
    name: Second
    depends_on: 1
    tasks:
      - id: TASK-2-1
        description: Second
        acceptance: ok
  - id: 3
    name: Third
    depends_on: 2
    tasks:
      - id: TASK-3-1
        description: Third
        acceptance: ok
```
"""
        )
        phases, _ = parse_phases_from_yaml(yaml_data)
        slices = [p.to_contract_slice() for p in phases]
        assert [s.id for s in slices] == ["slice-1", "slice-2", "slice-3"]
        assert slices[0].dependencies == []
        assert slices[1].dependencies == ["slice-1"]
        assert slices[2].dependencies == ["slice-2"]

    def test_depends_on_and_dependencies_both_present_warns(self):
        """When both ``depends_on`` and ``dependencies`` are present the
        canonical ``dependencies`` key wins and a warning is recorded —
        matching the existing ``slices:`` / ``phases:`` conflict policy.
        """
        yaml_data = {
            "slices": [
                {
                    "id": 1,
                    "name": "A",
                    "tasks": [{"id": "TASK-1-1", "description": "a", "acceptance": "ok"}],
                },
                {
                    "id": 2,
                    "name": "B",
                    "dependencies": "slice-1",
                    "depends_on": "slice-99",
                    "tasks": [{"id": "TASK-2-1", "description": "b", "acceptance": "ok"}],
                },
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        slices = [p.to_contract_slice() for p in phases]
        assert slices[1].dependencies == ["slice-1"]
        assert any("both 'dependencies' and 'depends_on'" in w.message for w in warnings)

    def test_to_contract_slice_drops_bool_dependencies(self):
        """``bool`` is a subclass of ``int`` in Python; without an
        explicit branch ``True`` would coerce to ``slice-1``. The
        ``to_contract_slice`` path drops bools instead so a typo like
        ``depends_on: true`` doesn't fabricate a fake dependency.
        """
        phase = ParsedPhase(
            number=2,
            name="X",
            goal="",
            dependencies=True,  # type: ignore[arg-type]
        )
        assert phase.to_contract_slice().dependencies == []

    def test_parse_phases_from_yaml_warns_on_bool_depends_on(self):
        """The production parse path must emit a ParseWarning when
        ``depends_on`` (or ``dependencies``) is a bool — otherwise the
        dropped dep is invisible to ``parse_plan`` consumers.
        Companion to ``test_to_contract_slice_drops_bool_dependencies``
        that exercises the same case end-to-end through the parser.
        """
        yaml_data = {
            "slices": [
                {
                    "id": 1,
                    "name": "A",
                    "tasks": [{"id": "TASK-1-1", "description": "a", "acceptance": "ok"}],
                },
                {
                    "id": 2,
                    "name": "B",
                    "depends_on": True,
                    "tasks": [{"id": "TASK-2-1", "description": "b", "acceptance": "ok"}],
                },
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        slices = [p.to_contract_slice() for p in phases]
        assert slices[1].dependencies == []
        assert any(
            "'depends_on' is a bool" in w.message and "Slice 2" in w.message for w in warnings
        )


class TestParsePhasesFromYaml:
    """Tests for parsing phases from structured YAML."""

    def test_basic_phase_parsing(self):
        """Test parsing a basic phases structure."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Setup",
                    "goal": "Initialize the project",
                    "tasks": [
                        {
                            "id": "TASK-1-1",
                            "description": "Create schema",
                            "acceptance": "Schema validates",
                            "files": ["schema.json"],
                        }
                    ],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 1
        assert phases[0].number == 1
        assert phases[0].name == "Setup"
        assert phases[0].goal == "Initialize the project"
        assert len(phases[0].tasks) == 1
        assert phases[0].tasks[0].description == "Create schema"
        assert phases[0].tasks[0].files_affected == ["schema.json"]

    def test_multiple_phases(self):
        """Test parsing multiple phases."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Setup",
                    "tasks": [{"id": "TASK-1-1", "description": "Task 1", "acceptance": "Done"}],
                },
                {
                    "id": 2,
                    "name": "Build",
                    "tasks": [{"id": "TASK-2-1", "description": "Task 2", "acceptance": "Done"}],
                },
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 2
        assert phases[0].name == "Setup"
        assert phases[1].name == "Build"

    def test_string_phase_id(self):
        """Test that string phase IDs are parsed correctly."""
        yaml_data = {
            "phases": [
                {
                    "id": "phase-3",
                    "name": "Test Phase",
                    "tasks": [{"id": "TASK-3-1", "description": "Test", "acceptance": "Done"}],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 1
        assert phases[0].number == 3

    def test_numeric_string_phase_id(self):
        """Test that numeric string IDs work."""
        yaml_data = {
            "phases": [
                {
                    "id": "2",
                    "name": "Second",
                    "tasks": [{"id": "TASK-2-1", "description": "Test", "acceptance": "Done"}],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert phases[0].number == 2

    def test_missing_phases_key_with_legacy_tasks(self):
        """Test fallback when phases key missing but tasks present."""
        yaml_data = {"tasks": [{"id": "TASK-1-1", "description": "Legacy", "acceptance": "Done"}]}
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 0  # Returns empty so caller falls back to legacy

    def test_missing_phase_id_warns(self):
        """Test that missing phase ID generates warning."""
        yaml_data = {
            "phases": [
                {
                    "name": "No ID Phase",
                    "tasks": [{"id": "TASK-1-1", "description": "Test", "acceptance": "Done"}],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 0
        assert any("missing 'id'" in w.message.lower() for w in warnings)

    def test_task_phase_mismatch_warns(self):
        """Test warning when task ID phase doesn't match container phase."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Phase One",
                    "tasks": [
                        {
                            "id": "TASK-2-1",  # Wrong phase number
                            "description": "Mismatched",
                            "acceptance": "Done",
                        }
                    ],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 1
        # Task should be assigned to container phase despite ID
        assert phases[0].tasks[0].phase_number == 1
        assert any("ID suggests phase 2" in w.message for w in warnings)

    def test_invalid_task_id_generates_new_id(self):
        """Test that invalid task ID format gets a new ID assigned."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Test",
                    "tasks": [
                        {
                            "id": "bad-format",
                            "description": "Task with bad ID",
                            "acceptance": "Done",
                        }
                    ],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 1
        assert phases[0].tasks[0].id == "TASK-1-1"
        assert any("doesn't match pattern" in w.message for w in warnings)

    def test_files_as_string_converted_to_list(self):
        """Test that single file string is converted to list."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Test",
                    "tasks": [
                        {
                            "id": "TASK-1-1",
                            "description": "Test",
                            "acceptance": "Done",
                            "files": "single-file.py",  # String instead of list
                        }
                    ],
                }
            ]
        }
        phases, _ = parse_phases_from_yaml(yaml_data)
        assert phases[0].tasks[0].files_affected == ["single-file.py"]

    def test_phases_sorted_by_number(self):
        """Test that phases are sorted by number regardless of input order."""
        yaml_data = {
            "phases": [
                {
                    "id": 3,
                    "name": "Third",
                    "tasks": [{"id": "TASK-3-1", "description": "Test", "acceptance": "Done"}],
                },
                {
                    "id": 1,
                    "name": "First",
                    "tasks": [{"id": "TASK-1-1", "description": "Test", "acceptance": "Done"}],
                },
                {
                    "id": 2,
                    "name": "Second",
                    "tasks": [{"id": "TASK-2-1", "description": "Test", "acceptance": "Done"}],
                },
            ]
        }
        phases, _ = parse_phases_from_yaml(yaml_data)
        assert [p.number for p in phases] == [1, 2, 3]
        assert [p.name for p in phases] == ["First", "Second", "Third"]

    def test_duplicate_phase_id_warns_and_skips(self):
        """Test that duplicate phase IDs generate warning and skip second phase."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "First Phase 1",
                    "tasks": [{"id": "TASK-1-1", "description": "First", "acceptance": "Done"}],
                },
                {
                    "id": 1,  # Duplicate ID
                    "name": "Second Phase 1",
                    "tasks": [{"id": "TASK-1-2", "description": "Second", "acceptance": "Done"}],
                },
                {
                    "id": 2,
                    "name": "Phase 2",
                    "tasks": [{"id": "TASK-2-1", "description": "Third", "acceptance": "Done"}],
                },
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 2
        assert phases[0].name == "First Phase 1"  # First one kept
        assert phases[1].name == "Phase 2"
        assert any("Duplicate phase ID: 1" in w.message for w in warnings)

    def test_pr_plan_key_produces_warning(self):
        """Test that pr_plan key (multi-PR format) produces a warning."""
        yaml_data = {
            "pr_plan": [
                {"title": "PR-1: First change"},
                {"title": "PR-2: Second change"},
            ],
            "phases": [
                {
                    "id": 1,
                    "name": "Setup",
                    "tasks": [{"id": "TASK-1-1", "description": "Test", "acceptance": "Done"}],
                }
            ],
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        # Phases should still parse normally
        assert len(phases) == 1
        assert phases[0].name == "Setup"
        # But a warning about pr_plan should be emitted
        assert any("pr_plan" in w.message for w in warnings)
        assert any("not supported" in w.message for w in warnings)

    def test_pr_plan_key_without_phases_returns_empty(self):
        """Test that pr_plan without phases returns empty (treated as error by caller)."""
        yaml_data = {
            "pr_plan": [
                {"title": "PR-1: First change"},
                {"title": "PR-2: Second change"},
            ],
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        # No phases should be returned
        assert len(phases) == 0
        # Warning should indicate the error
        assert any("pr_plan" in w.message for w in warnings)
        assert any("without 'slices' or 'phases'" in w.message for w in warnings)

    def test_pr_plan_key_without_phases_full_parse_fails(self):
        """Test that pr_plan without phases causes parse_plan to fail."""
        content = """
# Plan

```yaml
# yaml-tasks
pr_plan:
  - title: "PR-1"
  - title: "PR-2"
```
"""
        result = parse_plan(content)
        assert not result.success
        assert any("pr_plan" in w.message for w in result.warnings)

    def test_pr_plan_key_in_full_parse(self):
        """Test that pr_plan key in yaml-tasks fence produces a warning via parse_plan."""
        content = """
# Plan

```yaml
# yaml-tasks
pr_plan:
  - title: "PR-1"
  - title: "PR-2"
phases:
  - id: 1
    name: Implementation
    tasks:
      - id: TASK-1-1
        description: Do the thing
        acceptance: Thing is done
```
"""
        result = parse_plan(content)
        assert result.success
        assert any("pr_plan" in w.message for w in result.warnings)

    def test_task_with_role_coder(self):
        """Test that a task with role: coder is parsed correctly."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Implementation",
                    "tasks": [
                        {
                            "id": "TASK-1-1",
                            "description": "Implement feature",
                            "acceptance": "Feature works",
                            "role": "coder",
                        }
                    ],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 1
        assert phases[0].tasks[0].role == "coder"

    def test_task_with_role_tester(self):
        """Test that a task with role: tester is parsed correctly."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Testing",
                    "tasks": [
                        {
                            "id": "TASK-1-1",
                            "description": "Write tests",
                            "acceptance": "Tests pass",
                            "role": "tester",
                        }
                    ],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 1
        assert phases[0].tasks[0].role == "tester"

    def test_task_with_role_documenter(self):
        """Test that a task with role: documenter is parsed correctly."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Documentation",
                    "tasks": [
                        {
                            "id": "TASK-1-1",
                            "description": "Write docs",
                            "acceptance": "Docs are complete",
                            "role": "documenter",
                        }
                    ],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 1
        assert phases[0].tasks[0].role == "documenter"

    def test_task_without_role_has_none(self):
        """Test that a task without role field has role=None."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Implementation",
                    "tasks": [
                        {
                            "id": "TASK-1-1",
                            "description": "Implement feature",
                            "acceptance": "Feature works",
                        }
                    ],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 1
        assert phases[0].tasks[0].role is None

    def test_task_with_invalid_role_warns_and_sets_none(self):
        """Test that an invalid role produces a warning and sets role to None."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Implementation",
                    "tasks": [
                        {
                            "id": "TASK-1-1",
                            "description": "Do something",
                            "acceptance": "Something done",
                            "role": "invalid",
                        }
                    ],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 1
        assert phases[0].tasks[0].role is None
        assert any("invalid role" in w.message for w in warnings)

    def test_role_preserved_through_to_contract_task(self):
        """Test that role is preserved when converting parsed task to contract task."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Testing",
                    "tasks": [
                        {
                            "id": "TASK-1-1",
                            "description": "Write unit tests",
                            "acceptance": "All tests pass",
                            "role": "tester",
                        }
                    ],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        task = phases[0].tasks[0].to_contract_task()
        assert task.role == "tester"

    def test_mixed_roles_in_same_phase(self):
        """Test that tasks in the same phase can have different roles."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Implementation",
                    "tasks": [
                        {
                            "id": "TASK-1-1",
                            "description": "Implement feature",
                            "acceptance": "Feature works",
                            "role": "coder",
                        },
                        {
                            "id": "TASK-1-2",
                            "description": "Write tests",
                            "acceptance": "Tests pass",
                            "role": "tester",
                        },
                        {
                            "id": "TASK-1-3",
                            "description": "Update docs",
                            "acceptance": "Docs updated",
                        },
                    ],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        assert len(phases) == 1
        tasks = phases[0].tasks
        assert len(tasks) == 3
        assert tasks[0].role == "coder"
        assert tasks[1].role == "tester"
        assert tasks[2].role is None


class TestAlphaSuffixAndDuplicates:
    """Regression tests for #1988 — task id regex was unanchored, letting
    TASK-1-3A and TASK-1-3B both collapse to task-1-3."""

    def test_alpha_suffix_task_ids_do_not_collide(self):
        """TASK-1-3A and TASK-1-3B should each get a unique contract id."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Setup",
                    "tasks": [
                        {"id": "TASK-1-1", "description": "First", "acceptance": "Done"},
                        {"id": "TASK-1-2", "description": "Second", "acceptance": "Done"},
                        {"id": "TASK-1-3A", "description": "Third-a", "acceptance": "Done"},
                        {"id": "TASK-1-3B", "description": "Third-b", "acceptance": "Done"},
                    ],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        contract_ids = [t.to_contract_task().id for t in phases[0].tasks]
        assert len(contract_ids) == len(set(contract_ids)), (
            f"Expected unique task ids, got: {contract_ids}"
        )

    def test_alpha_suffix_task_ids_emit_warnings(self):
        """Parser should warn when an alpha-suffixed id falls through to the
        synthesized-id path so reviewers see it."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Setup",
                    "tasks": [
                        {"id": "TASK-1-3A", "description": "A", "acceptance": "Done"},
                        {"id": "TASK-1-3B", "description": "B", "acceptance": "Done"},
                    ],
                }
            ]
        }
        _, warnings = parse_phases_from_yaml(yaml_data)
        messages = [w.message for w in warnings]
        assert any("TASK-1-3A" in m for m in messages), messages
        assert any("TASK-1-3B" in m for m in messages), messages

    def test_plain_numeric_ids_still_parse(self):
        """Regression: anchoring must not break the common case."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Setup",
                    "tasks": [
                        {"id": "TASK-1-1", "description": "One", "acceptance": "Done"},
                        {"id": "TASK-1-2", "description": "Two", "acceptance": "Done"},
                    ],
                }
            ]
        }
        phases, warnings = parse_phases_from_yaml(yaml_data)
        # No synthesized-id or duplicate warnings for plain numeric ids.
        unexpected = [
            w
            for w in warnings
            if "doesn't match pattern" in w.message or "Duplicate task id" in w.message
        ]
        assert unexpected == [], unexpected
        contract_ids = [t.to_contract_task().id for t in phases[0].tasks]
        assert contract_ids == ["task-1-1", "task-1-2"]

    def test_duplicate_plain_ids_warn(self):
        """Two identical TASK-1-3 entries should surface a duplicate warning."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Setup",
                    "tasks": [
                        {"id": "TASK-1-3", "description": "First", "acceptance": "Done"},
                        {"id": "TASK-1-3", "description": "Second", "acceptance": "Done"},
                    ],
                }
            ]
        }
        _, warnings = parse_phases_from_yaml(yaml_data)
        assert any("Duplicate task id" in w.message for w in warnings), warnings

    def test_legacy_parse_tasks_from_yaml_skips_alpha_suffix_with_warning(self):
        """Legacy parse_tasks_from_yaml should skip alpha-suffixed IDs and warn."""
        yaml_data = {
            "tasks": [
                {"id": "TASK-1-1", "description": "Normal", "acceptance": "Done"},
                {"id": "TASK-1-3A", "description": "Alpha-A", "acceptance": "Done"},
                {"id": "TASK-1-3B", "description": "Alpha-B", "acceptance": "Done"},
            ]
        }
        tasks, warnings = parse_tasks_from_yaml(yaml_data)
        # Only the plain numeric ID should parse
        assert len(tasks) == 1
        assert tasks[0].id == "TASK-1-1"
        # Both alpha-suffixed IDs should produce warnings
        messages = [w.message for w in warnings]
        assert any("TASK-1-3A" in m for m in messages), messages
        assert any("TASK-1-3B" in m for m in messages), messages

    def test_legacy_parse_tasks_from_yaml_plain_ids_no_warnings(self):
        """Legacy parse_tasks_from_yaml should parse plain numeric IDs without warnings."""
        yaml_data = {
            "tasks": [
                {"id": "TASK-1-1", "description": "One", "acceptance": "Done"},
                {"id": "TASK-1-2", "description": "Two", "acceptance": "Done"},
            ]
        }
        tasks, warnings = parse_tasks_from_yaml(yaml_data)
        assert len(tasks) == 2
        assert warnings == []


class TestParsePlanWithYamlCodeFence:
    """Integration tests for parse_plan with yaml-tasks code fence."""

    def test_yaml_fence_takes_precedence(self):
        """Test that yaml-tasks fence is preferred over markdown regex."""
        content = """
# Plan Document

### Phase 1: Markdown Phase

- [TASK-1-1] Markdown task — Acceptance: From markdown

```yaml
# yaml-tasks
phases:
  - id: 1
    name: YAML Phase
    tasks:
      - id: TASK-1-1
        description: YAML task
        acceptance: From YAML
```
"""
        result = parse_plan(content)
        assert result.success
        assert len(result.phases) == 1
        # Should use YAML phase name, not markdown
        assert result.phases[0].name == "YAML Phase"
        assert result.phases[0].tasks[0].description == "YAML task"

    def test_complete_plan_with_appendix(self):
        """Test parsing a complete plan document with structured appendix."""
        content = """
# Plan: Add Authentication

> Issue: #123 | Phase: plan

## Summary

Add user authentication to the application.

## Implementation Phases

### Phase 1: Setup

**Goal**: Initialize authentication infrastructure

**Tasks**:
- [TASK-1-1] Create user model — Acceptance: Model migrations pass
- [TASK-1-2] Add auth middleware — Acceptance: Middleware loads

### Phase 2: Implementation

**Goal**: Implement login flow

**Tasks**:
- [TASK-2-1] Create login endpoint — Acceptance: Returns JWT token

---

## Structured Task Appendix

```yaml
# yaml-tasks
phases:
  - id: 1
    name: Setup
    goal: Initialize authentication infrastructure
    tasks:
      - id: TASK-1-1
        description: Create user model
        acceptance: Model migrations pass
        files:
          - models/user.py
          - migrations/001_user.py
      - id: TASK-1-2
        description: Add auth middleware
        acceptance: Middleware loads
        files:
          - middleware/auth.py
  - id: 2
    name: Implementation
    goal: Implement login flow
    tasks:
      - id: TASK-2-1
        description: Create login endpoint
        acceptance: Returns JWT token
        files:
          - api/auth.py
```

*Authored-by: egg*
"""
        result = parse_plan(content)
        assert result.success
        assert len(result.phases) == 2
        assert result.phases[0].name == "Setup"
        assert result.phases[1].name == "Implementation"
        assert len(result.phases[0].tasks) == 2
        assert len(result.phases[1].tasks) == 1
        # Check files are preserved
        assert "models/user.py" in result.phases[0].tasks[0].files_affected

    def test_fallback_to_markdown_on_yaml_error(self):
        """Test fallback to markdown when YAML fence is malformed."""
        content = """
### Phase 1: Setup

- [TASK-1-1] Markdown task — Acceptance: Works

```yaml
# yaml-tasks
phases:
  - this: is: invalid: yaml: [
```
"""
        result = parse_plan(content)
        assert result.success
        # Should fall back to markdown parsing
        assert len(result.phases) == 1
        assert result.phases[0].tasks[0].description == "Markdown task"
        # Should have warning about YAML failure
        assert any("Invalid YAML" in w.message for w in result.warnings)

    def test_goal_merged_from_markdown_if_missing(self):
        """Test that goal is taken from markdown if not in YAML."""
        content = """
### Phase 1: Setup

**Goal**: This is the goal from markdown

```yaml
# yaml-tasks
phases:
  - id: 1
    name: Setup
    tasks:
      - id: TASK-1-1
        description: Test
        acceptance: Done
```
"""
        result = parse_plan(content)
        assert result.success
        # Goal should be merged from markdown
        assert result.phases[0].goal == "This is the goal from markdown"

    def test_yaml_fence_cli_integration(self):
        """Test that yaml-fence parsed tasks work with CLI."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "sandbox"))
        from egg_lib.contract_cli import parse_task_id

        content = """
```yaml
# yaml-tasks
phases:
  - id: 2
    name: Build
    tasks:
      - id: TASK-2-3
        description: Build feature
        acceptance: Feature works
```
"""
        result = parse_plan(content)
        assert result.success

        contract_phases = result.to_contract_phases()
        task = contract_phases[0].tasks[0]
        assert task.id == "task-2-3"

        phase_idx, task_idx = parse_task_id(task.id)
        assert phase_idx == 1  # Phase 2 -> index 1
        assert task_idx == 2  # Task 3 -> index 2


class TestFindPlanCommentPriority:
    """Tests for plan comment detection priority in contract task population."""

    def test_yaml_fence_detection(self):
        """Test that yaml-tasks fence is detected."""
        import re

        YAML_FENCE_DETECT = re.compile(r"```(?:yaml|yml)\s*\n\s*#\s*yaml-tasks", re.IGNORECASE)
        comment = """
# Plan

```yaml
# yaml-tasks
phases:
  - id: 1
    name: Test
    tasks:
      - id: TASK-1-1
        description: Test
        acceptance: Done
```
"""
        # Check detection logic - use regex to verify marker is inside fence
        has_yaml_tasks = YAML_FENCE_DETECT.search(comment) is not None
        assert has_yaml_tasks

    def test_yaml_fence_false_positive_rejected(self):
        """Test that yaml-tasks marker outside fence is not detected."""
        import re

        YAML_FENCE_DETECT = re.compile(r"```(?:yaml|yml)\s*\n\s*#\s*yaml-tasks", re.IGNORECASE)
        # Comment with yaml-tasks mentioned in prose but not inside a fence
        comment = """
Here's a note about # yaml-tasks format.

```yaml
unrelated: data
```
"""
        # This should NOT match because the marker is not inside the fence
        has_yaml_tasks = YAML_FENCE_DETECT.search(comment) is not None
        assert not has_yaml_tasks

    def test_frontmatter_detection(self):
        """Test that YAML frontmatter is detected."""
        comment = """---
tasks:
  - id: TASK-1-1
    description: Test
    acceptance: Done
---

# Plan
"""
        has_frontmatter = comment.strip().startswith("---") and "tasks:" in comment
        assert has_frontmatter

    def test_markdown_detection(self):
        """Test that legacy markdown format is detected."""
        comment = """
## Phase 1: Setup

- [TASK-1-1] Test task — Acceptance: Done
"""
        has_markdown = "[TASK-" in comment and ("## Phase" in comment or "Phase 1:" in comment)
        assert has_markdown


class TestNormalizeOptionalString:
    """Tests for _normalize_optional_string helper."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            # None and empty
            (None, ""),
            ("", ""),
            ("   ", ""),
            # Literal 'None' (case-insensitive)
            ("None", ""),
            ("none", ""),
            ("NONE", ""),
            ("  None  ", ""),
            # Non-string values
            (123, "123"),
            (True, "True"),
            # Normal strings pass through (stripped)
            ("Run pytest", "Run pytest"),
            ("  Run pytest  ", "Run pytest"),
            # Multi-line: all lines 'None' → empty
            ("Pre-merge: None\nPost-merge: None", ""),
            ("Pre-merge: none\nPost-merge: NONE", ""),
            # Multi-line: mixed values → preserved
            ("Pre-merge: none\nPost-merge: deploy", "Pre-merge: none\nPost-merge: deploy"),
            (
                "Pre-merge: run migrations\nPost-merge: None",
                "Pre-merge: run migrations\nPost-merge: None",
            ),
            # Single-line with colon and 'None' value
            ("Pre-merge: None", ""),
            # N/A and other strings pass through
            ("N/A", "N/A"),
            ("No steps needed", "No steps needed"),
        ],
    )
    def test_normalize_optional_string(self, value, expected):
        assert _normalize_optional_string(value) == expected


class TestPRMetadataExtraction:
    """Tests for PR metadata extraction from YAML."""

    def test_extract_pr_metadata_present(self):
        """Test extracting PR metadata when present."""
        yaml_data = {
            "pr": {
                "title": "Add feature X",
                "description": "This PR adds feature X to improve Y.",
                "test_plan": "Run pytest to verify",
                "manual_steps": "Pre-merge: run migrations\nPost-merge: clear cache",
            },
            "phases": [
                {
                    "id": 1,
                    "name": "Setup",
                    "tasks": [{"id": "TASK-1-1", "description": "Test", "acceptance": "Done"}],
                }
            ],
        }
        pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings = (
            extract_pr_metadata_from_yaml(yaml_data)
        )
        assert pr_title == "Add feature X"
        assert pr_description == "This PR adds feature X to improve Y."
        assert pr_test_plan == "Run pytest to verify"
        assert pr_manual_steps == "Pre-merge: run migrations\nPost-merge: clear cache"
        assert len(warnings) == 0

    def test_extract_pr_metadata_absent(self):
        """Test extracting PR metadata when absent."""
        yaml_data = {
            "phases": [
                {
                    "id": 1,
                    "name": "Setup",
                    "tasks": [{"id": "TASK-1-1", "description": "Test", "acceptance": "Done"}],
                }
            ],
        }
        pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings = (
            extract_pr_metadata_from_yaml(yaml_data)
        )
        assert pr_title is None
        assert pr_description is None
        assert pr_test_plan is None
        assert pr_manual_steps is None
        assert len(warnings) == 0

    def test_extract_pr_metadata_none_yaml(self):
        """Test extracting PR metadata with None yaml_data."""
        pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings = (
            extract_pr_metadata_from_yaml(None)
        )
        assert pr_title is None
        assert pr_description is None
        assert pr_test_plan is None
        assert pr_manual_steps is None
        assert len(warnings) == 0

    def test_extract_pr_metadata_invalid_type(self):
        """Test extracting PR metadata when pr field is not an object."""
        yaml_data = {
            "pr": "not an object",
            "phases": [],
        }
        pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings = (
            extract_pr_metadata_from_yaml(yaml_data)
        )
        assert pr_title is None
        assert pr_description is None
        assert len(warnings) == 1
        assert "must be an object" in warnings[0].message

    def test_extract_pr_metadata_missing_title(self):
        """Test extracting PR metadata when title is missing."""
        yaml_data = {
            "pr": {
                "description": "No title here",
            },
            "phases": [],
        }
        pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings = (
            extract_pr_metadata_from_yaml(yaml_data)
        )
        assert pr_title is None
        assert pr_description is None
        assert len(warnings) == 1
        assert "missing required 'title' field" in warnings[0].message

    def test_extract_pr_metadata_empty_title(self):
        """Test extracting PR metadata when title is empty."""
        yaml_data = {
            "pr": {
                "title": "   ",
                "description": "Has description but empty title",
            },
            "phases": [],
        }
        pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings = (
            extract_pr_metadata_from_yaml(yaml_data)
        )
        assert pr_title is None
        assert pr_description is None
        assert len(warnings) == 1
        assert "cannot be empty" in warnings[0].message

    def test_extract_pr_metadata_title_not_string(self):
        """Test extracting PR metadata when title is not a string."""
        yaml_data = {
            "pr": {
                "title": 123,
                "description": "Title is a number",
            },
            "phases": [],
        }
        pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings = (
            extract_pr_metadata_from_yaml(yaml_data)
        )
        assert pr_title is None
        assert pr_description is None
        assert len(warnings) == 1
        assert "must be a string" in warnings[0].message

    def test_extract_pr_metadata_multiline_description(self):
        """Test extracting PR metadata with multiline description."""
        yaml_data = {
            "pr": {
                "title": "Add feature X",
                "description": """This PR adds feature X.

Key changes:
- Change 1
- Change 2
""",
            },
            "phases": [],
        }
        pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings = (
            extract_pr_metadata_from_yaml(yaml_data)
        )
        assert pr_title == "Add feature X"
        assert "Key changes:" in pr_description
        assert "- Change 1" in pr_description
        # Missing test_plan warning expected
        assert len(warnings) == 1
        assert "test_plan" in warnings[0].message

    def test_extract_pr_metadata_empty_description(self):
        """Test extracting PR metadata with empty description."""
        yaml_data = {
            "pr": {
                "title": "Add feature X",
            },
            "phases": [],
        }
        pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings = (
            extract_pr_metadata_from_yaml(yaml_data)
        )
        assert pr_title == "Add feature X"
        assert pr_description == ""
        # Missing test_plan warning expected
        assert len(warnings) == 1
        assert "test_plan" in warnings[0].message

    def test_extract_pr_metadata_title_over_70_chars_warns(self):
        """Test that PR titles over 70 characters generate a warning."""
        long_title = "A" * 75  # 75 chars
        yaml_data = {
            "pr": {
                "title": long_title,
                "description": "Description",
            },
            "phases": [],
        }
        pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings = (
            extract_pr_metadata_from_yaml(yaml_data)
        )
        assert pr_title == long_title
        assert pr_description == "Description"
        # Warnings: length + missing test_plan
        assert len(warnings) == 2
        warning_messages = [w.message for w in warnings]
        assert any("exceeds recommended length" in m for m in warning_messages)
        assert any("75 chars" in m for m in warning_messages)
        assert any("test_plan" in m for m in warning_messages)

    def test_extract_pr_metadata_title_exactly_70_chars_no_warning(self):
        """Test that PR titles at exactly 70 characters do not warn."""
        title_70 = "A" * 70  # Exactly 70 chars
        yaml_data = {
            "pr": {
                "title": title_70,
                "description": "Description",
                "test_plan": "Run pytest",
            },
            "phases": [],
        }
        pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings = (
            extract_pr_metadata_from_yaml(yaml_data)
        )
        assert pr_title == title_70
        assert len(warnings) == 0


class TestParsePlanWithPRMetadata:
    """Tests for parse_plan with PR metadata integration."""

    def test_parse_plan_with_pr_metadata(self):
        """Test parsing a complete plan with PR metadata."""
        content = """
# Plan

## Structured Task Appendix

```yaml
# yaml-tasks
pr:
  title: "Add retry logic"
  description: |
    Implements exponential backoff retry for API requests.
    This improves reliability.
  test_plan: |
    - Automated: test_retry.py covers backoff timing
    - Manual: verify retry behavior with flaky endpoint
  manual_steps: |
    Pre-merge: none
    Post-merge: none
phases:
  - id: 1
    name: Implementation
    goal: Add retry logic
    tasks:
      - id: TASK-1-1
        description: Add retry module
        acceptance: Module works
```
"""
        result = parse_plan(content)
        assert result.success
        assert result.pr_title == "Add retry logic"
        assert "exponential backoff" in result.pr_description
        assert "Automated" in result.pr_test_plan
        # manual_steps with all-none values is normalized to empty
        assert result.pr_manual_steps == ""
        assert len(result.phases) == 1

    def test_parse_plan_without_pr_metadata(self):
        """Test parsing a plan without PR metadata."""
        content = """
# Plan

```yaml
# yaml-tasks
phases:
  - id: 1
    name: Implementation
    tasks:
      - id: TASK-1-1
        description: Test task
        acceptance: Done
```
"""
        result = parse_plan(content)
        assert result.success
        assert result.pr_title is None
        assert result.pr_description is None
        assert len(result.phases) == 1

    def test_parse_plan_pr_metadata_warning_preserved(self):
        """Test that PR metadata warnings are preserved in parse result."""
        content = """
# Plan

```yaml
# yaml-tasks
pr:
  description: "Missing title"
phases:
  - id: 1
    name: Implementation
    tasks:
      - id: TASK-1-1
        description: Test task
        acceptance: Done
```
"""
        result = parse_plan(content)
        assert result.success
        assert result.pr_title is None
        assert result.pr_description is None
        assert any("missing required 'title' field" in w.message for w in result.warnings)


class TestYamlScalarQuotingRegression:
    """Regression tests for #1974 — task_planner emitting unquoted YAML
    scalars whose text contained ``: `` sequences, which PyYAML interpreted
    as the start of a nested mapping and broke contract population.
    """

    # The exact fragment pattern that broke issue #1932's pipeline.
    BROKEN_DESCRIPTION = (
        "Add `sequence: int = 0` field to `Event` dataclass in "
        "`orchestrator/events.py`. Populate from a new "
        "`EventBus._sequence: int` counter."
    )

    def test_unquoted_scalar_with_colon_triggers_parse_warning(self):
        """Plain-scalar description containing `` `code: type` `` raises a
        YAML ScannerError inside ``parse_yaml_code_fence``, which records a
        warning and forces the markdown fallback. This reproduces the silent
        failure from #1932 — the ``pr:`` block is lost.
        """
        content = f"""# Plan

```yaml
# yaml-tasks
pr:
  title: "Fix sequencing"
  description: |
    Body.
phases:
  - id: 1
    name: Implementation
    tasks:
      - id: TASK-1-1
        description: {self.BROKEN_DESCRIPTION}
        acceptance: Works
```
"""
        result = parse_plan(content)
        # The YAML fence parser records a scanner error warning and falls
        # back to markdown — which recovers no ``pr:`` block.
        assert any("Invalid YAML in yaml-tasks code fence" in w.message for w in result.warnings)
        assert result.pr_title is None

    def test_block_scalar_safely_carries_colon_content(self):
        """With the description wrapped in a block scalar (``|-``), the same
        text is preserved literally and parsing succeeds — pr_title is set
        and the contract-populate path fires normally.
        """
        content = f"""# Plan

```yaml
# yaml-tasks
pr:
  title: "Fix sequencing"
  description: |
    Body.
  test_plan: |
    - Automated: unit test
phases:
  - id: 1
    name: |-
      Implementation
    goal: |-
      Add sequencing
    tasks:
      - id: TASK-1-1
        description: |-
          {self.BROKEN_DESCRIPTION}
        acceptance: |-
          Works
```
"""
        result = parse_plan(content)
        assert result.success, result.error
        assert result.pr_title == "Fix sequencing"
        assert len(result.phases) == 1
        task = result.phases[0].tasks[0]
        # Block scalar preserves backticks, colons, and inline code snippets.
        assert "`sequence: int = 0`" in task.description
        assert "`EventBus._sequence: int`" in task.description
        # No YAML scanner warnings.
        assert not any(
            "Invalid YAML in yaml-tasks code fence" in w.message for w in result.warnings
        )


# ---------------------------------------------------------------------------
# #2777 TASK-1-1a — plan-phase pre-flight validator
# ---------------------------------------------------------------------------


def _valid_plan_markdown(
    *,
    title: str = "Implement #2777",
    description: str = "Body content.",
    test_plan: str = "- Automated: unit tests under tests/",
    manual_steps: str | None = "",
    include_yaml_fence: bool = True,
    include_pr_block: bool = True,
    omit_keys: tuple[str, ...] = (),
) -> str:
    """Build a plan-draft markdown payload for the pre-flight validator.

    The helper produces the canonical-shape plan used by
    :func:`validate_plan_for_implement_phase` happy path, then lets each
    test selectively remove fields via ``omit_keys`` or null them out via
    ``manual_steps=None`` so the per-case rejection paths are exercised
    against a known-good baseline rather than diverging hand-rolled
    fixtures.
    """
    if not include_yaml_fence:
        return "# Plan\n\nNo yaml-tasks fence here at all.\n"
    if not include_pr_block:
        return (
            "# Plan\n\n"
            "```yaml\n"
            "# yaml-tasks\n"
            "phases:\n"
            "  - id: 1\n"
            "    name: slice-1\n"
            "    tasks: []\n"
            "```\n"
        )
    lines = ["# Plan", "", "```yaml", "# yaml-tasks", "pr:"]
    if "title" not in omit_keys:
        lines.append(f'  title: "{title}"')
    if "description" not in omit_keys:
        lines.append(f'  description: "{description}"')
    if "test_plan" not in omit_keys:
        lines.append(f'  test_plan: "{test_plan}"')
    if "manual_steps" not in omit_keys:
        if manual_steps is None:
            # Emit a bare ``manual_steps:`` — yaml.safe_load yields None.
            lines.append("  manual_steps:")
        else:
            lines.append(f'  manual_steps: "{manual_steps}"')
    lines.extend(
        [
            "phases:",
            "  - id: 1",
            "    name: slice-1",
            "    tasks: []",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


class TestPlanPreflightErrorShape:
    """The :class:`PlanPreflightError` exception itself (#2777 TASK-1-1a).

    Pins the public surface the implement-phase guard and the plan-phase
    BRC NACK rendering both consume: ``missing_fields`` is an ordered
    ``list[str]`` and the stringified exception names every missing
    field by its dotted path.
    """

    def test_missing_fields_attribute_is_list(self):
        err = PlanPreflightError(["pr.title", "pr.description"])
        assert isinstance(err.missing_fields, list)
        assert err.missing_fields == ["pr.title", "pr.description"]

    def test_str_contains_every_missing_field_name(self):
        err = PlanPreflightError(["yaml-tasks", "pr.title", "pr.description"])
        msg = str(err)
        # Every missing field name appears verbatim in the message.
        assert "yaml-tasks" in msg
        assert "pr.title" in msg
        assert "pr.description" in msg

    def test_str_includes_optional_detail(self):
        err = PlanPreflightError(["pr.manual_steps"], detail="explicit empty allowed")
        msg = str(err)
        assert "pr.manual_steps" in msg
        assert "explicit empty allowed" in msg

    def test_constructor_copies_missing_fields_list(self):
        """The class must not store the caller's list by reference.

        Adversarial: a downstream consumer that mutated the input list
        post-raise could corrupt the on-disk audit log. Pin defensive
        copying so the exception object owns an independent list.
        """
        src = ["pr.title"]
        err = PlanPreflightError(src)
        src.append("mutated")
        assert err.missing_fields == ["pr.title"]

    def test_empty_missing_fields_produces_unspecified_marker(self):
        err = PlanPreflightError([])
        msg = str(err)
        assert "<unspecified>" in msg


class TestValidatePlanForImplementPhaseHappyPath:
    """Valid plan drafts pass without raising."""

    def test_full_plan_passes(self):
        # Sanity: a plan with all required fields populated returns
        # without raising. The function is annotated ``-> None``, so we
        # exercise it for side effects (no exception) rather than
        # asserting on a return value.
        content = _valid_plan_markdown()
        validate_plan_for_implement_phase(content)  # does not raise

    def test_empty_manual_steps_explicitly_allowed(self):
        """Case (e) carve-out: empty string for ``manual_steps`` is OK.

        The planner explicitly emits ``manual_steps: ""`` for slices
        with no manual steps. Per the docstring this MUST be accepted —
        only ``None`` / missing-key counts as missing.
        """
        content = _valid_plan_markdown(manual_steps="")
        validate_plan_for_implement_phase(content)  # does not raise

    def test_block_scalar_values_accepted(self):
        """Multi-line block scalar values still satisfy the validator.

        Adversarial: a regression that only checked single-line scalars
        would silently reject every real-world plan, since planners
        habitually use ``|-`` block scalars for descriptions.
        """
        content = (
            "# Plan\n\n"
            "```yaml\n"
            "# yaml-tasks\n"
            "pr:\n"
            '  title: "Implement #2777"\n'
            "  description: |-\n"
            "    Multi-line body\n"
            "    spans several lines.\n"
            "  test_plan: |-\n"
            "    - Automated: unit tests\n"
            "    - Manual: smoke test\n"
            '  manual_steps: ""\n'
            "phases: []\n"
            "```\n"
        )
        validate_plan_for_implement_phase(content)  # does not raise


class TestValidatePlanForImplementPhaseRejections:
    """The five rejection cases (a)–(e) named in TASK-1-1a's acceptance."""

    # ------------------------------------------------------------------
    # Case (a): yaml-tasks fence missing or unparseable.
    # ------------------------------------------------------------------

    def test_a_missing_yaml_fence(self):
        """No yaml-tasks code fence at all → reject with `yaml-tasks`."""
        content = _valid_plan_markdown(include_yaml_fence=False)
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert excinfo.value.missing_fields == ["yaml-tasks"]
        # Detail is surfaced to the BRC NACK message.
        assert "yaml-tasks" in str(excinfo.value)

    def test_a_unparseable_yaml_fence(self):
        """A yaml-tasks fence that does not parse → reject as missing.

        Adversarial: the parser swallows malformed YAML to a warning
        rather than raising; the validator MUST still surface the
        absence to the plan-phase BRC.
        """
        content = (
            "# Plan\n\n"
            "```yaml\n"
            "# yaml-tasks\n"
            "pr: : : : :\n"  # malformed YAML
            "```\n"
        )
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert excinfo.value.missing_fields == ["yaml-tasks"]

    # ------------------------------------------------------------------
    # Structural: pr: block absent or non-dict.
    # ------------------------------------------------------------------

    def test_pr_block_entirely_missing(self):
        content = _valid_plan_markdown(include_pr_block=False)
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert excinfo.value.missing_fields == ["pr"]
        assert "pr" in str(excinfo.value)

    def test_pr_block_is_a_list_rejected(self):
        """``pr: [title, body]`` is a structural error, not field-level.

        Adversarial: a planner that confused YAML mapping vs list
        syntax could emit a list here. The validator must reject
        rather than crash on ``.get`` against the list.
        """
        content = (
            "# Plan\n\n"
            "```yaml\n"
            "# yaml-tasks\n"
            "pr:\n"
            "  - title-as-list-entry\n"
            "  - body-as-list-entry\n"
            "phases: []\n"
            "```\n"
        )
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert excinfo.value.missing_fields == ["pr"]

    # ------------------------------------------------------------------
    # Case (b): pr.title missing or empty.
    # ------------------------------------------------------------------

    def test_b_missing_pr_title(self):
        content = _valid_plan_markdown(omit_keys=("title",))
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.title" in excinfo.value.missing_fields
        assert "pr.title" in str(excinfo.value)

    def test_b_empty_pr_title(self):
        content = _valid_plan_markdown(title="")
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.title" in excinfo.value.missing_fields

    def test_b_whitespace_only_pr_title(self):
        """Whitespace-only ``pr.title`` is treated as empty (#2777 TASK-1-1a)."""
        content = _valid_plan_markdown(title="   ")
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.title" in excinfo.value.missing_fields

    # ------------------------------------------------------------------
    # Case (c): pr.description missing or empty.
    # ------------------------------------------------------------------

    def test_c_missing_pr_description(self):
        content = _valid_plan_markdown(omit_keys=("description",))
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.description" in excinfo.value.missing_fields

    def test_c_empty_pr_description(self):
        content = _valid_plan_markdown(description="")
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.description" in excinfo.value.missing_fields

    def test_c_whitespace_only_pr_description(self):
        content = _valid_plan_markdown(description="\t  \n  ")
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.description" in excinfo.value.missing_fields

    # ------------------------------------------------------------------
    # Case (d): pr.test_plan missing or empty.
    # ------------------------------------------------------------------

    def test_d_missing_pr_test_plan(self):
        content = _valid_plan_markdown(omit_keys=("test_plan",))
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.test_plan" in excinfo.value.missing_fields

    def test_d_empty_pr_test_plan(self):
        content = _valid_plan_markdown(test_plan="")
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.test_plan" in excinfo.value.missing_fields

    def test_d_whitespace_only_pr_test_plan(self):
        content = _valid_plan_markdown(test_plan="   ")
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.test_plan" in excinfo.value.missing_fields

    # ------------------------------------------------------------------
    # Case (e): pr.manual_steps key missing OR explicitly null.
    # ------------------------------------------------------------------

    def test_e_missing_pr_manual_steps_key(self):
        content = _valid_plan_markdown(omit_keys=("manual_steps",))
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.manual_steps" in excinfo.value.missing_fields

    def test_e_null_pr_manual_steps(self):
        """A bare ``manual_steps:`` (YAML null) must be rejected.

        The carve-out is for the *explicit empty string* form; a
        bare key with no value is ambiguous and could mask a planner
        regression that meant to populate the field. Reject with a
        detail message explicitly pointing at the ``""`` workaround
        so the planner fix is obvious.
        """
        content = _valid_plan_markdown(manual_steps=None)
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.manual_steps" in excinfo.value.missing_fields
        # The detail message must point operators at the workaround.
        assert "empty string" in str(excinfo.value).lower()


class TestValidatePlanForImplementPhaseAdversarial:
    """Probes for ways the validator could regress in real-world plans."""

    def test_all_four_pr_fields_missing_simultaneously(self):
        """Every missing field appears in ``missing_fields``, in order.

        Adversarial: a fragile implementation might short-circuit on
        the first missing field, hiding the rest from the planner's
        feedback. The plan-phase BRC NACK is more actionable when it
        lists every gap at once.
        """
        content = "# Plan\n\n```yaml\n# yaml-tasks\npr: {}\nphases: []\n```\n"
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert set(excinfo.value.missing_fields) == {
            "pr.title",
            "pr.description",
            "pr.test_plan",
            "pr.manual_steps",
        }

    def test_non_string_pr_title_treated_as_missing(self):
        """``pr.title: 123`` is not a string → missing.

        Adversarial: a planner that confused field types might emit an
        integer here. ``isinstance(title, str)`` is the guard the
        validator uses; pin it so a future ``str(title)`` coercion
        regression fails loudly.
        """
        content = (
            "# Plan\n\n"
            "```yaml\n"
            "# yaml-tasks\n"
            "pr:\n"
            "  title: 12345\n"
            '  description: "ok"\n'
            '  test_plan: "ok"\n'
            '  manual_steps: ""\n'
            "phases: []\n"
            "```\n"
        )
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.title" in excinfo.value.missing_fields

    def test_non_string_pr_description_treated_as_missing(self):
        content = (
            "# Plan\n\n"
            "```yaml\n"
            "# yaml-tasks\n"
            "pr:\n"
            '  title: "t"\n'
            "  description:\n"
            "    nested: dict\n"
            '  test_plan: "ok"\n'
            '  manual_steps: ""\n'
            "phases: []\n"
            "```\n"
        )
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.description" in excinfo.value.missing_fields

    def test_non_string_pr_test_plan_treated_as_missing(self):
        content = (
            "# Plan\n\n"
            "```yaml\n"
            "# yaml-tasks\n"
            "pr:\n"
            '  title: "t"\n'
            '  description: "ok"\n'
            "  test_plan:\n"
            "    - item-one\n"
            "    - item-two\n"
            '  manual_steps: ""\n'
            "phases: []\n"
            "```\n"
        )
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert "pr.test_plan" in excinfo.value.missing_fields

    def test_plain_text_no_yaml_fence_rejected(self):
        """A plan that is just paragraphs of text with no yaml-tasks fence
        is the most common 'missing yaml-tasks' regression mode.
        """
        content = "# Plan\n\nGoals:\n- something\n- something else\n\nApproach: thoughts.\n"
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        assert excinfo.value.missing_fields == ["yaml-tasks"]

    def test_yaml_fence_without_pr_or_phases(self):
        """A bare ``# yaml-tasks`` block with no contents → ``pr`` missing."""
        content = "# Plan\n\n```yaml\n# yaml-tasks\n{}\n```\n"
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        # Empty mapping passes the "yaml-tasks parsed" gate; pr is missing.
        assert excinfo.value.missing_fields == ["pr"]

    def test_top_level_yaml_is_a_list_treated_as_yaml_missing(self):
        """``yaml-tasks`` block that parses to a list, not a dict.

        ``parse_yaml_code_fence`` already rejects top-level non-dict
        YAML as a parse error (returns ``None``), so the validator
        surfaces this as the case (a) "yaml-tasks missing or
        unparseable" rejection. Pin that behavior so a future
        ``parse_yaml_code_fence`` relaxation that started returning
        the list through doesn't slip past the validator's ``pr.*``
        field checks via an ``AttributeError`` on ``.get('pr')``.
        """
        content = "# Plan\n\n```yaml\n# yaml-tasks\n- a\n- b\n```\n"
        with pytest.raises(PlanPreflightError) as excinfo:
            validate_plan_for_implement_phase(content)
        # parse_yaml_code_fence rejects non-dict top-level YAML
        # upstream, so the validator surfaces this as case (a).
        assert excinfo.value.missing_fields == ["yaml-tasks"]

    def test_exception_chains_no_inner_attribute_error(self):
        """The validator must never propagate an ``AttributeError`` /
        ``TypeError`` to callers — even adversarial inputs.

        Adversarial: ``yaml_data.get('pr')`` on a non-dict would raise
        ``AttributeError`` if the type guard regressed. Pin that the
        only exception type that escapes is ``PlanPreflightError``.
        """
        content = "# Plan\n\n```yaml\n# yaml-tasks\n[1, 2, 3]\n```\n"
        # Must raise PlanPreflightError, not AttributeError/TypeError.
        with pytest.raises(PlanPreflightError):
            validate_plan_for_implement_phase(content)

    def test_validator_exported_via___all__(self):
        """The validator and exception must be in plan_parser.__all__ so
        importers using ``from plan_parser import *`` see them.

        Adversarial: a coder who added the symbols without updating
        ``__all__`` would break wildcard imports elsewhere in the
        codebase. Pin the export so a regression fails fast.
        """
        from egg_contracts import plan_parser

        assert "validate_plan_for_implement_phase" in plan_parser.__all__
        assert "PlanPreflightError" in plan_parser.__all__

    def test_validator_return_annotation_is_none(self):
        """Explicit type pin: the validator is annotated ``-> None``.

        Adversarial: a refactor that returned ``yaml_data`` for caller
        reuse would silently change the API. The plan calls for a
        side-effect-free validator that raises on failure. Mypy is
        configured under ``-> None`` so attempting to use the return
        value would surface as a typing regression in
        ``make lint`` — pin the annotation explicitly here so a
        local refactor that bumps the return type is also caught
        by the runtime suite.
        """
        import inspect

        sig = inspect.signature(validate_plan_for_implement_phase)
        assert sig.return_annotation is None or sig.return_annotation == "None"

    def test_planpreflighterror_is_an_exception(self):
        """The error class must subclass ``Exception`` (so the BRC NACK
        renderer's exception-handling path catches it cleanly).

        Adversarial: a refactor that switched to ``BaseException`` would
        let it slip past ``except Exception`` handlers and trigger
        process-level abort. Pin the base class.
        """
        assert issubclass(PlanPreflightError, Exception)
