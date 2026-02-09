# The Agentic Feedback Loop

> Work, review, address feedback—the foundational cycle for high-quality agent-human collaboration.

This document describes the feedback loop model that underlies egg's SDLC pipeline. Understanding this model explains why the pipeline produces consistently high-quality results even for complex tasks.

## The Core Cycle

Every productive collaboration—human or agent—follows the same pattern:

```
Work → Review → Address Feedback → (repeat until done)
```

This cycle is the fundamental unit of quality assurance. A single pass rarely produces the best result. Iteration with feedback is how good work becomes excellent work.

### Why This Works

The cycle works because it separates two distinct cognitive modes:

1. **Production mode**: Focus on creating, implementing, solving
2. **Evaluation mode**: Focus on finding gaps, inconsistencies, improvements

Switching between these modes within a single pass leads to either paralysis (over-critiquing during creation) or blind spots (under-critiquing during review). Separating them into distinct phases produces better outcomes.

For agents, this separation is even more important. A fresh context window for review means the reviewer has no attachment to the implementation decisions, no accumulated assumptions, and no context pollution from the production phase.

## Two Feedback Loop Types

Egg uses two types of feedback loops, each serving a different purpose.

### The Agentic Feedback Loop

```
Agent works → Agent reviews → Agent addresses feedback → (repeat)
```

This is the internal quality mechanism. An agent produces work, a separate agent (or the same agent in a fresh context) reviews it, and the original agent incorporates feedback. This cycle repeats until the work meets quality standards or a threshold is reached.

**Key properties:**
- Fully automated—no human intervention required
- Fast iteration—cycles complete in minutes
- Catches obvious issues before human review
- Prevents wasted human attention on low-quality output

The agentic loop is not a replacement for human judgment. It's a filter that ensures humans review polished work, not rough drafts.

### The Human Feedback Loop

```
Human reviews → Agent addresses feedback → (repeat)
```

This is the alignment mechanism. Humans review agent output and provide feedback. The agent incorporates that feedback and produces improved output. This cycle continues until the human approves.

**Key properties:**
- Ensures alignment with human intent
- Catches issues agents can't evaluate (business context, strategic fit)
- Provides the final quality gate
- Maintains human accountability for decisions

The human loop is where trust is built. Every significant output passes through human review before becoming final.

## The SDLC Pipeline: Loops in Practice

Egg's SDLC pipeline combines both loop types in a structured workflow:

```
┌────────────────────────────────────────────────────────────────────┐
│                         SDLC Pipeline                              │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│   ┌──────────────────────────────────────────────────────────┐     │
│   │                    REFINE PHASE                          │     │
│   │                                                          │     │
│   │    ┌─────────────────────────────────────┐               │     │
│   │    │      Agentic Feedback Loop          │               │     │
│   │    │                                     │               │     │
│   │    │   Analyze → Review → Revise → ...   │               │     │
│   │    │                                     │               │     │
│   │    └─────────────────────────────────────┘               │     │
│   │                        │                                 │     │
│   │                        ▼                                 │     │
│   │    ┌─────────────────────────────────────┐               │     │
│   │    │      Human Feedback Loop            │               │     │
│   │    │                                     │               │     │
│   │    │   Human reviews → Approve/Feedback  │               │     │
│   │    │                                     │               │     │
│   │    └─────────────────────────────────────┘               │     │
│   │                                                          │     │
│   └──────────────────────────────────────────────────────────┘     │
│                              │                                     │
│                              ▼                                     │
│   ┌──────────────────────────────────────────────────────────┐     │
│   │                     PLAN PHASE                           │     │
│   │                                                          │     │
│   │    Agentic Loop → Human Loop → Approval                  │     │
│   │                                                          │     │
│   └──────────────────────────────────────────────────────────┘     │
│                              │                                     │
│                              ▼                                     │
│   ┌──────────────────────────────────────────────────────────┐     │
│   │                   IMPLEMENT PHASE                        │     │
│   │                                                          │     │
│   │    Agentic Loop (via PR review) → Tests/CI → Ready       │     │
│   │                                                          │     │
│   └──────────────────────────────────────────────────────────┘     │
│                              │                                     │
│                              ▼                                     │
│   ┌──────────────────────────────────────────────────────────┐     │
│   │                    MERGE (Human)                         │     │
│   │                                                          │     │
│   │    Human reviews PR → Approve → Merge                    │     │
│   │                                                          │     │
│   └──────────────────────────────────────────────────────────┘     │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### Phase Breakdown

| Phase | Agentic Loop | Human Loop | Purpose |
|-------|--------------|------------|---------|
| **Refine** | Agent analyzes problem, reviewer validates | Human approves analysis | Ensure shared understanding of the problem |
| **Plan** | Agent creates plan, reviewer validates | Human approves approach | Ensure alignment on implementation strategy |
| **Implement** | Agent codes, PR review provides feedback | CI/tests validate | Produce working, tested code |
| **Merge** | — | Human reviews and merges | Final human accountability gate |

### Why Each Human Gate Matters

**After Refine**: Misunderstanding the problem leads to wasted implementation effort. Human approval ensures the agent understood the requirement correctly before any code is written.

**After Plan**: A flawed plan produces flawed code. Human review of the plan catches architectural issues, missing requirements, and scope creep before implementation begins.

**At Merge**: The human-merge invariant ensures accountability. Every change to the codebase has a human who approved it. This is enforced structurally—the agent cannot merge PRs.

## Quality Through Structure

The feedback loop model explains why egg produces high-quality results:

### 1. Multiple Review Perspectives

Each output is reviewed by:
- The agentic reviewer (fresh context, systematic evaluation)
- The human reviewer (domain knowledge, strategic context)

Issues missed by one reviewer are caught by another.

### 2. Early Feedback Is Cheap

Feedback in the refine phase costs minutes. Feedback after implementation costs hours. The pipeline front-loads review to when changes are cheapest.

```
Cost of change:
  Refine phase:    ~5 minutes to revise analysis
  Plan phase:      ~15 minutes to update plan
  Implement phase: ~1 hour to refactor code
  After merge:     ~days to debug in production
