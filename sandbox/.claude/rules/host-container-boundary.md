# Host-Container Security Boundary

## Critical Rule: NO Claude/Anthropic API Calls from Host Services

**Host services MUST NOT call Claude or the Anthropic API directly.**

All Claude/LLM operations must go through the jib container using the `jib_exec` mechanism.

## Why This Matters

The security model relies on a strict boundary between:
1. **Host services** - Run on the host machine with access to credentials, systemd, etc.
2. **Container (jib)** - Sandboxed environment where Claude runs with limited access

If host services call Claude directly:
- Claude has access to host-level credentials (ANTHROPIC_API_KEY, GITHUB_TOKEN, etc.)
- Prompt injection attacks could potentially escalate to host-level access
- The security boundary is violated

## The Correct Pattern

**WRONG** - Direct Claude/Anthropic call from host:
```python
# host-services/slack/slack-receiver/message_categorizer.py
import anthropic  # DO NOT DO THIS

client = anthropic.Anthropic()
response = client.messages.create(...)  # SECURITY VIOLATION
```

**CORRECT** - Delegate to container via jib_exec:
```python
# host-services/something/handler.py
from jib_exec import jib_exec

# Let the container handle Claude interactions
result = jib_exec(
    processor="some-processor",
    task_type="categorize_message",
    context={"message": text},
    timeout=60,
)
```

## What Can Host Services Do?

Host services CAN:
- Parse and route messages
- Execute shell commands and systemd operations
- Make HTTP requests to non-Claude APIs
- Read/write files on the host
- Call `jib --exec` to delegate work to the container

Host services CANNOT:
- Import `anthropic` or any Claude SDK
- Call Claude API directly
- Import modules from `shared/claude/` (container-only)

## Enforcing This Rule

A lint check in `.github/workflows/` or pre-commit hook should verify:
1. No `import anthropic` in `host-services/`
2. No `from anthropic` in `host-services/`
3. No `anthropic` in `host-services/pyproject.toml` dependencies

## See Also

- `environment.md` - Container capabilities and limitations
- `host-services/shared/jib_exec.py` - How to delegate to container
- PR #387 - Example of what NOT to do (reverted)
