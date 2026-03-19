# Stop Telling AI Agents What Not to Do

Every AI coding agent ships with some version of the same instructions: "Don't merge pull requests." "Don't force push." "Don't expose credentials."

These work most of the time. Then they don't. Under context pressure, long conversations, or unexpected edge cases, agents ignore prompt-based rules. Not because they're broken, but because a prompt is a suggestion weighted against everything else in the context window. An agent told "never merge" can still decide merging is the right call if the surrounding context is persuasive enough.

The standard fix is better prompts. More emphatic, more specific, more redundant. But this doesn't solve the underlying problem: the agent still *can* merge. It just *shouldn't*.

## What we did instead

[egg](https://github.com/jwbron/egg) removes the capability entirely. Instead of rules the agent might forget, we built an environment where the dangerous action doesn't exist.

Every git and GitHub operation routes through a gateway sidecar. The agent uses `git` and `gh` normally, with no special APIs. Shell wrappers intercept each command and forward it to the gateway, which decides whether the operation is allowed.

Two examples:

**Merging doesn't exist.** When an agent runs `gh pr merge`, the wrapper catches it before it reaches the network. The agent sees:

```
================================================================================
  MERGE OPERATIONS NOT SUPPORTED
================================================================================

Human must merge PRs via the GitHub web interface.

This is a safety measure to ensure human review before merging.
================================================================================
```

There is no code path, no flag, no escalation that results in a merge. The capability is absent from the agent's universe. No amount of context pressure or creative prompting can produce a merge, because the system has no merge endpoint to call.

**Credentials don't exist in the sandbox.** The agent environment has zero GitHub tokens, zero API keys. The gateway injects credentials at the moment of each operation and strips them after. The agent cannot leak what it cannot see. This isn't a rule saying "don't print your token." There is no token to print.

The same pattern applies across the system. Agents can only push to `egg/`-prefixed branches. An agent in the "plan" phase physically cannot push code. An agent in "implement" cannot modify the contract. These aren't policies the agent has been told about. They're walls it runs into if it tries.

## Guidance at the point of failure

Here's the part that surprised us. We expected agents to get confused when operations failed. Instead, they adapted immediately, because the error messages tell them exactly what to do.

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

We see the same pattern with self-reviews. GitHub blocks bots from approving their own PRs. Rather than telling the agent about this edge case, the wrapper detects it, automatically downgrades the review to a comment, and preserves the original verdict in a hidden HTML marker. The agent doesn't know the workaround happened. It just works.

## Why wrappers, not MCP

A natural question: why not use MCP to expose constrained tool definitions?

MCP front-loads tool schemas and descriptions into context before the agent does anything. For a proprietary API the agent has never seen, that cost is justified. Without it, the agent is blind.

But agents already know `git` and `gh`. The training data *is* the documentation. Re-describing `git push` as an MCP tool wastes context and adds indirection to something the agent can already do natively.

Wrappers let agents use familiar tools in a familiar way. When an operation is denied, the agent gets a clear error at the exact moment it matters. MCP is the right choice for tools agents need to learn. Wrappers are the right choice for tools they already know.

## The principle

**Constraints belong in infrastructure, not in prompts.**

Prompts degrade under context pressure. Infrastructure doesn't. An agent two hours and 200,000 tokens into a task will hit the same gateway policy it would have hit in the first minute. The rules don't get pushed out by newer context. They don't get reinterpreted. They don't get forgotten.

If you find yourself writing more emphatic system prompts to stop an agent from doing something, consider whether you can remove the capability instead. It's the difference between a sign that says "don't open this door" and a wall.

---

egg is open source. [GitHub repo](https://github.com/jwbron/egg) | [Architecture overview](../architecture/README.md) | [Gateway details](../../gateway/README.md)
