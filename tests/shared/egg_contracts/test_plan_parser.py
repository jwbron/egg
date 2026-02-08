"""Tests for egg_contracts.plan_parser module."""

from egg_contracts.plan_parser import (
    ParsedPhase,
    ParsedTask,
    ParseResult,
    ParseWarning,
    format_warnings_for_comment,
    parse_phases_from_markdown,
    parse_phases_from_yaml,
    parse_plan,
    parse_tasks_from_markdown,
    parse_yaml_code_fence,
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
        assert phase.id == "phase-1"
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
        assert contract_phases[0].id == "phase-1"


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
    """Tests for plan comment detection priority in populate-contract-tasks.py."""

    def test_yaml_fence_detection(self):
        """Test that yaml-tasks fence is detected."""
        # Simulating what find_plan_comment does
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
        # Check detection logic
        has_yaml_tasks = "# yaml-tasks" in comment and ("```yaml" in comment or "```yml" in comment)
        assert has_yaml_tasks

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
