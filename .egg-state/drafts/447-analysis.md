# Analysis: Document SDLC Work Loop Concepts

> Issue: #447 | Phase: refine

## Problem Statement

The egg project has robust operational documentation for the SDLC pipeline (how to use it, what commands to run, phase transitions) but lacks a conceptual document explaining *why* this workflow exists and the fundamental patterns that make it effective. The issue describes a layered feedback loop model:

1. **Agentic Feedback Loop**: work → review → address feedback (agent-driven iteration)
2. **Human Feedback Loop**: review → address feedback (human-driven iteration)
3. **Broader Pipeline**: problem statement → refine → plan → implement → review → acceptance (with human gates at each transition)

This pattern—structured iteration with mandatory human oversight—is the conceptual foundation that enables high-quality autonomous software development. Creating clear documentation for this concept will:
- Help users understand why the workflow produces high-quality results
- Establish the conceptual foundation alongside the technical foundations (gateway, sandbox)
- Prepare for future extensions (agent delegation, task decomposition)

## Current Behavior

The existing documentation covers:

| Document | Focus | Gap |
|----------|-------|-----|
| `docs/guides/sdlc-pipeline.md` | Operational: phases, commands, contracts, CLI | Explains *how*, not *why* |
| `docs/adr/implemented/ADR-SDLC-Pipeline.md` | Architectural: threat model, role enforcement, contract schema | Explains *what* is enforced, not the workflow philosophy |
| `docs/collaboration-effectiveness.md` | Strategy: public async workflow, safety through constraints | Focuses on team collaboration, not the iteration model |
| `docs/guides/agent-mode-design.md` | Design: agent autonomy vs constraints | Guidelines for workflow design, not the core iteration pattern |

**The gap**: No document explains the fundamental "work → review → address feedback" loop as a composable pattern, why it works, and how it scales to larger tasks through delegation.

## Constraints

- **Documentation consistency**: New document must follow existing style (markdown, structure, cross-references)
- **Audience**: Both humans understanding the system and LLM agents using it as context
- **Placement**: Should fit logically in the documentation hierarchy (likely under `docs/` at the top level or in `docs/guides/`)
- **Integration**: Must update `docs/index.md` to reference the new document
- **Scope boundary**: This is conceptual documentation, not new implementation

## Options Considered

### Option A: New Top-Level Conceptual Document

**Approach**: Create `docs/agentic-work-loop.md` as a standalone conceptual document explaining the feedback loop model, then update `docs/index.md` and `docs/guides/sdlc-pipeline.md` to reference it.

**Pros**:
- Clear separation between conceptual foundations and operational guides
- Establishes a "theory" document that other docs can reference
- Easy to find in the documentation index
- Matches the pattern of `docs/collaboration-effectiveness.md` (strategy/philosophy doc at top level)

**Cons**:
- Adds another document to maintain
- May overlap slightly with existing docs

### Option B: Expand Existing SDLC Pipeline Guide

**Approach**: Add a "Conceptual Foundation" section to `docs/guides/sdlc-pipeline.md` explaining the work loop pattern.

**Pros**:
- Keeps related content together
- No new file to maintain
- Users reading the operational guide get the conceptual background

**Cons**:
- Makes an already detailed document longer
- Mixes conceptual and operational content
- Harder to reference from other documents

### Option C: New ADR-Style Document

**Approach**: Create `docs/adr/implemented/ADR-Agentic-Work-Loop.md` as an architectural decision record.

**Pros**:
- ADRs have a clear structure (context, decision, consequences)
- Fits with existing ADR pattern

**Cons**:
- ADRs document *decisions*, not conceptual frameworks
- The work loop isn't a decision—it's the core philosophy
- Would feel forced into the ADR template

## Recommended Approach

**Option A: New Top-Level Conceptual Document** is recommended.

Rationale:
1. The issue explicitly says this should be "the conceptual foundation of this project, along with the technical foundations of the gateway and sandbox"—positioning it as a foundational document, not an operational guide addition.
2. The pattern of `docs/collaboration-effectiveness.md` shows a precedent for strategy/philosophy documents at the top level.
3. The concept of nested feedback loops and agent delegation is significant enough to warrant its own document.
4. This approach allows the operational guide (`sdlc-pipeline.md`) to remain focused on *how* while the new doc covers *why*.

### Proposed Document Structure

```
docs/agentic-work-loop.md
├── Overview (the three-layer model)
├── The Agentic Feedback Loop
│   ├── Work phase
│   ├── Review phase
│   └── Address feedback phase
├── The Human Feedback Loop
│   ├── When humans enter the loop
│   └── Approval gates
├── The Complete Pipeline
│   ├── Problem statement → Acceptance
│   └── Phase diagram
├── Why This Works
│   ├── Quality through iteration
│   ├── Alignment through gates
│   └── Safety through structure
├── Scaling Through Delegation (future)
│   ├── Agent decomposition
│   └── Nested loops
└── Related documentation links
```

### Required Changes

1. **Create**: `docs/agentic-work-loop.md` with the structure above
2. **Update**: `docs/index.md` to add the new document under "Strategy" section
3. **Update**: `docs/guides/sdlc-pipeline.md` to reference the conceptual doc in the introduction

## Open Questions

**Open-ended questions for human input:**

1. **Document naming**: Is `agentic-work-loop.md` the right name? Alternatives could be `work-loop-model.md`, `iteration-model.md`, or `feedback-loop-architecture.md`. The issue uses "SDLC work loop" but that might conflate with the existing SDLC pipeline doc.

2. **Future delegation section**: The issue mentions "allowing the LLM to arbitrarily insert more agents in each agentic step to decompose work and delegate." Should the new document include a section on this future capability, or should we keep it focused on the current implemented pattern?

3. **Diagram style**: Should the document include ASCII diagrams (like existing docs), or is there a preference for a different format?

---

*Authored-by: egg*
