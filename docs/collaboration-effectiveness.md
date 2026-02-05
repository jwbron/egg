# Why egg Makes AI-Assisted Development Collaborative

egg is an open-source, sandboxed autonomous software engineering agent. Unlike black-box AI coding assistants that operate in private IDE sessions, egg is designed so that every interaction — from task assignment to code review — happens in shared, observable spaces. This document explains why that matters, what it means for day-to-day workflows, and how to use it effectively.

## The Core Insight: AI Work Should Be Visible

Most AI coding tools today operate inside a single developer's editor. The conversation, the decisions, and the intermediate reasoning all disappear when the session ends. This creates several problems:

- **No shared learning.** When one developer figures out how to prompt an AI agent to write effective integration tests, that knowledge stays in their head.
- **No oversight of AI-generated work.** Code appears in a PR, but the reasoning behind it — what the agent tried, what it discarded, what instructions it was given — is invisible.
- **No opportunity for early intervention.** By the time a reviewer sees the PR, the agent has already committed to an approach. If the approach is wrong, all that work is wasted.

egg solves these problems by making AI interactions public by default. The agent operates through GitHub issues, pull requests, and workflow logs — artifacts that the entire team already watches.

## How It Works in Practice

### Task Assignment Is Public

Work is assigned to egg by @mentioning it in a GitHub issue or PR comment. This means every task assignment is visible to the team:

```
@james-in-a-box implement the retry logic described in this issue
```

Anyone watching the repo sees the request, understands the scope, and can weigh in before the agent starts. This is fundamentally different from typing a prompt into a private IDE session.

### Plans Are Reviewable Before Implementation

For non-trivial work, egg's pipeline (see [issue #133](https://github.com/jwbron/egg/issues/133)) is designed to separate planning from implementation. The agent first produces a structured plan — posted as a comment on the issue or committed as a JSON contract file — that describes what it intends to do, which files it will change, and how it will verify correctness.

This creates a natural checkpoint. A teammate can glance at the plan and say "that approach won't work because of X" *before* a single line of code is written. In a private IDE session, that feedback loop doesn't exist.

### All Work Is Async

egg runs as a GitHub Action. You @mention it, then walk away. The agent works autonomously — reading the codebase, writing code, running tests — and reports back when it's done by posting a comment with a summary and a link to the workflow logs.

This async model has several advantages:

- **No blocked time.** You don't sit and watch the agent work. You assign a task from your phone on the train and review the result later.
- **Mobile-first review.** egg posts Slack notifications with summaries and quick-action buttons. You can review, approve, or request changes from your phone.
- **Parallel work.** Multiple engineers can have egg working on different issues simultaneously, each in its own isolated git worktree.

### Workflow Logs Are the Audit Trail

Every egg run produces a full GitHub Actions log. This means:

- **Reproducibility.** If something goes wrong, you can see exactly what the agent did, what tools it called, and what output it got.
- **Strategy sharing.** When a colleague gets a great result from egg, you can read the workflow log to see what prompt they used and how the agent interpreted it. This is dramatically more useful than "I asked Claude to do X and it worked."
- **Accountability.** Every PR created by egg links back to the workflow run that produced it. Code review is informed by the full context of *how* the code was generated.

## Collaboration Patterns That Emerge

### Early Feedback on AI-Directed Work

Because task assignments and plans are visible in issues and PRs, team members naturally provide input at the earliest possible stage. A common pattern:

1. Engineer A opens an issue and @mentions egg to plan a solution.
2. Engineer B sees the plan comment and suggests a different approach.
3. Engineer A refines the instructions.
4. egg implements the agreed-upon approach.

Without visibility, Engineer B's input would come at PR review time — after the agent has already written code based on the wrong approach.

### Shared Prompt Strategies

When @mention prompts and their results are all visible in issue threads, the team develops a shared understanding of what works. Someone discovers that giving egg explicit acceptance criteria produces better tests. Someone else finds that pointing it at a specific ADR leads to more consistent architecture. These strategies spread organically because the artifacts are public.

### Multi-Agent Review Pipelines

