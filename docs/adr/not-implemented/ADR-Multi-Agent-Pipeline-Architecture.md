# ADR: Multi-Agent Pipeline Architecture

**Driver:** James Wiesebron
**Approver:** TBD
**Contributors:** James Wiesebron, Tyler Burleigh, Claude (AI Pair Programming)
**Informed:** Engineering teams
**Proposed:** November 2025
**Status:** Proposed (Not Implemented)

---

## Table of Contents

- [Context](#context)
- [Decision](#decision)
- [Decision Matrix](#decision-matrix)
- [Multi-Agent Architecture](#multi-agent-architecture)
- [Context Engineering Principles](#context-engineering-principles)
- [Prompt Architecture](#prompt-architecture)
- [Agent Design Patterns](#agent-design-patterns)
- [Pipeline Patterns](#pipeline-patterns)
- [Review and Feedback Loops](#review-and-feedback-loops)
- [Model Selection Strategy](#model-selection-strategy)
- [Failure Modes & Reliability](#failure-modes--reliability)
- [Security & Isolation](#security--isolation)
- [Interoperability Standards](#interoperability-standards)
- [Enterprise Case Studies](#enterprise-case-studies)
- [Implementation Phases](#implementation-phases)
- [Consequences](#consequences)
- [Decision Permanence](#decision-permanence)
- [Alternatives Considered](#alternatives-considered)
- [References](#references)

## Context

### Background

**Problem Statement:**

The current egg architecture uses a **single-agent, single-invocation pattern** for task processing. Each workflow (Slack task, PR review, check failure analysis, etc.) invokes Claude Code once with a comprehensive prompt and relies on that single invocation to:

1. Understand the full context
2. Make all decisions
3. Execute all sub-tasks
4. Generate output

This approach has several limitations:

1. **Cognitive Load:** Complex tasks require a single agent to juggle multiple concerns simultaneously (understanding requirements, analyzing code, making decisions, testing, documenting)
2. **Failure Modes:** If the agent fails partway through, all progress is lost (no checkpointing)
3. **Context Window Pressure:** All context must be loaded upfront, even if only portions are needed
4. **Quality Variance:** Single-pass execution doesn't allow for review or refinement of intermediate steps
5. **Specialization Limits:** One prompt template must handle diverse task types (analysis vs. implementation vs. review)
6. **Parallel Work Constraints:** Can't divide work across multiple agents running concurrently

**Current Implementation:**

```
Slack Task → incoming-processor.py → Claude Code (single invocation)
  ├─ Understands task
  ├─ Gathers context
  ├─ Plans approach
  ├─ Implements changes
  ├─ Tests
  ├─ Creates PR
  └─ Updates beads
```

**Industry Patterns (2025):**

Multi-agent systems have become established best practice for complex LLM workflows. Research from [Anthropic's multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) showed **over 90% performance improvement** when using Claude Opus 4 as lead agent with Claude Sonnet 4 subagents compared to single-agent setups. [Microsoft Azure](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns), [Google Cloud](https://cloud.google.com/architecture/choose-design-pattern-agentic-ai-system), and industry practitioners have standardized on core orchestration patterns:

| Pattern | Example | Benefit | When to Use |
|---------|---------|---------|-------------|
| **Sequential Pipeline** | Plan → Implement → Test → Review | Specialization, checkpointing, clear dependencies | Multi-stage processes with linear workflow |
| **Parallel/Concurrent** | Multiple PR checks simultaneously | Reduced latency (vs sequential), comprehensive coverage | Independent analyses, time-sensitive workflows |
| **Hierarchical/Delegating** | Coordinator → Specialist agents | Complex orchestration, sub-task delegation | Large tasks requiring diverse expertise |
| **Debate/Consensus** | Multiple agents propose, best chosen | Quality through diversity, reduced bias | High-stakes decisions, ambiguous requirements |
| **Tool-Specialized** | Code agent, research agent, test agent | Deep expertise per domain | Domain-specific tasks requiring specialized knowledge |
| **Iterative Refinement** | Code → Test → Fix loop (max 3 iterations) | Quality improvement, graceful failure handling | Flaky processes, quality-critical outputs |

**Key Industry Insight**: According to 2025 industry data, over 80% of enterprise workloads are expected to run on AI-driven systems by 2026, with multi-agent architectures leading this transformation.

### What We're Deciding

This ADR proposes a **multi-agent pipeline architecture** for egg where:

1. **Workflows are decomposed into stages** with clear inputs/outputs
2. **Each stage can invoke a specialized agent** with tailored prompts and context
3. **Agents can run sequentially** (with checkpointing) or **in parallel** (for throughput)
4. **Coordination logic** manages agent orchestration, state, and error handling
5. **Reusable agent templates** enable consistent patterns across workflows

### 2025 Best Practices from Industry

Based on research from [Anthropic](https://www.anthropic.com/engineering/multi-agent-research-system), [Microsoft](https://azure.microsoft.com/en-us/blog/agent-factory-the-new-era-of-agentic-ai-common-use-cases-and-design-patterns/), and [leading AI practitioners](https://collabnix.com/multi-agent-and-multi-llm-architecture-complete-guide-for-2025/), the following principles emerged as critical for successful multi-agent systems:

**1. Context Engineering (Critical Success Factor)**

Context engineering is **the #1 job of engineers building AI agents** in 2025. Anthropic's research found that early agent systems failed when agents:
- Spawned 50 subagents for simple queries (lack of task scoping)
- Scoured the web endlessly for nonexistent sources (no termination criteria)
- Distracted each other with excessive updates (poor coordination boundaries)

**Each subagent must have:**
- Clear objective and output format
- Guidance on tools and sources to use
- Explicit task boundaries (what NOT to do)
- Termination criteria

**2. Economic Viability Through Value Alignment**

[Industry data](https://medium.com/@anishnarayan09/agentic-ai-automation-optimize-efficiency-minimize-token-costs-69185687713c) shows that:
- Agents use **~4× more tokens** than chat interactions
- Multi-agent systems use **~15× more tokens** than chats
- Complex agents with tool-calling consume **5-20× more tokens** than simple chains due to loops and retries

**Rule of thumb**: Multi-agent systems require tasks where the **value of the task is high enough** to pay for increased performance and cost.

**3. When to Use Multi-Agent vs Single-Agent**

Multi-agent systems excel at:
- Tasks requiring heavy parallelization
- Information exceeding single context windows
- Interfacing with numerous complex tools
- Tasks where quality/accuracy improvements justify 15× token cost

Multi-agent systems designed for **"reading" tasks** (analysis, research) tend to be more manageable than **"writing" tasks** (code generation, content creation).

**4. Orchestration Framework Selection**

[Industry guidance](https://research.aimultiple.com/agentic-orchestration/) suggests using orchestrators when you have **3+ of**:
- State management requirements
- Branching logic
- Parallelism needs
- Multiple tools/LLMs
- Strict observability requirements

**5. State Management and Checkpointing**

[Research on checkpoint/restore systems](https://eunomia.dev/blog/2025/05/11/checkpointrestore-systems-evolution-techniques-and-applications-in-ai-agents/) and [LangGraph state machines](https://dev.to/jamesli/langgraph-state-machines-managing-complex-agent-task-flows-in-production-36f4) emphasize:
- Every agent state change should be durably checkpointed
- Agents must survive crashes and infrastructure updates
- State should enable time-travel debugging
- Centralized state management prevents conflicts

### Key Requirements

**Functional:**
1. **Backward Compatibility:** Existing single-agent workflows continue to work
2. **Incremental Adoption:** Can migrate workflows one at a time to multi-agent
3. **State Persistence:** Pipeline progress survives container restarts (Beads integration)
4. **Error Recovery:** Failed stages can retry without restarting entire pipeline
5. **Observable:** Clear visibility into which agent did what and why

**Non-Functional:**
1. **Performance:** Multi-agent should be faster or similar to single-agent (not slower)
2. **Cost-Effective:** Token usage optimized (don't load unnecessary context)
3. **Maintainable:** Pipeline definitions readable and easy to modify
4. **Extensible:** New workflows easy to build from existing agent templates

## Decision

**We will implement a multi-agent pipeline architecture with coordinated sequential and parallel agent execution, while maintaining backward compatibility with existing single-agent workflows.**

### Core Principles

1. **Workflows as Pipelines:** Complex tasks decomposed into stages with clear contracts
2. **Specialized Agents:** Each stage uses a tailored agent with focused responsibilities
3. **State as First-Class:** Pipeline state tracked in Beads for persistence and observability
4. **Fail-Safe Coordination:** Orchestrator handles retries, fallbacks, and error propagation
5. **Gradual Migration:** Existing workflows continue working; migrate selectively based on value

### Approach Summary

| Component | Purpose | Implementation |
|-----------|---------|----------------|
| **Pipeline Definition** | Declarative workflow specification | YAML or Python DSL |
| **Pipeline Orchestrator** | Executes stages, manages state | Python framework |
| **Agent Templates** | Reusable agent prompts and configs | Jinja2 templates + Python |
| **State Manager** | Persistence and recovery | Beads integration |
| **Execution Models** | Sequential, parallel, conditional | Built-in patterns |

## Decision Matrix

| Decision Area | Chosen Approach | Key Rationale | Rejected Alternatives |
|---------------|-----------------|---------------|----------------------|
| **Pipeline Format** | Python DSL with YAML fallback | Flexible, type-safe, debugging support, aligns with [LangGraph approach](https://latenode.com/blog/langgraph-vs-autogen-vs-crewai-complete-ai-agent-framework-comparison-architecture-analysis-2025) | Pure YAML (less expressive), pure code (verbose) |
| **Orchestration** | Custom Python framework | Lightweight, egg-specific, no external deps, control over checkpointing | Airflow (heavy), Prefect (overkill), Temporal (complex), Microsoft Agent Framework (see below) |
| **Agent Invocation** | Subprocess with timeout | Isolation, resource control, crash recovery | In-process (less isolation), containers (slow startup) |
| **State Storage** | Beads with structured notes | Already integrated, git-backed, follows [checkpoint/restore patterns](https://eunomia.dev/blog/2025/05/11/checkpointrestore-systems-evolution-techniques-and-applications-in-ai-agents/) | Separate DB (complexity), files (unstructured) |
| **Parallelism** | Process pool (multiprocessing) | Simple, reliable, bounded concurrency, follows [Azure concurrent pattern](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns) | Threads (GIL), async (event loop complexity) |
| **Agent Templates** | Jinja2 + Python functions | Familiar, powerful, reusable, [context engineering focused](https://www.anthropic.com/engineering/multi-agent-research-system) | Plain strings (not reusable), pure Python (verbose) |
| **Token Optimization** | Stage-specific context loading | Reduce 5-20× token overhead, [optimize costs](https://medium.com/@anishnarayan09/agentic-ai-automation-optimize-efficiency-minimize-token-costs-69185687713c) | Load all context upfront (wasteful) |

## Multi-Agent Architecture

### System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   Pipeline Orchestrator                      │
│                                                              │
│  ┌────────────────────────────────────────────────────┐   │
│  │           Pipeline Definition (YAML/Python)          │   │
│  │  - Stages: Plan → Implement → Test → Review         │   │
│  │  - Agents: Planner, Coder, Tester, Reviewer         │   │
│  │  - Inputs/Outputs: Context → Code → Tests → PR      │   │
│  └────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐     │
│  │ Stage 1 │→ │ Stage 2 │→ │ Stage 3 │→ │ Stage 4 │     │
│  │ Plan    │  │ Implement│  │  Test   │  │ Review  │     │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘     │
│       │            │            │            │             │
│       ▼            ▼            ▼            ▼             │
│  ┌─────────────────────────────────────────────────┐     │
│  │              Agent Executor                       │     │
│  │  - Loads agent template                          │     │
│  │  - Injects stage-specific context                │     │
│  │  - Invokes Claude Code (subprocess)              │     │
│  │  - Parses and validates output                   │     │
│  └─────────────────────────────────────────────────┘     │
│                                                              │
│  ┌─────────────────────────────────────────────────┐     │
│  │              State Manager (Beads)                │     │
│  │  - Pipeline status (pending/in_progress/done)    │     │
│  │  - Stage results (Plan: ADR-042, Code: 3 files) │     │
│  │  - Checkpointing and resumption                  │     │
│  └─────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────┘
```

### Pipeline Definition Example

**Python DSL (Preferred for Complex Workflows):**

```python
from egg.pipelines import Pipeline, Stage, Agent, ParallelStages

# Define reusable agents
planner = Agent(
    name="planner",
    template="agents/planner.jinja2",
    timeout=5 * 60,  # 5 minutes
)

implementer = Agent(
    name="implementer",
    template="agents/implementer.jinja2",
    timeout=30 * 60,  # 30 minutes
)

tester = Agent(
    name="tester",
    template="agents/tester.jinja2",
    timeout=10 * 60,
)

reviewer = Agent(
    name="reviewer",
    template="agents/reviewer.jinja2",
    timeout=5 * 60,
)

# Define pipeline
feature_implementation = Pipeline(
    name="feature_implementation",
    description="Implement a new feature from requirements to PR",

    stages=[
        Stage(
            name="analyze_requirements",
            agent=planner,
            inputs={"task_description": "input.task"},
            outputs={"plan": "plan.md", "files_to_change": "list[str]"},
        ),

        Stage(
            name="implement_changes",
            agent=implementer,
            inputs={
                "plan": "stages.analyze_requirements.outputs.plan",
                "files": "stages.analyze_requirements.outputs.files_to_change",
            },
            outputs={"changed_files": "list[str]", "commit_sha": "str"},
        ),

        ParallelStages(
            name="quality_checks",
            stages=[
                Stage(
                    name="run_tests",
                    agent=tester,
                    inputs={"changed_files": "stages.implement_changes.outputs.changed_files"},
                    outputs={"test_results": "dict", "passed": "bool"},
                ),
                Stage(
                    name="security_scan",
                    agent=Agent(name="security_scanner", template="agents/security.jinja2"),
                    inputs={"changed_files": "stages.implement_changes.outputs.changed_files"},
                    outputs={"vulnerabilities": "list", "safe": "bool"},
                ),
            ],
        ),

        Stage(
            name="create_pr",
            agent=reviewer,
            inputs={
                "commit_sha": "stages.implement_changes.outputs.commit_sha",
                "test_results": "stages.quality_checks.run_tests.outputs.test_results",
                "security_results": "stages.quality_checks.security_scan.outputs.vulnerabilities",
            },
            outputs={"pr_url": "str"},
            conditions={
                "stages.quality_checks.run_tests.outputs.passed": True,
                "stages.quality_checks.security_scan.outputs.safe": True,
            },
        ),
    ],
)
```

**YAML (Simpler Workflows):**

```yaml
name: pr_review
description: Review a pull request with multiple specialized agents

stages:
  - name: analyze_code
    agent: code_analyzer
    inputs:
      pr_number: "{{ input.pr_number }}"
      repository: "{{ input.repository }}"
    outputs:
      - issues: list
      - complexity_score: float

  - name: analyze_tests
    agent: test_analyzer
    inputs:
      pr_number: "{{ input.pr_number }}"
    outputs:
      - coverage_delta: float
      - missing_tests: list

  - name: generate_review
    agent: reviewer
    inputs:
      code_issues: "{{ stages.analyze_code.outputs.issues }}"
      test_issues: "{{ stages.analyze_tests.outputs.missing_tests }}"
    outputs:
      - review_comment: str
      - approval_status: str
```

### Agent Template Example

**Planner Agent (`agents/planner.jinja2`):**

```jinja2
# Task Planning Agent

You are a specialized planning agent. Your job is to analyze a task and create a detailed implementation plan.

## Task Description

{{ task_description }}

## Your Responsibilities

1. **Understand Requirements:** Extract functional and non-functional requirements
2. **Identify Files:** Determine which files need to be created/modified
3. **Design Approach:** Outline the implementation strategy
4. **List Dependencies:** Note any prerequisites or blockers
5. **Estimate Complexity:** Rate as simple/moderate/complex

## Output Format (JSON)

Return ONLY valid JSON with this structure:

    {
      "requirements": {
        "functional": ["req1", "req2"],
        "non_functional": ["perf requirement", "security requirement"]
      },
      "files_to_change": [
        {"path": "src/foo.py", "reason": "Add new function"},
        {"path": "tests/test_foo.py", "reason": "Add tests"}
      ],
      "approach": "High-level strategy here",
      "dependencies": ["ADR-042", "Redis client"],
      "complexity": "moderate",
      "estimated_stages": 3
    }

## Context

{%- if beads_context %}
### Previous Work
{{ beads_context }}
{%- endif %}

{%- if related_docs %}
### Related Documentation
{{ related_docs }}
{%- endif %}

## Important Notes

- Focus on PLANNING only - do not implement
- Be specific about file changes
- Consider edge cases and error handling
- Output ONLY the JSON (no explanations before/after)
```

### State Management with Beads

Pipeline state is tracked in Beads using structured notes:

```python
# Pipeline creates a Beads task on start
beads_id = bd.create(
    title=f"Pipeline: {pipeline.name}",
    labels=[f"pipeline:{pipeline.name}", f"task:{task_id}"],
    description=f"Multi-agent pipeline execution for {task_description}"
)

# Each stage updates the task
bd.update(
    beads_id,
    notes=f"""
Stage: {stage.name}
Status: in_progress
Started: {timestamp}
Agent: {stage.agent.name}
Inputs: {json.dumps(stage.inputs)}
""",
    status="in_progress"
)

# On stage completion
bd.update(
    beads_id,
    notes=f"""
Stage: {stage.name}
Status: completed
Duration: {duration}s
Outputs: {json.dumps(stage.outputs)}
""",
)

# Pipeline result stored in Beads
bd.update(
    beads_id,
    notes=f"""
Pipeline: {pipeline.name}
Status: completed
Total Duration: {total_duration}s
Stages Completed: {len(completed_stages)}
Final Output: {json.dumps(final_output)}
""",
    status="closed"
)
```

**Resumption:**

```python
# On container restart or failure recovery
def resume_pipeline(beads_id):
    task = bd.show(beads_id)
    pipeline_state = parse_beads_notes(task.notes)

    # Find last completed stage
    last_stage = pipeline_state["last_completed_stage"]

    # Resume from next stage
    remaining_stages = pipeline.stages[last_stage + 1:]
    execute_stages(remaining_stages, previous_outputs=pipeline_state["outputs"])
```

## Context Engineering Principles

Context engineering is **the #1 job of engineers building AI agents**. This section provides detailed guidance on how to structure and deliver context to agents effectively.

### ACE Framework Reference

The [ACE (Agentic Context Engineering)](https://arxiv.org/abs/2510.04618) framework provides academic grounding for context management in agent systems. Research shows ACE achieves **+10.6% improvement on agent tasks** through systematic context optimization.

**ACE Core Concepts:**

| Concept | Description | Application |
|---------|-------------|-------------|
| **Evolving Playbooks** | Context as living documents that improve over time | Store successful context patterns for reuse |
| **Generation Phase** | Creating initial context from task requirements | Planner agent constructs initial context |
| **Reflection Phase** | Evaluating context effectiveness after execution | Reviewer agent assesses what context helped/hurt |
| **Curation Phase** | Refining context based on outcomes | Update templates based on success/failure patterns |

**Applying ACE to jib:**

```python
class ACEContextManager:
    """Manages context lifecycle following ACE principles."""

    def generate_context(self, task: dict) -> dict:
        """Generation phase: Create initial context from task."""
        return {
            "requirements": self.extract_requirements(task),
            "relevant_files": self.identify_relevant_files(task),
            "historical_patterns": self.load_similar_task_patterns(task),
        }

    def reflect_on_context(self, task: dict, outcome: dict) -> dict:
        """Reflection phase: What context was useful/harmful?"""
        return {
            "useful_context": outcome.get("context_used", []),
            "unused_context": self.identify_unused(task, outcome),
            "missing_context": outcome.get("context_needed", []),
        }

    def curate_context(self, reflection: dict):
        """Curation phase: Update templates for future tasks."""
        if reflection["missing_context"]:
            self.update_context_templates(reflection["missing_context"])
        if reflection["unused_context"]:
            self.prune_unnecessary_context(reflection["unused_context"])
```

### The Context Hierarchy

Context should be layered based on scope and relevance:

```
┌─────────────────────────────────────────────────────────────────┐
│                     Context Hierarchy                            │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ Layer 1: PERMANENT CONTEXT (Always Included)                 │ │
│  │                                                               │ │
│  │ - Agent identity and role                                    │ │
│  │ - Organization standards (Khan Academy values)               │ │
│  │ - Security boundaries (what NOT to do)                       │ │
│  │ - Output format requirements                                 │ │
│  │                                                               │ │
│  │ Size: ~500-1000 tokens*                                      │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                            │                                      │
│                            ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ Layer 2: WORKFLOW CONTEXT (Per Pipeline)                     │ │
│  │                                                               │ │
│  │ - Pipeline goal and current stage                            │ │
│  │ - Previous stage outputs (summaries, not raw)                │ │
│  │ - Relevant ADRs and standards                                │ │
│  │ - Quality criteria for this workflow                         │ │
│  │                                                               │ │
│  │ Size: ~1000-3000 tokens*                                     │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                            │                                      │
│                            ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ Layer 3: TASK CONTEXT (Per Stage)                            │ │
│  │                                                               │ │
│  │ - Specific files to read/modify                              │ │
│  │ - Test requirements                                          │ │
│  │ - Related code examples                                      │ │
│  │ - Error messages or failure context                          │ │
│  │                                                               │ │
│  │ Size: Variable (loaded on-demand)                            │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                            │                                      │
│                            ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ Layer 4: DYNAMIC CONTEXT (On-Demand)                         │ │
│  │                                                               │ │
│  │ - Tool outputs (file reads, command results)                 │ │
│  │ - External data (API responses, web searches)                │ │
│  │ - User clarifications                                        │ │
│  │ - Error recovery information                                 │ │
│  │                                                               │ │
│  │ Size: As needed                                              │ │
│  └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

> **Note:** Token estimates marked with * are starting guidelines. Calibrate these based on actual usage measurements—different models and tasks may require adjustment.

### Context Selection Guidelines

**What to Include:**

| Context Type | When to Include | How to Include |
|--------------|-----------------|----------------|
| **Requirements** | Always | Summarized, not raw JIRA |
| **Relevant Files** | When modifying | Full content or key sections |
| **Test Patterns** | When writing tests | Examples from same codebase |
| **ADRs** | For architectural work | Relevant decisions only |
| **Error Messages** | When debugging | Full stack trace + context |
| **Previous Decisions** | Multi-stage workflows | Summary from prior stages |

**What to Exclude:**

| Context Type | Why Exclude | Alternative |
|--------------|-------------|-------------|
| **Entire codebase** | Overwhelms reasoning | Targeted file loading |
| **Historical discussions** | Noise | Summarized decisions |
| **Unrelated ADRs** | Distraction | Index-based lookup |
| **Raw API responses** | Token waste | Parsed, relevant fields |
| **Sensitive data** | Security risk | Redacted or excluded |

### Context Compression Techniques

**1. Summarization Before Inclusion:**

```python
# Bad: Include entire JIRA ticket
context["jira"] = full_ticket_json  # 5000 tokens

# Good: Extract relevant fields
context["requirements"] = {
    "summary": ticket.summary,  # 50 tokens
    "acceptance_criteria": extract_criteria(ticket),  # 200 tokens
    "priority": ticket.priority,  # 10 tokens
}
```

**2. Progressive Disclosure:**

```python
# Stage 1: Planning - high-level only
context["codebase"] = get_file_tree()  # Structure, not content

# Stage 2: Implementation - targeted files
context["files"] = read_files(plan.files_to_modify)  # Only needed files

# Stage 3: Testing - test patterns
context["test_examples"] = get_similar_tests(plan.component)  # Relevant tests
```

**3. Reference Instead of Inline:**

```markdown
# Bad: Inline entire document
Here is our security policy: [5000 words of security docs]

# Good: Reference with summary
Security Policy: See ~/context-sync/confluence/security-policy.md
Key points for this task:
- Authentication: OAuth2 required for all endpoints
- Data: No PII in logs
- Validation: Input validation required
```

### Termination Criteria

Every agent must have explicit stopping conditions:

```yaml
agent:
  name: code_researcher
  termination_criteria:
    success:
      - "Found relevant code examples"
      - "Identified all files to modify"
      - "Confirmed no blocking dependencies"
    failure:
      - "No relevant code found after 3 search attempts"
      - "Circular dependency detected"
      - "Required file not found in codebase"
    timeout:
      max_duration: "5 minutes"
      max_tool_calls: 20
```

## Prompt Architecture

### Cache Optimization for Prompts

Based on lessons from the [Manus engineering blog](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus), cache-aware prompt design can significantly reduce latency and costs:

**Key Principles:**

| Principle | Description | Implementation |
|-----------|-------------|----------------|
| **Keep prefix stable** | System prompt and identity should rarely change | Put static content at the beginning of prompts |
| **Make context append-only** | Add new information at the end, don't modify middle | Structure prompts so new context appends |
| **Ensure deterministic serialization** | Same context should produce identical prompt text | Use sorted keys, consistent formatting |

**Cache-Optimized Prompt Structure:**

```
┌─────────────────────────────────────────────────────────────────┐
│ CACHE-FRIENDLY ZONE (Stable - rarely changes)                    │
│                                                                   │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ System Prompt (agent identity, behaviors, constraints)       │ │
│ └─────────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ Organization Standards (coding style, security rules)        │ │
│ └─────────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ Output Format Requirements (JSON schema, structure)          │ │
│ └─────────────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────────────┤
│ VARIABLE ZONE (Append-only - changes per invocation)            │
│                                                                   │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ Task Context (current task description)                      │ │
│ └─────────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ Dynamic Context (file contents, prior stage outputs)         │ │
│ └─────────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ Current Instruction (specific action for this invocation)    │ │
│ └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

**Implementation Tips:**

```python
class CacheAwarePromptComposer:
    """Compose prompts with cache optimization in mind."""

    def compose(self, agent_type: str, task_context: dict) -> str:
        # Static prefix (cacheable) - always first
        prompt_parts = [
            self.get_system_prompt(agent_type),  # Stable
            self.get_org_standards(),            # Stable
            self.get_output_format(agent_type),  # Stable
        ]

        # Dynamic suffix (append-only) - always last
        prompt_parts.extend([
            self.format_task_context(task_context),
            self.format_dynamic_context(task_context.get("files", [])),
            self.format_instruction(task_context.get("instruction")),
        ])

        return "\n\n".join(prompt_parts)

    def format_dynamic_context(self, files: list) -> str:
        # Deterministic ordering for cache consistency
        sorted_files = sorted(files, key=lambda f: f["path"])
        return "\n".join(
            f"## {f['path']}\n```\n{f['content']}\n```"
            for f in sorted_files
        )
```

### Layered Prompt Structure

```
┌─────────────────────────────────────────────────────────────────┐
│                      Prompt Composition                          │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ SYSTEM PROMPT (Identity Layer)                               │ │
│  │                                                               │ │
│  │ You are {agent_name}, a specialized agent for {purpose}.     │ │
│  │                                                               │ │
│  │ Core Behaviors:                                               │ │
│  │ - {behavior_1}                                               │ │
│  │ - {behavior_2}                                               │ │
│  │                                                               │ │
│  │ You MUST NOT:                                                │ │
│  │ - {constraint_1}                                             │ │
│  │ - {constraint_2}                                             │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                            │                                      │
│                            ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ CONTEXT BLOCK (Information Layer)                            │ │
│  │                                                               │ │
│  │ ## Task Context                                               │ │
│  │ {task_description}                                           │ │
│  │                                                               │ │
│  │ ## Available Information                                      │ │
│  │ {structured_context}                                         │ │
│  │                                                               │ │
│  │ ## Previous Stage Results                                     │ │
│  │ {prior_outputs}                                              │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                            │                                      │
│                            ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ INSTRUCTION BLOCK (Action Layer)                             │ │
│  │                                                               │ │
│  │ ## Your Task                                                  │ │
│  │ {specific_instruction}                                       │ │
│  │                                                               │ │
│  │ ## Expected Output                                            │ │
│  │ {output_format}                                              │ │
│  │                                                               │ │
│  │ ## Quality Criteria                                           │ │
│  │ {success_criteria}                                           │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                            │                                      │
│                            ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ EXAMPLE BLOCK (Optional - Demonstration Layer)               │ │
│  │                                                               │ │
│  │ ## Example Input                                              │ │
│  │ {example_input}                                              │ │
│  │                                                               │ │
│  │ ## Example Output                                             │ │
│  │ {example_output}                                             │ │
│  └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### Prompt Composition Engine

```python
from typing import Optional
from jinja2 import Environment, FileSystemLoader
import yaml


class PromptComposer:
    """Composes prompts from templates and context."""

    def __init__(self, template_dir: str = ".claude/templates"):
        self.env = Environment(loader=FileSystemLoader(template_dir))

    def compose(
        self,
        agent_type: str,
        task_context: dict,
        workflow_context: Optional[dict] = None,
        examples: Optional[list] = None,
    ) -> str:
        """Compose a complete prompt from layers."""

        # Load agent template
        template = self.env.get_template(f"{agent_type}.jinja2")

        # Build context hierarchy
        context = {
            # Layer 1: Permanent context (from agent definition)
            **self.load_agent_defaults(agent_type),

            # Layer 2: Workflow context
            **(workflow_context or {}),

            # Layer 3: Task context
            **task_context,

            # Layer 4: Examples (optional)
            "examples": examples or [],
        }

        return template.render(**context)

    def load_agent_defaults(self, agent_type: str) -> dict:
        """Load permanent context for agent type."""
        config_path = f".claude/agents/{agent_type}.yaml"
        with open(config_path) as f:
            return yaml.safe_load(f)
```

### Extended Planner Agent Template

**Planner Agent (`templates/planner.jinja2`):**

```jinja2
{# System Prompt #}
You are the Planning Agent for egg.

Your role is to analyze requirements and create actionable implementation plans.

## Core Behaviors
- Break complex tasks into discrete, testable steps
- Identify all files that need modification
- Estimate complexity (simple/moderate/complex)
- Flag risks and dependencies
- Consider edge cases and error scenarios

## You MUST NOT
- Write any code (planning only)
- Make architectural decisions without ADR references
- Skip the complexity assessment
- Ignore existing patterns in the codebase

{# Context Block #}
## Task Description
{{ task_description }}

## Codebase Structure
{{ codebase_summary }}

{% if related_adrs %}
## Relevant ADRs
{% for adr in related_adrs %}
- {{ adr.title }}: {{ adr.summary }}
{% endfor %}
{% endif %}

{% if beads_context %}
## Previous Work Context
{{ beads_context }}
{% endif %}

{# Instruction Block #}
## Your Task

Analyze the task description and create a detailed implementation plan.

## Expected Output

Return a JSON object with this structure:

```json
{
  "summary": "One-sentence summary of the plan",
  "complexity": "simple|moderate|complex",
  "phases": [
    {
      "name": "Phase name",
      "description": "What this phase accomplishes",
      "files_to_modify": ["path/to/file.py"],
      "dependencies": ["ADR-042", "existing-auth-module"],
      "risks": ["Risk description"],
      "success_criteria": ["Testable criteria"]
    }
  ],
  "total_files": 5,
  "estimated_tests": 12,
  "blockers": ["Any blocking issues identified"]
}
```

## Quality Criteria
- [ ] All acceptance criteria from task are addressed
- [ ] File paths are accurate (verify with codebase structure)
- [ ] Complexity rating justified
- [ ] Risks are specific and actionable
- [ ] Success criteria are testable
```

## Agent Design Patterns

### Agent Types and Responsibilities

| Agent Type | Purpose | Inputs | Outputs | Constraints |
|------------|---------|--------|---------|-------------|
| **Planner** | Analyze requirements, create plans | Task description, codebase summary | Implementation plan (JSON) | No code writing |
| **Implementer** | Write code per plan | Plan, target files | Code changes, commits | Follow plan exactly |
| **Tester** | Write and run tests | Changed files, test patterns | Test code, results | No production code |
| **Reviewer** | Evaluate code quality | Code changes, standards | Review comments, score | No modifications |
| **Fixer** | Resolve failures | Error logs, failing code | Fix commits | Minimal changes |
| **Documenter** | Write documentation | Code, API specs | Docs, READMEs | No code changes |
| **Researcher** | Find information | Query, context | Summary, sources | Read-only tools |

### Agent Definition Schema

```yaml
# .claude/agents/implementer.yaml
name: implementer
version: "1.0"
description: "Writes code based on implementation plans"

identity:
  role: "Code Implementation Agent"
  persona: "A senior engineer who writes clean, tested code"

capabilities:
  tools:
    - read_file
    - write_file
    - run_command
    - git_commit
  permissions:
    - modify_code: true
    - run_tests: true
    - create_commits: true
    - push_code: false  # Human must push
    - modify_config: false

constraints:
  must_not:
    - "Deviate from the provided plan"
    - "Skip writing tests"
    - "Introduce new dependencies without plan approval"
    - "Make architectural changes beyond plan scope"
  must:
    - "Follow existing code patterns"
    - "Include error handling"
    - "Write descriptive commit messages"
    - "Update related documentation"

context_requirements:
  required:
    - implementation_plan
    - target_files
    - test_patterns
  optional:
    - related_tests
    - style_guide
    - error_handling_patterns

output_format:
  type: structured
  schema:
    changed_files: "list[str]"
    commit_shas: "list[str]"
    tests_written: "list[str]"
    documentation_updated: "bool"

quality_criteria:
  - "All plan phases completed"
  - "Tests pass locally"
  - "No linter errors"
  - "Commits are atomic and well-described"

termination:
  success:
    - "All planned changes implemented"
    - "Tests pass"
  failure:
    - "Cannot resolve merge conflicts"
    - "Tests fail after 3 fix attempts"
    - "Blocked by missing dependency"
  timeout:
    max_duration: "30 minutes"
    max_iterations: 5
```

### Agent Template Inheritance

```
┌─────────────────────────────────────────────────────────────────┐
│                    Agent Template Hierarchy                      │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ BASE AGENT (base.yaml)                                       │ │
│  │                                                               │ │
│  │ - Organization standards (Khan Academy)                      │ │
│  │ - Security constraints (no credentials)                      │ │
│  │ - Communication style (concise, technical)                   │ │
│  │ - Error handling patterns                                    │ │
│  └──────────────────────────┬──────────────────────────────────┘ │
│                             │                                     │
│            ┌────────────────┼────────────────┐                   │
│            │                │                │                    │
│            ▼                ▼                ▼                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │ READING     │  │ WRITING     │  │ REVIEWING   │              │
│  │ AGENTS      │  │ AGENTS      │  │ AGENTS      │              │
│  │             │  │             │  │             │              │
│  │ - Planner   │  │ - Implmtr   │  │ - Reviewer  │              │
│  │ - Researcher│  │ - Tester    │  │ - Auditor   │              │
│  │ - Analyzer  │  │ - Fixer     │  │ - Validator │              │
│  │             │  │ - Documtr   │  │             │              │
│  │ Read-only   │  │ Read-write  │  │ Read-only   │              │
│  │ tools       │  │ tools       │  │ + scoring   │              │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
└─────────────────────────────────────────────────────────────────┘
```

### Specialized Agent Examples

**Researcher Agent (Reading):**

```yaml
name: researcher
extends: base
category: reading

identity:
  role: "Research Agent"
  persona: "A thorough investigator who finds relevant information"

capabilities:
  tools:
    - read_file
    - glob_search
    - grep_search
    - web_search
  permissions:
    - modify_code: false
    - run_tests: false

termination:
  success:
    - "Found sufficient relevant information"
    - "Identified all related files"
  failure:
    - "No relevant information after exhaustive search"
    - "Information contradictory, needs human input"
```

**Reviewer Agent (Reviewing):**

```yaml
name: reviewer
extends: base
category: reviewing

identity:
  role: "Code Review Agent"
  persona: "A constructive critic focused on code quality"

capabilities:
  tools:
    - read_file
    - git_diff
  permissions:
    - modify_code: false
    - post_comments: true

output_format:
  type: review
  schema:
    overall_score: "1-5"
    summary: "string"
    issues:
      - severity: "critical|major|minor|suggestion"
        file: "string"
        line: "int"
        message: "string"
        suggestion: "string"
    approved: "bool"

quality_criteria:
  - "All critical issues must be flagged"
  - "Suggestions are actionable"
  - "Feedback is constructive, not dismissive"
```

## Pipeline Patterns

### Pattern 1: Sequential Specialization

**Use Case:** Feature implementation requiring multiple specialized steps

```python
Pipeline(
    stages=[
        Stage("plan", planner_agent),
        Stage("implement", coder_agent),
        Stage("test", tester_agent),
        Stage("review", reviewer_agent),
        Stage("create_pr", pr_agent),
    ]
)
```

**Benefits:**
- Each agent specialized for its task
- Clear handoff points
- Easy to debug which stage failed
- Can cache stage outputs

**When to Use:**
- Complex tasks with distinct phases
- High-value tasks where quality matters
- Tasks requiring checkpointing (long-running)

### Pattern 2: Parallel Analysis

**Use Case:** PR review with multiple independent checks

```python
Pipeline(
    stages=[
        ParallelStages(
            Stage("code_review", code_reviewer_agent),
            Stage("security_scan", security_agent),
            Stage("performance_analysis", perf_agent),
            Stage("docs_check", docs_agent),
        ),
        Stage("synthesize_review", synthesis_agent),
    ]
)
```

**Benefits:**
- Faster execution (parallel)
- Independent perspectives
- Bounded resource usage

**When to Use:**
- Multiple independent analyses needed
- Time-sensitive workflows (PR reviews)
- Resource-efficient parallelism

### Pattern 3: Conditional Branching

**Use Case:** Different paths based on task complexity

```python
Pipeline(
    stages=[
        Stage("assess_complexity", complexity_agent),
        ConditionalStage(
            condition="stages.assess_complexity.outputs.complexity == 'simple'",
            if_true=Stage("quick_implement", simple_coder_agent),
            if_false=Pipeline(stages=[
                Stage("detailed_plan", planner_agent),
                Stage("implement_with_tests", full_coder_agent),
                Stage("comprehensive_review", reviewer_agent),
            ]),
        ),
    ]
)
```

**Benefits:**
- Adaptive resource allocation
- Simpler tasks complete faster
- Complex tasks get appropriate rigor

**When to Use:**
- Variable task complexity
- Cost optimization important
- Different quality bars for different tasks

### Pattern 4: Iterative Refinement

**Use Case:** Code generation with feedback loop

```python
Pipeline(
    stages=[
        Stage("generate_code", coder_agent),
        Stage("test_code", tester_agent),
        LoopUntil(
            condition="stages.test_code.outputs.all_tests_passed",
            max_iterations=3,
            stages=[
                Stage("fix_failures", fixer_agent, inputs={
                    "code": "stages.generate_code.outputs.code",
                    "failures": "stages.test_code.outputs.failures",
                }),
                Stage("test_code", tester_agent),
            ],
        ),
        Stage("finalize", finalizer_agent),
    ]
)
```

**Benefits:**
- Quality improvement through iteration
- Handles failures gracefully
- Bounded retry attempts

**When to Use:**
- Flaky processes (tests, builds)
- Quality-critical outputs
- Self-correcting workflows

### Pattern 5: Debate/Consensus

**Use Case:** High-stakes decisions (security, architecture)

```python
Pipeline(
    stages=[
        ParallelStages(
            Stage("propose_solution_1", architect_agent_1),
            Stage("propose_solution_2", architect_agent_2),
            Stage("propose_solution_3", architect_agent_3),
        ),
        Stage("critique_all", critic_agent, inputs={
            "solutions": [
                "stages.propose_solution_1.outputs.proposal",
                "stages.propose_solution_2.outputs.proposal",
                "stages.propose_solution_3.outputs.proposal",
            ],
        }),
        Stage("select_best", selector_agent, inputs={
            "solutions": "stages.critique_all.inputs.solutions",
            "critiques": "stages.critique_all.outputs.critiques",
        }),
    ]
)
```

**Benefits:**
- Multiple perspectives
- Higher quality decisions
- Reduced bias

**When to Use:**
- High-impact decisions
- Ambiguous requirements
- Trade-offs between approaches

### Token Optimization Strategies

Given that multi-agent systems use **~15× more tokens** than single-agent chats ([source](https://medium.com/@anishnarayan09/agentic-ai-automation-optimize-efficiency-minimize-token-costs-69185687713c)), optimization is critical for economic viability.

**Key Optimization Techniques:**

1. **Stage-Specific Context Loading**
   - Load only context needed for each stage
   - Don't pass full codebase to every agent
   - Planner needs requirements; Coder needs relevant files; Tester needs test framework info

2. **Prompt Engineering**
   - [Research shows](https://adam.holter.com/ai-costs-in-2025-cheaper-tokens-pricier-workflows-why-your-bill-is-still-rising/) optimized prompts reduce token consumption 30-50%
   - Concise prompting and context pruning cut usage 40-50%
   - Each agent template should be minimal yet complete

3. **Dynamic Model Selection**
   - Use smaller models (Haiku) for simple stages (validation, formatting)
   - Reserve Opus/Sonnet for complex reasoning stages
   - Anthropic's system used Opus 4 (lead) + Sonnet 4 (subagents) for cost balance

4. **Termination Criteria**
   - Prevent agents from running indefinitely
   - Set max iterations for loops (typically 3)
   - Include explicit "done" conditions in agent prompts

5. **Modular Agent Design**
   - Break agents into smaller, specialized units
   - Reduces coordination overhead by ~20%
   - Avoids redundant tool calls and retrieval

**Measurement:**
- Track token usage per stage
- Compare multi-agent vs single-agent baselines
- Target: multi-agent should provide >2× quality improvement to justify 15× token cost

## Review and Feedback Loops

### Automated Review Stages

Quality gates are workflow stages, not afterthoughts. Every pipeline should include automated review stages before human review.

**Plan Review Criteria:**

```yaml
# .claude/review/plan_criteria.yaml
plan_review:
  required_fields:
    - summary
    - complexity
    - phases
    - success_criteria

  complexity_validation:
    simple:
      max_files: 3
      max_phases: 2
    moderate:
      max_files: 10
      max_phases: 4
    complex:
      requires: "ADR reference or justification"

  quality_checks:
    - name: "specific_files"
      description: "All file paths must be verifiable"
      validator: "file_exists_check"

    - name: "testable_criteria"
      description: "Success criteria must be testable"
      validator: "criteria_specificity_check"

    - name: "risk_assessment"
      description: "Complex changes must identify risks"
      condition: "complexity in ['moderate', 'complex']"
      validator: "has_risks_field"
```

**Code Review Criteria:**

```yaml
# .claude/review/code_criteria.yaml
code_review:
  severity_levels:
    critical:
      must_fix: true
      examples:
        - "Security vulnerability"
        - "Data loss risk"
        - "Breaking change without migration"
    major:
      must_fix: true
      examples:
        - "Missing error handling"
        - "No tests for new functionality"
        - "Performance regression"
    minor:
      must_fix: false
      examples:
        - "Code style inconsistency"
        - "Missing documentation"
        - "Unnecessary complexity"
    suggestion:
      must_fix: false
      examples:
        - "Alternative approach possible"
        - "Refactoring opportunity"

  automatic_checks:
    - name: "tests_exist"
      rule: "new_files.any(f => f.is_code) implies new_files.any(f => f.is_test)"
    - name: "no_debug_code"
      rule: "not changes.contains('console.log') and not changes.contains('print(')"
    - name: "error_handling"
      rule: "new_functions.all(f => f.has_try_catch or f.has_error_return)"

  human_escalation:
    conditions:
      - "security_issues.count > 0"
      - "breaking_changes.count > 0"
      - "review_score < 3"
```

### Feedback Integration

**How Review Feedback Flows:**

```
┌─────────────────────────────────────────────────────────────────┐
│                    Feedback Integration Flow                     │
│                                                                   │
│  ┌───────────────┐     ┌───────────────┐     ┌───────────────┐  │
│  │   Agent       │     │   Review      │     │   Feedback    │  │
│  │   Output      │────▶│   Stage       │────▶│   Parser      │  │
│  └───────────────┘     └───────────────┘     └───────┬───────┘  │
│                                                       │          │
│                                   ┌───────────────────┴───────┐  │
│                                   │                           │  │
│                                   ▼                           ▼  │
│                        ┌───────────────┐           ┌──────────┐ │
│                        │   Actionable  │           │ Context  │ │
│                        │   Issues      │           │ Updates  │ │
│                        └───────┬───────┘           └────┬─────┘ │
│                                │                        │       │
│                                ▼                        ▼       │
│                        ┌───────────────┐     ┌───────────────┐  │
│                        │   Fixer       │     │   Next Stage  │  │
│                        │   Agent       │     │   Context     │  │
│                        │               │     │               │  │
│                        │ Addresses     │     │ Includes      │  │
│                        │ specific      │     │ review        │  │
│                        │ issues        │     │ learnings     │  │
│                        └───────────────┘     └───────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

**Feedback Format:**

```json
{
  "review_id": "rev-abc123",
  "overall_assessment": {
    "approved": false,
    "score": 3,
    "summary": "Good implementation but missing edge case handling"
  },
  "issues": [
    {
      "id": "issue-1",
      "severity": "major",
      "file": "src/auth/oauth.py",
      "line": 45,
      "message": "Missing null check for token response",
      "suggestion": "Add: if not token_response: raise AuthError(...)",
      "actionable": true,
      "auto_fixable": true
    },
    {
      "id": "issue-2",
      "severity": "minor",
      "file": "src/auth/oauth.py",
      "line": 67,
      "message": "Magic number should be constant",
      "suggestion": "Extract 3600 to TOKEN_EXPIRY_SECONDS constant",
      "actionable": true,
      "auto_fixable": true
    }
  ],
  "praise": [
    {
      "file": "src/auth/oauth.py",
      "line": 12,
      "message": "Good use of dependency injection for the HTTP client"
    }
  ],
  "context_for_next_stage": {
    "key_concerns": ["null safety", "token validation"],
    "patterns_to_follow": ["existing error handling in auth module"],
    "tests_needed": ["test_oauth_null_token", "test_oauth_expired_token"]
  }
}
```

### Review Cadence Patterns

Based on discussion with Tyler Burleigh, review stages can be configured at different cadences:

| Cadence | Review After | Best For | Trade-off |
|---------|--------------|----------|-----------|
| **Task** | Each individual task | Maximum quality, catches issues early | Higher latency, more token usage |
| **Phase** | Each pipeline phase | Balance of quality and speed | Recommended default |
| **Plan** | Entire plan implementation | Faster end-to-end | May miss issues until late |

**Recommendation:** Phase-level review is the sweet spot for most workflows. Entire-plan-level review tends to catch too much at once and is harder to act on.

> **Validation Note:** This recommendation should be validated empirically once the system is in production. Different workflow types (bug fixes vs. feature development vs. refactoring) may have different optimal review cadences.

### Proactive vs Reactive Review Modes

Reviews can operate in two distinct modes, each suited to different situations:

| Mode | Description | When to Use | Cache Impact |
|------|-------------|-------------|--------------|
| **Proactive** | AI-led summaries generated automatically after each stage | Standard workflows, status updates | May invalidate cache if summaries inserted mid-context |
| **Reactive** | On-demand Q&A when human requests review | Human-in-the-loop workflows, ad-hoc checks | Cache-friendly (appends to context) |

**Proactive Review Pattern:**

```python
class ProactiveReviewer:
    """Generates review summaries automatically after each stage."""

    async def review_stage_output(self, stage: str, output: dict) -> dict:
        """Proactively review and summarize stage output."""
        return await self.review_agent.analyze({
            "stage": stage,
            "output": output,
            "instruction": """
                Provide a brief summary:
                1. What was accomplished
                2. Any concerns or issues
                3. Readiness for next stage (ready/blocked/needs-review)
            """,
        })
```

**Reactive Review Pattern:**

```python
class ReactiveReviewer:
    """Responds to on-demand review requests."""

    async def handle_review_request(self, query: str, context: dict) -> dict:
        """Answer specific questions about work in progress."""
        # Append query to existing context (cache-friendly)
        augmented_context = {
            **context,
            "review_query": query,
        }
        return await self.review_agent.respond(augmented_context)
```

**Hybrid Approach (Recommended):**

Combine both modes for optimal results:
- Use **proactive summaries** at phase boundaries (stored separately, not in prompt cache)
- Use **reactive Q&A** for detailed investigation
- Keep proactive summaries brief to minimize token usage
- Store summaries in Beads notes rather than inline context

```python
class HybridReviewStrategy:
    """Combines proactive and reactive review modes."""

    async def on_phase_complete(self, phase: str, output: dict):
        # Proactive: Generate and store summary
        summary = await self.proactive_reviewer.review_stage_output(phase, output)
        await self.beads.update_notes(f"Phase {phase} summary: {summary}")

    async def on_human_query(self, query: str):
        # Reactive: Answer specific questions
        context = await self.load_current_context()
        return await self.reactive_reviewer.handle_review_request(query, context)
```

**Cache-Aware Review Feedback:**

When integrating review feedback back into agent context, follow these cache-friendly patterns:

1. **Store feedback separately**: Keep review comments in Beads, reference by ID in context
2. **Summarize before inclusion**: Include summarized feedback, not full review history
3. **Append, don't insert**: Add feedback at the end of context, preserving stable prefix

## Model Selection Strategy

### Task-Complexity Mapping

| Task Complexity | Characteristics | Recommended Model | Rationale |
|-----------------|-----------------|-------------------|-----------|
| **Simple** | Single file, well-defined, < 50 lines | Haiku | Fast, cheap, sufficient |
| **Moderate** | Multiple files, some ambiguity, tests needed | Sonnet | Good balance |
| **Complex** | Architectural, multi-phase, high stakes | Opus | Best reasoning |
| **Validation** | Format checking, linting, simple review | Haiku | Speed matters |
| **Research** | Web search, doc synthesis, exploration | Sonnet | Good comprehension |
| **Decision** | Architecture, trade-offs, high impact | Opus | Deep reasoning |

### Dynamic Model Selection

```python
class ModelSelector:
    """Selects appropriate model based on task characteristics."""

    def select_model(
        self,
        task_type: str,
        complexity: str,
        stage: str,
        token_budget: Optional[int] = None,
    ) -> str:
        """Select model based on task requirements."""

        # Stage-specific overrides
        stage_models = {
            "validation": "haiku",
            "formatting": "haiku",
            "planning": "sonnet",
            "implementation": "sonnet",
            "architecture_decision": "opus",
            "security_review": "opus",
        }

        if stage in stage_models:
            return stage_models[stage]

        # Complexity-based selection
        complexity_models = {
            "simple": "haiku",
            "moderate": "sonnet",
            "complex": "opus",
        }

        base_model = complexity_models.get(complexity, "sonnet")

        # Budget constraints can force downgrade
        if token_budget and token_budget < 1000:
            return "haiku"

        return base_model
```

### Cost-Quality Trade-offs

| Model | Cost Factor | Quality Factor | Best For |
|-------|-------------|----------------|----------|
| Haiku | 1x | Good enough | Validation, formatting, simple tasks |
| Sonnet | 3x | Better | Most development work |
| Opus | 15x | Best | Critical decisions, complex reasoning |

**Optimization Guidelines:**

1. **Use Haiku for:** Validation stages, format checking, simple transformations
2. **Use Sonnet for:** Implementation, testing, standard reviews, research
3. **Use Opus for:** Architectural decisions, security reviews, complex debugging

**Multi-Model Consensus (Advanced):**

For high-stakes decisions, consider running multiple models in parallel and synthesizing results.

> **Cost Consideration:** Multi-model consensus runs several models concurrently, which can be expensive. Reserve this pattern for decisions where correctness is critical and errors are costly—security decisions, architectural choices, or safety-critical code paths. For routine development tasks, single-model execution is typically sufficient.

```python
async def consensus_decision(query, context):
    # Run multiple models in parallel
    results = await asyncio.gather(
        sonnet.analyze(query, context),
        opus.analyze(query, context),
        # Optionally include external models via MCP
    )

    # Synthesize with lead model
    return await opus.synthesize(
        query=query,
        perspectives=results,
        instruction="Identify consensus and note significant disagreements"
    )
```

### Framework Comparison for Future Implementation

While this ADR recommends a custom implementation (see [Alternative 3](#alternative-3-agentic-framework-langgraph-crewai-autogen-microsoft-agent-framework) for full analysis), the following quick reference may inform future decisions if we need to adopt or integrate an external framework:

| Framework | Best For | Avoid If | jib Compatibility |
|-----------|----------|----------|-------------------|
| **[LangGraph](https://python.langchain.com/docs/langgraph)** | Complex workflows with cycles, sophisticated state management | Need quick iteration, simple linear pipelines | Good - graph model aligns with pipeline stages |
| **[CrewAI](https://github.com/joaomdmoura/crewAI)** | Role-based teams, rapid prototyping, YAML-first config | Need fine-grained control, complex logging | Moderate - simpler but less flexible |
| **[LlamaIndex](https://docs.llamaindex.ai/en/stable/)** | RAG-heavy workflows, document-centric agents | Primarily code execution tasks | Low - focused on retrieval, not code agents |
| **[AutoGen](https://microsoft.github.io/autogen/)** | Research, multi-model conversations, LLM-to-LLM chat | Production stability, clean abstractions | Low - verbosity, no DAG support |
| **[Microsoft Agent Framework](https://azure.microsoft.com/en-us/blog/introducing-microsoft-agent-framework/)** | Enterprise deployments, durability requirements | Avoiding Microsoft ecosystem lock-in | Unknown - too new (public preview) |

**Decision Guidance:**

- **Start custom, adopt later**: Build minimal custom orchestration first, adopt framework only if custom solution becomes a bottleneck
- **Pattern borrowing**: Study framework patterns (LangGraph's graph cycles, CrewAI's role definitions) even without adopting the framework
- **Integration points**: Consider MCP as the integration layer if external frameworks need to interact with egg agents

**Framework Evaluation Criteria (when revisiting this decision):**

1. Does it improve on our custom solution's weaknesses?
2. Does it support Beads-based state management or require migration?
3. What's the Claude Code integration story?
4. Does the community actively maintain it?
5. What's the lock-in risk?

## Failure Modes & Reliability

Understanding how multi-agent systems fail is critical for building reliable pipelines. The [UC Berkeley MAST (Multi-Agent System Failure Taxonomy)](https://arxiv.org/abs/2503.13657) study provides the first comprehensive analysis of failure modes across 7 popular frameworks and 150+ tasks.

### The MAST Taxonomy: 14 Failure Modes

The research identifies **14 unique failure modes** organized into 3 categories, distributed almost evenly:

| Category | % of Failures | Key Failure Modes |
|----------|---------------|-------------------|
| **Specification & System Design** | 37% | Unclear task boundaries, poor role definitions, missing termination criteria |
| **Inter-Agent Misalignment** | 31% | Task misalignment, reasoning-action mismatches, coordination failures |
| **Task Verification & Termination** | 31% | Incomplete verification, premature termination, infinite loops |

**Key Findings:**
- Even state-of-the-art open-source MASs like ChatDev achieve only **33% correctness** on benchmark tasks
- Unlike single-agent frameworks, MAS must address **inter-agent misalignment**, conversation resets, and incomplete task verification
- The research team developed an **LLM-as-a-Judge pipeline** that detects failure modes with **94% accuracy** vs human experts

### Reliability Best Practices

Based on [industry reliability research](https://www.getmaxim.ai/articles/ensuring-ai-agent-reliability-in-production-environments-strategies-and-solutions/), production-grade agents require:

**1. Error Handling for Probabilistic Systems**

```python
# Graceful degradation pattern
class AgentExecutor:
    def execute_with_fallback(self, stage, inputs):
        try:
            return self.primary_agent.execute(stage, inputs)
        except AgentTimeout:
            return self.fallback_agent.execute(stage, inputs)
        except AgentError as e:
            if self.can_retry(e):
                return self.retry_with_backoff(stage, inputs)
            return self.graceful_degradation(stage, inputs)
```

**2. Circuit Breaker Pattern**

Detect persistent failures and route traffic away from failing components:

```python
class CircuitBreaker:
    def __init__(self, failure_threshold=3, reset_timeout=60):
        self.failures = 0
        self.state = "closed"  # closed, open, half-open

    def call(self, func, *args):
        if self.state == "open":
            raise CircuitOpenError("Agent unavailable")
        try:
            result = func(*args)
            self.reset()
            return result
        except Exception as e:
            self.record_failure()
            if self.failures >= self.failure_threshold:
                self.trip()
            raise
```

**3. Observability Requirements**

[Effective observability](https://dev.to/nexaitech/ai-agent-orchestration-in-2025-how-to-build-scalable-secure-and-observable-multi-agent-systems-2flc) must monitor four interdependent components:

| Component | Metrics |
|-----------|---------|
| **Data Quality** | Input validation rates, context completeness |
| **System Performance** | Token usage, latency distributions, error rates |
| **Code Behavior** | Tool invocation success, stage completion rates |
| **Model Responses** | Output quality scores, task completion success |

**4. Distributed Tracing**

Enable precise diagnosis of bottlenecks and failure points:

```python
@trace_stage("implement_changes")
async def implement_changes(self, plan, files):
    with self.tracer.span("load_context"):
        context = await self.load_relevant_files(files)

    with self.tracer.span("invoke_agent"):
        result = await self.implementer.execute(plan, context)

    with self.tracer.span("validate_output"):
        self.validate_changes(result)

    return result
```

### Reliability Metrics to Track

A simple AI agent workflow involving document retrieval, LLM inference, external API calls, and response formatting can achieve only **98% combined reliability** when individual components maintain 99-99.9% uptime. Track:

- **Task completion rate** by workflow type
- **Stage failure rates** to identify weak points
- **Mean time to recovery** from failures
- **False positive/negative rates** for validation stages

## Security & Isolation

Multi-agent systems introduce security challenges beyond existing cyber-security frameworks. When agents interact directly or through shared environments, [novel threats emerge](https://arxiv.org/html/2505.02077v1) that cannot be addressed by securing individual agents in isolation.

### Security Threat Model

| Threat Category | Examples | Mitigation |
|-----------------|----------|------------|
| **Inter-Agent Attacks** | Steganographic collusion, coordinated manipulation | Agent isolation, communication auditing |
| **Prompt Injection** | Malicious inputs exploiting agent prompts | Input sanitization, prompt firewalling |
| **Privilege Escalation** | Agents exceeding intended permissions | Principle of least privilege, RBAC |
| **Data Exfiltration** | Agents leaking sensitive context | Network isolation, output filtering |

### Production Security Best Practices

**1. Sandboxing and Isolation**

[Maintain strict sandboxing](https://dev.to/nexaitech/ai-agent-orchestration-in-2025-how-to-build-scalable-secure-and-observable-multi-agent-systems-2flc) between environments:

```python
class SandboxedAgentExecutor:
    def __init__(self, config):
        self.container_config = {
            "network_mode": "none",  # No network by default
            "read_only": True,
            "mem_limit": "2g",
            "cpu_quota": 50000,  # 50% CPU
            "security_opt": ["no-new-privileges:true"],
        }

    def execute(self, agent, inputs):
        # Run in isolated container
        with self.create_sandbox() as sandbox:
            result = sandbox.run(agent, inputs)
            # Validate output before returning
            self.validate_output(result)
            return result
```

**2. Principle of Least Privilege**

Apply strict, granular access controls to every agent:

```python
class AgentPermissions:
    READ_FILES = "read_files"
    WRITE_FILES = "write_files"
    EXECUTE_COMMANDS = "execute_commands"
    NETWORK_ACCESS = "network_access"

# Example: Planner agent only needs read access
planner_permissions = {AgentPermissions.READ_FILES}

# Example: Implementer needs read/write but no network
implementer_permissions = {
    AgentPermissions.READ_FILES,
    AgentPermissions.WRITE_FILES,
}
```

**3. Dynamic LLM Firewalls**

[Recent research introduces dynamic firewalls](https://arxiv.org/html/2505.02077v1) to secure agent interactions:

- **LlamaFirewall** (Meta): Runtime filtering of agent outputs
- **Prompt injection defense**: Sanitize inputs before agent processing
- **Output validation**: Verify agent actions before execution

**4. Audit Logging**

Every agent action must be traceable:

```python
class AuditLogger:
    def log_agent_action(self, agent_id, action, inputs, outputs, permissions_used):
        self.log({
            "timestamp": datetime.utcnow().isoformat(),
            "agent_id": agent_id,
            "action": action,
            "inputs_hash": self.hash_sensitive(inputs),
            "outputs_hash": self.hash_sensitive(outputs),
            "permissions": list(permissions_used),
            "trace_id": self.current_trace_id,
        })
```

### jib-Specific Security Considerations

Given jib's sandboxed Docker environment:

| jib Security Feature | How It Helps Multi-Agent |
|---------------------|--------------------------|
| Network isolation | Prevents agents from making unauthorized external calls |
| No production credentials | Limits blast radius of compromised agents |
| Git-backed state (Beads) | Audit trail for all agent actions |
| Container isolation | Each agent invocation is isolated |
| GitHub App token scope | Limited to specific repository operations |

## Interoperability Standards

Two emerging protocols are shaping how agents communicate and integrate with tools:

### Model Context Protocol (MCP)

[MCP](https://modelcontextprotocol.io/) is an open standard introduced by Anthropic in November 2024 for agent-to-tool communication.

**Adoption Status (December 2025):**
- OpenAI adopted MCP in March 2025 across ChatGPT, Agents SDK, and Responses API
- Google DeepMind confirmed MCP support in Gemini models
- ~90% of organizations expected to use MCP by end of 2025
- Block has developed 60+ MCP servers internally

**Production Challenges:**
- Security: Prompt injection, over-permissive defaults, lack of tenant isolation
- Scalability: stdio transport creates issues at enterprise scale
- Authentication: Lacking OAuth compliance, SSO integration, granular permissions
- Latency: Remote servers introduce delays for time-sensitive applications

**MCP Integration for egg:**

```python
# Example MCP tool registration for pipeline stages
mcp_tools = {
    "git_operations": MCPServer("https://github.com/mcp/git"),
    "code_analysis": MCPServer("https://internal/mcp/analysis"),
    "test_runner": MCPServer("https://internal/mcp/testing"),
}

class AgentWithMCP(Agent):
    def __init__(self, name, tools: list[str]):
        self.tools = [mcp_tools[t] for t in tools]

    def execute(self, inputs):
        # Tools available via MCP during execution
        return self.run_with_tools(inputs, self.tools)
```

### Agent2Agent (A2A) Protocol

[A2A](https://a2a-protocol.org/) is Google's open protocol (April 2025) for agent-to-agent communication, now under Linux Foundation governance.

**Key Features:**
- **Capability Discovery**: Agents advertise capabilities via JSON "Agent Cards"
- **Task Management**: Client/remote agent model for task delegation
- **Multimodal Support**: Supports text, audio, and video streaming
- **Built on Web Standards**: JSON-RPC 2.0 over HTTPS

**How A2A and MCP Complement Each Other:**

| Protocol | Purpose | Use Case |
|----------|---------|----------|
| **MCP** | Agent-to-tool | Connecting agents to APIs, databases, file systems |
| **A2A** | Agent-to-agent | Cross-system agent collaboration, delegation |

**A2A for egg Multi-Agent Pipelines:**

```python
# Hypothetical A2A integration for external agent delegation
class ExternalAgentDelegate:
    def __init__(self, agent_card_url):
        self.agent = A2AClient(agent_card_url)
        self.capabilities = self.agent.discover_capabilities()

    async def delegate_task(self, task_type, inputs):
        if task_type not in self.capabilities:
            raise UnsupportedTaskError(f"Agent doesn't support {task_type}")

        # A2A task submission
        task_id = await self.agent.submit_task(task_type, inputs)
        return await self.agent.await_result(task_id)
```

**Industry Adoption:**
- 100+ technology companies supporting A2A
- Partners include Atlassian, Box, Salesforce, SAP, ServiceNow, PayPal
- Service providers: Accenture, Deloitte, Cognizant, Capgemini

### Implications for egg

**Short-term:**
- Monitor MCP ecosystem for useful tool integrations
- Design agent templates with standard input/output contracts (future A2A compatibility)

**Medium-term:**
- Consider MCP for tool integrations (code analysis, testing)
- Evaluate A2A for delegating specialized tasks to external agents

**Long-term:**
- egg agents could expose A2A Agent Cards for external consumption
- Enable hybrid workflows with internal + external agents

## Enterprise Case Studies

Real-world deployments provide valuable insights into what works at scale.

### Salesforce Agentforce (2025)

[Salesforce's Agentforce](https://salesforcedevops.net/index.php/2025/06/23/salesforce-agentforce-3/) represents one of the largest enterprise multi-agent deployments:

**Results:**
- **5,000+ businesses** using Agentforce (Indeed, OpenTable, Heathrow Airport)
- **Wiley**: 40%+ increase in case resolution vs old bot
- **1-800Accountant**: 70% autonomous resolution during tax season
- **Engine**: 15% reduction in average case handle time
- **Grupo Globo**: 22% increase in subscriber retention

**Success Factors:**
1. **Data Readiness**: AI quality depends on data quality - extensive data cleansing required
2. **Team Upskilling**: New roles like "AI Operations Manager" emerging
3. **Governance First**: Built-in safeguards for bias detection, role-based access, explainability
4. **Observability**: Performance dashboards tracking adoption, feedback, success rates, costs

### Anthropic Research System

[Anthropic's multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) (June 2025) demonstrates best-in-class architecture:

**Architecture:**
- Lead agent (Claude Opus 4) coordinates research strategy
- Subagents (Claude Sonnet 4) execute parallel searches
- 90.2% performance improvement over single-agent Opus 4

**Key Engineering Lessons:**
1. **Claude as Prompt Engineer**: Given a prompt and failure mode, Claude can diagnose and suggest improvements
2. **Tool-Testing Agent**: Attempts flawed MCP tools, then rewrites descriptions to avoid failures - **40% decrease in task completion time**
3. **Search Strategy**: "Start broad, then narrow" - agents default to overly specific queries

### Block (Square) MCP Deployment

Block has developed **60+ MCP servers** reflecting patterns observed across their ecosystem:

**Lessons:**
- Standardization pays off: Consistent MCP interface across tools
- Security is paramount: Enterprise-grade access control
- Observability is essential: OpenTelemetry integration across all agents

### Common Enterprise Deployment Challenges

| Challenge | Small Orgs (<500) | Large Orgs (>10,000) |
|-----------|-------------------|----------------------|
| Primary blocker | **Cost** | **Compliance & Security** |
| Adoption rate | Higher experimentation | More governance required |
| Key success factor | ROI demonstration | Audit trails, RBAC |

**Industry Outlook:**
- **93%** of IT leaders plan to deploy autonomous agents within 2 years
- **282%** increase in AI adoption among CIOs (Salesforce 2025 study)
- **50%** of enterprises expected to adopt agent-based modeling by 2027 (Gartner)

### Latency Optimization Techniques

Enterprise deployments have validated several latency optimization techniques:

**1. Parallel Execution**

[Switching from serial to parallel execution](https://georgian.io/reduce-llm-costs-and-latency-guide/) for independent queries can reduce latency by **20-50%**:

```python
# Serial (slow)
result1 = await agent.search(query1)
result2 = await agent.search(query2)
result3 = await agent.search(query3)

# Parallel (fast)
results = await asyncio.gather(
    agent.search(query1),
    agent.search(query2),
    agent.search(query3),
)
```

**2. Parallel SLM/LLM Architecture**

Run Small Language Model (SLM) for quick initial responses while Large Language Model (LLM) generates higher-quality results:

```python
async def hybrid_response(query):
    # Start both in parallel
    quick_task = asyncio.create_task(slm.generate(query))
    full_task = asyncio.create_task(llm.generate(query))

    # Return quick response immediately, full response when ready
    quick_result = await quick_task
    yield quick_result  # Immediate response

    full_result = await full_task
    yield full_result  # Higher quality follow-up
```

**3. Smart Caching**

[Caching can reduce latency by up to 70%](https://superagi.com/optimizing-ai-agent-performance-advanced-techniques-and-tools-for-open-source-agentic-frameworks-in-2025/):

```python
class SemanticCache:
    def __init__(self, similarity_threshold=0.95):
        self.cache = {}
        self.embeddings = {}

    async def get_or_compute(self, query, compute_fn):
        query_embedding = await self.embed(query)

        # Check for semantically similar cached queries
        for cached_query, cached_embedding in self.embeddings.items():
            if self.similarity(query_embedding, cached_embedding) > self.threshold:
                return self.cache[cached_query]

        # Cache miss - compute and store
        result = await compute_fn(query)
        self.cache[query] = result
        self.embeddings[query] = query_embedding
        return result
```

**4. Graph-Based Parallel Decomposition**

[Research on agentic graph compilation](https://arxiv.org/html/2511.19635) shows complex problems can be partitioned into independent subgraphs for parallel solving:

- Heavy models (Opus) for ahead-of-time workflow planning
- Lightweight models (Haiku) for executing individual nodes
- Recursive concurrent task decomposition without global bottlenecks

## Implementation Phases

### Phase 1: Core Framework

**Goal:** Build minimal pipeline orchestrator and prove concept

**Success Criteria:**
- Sequential pipeline execution works
- Beads state management integrated
- Single workflow migrated successfully (e.g., feature implementation)
- Observable pipeline state via Beads

**Components:**
- Pipeline DSL (Python API)
- Orchestrator (stage execution, error handling)
- State manager (Beads integration)
- Agent executor (subprocess invocation)
- 2-3 agent templates (planner, coder, reviewer)

**Deliverables:**
- `jib/pipelines/` - Core framework
- `jib/agents/` - Agent templates
- 1 working pipeline (feature_implementation)
- Tests for orchestrator
- Documentation

### Phase 2: Parallel Execution

**Goal:** Enable parallel stage execution

**Success Criteria:**
- ParallelStages executes concurrently
- Resource limits enforced (max_workers)
- Error handling for partial failures
- 1-2 workflows use parallelism (PR review, multi-repo analysis)

**Components:**
- Process pool executor
- Parallel stage coordinator
- Output aggregation
- Timeouts and cancellation

**Deliverables:**
- Parallel execution support
- 2 parallel workflows
- Performance benchmarks

### Phase 3: Advanced Patterns

**Goal:** Support conditional, iterative, and debate patterns

**Success Criteria:**
- ConditionalStage works
- LoopUntil pattern implemented
- Debate/consensus pattern works
- 3+ workflows use advanced patterns

**Components:**
- Conditional branching
- Loop constructs
- Consensus mechanisms
- Dynamic pipeline generation

**Deliverables:**
- Full pattern library
- 5+ production workflows
- Pattern cookbook

### Phase 4: Migration & Optimization

**Goal:** Migrate all high-value workflows, optimize performance

**Success Criteria:**
- All complex workflows migrated
- Token usage optimized (vs. single-agent baseline)
- Execution time competitive or better
- Developer satisfaction high

**Components:**
- Workflow migration toolkit
- Performance profiling
- Cost analysis
- Developer UX improvements

**Deliverables:**
- Migration guide
- Performance report
- Cost comparison
- Production rollout

## Consequences

### Positive

**For Development:**
- ✅ Higher quality output (specialized agents, review steps)
- ✅ Better error recovery (checkpoint and resume)
- ✅ Faster complex workflows (parallelism)
- ✅ More observable (stage-by-stage visibility)
- ✅ Easier to optimize (profile per-stage token usage)
- ✅ Reusable agents (templates shared across workflows)

**For Operations:**
- ✅ Lower cost for some workflows (load only needed context per stage)
- ✅ Better resource utilization (parallel execution)
- ✅ Easier debugging (know which stage failed)
- ✅ State persistence (survive container restarts)

**For Users:**
- ✅ More reliable task completion
- ✅ Better quality results
- ✅ Faster turnaround on some tasks
- ✅ Clearer progress updates (stage-by-stage)

### Negative / Trade-offs

**Initial Costs:**
- ⚠️ Framework development time
- ⚠️ Workflow migration effort
- ⚠️ Learning curve for new patterns
- ⚠️ More complex debugging (distributed)

**Ongoing Considerations:**
- ⚠️ Increased code complexity (orchestration logic)
- ⚠️ Potential for over-engineering (not all tasks need multi-agent)
- ⚠️ More moving parts (more failure modes)
- ⚠️ **Economic impact**: [15× token usage](https://medium.com/@anishnarayan09/agentic-ai-automation-optimize-efficiency-minimize-token-costs-69185687713c) vs single-agent; must justify with value
- ⚠️ **Industry failure rate**: [40% of agentic AI projects canceled](https://galileo.ai/blog/hidden-cost-of-agentic-ai) before production by 2027 (Gartner prediction)

**Risks:**
- ⚠️ Poor stage boundaries → worse than single-agent
- ⚠️ Over-specialization → context loss between stages
- ⚠️ Coordination bugs hard to debug
- ⚠️ Performance regression if parallelism poorly tuned

**Mitigations:**
- Maintain single-agent as fallback
- Measure token usage per workflow (optimize or revert)
- Comprehensive logging and observability
- Clear guidelines on when to use multi-agent vs. single-agent
- Gradual rollout with A/B comparison

## Decision Permanence

**Reversible Decisions (Low Cost to Change):**
- Pipeline DSL syntax (Python vs. YAML)
- Agent template format (Jinja2 vs. other)
- Specific workflow definitions
- Execution timeouts and resource limits
- Parallelism strategy (process vs. thread)

**Semi-Permanent (Moderate Cost to Change):**
- Core orchestrator design
- Beads state schema
- Agent invocation mechanism (subprocess vs. other)
- Error handling strategy
- Pattern library structure

**Permanent (High Cost to Change):**
- Multi-agent philosophy (vs. single-agent)
- State persistence requirement (Beads integration)
- Backward compatibility promise
- Observable pipeline execution

**Review Cadence:**
- **Weekly:** Workflow performance metrics, token usage
- **Monthly:** Pattern effectiveness, developer feedback
- **Quarterly:** Architecture assessment, migration progress
- **Annually:** Strategic review of multi-agent approach

## Alternatives Considered

### Alternative 1: Stay with Single-Agent

**Approach:** Keep current architecture, improve prompts and context

**Pros:**
- No new complexity
- No migration effort
- Simpler to understand
- Lower maintenance burden

**Cons:**
- Doesn't address core limitations (context overload, no checkpointing, no specialization)
- Quality ceiling lower
- No parallelism benefits
- State management still ad-hoc

**Rejected Because:** Incremental improvements won't solve fundamental limitations. Multi-agent enables qualitatively better capabilities.

### Alternative 2: External Orchestration (Airflow, Prefect, Temporal)

**Approach:** Use existing workflow orchestration framework

**Pros:**
- Battle-tested
- Rich feature set (UI, monitoring, retries)
- Community support
- Mature tooling

**Cons:**
- Heavy dependencies (databases, web UI, scheduler)
- Over-engineered for egg's needs
- Complex setup and maintenance
- Not designed for LLM workflows (no agent-specific features)
- Learning curve for team

**Rejected Because:** Too much infrastructure for egg's scale. Custom lightweight solution better fits needs and constraints.

### Alternative 3: Agentic Framework (LangGraph, CrewAI, AutoGen, Microsoft Agent Framework)

**Approach:** Use purpose-built multi-agent LLM framework

Based on [comprehensive 2025 framework comparison](https://latenode.com/blog/langgraph-vs-autogen-vs-crewai-complete-ai-agent-framework-comparison-architecture-analysis-2025):

**Framework Characteristics:**

| Framework | Strengths | Weaknesses |
|-----------|-----------|------------|
| **LangGraph** | Graph-based architecture, sophisticated orchestration, fine-grained state management | Rigid state (defined upfront), steeper learning curve |
| **CrewAI** | Intuitive role-based design, fast iteration, YAML-driven simplicity | Logging challenges, less flexible for dynamic workflows |
| **AutoGen** | Conversational architecture, research-grade flexibility, LLM-to-LLM collaboration | Procedural code style, verbosity, no DAG support |
| **Microsoft Agent Framework** | [Enterprise-grade](https://azure.microsoft.com/en-us/blog/introducing-microsoft-agent-framework/), converges AutoGen + Semantic Kernel, built-in observability/durability | Public preview (new), enterprise focus, potential complexity |

**Pros:**
- Designed for LLM agents
- Built-in patterns (sequential, parallel, hierarchical)
- Active development and community examples
- Microsoft Agent Framework offers [durability and checkpointing](https://techcommunity.microsoft.com/blog/appsonazureblog/bulletproof-agents-with-the-durable-task-extension-for-microsoft-agent-framework/4467122)

**Cons:**
- External dependency (increase complexity)
- Abstractions may not fit jib's needs (Beads, Claude Code specifics, container isolation)
- Migration effort still required
- Less control over orchestration details
- LangGraph: demands higher upfront investment
- CrewAI: logging difficulties ("huge pain" according to practitioners)
- AutoGen: code readability drops as network complexity grows
- Microsoft Agent Framework: public preview, unproven stability

**Rejected Because:**
- Prefer to own orchestration logic for egg-specific needs (Beads integration, Claude Code specifics, container isolation)
- Custom solution provides better control over checkpointing strategy
- Can adopt or integrate framework patterns later if custom solution proves insufficient
- [Context engineering](https://www.anthropic.com/engineering/multi-agent-research-system) requirements are egg-specific
- Frameworks add dependency overhead for features we can build more simply

### Alternative 4: Agent Mesh (Fully Autonomous Collaboration)

**Approach:** Agents discover and collaborate without central orchestration

**Pros:**
- Maximum flexibility
- Emergent behavior
- Self-organizing

**Cons:**
- Unpredictable outcomes
- Hard to debug
- No clear ownership
- Coordination overhead high
- State management complex

**Rejected Because:** Too experimental for production use. Need deterministic, observable workflows.

### Alternative 5: Hybrid (Single-Agent + Multi-Agent)

**Approach:** Use single-agent for simple tasks, multi-agent for complex

**Pros:**
- Best of both worlds
- Gradual adoption
- Right-sized complexity
- Cost-effective

**Cons:**
- Two systems to maintain
- When to use which?
- Potential confusion

**Accepted:** This is actually the chosen approach - backward compatibility with single-agent, selective migration to multi-agent.

## References

### Internal Documentation
- [ADR: Autonomous Software Engineer](../in-progress/ADR-Autonomous-Software-Engineer.md) - Core jib architecture
- [ADR: LLM Documentation Index Strategy](../implemented/ADR-LLM-Documentation-Index-Strategy.md) - Multi-agent doc generation example

### Related Slack Discussion
- Multi-agent pipeline discussion (2025-12-01)
  - Tyler Burleigh's plugin patterns: [claude-sdd-toolkit](https://github.com/tylerburleigh/claude-sdd-toolkit) - Multi-provider agent consultation
  - Multi-model chorus concept: [claude-model-chorus](https://github.com/tylerburleigh/claude-model-chorus) - Ensemble approaches
  - Review cadence patterns: task, phase, or entire plan implementation review

### Industry Research & Best Practices (2025)
- [Anthropic: How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) - 90% performance improvement with multi-agent
- [Anthropic: Building agents with the Claude Agent SDK](https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk) - SDK best practices
- [Multi-Agent and Multi-LLM Architecture: Complete Guide for 2025](https://collabnix.com/multi-agent-and-multi-llm-architecture-complete-guide-for-2025/)
- [Microsoft Azure: AI Agent Orchestration Patterns](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns) - Sequential, concurrent, group chat patterns
- [Microsoft: Agent Factory - Agentic AI Design Patterns](https://azure.microsoft.com/en-us/blog/agent-factory-the-new-era-of-agentic-ai-common-use-cases-and-design-patterns/)
- [Google Cloud: Choose a design pattern for your agentic AI system](https://cloud.google.com/architecture/choose-design-pattern-agentic-ai-system)
- [LangChain: How and when to build multi-agent systems](https://blog.langchain.com/how-and-when-to-build-multi-agent-systems/)
- [ZenML: LLM Agents in Production](https://www.zenml.io/blog/llm-agents-in-production-architectures-challenges-and-best-practices) - Production deployment challenges

### Failure Modes & Reliability Research
- [UC Berkeley: Why Do Multi-Agent LLM Systems Fail? (MAST)](https://arxiv.org/abs/2503.13657) - 14 failure modes taxonomy
- [MAST Project Page](https://sky.cs.berkeley.edu/project/mast/) - Dataset and LLM annotator
- [Understanding and Mitigating Failure Modes in LLM-Based Multi-Agent Systems](https://www.marktechpost.com/2025/03/25/understanding-and-mitigating-failure-modes-in-llm-based-multi-agent-systems/)
- [Ensuring AI Agent Reliability in Production Environments](https://www.getmaxim.ai/articles/ensuring-ai-agent-reliability-in-production-environments-strategies-and-solutions/)
- [AI Agent Orchestration in 2025: Scalable, Secure, Observable](https://dev.to/nexaitech/ai-agent-orchestration-in-2025-how-to-build-scalable-secure-and-observable-multi-agent-systems-2flc)

### Security & Isolation
- [Open Challenges in Multi-Agent Security](https://arxiv.org/html/2505.02077v1) - Novel security threats in MAS
- [The attack surface you can't see: Securing autonomous AI](https://www.cio.com/article/4071216/the-attack-surface-you-cant-see-securing-your-autonomous-ai-and-agentic-systems.html)
- [Unit 42: Top 10 AI Agent Security Risks](https://chapinindustries.com/2025/05/04/ai-agents-are-here-so-are-the-threats-unit-42-unveils-the-top-10-ai-agent-security-risks/)

### Interoperability Protocols
- [A2A Protocol (Agent2Agent)](https://a2a-protocol.org/) - Official protocol documentation
- [Google: Announcing the Agent2Agent Protocol](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/)
- [Linux Foundation: A2A Protocol Project Launch](https://www.linuxfoundation.org/press/linux-foundation-launches-the-agent2agent-protocol-project-to-enable-secure-intelligent-communication-between-ai-agents)
- [Inside Google's A2A Protocol](https://towardsdatascience.com/inside-googles-agent2agent-a2a-protocol-teaching-ai-agents-to-talk-to-each-other/)
- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) - Official documentation
- [MCP: Landscape, Security Threats, and Future Directions](https://arxiv.org/abs/2503.23278)
- [Enterprise Challenges in Deploying Remote MCP Servers](https://www.descope.com/blog/post/enterprise-mcp)

### Enterprise Case Studies
- [Salesforce Agentforce 3: Building Production Infrastructure](https://salesforcedevops.net/index.php/2025/06/23/salesforce-agentforce-3/)
- [Salesforce: Top AI Agent Statistics for 2025](https://www.salesforce.com/news/stories/ai-agents-statistics/)
- [AI agents move from demos to deployment at enterprise scale](https://siliconangle.com/2025/10/23/ai-agents-move-demos-deployment-enterprise-scale-dreamforce/)
- [OpenAI: New tools for building agents](https://openai.com/index/new-tools-for-building-agents/) - Agents SDK announcement

### Economic & Cost Optimization
- [Agentic AI Automation: Optimize Efficiency, Minimize Token Costs](https://medium.com/@anishnarayan09/agentic-ai-automation-optimize-efficiency-minimize-token-costs-69185687713c)
- [AI Costs in 2025: Cheaper Tokens, Pricier Workflows](https://adam.holter.com/ai-costs-in-2025-cheaper-tokens-pricier-workflows-why-your-bill-is-still-rising/)
- [The Hidden Costs of Agentic AI](https://galileo.ai/blog/hidden-cost-of-agentic-ai) - 40% of projects fail before production
- [A Practical Guide to Reducing Latency and Costs in Agentic AI](https://georgian.io/reduce-llm-costs-and-latency-guide/)

### State Management & Checkpointing
- [Checkpoint/Restore Systems: Evolution and Applications in AI Agents](https://eunomia.dev/blog/2025/05/11/checkpointrestore-systems-evolution-techniques-and-applications-in-ai-agents/)
- [Bulletproof agents with durable task extension](https://techcommunity.microsoft.com/blog/appsonazureblog/bulletproof-agents-with-the-durable-task-extension-for-microsoft-agent-framework/4467122)
- [LangGraph State Machines: Managing Complex Agent Task Flows in Production](https://dev.to/jamesli/langgraph-state-machines-managing-complex-agent-task-flows-in-production-36f4)
- [Multi-Agent AI Failure Recovery That Actually Works](https://galileo.ai/blog/multi-agent-ai-system-failure-recovery)
- [Beyond context windows: How AI agent memory is evolving](https://bdtechtalks.com/2025/08/31/ai-agent-memory-frameworks/)
- [Context Engineering for Agents](https://rlancemartin.github.io/2025/06/23/context_engineering/)

### Benchmarks & Evaluation
- [Benchmarking Multi-Agent AI: Insights & Practical Use](https://galileo.ai/blog/benchmarks-multi-agent-ai)
- [MultiAgentBench: Evaluating Collaboration and Competition](https://arxiv.org/abs/2503.01935)
- [TheAgentCompany: Benchmarking LLM Agents on Real World Tasks](https://arxiv.org/abs/2412.14161)
- [A Comprehensive Guide to Evaluating Multi-Agent LLM Systems](https://orq.ai/blog/multi-agent-llm-eval-system)

### Framework Comparisons
- [LangGraph vs AutoGen vs CrewAI: Complete Comparison 2025](https://latenode.com/blog/langgraph-vs-autogen-vs-crewai-complete-ai-agent-framework-comparison-architecture-analysis-2025)
- [A Detailed Comparison of Top 6 AI Agent Frameworks in 2025](https://www.turing.com/resources/ai-agent-frameworks)
- [Comparing Open-Source AI Agent Frameworks](https://langfuse.com/blog/2025-03-19-ai-agent-comparison)
- [Microsoft Agent Framework](https://azure.microsoft.com/en-us/blog/introducing-microsoft-agent-framework/) - Converges AutoGen + Semantic Kernel
- [OpenAI Agents SDK (replaced Swarm)](https://github.com/openai/swarm) - Production-ready evolution

### Orchestration Patterns
- [9 Agentic AI Workflow Patterns Transforming AI Agents in 2025](https://www.marktechpost.com/2025/08/09/9-agentic-ai-workflow-patterns-transforming-ai-agents-in-2025/)
- [Top 10+ Agentic Orchestration Frameworks & Tools](https://research.aimultiple.com/agentic-orchestration/)
- [Agent Orchestration Patterns: Linear and Adaptive Approaches](https://www.getdynamiq.ai/post/agent-orchestration-patterns-in-multi-agent-systems-linear-and-adaptive-approaches-with-dynamiq)
- [Agentic Graph Compilation for Software Engineering Agents](https://arxiv.org/html/2511.19635) - Parallel decomposition research

### Legacy References
- [LangGraph Documentation](https://python.langchain.com/docs/langgraph) - Graph-based multi-agent patterns
- [CrewAI](https://github.com/joaomdmoura/crewAI) - Role-based agent orchestration
- [AutoGen](https://microsoft.github.io/autogen/) - Original Microsoft research multi-agent framework
- [Temporal Workflows](https://temporal.io/) - Distributed workflow orchestration

---

**Last Updated:** 2025-12-01
**Next Review:** 2026-01-01 (Monthly)
**Status:** Proposed (Not Implemented)
