# How egg Makes AI-Assisted Development Collaborative

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

**Plans are reviewable before implementation.** For non-trivial work, egg produces a structured plan — posted as a comment or committed as a contract file — describing what it intends to do, which files it will change, and how it will verify correctness. This is the highest-leverage collaboration point: 5 minutes of plan review saves hours of rework.

**All work is async.** You @mention egg, then walk away. It reads the codebase, writes code, runs tests, and reports back with a summary and a link to the workflow logs. You can assign a task from your phone and review the result later.

**Workflow logs are the audit trail.** Every run produces a full GitHub Actions log — what the agent did, what it tried, what output it got. When a colleague gets a great result, you can read the log to see exactly what prompt and approach worked. This is dramatically more useful than "I asked the AI to do X and it worked."

## What Changes for the Team

### Everyone gets earlier input

Without visibility, feedback comes at PR review — after the agent has already committed to an approach. With egg, the pattern shifts:

1. Engineer A opens an issue and @mentions egg to plan a solution.
2. Engineer B sees the plan and suggests a different approach.
3. Engineer A refines the instructions.
4. egg implements the agreed-upon approach.

### Prompt strategies become shared knowledge

When prompts and results are visible in issue threads, the team develops a shared understanding of what works. Someone discovers that explicit acceptance criteria produce better tests. Someone else finds that referencing a specific ADR leads to more consistent architecture. These strategies spread organically because the artifacts are public.

### Reviewing AI work becomes more informed

When reviewing an egg-generated PR, clicking through to the workflow run often reveals issues the diff alone doesn't — the agent might have misunderstood a requirement and produced technically correct but functionally wrong code. The reasoning is available, not just the result.

### Roles shift, not shrink

- **Developers** spend more time on task specification and plan review, less on implementation of well-defined work.
- **Reviewers** review with full context (logs, plan, reasoning), not just a diff.
- **Tech leads** shape work through issue descriptions and plan feedback rather than pairing sessions.
- **Junior engineers** learn from visible AI reasoning chains and review feedback — every egg interaction is a worked example.

## Trust Through Constraints

egg's sandboxed architecture is what makes the collaborative workflow trustworthy. The key principle: **security through infrastructure, not instructions.** Telling an LLM "don't push to main" can be bypassed. Removing the ability at the network layer cannot.

| Constraint | Why It Enables Collaboration |
|---|---|
| Agent cannot merge PRs | Reviewers know their approval is the final gate, not a suggestion |
| Agent can only push to `egg/*` branches | No risk of clobbering someone else's work on shared branches |
| Credentials never enter the sandbox | No credential exposure risk in public logs |
| Network isolation in private mode | Confidential code stays confidential even with full workflow visibility |
| Gateway enforces all policies | Controls can't be bypassed by prompt injection — they're infrastructure |

### Security by design reduces developer burden

Traditional AI coding tools leave safety as a human responsibility — developers must audit every interaction for credential leaks, unintended side effects, and scope violations. egg's infrastructure-enforced constraints ([security architecture](https://github.com/jwbron/james-in-a-box/pull/659)) eliminate entire categories of risk by design:

- **Credentials never enter the sandbox**, so developers don't need to worry about the agent leaking tokens in commits, logs, or PR descriptions. There's nothing to leak.
- **The agent structurally cannot merge code**, so reviewers can focus on correctness ("is this the right approach?") rather than safety ("will this accidentally ship?").
- **Network isolation prevents data exfiltration** — even if the agent is influenced by prompt injection from a malicious issue or ticket, it can't send data anywhere unauthorized.
- **Branch ownership is enforced at the gateway**, so there's no risk of the agent overwriting someone else's work, regardless of what instructions it receives.

This shifts the reviewer's job from defensive auditing to quality assessment. You stop asking "could this agent do something dangerous?" and start asking "is this output correct?" — which is a better use of engineering time and a more sustainable model as AI-generated code volume increases.

### Structured verification

A separate agent invocation — with its own context window — reviews the implementing agent's work ([#133](https://github.com/jwbron/egg/issues/133)). The implementing agent cannot mark its own work as complete. If the review cycle doesn't converge, the agent stops and escalates to a human rather than burning tokens. Work is broken into phases of ~1-2k lines, keeping PRs digestible for human reviewers.

## When egg Is (and Isn't) a Good Fit

**Good fit:**
- Well-defined tasks with clear acceptance criteria
- Work that benefits from plan review (refactors, new features, bug fixes with known root cause)
- Teams that want AI work to be reviewable and auditable
- Parallel task execution — multiple issues worked simultaneously in isolated worktrees

**Less ideal:**
- Rapid exploratory prototyping where you want tight feedback loops in an IDE
- Tasks requiring real-time back-and-forth (use a conversational AI tool instead)
- Trivial one-line fixes where the overhead of plan → implement → PR isn't worth it
- Work requiring access to external services not available in the sandbox

## Tips for Effective Use

**Write prompts like mini-specs.** State the goal, not just the action. Reference relevant issues, ADRs, or code. Include acceptance criteria. Your prompt is visible to the team — make it useful context for them too.

**Use the plan phase.** For anything beyond a trivial fix, ask egg to plan first. Loop in teammates who own adjacent systems. Get alignment before code is written.

**Let the agent fail visibly.** Resist redoing work privately when egg produces a bad result. Post feedback in the issue thread, refine the instructions, let it try again. The failed attempt and correction are valuable signal for the team.

**Share what works.** When you get a good result, share the prompt and link to the issue or workflow run. Over time, this builds a shared playbook.

## Roadmap

| Feature | Issue | What it adds |
|---|---|---|
| Multi-agent SDLC pipeline | [#133](https://github.com/jwbron/egg/issues/133) | Full issue-to-PR pipeline with independent verification at every stage |
| AI code review bots | [#134](https://github.com/jwbron/egg/issues/134) | Automated reviewers posting inline PR comments (security, standards, outsider perspective) |
| CI autofixing | [#138](https://github.com/jwbron/egg/issues/138) | Automatic diagnosis and repair of CI failures on agent-owned PRs |
| Dedicated agent accounts | [#132](https://github.com/jwbron/egg/issues/132) | Safe @mentionable GitHub identities for agent invocation |
