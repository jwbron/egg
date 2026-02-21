# Agent Development Guide

This guide explains how to add new specialized agents to the multi-agent orchestration system.

## Overview

The multi-agent system breaks down the implement phase into specialized agents that run in parallel where dependencies allow. Each agent has:

- **Role**: A unique identifier and description
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
    │ (wave 1)│         │ (wave 2) │        │ (wave 2)  │
    └─────────┘         └──────────┘        └───────────┘
                              │
                              ▼
                       ┌────────────┐
                       │ Integrator │
                       │  (wave 3)  │
                       └────────────┘
```

## Adding a New Agent

### Step 1: Define the Role

Add the role to `shared/egg_contracts/agent_roles.py`:

```python
class AgentRole(StrEnum):
    CODER = "coder"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    INTEGRATOR = "integrator"
    MY_NEW_AGENT = "my_new_agent"  # Add your role
```

### Step 2: Create Role Definition

Define the agent's responsibilities and constraints:

```python
MY_NEW_AGENT_ROLE = AgentRoleDefinition(
    role=AgentRole.MY_NEW_AGENT,
    description="Brief description of what this agent does",
    responsibilities=[
        "First responsibility",
        "Second responsibility",
        "Third responsibility",
    ],
    dependencies=[AgentRole.CODER],  # Which agents must complete first
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

### Step 3: Register the Role

Add it to the `AGENT_ROLES` registry:

```python
AGENT_ROLES: dict[AgentRole, AgentRoleDefinition] = {
    AgentRole.CODER: CODER_ROLE,
    AgentRole.TESTER: TESTER_ROLE,
    AgentRole.DOCUMENTER: DOCUMENTER_ROLE,
    AgentRole.INTEGRATOR: INTEGRATOR_ROLE,
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

Create `sandbox/.claude/commands/my-new-agent-mode.md`:

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

Register the agent with the local orchestrator's multi-agent system in `orchestrator/multi_agent.py`:

1. Add to the dispatch logic
2. Add to the parallel groups configuration
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
2. If blocked, deny access
3. Check `allowed_write` patterns
4. If no allowed patterns match, deny access

## Handoff Data

Agents communicate via JSON files in `.egg-state/agent-outputs/`:

```
.egg-state/agent-outputs/
├── coder-output.json
├── tester-output.json
├── documenter-output.json
└── integrator-output.json
```

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
    AgentRole.INTEGRATOR,
])

# Returns execution waves:
# Wave 1: [CODER]
# Wave 2: [TESTER, DOCUMENTER, MY_NEW_AGENT]  # Parallel
# Wave 3: [INTEGRATOR]
```

## Testing Your Agent

1. **Unit tests**: Test role definition and file access
2. **Integration tests**: Test handoff reading/writing
3. **Workflow tests**: Test the full dispatch cycle

Run tests:

```bash
pytest tests/sdlc/test_multi_agent_orchestration.py -v
pytest tests/gateway/test_agent_restrictions.py -v
```

## Prompt Context Scoping

Agent prompts are built with role-appropriate context via `_build_role_context()` in `orchestrator/routes/pipelines.py`. When adding a new agent, understand how context is scoped:

**Analysis roles** (architect, task_planner, risk_analyst) receive the full issue body in a `## Task Description` section. They need complete context for problem analysis and planning.

**Execution roles** (tester, documenter, integrator, and any new execution agents) receive:
- A `## Background` section with a 1-2 sentence summary extracted from the issue
- A `## Phase Scope` section with task details when running in Tier 3 (descriptions, acceptance criteria, affected files)
- A `## For More Context` section with pointers to the full issue (`gh issue view`), handoff data, and git diff

When adding a new execution role, `_build_role_context()` will automatically provide the summarized context. If the role needs phase-specific instructions (like the tester's "Focus your testing on..." or the documenter's "Focus your documentation on..."), add a condition in `_build_role_context()` for the new role.

Phase-scoped coders (Tier 3) use `_build_phase_scoped_prompt()`, which embeds the plan overview (not the full plan) and one-line summaries of other phases. This is separate from `_build_role_context()`.

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
