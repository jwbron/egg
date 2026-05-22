# Plan: [Issue Title]

> Issue: #[number] | Phase: plan

## Summary

[2-3 sentence overview of the implementation approach. Reference the analysis document if applicable.]

## Implementation Phases

### Phase 1: [Phase Name]

**Goal**: [What this phase achieves]

**Tasks**:
- [TASK-1-1] [Task description] — Acceptance: [Criteria for completion]
- [TASK-1-2] [Task description] — Acceptance: [Criteria for completion]

**Dependencies**: [What must be completed before this phase]

**Exit criteria**: [How we know this phase is complete]

### Phase 2: [Phase Name]

**Goal**: [What this phase achieves]

**Tasks**:
- [TASK-2-1] [Task description] — Acceptance: [Criteria for completion]
- [TASK-2-2] [Task description] — Acceptance: [Criteria for completion]

**Dependencies**: Phase 1

**Exit criteria**: [How we know this phase is complete]

## Test Strategy

- **Unit tests**: [What unit tests will be added]
- **Integration tests**: [What integration tests will be added]
- **Manual testing**: [Steps for manual verification]

## Rollback Plan

[How to revert if something goes wrong. Include specific commands or steps.]

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| [Risk 1] | Low/Med/High | Low/Med/High | [How to mitigate] |
| [Risk 2] | Low/Med/High | Low/Med/High | [How to mitigate] |

## Migration Notes

[If applicable: database migrations, config changes, breaking changes for users]

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title, description, test plan, and manual steps that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "[Concise PR title, max 70 chars]"
  description: |
    [2-3 sentence description of the PR. Explain the problem being solved
    and the approach taken. Link to the issue for additional context.]
  test_plan: |
    - Automated: [which tests cover the changes]
    - Manual: [specific steps a reviewer should take to verify]
  manual_steps: |
    Pre-merge: [any required steps before merging, e.g. migrations, config changes]
    Post-merge: [any required steps after merging, e.g. deployments]
  # Optional context-PR framing (#2548); omit to reuse pr.title / pr.description.
  # context_title: |-
  #   Strategic plan for #<issue> — refine/plan analysis + BRC history
  # context_description: |-
  #   Carries the refine analysis, the plan, the BRC consensus
  #   history that approved each, and the agent transcripts —
  #   so reviewers approaching the slice stack can see the strategic
  #   narrative on a PR that targets the configured base branch.
slices:
  - id: 1
    name: |-
      [Phase Name]
    goal: |-
      [What this phase achieves]
    tasks:
      - id: TASK-1-1
        description: |-
          [Task description — block scalars keep text with `code: type`
          snippets, URLs, and other `: ` sequences safe from YAML parsing.]
        acceptance: |-
          [Criteria for completion]
        role: coder           # Optional: coder | tester | documenter
        files:
          - [path/to/file]
      - id: TASK-1-2
        description: |-
          [Task description]
        acceptance: |-
          [Criteria for completion]
        role: tester
        files:
          - [path/to/test_file]
  - id: 2
    name: |-
      [Phase Name]
    goal: |-
      [What this phase achieves]
    dependencies: "slice-1"
    tasks:
      - id: TASK-2-1
        description: |-
          [Task description]
        acceptance: |-
          [Criteria for completion]
        role: coder
        files:
          - [path/to/file]
```

> **YAML safety**: Always use block scalars (`|-`) for `name`, `goal`,
> `description`, and `acceptance`. Plain unquoted scalars break when the
> text contains `` `code: type` `` snippets or other `: ` sequences —
> PyYAML reads them as nested mappings and the parser silently drops back
> to markdown fallback, losing the `pr:` block (see issue #1974). Block
> scalars may safely contain nested `` ``` `` code fence examples; the
> yaml-tasks fence parser (#2743) anchors on line-boundary `` ``` ``
> markers so inner fences don't truncate the block.

> **Role assignment**: The optional `role` field assigns a task to a specific execution agent
> (`coder`, `tester`, or `documenter`). Assign roles based on which agent is permitted to modify
> the task's files — see [Agent Roles Reference](../reference/agent-roles.md#role-aware-task-assignment)
> for the file-to-role mapping. Tasks without a `role` default to the coder.

> **Context-PR framing (#2548)**: `pr.context_title` and `pr.context_description`
> are *optional* keys planners may emit to give the dedicated context PR a
> different framing from the slice PRs (e.g. "Strategic plan for #N" vs the
> slice's "Implement …"). When omitted the orchestrator falls back to
> `pr.title` / `pr.description`. Two sibling fields — `pr.context_branch` and
> `pr.context_pr_number` — exist on the contract but are populated by the
> orchestrator after the context branch is created and the context PR is
> opened; planners must NOT emit them.
>
> **As of slice-1 (#2548 part 1)**, only the schema fields and this
> planner-prompt guidance are wired. The orchestrator branch-creation
> and PR-opening hooks land in #2548 slices 3-4 — until those slices
> merge, any `context_title` / `context_description` a planner emits
> flows through the parser into `PRMetadata` but nothing acts on it
> yet, so emitting them now is forward-compatibly safe but does not
> change the rendered PR.

> **Slices vs. phases (#2137)**: The plan parser accepts either `slices:`
> (canonical, post-#2137) or `phases:` (legacy alias) at the top of the
> `# yaml-tasks` block. New plans should emit `slices:` so they ingest as
> the slice-DAG implement model expects. Within each slice, `depends_on:`
> is accepted as a non-canonical alias for the schema-canonical
> `dependencies:` key (#2743). When both keys are present the canonical
> key wins and a validation warning is emitted; using `depends_on:` alone
> is accepted without a warning. Use `dependencies:` in new plans. Each
> slice is independently
> implementable and gets its own integration branch, agent team, BRC
> consensus, and PR. The slice DAG must be a **forest** — each slice has
> at most one DAG parent. Multi-parent slices are rejected at plan
> ingestion. When a planner identifies a would-be multi-parent slice, it
> serialises the upstream cluster into a chain and records the chosen
> order on the downstream slice's `serialized_chain_order: list[str]`
> field. See [Slice-DAG Implement Phase](../architecture/slice-dag.md)
> for the full design.

---

*Authored-by: egg*
