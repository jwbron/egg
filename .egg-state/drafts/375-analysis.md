# Analysis: HITL feedback flow is awkward and unclear

> Issue: #375 | Phase: refine

## Problem Statement

The current Human-in-the-Loop (HITL) feedback flow in the SDLC pipeline is awkward and unclear. Humans are uncertain about where and how to provide feedback on analysis documents, plans, and agent questions. The issue author has resorted to editing analysis documents directly to provide feedback, which is not the intended workflow.

**Current state**: The system uses checkbox-based decisions in GitHub comments, but lacks a clear mechanism for free-form feedback and guidance beyond selecting predefined options.

**Desired outcome**: A clear, intuitive way for humans to provide feedback, answer questions, and guide the agent without ambiguity about where to respond or how responses will be processed.

## Current Behavior

The HITL system currently works as follows:

1. **Agent posts analysis/plan comments** with embedded checkboxes using the `<!-- egg-hitl-decision -->` marker (see `shared/egg_contracts/hitl.py:98-128`)

2. **Multiple-choice decisions** are handled via checkboxes:
   - Human edits the comment to check a box
   - `sdlc-hitl.yml` workflow detects the edit
   - 30-second debounce allows for multiple changes
   - Decision is recorded in the contract

3. **Phase approvals** use a separate `<!-- egg-phase-approval -->` marker with a single "Approve" checkbox

4. **Free-form feedback** has no formal mechanism:
   - Analysis template suggests "include as plain text" for open-ended questions
   - Humans are instructed to "respond via comment"
   - No workflow automation picks up these comment responses
   - Agent must be re-invoked to see feedback

**Key pain points**:
- Editing analysis documents feels wrong (they're meant to be read-only outputs)
- Adding a reply comment may not be noticed by the agent
- No clear "this is where you provide feedback" section
- No indication when feedback has been processed

## Constraints

- **GitHub API limitations**: Issue comments can be edited or created, but not "linked" as replies to specific sections
- **Workflow triggers**: Currently only `issue_comment.edited` is monitored; `issue_comment.created` would require additional workflow logic
- **Debounce timing**: The 30-second debounce works well for checkboxes but may not suit longer-form text input
- **Comment authorship**: Humans cannot directly edit bot-authored comments (only bot can edit its own comments)
- **Audit trail**: All feedback should be captured in the contract's audit log for traceability
- **Single authorized user**: Currently only `jwbron` can trigger HITL decisions (line 32-33 of `sdlc-hitl.yml`)

## Options Considered

### Option A: Separate Bot Comment for Feedback and Questions

**Approach**: When the agent posts an analysis, it also posts a separate comment with:
- A clearly marked "feedback section" for the human to edit
- Explicit questions that need answers
- Instructions on how to provide input

The workflow monitors edits to this feedback comment. When edited, it re-invokes the agent with the feedback content.

**Structure**:
```markdown
## Feedback & Questions

<!-- egg-feedback issue=375 phase=refine -->

### Questions from egg

1. **Question 1**: [Agent's question here]

   Answer: _[Edit this line to provide your answer]_

2. **Question 2**: [Another question]

   Answer: _[Edit this line to provide your answer]_

### Additional Feedback

_Edit below to provide any additional feedback, corrections, or guidance:_

---

[Your feedback here]

---

<!-- egg-feedback-end -->

When done, check the box below:
- [ ] Feedback complete, please proceed
```

**Pros**:
- Clear, dedicated location for feedback
- Structured Q&A format reduces ambiguity
- Human edits their own content (not the analysis document)
- Workflow can detect when feedback is ready via checkbox
- Separates content (analysis) from interaction (feedback)

**Cons**:
- Two comments per phase adds clutter
- Requires parsing free-form text from a structured area
- Human must know to edit the specific comment
- Multi-line text editing in GitHub comments can be fiddly

### Option B: Inline Feedback Markers in Analysis Comment

**Approach**: Keep a single analysis comment but add designated inline areas for feedback. Use special markers that the agent can parse.

**Structure**:
```markdown
## Analysis: Issue Title

[Analysis content...]

## Questions

1. Which caching strategy?
   <!-- egg-answer q=1 -->
   _Your answer here_
   <!-- /egg-answer -->

2. Expected request volume?
   <!-- egg-answer q=2 -->
   _Your answer here_
   <!-- /egg-answer -->

## Your Feedback
<!-- egg-feedback -->
_Add any corrections or additional guidance here_
<!-- /egg-feedback -->

---
- [ ] Approve and advance to next phase
```

**Pros**:
- Single comment keeps context together
- Inline answers appear next to questions
- Uses familiar HTML comment markers
- Human can still edit bot's comment (via GitHub's edit feature for repo members)

