# Agent Development Guide

This guide explains how to add new specialized agents to the multi-agent orchestration system.

## Overview

The multi-agent system runs specialized agents concurrently across all pipeline phases (refine, plan, implement, and review) via BRC consensus. Each agent has:

- **Role**: A unique identifier and description
- **Category**: One of EXECUTION, ANALYSIS, REVIEW, UTILITY, or INTERFACE (see [Agent Roles Reference](../reference/agent-roles.md))
- **Responsibilities**: Specific tasks the agent performs
- **Dependencies**: Which agents must complete first
- **File access patterns**: What files the agent can read/write

## Agent Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Orchestrator                             │
│  Reads contract, dispatches agents, manages state           │
└─────────────────────────────────────────────────────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                    ▼
    ┌─────────┐         ┌──────────┐        ┌───────────┐
    │  Coder  │         │  Tester  │        │Documenter │
    └─────────┘         └──────────┘        └───────────┘
```

## Adding a New Agent

### Step 1: Define the Role

Add the role to `shared/egg_contracts/agent_roles.py`:

```python
class AgentRole(StrEnum):
    CODER = "coder"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    # Utility roles
    AUTOFIXER = "autofixer"
    CONFLICT_RESOLVER = "conflict_resolver"
    MY_NEW_AGENT = "my_new_agent"  # Add your role
```

### Step 2: Create Role Definition

Define the agent's responsibilities, constraints, and **category**:

```python
from egg_contracts.agent_roles import AgentCategory

MY_NEW_AGENT_ROLE = AgentRoleDefinition(
    role=AgentRole.MY_NEW_AGENT,
    description="Brief description of what this agent does",
    responsibilities=[
        "First responsibility",
        "Second responsibility",
        "Third responsibility",
    ],
    dependencies=[AgentRole.CODER],  # Which agents must complete first
    category=AgentCategory.UTILITY,  # EXECUTION, ANALYSIS, REVIEW, UTILITY, or INTERFACE
    file_access=FileAccessPattern(
        allowed_read=[],  # Empty = can read all files
        allowed_write=[
            "path/to/allowed/",
            "**/*.specific_extension",
        ],
        blocked_write=[
            "path/to/blocked/",
        ],
    ),
    can_run_in_parallel=True,  # Can run alongside other agents?
    produces_outputs=["output_key"],  # What handoff data it produces
    requires_inputs=["changed_files"],  # What handoff data it needs
)
```

**Category assignment guidelines:**
- **EXECUTION**: Agents that produce primary artifacts (code, tests, docs)
- **ANALYSIS**: Agents that analyze tasks and plan work (no code output)
- **REVIEW**: Agents that validate quality and produce verdicts
- **UTILITY**: Cross-cutting support agents (auto-fixes, conflict resolution)
- **INTERFACE**: Monitoring and health-check agents

### Step 3: Register the Role

Add it to the `AGENT_ROLES` registry:

```python
AGENT_ROLES: dict[AgentRole, AgentRoleDefinition] = {
    AgentRole.CODER: CODER_ROLE,
    AgentRole.TESTER: TESTER_ROLE,
    AgentRole.DOCUMENTER: DOCUMENTER_ROLE,
    AgentRole.MY_NEW_AGENT: MY_NEW_AGENT_ROLE,
}
```

### Step 4: Create Prompt Builder

Create `action/build-my-new-agent-prompt.sh`:

```bash
#!/usr/bin/env bash
# build-my-new-agent-prompt.sh — Build a focused prompt for My New Agent

set -euo pipefail

# ... (see existing prompt builders for template)

build_my_new_agent_prompt() {
    # Use quoted heredocs for static content
    cat <<'EOF'
You are the **My New Agent** agent in a multi-agent SDLC pipeline.

## Your Role
...
EOF

    # Use printf for dynamic content
    printf 'Repository: %s\n' "${GITHUB_REPOSITORY}"
}

# Main
prompt=$(build_my_new_agent_prompt)
# ... output handling
```

### Step 5: Create Mode File

Create `sandbox/agent-config/commands/my-new-agent-mode.md`:

```markdown
# My New Agent Mode