```

### 3. Forced Thoroughness

The pipeline's rigid structure ensures each phase gets proper attention. Without structure, it's tempting to skip review or rush through planning. The pipeline makes this impossible.

### 4. Context Window Isolation

Each agent invocation runs in a fresh context. The reviewer has no memory of implementation decisions, no accumulated assumptions. This prevents the "forest for the trees" problem where familiarity breeds blindness.

## Scaling with Delegation

The model scales by allowing agents to delegate sub-tasks, each with its own feedback loop:

```
┌─────────────────────────────────────────────────────────────┐
│                      Main Task                              │
│                                                             │
│   Work → Review → Feedback                                  │
│     │                                                       │
│     ├── Sub-task A: Work → Review → Feedback                │
│     │     │                                                 │
│     │     └── Sub-sub-task: Work → Review → Feedback        │
│     │                                                       │
│     └── Sub-task B: Work → Review → Feedback                │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

Each level maintains its own quality cycle. The parent task doesn't need to understand the details of sub-tasks—only whether they meet their acceptance criteria.

This enables agents to tackle larger tasks by decomposition. A complex feature becomes a tree of manageable sub-tasks, each with quality assurance at its level.

## Running the Loop Manually

You don't need the full SDLC pipeline to benefit from the feedback loop model. The pattern works in any context:

### Simple Version

1. Ask the agent to do the work
2. Ask it to review its own work (in a new conversation or with explicit review framing)
3. Ask it to address the issues found
4. Repeat until satisfied

### Structured Version

1. **Refine**: "Analyze this problem. What are the constraints? What are the options?"
2. **Review**: "Review this analysis. What's missing? What's wrong?"
3. **Plan**: "Create an implementation plan for the chosen approach."
4. **Review**: "Review this plan. Is it complete? Are the tasks clear?"
5. **Implement**: Execute the plan
6. **Review**: Review the implementation

Each review should ideally happen in a fresh context (new conversation) or with explicit framing that encourages critical evaluation.

## Key Principles

### Separation of Concerns

The producer's job is to produce. The reviewer's job is to critique. Don't ask a single agent to do both simultaneously—the modes interfere with each other.

### Fresh Context for Review

Context pollution is real. An agent that produced work has accumulated assumptions and blind spots. A fresh context provides genuine independent review.

### Human Gates at Phase Boundaries

Automated loops run freely within phases. Human approval gates phase transitions. This balances efficiency (fast agentic iteration) with control (human approval at key points).

### Structural Enforcement

Important constraints (like the human-merge invariant) are enforced structurally, not by instruction. An agent can't bypass a network-level block, but it can ignore a prompt-level restriction.

## Related Documentation

- [SDLC Pipeline Guide](guides/sdlc-pipeline.md) — Operational details of the pipeline
- [ADR: SDLC Pipeline](adr/implemented/ADR-SDLC-Pipeline.md) — Architecture and security model
- [Why egg Works](collaboration-effectiveness.md) — Safety, quality, and collaboration model
- [HITL Decisions](hitl-decisions.md) — Human-in-the-loop workflow details

---

*The feedback loop is simple in concept but powerful in practice. Work, review, address feedback. Repeat until excellent.*
