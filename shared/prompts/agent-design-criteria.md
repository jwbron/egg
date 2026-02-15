<!-- Shared agent-design review criteria: consumed by GHA prompt scripts AND orchestrator pipelines.
     Keep this file output-format-agnostic (no gh commands, no verdict JSON references). -->

## Review Philosophy

The guidelines in `docs/guides/agent-mode-design.md` are **guidelines, not absolute rules**. Apply them with judgment:

- **Orienting vs constraining**: The key question is whether context *helps* the agent work effectively or *constrains* its ability to explore. Lightweight metadata, task context, and small summaries that orient the agent are fine—even encouraged. The concern is with large pre-fetched diffs or logs that prevent the agent from seeing what it needs.

- **Practical balance**: A design that's 80% aligned but works well is better than 100% pure but fragile. Preserve useful functionality while avoiding unnecessary complexity.

- **Benefit of the doubt**: If a design choice could be interpreted as either helpful orientation or problematic pre-fetching, lean toward the charitable interpretation unless there's clear evidence of harm.

## What to Look For

Flag these **clear** anti-patterns:

1. **Excessive pre-fetching**: Baking *large* diffs (10KB+) or full file contents into prompts. Small metadata and task context are fine.
2. **Structured output for humans**: Requiring JSON when output goes directly to humans (PR comments, reviews)
3. **Post-processing pipelines**: Scripts that parse agent output to take actions the agent could take directly
4. **Rigid procedures**: Micromanaging step-by-step procedures when objectives would suffice
5. **Prompt-level security**: Using instructions for constraints that should be sandbox-enforced

## What to Skip

- General code quality, style, naming — the base review bot covers this
- Security issues unrelated to agent design — the base review bot covers this
- Correctness/logic errors — the base review bot covers this
- Borderline cases where the design choice is reasonable
