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

### Why async work stays high quality

The natural concern with async AI work is: if nobody is watching, how do you trust the output? Prompt instructions alone aren't enough — agents can run out of context mid-task, mark their own work as done without real verification, or spin on a broken approach indefinitely. egg addresses this with structural mechanisms, not behavioral ones.

**Independent verification** ([#133](https://github.com/jwbron/egg/issues/133)). A separate agent invocation — with its own context window and instructions — reviews the implementing agent's work. The reviewer can't be influenced by the implementer's reasoning because it literally doesn't share a context. The implementing agent cannot mark its own work as complete; only the review agent can flip that flag. This is the async equivalent of pair programming, but with a guaranteed-independent second opinion.

**Circuit breakers prevent wasted effort.** If the implement-review cycle doesn't converge after a configurable number of attempts, the agent stops, documents what's blocking it, and escalates to a human. This prevents the failure mode where an unsupervised agent burns hours of compute on an approach that won't work — a real risk with async execution where nobody is watching in real time.

**Small batches keep work reviewable.** Implementation is broken into phases of ~1-2k lines of change, each independently verified before the next begins. This means the PR that lands on a reviewer's desk is digestible, and any mistake has limited blast radius. It also means the agent's context stays fresh — no single invocation has to hold an entire large feature in memory.

**Multi-perspective automated review** ([#134](https://github.com/jwbron/egg/issues/134)). Beyond the implementation-verification loop, specialized review bots catch issues that a single reviewer — human or AI — would miss. A security reviewer flags vulnerabilities. A standards enforcer checks adherence to team ADRs. Most notably, a *bounded-context reviewer* deliberately operates without internal project knowledge, catching assumptions and documentation gaps from an outsider's perspective. When you can't read the agent's docs and the code still makes sense, that's a signal it will make sense to the next engineer who encounters it.

These mechanisms compound. The implementing agent writes code. An independent agent verifies it. Specialized reviewers check it from multiple angles. The entire chain is visible in the PR timeline. By the time a human reviewer sees the PR, the most common failure modes — incomplete implementation, standard violations, undocumented assumptions — have already been caught.

## When egg Is (and Isn't) a Good Fit

**Good fit:**
- Well-defined tasks with clear acceptance criteria
- Work that benefits from plan review (refactors, new features, bug fixes with known root cause)
- Teams that want AI work to be reviewable and auditable
- Parallel task execution — multiple issues worked simultaneously in isolated worktrees

**Better in CLI mode** (egg also runs as an interactive CLI, not just async via GitHub Actions):
- Rapid exploratory prototyping where you want tight feedback loops
- Tasks requiring real-time back-and-forth conversation

**Less ideal:**
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
