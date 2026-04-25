# SDK capability spike for #1962 advisor strategy

This file records the result of the `claude-agent-sdk` capability spike
that the planner deferred to the implementer per `decision-23` Option C
(spike → fork). It exists so reviewers can verify the implementation
choice (Option A native advisor tool vs. Option B two-call pattern)
without re-running the spike themselves.

## Probe command

```
$ pip show claude-agent-sdk | head -3
$ python3 -c "import claude_agent_sdk; src = open(claude_agent_sdk.__file__).read(); \
    print('advisor_20260301:', 'advisor_20260301' in src); \
    print('max_uses:', 'max_uses' in src)"
```

## Probe output

```
$ pip show claude-agent-sdk | head -3
Name: claude-agent-sdk
Version: 0.1.65
Summary: Python SDK for Claude Code

$ python3 -c "..."
advisor_20260301: False
max_uses: False
```

The vendored SDK (0.1.65, pinned at `sandbox/pyproject.toml` to
`>=0.1.65,<0.2`) does **not** expose `advisor_20260301` or `max_uses`
on the top-level module. Neither symbol appears in the package source.

## Conclusion

**Option B (two-call advisor pattern) will ship in this PR.** The
sandbox-side overseer will invoke `consult_advisor` (which itself calls
`run_agent_async` against the Opus model) through an orchestrator-
exposed MCP tool (`mcp__overseer__consult_advisor`). No reliance on a
native `advisor_20260301` tool — that capability is not exposed by the
pinned SDK version.

If the SDK upgrades within the `>=0.1.65,<0.2` window during
implementation and `advisor_20260301` becomes available, Option A
becomes a clean follow-up swap of the advisor invocation site (no
model-ID change, no protocol change). Nothing in this implementation
precludes the swap; the post-merge SDK-pin-bump follow-up issue
tracked in the PR description handles it.
