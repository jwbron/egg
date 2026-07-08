"""Refine-phase agent role definitions (#3543 decomposition).

Refine producers (refiner, simplifier; the simplifier also runs in the
plan phase) and the refine-phase reviewers (reviewer_refine,
reviewer_agent_design, first_principles_reviewer).
"""

from ._core import (
    _REVIEWER_BLOCKED_WRITE,
    AgentCategory,
    AgentRole,
    AgentRoleDefinition,
    FileAccessPattern,
)

# Refine-phase agent role definitions

REFINER_ROLE = AgentRoleDefinition(
    role=AgentRole.REFINER,
    description="Analyzes the task and produces a structured analysis in the refine phase",
    category=AgentCategory.ANALYSIS,
    responsibilities=[
        "Understand the problem or feature request",
        "Research the current codebase to understand existing patterns",
        "Identify constraints and dependencies",
        "Consider multiple implementation approaches with pros/cons",
        "Recommend an approach with justification",
        "Surface open questions as HITL decisions or feedback requests",
        "Write analysis to the draft file (NOT an implementation plan)",
    ],
    dependencies=[],  # Refiner runs first, no dependencies
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files
        allowed_write=[
            ".egg-state/drafts/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=[
            "**/*.py",
            "**/*.ts",
            "**/*.tsx",
            "**/*.js",
            "**/*.jsx",
            "**/*.go",
            "**/*.java",
            ".egg-state/contracts/",
        ],
    ),
    produces_outputs=["analysis_draft"],
    requires_inputs=[],
)

# Simplifier role (human-focused draft companions). Runs in BOTH the
# refine and plan phases as a producer of the ``*-human.md`` companion
# copy. It does NOT review the upstream producer — its work simply
# depends on the producer's pushed draft, so it is sequenced via the
# BRC dependency prompt (the coder→tester convention), not a review
# edge. ``dependencies`` is left empty: that field feeds the
# write-overlap / dependency-graph wave analysis (which runs per-phase
# against the phase's role set), and this single role-def spans two
# phases with two different upstreams, so coupling it to one producer
# here would be wrong. File access mirrors the refiner: drafts and
# agent-outputs only.
SIMPLIFIER_ROLE = AgentRoleDefinition(
    role=AgentRole.SIMPLIFIER,
    description=(
        "Distills the producer's draft into a jargon-free, human-focused "
        "companion summary for a broad audience (engineers, PMs, managers) "
        "in the refine and plan phases"
    ),
    category=AgentCategory.ANALYSIS,
    responsibilities=[
        "Read the upstream producer's draft (refine analysis or plan)",
        "Write a simplified, higher-level companion that captures the essence",
        "Keep it digestible for a broad audience — engineers, PMs, and "
        "managers — readable by a non-engineer",
        "Use no egg-internal jargon (no BRC, consensus, slice-DAG, contract, "
        "propose/ACK/NACK, phase, or agent-role terms) and no implementation "
        "minutiae (no file:line refs or code identifiers)",
        "Summarise the draft — do NOT review or critique it; no ACK/NACK or "
        "constraint-list framing in the companion",
        "Faithfully reflect the upstream draft — introduce no new scope",
    ],
    dependencies=[],
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files
        allowed_write=[
            ".egg-state/drafts/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=[
            "**/*.py",
            "**/*.ts",
            "**/*.tsx",
            "**/*.js",
            "**/*.jsx",
            "**/*.go",
            "**/*.java",
            ".egg-state/contracts/",
        ],
    ),
    produces_outputs=["analysis_draft_human", "plan_draft_human"],
    requires_inputs=["analysis_draft", "task_breakdown"],
)


REVIEWER_AGENT_DESIGN_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_AGENT_DESIGN,
    description="Reviews agent-mode design alignment",
    category=AgentCategory.REVIEW,
    responsibilities=[
        "Check for agent-mode design anti-patterns",
        "Verify autonomous operation capability",
        "Assess human-in-the-loop integration",
    ],
    dependencies=[AgentRole.REFINER],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=["analysis_draft"],
)

REVIEWER_REFINE_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_REFINE,
    description="Reviews refine phase analysis quality and completeness",
    category=AgentCategory.REVIEW,
    responsibilities=[
        "Verify the analysis correctly identifies the core problem",
        "Assess research quality and codebase exploration",
        "Evaluate options analysis and trade-off reasoning",
        "Check that constraints and dependencies are identified",
        "Validate the recommendation is justified and actionable",
    ],
    dependencies=[AgentRole.REFINER],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=["analysis_draft"],
)

# Adversarial first-principles reviewer (refine phase). Unlike
# ``reviewer_refine`` (which judges the *quality* of the analysis) this role
# questions the *premise and direction*: is the seed's rationale sound, is the
# stated direction the right one, or is there a materially simpler path / a
# better approach / a scope change — or should the work not happen at all? Its
# subject is the operator's seed (``contract.task_description``), read against
# codebase reality, plus the direction the refiner's draft is taking. It does
# NOT NACK the refiner (the refiner cannot rewrite the operator-owned seed, so
# a NACK is the wrong channel); it ACKs and, when it finds a concern worth a
# human's call, surfaces a redirect as a phase-scoped HITL decision. Mapped to
# ``Role.REVIEWER``; reviewers gained decision-create access in roles.py so it
# can raise that decision itself. File access mirrors the other refine
# reviewers: it writes its assessment to ``.egg-state/agent-outputs/`` (the
# accept-path reads the proposed redirect back from there) and its verdict to
# ``.egg-state/reviews/`` — never source, drafts, or contracts.
FIRST_PRINCIPLES_REVIEWER_ROLE = AgentRoleDefinition(
    role=AgentRole.FIRST_PRINCIPLES_REVIEWER,
    description=(
        "Adversarially reviews the pipeline's seed and the refiner's direction "
        "from first principles, surfacing significant redirects as HITL "
        "decisions in the refine phase"
    ),
    category=AgentCategory.REVIEW,
    responsibilities=[
        "Read the seed (the operator's task statement) and the refiner's draft",
        "Question the premise: is the rationale sound and the direction right?",
        "Surface concrete redirects where warranted — a materially simpler "
        "path, a different approach, a scope change, or not building it at all",
        "Raise redirects as phase-scoped HITL decisions for the operator; "
        "never NACK the refiner on first-principles grounds",
        "ACK the refiner once the first-principles pass is done",
    ],
    dependencies=[AgentRole.REFINER],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict", "first_principles_assessment"],
    requires_inputs=["analysis_draft"],
)
