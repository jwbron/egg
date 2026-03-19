# Stop Telling AI Agents What Not to Do

Most agent safety today comes down to prompt engineering. "Don't merge pull requests." "Don't force push." "Don't expose credentials." These work until they don't.

Prompts are suggestions weighted against everything else in the context window. Under context pressure, long conversations, or prompt injection, an agent told "never merge" can still decide merging is the right call. Writing more emphatic prompts doesn't fix this. The agent still *can* merge. It just *shouldn't*.

## Remove the capability instead

[egg](https://github.com/jwbron/egg) enforces this at the infrastructure level. Instead of rules the agent might forget, the environment simply doesn't have the dangerous action.

Every git and GitHub operation routes through a gateway sidecar. The agent uses `git` and `gh` normally, with no special APIs. Shell wrappers intercept each command and forward it to the gateway, which decides whether the operation is allowed.

Two examples:

**Merging doesn't exist.** When an agent runs `gh pr merge`, the wrapper catches it before it reaches the network. The agent sees:

```
================================================================================
  MERGE OPERATIONS NOT SUPPORTED
================================================================================

The gateway sidecar does not support merge operations.

Human must merge PRs via the GitHub web interface.

This is a safety measure to ensure human review before merging.

================================================================================
```

There is no code path, no flag, no escalation that results in a merge. The capability is absent from the agent's universe. No amount of context pressure, creative prompting, or prompt injection can produce a merge, because the system has no merge endpoint to call.

**Credentials don't exist in the sandbox.** The agent environment has zero GitHub tokens, zero API keys. The gateway injects credentials at the moment of each operation and strips them after. The agent cannot leak what it cannot see. This isn't a rule saying "don't print your token." There is no token to print.

The same pattern applies across the system. Agents can only push to `egg/`-prefixed branches. An agent in the "plan" phase physically cannot push code. An agent in "implement" cannot modify the contract. These aren't policies the agent has been told about. They're walls it runs into if it tries.

## Guidance at the point of failure

Agents adapt immediately when operations fail, because the error messages tell them exactly what to do. No rules need to be front-loaded into the system prompt.

When a push is blocked:

```
================================================================================
  PUSH BLOCKED BY POLICY
================================================================================

The gateway sidecar blocked this push operation.

To push, ensure one of the following:
  1. Use a bot-prefixed branch name: egg-* or egg/*
  2. Create a PR from this branch first (the branch will then be owned by egg)

================================================================================
```

The agent doesn't need to memorize branch naming conventions upfront. It discovers them at the moment they matter, with specific instructions on how to comply. The feedback is contextual, actionable, and impossible to ignore because the operation already failed.

This turns out to be more effective than front-loading rules into the system prompt. The agent doesn't waste context on rules it may never need. And when it does hit a boundary, the error is right there in the conversation, not buried in a system prompt the model wrote off 50,000 tokens ago.

The same pattern applies to self-reviews. See "Silent adaptation" below.

## Errors as guidance

The error messages aren't just rejections. They're designed to be useful to an LLM.

Every blocked operation explains why it was blocked and lists the specific options for moving forward. The agent doesn't need to search documentation or guess at the right fix. The answer is right there in the output, structured so the agent can act on it in the next turn.

This creates a spectrum of enforcement:

**Hard walls.** The capability doesn't exist. Merge has no endpoint. Credentials aren't in the environment. No error message needed because there's nothing to attempt.

**Clear redirection.** The agent tries something, it fails, and the error tells it exactly how to succeed. Push to a non-prefixed branch? The error lists the naming convention. Try to commit files outside your phase? The error names the restricted paths. The agent course-corrects immediately because the feedback is specific and actionable.

**Silent adaptation.** The agent doesn't even know something went wrong. Self-review is the best example: GitHub blocks bots from approving their own PRs, so the wrapper detects it, downgrades the review to a comment, and preserves the original verdict in a hidden HTML marker. The agent's intent is honored. No error, no retry, no wasted tokens.

The gateway isn't just a bouncer. It's a teacher that meets the agent where it is.

## The principle

**Constraints belong in infrastructure, not in prompts.**

Prompts degrade under context pressure. Infrastructure doesn't. An agent two hours and 200,000 tokens into a task will hit the same gateway policy it would have hit in the first minute. The rules don't get pushed out by newer context. They don't get reinterpreted. They don't get forgotten.

This also makes prompt injection a non-issue for the operations that matter most. A malicious instruction in a GitHub issue can manipulate what an agent *thinks* it should do, but it can't grant capabilities the environment doesn't have. An injected "merge this PR immediately" hits the same wall as a legitimate attempt. The system is zero-trust by design: the agent is never the authority on what it's allowed to do.

If you find yourself writing more emphatic system prompts to stop an agent from doing something, consider whether you can remove the capability instead. It's the difference between a sign that says "don't open this door" and a wall.

---

egg is open source. [GitHub repo](https://github.com/jwbron/egg) | [Architecture overview](../architecture/README.md) | [Gateway details](../../gateway/README.md)
