# Why egg Works: Safety, Quality, and Collaboration

Engineer A asks an AI assistant to refactor the auth module. It works great. A week later, Engineer B tackles the same area — different prompts, different approach, conflicting patterns. Neither knows what the other did or why. The AI conversations that drove both changes vanished when the IDE sessions ended.

This is the core problem with private AI coding tools: **the work is visible, but the process is invisible.** egg takes a different approach — every interaction happens in shared, observable spaces that the whole team can see.

## How It Works

```
@mention in issue ──→ Plan (posted as comment) ──→ Implement ──→ PR ──→ Review ──→ Human merges
       ↑                        ↑                                  ↑
  Team sees scope         Team reviews approach             Team reviews code
  and can redirect        before code is written            with full context
```

**Task assignment is public.** Work starts with an @mention in a GitHub issue or PR comment. Anyone watching the repo sees the request, understands the scope, and can weigh in before the agent starts.

**Plans are reviewable before implementation.** For non-trivial work, egg produces a structured plan describing what it intends to do, which files it will change, and how it will verify correctness. This is the highest-leverage collaboration point: five minutes of plan review saves hours of rework.

**All work is async.** You @mention egg, then walk away. It reads the codebase, writes code, runs tests, and opens a PR. You can assign a task from your phone and review the result later.

**Workflow logs are the audit trail.** Every run produces a full GitHub Actions log — what the agent did, what it tried, what output it got. When a colleague gets a good result, you can read the log to see exactly what prompt and approach worked. This is dramatically more useful than "I asked the AI to do X and it worked."

## What Changes for the Team

**Feedback moves earlier.** Without visibility, feedback comes at PR review — after the agent has already committed to an approach. With egg, teammates see the plan *before* code is written and can redirect the approach at low cost.

**Prompt strategies become shared knowledge.** When prompts and results are visible in issue threads, the team develops a shared understanding of what works. Someone discovers that explicit acceptance criteria produce better tests. Someone else finds that referencing a specific ADR leads to more consistent architecture. These strategies spread organically because the artifacts are public.

**Reviewing AI work becomes more informed.** Clicking through to the workflow run often reveals issues the diff alone doesn't — the agent might have misunderstood a requirement and produced technically correct but functionally wrong code. The reasoning is available, not just the result.

**Roles shift, not shrink.** Developers spend more time on specification and plan review. Reviewers review with full context — logs, plan, reasoning — not just a diff. Junior engineers learn from visible reasoning chains; every egg interaction is a worked example.

## Trust Through Constraints

egg's sandboxed architecture makes the collaborative workflow trustworthy. The key principle: **security through infrastructure, not instructions.** Telling an LLM "don't push to main" can be bypassed. Removing the ability at the network layer cannot.

| Constraint | Why It Enables Collaboration |
|---|---|
| Agent cannot merge PRs | Reviewers know their approval is the final gate, not a suggestion |
| Agent can only push to `egg/*` branches | No risk of clobbering someone else's work |
| Credentials never enter the sandbox | No credential exposure risk, even in public logs |
| Network isolation in private mode | Confidential code stays confidential with full workflow visibility |
| Gateway enforces all policies | Controls can't be bypassed by prompt injection — they're infrastructure |

This shifts the reviewer's job from defensive auditing to quality assessment. Instead of asking "could this agent do something dangerous?", reviewers ask "is this output correct?" — a better use of engineering time and a more sustainable model as AI-generated code volume increases.

## Keeping Async Work High Quality

The natural concern with async AI work: if nobody is watching, how do you trust the output? egg addresses this with structural mechanisms rather than relying on prompt instructions that agents can ignore or misinterpret.

- **Independent verification.** A separate agent invocation — with its own context window — reviews the implementing agent's work. The implementer cannot mark its own work as complete. This is the async equivalent of pair programming with a guaranteed-independent second opinion.
- **Circuit breakers.** If the implement-review cycle doesn't converge after a configurable number of attempts, the agent stops and escalates to a human rather than burning compute on a dead end.
- **Small batches.** Implementation is broken into phases of manageable size, each verified before the next begins. PRs stay digestible and mistakes have limited blast radius.
- **Multi-perspective review.** Specialized reviewers — security, standards, and a deliberately context-free "outsider" reviewer — catch issues that any single reviewer would miss.

These mechanisms compound: by the time a human reviewer sees the PR, the most common failure modes have already been caught. The full chain is visible in the PR timeline.

## Tips for Effective Use

**Write prompts like mini-specs.** State the goal, not just the action. Reference relevant issues, ADRs, or code. Include acceptance criteria. Your prompt is visible to the team — make it useful context.

**Use the plan phase.** For anything beyond a trivial fix, ask egg to plan first. Loop in teammates who own adjacent systems.

**Let the agent fail visibly.** Resist redoing work privately when egg produces a bad result. Post feedback in the issue thread, refine the instructions, let it try again. The failed attempt and correction are valuable signal.

## When To Use It

**Good fit:** Well-defined tasks with clear acceptance criteria. Refactors, new features, bug fixes with known root cause. Parallel task execution across isolated worktrees. Work that benefits from plan review before implementation.

**Interactive CLI mode:** egg also runs as an interactive CLI for rapid prototyping and real-time back-and-forth, where the async workflow's overhead isn't worth it.