[Issue #134](https://github.com/jwbron/egg/issues/134) describes AI-powered code review bots that complement egg's implementation work. The planned architecture uses the same GitHub Action infrastructure to run automated reviewers:

- **Security reviewer** — flags vulnerabilities and unsafe patterns in PRs
- **Standards enforcer** — checks adherence to team conventions and ADRs
- **Plan verification** — confirms that the PR changes match the stated requirements
- **Bounded-context reviewer** — a reviewer that deliberately lacks internal context, spotting assumptions and documentation gaps from an outsider's perspective

These reviewers post inline comments on PRs, visible to everyone. The AI's review reasoning is public, so the team can evaluate whether the feedback is useful and calibrate the reviewer over time. This is a significant departure from private linting — the review logic itself becomes a shared, improvable artifact.

### CI Autofixing as Collaborative Infrastructure

[Issue #138](https://github.com/jwbron/egg/issues/138) implements automatic CI failure fixing. When a lint or test check fails on an egg-owned PR, a new workflow triggers egg to diagnose the failure logs and push a fix. The entire loop — failure, diagnosis, fix, re-run — is visible in the PR timeline.

This matters for collaboration because:

- **Fixes are reviewable.** The autofix commit shows up in the PR diff. Reviewers see exactly what was changed and why.
- **Escalation is transparent.** After 2 failed autofix attempts, egg posts a comment asking for human help. The failure context — logs, prior fix attempts — is all in the thread.
- **The team learns.** Recurring CI fix patterns become visible. If egg keeps fixing the same linting rule, someone can add a pre-commit hook or update the coding standards to prevent the issue at source.

## Structured Verification: Trust Through Process

[Issue #133](https://github.com/jwbron/egg/issues/133) introduces structurally enforced checkpoints that prevent the agent from marking its own work as done. This is critical for collaborative trust:

- **Externalized verification.** A separate agent invocation (with its own context window and instructions) reviews the implementing agent's work. The reviewer cannot be influenced by the implementer's reasoning.
- **Contracts over TODO lists.** Progress is tracked in a structured JSON file that the implementing agent cannot unilaterally mark as complete. Only the review agent can flip the `passes` field.
- **Circuit breakers.** If the agent spins on review cycles without converging, it stops and escalates to a human. This prevents the failure mode where an agent burns tokens on an approach that won't work.
- **Small batches.** Work is broken into phases of ~1-2k lines of change, each independently reviewed. This keeps PRs digestible for human reviewers and limits blast radius.

The multi-stage pipeline — Refine Issue, Plan, Implement (per phase), Create PR — is orchestrated as separate GitHub Actions jobs. State passes between stages via the contract file committed to the branch. Every stage transition is a visible event in the workflow.

## Safety Model: Why Collaboration Requires Constraints

egg's sandboxed architecture isn't just a security feature — it's what makes the collaborative workflow trustworthy:

| Constraint | Why It Enables Collaboration |
|---|---|
| Agent cannot merge PRs | Reviewers know their approval is the final gate, not a suggestion |
| Agent can only push to `egg/*` branches | No risk of clobbering someone else's work on shared branches |
| Credentials never enter the sandbox | Team doesn't need to worry about credential exposure in public logs |
| Network isolation in private mode | Confidential code stays confidential even with full workflow visibility |
| Gateway enforces all policies | Controls can't be bypassed by prompt injection — they're infrastructure, not instructions |

The key principle: **security through infrastructure, not instructions.** Telling an LLM "don't push to main" can be bypassed. Removing the ability to push to main at the network layer cannot. This means the team can confidently give the agent broad autonomy within its sandbox, knowing the constraints are structural.

## How to Use egg Effectively

### Write Clear, Public Task Descriptions

Since your @mention prompt is visible to the team, treat it like a mini-spec:

- State the goal, not just the action ("add retry logic so transient failures don't break the pipeline" vs. "add retry logic")
- Reference relevant issues, ADRs, or code ("follow the pattern in `service/retry.py`")
- Include acceptance criteria ("tests should cover timeout, connection reset, and 5xx responses")

This benefits both the agent (better output) and your teammates (they understand the intent without asking).

### Use the Plan Phase for Alignment

For anything beyond a trivial fix, ask egg to plan first. Review the plan comment. Loop in teammates who own adjacent systems. Get alignment before the agent writes code. This is the highest-leverage collaboration point — 5 minutes of plan review saves hours of implementation rework.

### Review Workflow Logs, Not Just Diffs

When reviewing an egg-generated PR, click through to the workflow run. Understanding *how* the agent arrived at the code often reveals issues that the diff alone doesn't — for example, the agent might have misunderstood a requirement and produced technically correct but functionally wrong code.

### Share What Works

When you get a particularly good result from egg, share the prompt and approach in your team channel. Link to the issue or workflow run. Over time, this builds a shared playbook for effective AI-assisted development.

### Let the Agent Fail Visibly

Resist the temptation to redo work privately when egg produces a bad result. Instead, post feedback in the issue thread, refine the instructions, and let egg try again. The failed attempt and the correction are valuable signal for the team — they show what doesn't work and why.

## What's Coming

Based on open issues, several features will deepen egg's collaborative capabilities:

- **Multi-agent SDLC pipeline** ([#133](https://github.com/jwbron/egg/issues/133)) — Full issue-to-PR pipeline with independent verification at every stage, visible as sequential GitHub Actions jobs. Each stage (issue refinement, planning, implementation, PR creation) runs as a separate agent invocation with its own context window. A structured JSON contract file tracks progress across stages, and only a review agent — never the implementing agent — can mark work as complete.
- **AI code review bots** ([#134](https://github.com/jwbron/egg/issues/134)) — Automated reviewers that post inline comments on PRs. The planned MVP is a single-agent GitHub Action reviewer that runs on `pull_request` events, posting advisory comments (not blocking reviews). Specialized modes for security, standards enforcement, and outsider-perspective review are planned for later phases.
- **CI autofixing** ([#138](https://github.com/jwbron/egg/issues/138)) — Automatic diagnosis and repair of CI failures on agent-owned PRs, starting with lint fixes and expanding to test and security scan failures. Includes loop prevention (max 2 attempts before human escalation) and full visibility of the fix→verify cycle in the PR timeline.
- **Dedicated agent accounts** ([#132](https://github.com/jwbron/egg/issues/132)) — Safe @mentionable GitHub identities for agent invocation, making the interaction model more natural and preventing accidental mentions of unrelated users.

## Summary

egg makes AI-assisted development collaborative by operating in shared spaces rather than private sessions. Task assignments, plans, implementation, review, and verification all happen in GitHub issues and PRs — artifacts the whole team can see, comment on, and learn from. The async model means work happens in the background and results are delivered when ready. The sandboxed architecture means the team can trust the agent's constraints are real, not just requested. And the structured pipeline means quality gates are enforced by process, not by hope.

The result is a workflow where AI augments the team, not just the individual — and where the team gets better at using AI over time, because every interaction is a shared learning opportunity.
