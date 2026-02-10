# /sdlc - SDLC Pipeline Manager

Manage the local SDLC pipeline workflow.

## Usage

```
/sdlc [action] [options]
```

## Actions

### status
Show current SDLC state for the active issue.

```
/sdlc status
```

### refine
Execute the refine phase - analyze the issue.

```
/sdlc refine
```

### plan
Execute the plan phase - create implementation plan.

```
/sdlc plan
```

### implement
Execute the implement phase - write code.

```
/sdlc implement
```

### review
Check for human review decisions in local files.

```
/sdlc review
```

## Local File Interaction

The local SDLC workflow uses files in `.egg-state/local/` for human interaction:

- `analysis-review.md` - Review the analysis (refine phase)
- `plan-review.md` - Review the plan (plan phase)
- `feedback.md` - Answer open-ended questions
- `decisions.md` - Make multiple-choice decisions

## Instructions

When invoked, read the current SDLC state from:
1. Contract: `.egg-state/contracts/{issue}.json`
2. Local state: `.egg-state/local/`
3. Drafts: `.egg-state/drafts/`

Then execute the appropriate phase based on the contract state and any pending human decisions.

After completing work:
1. Save outputs to the appropriate files
2. Commit changes if in implement phase
3. Report status and next steps

## Example Session

```
User: /sdlc status
Assistant: Current SDLC state for issue #437:
  Phase: refine
  Status: In progress
  Draft: .egg-state/drafts/437-analysis.md (pending review)

User: /sdlc review
Assistant: Checking for human decisions...
  Found: analysis-review.md
  Decision: APPROVED
  Advancing to plan phase...
```