You are the **My New Agent** agent in a multi-agent SDLC pipeline.

## Role Summary
- **Primary responsibility**: ...
- **Runs when**: After X completes
- **Outputs**: ...

## File Access Constraints
...

## Workflow
...

## Handoff Output
...
```

### Step 6: Add Gateway Restrictions

Update `gateway/agent_restrictions.py` to enforce file access:

```python
AGENT_FILE_RESTRICTIONS = {
    "my_new_agent": {
        "allowed_write": [
            "path/to/allowed/",
        ],
        "blocked_write": [
            "**/*",  # Block everything not explicitly allowed
        ],
    },
}
```

### Step 7: Register with Orchestrator

Register the agent with the concurrent execution system:

1. Add the role to `shared/egg_contracts/agent_roles.py`
2. Configure file access patterns and phase assignments
3. Configure container spawning for the new agent role

### Step 8: Add Tests

Create tests in `tests/`:

```python
def test_my_new_agent_role_defined():
    """Verify the role is properly defined."""
    role = get_role_definition(AgentRole.MY_NEW_AGENT)
    assert role.description
    assert role.responsibilities

def test_my_new_agent_file_access():
    """Verify file access patterns work correctly."""
    role = get_role_definition(AgentRole.MY_NEW_AGENT)
    assert role.file_access.can_write("path/to/allowed/file.txt")
    assert not role.file_access.can_write("path/to/blocked/file.txt")
```

## File Access Patterns

The `FileAccessPattern` class supports:

| Pattern | Matches |
|---------|---------|
| `foo/` | Any file under `foo/` directory |
| `*.py` | Python files in root only |
| `**/*.py` | Python files at any depth |
| `foo/bar.py` | Exact file match |

Pattern evaluation order:
1. Check `blocked_write` patterns first
2. If blocked, check `block_exempt_patterns` — if the path matches an exemption, continue; otherwise deny access
3. Check `allowed_write` patterns
4. If no allowed patterns match, deny access

`block_exempt_patterns` carve out narrow exceptions from blocked patterns. For example, `**/*.md` is blocked for coders (documentation), but `.md` files in `sandbox/agent-config/` and `skills/` are functional code and exempted. Exempt paths must also appear in `allowed_patterns` — the exemption only bypasses the block check, it does not grant write access on its own.

## Push Enforcement and Cross-Role Pushes

As of [#2039](https://github.com/jwbron/egg/issues/2039), the gateway rejects any push whose own-authored files include a path outside the pushing role's allowed patterns. The handler attributes each commit in the unpushed range via the commit-authorship registry, partitions files into own-authored vs pulled-from-other-role, and checks the pushing role's write permissions against only the own-authored set.

See [Gateway Auto-Filter Architecture](../architecture/gateway-auto-filter.md) for the historical auto-filter design and the commit-authorship registry that still backs attribution.

### Push outcomes

When `POST /api/v1/git/push` encounters files outside the pushing role's allowed patterns:

| Own-file state | Pulled commits in range | Outcome | Response fields |
|----------------|-------------------------|---------|-----------------|
| All allowed | None | Plain push | `pushed_files`, `pulled_commits: []` |
| All allowed | Some | Plain push; pulled commits exempt | `pushed_files`, `pulled_commits: [{sha, author_role}]` |
| Any own-file blocked | Any | **Rejected** `403 restricted_path_modified` | `role`, `blocked_paths`, `recommended_action`, `doc_ref`, `pulled_commits`, `attribution_fallback` |

Phase / anchor / protected-file / branch-ownership / private-mode / concurrent-mode checks also return `403`.

### Recovery from `403 restricted_path_modified`

Drop the offending edits and re-propose with `--pre-merge-condition` per the conditional-ACK pattern ([#1998](https://github.com/jwbron/egg/issues/1998)). Pulled cross-role commits (attributed to another role via the registry) never block the push.

### Kill switch and audit

- `EGG_AGENT_RESTRICTIONS_ENFORCE=false` falls back to warn-only plain push.
- The audit event `push_denied_restricted_path_modified` fires on every rejection; `attribution_fallback: true` indicates the commit walk was unavailable and every file was treated as own-authored.

The client-side `--scope-filter` flag was removed in [#1882](https://github.com/jwbron/egg/issues/1882). See `sandbox/agent-config/rules/push-recovery.md` for the runtime rule set.

## Handoff Data

Agents communicate via JSON files in `.egg-state/agent-outputs/`, namespaced
by the issue number or pipeline ID to prevent merge conflicts:

```
.egg-state/agent-outputs/
├── {identifier}-coder-output.json
├── {identifier}-tester-output.json
└── {identifier}-documenter-output.json
```

For example, issue #871 produces `871-coder-output.json`, `871-tester-output.json`, etc.

**Backward compatibility:** `load_agent_output()` checks the namespaced path first, then
falls back to the old `{role}-output.json` path. This ensures in-flight pipelines created
before the namespacing change continue to work.

Standard handoff format:

```json
{
  "changed_files": ["list", "of", "files"],
  "commits": ["abc123", "def456"],
  "summary": "Brief description of what was done",
  "custom_field": "Agent-specific data"
}
```

## Dependency Graph

The orchestrator builds a dependency graph from role definitions:

```python
from egg_contracts.dependency_graph import compute_execution_plan

