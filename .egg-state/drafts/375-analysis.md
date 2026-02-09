# Analysis: HITL feedback flow is awkward and unclear

> Issue: #375 | Phase: refine

## Problem Statement

The current HITL (Human-In-The-Loop) feedback flow in the SDLC pipeline is awkward and unclear. Humans are uncertain about:
1. **Where to reply** — It's not obvious where to provide feedback or answer questions
2. **How to reply** — The mechanism for providing feedback (editing comments vs replying) is unclear
3. **What happens next** — The workflow for how the bot processes feedback is opaque

Currently, humans have been editing analysis documents directly to provide feedback, which is neither the intended workflow nor a scalable approach.

## Current Behavior

The existing HITL flow has two distinct mechanisms:

### 1. Checkbox-based Decisions (`hitl.py`, `sdlc-hitl.yml`)

For **multiple-choice questions**, the agent creates a decision via:
```bash
egg-contract add-decision --question "Which approach?" --options "A" "B" --format markdown
```

This outputs markdown with checkboxes that the human can interact with:
```markdown
<!-- egg-hitl-decision id=decision-1 -->

**Which approach?**

- [ ] A
- [ ] B
- [ ] Other (explain in reply)
```

When the human checks a checkbox and edits the comment, `sdlc-hitl.yml` triggers (on `issue_comment.edited`), waits 30 seconds (debounce), then processes the decision.

**Pain points:**
- Works well for predefined options
- "Other (explain in reply)" is vague — where should the reply go?
- No clear mechanism for follow-up or clarification

### 2. Open-ended Questions (Plain text)

For questions without predefined options, the agent is instructed to "list these as plain text" and the human will "respond via comment." However:
- There's **no formal detection mechanism** for these replies
- The agent has to manually parse issue comments to find answers
- No debounce or acknowledgment when feedback is provided
- Easy to miss or misinterpret feedback

### 3. Phase Approval (`sdlc-hitl.yml:handle-approval`)

The agent posts a phase completion comment with:
```markdown
<!-- egg-phase-approval -->
- [ ] Approve and advance to plan phase
```

When the human checks `[x] Approve`, the workflow detects this and advances the pipeline. This mechanism works well but is distinct from the feedback flow.

### 4. Internal Review Feedback

When an internal reviewer marks a draft as `needs_revision`, feedback is stored in the contract (`refine_review_feedback` / `plan_review_feedback`) and injected into the agent's next prompt. This is a well-defined flow but doesn't involve human feedback.

## Constraints

- **GitHub API limitations**: Comment editing triggers `issue_comment.edited`, but detecting *which part* was edited is non-trivial
- **Debounce requirement**: Rapid checkbox changes shouldn't trigger multiple workflow runs
- **Authorization**: Only authorized users (`jwbron`) should be able to trigger phase transitions
- **Bot safety**: The bot must not be able to trigger its own decisions
- **Markdown rendering**: The feedback mechanism must render correctly in GitHub's markdown renderer
- **Discoverability**: Humans need clear, intuitive instructions without reading docs

## Options Considered

### Option A: Dedicated Feedback Comment (Recommended in Issue)

**Approach**: The bot posts a **separate comment** specifically for feedback and questions. This comment contains:
1. A list of questions the bot has
2. A structured section for the human to edit their answers
3. Clear instructions at the top

Example:
```markdown
<!-- egg-feedback id=feedback-1 -->

## Questions & Feedback

Please **edit this comment** to answer questions or provide feedback.
When you're done, check the box below to notify me.

---

### Open Questions

**Q1: What is the expected request volume?**
> _Your answer here_

**Q2: Should we support legacy browsers?**
> _Your answer here_

---

### Additional Feedback (optional)
> _Add any other feedback or context here_

---

- [ ] Submit feedback (I'm done editing)
```

