# HITL (Human-In-The-Loop) Decision Workflow

This document explains how human decisions are captured and processed in the SDLC pipeline.

## Overview

The SDLC pipeline includes phases where human input is required before proceeding:
- **Refine phase**: Human approves the analysis before planning
- **Plan phase**: Human approves the implementation plan before coding

Two mechanisms exist for gathering human input:
1. **Formal HITL decisions** — Multiple-choice questions with checkboxes
2. **Phase approval** — Single checkbox to approve and advance to the next phase

## Formal HITL Decisions

Use formal decisions when you need the human to choose between predefined options.

### Creating a Decision

```bash
egg-contract add-decision \
  --question "Which caching strategy should we use?" \
  --options "Redis" "In-memory LRU" "File-based" \
  --format markdown
```

Output:
```markdown
<!-- egg-hitl-decision id=decision-1 -->

**Which caching strategy should we use?**

- [ ] Redis
- [ ] In-memory LRU
- [ ] File-based
- [ ] Other (explain in reply)
```

### How It Works

1. The agent includes this markdown in a GitHub comment
2. The `<!-- egg-hitl-decision id=... -->` marker identifies the decision
3. When the human checks a checkbox, GitHub triggers an `issue_comment.edited` event
4. The `sdlc-hitl.yml` workflow detects the change and updates the contract
5. If this was the last pending decision, the workflow advances to the next phase

### Auto-appended "Other" Option

When you provide `--options`, an "Other (explain in reply)" option is automatically
appended. If the human selects this, they can explain their preference in a follow-up
comment, which the agent will parse.

### Open-ended Questions

For questions without predefined options, don't use the `--options` flag:
- Simply list the question as plain text in your comment
- The human will respond via a new comment
- No special workflow processing occurs; the agent reads the reply

## Phase Approval

Phase approval is a simpler mechanism for advancing the pipeline.

### Format

```markdown
### Ready for Review

<!-- egg-phase-approval -->
- [ ] Approve and advance to plan phase

---

*Authored-by: egg*
```

### How It Works

1. The agent includes this at the end of phase completion comments
2. The `<!-- egg-phase-approval -->` marker identifies the approval section
3. When the human checks the `[x] Approve` checkbox, GitHub triggers an edit event
4. The `sdlc-hitl.yml` workflow's `handle-approval` job detects this
5. The workflow updates the contract phase and triggers the next pipeline run

### Key Differences from Decisions

| Aspect | Formal Decisions | Phase Approval |
|--------|-----------------|----------------|
| Marker | `<!-- egg-hitl-decision id=... -->` | `<!-- egg-phase-approval -->` |
| Purpose | Choose between options | Advance to next phase |
| Multiple options | Yes (with "Other") | No (single checkbox) |
| Workflow job | `handle-decision` | `handle-approval` |

## Workflow Detection

The `sdlc-hitl.yml` workflow triggers on `issue_comment.edited` events. It checks:

1. **For decisions**: Comment contains `<!-- egg-hitl-decision` and a checkbox changed
2. **For approvals**: Comment contains `<!-- egg-phase-approval` AND `[x] Approve`

### Security

- Only authorized users (configured in the workflow) can trigger phase transitions
- The bot cannot approve its own comments
- Debounce logic prevents rapid-fire updates when multiple boxes are checked quickly

## Best Practices

1. **Keep decisions focused**: One question per decision, with 2-4 clear options
2. **Always include "Other"**: The CLI does this automatically when using `--options`
3. **Separate concerns**: Use one comment for analysis/plan, another for approval
4. **Use descriptive questions**: Be specific about what you're asking

## Troubleshooting

### "Approval checkbox doesn't trigger workflow"

Check that:
- The `<!-- egg-phase-approval -->` marker is present
- The marker is on the line immediately before the checkbox
- The checkbox format is exactly `- [ ] Approve...` (spaces matter)
- The comment was edited (not a new comment)

### "Decision not detected"

Check that:
- The `<!-- egg-hitl-decision id=... -->` marker is present
- The decision ID uses only lowercase letters, numbers, and hyphens
- The checkbox format is standard markdown: `- [ ] Option` or `- [x] Option`

## Related Files

- `.github/workflows/sdlc-hitl.yml` — Workflow handling decisions and approvals
- `sandbox/egg_lib/contract_cli.py` — CLI for creating decisions
- `docs/templates/analysis.md` — Template showing decision usage
- `docs/templates/phase-completion.md` — Template for approval format
