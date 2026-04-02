# Role-Scope Enforcement Guide

How egg prevents agents from working outside their role boundaries, and how agents recover when gateway push validation rejects out-of-scope files.

## Problem

In multi-agent pipelines, each agent has a defined file scope (e.g., the tester writes tests, the documenter writes docs). Without enforcement, agents can spend significant context window writing files they're not allowed to push — then get stuck in retry loops when the gateway rejects the push.

This guide documents the defense-in-depth architecture that prevents this wasted effort and ensures graceful recovery.

## Defense-in-Depth: Three Enforcement Layers

Agent file scope is enforced at three layers, each catching violations progressively earlier:

```
Agent writes file
       │
       ▼
┌──────────────────────────────┐
│  1. SDK Tool Interception    │  ← Earliest: rejects Write/Edit before execution
│     (soft enforcement)       │     Saves tokens, drives delegation to correct agent
└──────────┬───────────────────┘
           │ (Bash writes bypass this layer)
           ▼
┌──────────────────────────────┐
│  2. Commit-Time Validation   │  ← Catches violations before commit is created
│     (gateway, pipeline only) │     Agent can unstage and fix immediately
└──────────┬───────────────────┘
           │
           ▼
┌──────────────────────────────┐
│  3. Push-Time Validation     │  ← Hard security boundary
│     (gateway enforcement)    │     Blocks the push with actionable remediation steps
└──────────────────────────────┘
```

### Layer 1: SDK Tool Interception (Soft)

The Agent SDK (`egg_agent`) intercepts file write tool calls (`Write`, `Edit`, `NotebookEdit`) and checks them against the role's `AgentFilePattern` **before execution**. If the file is outside the agent's scope, the tool returns an error immediately.

**What it catches:** All file writes through Claude's built-in tools.

**What it misses:** File writes through `Bash` commands (e.g., `echo > file.py`, `cat > file.py`). Reliably parsing file writes from shell commands is impractical, so Bash is not intercepted.

**Availability:** Only active in the headless Agent SDK (`egg_agent`). The interactive `claude` CLI is not affected.

**Source:** `shared/egg_agent/tool_interceptor.py`

### Layer 2: Commit-Time Validation (Pipeline Only)

For pipeline sessions, the gateway validates staged files at `git commit` time against role-based file restrictions. This catches violations that slipped through Layer 1 (e.g., via Bash).

**How it works:**
1. Agent runs `git commit`
2. Gateway inspects `git diff --cached --name-only` for staged files
3. Staged files are checked against blocked patterns for the agent's role
4. If violations are found, the commit is rejected with guidance to unstage the blocked files

**Error message:**
```
Commit blocked: <phase restriction message>. Unstage the blocked files with 'git reset HEAD <file>'.
```

**Defense-in-depth:** This is a complement to push-time validation, not a replacement. If commit-time validation encounters an error, it fails open — push-time validation remains the authoritative gate.

### Layer 3: Push-Time Validation (Hard Enforcement)

The gateway enforces role-based file restrictions at push time. This is the authoritative security boundary — no files outside the agent's scope can reach the remote.

**How it works:**
1. Agent runs `git push`
2. Gateway calls `get_changed_files_in_push()` to identify modified files
3. Each file is checked against the agent's `AgentFilePattern` (allowed and blocked patterns)
4. If any file violates the pattern, the push is rejected with HTTP 403

**Scoped file detection:** With per-agent worktree isolation, the gateway only reports files from the current agent's commits. This eliminates false positives from other agents' commits on the same branch.

**Error message (enriched):**
```
Push denied: agent role '<role>' cannot modify: <file1>, <file2>.
Remediation: Run 'git reset HEAD~1' to undo the commit, then re-commit with only your allowed files.
Your role (<role>) can write to: <allowed_patterns>
```

**Source:** `shared/egg_restrictions/patterns.py` (canonical patterns), `gateway/agent_restrictions.py` (push-time enforcement)

## Agent File Patterns

Each agent role has an `AgentFilePattern` that defines what it can write:

| Role | Allowed | Blocked |
|------|---------|---------|
| `coder` | Source code (`**/*.py`, `**/*.ts`, etc.), config files | `docs/`, `**/*.md`, `tests/`, `**/conftest.py` |
| `tester` | `tests/`, `**/test_*.py`, `**/*.test.ts`, `**/conftest.py` | `docs/`, `**/*.md` |
| `documenter` | `docs/`, `**/*.md`, `**/README.md` | Source code, test files |
| `reviewer_*` | `.egg-state/reviews/`, `.egg-state/agent-outputs/` | All source, docs, tests |
| `autofixer` | Source code, config files | `docs/`, `**/*.md` |

All roles have access to `.egg-state/agent-outputs/` for handoff data. For the complete list, see [Agent Roles Reference](../reference/agent-roles.md) or `shared/egg_restrictions/patterns.py`.

**Pattern precedence:** Blocked patterns are checked **first**. This prevents bypass via broad allowed patterns (e.g., `.egg-state/agent-outputs/` is allowed, but `.egg-state/contracts/` is blocked and takes precedence).

## Recovery When a Push is Rejected

When the gateway rejects a push, follow this sequence:

### Step 1: Understand What Was Blocked

The error message lists the specific files that violated the role boundary and shows your allowed patterns. Read it carefully.

### Step 2: Fix the Commit

```bash
# Undo the last commit (keeps files in working directory)
git reset HEAD~1

# Stage only the files you're allowed to push
git add <allowed-files>

# Recommit with only allowed files
git commit -m "Description of changes"
```

### Step 3: Push Again

```bash
git push origin HEAD
```

### Step 4: If Push Still Fails

If you've stripped out-of-scope files and the push still fails, signal the error:

```bash
egg-orch signal error --error "Push failed after scope correction: <details>" --recoverable
```

**Do NOT** push to a different branch name — the pipeline tracks commits on the assigned branch.

## Agent Prompt Boundaries

Each agent receives its file scope boundaries in its spawn prompt. The `## File Boundaries (Gateway-Enforced)` section explicitly lists allowed and blocked patterns so the agent knows its limits before starting work.

Example (tester agent):
```
## File Boundaries (Gateway-Enforced)

Your role (TESTER) can only push changes to files matching these patterns.
The gateway will **reject your push** if it includes files outside your boundaries.

**Allowed:** `tests/`, `test/`, `**/tests/`, `**/test/`, test file patterns, `**/conftest.py`
**Blocked:** `docs/`, `**/README.md`, `**/*.md`, `.egg-state/contracts/`
```

This prompt reinforcement works together with the enforcement layers — agents know their boundaries upfront and are redirected immediately if they try to cross them.

## Preventing Wasted Work

The most common cause of wasted context is an agent spending tokens on out-of-scope work before discovering the boundary at push time. The defense-in-depth layers prevent this:

1. **SDK interception** catches most violations instantly (Layer 1)
2. **Commit-time validation** catches Bash-based writes before they accumulate (Layer 2)
3. **Prompt boundaries** inform agents of their scope before they start (proactive)
4. **Enriched error messages** provide specific remediation steps (reactive)

If an agent does produce out-of-scope files, the enriched push rejection tells it exactly how to recover rather than leaving it to improvise — preventing retry loops.

## Configuration

### Enforcement Mode

The gateway enforces role-based file restrictions by default. To switch to warn-only mode (for migration or debugging):

```bash
export EGG_AGENT_RESTRICTIONS_ENFORCE=false
```

### Pattern Source

All patterns are defined in `shared/egg_restrictions/patterns.py`. The `AGENT_PATTERNS` registry maps each `AgentRole` to its `AgentFilePattern`. Both the gateway and the SDK tool interceptor import from this single source.

## Related Documentation

- [Agent Roles Reference](../reference/agent-roles.md) — Complete role definitions and file permissions
- [Gateway README](../../gateway/README.md) — Push policy rules and API endpoints
- [Agent Recovery Reference](../reference/agent-recovery.md) — Retry manager, circuit breaker, conflict detection
- [Concurrent Execution Guide](concurrent-execution.md) — BRC consensus and per-agent worktree isolation
- [Git Isolation Architecture](../architecture/git-isolation.md) — Worktree isolation and credential separation