**Pros**:
- **Clear single location** — Humans know exactly where to provide feedback
- **Editable structure** — Human fills in answers in a consistent format
- **Checkbox trigger** — Same debounce/workflow mechanism as decisions
- **Separates concerns** — Analysis/plan in one comment, feedback in another
- **Works with existing workflow** — Extends `sdlc-hitl.yml` with new marker

**Cons**:
- **Requires editing comment** — Some users find comment editing awkward
- **Multiple edits required** — User must edit answers *and* check the box
- **Parse complexity** — Agent must parse blockquotes or a structured format

### Option B: Reply-based Feedback with Magic Commands

**Approach**: The human replies to the analysis/plan comment with their feedback. The bot detects replies and parses feedback using structured commands.

Example human reply:
```markdown
/answer Q1: Around 10k requests per day
/answer Q2: No, we can drop IE11 support
/feedback Consider using Redis for caching
/approve
```

**Pros**:
- **Natural reply flow** — Humans reply as they would in normal conversation
- **No comment editing** — Some find replying more intuitive than editing
- **Explicit structure** — Magic commands are unambiguous

**Cons**:
- **Learning curve** — Humans must learn command syntax
- **Detection complexity** — Workflow must detect and parse replies, not just edits
- **Comment threading** — GitHub's comment threading is limited
- **Multiple replies** — Humans might split feedback across replies

### Option C: GitHub Issue Form Fields

**Approach**: Leverage GitHub Issue forms or structured sections within the issue body itself for feedback.

**Pros**:
- **GitHub-native** — Uses built-in GitHub features
- **Persistent location** — Feedback lives in the issue body

**Cons**:
- **Limited to issue creation** — Forms only work at issue creation time
- **Not dynamic** — Can't add questions mid-workflow
- **Mixing concerns** — Issue body is for requirements, not agent Q&A

### Option D: External Tool (Slack, Web Form)

**Approach**: Use a separate tool (Slack DMs, web form, etc.) for collecting feedback.

**Pros**:
- **Rich UI** — Can have proper form fields, dropdowns, etc.
- **Real-time notifications** — Better feedback loop

**Cons**:
- **Context switching** — Human must leave GitHub
- **Sync complexity** — Must sync feedback back to the issue
- **Additional dependencies** — Requires maintaining another system

## Recommended Approach

**Option A: Dedicated Feedback Comment** is recommended because:

1. **Builds on existing infrastructure** — Uses the same `issue_comment.edited` trigger and debounce mechanism already in place for checkbox decisions
2. **Clear affordance** — A dedicated comment with explicit instructions is more discoverable than magic commands or reply-based flows
3. **Single location** — Eliminates "where do I reply?" confusion by providing exactly one place for all feedback
4. **Consistent with current patterns** — Extends rather than replaces the checkbox paradigm

**Implementation approach**:
1. Add a new comment marker: `<!-- egg-feedback id=feedback-1 -->`
2. Extend `sdlc-hitl.yml` to handle feedback comments (detect edits, debounce, update contract)
3. Add a new contract field `feedback_responses` to store parsed answers
4. Modify agent prompts to generate the feedback comment and parse responses

## Open Questions

The following questions need human input before finalizing the implementation:

**Q1: Should answers be structured (blockquotes) or free-form?**

Structured blockquotes make parsing easier but feel more rigid:
```markdown
**Q1: Expected volume?**
> 10k requests/day
```

Free-form feels more natural but is harder to parse:
```markdown
**Q1: Expected volume?**
About 10k requests per day, maybe more during peak times.
```

**Q2: Should multiple-choice decisions remain separate from the feedback comment?**

Options:
- **Keep separate**: Checkbox decisions stay in analysis comment, open-ended questions go in feedback comment
- **Consolidate**: Move all questions (checkbox and open-ended) to a single feedback comment

**Q3: Should feedback require a "Submit" checkbox or auto-detect on edit?**

Options:
- **Require checkbox**: Human must check `[x] Submit feedback` after editing answers — explicit but extra step
- **Auto-detect**: Any edit to the feedback comment triggers processing — simpler but may fire on partial edits

---

*Authored-by: egg*