plan = compute_execution_plan([
    AgentRole.CODER,
    AgentRole.TESTER,
    AgentRole.DOCUMENTER,
    AgentRole.MY_NEW_AGENT,
])

# Returns execution waves:
# Wave 1: [CODER]
# Wave 2: [TESTER, DOCUMENTER, MY_NEW_AGENT]  # Parallel
```

## Testing Your Agent

1. **Unit tests**: Test role definition and file access
2. **Integration tests**: Test handoff reading/writing
3. **Workflow tests**: Test the full dispatch cycle

Run tests:

```bash
pytest tests/sdlc/test_concurrent_integration.py -v
pytest tests/gateway/test_agent_restrictions.py -v
```

## Prompt Context Scoping

Agent prompts are built with role-appropriate context via `_build_role_context()` in `orchestrator/routes/pipelines.py`. When adding a new agent, understand how context is scoped by category:

**Analysis roles** (architect, task_planner, risk_analyst) receive the full issue body in a `## Task Description` section. They need complete context for problem analysis and planning.

**Execution roles** (coder, tester, documenter) receive:
- A `## Background` section with a 1-2 sentence summary extracted from the issue
- A `## For More Context` section with pointers to the full issue (`gh issue view`), handoff data, and git diff
- **Role-filtered tasks**: During the implement phase, each agent only sees tasks assigned to its role via the `task.role` field. The coder also receives any unassigned tasks as a fallback. See [Agent Roles Reference](../reference/agent-roles.md#role-aware-task-assignment).

**Utility roles** (autofixer, conflict_resolver) receive targeted context specific to their task (e.g., lint output, conflict details, list of affected files).

**Interface role** (overseer) receives pipeline state, health alerts, and agent logs.

When adding a new execution role, `_build_role_context()` will automatically provide the summarized context and filter tasks by role. If the role needs phase-specific instructions (like the tester's "Focus your testing on..." or the documenter's "Focus your documentation on..."), add a condition in `_build_role_context()` for the new role.

## Best Practices

1. **Keep agents focused**: One clear responsibility per agent
2. **Minimize dependencies**: Fewer dependencies = more parallelism
3. **Use explicit file patterns**: Be specific about what files are allowed
4. **Write clear handoffs**: Other agents depend on your output
5. **Handle errors gracefully**: Write meaningful error info to handoff
6. **Test file restrictions**: Verify the gateway enforces your patterns

## Troubleshooting

### Agent not running

- Check dependencies are complete
- Verify agent is in `AGENT_ROLES` registry
- Check workflow dispatches the agent

### File access denied

- Review `allowed_write` patterns
- Check pattern order (blocked checked first)
- Verify gateway restrictions match role definition

### Handoff not found

- Ensure previous agent wrote handoff file
- Check file path matches expected location
- Verify JSON is valid
