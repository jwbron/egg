"""Tests for egg_contracts.plan_parser module."""

import pytest
from egg_contracts.plan_parser import (
    ParsedPhase,
    ParsedTask,
    ParseResult,
    ParseWarning,
    format_warnings_for_comment,
    parse_plan,
    parse_tasks_from_markdown,
    parse_phases_from_markdown,
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
        assert task.id == "task-1"
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
        assert "placeholder" in phase2_tasks[0].description.lower() or "manually" in phase2_tasks[0].description.lower()
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
