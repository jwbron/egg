"""Data models, typed errors, and compiled patterns for the plan parser.

Pure data layer extracted from the pre-split ``plan_parser.py`` (#3312
slice-7). Holds the parsed-document dataclasses (:class:`ParsedTask`,
:class:`ParsedPhase`, :class:`ParseWarning`, :class:`ParseResult`), the
typed :class:`PlanPreflightError`, the per-task Jira-field constants, and
the compiled regexes the YAML / markdown parsers share. Imported by every
other submodule; imports nothing from siblings, so it sits at the base of
the package's internal dependency DAG.

Every symbol here is AST-identical to its pre-split definition and
re-exports through the package barrel (``__init__.py``), so
``from egg_contracts.plan_parser import ParsedTask`` and
``patch("egg_contracts.plan_parser.ParseWarning")`` keep resolving.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..models import Slice, SliceStatus, Task, TaskStatus

# Placeholder acceptance criteria for tasks that couldn't be parsed.
# Used as a sentinel value to filter out non-real criteria during aggregation.
PLACEHOLDER_ACCEPTANCE_CRITERIA = "Human verification"


class PlanPreflightError(Exception):
    """Typed exception raised by :func:`validate_plan_preflight` when the
    planner output is missing structural inputs the orchestrator depends
    on (#2777, AC-1a).

    Carries a structured ``missing_fields`` payload so the BRC NACK
    surface — and the human-facing 422 returned by ``advance_phase`` —
    can name each missing field by name rather than emitting a generic
    "plan invalid" message.

    Derives from :class:`Exception` (not :class:`BaseException`):
    application-level callers that need to surface the error explicitly
    handle ``PlanPreflightError`` ahead of any broad ``except Exception``
    so the rejection always reaches the BRC NACK / 422 surface. Tests in
    TASK-3-8 assert the error is not swallowed by the four implement-
    phase entry paths.

    Attributes:
        missing_fields: Ordered list of field names that failed validation
            (e.g. ``["yaml-tasks", "pr.test_plan"]``). The first entry is
            also formatted into ``str(error)`` so logging shows the
            principal failure without consumers needing to special-case
            the structured payload.
    """

    def __init__(self, missing_fields: list[str], detail: str | None = None) -> None:
        if not missing_fields:
            # Mirroring the orchestrator's "must name the field" contract:
            # an empty payload would surface as a generic message and
            # defeats the purpose of the typed exception.
            raise ValueError("PlanPreflightError requires at least one missing field name")
        self.missing_fields: list[str] = list(missing_fields)
        self.detail: str | None = detail
        # Stable message shape so the BRC NACK surface and the 422 body
        # both render the same actionable text. Lead with the first
        # missing field; the full list is available on ``missing_fields``.
        primary = missing_fields[0]
        joined = ", ".join(missing_fields)
        message_parts = [
            f"Plan pre-flight validation failed: missing {primary}",
        ]
        if len(missing_fields) > 1:
            message_parts.append(f"(all missing: {joined})")
        if detail:
            message_parts.append(f"— {detail}")
        super().__init__(" ".join(message_parts))


# Valid values for the optional ``jira_action`` per-task YAML key
# (issue #1557 — Jira-epic SDLC support). Mirrors the ``Literal`` in
# ``Task.jira_action`` so the parser can reject unknown values with a
# ParseWarning instead of letting them slip through as silent drops.
JIRA_ACTION_VALUES = frozenset({"create", "edit", "wontdo", "split-of", "consolidate-into"})

# Valid values for the optional ``jira_action_status`` per-task YAML key
# (issue #1557 — Jira-epic SDLC support). Mirrors the ``Literal`` in
# ``Task.jira_action_status``. ``None`` (key absent) is also valid and
# is treated as ``'pending'`` by the APPLIER.
JIRA_ACTION_STATUS_VALUES = frozenset({"pending", "in_flight", "applied", "failed"})

# Pattern for ``jira_key`` per-task YAML key (issue #1557). Mirrors
# ``Task.jira_key`` exactly so the parser's warning matches the
# downstream Pydantic validator. Compiled once at import.
_JIRA_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$")

# Every per-slice key the parser actually consumes from the ``# yaml-tasks``
# appendix. Mirrors the ``slice`` definition in
# ``.egg/schemas/yaml-tasks.schema.json`` (which is ``additionalProperties:
# false``) plus the ``depends_on`` alias the parser tolerates (#2743). A key
# outside this set is silently ignored by the parser, so an unrecognised key
# means the planner expressed something the contract never sees — exactly the
# #2870 failure mode, where the architect emitted ``parent_slice_id`` and the
# whole slice dependency chain was dropped. The schema would have rejected it,
# but the schema is only enforced in tests, never at parse/populate time — so
# we surface the unknown key as a parse warning here to make the drift loud
# instead of silent.
_KNOWN_SLICE_KEYS = frozenset(
    {
        "id",
        "name",
        "goal",
        "dependencies",
        "depends_on",
        "serialized_chain_order",
        "exit_criteria",
        "tasks",
    }
)


@dataclass
class ParsedTask:
    """A task extracted from a plan document."""

    id: str
    phase_number: int
    task_number: int
    description: str
    acceptance_criteria: str
    files_affected: list[str] = field(default_factory=list)
    role: str | None = None
    # Jira-epic SDLC support (issue #1557). Optional per-task fields the
    # task-planner emits for epic-mode pipelines so the APPLIER can drive
    # idempotent Jira mutations on plan-gate approval. Default ``None`` —
    # ticket / github_issue mode plans never populate these.
    jira_key: str | None = None
    jira_action: str | None = None
    jira_action_status: str | None = None

    def to_contract_task(self) -> Task:
        """Convert to a contract Task model."""
        return Task(
            id=f"task-{self.phase_number}-{self.task_number}",
            description=self.description,
            status=TaskStatus.PENDING,
            acceptance_criteria=self.acceptance_criteria,
            files_affected=self.files_affected,
            role=self.role,
            jira_key=self.jira_key,
            jira_action=self.jira_action,  # type: ignore[arg-type]
            jira_action_status=self.jira_action_status,  # type: ignore[arg-type]
        )


@dataclass
class ParsedPhase:
    """A slice (legacy: phase) extracted from a plan document.

    The class name is preserved for backward compat but post-#2137 the
    canonical SDLC term is "slice". The plan parser accepts either
    ``slices:`` (canonical) or ``phases:`` (legacy alias) at the
    ``# yaml-tasks`` level — see ``parse_phases_from_yaml``.
    """

    number: int
    name: str
    goal: str
    tasks: list[ParsedTask] = field(default_factory=list)
    dependencies: str = ""
    exit_criteria: str = ""
    serialized_chain_order: list[str] = field(default_factory=list)

    def to_contract_phase(self) -> Slice:
        """Convert to a contract Slice model (legacy alias name)."""
        return self.to_contract_slice()

    def to_contract_slice(self) -> Slice:
        """Convert to a contract Slice model.

        Renamed from ``to_contract_phase`` in #2137. The output uses
        the canonical ``slice-<N>`` ID shape; legacy ``phase-<N>``
        dependency strings emitted by older planners are translated
        to ``slice-<N>`` so post-rename consumers see a uniform DAG.
        """
        # Normalize dependencies to slice-N format
        normalized_deps: list[str] = []
        if self.dependencies:
            raw_deps: Any = self.dependencies
            # Handle scalar (int / str), list, and integer formats. The
            # bare-int branch (``depends_on: 3``) was added for #2743:
            # pipeline-8b81ed32 declared ``depends_on: <int>`` on every
            # slice and the contract came back with empty deps because
            # the int fell through ``isinstance(dep_list, list)`` below.
            dep_list: list[str]
            if isinstance(raw_deps, bool):
                # ``bool`` is a subclass of ``int`` in Python — reject
                # explicitly to avoid silently converting ``True`` to
                # ``slice-1``.
                dep_list = []
            elif isinstance(raw_deps, int):
                dep_list = [str(raw_deps)]
            elif isinstance(raw_deps, str):
                dep_list = [d.strip() for d in raw_deps.split(",") if d.strip()]
            elif isinstance(raw_deps, list):
                dep_list = [str(d) for d in raw_deps]
            else:
                dep_list = []
            if isinstance(dep_list, list):
                for dep in dep_list:
                    dep_str = str(dep).strip()
                    if dep_str.startswith("slice-"):
                        normalized_deps.append(dep_str)
                    elif dep_str.startswith("phase-"):
                        # Legacy planner output — rewrite the prefix.
                        normalized_deps.append("slice-" + dep_str[len("phase-") :])
                    else:
                        # Try to extract slice/phase number — prefer
                        # explicit "slice N" / "phase N" patterns to
                        # avoid extracting unrelated numbers from prose.
                        m = re.search(r"(?:slice|phase)\s*(\d+)", dep_str, re.IGNORECASE)
                        if not m:
                            # Fall back to bare number only if the string is
                            # short (likely just "1" or "2", not prose).
                            if len(dep_str) <= 10:
                                m = re.search(r"(\d+)", dep_str)
                        if m:
                            normalized_deps.append(f"slice-{m.group(1)}")

        # Normalise serialized_chain_order entries the same way so the
        # planner can emit either ``slice-N`` or ``phase-N`` and the
        # contract always sees the canonical form.
        normalised_chain: list[str] = []
        for entry in self.serialized_chain_order:
            entry_str = str(entry).strip()
            if entry_str.startswith("slice-"):
                normalised_chain.append(entry_str)
            elif entry_str.startswith("phase-"):
                normalised_chain.append("slice-" + entry_str[len("phase-") :])
            else:
                m = re.search(r"(?:slice|phase)\s*(\d+)", entry_str, re.IGNORECASE)
                if not m and len(entry_str) <= 10:
                    m = re.search(r"(\d+)", entry_str)
                if m:
                    normalised_chain.append(f"slice-{m.group(1)}")

        return Slice(
            id=f"slice-{self.number}",
            name=self.name,
            goal=(self.goal or "").strip(),
            status=SliceStatus.PENDING,
            tasks=[task.to_contract_task() for task in self.tasks],
            dependencies=normalized_deps,
            serialized_chain_order=normalised_chain,
        )


@dataclass
class ParseWarning:
    """A warning generated during parsing."""

    line_number: int | None
    message: str
    context: str = ""


@dataclass
class ParseResult:
    """Result of parsing a plan document."""

    success: bool
    phases: list[ParsedPhase] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
    error: str | None = None
    raw_yaml: dict[str, Any] | None = None
    pr_title: str | None = None
    pr_description: str | None = None
    pr_test_plan: str | None = None
    pr_manual_steps: str | None = None
    # NOTE: the separate planner-emitted PR context-framing fields (#2548)
    # were removed in #2777 (cq-2 / cq-4). Under the new context-PR
    # topology the context PR opens on the work branch and reads its
    # title/body from ``pr_title`` / ``pr_description`` directly, so the
    # separate framing fields are obsolete.

    def to_contract_phases(self) -> list[Slice]:
        """Backward-compat alias for ``to_contract_slices`` (#2137).

        The canonical name is now ``to_contract_slices`` since the
        contract field is ``slices``; this alias keeps existing
        callers working during the transition window.
        """
        return self.to_contract_slices()

    def to_contract_slices(self) -> list[Slice]:
        """Convert all parsed slices to contract Slice models."""
        return [phase.to_contract_slice() for phase in self.phases]


# Regex pattern for task IDs in markdown
# Matches: [TASK-{phase}-{number}] description — Acceptance: criteria
TASK_PATTERN = re.compile(
    r"\[TASK-(\d+)-(\d+)\]\s*(.+?)\s*(?:—|--|-)\s*Acceptance:\s*(.+)",
    re.IGNORECASE,
)

# Pattern for phase headers
# Matches: ### Phase N: Name or ## Phase N: Name
PHASE_HEADER_PATTERN = re.compile(
    r"^#{2,3}\s*Phase\s+(\d+):\s*(.+)",
    re.IGNORECASE | re.MULTILINE,
)

# Pattern for goal lines within a phase section
GOAL_PATTERN = re.compile(r"\*\*Goal\*\*:\s*(.+)", re.IGNORECASE)

# Pattern for files in brackets
FILES_PATTERN = re.compile(r"\[([^\]]+)\]")

# Pattern for YAML code fence with yaml-tasks marker
# Matches: ```yaml\n# yaml-tasks\n...\n```
#
# The closing ``` must start at a line boundary (optionally preceded by 0–3
# CommonMark-style spaces). Without that anchor the non-greedy capture stops
# at the first inner ``` inside a YAML block scalar — e.g. a slice's ``goal:
# |`` block that demonstrates a shell command — silently truncating the rest
# of the slices block.  Issue #2743 (pipeline-f4c7d780): only 7 of 15 slices
# made it into the contract because slice 7's goal embedded an indented
# fenced example.
YAML_FENCE_PATTERN = re.compile(
    r"```(?:yaml|yml)\s*\n\s*#\s*yaml-tasks\s*\n((?:.*\n)*?)[ ]{0,3}```\s*(?:\n|$)",
    re.IGNORECASE,
)
