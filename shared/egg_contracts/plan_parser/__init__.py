"""
Plan document parser for extracting tasks into contract format.

This module parses plan documents (markdown) and extracts structured task
information that can be written to the contract JSON.

Parsing Strategy (Option C - Two-Pass Approach):
    The parser supports three extraction modes, in order of preference:

    1. YAML Code Fence (preferred): A ```yaml block marked with `# yaml-tasks`
       header containing structured task data. This is machine-parseable and
       type-checked, while allowing humans to review the prose plan above it.

    2. YAML Front Matter (legacy): A ---delimited YAML block at the start of
       the document. Still supported for backwards compatibility.

    3. Markdown Regex (fallback): Extract tasks from markdown list items using
       the [TASK-{phase}-{number}] pattern. This is fragile and may miss tasks
       if the LLM's output format drifts.

Task ID Format:
    Tasks must use the format: TASK-{phase}-{number}
    Example: TASK-1-1, TASK-2-3

YAML Code Fence Format:
    The structured appendix should be a YAML code block with the marker comment.
    Use block scalars (``|-``) for ``name``, ``goal``, ``description``, and
    ``acceptance`` — plain unquoted scalars break when the value contains a
    ``: `` sequence (e.g. a backticked ``code: type`` snippet), because PyYAML
    interprets it as a nested mapping and raises ``ScannerError``. See #1974.

    ```yaml
    # yaml-tasks
    phases:
      - id: 1
        name: |-
          Setup
        goal: |-
          Initialize the project
        tasks:
          - id: TASK-1-1
            description: |-
              Create contract JSON schema
            acceptance: |-
              Schema validates sample contracts
            files:
              - .egg/schemas/contract.schema.json
    ```

Parse Failure Handling:
    - If a phase contains no parseable tasks, a placeholder task is created
    - If the plan document is missing or malformed, parsing fails with a structured error
    - Parse results include a warnings[] array for human review
    - If YAML code fence parsing fails, falls back to markdown regex with a warning


Decomposition note (#3312 slice-7): the pre-split single-file module is
now a sub-package. This ``__init__.py`` is the **stable public API
barrel** -- every externally-referenced and ``unittest.mock.patch``-target
symbol re-exports here, so ``from egg_contracts.plan_parser import X`` and
``patch("egg_contracts.plan_parser.X")`` keep resolving unchanged. The
implementation lives in underscore-prefixed private submodules:

- ``_models``         -- dataclasses, ``PlanPreflightError``, Jira/slice
  constants, compiled regex patterns
- ``_yaml_parse``     -- ``# yaml-tasks`` fence + front-matter + per-task /
  per-slice / ``pr`` extraction
- ``_markdown_parse`` -- fragile markdown-regex fallback extraction
- ``_orchestration``  -- ``parse_plan`` / ``parse_plan_file`` three-tier
  driver + ``format_warnings_for_comment``
- ``_validators``     -- forest/cycle (#2137), file-overlap (#3046),
  role<->files (#2527), and pre-flight (#2777) validators

Pure refactor, no behaviour change: every re-exported symbol is
AST-identical to the pre-split definition. ``validate_plan_preflight``
calls ``parse_plan`` through this package module object so the
``patch("egg_contracts.plan_parser.parse_plan")`` seam keeps intercepting
it exactly as in the single-file module.
"""

from __future__ import annotations

from ._markdown_parse import parse_phases_from_markdown, parse_tasks_from_markdown
from ._models import (
    _JIRA_KEY_PATTERN,
    _KNOWN_SLICE_KEYS,
    FILES_PATTERN,
    GOAL_PATTERN,
    JIRA_ACTION_STATUS_VALUES,
    JIRA_ACTION_VALUES,
    PHASE_HEADER_PATTERN,
    PLACEHOLDER_ACCEPTANCE_CRITERIA,
    TASK_PATTERN,
    YAML_FENCE_PATTERN,
    ParsedPhase,
    ParsedTask,
    ParseResult,
    ParseWarning,
    PlanPreflightError,
)
from ._orchestration import format_warnings_for_comment, parse_plan, parse_plan_file
from ._validators import (
    _check_role_files,
    _detect_cycles,
    _eligible_producer_roles,
    _is_file_blocked_for_role,
    validate_forest,
    validate_plan_preflight,
    validate_slice_file_overlap,
    validate_task_role_alignment,
)
from ._yaml_parse import (
    _extract_jira_task_fields,
    _line_value_is_none,
    _normalize_optional_string,
    extract_pr_metadata_from_yaml,
    parse_phases_from_yaml,
    parse_tasks_from_yaml,
    parse_yaml_code_fence,
    parse_yaml_frontmatter,
)

# ``__all__`` lists the full re-export surface of the barrel. It is a
# superset of the pre-split module's original ``__all__`` (the 11 public
# names below the divider); the additional entries are symbols that were
# always importable as module globals on the single-file module and that
# external code / tests reference by name, so the barrel keeps them on the
# public API. No consumer used ``from egg_contracts.plan_parser import *``.
__all__ = (
    # ---- original pre-split ``__all__`` (public API) ----
    "ParsedPhase",
    "ParsedTask",
    "ParseResult",
    "ParseWarning",
    "PlanPreflightError",
    "format_warnings_for_comment",
    "parse_plan",
    "parse_plan_file",
    "validate_forest",
    "validate_plan_preflight",
    "validate_task_role_alignment",
    # ---- additional re-exported module globals (stable barrel surface) ----
    "FILES_PATTERN",
    "GOAL_PATTERN",
    "JIRA_ACTION_STATUS_VALUES",
    "JIRA_ACTION_VALUES",
    "PHASE_HEADER_PATTERN",
    "PLACEHOLDER_ACCEPTANCE_CRITERIA",
    "TASK_PATTERN",
    "YAML_FENCE_PATTERN",
    "_JIRA_KEY_PATTERN",
    "_KNOWN_SLICE_KEYS",
    "_check_role_files",
    "_detect_cycles",
    "_eligible_producer_roles",
    "_extract_jira_task_fields",
    "_is_file_blocked_for_role",
    "_line_value_is_none",
    "_normalize_optional_string",
    "extract_pr_metadata_from_yaml",
    "parse_phases_from_markdown",
    "parse_phases_from_yaml",
    "parse_tasks_from_markdown",
    "parse_tasks_from_yaml",
    "parse_yaml_code_fence",
    "parse_yaml_frontmatter",
    "validate_slice_file_overlap",
)