**Cons**:
- Editing bot comments feels wrong / may have permission issues
- Analysis and feedback are mixed together
- Harder to track what's changed between edits
- Human edits may accidentally break the analysis content

### Option C: Reply-Based Feedback with Structured Format

**Approach**: Human replies to the analysis comment using a structured format. A workflow monitors for new comments matching the format and processes them.

**Structure** (human posts as new comment):
```markdown
## Feedback for #375 Analysis

<!-- egg-feedback-reply issue=375 phase=refine -->

### Answers

1. **Caching strategy**: Redis with 5-minute TTL
2. **Request volume**: Approximately 1000 req/min peak

### Additional Notes

Consider also checking the edge case where...

---

- [x] Ready for processing
```

**Pros**:
- Human creates their own comment (natural authorship)
- Reply threading in GitHub provides visual hierarchy
- No editing of bot comments required
- Easy to track multiple rounds of feedback

**Cons**:
- Requires `issue_comment.created` trigger (new workflow logic)
- Human must follow a specific format
- May not be obvious that a structured reply is expected
- Multiple feedback comments could cause confusion

### Option D: GitHub Issue Form / Discussion Integration

**Approach**: Use GitHub's issue forms or discussions feature to create a structured feedback template, separate from issue comments.

**Pros**:
- Native GitHub forms provide structured input
- Clear separation between feedback and analysis
- Could use GitHub Discussions for threaded conversation

**Cons**:
- Significant architectural change
- Moves away from issue-centric workflow
- GitHub Forms are for issue creation, not in-flight feedback
- Discussions add complexity

## Recommended Approach

**Recommended: Option A (Separate Bot Comment for Feedback)**

This option provides the clearest user experience with minimal architectural changes:

1. **Clear separation of concerns**: Analysis is read-only output; feedback comment is the interaction point
2. **Explicit affordance**: The feedback comment makes it obvious where and how to respond
3. **Structured Q&A**: Agent's questions get structured answer slots, reducing ambiguity
4. **Familiar pattern**: Uses the existing checkbox-to-proceed pattern already in use for approvals
5. **Minimal workflow changes**: Extends existing `issue_comment.edited` handling with a new marker type

**Implementation outline**:
1. Add `<!-- egg-feedback -->` marker support to `hitl.py` and `sdlc-hitl.yml`
2. Modify phase prompts to instruct agents to post a separate feedback comment
3. Workflow detects feedback submission via checkbox, re-invokes agent with content
4. Agent parses answers and feedback, incorporates into next iteration

The slight clutter of two comments is outweighed by the clarity of having a dedicated, clearly-labeled feedback zone that humans own and edit.

## Open Questions

**For the issue author to address via reply:**

1. Should the feedback comment be minimized after processing (like decision status comments), or kept visible for audit trail?

2. How should multi-round feedback work? If the agent processes feedback and has follow-up questions, should it post a new feedback comment or update the existing one?

3. The current debounce is 30 seconds. For longer-form text feedback, should this be extended (e.g., 2 minutes) to allow more editing time?

---

*Authored-by: egg*
