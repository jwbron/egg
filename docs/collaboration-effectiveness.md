# Why egg Works: Safety, Quality, and Collaboration

Local AI coding tools create a paradox: they increase individual throughput while fragmenting team knowledge. Engineer A asks an assistant to refactor the auth module. A week later, Engineer B tackles the same area with different prompts, a different approach, and conflicting patterns. Neither knows what the other did or why—the AI conversations that drove both changes vanished when the IDE sessions ended.

egg offers an alternative: a public, async workflow where every interaction happens in shared, observable spaces that the whole team can see. This complements private AI tools rather than replacing them—use whichever mode fits the task.

## How It Works

```
@mention → Plan → Implement → PR → Review → Human merges
```

At each stage, the team has visibility and can intervene: redirect scope at mention, review approach at plan, review code at PR.

**Task assignment is public.** Work starts with an @mention in a GitHub issue or PR comment. Anyone watching the repo sees the request, understands the scope, and can weigh in before the agent starts.

**Plans are reviewable before implementation.** For non-trivial work, egg produces a structured plan describing what it intends to do, which files it will change, and how it will verify correctness. This is the highest-leverage collaboration point: five minutes of plan review saves hours of rework.

**All work is async.** You @mention egg, then walk away. It reads the codebase, writes code, runs tests, and opens a PR. You can assign a task from your phone and review the result later.

**Workflow logs are the audit trail.** Every run produces a full GitHub Actions log: what the agent did, what it tried, what output it got. When a colleague gets a good result, you can read the log to see exactly what prompt and approach worked. This is dramatically more useful than "I asked the AI to do X and it worked."

## What This Workflow Offers

egg adds a public, async option to your team's AI toolkit. Use it when the task benefits from visibility.

| Private AI Tools | egg's Public Workflow |
|---|---|
| Feedback at PR review, after the agent has committed to an approach | Feedback at plan stage, when redirecting costs minutes instead of hours |
| Prompt knowledge stays with individual developers | Shared prompt playbooks emerge organically from visible issue threads |
| Reviewing AI work is a black box—just a diff, no reasoning | Full reasoning visible in workflow logs, alongside the code |
| Developers adopt AI tools individually | Common patterns and standards develop naturally across the team |

When teams adopt the public workflow, knowledge compounds. Someone discovers that explicit acceptance criteria produce better tests. Someone else finds that referencing a specific ADR leads to more consistent architecture. These strategies spread because the artifacts are public, not locked in private chat sessions.

Junior engineers benefit most: every egg interaction is a worked example showing how senior engineers think through problems, write specifications, and review AI output.

## Trust Through Constraints

egg's sandboxed architecture makes the collaborative workflow trustworthy. The key principle: **security through infrastructure, not instructions.** Telling an LLM "don't push to main" can be bypassed. Removing the ability at the network layer cannot.

> **The human-merge invariant:** egg cannot merge pull requests. This isn't a policy—the gateway blocks merge commands at the network layer. Every change to your codebase requires human approval. This is the single strongest answer to "how do we maintain accountability?" and it's enforced by infrastructure, not trust.

| Constraint | Why It Enables Collaboration |
|---|---|
| Agent cannot merge PRs | Reviewers know their approval is the final gate, not a suggestion |
| Agent can only push to `egg/*` branches | No risk of clobbering someone else's work |
| Credentials never enter the sandbox | No credential exposure risk, even in public logs |
| Network isolation in private mode | Confidential code stays confidential with full workflow visibility |
| Gateway enforces all policies | Controls are infrastructure, not instructions |

This shifts the reviewer's job from defensive auditing to quality assessment. Instead of asking "could this agent do something dangerous?", reviewers ask "is this output correct?" That's a better use of engineering time and a more sustainable model as AI-generated code volume increases.

## Keeping Async Work High Quality

The natural concern with async AI work: if nobody is watching, how do you trust the output? egg addresses this with structural mechanisms rather than relying on prompt instructions that agents can ignore or misinterpret.

- **Independent verification.** A separate agent invocation (with its own context window) reviews the implementing agent's work. The implementer cannot mark its own work as complete. This is the async equivalent of pair programming with a guaranteed-independent second opinion.
- **Circuit breakers.** If the implement-review cycle doesn't converge after a configurable number of attempts, the agent stops and escalates to a human rather than burning compute on a dead end.
- **Small batches.** Implementation is broken into phases of manageable size, each verified before the next begins. PRs stay digestible and mistakes have limited blast radius.
- **Multi-perspective review.** Specialized reviewers (security, standards, and a deliberately context-free "outsider" reviewer) catch issues that any single reviewer would miss.

These mechanisms compound: by the time a human reviewer sees the PR, the most common failure modes have already been caught. The full chain is visible in the PR timeline.

## Tips for Effective Use

**Write prompts like mini-specs.** State the goal, not just the action. Reference relevant issues, ADRs, or code. Include acceptance criteria. Your prompt is visible to the team, so make it useful context.

**Use the plan phase.** For anything beyond a trivial fix, ask egg to plan first. Loop in teammates who own adjacent systems.

**Iterate in the open.** When egg produces a suboptimal result, post feedback in the issue thread rather than redoing the work privately. Refine the instructions, let it try again. The iteration history becomes valuable signal for the team.

## When To Use It

**Good fit:** Well-defined tasks with clear acceptance criteria. Refactors, new features, bug fixes with known root cause. Parallel task execution across isolated worktrees. Work that benefits from plan review before implementation.

**When the investment pays off:** Teams with two or more engineers working on the same codebase see the most benefit from the visibility model. Solo developers still gain the safety guarantees and async workflow, but the collaboration benefits compound with team size.

**Interactive CLI mode:** egg also runs as an interactive CLI for rapid prototyping and real-time back-and-forth, where the async workflow's overhead isn't worth it.

## Looking Forward

As AI capabilities expand, organizations face a scaling challenge: more AI-generated code means more code to review, verify, and maintain. egg's constraint-based trust model addresses this by shifting verification earlier (plans before code) and making reasoning visible (logs alongside diffs). The safety model scales without requiring organizations to choose between automation and oversight.
